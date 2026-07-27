#!/usr/bin/env python3
"""
Fill missing `members` for `type: band` artists from MusicBrainz.

For every ``content/artists/*/index.md`` fiche with ``type: band`` and no
``members`` block, this script looks the band up on MusicBrainz, reads its
"member of band" relations (person, begin/end years, instruments) and generates
the ``members`` block::

    members:
      - id: "<person-uuid>"
        roles:
          - guitar
        periods:
          - start: 2009
          - start: 1998
            end: 2003

Member persons are resolved against the existing fiches (title + aliases,
case-insensitive); missing persons get a minimal ``type: person`` fiche (empty
socials + todo — descriptions/socials are enriched later, per the project
conventions).

MusicBrainz instruments are mapped onto the project's role vocabulary
(``sing``, ``guitar``, ``bass``, ``drums``, ``keys``, ``other``); periods are
ordered most-recent-first; members with an open period come first.

It is intentionally conservative (same spirit as ``fill_spotify.py`` /
``fill_birthdate.py``):
- requires exactly one exact MusicBrainz ``Group`` match (else skip);
- skips bands whose fiche already has a ``members`` block;
- skips bands whose MusicBrainz entry has no member relations;
- ``--dry-run`` reports without writing anything (no fiche creation either).

Examples:
  python3 snippets/fill_members.py --dry-run --limit 10
  python3 snippets/fill_members.py --from-slug gojira --limit 50
  python3 snippets/fill_members.py --insecure-ssl
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
import uuid as uuid_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unidecode import unidecode

ROOT = Path(__file__).resolve().parents[1]
ARTISTS_DIR = ROOT / "content" / "artists"

DEFAULT_CACHE_DIR = Path("/tmp/muzic_live_musicbrainz_members_cache")
DEFAULT_REPORT = Path("/tmp/muzic_live_band_members_report.tsv")
USER_AGENT = "MuzicLiveResearch/1.0 (band members enrichment)"

# Accept MusicBrainz partial dates: YYYY, YYYY-MM or YYYY-MM-DD.
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

PERSON_TEMPLATE = """\
---
id: "{id}"
title: "{title}"
type: person
socials:
  facebook: ""
  instagram: ""
  tiktok: ""
  threads: ""
  x: ""
  youtube: ""
  web: ""
  email: ""
  amazon: ""
  apple: ""
  deezer: ""
  qobuz: ""
  spotify: ""
  tidal: ""
todo:
  - Add picture
  - Add socials
  - Add description
