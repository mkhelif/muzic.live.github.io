#!/usr/bin/env python3
"""Fill missing artist socials, band members and birth/death dates from
MusicBrainz — in a single pass.

This replaces six scripts that each queried MusicBrainz separately for the
same artists (``fill_spotify.py``, ``fill_amazon.py``, ``fill_tidal.py``,
``fill_qobuz.py``, ``fill_members.py``, ``fill_birthdate.py``). MusicBrainz
returns all of the data those scripts needed — url relations (Spotify, Amazon
Music, Tidal, Qobuz, Apple Music, Deezer), "member of band" relations, and
life-span (birth/death) — from the very same artist lookup, so doing one
search + one lookup per artist and applying every field that's still missing
costs at most **2 requests per artist** instead of up to 12.

For every ``content/artists/*/index.md`` fiche that's still missing at least
one of these fields, this script:

1. Searches MusicBrainz (``musicbrainz.org`` — no key required) for an exact,
   unambiguous name match, narrowed by the fiche's local ``type`` (``band`` ->
   MusicBrainz ``Group``, ``person`` -> ``Person``) when known.
2. Looks that artist up once with ``inc=url-rels+artist-rels``, which returns
   url relations, "member of band" relations and life-span together.
3. Fills whichever of the following are still empty/absent, each requiring
   its own unambiguous single match:
   - ``socials.spotify`` / ``amazon`` / ``tidal`` / ``qobuz`` / ``apple`` /
     ``deezer`` (apple/deezer are a bonus here — both fill_apple.py and
     fill_deezer.py still exist and cover artists MusicBrainz doesn't)
   - ``socials.musicbrainz``: the artist's own MusicBrainz id, so future runs
     (or other scripts) can look it up directly instead of re-searching
   - ``members:`` (only for ``type: band``, from "member of band" relations;
     missing member fiches are created as minimal ``type: person`` stubs)
   - ``lifespan.start`` / ``lifespan.end`` (only for ``type: person``, from
     ``life-span``; named ``lifespan`` and not ``date``, which Hugo reserves)
4. For a ``type: person`` fiche, also reads the mirror of step 3's members
   lookup — "member of band" relations pointing *forward* from the person to
   each band they are/were in — and creates a minimal ``type: band`` stub for
   any of those bands that don't exist locally yet. That new band gets its own
   full ``members:`` list the next time this script processes it (step 3
   above), rather than being filled here to avoid a second lookup. This only
   runs when the person is already being processed for some other missing
   field (see "costs at most 2 requests" above) — a fiche with every field
   already filled won't be re-looked-up just to check for new bands.

MusicBrainz also exposes, but this script does not yet use: ``country`` /
``area`` (origin), ``gender`` (persons), ``tags`` (genres), aliases, a free-text
``disambiguation``, and — in the very same url-rels list already fetched here —
"official homepage" and "social network" relations that could fill
``socials.web`` / ``facebook`` / ``instagram`` / ``x`` / ``youtube`` too (left
out for now since those relations aren't platform-tagged and need domain-based
dispatch to resolve safely).

Safety, same spirit as the scripts it replaces:

* every field is accepted only on an exact, unambiguous match; ambiguous or
  missing data is skipped and reported, never guessed.
* set ``DRY_RUN = True`` to report proposed changes without writing or
  creating anything.
* artists searched within the last month are skipped (``lastUpdate`` key
  ``musicbrainz``), so a repeated run doesn't re-query artists MusicBrainz has
  nothing new for.

Run from the repository root::

    python3 snippets/fill_musicbrainz.py
"""

import csv
import re
import sys
import traceback
import uuid as uuid_module
from os import listdir
from pathlib import Path
from time import sleep
from urllib.parse import quote

import frontmatter
from unidecode import unidecode

import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When True, only report proposed changes; write and create nothing.
DRY_RUN = False

# Politeness delay between MusicBrainz requests (seconds). MusicBrainz's
# anonymous rate limit is ~1 request/second; utils.http_get also retries
# 429/503 with backoff.
REQUEST_DELAY = 1.1

