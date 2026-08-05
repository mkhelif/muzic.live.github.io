#!/usr/bin/env python3
"""Fetch concerts from Songkick for artists declaring a ``socials.songkick`` id.

An ``events.py`` provider, alongside ``bandsintown.py`` / ``apple.py`` /
``deezer.py``. Songkick is already the source behind most of the ticket URLs
stored in the repository, so importing from it directly closes the loop.

Unlike the others this one uses Songkick's **documented JSON API** rather than
scraping, which makes it the most stable of the four — no HTML selectors to
break. It needs an API key: request one at
https://www.songkick.com/api_key_requests/new and expose it as::

    export SONGKICK_API_KEY=...

Two things Songkick gives us that the scrapers don't:

* ``type`` is ``"Concert"`` or ``"Festival"``, so festivals are flagged
  explicitly instead of being guessed from the line-up size (only Deezer does
  this too);
* ``performance[]`` carries the full bill with a ``billing`` rank, so the
  line-up is complete rather than headliner-only.

Endpoint (documented at https://www.songkick.com/developer):

    https://api.songkick.com/api/3.0/artists/{id}/calendar.json?apikey=...

Run from the repository root::

    python3 snippets/songkick.py
"""

import os
from datetime import date, datetime
from urllib.parse import quote

import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROVIDER = "songkick"
SOCIAL_KEY = "songkick"

# Songkick publishes no hard rate limit; stay polite and let utils.http_get
# retry 429/503 with backoff.
REQUEST_INTERVAL = 1.0
REQUEST_JITTER = 0.3
THROTTLE = (REQUEST_INTERVAL, REQUEST_JITTER)

# Songkick is the only provider exposing an artist's *past* events, through
# the gigography endpoint (same response shape as the calendar).
SUPPORTS_PAST = True

API_ROOT = "https://api.songkick.com/api/3.0"
# `calendar` = upcoming, `gigography` = past. Same parameters, same objects.
ARTIST_EVENTS_URL = (
    API_ROOT + "/artists/{id}/{feed}.json?apikey={key}&page={page}&per_page={per_page}"
)
# Songkick can also resolve an artist by MusicBrainz id — see fill_songkick.py.
ARTIST_EVENTS_MBID_URL = (
    API_ROOT + "/artists/mbid:{mbid}/{feed}.json?apikey={key}&page={page}&per_page={per_page}"
)
SEARCH_URL = API_ROOT + "/search/artists.json?apikey={key}&query={q}"

# Max allowed by the API.
PER_PAGE = 50
# Safety net against runaway pagination.
MAX_PAGES = 20

# Event statuses worth importing ("cancelled" / "postponed" are not).
KEEP_STATUS = {"ok"}


class MissingApiKey(RuntimeError):
    def __str__(self):
        return (
            "SONGKICK_API_KEY is not set. Request a key at "
            "https://www.songkick.com/api_key_requests/new then run:\n"
            "    export SONGKICK_API_KEY=<your key>"
        )


def api_key():
    key = os.environ.get("SONGKICK_API_KEY", "").strip()
    if not key:
        raise MissingApiKey()
    return key


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------

def _results_page(url):
    """GET a Songkick endpoint and return its ``resultsPage``, or ``None``."""
    response = utils.http_get(url)
    if not response.ok:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    page = payload.get("resultsPage") or {}
    return page if page.get("status") == "ok" else None


def _paginate(url_template, since=None, **kwargs):
    """Yield every event across a paginated feed.

    ``since`` bounds the window: Songkick requires ``min_date`` and
    ``max_date`` to be given together, so both are sent."""
    # Songkick rejects min_date without max_date, so send the pair: from the
    # floor up to today (this is only ever used for the past feed).
    suffix = f"&min_date={since}&max_date={date.today().isoformat()}" if since else ""
    for page_number in range(1, MAX_PAGES + 1):
        url = url_template.format(page=page_number, per_page=PER_PAGE, **kwargs)
        page = _results_page(url + suffix)
        if page is None:
            return
        events = ((page.get("results") or {}).get("event")) or []
        for event in events:
            yield event
        # Stop once we've seen everything the API says exists.
        if page_number * PER_PAGE >= int(page.get("totalEntries") or 0):
            return


