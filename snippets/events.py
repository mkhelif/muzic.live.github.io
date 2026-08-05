#!/usr/bin/env python3
"""Import concerts for every artist, from every provider, in one pass.

The single entry point for event imports. Each provider module
(``bandsintown.py``, ``apple.py``, ``deezer.py``) knows only how to *fetch*
its own concerts and normalise them; this module owns everything that used to
be copy-pasted three times over: merging, festival handling, venue and artist
creation, the file layout, and the per-provider ``lastUpdate`` bookkeeping.

``spotify.py`` is deliberately **not** wired in: it needs a manually refreshed
token, so it stays a standalone script.

The common event model
----------------------

``fetch_events(provider_id, artist_name)`` returns a list of::

    {
        "date":     datetime,        # tz-aware when the provider says so
        "lineup":   ["Name", ...],   # translated names, never empty
        "location": {                # already resolved by the provider
            "country": {"name": "France", "code": "FRA"},
            "city":      "Lyon",
            "name":      "Le Transbordeur",
            "latitude":  45.7,       # optional
            "longitude": 4.8,        # optional
        },
        "ticket":   "https://..." or None,
        "festival": bool,            # provider hint; see below
        "source":   "deezer",
    }

A provider module also declares ``PROVIDER`` (its ``lastUpdate`` key),
``SOCIAL_KEY`` (where its id lives under ``socials``) and ``THROTTLE``
(``(interval, jitter)`` seconds, applied to ``utils.http_get``).

Merging
-------

The same concert is usually listed by several providers with different
line-ups — Apple bills one artist per page, Bandsintown lists the bill,
Deezer lists contributors. Events are merged on (date, venue), and the union
of the line-ups wins, so one file is written per real concert instead of one
per provider.

Festivals
---------

A festival day is curated by hand as a festival event, so no event file is
written for it. But its line-up is still valuable: **the artist fiches are
created first, and only then is the event skipped**. Running this script
therefore populates the roster of every festival it meets, ready for the
festival day to be written up.

An event counts as a festival when the provider says so (Deezer's
``isFestival``) or when its line-up exceeds ``MAX_LINEUP``.

Run from the repository root::

    python3 snippets/events.py                       # every provider, upcoming
    python3 snippets/events.py deezer apple          # a subset
    python3 snippets/events.py setlistfm --past      # backfill past concerts
    python3 snippets/events.py --past --since=2020-01-01
    python3 snippets/bandsintown.py                  # one provider, same driver
"""

import sys
import traceback
from datetime import datetime
from os import listdir
from pathlib import Path

import apple
import bandsintown
import deezer
import setlistfm
import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Every provider events.py can drive. spotify.py is excluded on purpose: its
# tokens must be refreshed by hand.
PROVIDERS = [bandsintown, apple, deezer, setlistfm]

# Import past concerts as well, from the providers that expose them
# (``SUPPORTS_PAST``): setlist.fm's archive, and Bandsintown's few past
# entries. Apple and Deezer only ever publish upcoming shows.
#
# setlistfm is past-*only*, so with INCLUDE_PAST off it is simply inert.
#
# Off by default, and deliberately so: the archive is far larger than the
# upcoming calendar, so a full past import can add tens of thousands of event
# pages and inflate the Hugo build accordingly. Turn it on for a bounded set of
# artists (LIMIT, or one provider at a time) rather than the whole repository,
# and keep PAST_SINCE tight.
INCLUDE_PAST = True

# Oldest past event to import (YYYY-MM-DD), applied by the providers that can
# bound their query server-side. None = no floor.
PAST_SINCE = "1970-01-01"

# Beyond this many artists a concert is treated as a festival day.
MAX_LINEUP = 5

# Sanity window for an event date. A provider that parses dates out of page
# text can latch onto the wrong number — apple.py once dated concerts to 1945
# and 1962, having matched "Place du 8 Mai 1945" and "Rue du 19 Mars 1962" in
# the venue's address. Those are real French street names, so the parse looked
# perfectly valid. This is the backstop: whatever the source, an event outside
# the window is reported and dropped rather than filed.
MIN_EVENT_YEAR = 1950
FUTURE_TOLERANCE_DAYS = 365 * 3