# Process at most this many artists (0 = no limit). Handy for a first test run.
LIMIT = 0

# Front-matter key (under `lastUpdate`) recording when we last searched
# MusicBrainz for an artist.
LOOKUP_PROVIDER = "musicbrainz"

# Public MusicBrainz API (no API key needed).
SEARCH_URL = "https://musicbrainz.org/ws/2/artist/?query={q}&fmt=json&limit=25"
LOOKUP_URL = "https://musicbrainz.org/ws/2/artist/{mbid}?inc=url-rels+artist-rels&fmt=json"

LOCAL_TYPE_TO_MUSICBRAINZ = {"band": "Group", "person": "Person"}

# URL relation patterns for each streaming socials field, matched against
# every url-rels relation regardless of its MusicBrainz relationship type
# (e.g. "streaming" vs "free streaming" vs "purchase for download").
SOCIAL_PATTERNS = {
    "spotify": re.compile(r"open\.spotify\.com/artist/([A-Za-z0-9]+)"),
    "amazon": re.compile(r"music\.amazon\.[a-z.]+/artists/([A-Za-z0-9]+)"),
    "tidal": re.compile(r"tidal\.com/(?:browse/)?artist/(\d+)"),
    "qobuz": re.compile(
        r"qobuz\.com/(?:[a-z]{2}-[a-z]{2}/)?(?:interpreter|artist)/(?:[^/?]+/)?(\d+)(?:[/?]|$)"
    ),
    # Both the legacy itunes.apple.com/<cc>/artist/id<digits> and the current
    # music.apple.com/<cc>/artist/<digits> forms resolve to the same numeric id.
    "apple": re.compile(r"(?:music|itunes)\.apple\.com/[a-z]{2}/artist/(?:id)?(\d+)"),
    "deezer": re.compile(r"deezer\.com/(?:[a-z]{2}/)?artist/(\d+)"),
}

# Socials keys filled straight from url-rels (SOCIAL_PATTERNS), plus
# "musicbrainz" which is filled from the artist's own MusicBrainz id rather
# than a relation.
ALL_SOCIAL_KEYS = list(SOCIAL_PATTERNS) + ["musicbrainz"]

# Order socials keys appear in when a fiche is created (see
# utils.get_or_create_artist / PERSON_TEMPLATE below), used to anchor a
# newly-filled key's insertion point when it's entirely absent.
SOCIAL_ORDER = [
    "facebook", "instagram", "tiktok", "threads", "x", "bandsintown",
    "youtube", "web", "email", "amazon", "apple", "deezer", "qobuz",
    "spotify", "tidal", "musicbrainz",
]

# Accept MusicBrainz partial dates: YYYY, YYYY-MM or YYYY-MM-DD.
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

ROLE_ORDER = ["sing", "guitar", "bass", "drums", "keys", "other"]

# Optional exhaustive instrument -> role mapping, generated by
# export_instruments.py and editable by hand. Falls back to keyword
# heuristics for anything not listed.
INSTRUMENT_ROLES_CSV = Path(__file__).resolve().parent / "instrument_roles.csv"
VALID_ROLES = set(ROLE_ORDER)
_instrument_roles = None

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
  musicbrainz: ""
todo:
  - Add picture
  - Add socials
  - Add description
---
"""

BAND_TEMPLATE = """\
---
id: "{id}"
title: "{title}"
type: band
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
  musicbrainz: ""
todo:
  - Add picture
  - Add socials
  - Add description
  - Add members
