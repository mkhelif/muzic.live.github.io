#!/usr/bin/env python3
"""Create `events` (and `venues` when missing) from Bandsintown.

For every artist fiche that declares a Bandsintown id in its front matter
(``socials.bandsintown``), this script reads the artist's public Bandsintown
page and materialises their concerts as event files under
``content/events/YYYY/MM/DD/``, creating the venue hierarchy
(``content/venues/<country>/<city>/<venue>/``) on the fly when needed.

Note: Bandsintown's open REST API (rest.bandsintown.com) now requires an
approved partner ``app_id`` and returns an "explicit deny" error otherwise, so
this script instead parses the schema.org ``MusicEvent`` JSON-LD that the public
artist page renders server-side — no API key required.

The public site is fronted by Cloudflare's bot protection, which blocks plain
``requests``. Install the optional ``cloudscraper`` dependency to clear the
challenge automatically::

    pip install cloudscraper

Shared plumbing (HTTP session + Cloudflare handling, slugging, front matter,
country lookup, alias-aware artist creation, venue hierarchy) lives in
``snippets/utils.py``.

Run it from the repository root (paths are relative)::

    python3 snippets/bandsintown.py
"""

import json
import re
import traceback
import uuid
from datetime import datetime
from os import listdir
from pathlib import Path

import pycountry

import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Public artist page. ``id_<id>`` resolves by the stable Bandsintown artist id
# (the value stored in ``socials.bandsintown``); Bandsintown redirects it to the
# slugged URL, which the session follows automatically.
ARTIST_URL = "https://www.bandsintown.com/a/{id}"

# Which concerts to keep: "upcoming", "past" or "all". Applied client-side from
# the event start date (the page's JSON-LD mostly lists upcoming shows).
DATE_FILTER = "upcoming"

# Skip events whose line-up is larger than this (most likely festivals, which
# are better handled as a dedicated festival "day" event). Mirrors the guard
# used in spotify.py's get_concert.
MAX_LINEUP = 5

# Throttle: Bandsintown starts replying 416 when hit too fast. Space requests
# out (with jitter) so the run stays polite. utils.http_get also retries 416/429
# with backoff. Bump these if 416s still appear.
REQUEST_INTERVAL = 3.0  # minimum seconds between requests
REQUEST_JITTER = 1.5    # extra random 0..JITTER seconds per request

_JSONLD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Bandsintown public page (schema.org JSON-LD)
# ---------------------------------------------------------------------------

def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _iter_jsonld_nodes(html):
    """Yield every JSON-LD object embedded in the page, flattening arrays and
    ``@graph`` wrappers."""
    for match in _JSONLD_RE.finditer(html):
        try:
            parsed = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        stack = [parsed]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if isinstance(node.get("@graph"), list):
                    stack.extend(node["@graph"])
                yield node


def _country_name(address):
    country = address.get("addressCountry")
    if isinstance(country, dict):
        return country.get("name")
    return country


def _normalize_event(node):
    """Map a schema.org ``MusicEvent`` node to the internal event dict used by
    the venue/artist/event writers, or return ``None`` if it is not an event."""
    types = _as_list(node.get("@type"))
    if not any(t in ("MusicEvent", "Event", "Festival") for t in types):
        return None
    if not node.get("startDate"):
        return None

    location = node.get("location") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    address = location.get("address") or {}
    if not isinstance(address, dict):
        address = {}
    geo = location.get("geo") or {}

    performers = [
        p.get("name")
        for p in _as_list(node.get("performer"))
        if isinstance(p, dict) and p.get("name")
    ]
    offers = [
        {"type": "Tickets", "url": o.get("url")}
        for o in _as_list(node.get("offers"))
        if isinstance(o, dict) and o.get("url")
    ]

    return {
        "datetime": node.get("startDate"),
        "lineup": performers,
        "venue": {
            "name": location.get("name"),
            "city": address.get("addressLocality"),
            "region": address.get("addressRegion"),
            "country": _country_name(address),
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
        },
        "offers": offers,
        "url": node.get("url"),
    }


def _passes_date_filter(dt):
    if DATE_FILTER == "all":
        return True
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return dt >= now if DATE_FILTER == "upcoming" else dt < now