---
"""

ROLE_ORDER = ["sing", "guitar", "bass", "drums", "keys", "other"]


@dataclass(frozen=True)
class BandFile:
    path: Path
    slug: str
    title: str
    text: str


# ---------------------------------------------------------------------------
# Front matter helpers (textual, minimal-diff)
# ---------------------------------------------------------------------------

def normalize(value: str | None) -> str:
    value = (value or "").strip().replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", value).casefold()


def format_filename(name: str) -> str:
    return re.sub("-{2,}", "-", re.sub("[^a-z0-9]", "-", unidecode(name).lower()))


def yaml_quote(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


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


def frontmatter_aliases(frontmatter: str) -> list[str]:
    match = re.search(r"^aliases:\s*\n((?:[ \t]+-[^\n]*\n)+)", frontmatter, re.M)
    if not match:
        return []
    return [
        unquote_yaml_scalar(line.strip()[1:].strip())
        for line in match.group(1).splitlines()
        if line.strip().startswith("-")
    ]


def has_members_block(frontmatter: str) -> bool:
    return re.search(r"^members:", frontmatter, re.M) is not None


# ---------------------------------------------------------------------------
# Local artist index (title + aliases -> id), and person creation
# ---------------------------------------------------------------------------

_person_index: dict[str, str] | None = None


def build_person_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for path in sorted(ARTISTS_DIR.glob("*/index.md")):
        split = split_frontmatter(path.read_text(encoding="utf-8"))
        if not split:
            continue
        frontmatter, _body = split
        artist_id = frontmatter_value(frontmatter, "id")
        if not artist_id:
            continue
        keys = [frontmatter_value(frontmatter, "title")]
        keys.extend(frontmatter_aliases(frontmatter))
        for key in keys:
            if key:
                index.setdefault(normalize(key), artist_id)
    return index


def get_person_index() -> dict[str, str]:
    global _person_index
    if _person_index is None:
        _person_index = build_person_index()
    return _person_index


def get_or_create_person(name: str, dry_run: bool) -> tuple[str, bool]:
    """Return ``(person_id, created)``, reusing existing fiches by title/alias
    and creating a minimal ``type: person`` fiche otherwise."""
    index = get_person_index()
    existing = index.get(normalize(name))
    if existing:
        return existing, False

    directory = ARTISTS_DIR / format_filename(name)
    file = directory / "index.md"
    if file.exists():
        split = split_frontmatter(file.read_text(encoding="utf-8"))
        if split:
            found = frontmatter_value(split[0], "id")
            if found:
                index[normalize(name)] = found
                return found, False

    person_id = str(uuid_module.uuid4())
    if not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
        file.write_text(
            PERSON_TEMPLATE.format(id=person_id, title=yaml_quote(name)),
            encoding="utf-8",
        )
    index[normalize(name)] = person_id
    return person_id, True


# ---------------------------------------------------------------------------
# MusicBrainz
# ---------------------------------------------------------------------------

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
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
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


def search_group(band: BandFile, args, ssl_context) -> dict[str, Any]:
    query = f'artist:"{band.title.replace(chr(34), chr(92) + chr(34))}" AND type:group'
    url = "https://musicbrainz.org/ws/2/artist/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": "25"}
    )
    return request_json(
        url, cache_path(args.cache_dir, "search", band.title),
        args.delay, ssl_context, args.retries,
    )


def lookup_group(mbid: str, args, ssl_context) -> dict[str, Any]:
    url = f"https://musicbrainz.org/ws/2/artist/{mbid}?" + urllib.parse.urlencode(
        {"inc": "artist-rels", "fmt": "json"}
    )
    return request_json(
        url, cache_path(args.cache_dir, "lookup", mbid),
        args.delay, ssl_context, args.retries,
    )


def exact_group_candidates(
    band: BandFile, search_data: dict[str, Any], require_score_100: bool
) -> list[dict[str, Any]]:
    target = normalize(band.title)
    candidates: dict[str, dict[str, Any]] = {}
    for candidate in search_data.get("artists") or []:
        score = int(candidate.get("score") or 0)
        if require_score_100 and score != 100:
            continue
        if not require_score_100 and score < 95:
            continue
        if candidate.get("type") != "Group":
            continue
        names = {candidate.get("name"), candidate.get("sort-name")}
        for alias in candidate.get("aliases") or []:
            names.add(alias.get("name"))
            names.add(alias.get("sort-name"))
        if any(normalize(name) == target for name in names if name):
            candidates[candidate["id"]] = candidate
    return list(candidates.values())


# ---------------------------------------------------------------------------
# Members extraction
# ---------------------------------------------------------------------------

def map_role(attribute: str) -> str:
    """Map a MusicBrainz instrument/attribute to the project role vocabulary."""
    a = attribute.lower()
    if "vocal" in a or "sing" in a:
        return "sing"
    if "bass" in a:  # before guitar: "bass guitar"
        return "bass"
    if "guitar" in a or "banjo" in a or "mandolin" in a:
        return "guitar"
    if "drum" in a or "percussion" in a:
        return "drums"
    if "keyboard" in a or "piano" in a or "synth" in a or "organ" in a:
        return "keys"
    return "other"


def year_of(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value[:4]) if _DATE_RE.match(value) else None


def extract_members(lookup_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate 'member of band' relations by person: union of roles, list of
    periods (most recent first)."""
    people: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for relation in lookup_data.get("relations") or []:
        if relation.get("type") != "member of band":
            continue
        if relation.get("direction") != "backward":
            continue
        person = relation.get("artist") or {}
        name = (person.get("name") or "").strip()
        if not name:
            continue

        key = person.get("id") or name
        if key not in people:
            people[key] = {"name": name, "roles": set(), "periods": []}
            order.append(key)

        roles = {map_role(a) for a in relation.get("attributes") or []}
        people[key]["roles"].update(roles or set())

        start = year_of(relation.get("begin"))
        end = year_of(relation.get("end")) if relation.get("ended") else None
        if start is not None:
            people[key]["periods"].append({"start": start, "end": end})
        elif end is not None:
            # End without begin: keep the stint, year-level only.
            people[key]["periods"].append({"start": end, "end": end})

    members = []
    for key in order:
        entry = people[key]
        roles = [r for r in ROLE_ORDER if r in entry["roles"]] or ["other"]
        periods = sorted(entry["periods"], key=lambda p: p["start"], reverse=True)
        members.append({
            "name": entry["name"],
            "roles": roles,
            "periods": periods,
            "current": any(p["end"] is None for p in periods) if periods else False,
        })

    # Current members first, then former; stable by MusicBrainz order.
    return [m for m in members if m["current"]] + [m for m in members if not m["current"]]


