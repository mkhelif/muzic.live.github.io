#!/usr/bin/env python3
"""Refresh artist data from MusicBrainz using the id stored in the fiche.

The companion of ``fill_musicbrainz.py``, split the same way as
``bandsintown.py`` / ``fill_bandsintown.py``:

* ``fill_musicbrainz.py`` **discovers** the id — it searches MusicBrainz by
  name for the fiches whose ``socials.musicbrainz`` is still empty;
* this script **uses** it — for every fiche that already declares
  ``socials.musicbrainz``, it goes straight to the lookup endpoint.

That direct lookup is the speed-up: a stored id turns the previous
*search + lookup* into a single request per artist, halving a run that is
bounded by MusicBrainz's ~1 request/second anonymous rate limit. It also
removes the search's failure mode entirely — no more "no match" or
"ambiguous" for artists whose id we already know, so renamed or
oddly-spelled artists keep getting updated.

One ``inc=url-rels+artist-rels`` lookup returns url relations, "member of
band" relations and life-span together, so a single request fills whichever
of these is still missing:

- ``members:`` (``type: band``, from "member of band" relations; missing
  member fiches are created as minimal ``type: person`` stubs)
- ``lifespan.start`` / ``lifespan.end`` (``type: person``, from ``life-span``;
  named ``lifespan`` and not ``date``, which Hugo reserves)
- ``socials.spotify`` / ``amazon`` / ``tidal`` / ``qobuz`` / ``apple`` /
  ``deezer``, each accepted only on an unambiguous single match
- for a ``type: person``, minimal ``type: band`` stubs for the bands they are
  a member of that don't exist locally yet

Safety, unchanged: every field is accepted only on an exact, unambiguous
match; ambiguous or missing data is skipped and reported, never guessed. Set
``DRY_RUN = True`` to report proposed changes without writing anything.
Artists refreshed within the last month are skipped (``lastUpdate`` key
``musicbrainz``).

MusicBrainz also exposes, but this script does not yet use: ``country`` /
``area`` (origin), ``gender`` (persons), ``tags`` (genres), aliases and the
free-text ``disambiguation``.

Run from the repository root::

    python3 snippets/musicbrainz.py
    python3 snippets/musicbrainz.py --refresh-members --dry-run
    python3 snippets/musicbrainz.py --refresh-members --limit 20
"""

import argparse
import csv
import re
import sys
import traceback
import uuid as uuid_module
from os import listdir
from pathlib import Path

import frontmatter
from unidecode import unidecode

import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When True, only report proposed changes; write and create nothing.
DRY_RUN = False

# Throttle. MusicBrainz's anonymous rate limit is ~1 request/second and it
# answers 503 above it. These are applied by utils.http_get itself (see main),
# so retries are spaced too — a bare sleep between artists would not cover them.
REQUEST_INTERVAL = 1.1  # minimum seconds between requests
REQUEST_JITTER = 0.2    # extra random 0..JITTER seconds per request

# Process at most this many artists (0 = no limit). Handy for a first test run.
LIMIT = 0

# Rewrite the `members:` block of bands that already have one, instead of only
# filling bands that have none. Use it after changing instrument_roles.csv or
# ROLE_ORDER, so existing rosters pick up the new mapping. Set by
# --refresh-members; see the caveats in that flag's help.
REFRESH_MEMBERS = False

# Front-matter key (under `lastUpdate`) recording when we last refreshed an
# artist from MusicBrainz. fill_musicbrainz.py uses a separate key for its
# id *search*, so the two operations never mask each other.
LOOKUP_PROVIDER = "musicbrainz"

# Public MusicBrainz API (no API key needed).
LOOKUP_URL = "https://musicbrainz.org/ws/2/artist/{mbid}?inc=url-rels+artist-rels&fmt=json"

# MusicBrainz requires a descriptive User-Agent and blocks generic ones.
# https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
USER_AGENT = "MuzicLive/1.0 (https://muzic.live)"
HEADERS = {"User-Agent": USER_AGENT}

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
    # Songkick artist URLs are /artists/<numeric id>-<slug>; events.py needs
    # that numeric id (fill_songkick.py resolves it by name otherwise).
    "songkick": re.compile(r"songkick\.com/artists/(\d+)"),
}

# Order socials keys appear in when a fiche is created (see
# utils.get_or_create_artist / PERSON_TEMPLATE below), used to anchor a
# newly-filled key's insertion point when it's entirely absent.
SOCIAL_ORDER = [
    "facebook", "instagram", "tiktok", "threads", "x", "bandsintown",
    "songkick", "youtube", "web", "email", "amazon", "apple", "deezer",
    "qobuz", "spotify", "tidal", "musicbrainz",
]

# Accept MusicBrainz partial dates: YYYY, YYYY-MM or YYYY-MM-DD.
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