# When True, report what would happen and create nothing.
DRY_RUN = False

# Process at most this many artists (0 = no limit).
LIMIT = 0


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def _merge_key(event):
    """Identify a real-world concert: same day, same venue."""
    location = event["location"]
    return (
        event["date"].date(),
        utils.format_filename(location["name"]),
        utils.format_filename(location["city"]),
    )


def merge(events):
    """Collapse the same concert reported by several providers into one, taking
    the union of the line-ups and the first ticket URL available."""
    merged = {}
    for event in events:
        key = _merge_key(event)
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(event, lineup=list(event["lineup"]),
                               sources=[event["source"]])
            continue

        for name in event["lineup"]:
            if name not in existing["lineup"]:
                existing["lineup"].append(name)
        existing["ticket"] = existing.get("ticket") or event.get("ticket")
        existing["festival"] = existing["festival"] or event["festival"]
        # Keep the most precise coordinates we were given.
        for axis in ("latitude", "longitude"):
            if not existing["location"].get(axis):
                existing["location"][axis] = event["location"].get(axis)
        if event["source"] not in existing["sources"]:
            existing["sources"].append(event["source"])

    for event in merged.values():
        event["lineup"] = sorted(set(event["lineup"]))
    return list(merged.values())


def implausible(event):
    """Return why an event's date can't be real, or ``None`` when it's fine."""
    date = event["date"]
    if date.year < MIN_EVENT_YEAR:
        return f"dated {date.date()} — before {MIN_EVENT_YEAR}"
    ahead = (date.replace(tzinfo=None) - datetime.now()).days
    if ahead > FUTURE_TOLERANCE_DAYS:
        return f"dated {date.date()} — more than {FUTURE_TOLERANCE_DAYS // 365} years ahead"
    return None


def is_festival(event):
    """A provider flag, or a line-up too large to be a normal bill."""
    return bool(event["festival"]) or len(event["lineup"]) > MAX_LINEUP


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_event(event):
    """Create the artists, the venue and the event file.

    Returns ``(path, status)`` where status is one of ``"created"``,
    ``"exists"``, ``"festival"`` or ``"implausible"``.

    The artists are created **before** the festival check on purpose: a
    festival day is written up by hand later, and having every act on the bill
    already on file is exactly what that work needs."""
    # A bad date must not create anything at all — not even the artists.
    if implausible(event):
        return None, "implausible"

    if DRY_RUN:
        status = "festival" if is_festival(event) else "created"
        return None, status

    # 1. Artists first — this is what a festival line-up leaves behind.
    artist_ids = [utils.get_or_create_artist(name) for name in event["lineup"]]

    # 2. Only now decide whether the event itself is ours to write.
    if is_festival(event):
        return None, "festival"

    venue_id = utils.get_or_create_venue(event["location"])

    date = event["date"]
    directory = Path(f"./content/events/{date.year}/{date.month:02d}/{date.day:02d}")
    directory.mkdir(parents=True, exist_ok=True)
    filename = "-".join(utils.format_filename(n) for n in event["lineup"]) + ".md"
    event_file = directory.joinpath(filename)
    if event_file.exists():
        return event_file, "exists"

    lines = [
        "---",
        f"date: {date.isoformat()}",
        f'venue: "{venue_id}"',
        "artists:",
    ]
    lines += [f'  - "{aid}"' for aid in artist_ids]
    ticket = utils.clean_ticket_url(event.get("ticket"))
    if ticket:
        lines += ["tickets:", f'  web: "{utils.yaml_quote(ticket)}"']
    lines += ["---", ""]
    event_file.write_text("\n".join(lines), encoding="UTF-8")
    return event_file, "created"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def collect(providers, data, name):
    """Fetch and merge one artist's events from every provider that has an id
    for them and hasn't been refreshed recently.

    Returns ``(events, used, skipped)``: the merged events, the providers that
    were queried (to stamp afterwards) and those skipped as fresh."""
    socials = data.get("socials")
    socials = socials if isinstance(socials, dict) else {}

    raw, used, skipped = [], [], []
    for provider in providers:
        provider_id = socials.get(provider.SOCIAL_KEY)
        if not provider_id:
            continue
        # Only stamp providers whose id exists, and skip the fresh ones.
        if not utils.is_stale(data, provider.PROVIDER):
            skipped.append(provider.PROVIDER)
            continue

        interval, jitter = getattr(provider, "THROTTLE", (0.0, 0.0))
        utils.MIN_REQUEST_INTERVAL = interval
        utils.REQUEST_JITTER = jitter

        # Only ask for past events from providers that actually have them.
        past = INCLUDE_PAST and getattr(provider, "SUPPORTS_PAST", False)
        raw.extend(provider.fetch_events(
            str(provider_id), name, past=past, since=PAST_SINCE if past else None))
        used.append(provider)

    return merge(raw), used, skipped


