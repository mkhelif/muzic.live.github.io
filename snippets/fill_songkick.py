#!/usr/bin/env python3
"""Fill the missing ``songkick`` id in artist fiches.

The discovery half of the Songkick pair, mirroring ``fill_bandsintown.py`` /
``bandsintown.py`` and ``fill_musicbrainz.py`` / ``musicbrainz.py``:

* **this script** finds the numeric Songkick artist id and writes it into
  ``socials.songkick``;
* ``songkick.py`` then uses that id to import concerts — upcoming *and* past.

It resolves the id two ways, cheapest and safest first:

1. **By MusicBrainz id.** Songkick accepts ``artists/mbid:{mbid}/calendar``,
   so a fiche that already carries ``socials.musicbrainz`` needs no name
   matching at all: whichever performance in the response names this artist
   carries their Songkick id. Zero ambiguity — this is the reason to run
   ``fill_musicbrainz.py`` first.
2. **By name search**, as a fallback: ``search/artists.json``, accepted only
   when exactly one result is an exact, normalised name match. Zero or several
   matches are skipped and reported, never guessed.

Note that (1) only works when the artist has events on file at Songkick;
otherwise the calendar comes back empty and we fall through to (2).

Needs an API key — request one at
https://www.songkick.com/api_key_requests/new, then::

    export SONGKICK_API_KEY=...
    python3 snippets/fill_songkick.py

* ``DRY_RUN`` is ``True`` by default: it reports what it would write and
  changes nothing.
* artists searched within the last month are skipped (``lastUpdate`` key
  ``songkick-lookup``, separate from the ``songkick`` key ``songkick.py`` uses
  for event refreshes).
"""

import re
import sys
import traceback
from os import listdir
from pathlib import Path

import frontmatter
from unidecode import unidecode

import musicbrainz as mb
import songkick as sk
import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When True, only report proposed changes; write nothing.
DRY_RUN = True

# Process at most this many artists (0 = no limit).
LIMIT = 0

# Front-matter key (under `lastUpdate`) recording when we last *searched*
# Songkick for an artist's id.
LOOKUP_PROVIDER = "songkick-lookup"


def normalize(value):
    """ascii, lowercase, alphanumerics only — for name comparison."""
    return re.sub(r"[^a-z0-9]", "", unidecode(value or "").lower())


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def find_by_mbid(mbid, name, key):
    """Return the Songkick artist id via the MusicBrainz id, or ``None``.

    The calendar is addressed by mbid, so every event it returns belongs to
    this artist; we just read the id off the matching performance."""
    target = normalize(name)
    for event in sk.get_artist_events_by_mbid(mbid, key=key):
        for performance in event.get("performance") or []:
            artist = (performance or {}).get("artist") or {}
            if not artist.get("id"):
                continue
            if normalize(artist.get("displayName")) == target:
                return str(artist["id"])
    return None


def find_by_name(name, key):
    """Return ``(id, status)`` from the artist search: ``"ok"`` on a single
    exact match, else ``"ambiguous"`` or ``"none"``."""
    target = normalize(name)
    if not target:
        return None, "none"

    ids = {
        str(artist["id"])
        for artist in sk.search_artists(name, key=key)
        if isinstance(artist, dict) and artist.get("id")
        and normalize(artist.get("displayName")) == target
    }
    if len(ids) == 1:
        return ids.pop(), "ok"
    return None, "ambiguous" if ids else "none"


# ---------------------------------------------------------------------------
# Candidate collection
# ---------------------------------------------------------------------------

def load_candidates():
    """Return the artists with no ``socials.songkick`` yet, skipping those
    searched within the last month."""
    candidates = []
    skipped_have = skipped_fresh = parse_errors = 0
    for slug in sorted(listdir("./content/artists")):
        file = Path(f"./content/artists/{slug}/index.md")
        if not file.exists():
            continue
        try:
            data = frontmatter.loads(file.read_text(encoding="utf-8"))
        except Exception:
            print(f"! {slug}: cannot parse front matter")
            parse_errors += 1
            continue

        title = data.get("title")
        if not title:
            continue
        socials = data.get("socials")
        socials = socials if isinstance(socials, dict) else {}

        if socials.get("songkick"):
            skipped_have += 1
            continue
        if not utils.is_stale(data, LOOKUP_PROVIDER):
            skipped_fresh += 1
            continue

        candidates.append((file, title, socials.get("musicbrainz")))

    return candidates, skipped_have, skipped_fresh, parse_errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    try:
        key = sk.api_key()
    except sk.MissingApiKey as error:
        print(error)
        raise SystemExit(1)

    utils.MIN_REQUEST_INTERVAL = sk.REQUEST_INTERVAL
    utils.REQUEST_JITTER = sk.REQUEST_JITTER

    candidates, skipped_have, skipped_fresh, parse_errors = load_candidates()
    if LIMIT:
        candidates = candidates[:LIMIT]

    total = len(candidates)
    with_mbid = sum(1 for _, _, mbid in candidates if mbid)
    eta = total * (sk.REQUEST_INTERVAL + sk.REQUEST_JITTER / 2) / 3600
    print(
        f"Artists to resolve on Songkick: {total} "
        f"({with_mbid} via their MusicBrainz id, {total - with_mbid} by name). "
        f"Already have an id: {skipped_have}, searched recently: {skipped_fresh}. "
        f"Mode: {'DRY-RUN' if DRY_RUN else 'WRITE'}. ~{eta:.1f}h+. "
        f"Interrupting is safe: progress is recorded per artist."
    )
    if total == 0:
        print("Nothing to do.")
        return

    filled = by_mbid = no_match = ambiguous = errors = 0
    for index, (file, title, mbid) in enumerate(candidates, start=1):
        prefix = f"[{index}/{total}]"
        songkick_id, how = None, ""
        try:
            if mbid:
                songkick_id = find_by_mbid(mbid, title, key)
                if songkick_id:
                    how, by_mbid = " (via musicbrainz)", by_mbid + 1
            if songkick_id is None:
                songkick_id, status = find_by_name(title, key)
                if songkick_id is None:
                    if status == "ambiguous":
                        print(f"{prefix} ? {title}: several matching ids, skipped")
                        ambiguous += 1
                    else:
                        print(f"{prefix} - {title}: no match")
                        no_match += 1
                    continue

            if not DRY_RUN:
                text, changed = mb.add_social(
                    file.read_text(encoding="utf-8"), "songkick", songkick_id)
                if changed:
                    file.write_text(text, encoding="utf-8")
            tag = "[dry-run] " if DRY_RUN else "+ "
            print(f"{prefix} {tag}{title} -> {songkick_id}{how}")
            filled += 1
        except Exception:
            print(f"{prefix} ! {title}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
            continue
        finally:
            if not DRY_RUN:
                utils.set_last_update(file, LOOKUP_PROVIDER)

    print(
        "\nDone. "
        f"{'would fill' if DRY_RUN else 'filled'}={filled} (via mbid: {by_mbid}), "
        f"no_match={no_match}, ambiguous={ambiguous}, "
        f"already_had={skipped_have}, searched_recently={skipped_fresh}, "
        f"errors={errors + parse_errors}"
    )
    if DRY_RUN and filled:
        print("DRY_RUN is on — set DRY_RUN = False to write these ids.")


if __name__ == "__main__":
    main()
