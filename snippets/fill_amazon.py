#!/usr/bin/env python3
"""Fill the missing ``amazon`` id in artist fiches, via MusicBrainz.

Amazon Music has no public, unauthenticated search API (its web player is an
SPA backed by an internal, session-authenticated endpoint, and amazon.com
itself blocks scraping). MusicBrainz aggregates external streaming links
instead, including ``https://music.amazon.com/artists/<id>`` URL relations
(relationship type "streaming"), sourced from Wikidata and manual edits — so
this script searches MusicBrainz's public API (``musicbrainz.org`` — no key
required) by the artist's title and, only on an **exact, unambiguous match**
with exactly one Amazon Music relation, writes that id into the fiche's
``socials`` block.

It shares its plumbing with ``utils.py`` (HTTP session, slugging, front
matter) and mirrors ``fill_deezer.py`` / ``fill_apple.py``:

* ``DRY_RUN`` is ``True`` by default — it prints what it *would* write and
  changes nothing. Set it to ``False`` to persist.
* a candidate is accepted only when exactly one MusicBrainz artist has a
  100-score exact name match, and that artist carries exactly one Amazon
  Music relation; zero or several distinct matches are skipped, never
  guessed.
* artists searched within the last week are skipped (``lastUpdate`` key
  ``amazon-lookup``), to avoid re-querying the many artists with no
  MusicBrainz entry or no Amazon Music link.

Run from the repository root::

    python3 snippets/fill_amazon.py
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

# Politeness delay between MusicBrainz requests (seconds). MusicBrainz's
# anonymous rate limit is ~1 request/second; utils.http_get also retries
# 429/503 with backoff.
REQUEST_DELAY = 1.1

# Process at most this many artists (0 = no limit). Handy for a first test run.
LIMIT = 0

# Front-matter key (under `lastUpdate`) recording when we last searched
# MusicBrainz for an artist's Amazon Music id.
LOOKUP_PROVIDER = "amazon-lookup"

# Public MusicBrainz API (no API key needed).
SEARCH_URL = "https://musicbrainz.org/ws/2/artist/?query={q}&fmt=json&limit=25"
LOOKUP_URL = "https://musicbrainz.org/ws/2/artist/{mbid}?inc=url-rels&fmt=json"

AMAZON_ARTIST_RE = re.compile(r"music\.amazon\.[a-z.]+/artists/([A-Za-z0-9]+)")


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

def find_musicbrainz_id(name):
    """Return the single MusicBrainz id whose name/alias is an exact,
    100-score match for ``name``, or ``None`` when zero or several match."""
    target = normalize(name)
    if not target:
        return None

    query = f'artist:"{name.replace(chr(34), chr(92) + chr(34))}"'
    response = utils.http_get(SEARCH_URL.format(q=quote(query)))
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
        names = {candidate.get("name"), candidate.get("sort-name")}
        for alias in candidate.get("aliases") or []:
            names.add(alias.get("name"))
            names.add(alias.get("sort-name"))
        if any(normalize(n) == target for n in names if n):
            candidates[candidate["id"]] = candidate

    return next(iter(candidates)) if len(candidates) == 1 else None


def find_amazon_id(name):
    """Return ``(id, status)`` for an artist name.

    status is one of: ``"ok"`` (single exact match), ``"none"`` (no exact
    MusicBrainz match, or no Amazon Music relation on it), ``"ambiguous"``
    (several distinct Amazon ids on the same MusicBrainz artist)."""
    mbid = find_musicbrainz_id(name)
    if mbid is None:
        return None, "none"

    sleep(REQUEST_DELAY)
    response = utils.http_get(LOOKUP_URL.format(mbid=mbid))
    if not response.ok:
        return None, "none"
    try:
        data = response.json()
    except ValueError:
        return None, "none"

    ids = set()
    for relation in data.get("relations") or []:
        url = (relation.get("url") or {}).get("resource") or ""
        match = AMAZON_ARTIST_RE.search(url)
        if match:
            ids.add(match.group(1))

    if len(ids) == 1:
        return ids.pop(), "ok"
    if len(ids) > 1:
        return None, "ambiguous"
    return None, "none"


# ---------------------------------------------------------------------------
# Writing the id into the fiche
# ---------------------------------------------------------------------------

def add_amazon(text, amazon_id):
    """Set ``amazon: "<id>"`` in the fiche's socials block.

    Fills the existing (empty) ``amazon:`` key when present; otherwise inserts
    an ``amazon`` line before ``apple`` (or as the first socials child), or
    falls back to a frontmatter round-trip for inline ``socials: { ... }``."""
    if re.search(r'^\s*amazon:\s*["\']?[^"\'\s].*$', text, re.MULTILINE):
        return text, False  # already has a value

    # Preferred: fill the existing empty amazon key.
    new_text, count = re.subn(
        r'^(\s*)amazon:\s*(?:""|\'\')?\s*$',
        rf'\g<1>amazon: "{amazon_id}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count:
        return new_text, True

    # No amazon key: insert before apple, else as the first socials child.
    apple = re.search(r"^([ \t]+)apple:.*\n", text, re.MULTILINE)
    if apple:
        idx = apple.start()
        indent = apple.group(1)
        return text[:idx] + f'{indent}amazon: "{amazon_id}"\n' + text[idx:], True

    socials = re.search(r"^socials:[ \t]*\n", text, re.MULTILINE)
    if socials:
        idx = socials.end()
        return text[:idx] + f'  amazon: "{amazon_id}"\n' + text[idx:], True

    # Inline socials or unusual layout: reserialise via frontmatter.
    post = frontmatter.loads(text)
    block = post.get("socials")
    if not isinstance(block, dict):
        return text, False
    block["amazon"] = amazon_id
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
        if socials.get("amazon"):
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
        f"Artists to search on Amazon Music (via MusicBrainz): {total} "
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
            amazon_id, status = find_amazon_id(name)
        except utils.CloudflareBlocked as exc:
            print(f"\n{exc}")
            return
        except Exception:
            print(f"{prefix} ! {name}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
            sleep(REQUEST_DELAY)
            continue

        if status == "ok" and DRY_RUN:
            print(f"{prefix} [dry-run] {name} -> {amazon_id}")
            filled += 1
        elif status == "ok":
            new_text, changed = add_amazon(file.read_text(encoding="utf-8"), amazon_id)
            if changed:
                file.write_text(new_text, encoding="utf-8")
                print(f"{prefix} + {name} -> {amazon_id}")
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
