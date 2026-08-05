#!/usr/bin/env python3
"""Create `events` (and `venues` / `artists` when missing) from Deezer.

For every artist fiche that declares a Deezer id in its front matter
(``socials.deezer``), this script queries Deezer's GraphQL API
(``pipe.deezer.com``) for the artist's upcoming live events and materialises
them as event files under ``content/events/YYYY/MM/DD/``, creating the venue
hierarchy (``content/venues/<country>/<city>/<venue>/``) and any missing
line-up artist fiches on the fly.

Like ``spotify.py``, ``bandsintown.py`` and ``apple.py`` it:

* records ``lastUpdate.deezer`` per artist and skips artists refreshed within
  the last week (with a log line);
* shares its plumbing with ``utils.py`` (alias-aware ``get_or_create_artist``,
  venue creation, country lookup, name overrides, throttling);
* never overwrites an existing event file.

Deezer's GraphQL endpoint requires a JWT, but Deezer mints **anonymous** tokens
without any account or API registration (the web player does the same):
``auth.deezer.com/login/anonymous`` returns a short-lived JWT (~6 minutes),
which this script fetches and refreshes automatically.

Run from the repository root::

    python3 snippets/deezer.py
"""

from datetime import datetime
from os import listdir
from pathlib import Path

import time
import traceback

import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = "https://pipe.deezer.com/api"

# Anonymous JWT endpoint (no account / registration required); tokens expire
# after ~6 minutes and are refreshed automatically.
AUTH_URL = "https://auth.deezer.com/login/anonymous?jo=p&rto=c&i=c"
JWT_MAX_AGE = 240  # seconds before proactively refreshing the token

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) "
        "Gecko/20100101 Firefox/149.0"
    ),
    "Accept": "*/*",
    "Accept-Language": "fr-FR",
    "Content-Type": "application/json",
    "Referer": "https://www.deezer.com/",
    "Origin": "https://www.deezer.com",
}

# Number of live events requested per artist.
EVENTS_FIRST = 70

# Skip events whose line-up is larger than this (most likely festivals, which
# are better handled as dedicated festival "day" events). Mirrors the guard
# used by the other importers.
MAX_LINEUP = 5

# Deezer country codes that differ from ISO 3166-1 alpha-2.
COUNTRIES_MAPPING = {
    "FP": "PF",
}

# Throttle between GraphQL requests (seconds); utils.http machinery also
# retries 429/503 with backoff.
REQUEST_INTERVAL = 1.0

# GraphQL queries, trimmed to the fields actually used.
LIVE_EVENT_LIST_QUERY = """\
query LiveEventList($artistId: String!, $liveEventsFirst: Int!) {
  artist(artistId: $artistId) {
    id
    name
    liveEvents(first: $liveEventsFirst, types: [CONCERT, FESTIVAL], statuses: [PENDING]) {
      edges {
        node {
          id
          countryCode
        }
      }
    }
  }
}"""

LIVE_EVENT_QUERY = """\
query LiveEvent($eventId: String!, $contributorsFirst: Int = 12) {
  liveEvent(liveEventId: $eventId) {
    id
    name
    startDate
    venue
    cityName
    sources {
      defaultUrl
    }
    types {
      isConcert
      isFestival
      isLivestreamConcert
      isLivestreamFestival
    }
    contributors(first: $contributorsFirst) {
      edges {
        node {
          ... on Artist {
            id
            name
          }
        }
      }
    }
  }
}"""


# ---------------------------------------------------------------------------
# Deezer GraphQL API (anonymous JWT, auto-refreshed)
# ---------------------------------------------------------------------------

_JWT = None
_JWT_FETCHED_AT = 0.0


def get_jwt(force=False):
    """Return a valid anonymous JWT, fetching/refreshing it when needed."""
    global _JWT, _JWT_FETCHED_AT
    if force or _JWT is None or time.monotonic() - _JWT_FETCHED_AT > JWT_MAX_AGE:
        session = utils.get_session()
        response = session.post(AUTH_URL, headers=DEFAULT_HEADERS, timeout=30)
        if not response.ok:
            # Some fronts accept GET on this endpoint.
            response = session.get(AUTH_URL, headers=DEFAULT_HEADERS, timeout=30)
        if not response.ok:
            raise Exception(
                f"Could not obtain an anonymous Deezer JWT ({response.status_code})"
            )
        token = (response.json() or {}).get("jwt")
        if not token:
            raise Exception("Anonymous Deezer auth did not return a jwt")
        _JWT = token
        _JWT_FETCHED_AT = time.monotonic()
    return _JWT


