#!/usr/bin/env python3
"""Create `events` (and `venues` when missing) from Apple Music.

For every artist fiche that declares an Apple Music id in its front matter
(``socials.apple``), this script reads the artist's public Apple Music concerts
page and materialises the upcoming shows as event files under
``content/events/YYYY/MM/DD/``, creating the venue hierarchy
(``content/venues/<country>/<city>/<venue>/``) — with coordinates — as needed.

Apple Music concert pages are server-rendered. The list lives at
``/{storefront}/concerts/artist/<appleId>`` and links to per-concert pages
``/{storefront}/concerts/ce.<uuid>``. Each concert page embeds an Apple Maps
link that carries the venue name, coordinates and full address (ending with the
country), plus the localized date and a ticket link.

Like ``spotify.py`` and ``bandsintown.py`` it records ``lastUpdate.apple`` per
artist and skips artists refreshed within the last week, and shares its plumbing
(HTTP session, slugging, country + venue creation) with ``utils.py``.

Run from the repository root::

    python3 snippets/apple.py
"""

from datetime import datetime
from os import listdir
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import html
import re
import time
import traceback

import utils


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Storefront (country) used for the Apple Music pages. French dates are parsed.
STOREFRONT = "fr"

ARTIST_CONCERTS_URL = "https://music.apple.com/{sf}/concerts/artist/{id}"
CONCERT_URL = "https://music.apple.com/{sf}/concerts/{ce}"

# Politeness delay between per-concert page fetches (seconds).
REQUEST_DELAY = 0.5

_CONCERT_ID_RE = re.compile(r"/concerts/(ce\.[0-9a-fA-F-]+)")
_MAPS_RE = re.compile(r"https://maps\.apple\.com/place\?([^\"'\s<>]+)")

# French month names -> month number, to parse "25 novembre 2026".
_MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_DATE_RE = re.compile(
    r"(\d{1,2})\s+(" + "|".join(_MONTHS_FR) + r")\s+(\d{4})", re.IGNORECASE
)
_TIME_RE = re.compile(r"(\d{1,2})\s*[:h]\s*(\d{2})?")


# ---------------------------------------------------------------------------
# Concert list
# ---------------------------------------------------------------------------

def get_concert_ids(apple_id):
    """Return the ordered, de-duplicated list of ``ce.<uuid>`` concert ids for
    an artist's Apple Music concerts page."""
    response = utils.http_get(ARTIST_CONCERTS_URL.format(sf=STOREFRONT, id=apple_id))
    if not response.ok:
        raise Exception(
            f"Failed to fetch concerts for {apple_id} ({response.status_code})"
        )
    ids = []
    seen = set()
    for match in _CONCERT_ID_RE.finditer(response.text):
        ce = match.group(1)
        if ce not in seen:
            seen.add(ce)
            ids.append(ce)
    return ids


# ---------------------------------------------------------------------------
# Concert detail parsing
# ---------------------------------------------------------------------------

def _plain_text(html_text):
    return html.unescape(re.sub(r"<[^>]+>", " ", html_text))


def _parse_maps_link(html_text):
    """Return ``(name, city, country, latitude, longitude)`` from the Apple Maps
    link on the page, or ``None``."""
    match = _MAPS_RE.search(html_text)
    if not match:
        return None
    params = parse_qs(html.unescape(match.group(1)))
    name = (params.get("name", [""])[0] or "").strip()
    address = (params.get("address", [""])[0] or "").strip()
    coordinate = (params.get("coordinate", [""])[0] or "").strip()

    latitude = longitude = None
    if "," in coordinate:
        latitude, longitude = (p.strip() for p in coordinate.split(",", 1))

    # Country is the last address segment; city is the last meaningful segment
    # before it (skipping the postal code).
    segments = [s.strip() for s in address.split(",") if s.strip()]
    country = segments[-1] if segments else None
    city = None
    if country:
        rest = [
            s for s in segments[:-1]
            if not s.isdigit() and s.lower() != country.lower()
        ]
        city = rest[-1] if rest else None

    if not (name and city and country):
        return None
    return name, city, country, latitude, longitude


