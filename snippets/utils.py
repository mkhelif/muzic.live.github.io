#!/usr/bin/env python3
"""Shared helpers for the content-import snippets.

Used by ``spotify.py``, ``deezer.py``, ``bandsintown.py`` and
``fill_bandsintown.py``. Groups everything those scripts had in common:

* name slugging (``format_filename``) and front-matter I/O (``load_frontmatter``),
* country lookup with French names + ISO alpha-3 codes (``COUNTRIES``) and the
  manual name overrides (``translate`` / ``VENUES`` / ``ARTISTS``),
* alias-aware artist creation (``get_or_create_artist``) backed by a cached
  title/alias index so duplicate fiches are never recreated,
* venue hierarchy creation (``get_or_create_location*``),
* an HTTP session that clears Cloudflare when ``cloudscraper`` is installed
  (``http_get`` / ``get_session`` / ``CloudflareBlocked``).

All file paths are relative to the repository root, so run the importing scripts
from there.
"""

import gettext
import re
import uuid
from datetime import date, datetime
from pathlib import Path

import frontmatter
import pycountry
import requests
from unidecode import unidecode

# ---------------------------------------------------------------------------
# Country lookup (French names + ISO alpha-3 codes), keyed by alpha-2.
# ---------------------------------------------------------------------------

french = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=["fr"])
french.install()
_ = french.gettext

COUNTRIES = {}
for _country in pycountry.countries:
    COUNTRIES[_country.alpha_2.upper()] = {
        "name": _(_country.name),
        "code": _country.alpha_3,
    }

# Kosovo uses the user-assigned code "XK", which is not an official ISO 3166-1
# entry and therefore absent from pycountry.
COUNTRIES.setdefault("XK", {"name": _("Kosovo"), "code": "XKX"})


# ---------------------------------------------------------------------------
# Manual name overrides (provider labels -> canonical fiche titles).
# ---------------------------------------------------------------------------

VENUES = {
    "House of Blues Las Vegas ": "House of Blues",
    "Cournon D Auvergne": "Cournon d'Auvergne",
    "Paris 18": "Paris",
}

ARTISTS = {
    "Carlos Santana": "Santana",
    "Udo Dirkschneider": "Dirkschneider",
}


def translate(key, hash):
    """Return the override for ``key`` in ``hash`` if any, else ``key``."""
    return hash[key] if key in hash else key


def format_filename(name):
    """Slugify a name the way the whole project expects (ascii, lowercase,
    non-alphanumerics collapsed to single dashes)."""
    return re.sub(
        "-{2,}", "-", re.sub("[^a-z0-9]", "-", unidecode(name).lower())
    )


def load_frontmatter(file):
    """Parse the YAML front matter of a ``Path`` fiche."""
    try:
        return frontmatter.loads(file.read_text())
    except Exception as error:
        print(f"Failed to load frontmatter for {file}")
        raise error


# ---------------------------------------------------------------------------
# Per-provider update tracking.
#
# Each fiche records when a provider last refreshed it:
#
#     lastUpdate:
#       spotify: 2026-07-24
#       bandsintown: 2026-07-24
#
# Importers call is_stale() to skip artists refreshed within the last week and
# set_last_update() to stamp the fiche after a successful check.
# ---------------------------------------------------------------------------

STALE_AFTER_DAYS = 7