---
"""


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def normalize(value):
    """Collapse a name to comparable form: ascii, lowercase, alphanumerics only
    (so "Kölsch" == "kolsch", "A Perfect Circle" == "aperfectcircle")."""
    return re.sub(r"[^a-z0-9]", "", unidecode(value or "").lower())


# ---------------------------------------------------------------------------
# MusicBrainz: one search + one lookup per artist
# ---------------------------------------------------------------------------

def find_musicbrainz_id(name, local_type):
    """Return the single MusicBrainz id whose name/alias is an exact,
    100-score match for ``name`` (optionally narrowed by ``local_type``), or
    ``None`` when zero or several match."""
    target = normalize(name)
    if not target:
        return None

    query = f'artist:"{name.replace(chr(34), chr(92) + chr(34))}"'
    wanted_type = LOCAL_TYPE_TO_MUSICBRAINZ.get(local_type)
    if wanted_type:
        query += f" AND type:{wanted_type.lower()}"

    response = utils.http_get(SEARCH_URL.format(q=quote(query)))
    if not response.ok:
        return None
    try:
        data = response.json()
    except ValueError:
        return None

    candidates = {}
    for candidate in data.get("artists") or []:
        if int(candidate.get("score") or 0) != 100:
            continue
        if wanted_type and candidate.get("type") != wanted_type:
            continue
        names = {candidate.get("name"), candidate.get("sort-name")}
        for alias in candidate.get("aliases") or []:
            names.add(alias.get("name"))
            names.add(alias.get("sort-name"))
        if any(normalize(n) == target for n in names if n):
            candidates[candidate["id"]] = candidate

    return next(iter(candidates)) if len(candidates) == 1 else None


def lookup_musicbrainz(mbid):
    """Fetch one artist with url-rels + artist-rels + life-span (the latter is
    core data, always included)."""
    response = utils.http_get(LOOKUP_URL.format(mbid=mbid))
    if not response.ok:
        return None
    try:
        return response.json()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Socials extraction
# ---------------------------------------------------------------------------

def extract_socials(relations):
    """Return ``{provider: (id, status)}`` for every provider in
    SOCIAL_PATTERNS. status is "ok" (single match), "none" or "ambiguous"."""
    ids_by_provider = {provider: set() for provider in SOCIAL_PATTERNS}
    for relation in relations:
        if relation.get("target-type") != "url":
            continue
        url = (relation.get("url") or {}).get("resource") or ""
        for provider, pattern in SOCIAL_PATTERNS.items():
            match = pattern.search(url)
            if match:
                ids_by_provider[provider].add(match.group(1))

    result = {}
    for provider, ids in ids_by_provider.items():
        if len(ids) == 1:
            result[provider] = (ids.pop(), "ok")
        elif len(ids) > 1:
            result[provider] = (None, "ambiguous")
        else:
            result[provider] = (None, "none")
    return result


# ---------------------------------------------------------------------------
# Members extraction (band -> person relations)
# ---------------------------------------------------------------------------

def get_instrument_roles():
    global _instrument_roles
    if _instrument_roles is None:
        _instrument_roles = {}
        if INSTRUMENT_ROLES_CSV.exists():
            with INSTRUMENT_ROLES_CSV.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    name = (row.get("instrument") or "").strip().lower()
                    role = (row.get("role") or "").strip().lower()
                    if name and role in VALID_ROLES:
                        _instrument_roles[name] = role
    return _instrument_roles


def map_role(attribute):
    """Map a MusicBrainz instrument/attribute to the project role vocabulary,
    preferring the instrument_roles.csv mapping when available."""
    a = attribute.lower().strip()
    mapped = get_instrument_roles().get(a)
    if mapped:
        return mapped
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


def year_of(value):
    value = (value or "").strip()
    return int(value[:4]) if _DATE_RE.match(value) else None


def extract_band_memberships(relations):
    """Return the distinct band names a ``type: person`` artist is/was a
    member of — the mirror of extract_members(), read from the person's own
    lookup where "member of band" relations point *forward* to the band."""
    seen = set()
    bands = []
    for relation in relations:
        if relation.get("type") != "member of band":
            continue
        if relation.get("direction") != "forward":
            continue
        band = relation.get("artist") or {}
        name = (band.get("name") or "").strip()
        mbid = band.get("id")
        if not name or mbid in seen:
            continue
        seen.add(mbid)
        bands.append(name)
    return bands


def extract_members(relations):
    """Aggregate 'member of band' relations by person: union of roles, list of
    periods (most recent first). Returns [] when there are none."""
    people = {}
    order = []

    for relation in relations:
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

        # Qualifier attributes are not roles (see the MusicBrainz "member of
        # band" relationship definition); only vocal/instrument values are.
        qualifiers = {"original", "additional", "eponymous", "principal", "minor"}
        roles = {
            map_role(a)
            for a in relation.get("attributes") or []
            if a.lower() not in qualifiers
        }
        people[key]["roles"].update(roles or set())

        start = year_of(relation.get("begin"))
        end = year_of(relation.get("end")) if relation.get("ended") else None
        if start is not None:
            people[key]["periods"].append({"start": start, "end": end})
        elif end is not None:
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


def render_members(members, ids):
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


# ---------------------------------------------------------------------------
# Local artist index (title + aliases -> id), for resolving/creating members
# and band memberships. Generic across type: band/person fiches.
# ---------------------------------------------------------------------------

_artist_index = None


def build_artist_index():
    index = {}
    for path in sorted(Path("./content/artists").glob("*/index.md")):
        try:
            data = frontmatter.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        artist_id = data.get("id")
        if not artist_id:
            continue
        keys = [data.get("title")]
        aliases = data.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        keys.extend(aliases)
        for key in keys:
            if key:
                index.setdefault(normalize(key), str(artist_id))
    return index


def get_artist_index():
    global _artist_index
    if _artist_index is None:
        _artist_index = build_artist_index()
    return _artist_index


def _get_or_create_stub(name, template):
    """Return ``(artist_id, created)``, reusing an existing fiche by
    title/alias and creating a minimal stub from ``template`` otherwise."""
    index = get_artist_index()
    existing = index.get(normalize(name))
    if existing:
        return existing, False

    directory = Path(f"./content/artists/{utils.format_filename(name)}")
    file = directory.joinpath("index.md")
    if file.exists():
        found = frontmatter.loads(file.read_text(encoding="utf-8")).get("id")
        if found:
            index[normalize(name)] = str(found)
            return str(found), False

    new_id = str(uuid_module.uuid4())
    if not DRY_RUN:
        directory.mkdir(parents=True, exist_ok=True)
        file.write_text(
            template.format(id=new_id, title=utils.yaml_quote(name)),
            encoding="utf-8",
        )
    index[normalize(name)] = new_id
    return new_id, True


def get_or_create_person(name):
    """Return ``(person_id, created)``, reusing an existing fiche by
    title/alias and creating a minimal ``type: person`` stub otherwise."""
    return _get_or_create_stub(name, PERSON_TEMPLATE)


def get_or_create_band(name):
    """Return ``(band_id, created)``, reusing an existing fiche by
    title/alias and creating a minimal ``type: band`` stub otherwise (its own
    members are filled the next time this script processes that band)."""
    return _get_or_create_stub(name, BAND_TEMPLATE)


# ---------------------------------------------------------------------------
# Writing fields into the fiche (minimal-diff, regex-based)
# ---------------------------------------------------------------------------

def add_social(text, key, value):
    """Set ``key: "<value>"`` in the fiche's socials block.

    Fills the existing (empty) key when present; otherwise inserts a line
    before the first subsequent key from SOCIAL_ORDER that's actually present
    (or as the first socials child), or falls back to a frontmatter
    round-trip for inline ``socials: { ... }``."""
    if re.search(rf'^\s*{re.escape(key)}:\s*["\']?[^"\'\s].*$', text, re.MULTILINE):
        return text, False  # already has a value

    new_text, count = re.subn(
        rf'^(\s*){re.escape(key)}:\s*(?:""|\'\')?\s*$',
        rf'\g<1>{key}: "{value}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count:
        return new_text, True

    following = SOCIAL_ORDER[SOCIAL_ORDER.index(key) + 1:] if key in SOCIAL_ORDER else []
    for next_key in following:
        anchor = re.search(rf"^([ \t]+){re.escape(next_key)}:.*\n", text, re.MULTILINE)
        if anchor:
            idx = anchor.start()
            indent = anchor.group(1)
            return text[:idx] + f'{indent}{key}: "{value}"\n' + text[idx:], True

    socials = re.search(r"^socials:[ \t]*\n", text, re.MULTILINE)
    if socials:
        idx = socials.end()
        return text[:idx] + f'  {key}: "{value}"\n' + text[idx:], True

    post = frontmatter.loads(text)
    block = post.get("socials")
    if not isinstance(block, dict):
        return text, False
    block[key] = value
    post["socials"] = block
    return frontmatter.dumps(post) + "\n", True


def has_members_block(text):
    return re.search(r"^members:", text, re.MULTILINE) is not None


def insert_members(text, block):
    """Insert the members block just before ``socials:`` (or before the
    closing front-matter ``---`` as a fallback)."""
    match = re.search(r"^socials:[ \t]*\n", text, re.MULTILINE)
    if match:
        return text[:match.start()] + block + text[match.start():]
    closing = re.search(r"\n---[ \t]*(?:\n|$)", text)
    if not closing:
        return None
    return text[:closing.start() + 1] + block + text[closing.start() + 1:]


def has_lifespan_block(text):
    return re.search(r"^lifespan:", text, re.MULTILINE) is not None


def insert_lifespan(text, start, end):
    """Insert a ``lifespan:`` block before the closing front-matter ``---``,
    writing only the keys found. Returns None if there's nothing to write.

    The block is ``lifespan: {start, end}`` — *not* ``date:``, which Hugo
    reserves for the page's own date and would misparse as a map."""
    if not start and not end:
        return None
    closing = re.search(r"\n---[ \t]*(?:\n|$)", text)
    if not closing:
        return None
    lines = ["lifespan:"]
    if start:
        lines.append(f"  start: {start}")
    if end:
        lines.append(f"  end: {end}")
    block = "\n".join(lines) + "\n"
    return text[:closing.start() + 1] + block + text[closing.start() + 1:]