def get_artist_events(songkick_id, key=None, past=False, since=None):
    """Return the raw Songkick event objects for an artist id.

    ``past=True`` reads the gigography (past events) instead of the calendar
    (upcoming); ``since`` (YYYY-MM-DD) bounds how far back to go."""
    return list(_paginate(
        ARTIST_EVENTS_URL,
        id=quote(str(songkick_id)),
        feed="gigography" if past else "calendar",
        key=key or api_key(),
        since=since if past else None,
    ))


def get_artist_events_by_mbid(mbid, key=None, past=False, since=None):
    """Same, resolved through the artist's MusicBrainz id."""
    return list(_paginate(
        ARTIST_EVENTS_MBID_URL,
        mbid=quote(str(mbid)),
        feed="gigography" if past else "calendar",
        key=key or api_key(),
        since=since if past else None,
    ))


def search_artists(name, key=None):
    """Return the raw Songkick artist objects matching ``name``."""
    page = _results_page(SEARCH_URL.format(key=key or api_key(), q=quote(name)))
    if page is None:
        return []
    return ((page.get("results") or {}).get("artist")) or []


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def event_lineup(event, fallback_name):
    """Return the bill, ordered by Songkick's billing index (headliner first)."""
    performances = sorted(
        (p for p in (event.get("performance") or []) if isinstance(p, dict)),
        key=lambda p: p.get("billingIndex") or 99,
    )
    lineup = []
    for performance in performances:
        artist = performance.get("artist") or {}
        name = (artist.get("displayName") or performance.get("displayName") or "").strip()
        if not name:
            continue
        name = utils.translate(name, utils.ARTISTS)
        if name not in lineup:
            lineup.append(name)
    return lineup or [fallback_name]


def build_location(event):
    """Normalise a Songkick venue into the location dict the venue creator
    expects, or ``None`` when the venue is unknown.

    Songkick documents that a show can have no venue yet (tour announced,
    venues not) — those are skipped rather than filed under a placeholder."""
    venue = event.get("venue") or {}
    name = (venue.get("displayName") or "").strip()
    if not name or name.lower() == "unknown venue":
        return None

    metro = venue.get("metroArea") or {}
    city = (metro.get("displayName") or "").strip()
    country_name = ((metro.get("country") or {}).get("displayName") or "").strip()
    if not city or not country_name:
        return None

    country = utils.resolve_country(country_name)
    if country is None:
        return None

    city = utils.translate(city.title(), utils.VENUES)
    name = utils.translate(name.title(), utils.VENUES)
    if not city or not name:
        return None

    return {
        "country": country,
        "city": city,
        "name": name,
        "latitude": venue.get("lat"),
        "longitude": venue.get("lng"),
    }


def _parse_start(event):
    """Return the event's start as a datetime, or ``None``.

    ``start.datetime`` carries the time zone but is absent for date-only
    announcements, where only ``start.date`` is set."""
    start = event.get("start") or {}
    stamp = start.get("datetime")
    if stamp:
        try:
            return datetime.fromisoformat(stamp)
        except ValueError:
            pass
    day = start.get("date")
    if not day:
        return None
    try:
        return datetime.fromisoformat(day)
    except ValueError:
        return None


def normalize_event(event, artist_name):
    """Map a Songkick event object to events.py's common model, or ``None``."""
    if (event.get("status") or "ok") not in KEEP_STATUS:
        return None
    date = _parse_start(event)
    if date is None:
        return None
    location = build_location(event)
    if location is None:
        return None

    return {
        "date": date,
        "lineup": event_lineup(event, artist_name),
        "location": location,
        # The event page doubles as the ticket link; clean_ticket_url strips
        # the utm_* / referer_info tracking Songkick appends.
        "ticket": event.get("uri"),
        "festival": (event.get("type") or "").lower() == "festival",
        "source": PROVIDER,
    }


# ---------------------------------------------------------------------------
# events.py adapter
# ---------------------------------------------------------------------------

def fetch_events(provider_id, artist_name, past=False, since=None):
    """Return this artist's concerts in events.py's common event model.

    With ``past=True`` the gigography is read as well, so Songkick is the only
    provider that can backfill an artist's concert history."""
    key = api_key()
    raw_events = list(get_artist_events(provider_id, key=key))
    if past:
        raw_events += get_artist_events(provider_id, key=key, past=True, since=since)

    events = []
    for raw in raw_events:
        event = normalize_event(raw, artist_name)
        if event is not None:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Entry point: run this provider alone, through the shared events.py driver.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    import events  # local import: events.py imports this module

    try:
        api_key()
    except MissingApiKey as error:
        print(error)
        raise SystemExit(1)

    events.run([sys.modules[__name__]])