def get_artist_events(bandsintown_id):
    """Return the artist's events, parsed from the public Bandsintown page's
    schema.org JSON-LD. De-duplicated and filtered by ``DATE_FILTER``."""
    response = utils.http_get(ARTIST_URL.format(id=bandsintown_id))
    if not response.ok:
        raise Exception(
            f"Failed to fetch page for {bandsintown_id} "
            f"({response.status_code}): {response.text[:200]}"
        )

    events = []
    seen = set()
    for node in _iter_jsonld_nodes(response.text):
        event = _normalize_event(node)
        if event is None:
            continue
        key = (event["datetime"], (event["venue"].get("name") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        try:
            dt = datetime.fromisoformat(event["datetime"])
        except ValueError:
            continue
        if not _passes_date_filter(dt):
            continue
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Location handling
# ---------------------------------------------------------------------------

# Country names pycountry cannot resolve (user-assigned / non-ISO), mapped to
# the alpha-2 keys used in utils.COUNTRIES.
MANUAL_COUNTRIES = {
    "kosovo": "XK",
}


def resolve_country(name):
    """Map a Bandsintown country name to the ``{name, code}`` dict expected by
    the venue creators (French name + ISO alpha-3 code)."""
    if not name:
        return None
    try:
        alpha_2 = pycountry.countries.lookup(name).alpha_2
    except LookupError:
        alpha_2 = MANUAL_COUNTRIES.get(name.strip().lower())
    if alpha_2 and alpha_2 in utils.COUNTRIES:
        return utils.COUNTRIES[alpha_2]
    # Unknown country: fall back to the raw name as both label and code.
    return {"name": name, "code": name}


def build_location(venue):
    """Normalise a Bandsintown ``venue`` object into the location dict used to
    create the venue hierarchy. Returns ``None`` when data is insufficient."""
    country = resolve_country(venue.get("country"))
    if country is None:
        return None

    city = utils.translate((venue.get("city") or "").strip().title(), utils.VENUES)
    name = utils.translate((venue.get("name") or "").strip().title(), utils.VENUES)
    if not city or not name:
        return None

    return {
        "country": country,
        "city": city,
        "name": name,
        "latitude": venue.get("latitude"),
        "longitude": venue.get("longitude"),
    }


def get_or_create_venue(location):
    """Return the id of the venue fiche, creating the country/city/venue chain
    when needed. Unlike utils.get_or_create_location, this stores the Bandsintown
    coordinates when they are available."""
    directory = Path(
        "./content/venues/"
        f"{utils.format_filename(location['country']['code'])}/"
        f"{utils.format_filename(location['city'])}/"
        f"{utils.format_filename(location['name'])}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    file = directory.joinpath("index.md")

    if file.exists():
        venue_id = utils.load_frontmatter(file).get("id", None)
        if venue_id is None:
            raise Exception(f"Existing venue without id: {file}")
        return str(venue_id)

    parent_id = utils.get_or_create_location_city(
        location["country"], location["city"]
    )
    venue_id = uuid.uuid4()

    lines = [
        "---",
        f'id: "{venue_id}"',
        f'venue: "{parent_id}"',
        f'title: "{utils.yaml_quote(location["name"])}"',
    ]
    if location.get("latitude") and location.get("longitude"):
        lines += [
            "coordinates:",
            f"  latitude: {location['latitude']}",
            f"  longitude: {location['longitude']}",
            "  zoom: 15",
        ]
    lines += ["---", ""]
    file.write_text("\n".join(lines), encoding="UTF-8")
    return str(venue_id)


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------

def event_lineup(event, fallback_name):
    """Return a sorted, de-duplicated, translated line-up for an event."""
    lineup = [
        utils.translate(a.strip(), utils.ARTISTS)
        for a in (event.get("lineup") or [])
        if a and a.strip()
    ]
    if not lineup:
        lineup = [fallback_name]
    return sorted(set(lineup))


def ticket_url(event):
    for offer in (event.get("offers") or []):
        if offer.get("url"):
            return offer["url"]
    # Fall back to the event page (Bandsintown RSVP / ticket redirect).
    return event.get("url")


def write_event(event, artist_name):
    """Create a single event file from a Bandsintown event. Returns the path
    if written, or ``None`` when skipped."""
    date = datetime.fromisoformat(event["datetime"])
    date_format = f"{date.year}/{date.month:02d}/{date.day:02d}"

    location = build_location(event.get("venue") or {})
    if location is None:
        return None

    lineup = event_lineup(event, artist_name)
    if len(lineup) > MAX_LINEUP:
        return None

    artist_ids = [utils.get_or_create_artist(a) for a in lineup]
    venue_id = get_or_create_venue(location)

    filename = "-".join(utils.format_filename(a) for a in lineup) + ".md"
    directory = Path(f"./content/events/{date_format}")
    directory.mkdir(parents=True, exist_ok=True)
    event_file = directory.joinpath(filename)
    if event_file.exists():
        return None

    lines = [
        "---",
        f"date: {date.isoformat()}",
        f'venue: "{venue_id}"',
        "artists:",
    ]
    lines += [f'  - "{aid}"' for aid in artist_ids]

    url = utils.clean_ticket_url(ticket_url(event))
    if url:
        lines += ["tickets:", f'  web: "{utils.yaml_quote(url)}"']

    lines += ["---", ""]
    event_file.write_text("\n".join(lines), encoding="UTF-8")
    return event_file


# ---------------------------------------------------------------------------
# Entry point: iterate over every artist declaring a Bandsintown id.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Throttle Bandsintown requests to avoid its 416 rate-limit responses.
    utils.MIN_REQUEST_INTERVAL = REQUEST_INTERVAL
    utils.REQUEST_JITTER = REQUEST_JITTER

    for artist in sorted(listdir("./content/artists")):
        file = Path(f"./content/artists/{artist}/index.md")
        if not file.exists():
            continue

        data = utils.load_frontmatter(file)
        socials = data.get("socials", None)
        bandsintown_id = socials.get("bandsintown", None) if socials else None
        if not bandsintown_id:
            continue

        name = data.get("title", None)

        # Skip artists already refreshed from Bandsintown recently.
        if not utils.is_stale(data, "bandsintown"):
            print(f"{name} (skipped: refreshed recently)")
            continue

        print(name)
        try:
            for event in get_artist_events(bandsintown_id):
                created = write_event(event, name)
                if created is not None:
                    print(f"  + {created}")
            # Mark this artist as refreshed from Bandsintown today.
            utils.set_last_update(file, "bandsintown")
        except utils.CloudflareBlocked as blocked:
            print(f"\n{blocked}")
            break
        except Exception:
            print(traceback.format_exc())