# The project's role vocabulary, in display order (a member's roles are
# rendered in this order). It must stay in sync with the French labels in
# layouts/_partials/artists/member-roles.html — a role missing there renders
# as nothing at all.
#
# Grouped by family: voice, rhythm section, keys, strings, winds, then the
# non-instrumental roles.
ROLE_ORDER = [
    "sing",
    "guitar", "bass",
    "drums", "percussion",
    "keys", "accordion",
    "strings", "violin", "harp", "banjo", "mandolin",
    "wind", "flute", "saxophone", "trumpet", "trombone", "harmonica", "bagpipe",
    "dj", "dance",
    "other",
]

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
  songkick: ""
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
  songkick: ""
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
# MusicBrainz: one lookup per artist, by the id stored in the fiche
# ---------------------------------------------------------------------------

def lookup_musicbrainz(mbid):
    """Fetch one artist with url-rels + artist-rels + life-span (the latter is
    core data, always included). A single request: the id is already known."""
    response = utils.http_get(LOOKUP_URL.format(mbid=mbid), headers=HEADERS)
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
    """Load instrument_roles.csv, reporting any row whose role is outside the
    vocabulary.

    Rows with an unknown role are ignored — the instrument then falls back to
    map_role()'s keyword heuristics, and usually lands on "other". That used to
    happen silently, which quietly wasted a third of a hand-edited CSV, so any
    rejected role is now named on stderr with a count."""
    global _instrument_roles
    if _instrument_roles is None:
        _instrument_roles = {}
        rejected = {}
        if INSTRUMENT_ROLES_CSV.exists():
            with INSTRUMENT_ROLES_CSV.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    name = (row.get("instrument") or "").strip().lower()
                    role = (row.get("role") or "").strip().lower()
                    if not name or not role:
                        continue
                    if role in VALID_ROLES:
                        _instrument_roles[name] = role
                    else:
                        rejected[role] = rejected.get(role, 0) + 1
        if rejected:
            listing = ", ".join(f"{r}={n}" for r, n in sorted(
                rejected.items(), key=lambda kv: -kv[1]))
            print(
                f"! {INSTRUMENT_ROLES_CSV.name}: ignoring "
                f"{sum(rejected.values())} row(s) with a role outside "
                f"ROLE_ORDER ({listing}). Add the role to ROLE_ORDER and to "
                f"layouts/_partials/artists/member-roles.html, or remap those "
                f"rows.", file=sys.stderr)
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


def render_members(members, ids, extras=None):
    """Render the ``members:`` block.

    ``extras`` maps a member id to raw front-matter lines to re-emit for that
    member — used by --refresh-members to carry hand-added keys (``touring``,
    for instance) across a rewrite, since MusicBrainz cannot reproduce them."""
    extras = extras or {}
    lines = ["members:"]
    for member in members:
        member_id = ids[member["name"]]
        lines.append(f'  - id: "{member_id}"')
        lines.extend(extras.get(member_id, []))
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


# The whole `members:` block: the key plus every indented (or blank) line under
# it, up to the next top-level front-matter key.
MEMBERS_BLOCK = re.compile(r"^members:[ \t]*\n(?:[ \t]+[^\n]*\n)*", re.MULTILINE)

# Keys render_members() produces by itself; anything else under a member entry
# was added by hand and must survive a refresh.
GENERATED_MEMBER_KEYS = {"id", "roles", "periods"}


def existing_member_extras(text):
    """Return ``{member id: [raw lines]}`` for hand-added keys in the current
    ``members:`` block, so --refresh-members doesn't silently delete them."""
    try:
        members = frontmatter.loads(text).get("members") or []
    except Exception:
        return {}
    if not isinstance(members, list):
        return {}

    extras = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        member_id = str(member.get("id") or "")
        custom = [k for k in member if k not in GENERATED_MEMBER_KEYS]
        if not member_id or not custom:
            continue
        lines = []
        for key in custom:
            value = member[key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, str):
                value = f'"{utils.yaml_quote(value)}"'
            lines.append(f"    {key}: {value}")
        extras[member_id] = lines
    return extras


def replace_members(text, block):
    """Swap the existing ``members:`` block for ``block``. Returns None when
    there is no block to replace."""
    if not MEMBERS_BLOCK.search(text):
        return None
    return MEMBERS_BLOCK.sub(lambda _: block, text, count=1)


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
    """Return the artists that declare a ``socials.musicbrainz`` id and still
    miss at least one field, skipping those refreshed within the last month.

    Artists without an id are *not* handled here — that's fill_musicbrainz.py's
    job. Skipping them is what keeps this run to one request per artist."""
    candidates = []
    skipped_no_id = skipped_complete = skipped_fresh = parse_errors = 0
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

        mbid = socials.get("musicbrainz")
        if not mbid:
            skipped_no_id += 1
            continue

        needs_social = {p for p in SOCIAL_PATTERNS if not socials.get(p)}
        # Normally only bands *without* a roster are candidates. With
        # --refresh-members, bands that already have one are reconsidered so
        # their roles can be recomputed.
        needs_members = artist_type == "band" and (
            REFRESH_MEMBERS or not has_members_block(text))
        needs_dates = artist_type == "person" and not has_lifespan_block(text)

        if not needs_social and not needs_members and not needs_dates:
            skipped_complete += 1
            continue
        if not utils.is_stale(data, LOOKUP_PROVIDER):
            skipped_fresh += 1
            continue

        candidates.append(
            (file, title, artist_type, str(mbid), needs_social, needs_members, needs_dates)
        )

    return candidates, skipped_no_id, skipped_complete, skipped_fresh, parse_errors