def _parse_date(value):
    """Coerce a YAML date / datetime / string into a ``date``, or None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def last_update(data, provider):
    """Return the stored ``lastUpdate.<provider>`` value, or None."""
    updates = data.get("lastUpdate")
    return updates.get(provider) if isinstance(updates, dict) else None


def is_stale(data, provider, today=None, max_age_days=STALE_AFTER_DAYS):
    """True if ``provider`` never refreshed this fiche, or did so at least
    ``max_age_days`` days ago."""
    value = last_update(data, provider)
    stamped = _parse_date(value) if value not in (None, "") else None
    if stamped is None:
        return True
    today = today or date.today()
    return (today - stamped).days >= max_age_days


def set_last_update(file, provider, when=None):
    """Stamp ``lastUpdate.<provider>`` in the fiche at ``file`` (a Path) with
    ``when`` (defaults to today), editing the YAML in place with a minimal diff."""
    when = when or date.today()
    value = when.isoformat() if isinstance(when, (date, datetime)) else str(when)
    file.write_text(_upsert_last_update(file.read_text(), provider, value))


def _upsert_last_update(text, provider, value):
    block = re.search(r"^lastUpdate:[ \t]*\n", text, re.MULTILINE)
    if block:
        start = block.end()
        following = re.search(r"^(?=\S)", text[start:], re.MULTILINE)
        end = start + following.start() if following else len(text)
        segment = text[start:end]
        line = re.search(
            rf"^([ \t]+){re.escape(provider)}:.*$", segment, re.MULTILINE
        )
        if line:
            segment = (
                segment[:line.start()]
                + f"{line.group(1)}{provider}: {value}"
                + segment[line.end():]
            )
        else:
            segment = f"  {provider}: {value}\n" + segment
        return text[:start] + segment + text[end:]

    # No lastUpdate block yet: add one just before the front-matter closing '---'.
    if not text.startswith("---"):
        return text
    closing = re.search(r"\n---[ \t]*(?:\n|$)", text)
    if not closing:
        return text
    insert_at = closing.start() + 1
    return text[:insert_at] + f"lastUpdate:\n  {provider}: {value}\n" + text[insert_at:]


# ---------------------------------------------------------------------------
# Alias-aware artist index.
#
# Case-insensitive index of existing artists, keyed by title and by every
# `aliases` entry, mapping to the artist id. Built once and kept up to date as
# new artists are created so that duplicates (e.g. "Bigflo & Oli" vs
# "Bigflo et Oli") are resolved to a single fiche.
# ---------------------------------------------------------------------------

_ARTIST_INDEX = None


def _index_key(name):
    return name.strip().lower()


def build_artist_index():
    index = {}
    artists_dir = Path("./content/artists")
    if not artists_dir.is_dir():
        return index
    for entry in sorted(artists_dir.iterdir()):
        file = entry.joinpath("index.md")
        if not file.exists():
            continue
        data = load_frontmatter(file)
        artist_id = data.get("id", None)
        if artist_id is None:
            continue
        artist_id = str(artist_id)

        # Register the title and every alias (case-insensitive).
        keys = [data.get("title", None)]
        aliases = data.get("aliases", None) or []
        if isinstance(aliases, str):
            aliases = [aliases]
        keys.extend(aliases)
        for key in keys:
            if key:
                index.setdefault(_index_key(key), artist_id)
    return index


def get_artist_index():
    global _ARTIST_INDEX
    if _ARTIST_INDEX is None:
        _ARTIST_INDEX = build_artist_index()
    return _ARTIST_INDEX


def get_or_create_artist(name):
    index = get_artist_index()

    # Reuse an existing artist when the name matches a title or alias.
    existing_id = index.get(_index_key(name))
    if existing_id is not None:
        return existing_id

    artist_id = None
    directory = Path(f"./content/artists/{format_filename(name)}")
    directory.mkdir(parents=True, exist_ok=True)
    file = directory.joinpath("index.md")

    if file.exists():
        artist_id = load_frontmatter(file).get("id", None)
    else:
        artist_id = uuid.uuid4()
        file.write_text(f"""\
---
id: "{artist_id}"
title: "{name}"
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
""")
    if artist_id is None:
        raise Exception(f"Could not create artist {name}")
    artist_id = str(artist_id)

    # Keep the index current so subsequent lookups in this run reuse it.
    index[_index_key(name)] = artist_id
    return artist_id


# ---------------------------------------------------------------------------
# Venue hierarchy: country -> city -> venue.
# ---------------------------------------------------------------------------

def get_or_create_location_country(country):
    country_id = None
    directory = Path(f"./content/venues/{format_filename(country['code'])}")
    directory.mkdir(parents=True, exist_ok=True)
    file = directory.joinpath("_index.md")

    if file.exists():
        country_id = load_frontmatter(file).get("id", None)
    else:
        country_id = uuid.uuid4()
        file.write_text(f"""\
