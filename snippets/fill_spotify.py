#!/usr/bin/env python3
"""
Fill missing artist Spotify IDs from MusicBrainz.

The script is intentionally conservative:
- it only edits files under content/artists/*/index.md;
- it skips artists that already have a non-empty socials.spotify value;
- it requires one exact MusicBrainz artist match;
- when the local artist has type: band/person, it requires the matching
  MusicBrainz type Group/Person;
- it requires exactly one Spotify artist URL on that MusicBrainz artist;
- ambiguous names, missing relations, multiple Spotify IDs, and network errors
  are reported but left unchanged.

Examples:
  python3 snippets/fill_artist_spotify_from_musicbrainz.py --dry-run --limit 25
  python3 snippets/fill_artist_spotify_from_musicbrainz.py --insecure-ssl
  python3 snippets/fill_artist_spotify_from_musicbrainz.py --from-slug deicide --limit 100
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

DEFAULT_CACHE_DIR = Path("/tmp/muzic_live_musicbrainz_spotify_cache")
DEFAULT_REPORT = Path("/tmp/muzic_live_artist_spotify_musicbrainz_report.tsv")
USER_AGENT = "Research/1.0"

LOCAL_TYPE_TO_MUSICBRAINZ = {
    "band": "Group",
    "person": "Person",
}

SPOTIFY_ARTIST_RE = re.compile(r"open\.spotify\.com/artist/([A-Za-z0-9]+)")


@dataclass(frozen=True)
class ArtistFile:
    path: Path
    slug: str
    title: str
    artist_type: str
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


def spotify_value(frontmatter: str) -> str:
    match = re.search(r"^\s{2}spotify:\s*(.*?)\s*$", frontmatter, re.M)
    return unquote_yaml_scalar(match.group(1)) if match else ""


def has_non_empty_spotify(frontmatter: str) -> bool:
    return bool(spotify_value(frontmatter).strip())


def load_artist_files(from_slug: str | None = None) -> list[ArtistFile]:
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
        if has_non_empty_spotify(frontmatter):
            continue
        title = frontmatter_value(frontmatter, "title")
        if not title:
            continue
        artists.append(
            ArtistFile(
                path=path,
                slug=slug,
                title=title,
                artist_type=frontmatter_value(frontmatter, "type"),
                text=text,
            )
        )
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
) -> tuple[dict[str, Any], bool]:
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8")), True

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=35, context=ssl_context) as response:
                data = json.load(response)
            cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            time.sleep(delay_seconds)
            return data, False
        except urllib.error.HTTPError as error:
            if error.code == 503 or error.code == 429:
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
) -> tuple[dict[str, Any], bool]:
    query = f'artist:"{artist.title.replace(chr(34), chr(92) + chr(34))}"'
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


def lookup_musicbrainz_artist(
    musicbrainz_id: str,
    cache_dir: Path,
    delay_seconds: float,
    ssl_context: ssl.SSLContext | None,
    retries: int,
) -> tuple[dict[str, Any], bool]:
    url = f"https://musicbrainz.org/ws/2/artist/{musicbrainz_id}?" + urllib.parse.urlencode(
        {"inc": "url-rels", "fmt": "json"}
    )
    return request_json(
        url=url,
        cache_file=cache_path(cache_dir, "lookup", musicbrainz_id),
        delay_seconds=delay_seconds,
        ssl_context=ssl_context,
        retries=retries,
    )


def exact_musicbrainz_candidates(
    artist: ArtistFile,
    search_data: dict[str, Any],
    require_score_100: bool,
) -> list[dict[str, Any]]:
    target = normalize(artist.title)
    wanted_type = LOCAL_TYPE_TO_MUSICBRAINZ.get(artist.artist_type)
    candidates: dict[str, dict[str, Any]] = {}

    for candidate in search_data.get("artists") or []:
        score = int(candidate.get("score") or 0)
        if require_score_100 and score != 100:
            continue
        if not require_score_100 and score < 95:
            continue
        if wanted_type and candidate.get("type") != wanted_type:
            continue

        names = {candidate.get("name"), candidate.get("sort-name")}
        for alias in candidate.get("aliases") or []:
            names.add(alias.get("name"))
            names.add(alias.get("sort-name"))

        if any(normalize(name) == target for name in names if name):
            candidates[candidate["id"]] = candidate

    return list(candidates.values())


def spotify_ids_from_relations(lookup_data: dict[str, Any]) -> tuple[list[str], list[str]]:
    ids: set[str] = set()
    urls: list[str] = []
    for relation in lookup_data.get("relations") or []:
        url = ((relation.get("url") or {}).get("resource") or "").strip()
        match = SPOTIFY_ARTIST_RE.search(url)
        if match:
            ids.add(match.group(1))
            urls.append(url)
    return sorted(ids), urls


def set_spotify(text: str, spotify_id: str) -> str | None:
    split = split_frontmatter(text)
    if not split:
        return None
    frontmatter, body = split

    new_frontmatter, replacements = re.subn(
        r'^(\s{2}spotify:\s*)["\']?\s*["\']?\s*$',
        rf'\1"{spotify_id}"',
        frontmatter,
        count=1,
        flags=re.M,
    )
    if replacements:
        return f"---{new_frontmatter}---{body}"

    lines = frontmatter.splitlines()
    socials_index = next((index for index, line in enumerate(lines) if line.strip() == "socials:"), None)
    if socials_index is None:
        return None

    insert_at = socials_index + 1
    for index in range(socials_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith("  "):
            break
        if re.match(r"^\s{2}[A-Za-z0-9_-]+:", line):
            insert_at = index + 1

    lines.insert(insert_at, f'  spotify: "{spotify_id}"')
    return "---\n" + "\n".join(lines) + "\n---" + body


def process_artist(
    artist: ArtistFile,
    cache_dir: Path,
    delay_seconds: float,
    ssl_context: ssl.SSLContext | None,
    retries: int,
    dry_run: bool,
    require_score_100: bool,
) -> tuple[str, str, str, str, str, str]:
    search_data, _search_cache_hit = search_musicbrainz(
        artist=artist,
        cache_dir=cache_dir,
        delay_seconds=delay_seconds,
        ssl_context=ssl_context,
        retries=retries,
    )
    candidates = exact_musicbrainz_candidates(
        artist=artist,
        search_data=search_data,
        require_score_100=require_score_100,
    )
    if len(candidates) != 1:
        return ("skip", "", "", "", "", f"exact_candidates={len(candidates)}")

    candidate = candidates[0]
    lookup_data, _lookup_cache_hit = lookup_musicbrainz_artist(
        musicbrainz_id=candidate["id"],
        cache_dir=cache_dir,
        delay_seconds=delay_seconds,
        ssl_context=ssl_context,
        retries=retries,
    )
    spotify_ids, spotify_urls = spotify_ids_from_relations(lookup_data)
    if len(spotify_ids) != 1:
        return (
            "skip",
            "",
            candidate.get("id", ""),
            candidate.get("name", ""),
            "",
            f"spotify_ids={len(spotify_ids)}",
        )

    new_text = set_spotify(artist.text, spotify_ids[0])
    if not new_text or new_text == artist.text:
        return (
            "skip",
            spotify_ids[0],
            candidate.get("id", ""),
            candidate.get("name", ""),
            spotify_urls[0] if spotify_urls else "",
            "replace_failed_or_no_socials_block",
        )

    if not dry_run:
        artist.path.write_text(new_text, encoding="utf-8")

    return (
        "update",
        spotify_ids[0],
        candidate.get("id", ""),
        candidate.get("name", ""),
        spotify_urls[0] if spotify_urls else "",
        "dry_run" if dry_run else "ok",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill missing content/artists socials.spotify values from MusicBrainz URL relations."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report updates without writing files.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N artists.")
    parser.add_argument("--from-slug", help="Resume from a specific artist folder slug.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help=f"Cache directory. Default: {DEFAULT_CACHE_DIR}")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help=f"TSV report path. Default: {DEFAULT_REPORT}")
    parser.add_argument("--delay", type=float, default=1.1, help="Delay after uncached MusicBrainz requests. Default: 1.1")
    parser.add_argument("--retries", type=int, default=3, help="Network retries per request. Default: 3")
    parser.add_argument(
        "--insecure-ssl",
        action="store_true",
        help="Disable SSL certificate verification if local Python certificates are broken.",
    )
    parser.add_argument(
        "--allow-score-95",
        action="store_true",
        help="Allow MusicBrainz score >= 95 instead of requiring 100. More productive, less strict.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ssl_context = ssl._create_unverified_context() if args.insecure_ssl else None
    artists = load_artist_files(from_slug=args.from_slug)
    if args.limit:
        artists = artists[: args.limit]

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    updated = 0
    skipped = 0
    errors = 0

    print(f"artists_to_process={len(artists)}")
    print(f"dry_run={args.dry_run}")
    print(f"cache_dir={args.cache_dir}")
    print(f"report={args.report}")

    with args.report.open("w", encoding="utf-8", newline="") as report_file:
        writer = csv.writer(report_file, delimiter="\t")
        writer.writerow(
            [
                "title",
                "slug",
                "path",
                "decision",
                "spotify_id",
                "musicbrainz_id",
                "musicbrainz_name",
                "spotify_url",
                "reason",
            ]
        )

        for index, artist in enumerate(artists, start=1):
            try:
                decision, spotify_id, mbid, mb_name, spotify_url, reason = process_artist(
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
                else:
                    skipped += 1
            except ssl.SSLCertVerificationError as error:
                print(
                    "SSL certificate verification failed. Re-run with --insecure-ssl "
                    "if you trust this network and want to continue.",
                    file=sys.stderr,
                )
                raise error
            except KeyboardInterrupt:
                print("\nInterrupted by user. Re-run the script to resume; cached requests will be reused.", file=sys.stderr)
                return 130
            except Exception as error:
                decision = "error"
                spotify_id = ""
                mbid = ""
                mb_name = ""
                spotify_url = ""
                reason = repr(error)
                errors += 1

            writer.writerow(
                [
                    artist.title,
                    artist.slug,
                    str(artist.path.relative_to(ROOT)),
                    decision,
                    spotify_id,
                    mbid,
                    mb_name,
                    spotify_url,
                    reason,
                ]
            )
            report_file.flush()

            if index % 25 == 0 or index == len(artists):
                print(
                    f"progress {index}/{len(artists)} "
                    f"updated={updated} skipped={skipped} errors={errors} last_slug={artist.slug}",
                    flush=True,
                )

    print("done")
    print(f"updated={updated}")
    print(f"skipped={skipped}")
    print(f"errors={errors}")
    print(f"report={args.report}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