# ---------------------------------------------------------------------------
# Candidate collection
# ---------------------------------------------------------------------------

def load_candidates():
    """Return artists with at least one still-missing field, skipping those
    searched within the last month."""
    candidates = []
    skipped_complete = skipped_fresh = parse_errors = 0
    for slug in sorted(listdir("./content/artists")):
        file = Path(f"./content/artists/{slug}/index.md")
        if not file.exists():
            continue
        try:
            text = file.read_text(encoding="utf-8")
            data = frontmatter.loads(text)
        except Exception:
            print(f"! {slug}: cannot parse front matter")
            parse_errors += 1
            continue

        title = data.get("title")
        if not title:
            continue
        artist_type = data.get("type")
        socials = data.get("socials")
        socials = socials if isinstance(socials, dict) else {}

        needs_social = {p for p in ALL_SOCIAL_KEYS if not socials.get(p)}
        needs_members = artist_type == "band" and not has_members_block(text)
        needs_dates = artist_type == "person" and not has_lifespan_block(text)

        if not needs_social and not needs_members and not needs_dates:
            skipped_complete += 1
            continue
        if not utils.is_stale(data, LOOKUP_PROVIDER):
            skipped_fresh += 1
            continue

        candidates.append((file, title, artist_type, needs_social, needs_members, needs_dates))

    return candidates, skipped_complete, skipped_fresh, parse_errors


