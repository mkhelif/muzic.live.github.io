#!/usr/bin/env python3
"""Fill the missing ``deezer`` id in artist fiches, via the Deezer API.

For every ``content/artists/*/index.md`` whose ``socials.deezer`` is absent or
empty, this script searches the public Deezer API (``api.deezer.com`` — no key
required) by the artist's title and, only on an **exact, normalised name
match**, writes the numeric Deezer artist id into the fiche's ``socials`` block.

It shares its plumbing with ``utils.py`` (HTTP session, slugging, front matter)
and mirrors ``fill_bandsintown.py``:

* ``DRY_RUN`` is ``True`` by default — it prints what it *would* write and
  changes nothing. Set it to ``False`` to persist.
* a candidate is accepted only when exactly one Deezer artist has the same
  normalised name; zero or several distinct matches are skipped, never guessed.
* artists searched within the last week are skipped (``lastUpdate`` key
  ``deezer-lookup``), to avoid re-querying the many artists Deezer does not
  know — kept separate from the ``deezer`` key used by ``deezer.py`` for events.

Run from the repository root::

    python3 snippets/fill_deezer.py
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

# Politeness delay between artists (seconds). Deezer's API is generous, but stay
# gentle; utils.http_get also retries 429/503 with backoff.
REQUEST_DELAY = 0.34

# Process at most this many artists (0 = no limit). Handy for a first test run.
LIMIT = 0

# Front-matter key (under `lastUpdate`) recording when we last searched Deezer
# for an artist's id — kept separate from the `deezer` events key.
LOOKUP_PROVIDER = "deezer-lookup"

# Public Deezer search endpoint (no API key needed).
SEARCH_URL = "https://api.deezer.com/search/artist?q={q}&limit=25"


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

def find_deezer_id(name):
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

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None, "none"

    ids = {
        str(artist["id"])
        for artist in items
        if isinstance(artist, dict) and artist.get("id")
        and normalize(artist.get("name")) == target
    }
    if len(ids) == 1:
        return ids.pop(), "ok"
    if len(ids) > 1:
        return None, "ambiguous"
    return None, "none"


# ---------------------------------------------------------------------------
# Writing the id into the fiche
# ---------------------------------------------------------------------------

def add_deezer(text, deezer_id):
    """Set ``deezer: "<id>"`` in the fiche's socials block.

    Fills the existing (empty) ``deezer:`` key when present; otherwise inserts a
    ``deezer`` line before ``qobuz`` (or as the first socials child), or falls
    back to a frontmatter round-trip for inline ``socials: { ... }``."""
    if re.search(r'^\s*deezer:\s*["\']?[^"\'\s].*$', text, re.MULTILINE):
        return text, False  # already has a value

    # Preferred: fill the existing empty deezer key.
    new_text, count = re.subn(
        r'^(\s*)deezer:\s*(?:""|\'\')?\s*$',
        rf'\g<1>deezer: "{deezer_id}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count:
        return new_text, True

    # No deezer key: insert before qobuz, else as the first socials child.
    qobuz = re.search(r"^([ \t]+)qobuz:.*\n", text, re.MULTILINE)
    if qobuz:
        idx = qobuz.start()
        indent = qobuz.group(1)
        return text[:idx] + f'{indent}deezer: "{deezer_id}"\n' + text[idx:], True

    socials = re.search(r"^socials:[ \t]*\n", text, re.MULTILINE)
    if socials:
        idx = socials.end()
        return text[:idx] + f'  deezer: "{deezer_id}"\n' + text[idx:], True

    # Inline socials or unusual layout: reserialise via frontmatter.
    post = frontmatter.loads(text)
    block = post.get("socials")
    if not isinstance(block, dict):
        return text, False
    block["deezer"] = deezer_id
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
        if socials.get("deezer"):
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
        f"Artists to search on Deezer: {total} "
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
            deezer_id, status = find_deezer_id(name)
        except utils.CloudflareBlocked as exc:
            print(f"\n{exc}")
            return
        except Exception:
            print(f"{prefix} ! {name}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
            sleep(REQUEST_DELAY)
            continue

        if status == "ok" and DRY_RUN:
            print(f"{prefix} [dry-run] {name} -> {deezer_id}")
            filled += 1
        elif status == "ok":
            new_text, changed = add_deezer(file.read_text(encoding="utf-8"), deezer_id)
            if changed:
                file.write_text(new_text, encoding="utf-8")
                print(f"{prefix} + {name} -> {deezer_id}")
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