def render_members(members: list[dict[str, Any]], ids: dict[str, str]) -> str:
    lines = ["members:"]
    for member in members:
        lines.append(f'  - id: "{ids[member["name"]]}"')
        lines.append("    roles:")
        for role in member["roles"]:
            lines.append(f"      - {role}")
        if member["periods"]:
            lines.append("    periods:")
            for period in member["periods"]:
                lines.append(f"      - start: {period['start']}")
                if period["end"] is not None:
                    lines.append(f"        end: {period['end']}")
    return "\n".join(lines) + "\n"


def insert_members(text: str, block: str) -> str | None:
    """Insert the members block just before the ``socials:`` line (or before the
    closing ``---`` as a fallback)."""
    match = re.search(r"^socials:[ \t]*\n", text, re.M)
    if match:
        return text[:match.start()] + block + text[match.start():]
    closing = re.search(r"\n---[ \t]*(?:\n|$)", text)
    if not closing:
        return None
    return text[:closing.start() + 1] + block + text[closing.start() + 1:]


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def load_band_files(from_slug: str | None) -> list[BandFile]:
    bands: list[BandFile] = []
    for path in sorted(ARTISTS_DIR.glob("*/index.md")):
        slug = path.parent.name
        if from_slug and slug < from_slug:
            continue
        text = path.read_text(encoding="utf-8")
        split = split_frontmatter(text)
        if not split:
            continue
        frontmatter, _body = split
        if frontmatter_value(frontmatter, "type") != "band":
            continue
        if has_members_block(frontmatter):
            continue
        title = frontmatter_value(frontmatter, "title")
        if not title:
            continue
        bands.append(BandFile(path=path, slug=slug, title=title, text=text))
    return bands


def process_band(band: BandFile, args, ssl_context) -> tuple[str, str, int, int, str]:
    """Return ``(decision, mbid, members_count, persons_created, reason)``."""
    search_data = search_group(band, args, ssl_context)
    candidates = exact_group_candidates(band, search_data, not args.allow_score_95)
    if len(candidates) != 1:
        return ("skip", "", 0, 0, f"exact_candidates={len(candidates)}")

    mbid = candidates[0]["id"]
    lookup_data = lookup_group(mbid, args, ssl_context)
    members = extract_members(lookup_data)
    if not members:
        return ("skip", mbid, 0, 0, "no_member_relations")

    created = 0
    ids: dict[str, str] = {}
    for member in members:
        person_id, was_created = get_or_create_person(member["name"], args.dry_run)
        ids[member["name"]] = person_id
        created += int(was_created)

    new_text = insert_members(band.text, render_members(members, ids))
    if not new_text or new_text == band.text:
        return ("skip", mbid, len(members), created, "insert_failed")

    if not args.dry_run:
        band.path.write_text(new_text, encoding="utf-8")
    return ("update", mbid, len(members), created, "dry_run" if args.dry_run else "ok")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill content/artists members for type:band from MusicBrainz."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report updates without writing files.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N bands.")
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
    bands = load_band_files(args.from_slug)
    if args.limit:
        bands = bands[: args.limit]

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    updated = skipped = errors = persons_created = 0
    print(f"bands_to_process={len(bands)}")
    print(f"dry_run={args.dry_run}")

    with args.report.open("w", encoding="utf-8", newline="") as report_file:
        writer = csv.writer(report_file, delimiter="\t")
        writer.writerow(["title", "slug", "path", "decision", "musicbrainz_id", "members", "persons_created", "reason"])

        for index, band in enumerate(bands, start=1):
            try:
                decision, mbid, count, created, reason = process_band(band, args, ssl_context)
                if decision == "update":
                    updated += 1
                    persons_created += created
                    print(f"+ {band.title}: {count} members ({created} new fiches)", flush=True)
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
                decision, mbid, count, created, reason = "error", "", 0, 0, repr(error)
                errors += 1

            writer.writerow([band.title, band.slug, str(band.path.relative_to(ROOT)), decision, mbid, count, created, reason])
            report_file.flush()

            if index % 25 == 0 or index == len(bands):
                print(
                    f"progress {index}/{len(bands)} updated={updated} skipped={skipped} "
                    f"errors={errors} last_slug={band.slug}",
                    flush=True,
                )

    print("done")
    print(f"updated={updated} skipped={skipped} errors={errors} persons_created={persons_created}")
    print(f"report={args.report}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