---
id: "{country_id}"
title: "{country['name']}"
---
""")
    if country_id is None:
        raise Exception(f"Could not create country {country}")
    return country_id


def get_or_create_location_city(country, city):
    city_id = None
    directory = Path(
        f"./content/venues/{format_filename(country['code'])}/{format_filename(city)}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    file = directory.joinpath("_index.md")

    if file.exists():
        city_id = load_frontmatter(file).get("id", None)
    else:
        city_id = uuid.uuid4()
        file.write_text(f"""\
---
id: "{city_id}"
venue: "{get_or_create_location_country(country)}"
title: "{city}"
---
""")
    if city_id is None:
        raise Exception(f"Could not create city {city} - {country}")
    return city_id


def get_or_create_location(location):
    location_id = None
    directory = Path(
        f"./content/venues/{format_filename(location['country']['code'])}/"
        f"{format_filename(location['city'])}/{format_filename(location['name'])}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    file = directory.joinpath("index.md")

    if file.exists():
        location_id = load_frontmatter(file).get("id", None)
    else:
        location_id = uuid.uuid4()
        file.write_text(f"""\
---
id: "{location_id}"
venue: "{get_or_create_location_city(location['country'], location['city'])}"
title: "{location['name']}"
---
""")
    if location_id is None:
        raise Exception(
            "Could not create location "
            f"{location['country']['name']} - {location['city']} - {location['name']}"
        )
    return location_id


# ---------------------------------------------------------------------------
# HTTP session with Cloudflare handling (shared by the web scrapers).
# ---------------------------------------------------------------------------

# A browser-like User-Agent avoids being served an empty/blocked page when the
# plain-requests fallback is used.
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

_SESSION = None
USING_SCRAPER = False


class CloudflareBlocked(Exception):
    """Raised when a request is stopped by Cloudflare's bot challenge."""

    def __init__(self, url=None):
        self.url = url
        location = f" to {url}" if url else ""
        super().__init__(
            f"Cloudflare bot challenge blocked the request{location}. "
            "Install the optional dependency to clear it:\n"
            "    pip install cloudscraper\n"
            "If it is already installed and still blocked, Cloudflare is serving "
            "a hard challenge — run from a residential IP / proxy, or use an "
            "approved provider API key."
        )


def get_session():
    """Return a shared HTTP session. Prefer ``cloudscraper`` (which clears the
    Cloudflare challenge); fall back to plain ``requests``."""
    global _SESSION, USING_SCRAPER
    if _SESSION is None:
        try:
            import cloudscraper

            _SESSION = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "darwin", "mobile": False}
            )
            USING_SCRAPER = True
        except ImportError:
            _SESSION = requests.Session()
            USING_SCRAPER = False
    return _SESSION


def is_cloudflare_challenge(response):
    if response.status_code not in (403, 429, 503):
        return False
    if response.headers.get("cf-mitigated"):
        return True
    body = response.text[:1500].lower()
    return any(m in body for m in ("just a moment", "cf-chl", "cloudflare"))


def http_get(url, timeout=30):
    """GET ``url`` through the shared session, raising ``CloudflareBlocked`` on a
    bot challenge. The caller checks ``response.ok`` for other statuses."""
    session = get_session()
    # cloudscraper needs to control its own User-Agent to match its TLS
    # fingerprint; only send our browser headers on the plain-requests fallback.
    headers = {} if USING_SCRAPER else BROWSER_HEADERS
    response = session.get(url, headers=headers, timeout=timeout)
    if is_cloudflare_challenge(response):
        raise CloudflareBlocked(url)
    return response
