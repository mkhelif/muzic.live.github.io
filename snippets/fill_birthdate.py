#!/usr/bin/env python3
"""
Fill missing birth/death dates for `type: person` artists from MusicBrainz.

MusicBrainz exposes, for artists of type ``Person``, a ``life-span`` with a
``begin`` (date of birth) and ``end`` (date of death). This script looks those
up on the internet (MusicBrainz web service, no API key) and writes them into
the fiche front matter as::

    date:
      birth: <date of birth>
      death: <date of death>

It is intentionally conservative (same spirit as ``fill_spotify.py``):
- it only edits ``content/artists/*/index.md`` fiches whose ``type`` is
  ``person``;
- it skips fiches that already have a top-level ``date:`` block;
- it requires exactly one exact MusicBrainz ``Person`` match (else it skips);
- it only writes the keys it actually found (``death`` is omitted for the
  living); if nothing is found, it writes nothing at all.

Examples:
  python3 snippets/fill_birthdate.py --dry-run --limit 25
  python3 snippets/fill_birthdate.py --from-slug marcus-miller --limit 100
  python3 snippets/fill_birthdate.py --insecure-ssl
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARTISTS_DIR = ROOT / "content" / "artists"

DEFAULT_CACHE_DIR = Path("/tmp/muzic_live_musicbrainz_lifespan_cache")
DEFAULT_REPORT = Path("/tmp/muzic_live_artist_birthdate_report.tsv")
USER_AGENT = "MuzicLiveResearch/1.0 (birthdate enrichment)"

# Accept MusicBrainz partial dates: YYYY, YYYY-MM or YYYY-MM-DD.
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


@dataclass(frozen=True)
class ArtistFile:
    path: Path
    slug: str
    title: str
    text: str


def normalize(value: str | None) -> str:
    value = (value or "").strip().replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", value).casefold()


def split_frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def frontmatter_value(frontmatter: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*?)\s*$", frontmatter, re.M)
    return unquote_yaml_scalar(match.group(1)) if match else ""


def has_date_block(frontmatter: str) -> bool:
    return re.search(r"^date:", frontmatter, re.M) is not None


def load_person_artists(from_slug: str | None = None) -> list[ArtistFile]:
    artists: list[ArtistFile] = []
    for path in sorted(ARTISTS_DIR.glob("*/index.md")):
        slug = path.parent.name
        if from_slug and slug < from_slug:
            continue
        text = path.read_text(encoding="utf-8")
        split = split_frontmatter(text)
        if not split:
            continue
        frontmatter, _body = split
        if frontmatter_value(frontmatter, "type") != "person":
            continue
        if has_date_block(frontmatter):
            continue
        title = frontmatter_value(frontmatter, "title")
        if not title:
            continue
        artists.append(ArtistFile(path=path, slug=slug, title=title, text=text))
    return artists


def cache_path(cache_dir: Path, namespace: str, key: str) -> Path:
    safe = urllib.parse.quote(key, safe="")
    directory = cache_dir / namespace
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe}.json"


def request_json(
    url: str,
    cache_file: Path,
    delay_seconds: float,
    ssl_context: ssl.SSLContext | None,
    retries: int,
) -> dict[str, Any]:
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=35, context=ssl_context) as response:
                data = json.load(response)
            cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            time.sleep(delay_seconds)
            return data
        except urllib.error.HTTPError as error:
            if error.code in (503, 429):
                time.sleep(10 + attempt * 10)
                continue
            raise
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(5 + attempt * 5)
    raise RuntimeError(f"Could not fetch {url}")


def search_musicbrainz(
    artist: ArtistFile,
    cache_dir: Path,
    delay_seconds: float,
    ssl_context: ssl.SSLContext | None,
    retries: int,
) -> dict[str, Any]:
    query = f'artist:"{artist.title.replace(chr(34), chr(92) + chr(34))}" AND type:person'
    url = "https://musicbrainz.org/ws/2/artist/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": "25"}
    )
    return request_json(
        url=url,
        cache_file=cache_path(cache_dir, "search", artist.title),
        delay_seconds=delay_seconds,
        ssl_context=ssl_context,
        retries=retries,
    )


def exact_person_candidates(
    artist: ArtistFile,
    search_data: dict[str, Any],
    require_score_100: bool,
) -> list[dict[str, Any]]:
    target = normalize(artist.title)
    candidates: dict[str, dict[str, Any]] = {}

    for candidate in search_data.get("artists") or []:
        score = int(candidate.get("score") or 0)
        if require_score_100 and score != 100:
            continue
        if not require_score_100 and score < 95:
            continue
        if candidate.get("type") != "Person":
            continue

        names = {candidate.get("name"), candidate.get("sort-name")}
        for alias in candidate.get("aliases") or []:
            names.add(alias.get("name"))
            names.add(alias.get("sort-name"))

        if any(normalize(name) == target for name in names if name):
            candidates[candidate["id"]] = candidate

    return list(candidates.values())


def life_span(candidate: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (birth, death) MusicBrainz dates for a Person, validated."""
    span = candidate.get("life-span") or {}
    begin = (span.get("begin") or "").strip()
    end = (span.get("end") or "").strip()
    birth = begin if _DATE_RE.match(begin) else None
    death = end if _DATE_RE.match(end) else None
    return birth, death