def _is_jwt_error(payload):
    for error in payload.get("errors") or []:
        message = (error.get("message") or "").lower()
        if "jwt" in message or "token" in message or "unauthorized" in message:
            return True
    return False


def graphql(operation, query, variables):
    """POST a GraphQL query through the shared session (throttled), with the
    anonymous JWT attached; refreshes the token once on auth errors."""
    session = utils.get_session()
    for attempt in range(2):
        headers = dict(DEFAULT_HEADERS)
        headers["Authorization"] = f"Bearer {get_jwt(force=attempt > 0)}"
        utils._throttle()
        response = session.post(
            API_URL,
            headers=headers,
            json={"operationName": operation, "variables": variables, "query": query},
            timeout=30,
        )
        utils._last_request_at = time.monotonic()
        if response.status_code in (401, 403) and attempt == 0:
            continue  # refresh the token and retry once
        if not response.ok:
            raise Exception(
                f"Deezer API error for {operation} ({response.status_code}): "
                f"{response.text[:200]}"
            )
        payload = response.json()
        if payload.get("errors"):
            if _is_jwt_error(payload) and attempt == 0:
                continue  # refresh the token and retry once
            raise Exception(
                f"Deezer API error for {operation}: {payload['errors'][0].get('message')}"
            )
        return payload.get("data") or {}
    raise Exception(f"Deezer API auth failed for {operation}")


def get_artist_events(deezer_id):
    """Return the list of pending live event ``(id, countryCode)`` pairs for an
    artist."""
    data = graphql(
        "LiveEventList",
        LIVE_EVENT_LIST_QUERY,
        {"artistId": str(deezer_id), "liveEventsFirst": EVENTS_FIRST},
    )
    artist = data.get("artist") or {}
    edges = ((artist.get("liveEvents") or {}).get("edges")) or []
    events = []
    for edge in edges:
        node = edge.get("node") or {}
        if node.get("id") and node.get("countryCode"):
            events.append((node["id"], node["countryCode"]))
    return events


def get_concert(event_id, country_code):
    """Fetch one live event and normalise it to the internal concert dict, or
    return ``None`` when the event is unusable (missing venue/city/date)."""
    data = graphql(
        "LiveEvent",
        LIVE_EVENT_QUERY,
        {"eventId": event_id, "contributorsFirst": 12},
    )
    event = data.get("liveEvent") or {}
    if not event:
        return None

    start = event.get("startDate")
    venue = (event.get("venue") or "").strip()
    city = (event.get("cityName") or "").strip()
    if not (start and venue and city):
        return None

    alpha_2 = utils.translate(country_code, COUNTRIES_MAPPING)
    country = utils.COUNTRIES.get(alpha_2)
    if country is None:
        return None

    types = event.get("types") or {}
    lineup = sorted({
        utils.translate((edge.get("node") or {}).get("name", "").strip(), utils.ARTISTS)
        for edge in ((event.get("contributors") or {}).get("edges")) or []
        if (edge.get("node") or {}).get("name")
    })
    if not lineup:
        return None

    return {
        "date": start,
        "location": {"country": country, "city": city, "name": venue},
        "artists": lineup,
        "festival": bool(types.get("isFestival") or types.get("isLivestreamFestival")),
        "ticket": ((event.get("sources") or {}).get("defaultUrl") or "").strip() or None,
    }


# ---------------------------------------------------------------------------
# events.py adapter
# ---------------------------------------------------------------------------

PROVIDER = "deezer"
SOCIAL_KEY = "deezer"
THROTTLE = (REQUEST_INTERVAL, 0.0)


def fetch_events(provider_id, artist_name):
    """Return this artist's concerts in events.py's common event model.

    Deezer is the only provider that flags festivals explicitly
    (``types.isFestival``); events.py trusts that flag and falls back to the
    line-up size heuristic for the others."""
    events = []
    for event_id, country_code in get_artist_events(provider_id):
        concert = get_concert(event_id, country_code)
        if concert is None:
            continue
        try:
            date = datetime.fromisoformat(concert["date"])
        except (KeyError, TypeError, ValueError):
            continue
        events.append({
            "date": date,
            "lineup": concert["artists"] or [artist_name],
            "location": concert["location"],
            "ticket": concert.get("ticket"),
            "festival": bool(concert.get("festival")),
            "source": PROVIDER,
        })
    return events


# ---------------------------------------------------------------------------
# Entry point: run this provider alone, through the shared events.py driver.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    import events  # local import: events.py imports this module

    events.run([sys.modules[__name__]])
