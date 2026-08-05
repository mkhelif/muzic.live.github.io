#!/usr/bin/env python3
"""Fill the missing ``musicbrainz`` id in artist fiches, by name search.

The discovery half of the MusicBrainz pair, mirroring
``fill_bandsintown.py`` / ``bandsintown.py``:

* **this script** searches MusicBrainz by name for the fiches whose
  ``socials.musicbrainz`` is still empty, and writes the id it confirms;
* ``musicbrainz.py`` then uses that id to refresh members, socials and
  lifespan with a single lookup per artist.

Splitting them is the speed-up. Searching is the expensive, failure-prone
half: it costs an extra request per artist and can return nothing (or several
candidates) for a renamed or oddly-spelled artist. Once the id is stored, that
cost is paid once and never again — ``musicbrainz.py`` goes straight to the
lookup, and every subsequent refresh is one request instead of two.

Having found an id, this script immediately reuses ``musicbrainz.py``'s
``process_artist`` to apply everything the follow-up lookup returns, so a
freshly discovered artist is filled in the same run (search + lookup, exactly
what the previous single script cost) rather than waiting for the next one.

Safety, unchanged: an id is accepted only when exactly one MusicBrainz artist
is a 100-score, exact name match — narrowed by the fiche's local ``type``
(``band`` -> ``Group``, ``person`` -> ``Person``) when known. Zero or several
matches are skipped and reported, never guessed.

* ``DRY_RUN`` mirrors ``musicbrainz.DRY_RUN`` — set it there.
* artists searched within the last month are skipped (``lastUpdate`` key
  ``musicbrainz-lookup``, kept separate from the ``musicbrainz`` key used by
  ``musicbrainz.py`` for refreshes), so repeated runs don't re-search the many
  artists MusicBrainz simply doesn't know.

Run from the repository root::

    python3 snippets/fill_musicbrainz.py
"""

import sys
import traceback
from os import listdir
from pathlib import Path
from urllib.parse import quote

import frontmatter

import musicbrainz as mb
import utils


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Process at most this many artists (0 = no limit). Handy for a first test run.
LIMIT = 0

# Front-matter key (under `lastUpdate`) recording when we last *searched*
# MusicBrainz for an artist's id — separate from musicbrainz.py's refresh key.
LOOKUP_PROVIDER = "musicbrainz-lookup"

SEARCH_URL = "https://musicbrainz.org/ws/2/artist/?query={q}&fmt=json&limit=25"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def find_musicbrainz_id(name, local_type):
    """Return the single MusicBrainz id whose name/alias is an exact,
    100-score match for ``name`` (optionally narrowed by ``local_type``), or
    ``None`` when zero or several match."""
    target = mb.normalize(name)
    if not target:
        return None

    query = f'artist:"{name.replace(chr(34), chr(92) + chr(34))}"'
    wanted_type = mb.LOCAL_TYPE_TO_MUSICBRAINZ.get(local_type)
    if wanted_type:
        query += f" AND type:{wanted_type.lower()}"

    response = utils.http_get(SEARCH_URL.format(q=quote(query)), headers=mb.HEADERS)
    if not response.ok:
        return None
    try:
        data = response.json()
    except ValueError:
        return None

    candidates = {}
    for candidate in data.get("artists") or []:
        if int(candidate.get("score") or 0) != 100:
            continue
        if wanted_type and candidate.get("type") != wanted_type:
            continue
        names = {candidate.get("name"), candidate.get("sort-name")}
        for alias in candidate.get("aliases") or []:
            names.add(alias.get("name"))
            names.add(alias.get("sort-name"))
        if any(mb.normalize(n) == target for n in names if n):
            candidates[candidate["id"]] = candidate

    return next(iter(candidates)) if len(candidates) == 1 else None


# ---------------------------------------------------------------------------
# Candidate collection
# ---------------------------------------------------------------------------

def load_candidates():
    """Return the artists with no ``socials.musicbrainz`` id yet, skipping
    those searched within the last month."""
    candidates = []
    skipped_have = skipped_fresh = parse_errors = 0
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
        socials = data.get("socials")
        socials = socials if isinstance(socials, dict) else {}

        if socials.get("musicbrainz"):
            skipped_have += 1
            continue
        if not utils.is_stale(data, LOOKUP_PROVIDER):
            skipped_fresh += 1
            continue

        artist_type = data.get("type")
        candidates.append((
            file, title, artist_type,
            {p for p in mb.SOCIAL_PATTERNS if not socials.get(p)},
            artist_type == "band" and not mb.has_members_block(text),
            artist_type == "person" and not mb.has_lifespan_block(text),
        ))

    return candidates, skipped_have, skipped_fresh, parse_errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # Throttle every MusicBrainz request (including http_get's own retries).
    utils.MIN_REQUEST_INTERVAL = mb.REQUEST_INTERVAL
    utils.REQUEST_JITTER = mb.REQUEST_JITTER

    candidates, skipped_have, skipped_fresh, parse_errors = load_candidates()
    if LIMIT:
        candidates = candidates[:LIMIT]

    total = len(candidates)
    # Search, then a lookup only for the artists actually matched.
    eta = total * (mb.REQUEST_INTERVAL + mb.REQUEST_JITTER / 2) / 3600
    print(
        f"Artists to search on MusicBrainz: {total} "
        f"(already have an id: {skipped_have}, "
        f"searched within the last month: {skipped_fresh}). "
        f"Mode: {'DRY-RUN' if mb.DRY_RUN else 'WRITE'}. "
        f"~{eta:.1f}h+ (1 search each, plus 1 lookup per match). "
        f"Interrupting is safe: progress is recorded per artist."
    )
    if total == 0:
        print("Nothing to do.")
        return

    filled = no_match = errors = 0
    for index, (file, title, artist_type, needs_social, needs_members, needs_dates) in enumerate(
        candidates, start=1
    ):
        prefix = f"[{index}/{total}]"
        try:
            mbid = find_musicbrainz_id(title, artist_type)
            if mbid is None:
                print(f"{prefix} - {title}: no match")
                no_match += 1
                continue

            # Write the id first so it is kept even if the follow-up fails,
            # then let musicbrainz.py fill everything else from one lookup.
            if not mb.DRY_RUN:
                text, changed = mb.add_social(
                    file.read_text(encoding="utf-8"), "musicbrainz", mbid)
                if changed:
                    file.write_text(text, encoding="utf-8")

            changes = mb.process_artist(
                file, title, artist_type, mbid,
                needs_social - {"musicbrainz"}, needs_members, needs_dates,
            )
            tag = "[dry-run] " if mb.DRY_RUN else "+ "
            detail = f": {', '.join(changes)}" if changes else ""
            print(f"{prefix} {tag}{title} -> {mbid}{detail}")
            filled += 1
        except utils.CloudflareBlocked as exc:
            print(f"\n{exc}")
            break
        except Exception:
            print(f"{prefix} ! {title}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
            continue
        finally:
            if not mb.DRY_RUN:
                utils.set_last_update(file, LOOKUP_PROVIDER)

    print(
        "\nDone. "
        f"{'would fill' if mb.DRY_RUN else 'filled'}={filled}, "
        f"no_match={no_match}, already_had={skipped_have}, "
        f"searched_recently={skipped_fresh}, errors={errors + parse_errors}"
    )
    if mb.DRY_RUN and filled:
        print("DRY_RUN is on (musicbrainz.py) — set it to False to write.")


if __name__ == "__main__":
    main()