def run(providers=None):
    """Iterate over every artist, importing events from ``providers``."""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    providers = providers or PROVIDERS
    names = ", ".join(p.PROVIDER for p in providers)
    if INCLUDE_PAST:
        past_from = [p.PROVIDER for p in providers
                     if getattr(p, "SUPPORTS_PAST", False)]
        window = (f"upcoming + past since {PAST_SINCE} (from {', '.join(past_from)})"
                  if past_from else "upcoming only (no selected provider has past events)")
    else:
        window = "upcoming only"
    print(f"Importing events from: {names}. {window}. "
          f"Mode: {'DRY-RUN' if DRY_RUN else 'WRITE'}.\n")

    processed = created = festivals = dropped = errors = 0
    for slug in sorted(listdir("./content/artists")):
        file = Path(f"./content/artists/{slug}/index.md")
        if not file.exists():
            continue
        try:
            data = utils.load_frontmatter(file)
        except Exception:
            errors += 1
            continue

        name = data.get("title")
        if not name:
            continue

        socials = data.get("socials")
        socials = socials if isinstance(socials, dict) else {}
        if not any(socials.get(p.SOCIAL_KEY) for p in providers):
            continue

        try:
            events, used, skipped = collect(providers, data, name)
        except utils.CloudflareBlocked as blocked:
            print(f"\n{blocked}")
            break
        except Exception:
            print(f"! {name}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
            continue

        if not used:
            if skipped:
                print(f"{name} (skipped: refreshed recently — {', '.join(skipped)})")
            continue

        processed += 1
        print(f"{name} [{', '.join(p.PROVIDER for p in used)}]")
        for event in events:
            try:
                path, status = write_event(event)
            except Exception:
                print(f"  ! {traceback.format_exc().splitlines()[-1]}")
                errors += 1
                continue
            location = event["location"]
            if status == "created":
                created += 1
                print(f"  + {path or event['date'].date()}")
            elif status == "implausible":
                dropped += 1
                print(f"  ! dropped: {implausible(event)} "
                      f"({', '.join(event['lineup'])} @ {location['name']}) "
                      f"[{', '.join(event.get('sources', []))}]")
            elif status == "festival":
                festivals += 1
                print(f"  - (festival) {event['date'].date()}: "
                      f"{', '.join(event['lineup'])} "
                      f"({location['name']}, {location['city']}) "
                      f"— artists created, event left to curation")

        # Stamp only the providers that were actually queried.
        if not DRY_RUN:
            for provider in used:
                utils.set_last_update(file, provider.PROVIDER)

        if LIMIT and processed >= LIMIT:
            break

    print(f"\nDone. artists={processed}, events_created={created}, "
          f"festivals_skipped={festivals}, implausible_dates={dropped}, "
          f"errors={errors}")


def main():
    global INCLUDE_PAST, PAST_SINCE

    args = sys.argv[1:]
    if "--past" in args:
        INCLUDE_PAST = True
        args.remove("--past")
    for arg in list(args):
        if arg.startswith("--since="):
            PAST_SINCE = arg.split("=", 1)[1] or None
            args.remove(arg)

    wanted = [a.lower() for a in args]
    if not wanted:
        return run(PROVIDERS)
    known = {p.PROVIDER: p for p in PROVIDERS}
    unknown = [w for w in wanted if w not in known]
    if unknown:
        print(f"Unknown provider(s): {', '.join(unknown)}. "
              f"Available: {', '.join(known)}.")
        raise SystemExit(1)
    return run([known[w] for w in wanted])


if __name__ == "__main__":
    main()