# ---------------------------------------------------------------------------
# Per-artist processing
# ---------------------------------------------------------------------------

def process_artist(file, title, artist_type, needs_social, needs_members, needs_dates):
    """Return a list of "+"-prefixed change descriptions (empty if nothing
    was found/written)."""
    mbid = find_musicbrainz_id(title, artist_type)
    if mbid is None:
        return []

    sleep(REQUEST_DELAY)
    data = lookup_musicbrainz(mbid)
    sleep(REQUEST_DELAY)
    if data is None:
        return []

    changes = []
    text = file.read_text(encoding="utf-8")
    relations = data.get("relations") or []

    if needs_social:
        socials = extract_socials(relations)
        socials["musicbrainz"] = (mbid, "ok")  # the id we already confirmed unambiguous
        for provider in needs_social:
            value, status = socials.get(provider, (None, "none"))
            if status != "ok":
                continue
            new_text, changed = add_social(text, provider, value)
            if changed:
                text = new_text
                changes.append(f"{provider}={value}")

    if needs_members:
        members = extract_members(relations)
        if members:
            ids = {}
            created_names = []
            for member in members:
                person_id, created = get_or_create_person(member["name"])
                ids[member["name"]] = person_id
                if created:
                    created_names.append(member["name"])
            new_text = insert_members(text, render_members(members, ids))
            if new_text:
                text = new_text
                suffix = f", created {len(created_names)} fiches" if created_names else ""
                changes.append(f"members={len(members)}{suffix}")

    if needs_dates:
        span = data.get("life-span") or {}
        begin = (span.get("begin") or "").strip()
        finish = (span.get("end") or "").strip()
        start = begin if _DATE_RE.match(begin) else None
        end = finish if _DATE_RE.match(finish) else None
        if start or end:
            new_text = insert_lifespan(text, start, end)
            if new_text:
                text = new_text
                changes.append(f"lifespan=start:{start or '-'}/end:{end or '-'}")

    # For persons, also create stub fiches for any band they're a member of
    # that doesn't exist locally yet — the mirror of what needs_members does
    # for bands. Each new band gets its own full members list the next time
    # this script processes it (needs_members will be true for its fresh,
    # empty members block).
    if artist_type == "person":
        created_bands = []
        for band_name in extract_band_memberships(relations):
            _, created = get_or_create_band(band_name)
            if created:
                created_bands.append(band_name)
        if created_bands:
            changes.append(f"created_bands={len(created_bands)} ({', '.join(created_bands)})")

    if changes and not DRY_RUN:
        file.write_text(text, encoding="utf-8")

    return changes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    candidates, skipped_complete, skipped_fresh, parse_errors = load_candidates()
    if LIMIT:
        candidates = candidates[:LIMIT]

    total = len(candidates)
    print(
        f"Artists to check on MusicBrainz: {total} "
        f"(nothing missing: {skipped_complete}, "
        f"searched within the last month: {skipped_fresh}). "
        f"Mode: {'DRY-RUN' if DRY_RUN else 'WRITE'}."
    )
    if total == 0:
        print("Nothing to do.")
        return

    filled = no_match = errors = 0
    for index, (file, title, artist_type, needs_social, needs_members, needs_dates) in enumerate(
        candidates, start=1
    ):
        prefix = f"[{index}/{total}]"
        try:
            changes = process_artist(
                file, title, artist_type, needs_social, needs_members, needs_dates
            )
        except utils.CloudflareBlocked as exc:
            print(f"\n{exc}")
            return
        except Exception:
            print(f"{prefix} ! {title}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
            continue
        finally:
            if not DRY_RUN:
                utils.set_last_update(file, LOOKUP_PROVIDER)

        if changes:
            tag = "[dry-run] " if DRY_RUN else "+ "
            print(f"{prefix} {tag}{title}: {', '.join(changes)}")
            filled += 1
        else:
            print(f"{prefix} - {title}: no match")
            no_match += 1

    print(
        "\nDone. "
        f"{'would update' if DRY_RUN else 'updated'}={filled}, "
        f"no_match={no_match}, "
        f"already_complete={skipped_complete}, searched_recently={skipped_fresh}, "
        f"errors={errors + parse_errors}"
    )
    if DRY_RUN and filled:
        print("DRY_RUN is on — set DRY_RUN = False to write these changes.")


if __name__ == "__main__":
    main()
