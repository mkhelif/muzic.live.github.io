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
# Event writing
# ---------------------------------------------------------------------------

def write_event(concert):
    """Create a single event file (and its venue / line-up artists). Returns the
    path if written, or ``None`` when skipped."""
    date = datetime.fromisoformat(concert["date"])
    date_format = f"{date.year}/{date.month:02d}/{date.day:02d}"

    if len(concert["artists"]) > MAX_LINEUP:
        return None

    artist_ids = [utils.get_or_create_artist(name) for name in concert["artists"]]
    location_id = utils.get_or_create_location(concert["location"])

    directory = Path(f"./content/events/{date_format}")
    directory.mkdir(parents=True, exist_ok=True)
    filename = "-".join(utils.format_filename(name) for name in concert["artists"]) + ".md"
    event_file = directory.joinpath(filename)
    if event_file.exists():
        return None

    lines = [
        "---",
        f"date: {date.isoformat()}",
        f'venue: "{location_id}"',
        "artists:",
    ]
    lines += [f'  - "{aid}"' for aid in artist_ids]
    ticket = utils.clean_ticket_url(concert.get("ticket"))
    if ticket:
        lines += ["tickets:", f'  web: "{utils.yaml_quote(ticket)}"']
    lines += ["---", ""]
    event_file.write_text("\n".join(lines), encoding="UTF-8")
    return event_file


# ---------------------------------------------------------------------------
# Entry point: iterate over every artist declaring a Deezer id.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    utils.MIN_REQUEST_INTERVAL = REQUEST_INTERVAL

    for artist in sorted(listdir("./content/artists")):
        file = Path(f"./content/artists/{artist}/index.md")
        if not file.exists():
            continue

        data = utils.load_frontmatter(file)
        socials = data.get("socials", None)
        deezer_id = socials.get("deezer", None) if socials else None
        if not deezer_id:
            continue

        name = data.get("title", None)

        # Skip artists already refreshed from Deezer within the last week.
        if not utils.is_stale(data, "deezer"):
            print(f"{name} (skipped: refreshed within the last week)")
            continue

        print(name)
        try:
            for event_id, country_code in get_artist_events(deezer_id):
                concert = get_concert(event_id, country_code)
                if concert is None:
                    continue

                # Festivals are better handled as curated festival day events.
                if concert["festival"]:
                    location = concert["location"]
                    print(
                        f"  - (festival) {concert['date'][:10]}: "
                        f"{', '.join(concert['artists'])} "
                        f"({location['name']}, {location['city']}, {location['country']['name']})"
                    )
                    continue

                created = write_event(concert)
                if created is not None:
                    print(f"  + {created}")

            # Mark this artist as refreshed from Deezer today.
            utils.set_last_update(file, "deezer")
        except Exception:
            print(traceback.format_exc())