def set_dates(text: str, birth: str | None, death: str | None) -> str | None:
    """Insert a ``date:`` block (before the front-matter closing ``---``),
    writing only the keys found. Returns None if nothing to write or a block is
    already present."""
    if not birth and not death:
        return None
    split = split_frontmatter(text)
    if not split:
        return None
    frontmatter, body = split
    if has_date_block(frontmatter):
        return None

    lines = ["date:"]
    if birth:
        lines.append(f"  birth: {birth}")
    if death:
        lines.append(f"  death: {death}")

    new_frontmatter = frontmatter.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    return f"---{new_frontmatter}---{body}"


def process_artist(
    artist: ArtistFile,
    cache_dir: Path,
    delay_seconds: float,
    ssl_context: ssl.SSLContext | None,
    retries: int,
    dry_run: bool,
    require_score_100: bool,
) -> tuple[str, str, str, str, str]:
    search_data = search_musicbrainz(
        artist=artist,
        cache_dir=cache_dir,
        delay_seconds=delay_seconds,
        ssl_context=ssl_context,
        retries=retries,
    )
    candidates = exact_person_candidates(artist, search_data, require_score_100)
    if len(candidates) != 1:
        return ("skip", "", "", "", f"exact_candidates={len(candidates)}")

    candidate = candidates[0]
    birth, death = life_span(candidate)
    if not birth and not death:
        return ("skip", "", "", candidate.get("id", ""), "no_life_span")

    new_text = set_dates(artist.text, birth, death)
    if not new_text or new_text == artist.text:
        return ("skip", birth or "", death or "", candidate.get("id", ""), "date_block_present")

    if not dry_run:
        artist.path.write_text(new_text, encoding="utf-8")
    return ("update", birth or "", death or "", candidate.get("id", ""), "dry_run" if dry_run else "ok")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill content/artists date.birth / date.death for type:person from MusicBrainz."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report updates without writing files.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N artists.")
    parser.add_argument("--from-slug", help="Resume from a specific artist folder slug.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help=f"Cache directory. Default: {DEFAULT_CACHE_DIR}")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help=f"TSV report path. Default: {DEFAULT_REPORT}")
    parser.add_argument("--delay", type=float, default=1.1, help="Delay after uncached MusicBrainz requests. Default: 1.1")
    parser.add_argument("--retries", type=int, default=3, help="Network retries per request. Default: 3")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable SSL verification if local certificates are broken.")
    parser.add_argument("--allow-score-95", action="store_true", help="Allow MusicBrainz score >= 95 instead of requiring 100.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ssl_context = ssl._create_unverified_context() if args.insecure_ssl else None
    artists = load_person_artists(from_slug=args.from_slug)
    if args.limit:
        artists = artists[: args.limit]

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    updated = skipped = errors = 0
    print(f"person_artists_to_process={len(artists)}")
    print(f"dry_run={args.dry_run}")

    with args.report.open("w", encoding="utf-8", newline="") as report_file:
        writer = csv.writer(report_file, delimiter="\t")
        writer.writerow(["title", "slug", "path", "decision", "birth", "death", "musicbrainz_id", "reason"])

        for index, artist in enumerate(artists, start=1):
            try:
                decision, birth, death, mbid, reason = process_artist(
                    artist=artist,
                    cache_dir=args.cache_dir,
                    delay_seconds=args.delay,
                    ssl_context=ssl_context,
                    retries=args.retries,
                    dry_run=args.dry_run,
                    require_score_100=not args.allow_score_95,
                )
                if decision == "update":
                    updated += 1
                    print(f"+ {artist.title} -> birth={birth or '-'} death={death or '-'}", flush=True)
                else:
                    skipped += 1
            except ssl.SSLCertVerificationError as error:
                print(
                    "SSL certificate verification failed. Re-run with --insecure-ssl "
                    "if you trust this network.",
                    file=sys.stderr,
                )
                raise error
            except KeyboardInterrupt:
                print("\nInterrupted; re-run to resume (cached requests are reused).", file=sys.stderr)
                return 130
            except Exception as error:
                decision, birth, death, mbid, reason = "error", "", "", "", repr(error)
                errors += 1

            writer.writerow([artist.title, artist.slug, str(artist.path.relative_to(ROOT)), decision, birth, death, mbid, reason])
            report_file.flush()

            if index % 25 == 0 or index == len(artists):
                print(
                    f"progress {index}/{len(artists)} updated={updated} skipped={skipped} "
                    f"errors={errors} last_slug={artist.slug}",
                    flush=True,
                )

    print("done")
    print(f"updated={updated} skipped={skipped} errors={errors}")
    print(f"report={args.report}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