# ---------------------------------------------------------------------------
# Per-artist processing
# ---------------------------------------------------------------------------

def process_artist(file, title, artist_type, mbid, needs_social, needs_members, needs_dates):
    """Apply everything one MusicBrainz lookup can fill. Returns a list of
    change descriptions (empty when the lookup found nothing usable).

    Exposed as a function so fill_musicbrainz.py can reuse it right after it
    discovers an id, instead of re-fetching the same artist."""
    data = lookup_musicbrainz(mbid)
    if data is None:
        return []

    changes = []
    text = file.read_text(encoding="utf-8")
    relations = data.get("relations") or []

    if needs_social:
        socials = extract_socials(relations)
        socials["musicbrainz"] = (mbid, "ok")  # the id the fiche already carries
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

            refreshing = has_members_block(text)
            if refreshing:
                # Carry hand-added per-member keys across the rewrite, and warn
                # about members the fiche has that MusicBrainz doesn't know:
                # they are about to disappear.
                extras = existing_member_extras(text)
                known = set(ids.values())
                current = {
                    str(m.get("id")) for m in (frontmatter.loads(text).get("members") or [])
                    if isinstance(m, dict) and m.get("id")
                }
                lost = current - known
                block = render_members(members, ids, extras)
                new_text = replace_members(text, block)
                note = f", {len(lost)} dropped" if lost else ""
                note += f", {len(extras)} kept custom" if extras else ""
            else:
                new_text = insert_members(text, render_members(members, ids))
                note = ""

            if new_text:
                text = new_text
                suffix = f", created {len(created_names)} fiches" if created_names else ""
                verb = "members~" if refreshing else "members="
                changes.append(f"{verb}{len(members)}{suffix}{note}")

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

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Refresh artist data from MusicBrainz using the stored id.")
    parser.add_argument(
        "--refresh-members", action="store_true",
        help="Also rewrite the `members:` block of bands that already have "
             "one, so they pick up a changed instrument_roles.csv / ROLE_ORDER. "
             "The roster is re-derived from MusicBrainz: hand-added per-member "
             "keys (e.g. `touring`) are carried over, but members MusicBrainz "
             "does not list are dropped — the count is reported per band. "
             "Try it with --dry-run first.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report the changes without writing anything.")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N artists.")
    return parser.parse_args(argv)


def main(argv=None):
    global DRY_RUN, LIMIT, REFRESH_MEMBERS

    args = parse_args(argv)
    REFRESH_MEMBERS = args.refresh_members
    if args.dry_run:
        DRY_RUN = True
    if args.limit is not None:
        LIMIT = args.limit

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # Throttle every MusicBrainz request (including http_get's own retries).
    utils.MIN_REQUEST_INTERVAL = REQUEST_INTERVAL
    utils.REQUEST_JITTER = REQUEST_JITTER

    candidates, skipped_no_id, skipped_complete, skipped_fresh, parse_errors = load_candidates()
    if LIMIT:
        candidates = candidates[:LIMIT]

    total = len(candidates)
    eta = total * (REQUEST_INTERVAL + REQUEST_JITTER / 2) / 3600
    print(
        f"Artists to refresh from MusicBrainz: {total} "
        f"(no musicbrainz id — run fill_musicbrainz.py: {skipped_no_id}, "
        f"nothing missing: {skipped_complete}, "
        f"refreshed within the last month: {skipped_fresh}). "
        f"Mode: {'DRY-RUN' if DRY_RUN else 'WRITE'}"
        f"{', REFRESHING existing members' if REFRESH_MEMBERS else ''}. "
        f"1 request per artist (~{eta:.1f}h). "
        f"Interrupting is safe: progress is recorded per artist."
    )
    if total == 0:
        print("Nothing to do.")
        return

    filled = no_match = errors = 0
    for index, (
        file, title, artist_type, mbid, needs_social, needs_members, needs_dates
    ) in enumerate(candidates, start=1):
        prefix = f"[{index}/{total}]"
        try:
            changes = process_artist(
                file, title, artist_type, mbid, needs_social, needs_members, needs_dates
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
            print(f"{prefix} - {title}: nothing new")
            no_match += 1

    print(
        "\nDone. "
        f"{'would update' if DRY_RUN else 'updated'}={filled}, "
        f"nothing_new={no_match}, "
        f"no_id={skipped_no_id}, already_complete={skipped_complete}, "
        f"refreshed_recently={skipped_fresh}, "
        f"errors={errors + parse_errors}"
    )
    if DRY_RUN and filled:
        print("DRY_RUN is on — set DRY_RUN = False to write these changes.")


if __name__ == "__main__":
    main()