def _parse_datetime(text):
    """Parse the French date (+ time) into a naive datetime, or ``None``."""
    date_match = _DATE_RE.search(text)
    if not date_match:
        return None
    day = int(date_match.group(1))
    month = _MONTHS_FR[date_match.group(2).lower()]
    year = int(date_match.group(3))

    hour = minute = 0
    time_match = _TIME_RE.search(text, date_match.end(), date_match.end() + 40)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _ticket_url(html_text):
    match = re.search(r'href="(https://[^"]*bandsintown\.com/e/[^"]+)"', html_text)
    return html.unescape(match.group(1)) if match else None


def parse_concert(html_text):
    """Return an internal concert dict from a concert page, or ``None``."""
    maps = _parse_maps_link(html_text)
    if maps is None:
        return None
    name, city, country, latitude, longitude = maps

    when = _parse_datetime(_plain_text(html_text))
    if when is None:
        return None

    return {
        "datetime": when,
        "venue": {
            "name": name,
            "city": city,
            "country": country,
            "latitude": latitude,
            "longitude": longitude,
        },
        "ticket": _ticket_url(html_text),
    }


def build_location(venue):
    """Normalise the parsed venue into the location dict the venue creator
    expects, or ``None``."""
    country = utils.resolve_country(venue.get("country"))
    if country is None:
        return None
    city = utils.translate(venue["city"], utils.VENUES)
    name = utils.translate(venue["name"], utils.VENUES)
    if not city or not name:
        return None
    return {
        "country": country,
        "city": city,
        "name": name,
        "latitude": venue.get("latitude"),
        "longitude": venue.get("longitude"),
    }


# ---------------------------------------------------------------------------
# Event writing
# ---------------------------------------------------------------------------

def write_event(concert, artist_name):
    """Create a single event file from a parsed concert. Returns the path if
    written, or ``None`` when skipped."""
    date = concert["datetime"]
    date_format = f"{date.year}/{date.month:02d}/{date.day:02d}"

    location = build_location(concert["venue"])
    if location is None:
        return None

    artist_id = utils.get_or_create_artist(artist_name)
    venue_id = utils.get_or_create_venue(location)

    directory = Path(f"./content/events/{date_format}")
    directory.mkdir(parents=True, exist_ok=True)
    event_file = directory.joinpath(utils.format_filename(artist_name) + ".md")
    if event_file.exists():
        return None

    lines = [
        "---",
        f"date: {date.isoformat()}",
        f'venue: "{venue_id}"',
        "artists:",
        f'  - "{artist_id}"',
    ]
    ticket = utils.clean_ticket_url(concert.get("ticket"))
    if ticket:
        lines += ["tickets:", f'  web: "{utils.yaml_quote(ticket)}"']
    lines += ["---", ""]
    event_file.write_text("\n".join(lines), encoding="UTF-8")
    return event_file


# ---------------------------------------------------------------------------
# Entry point: iterate over every artist declaring an Apple Music id.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for artist in sorted(listdir("./content/artists")):
        file = Path(f"./content/artists/{artist}/index.md")
        if not file.exists():
            continue

        data = utils.load_frontmatter(file)
        socials = data.get("socials", None)
        apple_id = socials.get("apple", None) if socials else None
        if not apple_id:
            continue

        name = data.get("title", None)

        # Skip artists already refreshed from Apple Music within the last week.
        if not utils.is_stale(data, "apple"):
            print(f"{name} (skipped: refreshed within the last week)")
            continue

        print(name)
        try:
            for ce in get_concert_ids(apple_id):
                response = utils.http_get(CONCERT_URL.format(sf=STOREFRONT, ce=ce))
                if not response.ok:
                    continue
                concert = parse_concert(response.text)
                if concert is not None:
                    created = write_event(concert, name)
                    if created is not None:
                        print(f"  + {created}")
                time.sleep(REQUEST_DELAY)
            # Mark this artist as refreshed from Apple Music today.
            utils.set_last_update(file, "apple")
        except utils.CloudflareBlocked as blocked:
            print(f"\n{blocked}")
            break
        except Exception:
            print(traceback.format_exc())
