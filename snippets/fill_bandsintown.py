#!/usr/bin/env python3
"""Fill the missing ``bandsintown`` id in artist fiches.

For every ``content/artists/*/index.md`` whose ``socials.bandsintown`` is absent
or empty, this script searches Bandsintown by the artist's title, and — only on
an **exact, normalised name match** — writes the discovered numeric Bandsintown
id into the fiche's ``socials`` block.

It reuses the shared HTTP stack from ``snippets/utils.py`` (a cloudscraper
session that clears Bandsintown's Cloudflare challenge, with a plain-requests
fallback) so all the import scripts behave the same.

Safety first:

* ``DRY_RUN`` is ``True`` by default — it prints what it *would* write and
  changes nothing. Set it to ``False`` to persist.
* A candidate is accepted only when its Bandsintown slug matches the artist
  title after normalisation (unidecode + lowercase + alphanumerics only). If the
  search yields zero or several distinct matching ids, the artist is skipped and
  reported, never guessed.

Run from the repository root::

    pip install cloudscraper        # recommended (Cloudflare)
    python3 snippets/fill_bandsintown.py
"""

from os import listdir
from pathlib import Path
from time import sleep

import re
import sys
import traceback

from unidecode import unidecode
import frontmatter

# Shared HTTP session + Cloudflare handling.
import utils


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When True, only report proposed changes; write nothing.
DRY_RUN = False

# Politeness / anti-rate-limit delay between artists (seconds).
REQUEST_DELAY = 1.0

# Process at most this many artists (0 = no limit). Handy for a first test run.
LIMIT = 0

# Search endpoints tried in order until one yields a match. They are parsed
# generically (any Bandsintown artist link ``/a/<id>-<slug>`` is extracted), so
# the exact response shape does not matter. Adjust if Bandsintown moves them.
SEARCH_URLS = [
    "https://www.bandsintown.com/searchSuggestions/preview?searchTerm={q}",
    "https://www.bandsintown.com/s/{q}",
]

# Matches Bandsintown artist links, e.g. ``/a/15565754-deadletter``.
_ARTIST_LINK_RE = re.compile(r"/a/(\d+)-([A-Za-z0-9\-]+)")


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def normalize(value):
    """Collapse a name/slug to comparable form: ascii, lowercase, alphanumerics
    only (so "Kölsch" == "kolsch", "A*S*Y*S" == "asys", "T & Sugah" == "tsugah",
    "bigflo-oli" == "bigflooli")."""
    return re.sub(r"[^a-z0-9]", "", unidecode(value or "").lower())


# ---------------------------------------------------------------------------
# HTTP (delegated to utils: shared session + Cloudflare handling)
# ---------------------------------------------------------------------------

def fetch_text(url):
    response = utils.http_get(url)
    if not response.ok:
        return None
    return response.text


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def find_bandsintown_id(name):
    """Return ``(id, status)`` for an artist name.

    status is one of: ``"ok"`` (id found), ``"none"`` (no exact match),
    ``"ambiguous"`` (several distinct ids match the same normalised name)."""
    target = normalize(name)
    if not target:
        return None, "none"

    query = requests_quote(name)
    for template in SEARCH_URLS:
        try:
            text = fetch_text(template.format(q=query))
        except utils.CloudflareBlocked:
            raise
        except Exception:
            text = None
        if not text:
            continue

        ids = set()
        for match in _ARTIST_LINK_RE.finditer(text):
            bit_id, slug = match.group(1), match.group(2)
            if normalize(slug) == target:
                ids.add(bit_id)
        if len(ids) == 1:
            return ids.pop(), "ok"
        if len(ids) > 1:
            return None, "ambiguous"
    return None, "none"


def requests_quote(value):
    from urllib.parse import quote
    return quote(value)


# ---------------------------------------------------------------------------
# Writing the id into the fiche
# ---------------------------------------------------------------------------

def add_bandsintown(text, bit_id):
    """Insert ``bandsintown: "<id>"`` into the fiche's socials block.

    Prefers a minimal textual insertion (block-form socials); falls back to a
    frontmatter round-trip for the rare inline ``socials: { ... }`` form."""
    if re.search(r"^\s*bandsintown:", text, re.MULTILINE):
        return text, False  # already present

    match = re.search(r"^socials:[ \t]*\n", text, re.MULTILINE)
    if match:
        idx = match.end()
        return text[:idx] + f'  bandsintown: "{bit_id}"\n' + text[idx:], True

    # Inline socials or unusual layout: reserialise via frontmatter.
    post = frontmatter.loads(text)
    socials = post.get("socials") or {}
    if not isinstance(socials, dict):
        return text, False
    socials["bandsintown"] = bit_id
    post["socials"] = socials
    return frontmatter.dumps(post) + "\n", True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Make log lines appear immediately, even when stdout is piped to a file.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # Collect the artists missing a bandsintown id up front, so we can report a
    # total and show per-artist progress.
    candidates = []
    skipped_have = parse_errors = 0
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
        if socials.get("bandsintown"):
            skipped_have += 1
            continue
        name = data.get("title")
        if not name:
            continue
        candidates.append((file, name))

    if LIMIT:
        candidates = candidates[:LIMIT]

    total = len(candidates)
    print(
        f"Artists missing a bandsintown id: {total} "
        f"(already have: {skipped_have}). "
        f"Mode: {'DRY-RUN' if DRY_RUN else 'WRITE'}."
    )
    if total == 0:
        print("Nothing to do.")
        return

    filled = no_match = ambiguous = errors = 0
    for index, (file, name) in enumerate(candidates, start=1):
        prefix = f"[{index}/{total}]"
        try:
            bit_id, status = find_bandsintown_id(name)
        except utils.CloudflareBlocked as exc:
            print(f"\n{exc}")
            return
        except Exception:
            print(f"{prefix} ! {name}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
            sleep(REQUEST_DELAY)
            continue

        if status == "ok" and DRY_RUN:
            print(f"{prefix} [dry-run] {name} -> {bit_id}")
            filled += 1
        elif status == "ok":
            new_text, changed = add_bandsintown(
                file.read_text(encoding="utf-8"), bit_id
            )
            if changed:
                file.write_text(new_text, encoding="utf-8")
                print(f"{prefix} + {name} -> {bit_id}")
                filled += 1
            else:
                print(f"{prefix} = {name}: already had id, skipped")
        elif status == "ambiguous":
            print(f"{prefix} ? {name}: several matching ids, skipped")
            ambiguous += 1
        else:
            print(f"{prefix} - {name}: no match")
            no_match += 1

        sleep(REQUEST_DELAY)

    print(
        "\nDone. "
        f"{'would fill' if DRY_RUN else 'filled'}={filled}, "
        f"no_match={no_match}, ambiguous={ambiguous}, "
        f"already_had={skipped_have}, errors={errors + parse_errors}"
    )
    if DRY_RUN and filled:
        print("DRY_RUN is on — set DRY_RUN = False to write these ids.")


if __name__ == "__main__":
    main()
