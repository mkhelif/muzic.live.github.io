#!/usr/bin/env python3
"""Import an artist's **past** concerts from setlist.fm.

Replaces the Songkick provider: Songkick has suspended new API key
applications ("we are unable to process new applications for API keys"), so
that route is closed for this repository. setlist.fm covers the same need —
and covers it better for the archive, since setlists are by definition
concerts that actually took place.

Why it slots in with no discovery script
----------------------------------------

setlist.fm is indexed by **MusicBrainz MBID**::

    GET /1.0/artist/{mbid}/setlists

That is the id ``fill_musicbrainz.py`` / ``musicbrainz.py`` already store in
``socials.musicbrainz``. So this provider declares ``SOCIAL_KEY =
"musicbrainz"`` and needs no ``fill_setlistfm.py`` at all: every artist whose
MusicBrainz id is known is immediately reachable, and the pool grows on its
own every time ``fill_musicbrainz.py`` runs.

Past only
---------

setlist.fm holds no upcoming dates, so ``fetch_events`` returns nothing unless
``events.INCLUDE_PAST`` is on — no point spending a request otherwise. The
upcoming calendar stays the job of bandsintown / apple / deezer.

API key
-------

Free for non-commercial use, self-service (unlike Songkick): register at
https://www.setlist.fm/signup then request a key at
https://www.setlist.fm/settings/api, and::

    export SETLISTFM_API_KEY=...

Note the terms: the API is free for non-commercial projects only.

Not used yet, but available in the very same response: ``set[].song[]`` (the
actual setlist) and ``tour.name`` — either could enrich a concert review.

Run from the repository root::

    python3 snippets/events.py setlistfm --past
"""

import os
from datetime import datetime
from urllib.parse import quote

import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROVIDER = "setlistfm"
# The MusicBrainz id *is* the setlist.fm artist key — no separate id to fill.
SOCIAL_KEY = "musicbrainz"

# setlist.fm only knows about concerts that already happened.
SUPPORTS_PAST = True

REQUEST_INTERVAL = 1.0
REQUEST_JITTER = 0.3
THROTTLE = (REQUEST_INTERVAL, REQUEST_JITTER)

SETLISTS_URL = "https://api.setlist.fm/rest/1.0/artist/{mbid}/setlists?p={page}"

# 20 per page, fixed by the API.
ITEMS_PER_PAGE = 20
MAX_PAGES = 25  # safety net: ~500 concerts per artist


class MissingApiKey(RuntimeError):
    def __str__(self):
        return (
            "SETLISTFM_API_KEY is not set. Register at "
            "https://www.setlist.fm/signup, request a key at "
            "https://www.setlist.fm/settings/api, then run:\n"
            "    export SETLISTFM_API_KEY=<your key>"
        )


def api_key():
    key = os.environ.get("SETLISTFM_API_KEY", "").strip()
    if not key:
        raise MissingApiKey()
    return key


def headers(key):
    # The key travels in a header, and JSON must be asked for explicitly —
    # the API answers XML by default.
    return {"x-api-key": key, "Accept": "application/json"}


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------

def get_artist_setlists(mbid, key=None, since=None):
    """Yield the artist's setlists, newest first, stopping at ``since``.

    setlist.fm has no date filter on this endpoint, so ``since`` is applied
    client-side — but since results are ordered most-recent-first, reaching it
    lets us stop paginating instead of walking the whole history."""
    key = key or api_key()
    floor = None
    if since:
        try:
            floor = datetime.fromisoformat(since).date()
        except ValueError:
            floor = None

    for page in range(1, MAX_PAGES + 1):
        response = utils.http_get(
            SETLISTS_URL.format(mbid=quote(str(mbid)), page=page),
            headers=headers(key),
        )
        # 404 simply means "this artist has no setlists".
        if not response.ok:
            return
        try:
            payload = response.json()
        except ValueError:
            return

        setlists = payload.get("setlist") or []
        if not setlists:
            return
        for setlist in setlists:
            date = parse_event_date(setlist.get("eventDate"))
            if floor and date and date.date() < floor:
                return  # ordered newest first: everything after is older
            yield setlist

        if page * ITEMS_PER_PAGE >= int(payload.get("total") or 0):
            return


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def parse_event_date(value):
    """setlist.fm dates are ``DD-MM-YYYY``; return a naive datetime at 00:00.

    No time of day is published, so none is invented."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%m-%Y")
    except ValueError:
        return None


def build_location(setlist):
    """Normalise the setlist's venue, or ``None`` when it is unusable."""
    venue = setlist.get("venue") or {}
    name = (venue.get("name") or "").strip()
    city_data = venue.get("city") or {}
    city = (city_data.get("name") or "").strip()
    country_code = ((city_data.get("country") or {}).get("code") or "").strip()
    if not (name and city and country_code):
        return None

    country = utils.resolve_country(country_code)
    if country is None:
        return None

    city = utils.translate(city.title(), utils.VENUES)
    name = utils.translate(name.title(), utils.VENUES)
    if not city or not name:
        return None

    # `city.coords` is the *city centre*, not the venue — storing it as venue
    # coordinates would be false precision, so it is deliberately dropped.
    # Bandsintown and Apple supply real venue coordinates when they know them.
    return {"country": country, "city": city, "name": name}


def normalize_setlist(setlist, artist_name):
    """Map a setlist to events.py's common model, or ``None``."""
    date = parse_event_date(setlist.get("eventDate"))
    if date is None:
        return None
    location = build_location(setlist)
    if location is None:
        return None

    artist = (setlist.get("artist") or {}).get("name") or artist_name
    return {
        "date": date,
        # One setlist is one artist's performance; events.py merges the bills
        # of everyone who played the same venue that night.
        "lineup": [utils.translate(artist.strip(), utils.ARTISTS)],
        "location": location,
        # A past concert has no tickets to sell.
        "ticket": None,
        # setlist.fm has no festival flag; events.py falls back to the
        # line-up size heuristic once the bills are merged.
        "festival": False,
        "source": PROVIDER,
    }


# ---------------------------------------------------------------------------
# events.py adapter
# ---------------------------------------------------------------------------

def fetch_events(provider_id, artist_name, past=False, since=None):
    """Return this artist's past concerts in events.py's common event model.

    Returns nothing when ``past`` is off: setlist.fm has no upcoming dates, so
    a request would be wasted."""
    if not past:
        return []

    key = api_key()
    events = []
    for setlist in get_artist_setlists(provider_id, key=key, since=since):
        event = normalize_setlist(setlist, artist_name)
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

    events.INCLUDE_PAST = True  # this provider has nothing else to offer
    events.run([sys.modules[__name__]])
