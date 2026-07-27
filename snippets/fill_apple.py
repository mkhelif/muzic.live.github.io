#!/usr/bin/env python3
"""Fill the missing ``apple`` id in artist fiches, via the iTunes Search API.

For every ``content/artists/*/index.md`` whose ``socials.apple`` is absent or
empty, this script searches Apple's public iTunes Search API
(``itunes.apple.com/search`` — no key or registration required) by the artist's
title and, only on an **exact, normalised name match**, writes the numeric
Apple Music artist id into the fiche's ``socials`` block. That id is the same
one used by ``music.apple.com/.../artist/<id>`` and by ``apple.py`` to fetch
concerts.

It shares its plumbing with ``utils.py`` and mirrors ``fill_deezer.py``:

* ``DRY_RUN`` is ``True`` by default — it prints what it *would* write and
  changes nothing. Set it to ``False`` to persist.
* a candidate is accepted only when exactly one Apple artist has the same
  normalised name; zero or several distinct matches are skipped, never guessed.
* artists searched within the last week are skipped (``lastUpdate`` key
  ``apple-lookup``), to avoid re-querying the many artists Apple does not
  know — kept separate from the ``apple`` key used by ``apple.py`` for events.

Run from the repository root::

    python3 snippets/fill_apple.py
"""

from os import listdir
from pathlib import Path
from time import sleep
from urllib.parse import quote

import re
import sys
import traceback

from unidecode import unidecode
import frontmatter

import utils


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When True, only report proposed changes; write nothing.
DRY_RUN = False

# Politeness delay between artists (seconds). Apple rate-limits the iTunes
# Search API to ~20 requests/minute; utils.http_get also retries 429/503.
REQUEST_DELAY = 3.5

# Process at most this many artists (0 = no limit). Handy for a first test run.
LIMIT = 0

# Front-matter key (under `lastUpdate`) recording when we last searched Apple
# for an artist's id — kept separate from the `apple` events key.
LOOKUP_PROVIDER = "apple-lookup"

# Public iTunes Search endpoint (no API key needed).
SEARCH_URL = "https://itunes.apple.com/search?term={q}&entity=musicArtist&limit=25"


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def normalize(value):
    """Collapse a name to comparable form: ascii, lowercase, alphanumerics only
    (so "Kölsch" == "kolsch", "A Perfect Circle" == "aperfectcircle")."""
    return re.sub(r"[^a-z0-9]", "", unidecode(value or "").lower())


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def find_apple_id(name):
    """Return ``(id, status)`` for an artist name.

    status is one of: ``"ok"`` (single exact match), ``"none"`` (no exact
    match), ``"ambiguous"`` (several distinct ids share the same name)."""
    target = normalize(name)
    if not target:
        return None, "none"

    response = utils.http_get(SEARCH_URL.format(q=quote(name)))
    if not response.ok:
        return None, "none"
    try:
        data = response.json()
    except ValueError:
        return None, "none"

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return None, "none"

    ids = {
        str(artist["artistId"])
        for artist in results
        if isinstance(artist, dict) and artist.get("artistId")
        and normalize(artist.get("artistName")) == target
    }
    if len(ids) == 1:
        return ids.pop(), "ok"
    if len(ids) > 1:
        return None, "ambiguous"
    return None, "none"


# ---------------------------------------------------------------------------
# Writing the id into the fiche
# ---------------------------------------------------------------------------

def add_apple(text, apple_id):
    """Set ``apple: "<id>"`` in the fiche's socials block.

    Fills the existing (empty) ``apple:`` key when present; otherwise inserts an
    ``apple`` line before ``deezer`` (or as the first socials child), or falls
    back to a frontmatter round-trip for inline ``socials: { ... }``."""
    if re.search(r'^\s*apple:\s*["\']?[^"\'\s].*$', text, re.MULTILINE):
        return text, False  # already has a value

    # Preferred: fill the existing empty apple key.
    new_text, count = re.subn(
        r'^(\s*)apple:\s*(?:""|\'\')?\s*$',
        rf'\g<1>apple: "{apple_id}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count:
        return new_text, True

    # No apple key: insert before deezer, else as the first socials child.
    deezer = re.search(r"^([ \t]+)deezer:.*\n", text, re.MULTILINE)
    if deezer:
        idx = deezer.start()
        indent = deezer.group(1)
        return text[:idx] + f'{indent}apple: "{apple_id}"\n' + text[idx:], True

    socials = re.search(r"^socials:[ \t]*\n", text, re.MULTILINE)
    if socials:
        idx = socials.end()
        return text[:idx] + f'  apple: "{apple_id}"\n' + text[idx:], True

    # Inline socials or unusual layout: reserialise via frontmatter.
    post = frontmatter.loads(text)
    block = post.get("socials")
    if not isinstance(block, dict):
        return text, False
    block["apple"] = apple_id
    post["socials"] = block
    return frontmatter.dumps(post) + "\n", True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

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
        socials = data.get("socials")
        socials = socials if isinstance(socials, dict) else {}
        if socials.get("apple"):
            skipped_have += 1
            continue
        name = data.get("title")
        if not name:
            continue
        # Skip artists we already searched within the last week.
        if not utils.is_stale(data, LOOKUP_PROVIDER):
            skipped_fresh += 1
            continue
        candidates.append((file, name))

    if LIMIT:
        candidates = candidates[:LIMIT]

    total = len(candidates)
    print(
        f"Artists to search on Apple Music: {total} "
        f"(already have id: {skipped_have}, "
        f"searched within the last week: {skipped_fresh}). "
        f"Mode: {'DRY-RUN' if DRY_RUN else 'WRITE'}."
    )
    if total == 0:
        print("Nothing to do.")
        return

    filled = no_match = ambiguous = errors = 0
    for index, (file, name) in enumerate(candidates, start=1):
        prefix = f"[{index}/{total}]"
        try:
            apple_id, status = find_apple_id(name)
        except utils.CloudflareBlocked as exc:
            print(f"\n{exc}")
            return
        except Exception:
            print(f"{prefix} ! {name}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
            sleep(REQUEST_DELAY)
            continue

        if status == "ok" and DRY_RUN:
            print(f"{prefix} [dry-run] {name} -> {apple_id}")
            filled += 1
        elif status == "ok":
            new_text, changed = add_apple(file.read_text(encoding="utf-8"), apple_id)
            if changed:
                file.write_text(new_text, encoding="utf-8")
                print(f"{prefix} + {name} -> {apple_id}")
                filled += 1
            else:
                print(f"{prefix} = {name}: already had id, skipped")
        elif status == "ambiguous":
            print(f"{prefix} ? {name}: several matching ids, skipped")
            ambiguous += 1
            if not DRY_RUN:
                utils.set_last_update(file, LOOKUP_PROVIDER)
        else:
            print(f"{prefix} - {name}: no match")
            no_match += 1
            if not DRY_RUN:
                utils.set_last_update(file, LOOKUP_PROVIDER)

        sleep(REQUEST_DELAY)

    print(
        "\nDone. "
        f"{'would fill' if DRY_RUN else 'filled'}={filled}, "
        f"no_match={no_match}, ambiguous={ambiguous}, "
        f"already_had={skipped_have}, searched_recently={skipped_fresh}, "
        f"errors={errors + parse_errors}"
    )
    if DRY_RUN and filled:
        print("DRY_RUN is on — set DRY_RUN = False to write these ids.")


if __name__ == "__main__":
    main()
