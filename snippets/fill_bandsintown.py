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
* The run covers **every** artist still missing an id (``LIMIT = 0``), throttled
  through ``utils.MIN_REQUEST_INTERVAL`` so Bandsintown does not answer 416.
  Each artist is stamped in ``lastUpdate`` as it is processed, so the run can be
  interrupted and relaunched: it resumes where it stopped, and artists searched
  within the last week are skipped.

Run from the repository root::

    pip install cloudscraper        # recommended (Cloudflare)
    python3 snippets/fill_bandsintown.py
"""

import html
import re
import sys
import traceback
from os import listdir
from pathlib import Path
from time import sleep

import frontmatter
from unidecode import unidecode

# Shared HTTP session + Cloudflare handling.
import utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When True, only report proposed changes; write nothing.
DRY_RUN = False

# Throttle: Bandsintown starts replying 416 when hit too fast. These are applied
# by utils.http_get itself (see main()), so *every* request is spaced out —
# including the retries http_get performs internally — instead of relying on a
# sleep between artists, which left retries unthrottled. Same values as
# bandsintown.py; bump them if 416s still appear.
REQUEST_INTERVAL = 3.0  # minimum seconds between requests
REQUEST_JITTER = 1.5    # extra random 0..JITTER seconds per request

# Cooldown before retrying an artist once after a Cloudflare challenge, so a
# transient block does not abort a multi-hour full run.
COOLDOWN = 120.0

# Process at most this many artists (0 = no limit). Handy for a first test run.
LIMIT = 0

# Front-matter key (under `lastUpdate`) recording when we last *searched*
# Bandsintown for an artist's id. Kept separate from the `bandsintown` key that
# bandsintown.py uses for event fetches, so the two operations never interfere.
# Artists searched within the last week are skipped, to avoid re-searching the
# many artists that have no Bandsintown page.
LOOKUP_PROVIDER = "bandsintown-lookup"

# Bandsintown resolves an artist's vanity slug to its numeric page, e.g.
#   https://www.bandsintown.com/a-perfect-circle  ->  /a/432-a-perfect-circle
# We build that slug from the artist title, follow the redirect, read the numeric
# id, and only trust it when the page's og:title matches the artist.
ARTIST_VANITY_URL = "https://www.bandsintown.com/{slug}"

# Matches the numeric id in a Bandsintown artist URL, e.g. ``/a/432``.
_ARTIST_ID_RE = re.compile(r"/a/(\d+)")


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def normalize(value):
    """Collapse a name/slug to comparable form: ascii, lowercase, alphanumerics
    only (so "Kölsch" == "kolsch", "A*S*Y*S" == "asys", "T & Sugah" == "tsugah",
    "bigflo-oli" == "bigflooli")."""
    return re.sub(r"[^a-z0-9]", "", unidecode(value or "").lower())


# ---------------------------------------------------------------------------
# Resolution (via the artist's Bandsintown vanity URL)
# ---------------------------------------------------------------------------

def _meta_content(html_text, prop):
    """Return the (unescaped) content of a ``<meta property|name="prop">`` tag."""
    for pattern in (
        r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop)
        + r'["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:property|name)=["\']'
        + re.escape(prop) + r'["\']',
    ):
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        if match:
            return html.unescape(match.group(1))
    return None


def _artist_id(text):
    match = _ARTIST_ID_RE.search(text or "")
    return match.group(1) if match else None


def find_bandsintown_id(name):
    """Return ``(id, status)`` for an artist name by resolving its Bandsintown
    vanity URL and confirming the page belongs to the same artist.

    status is one of: ``"ok"`` (id found) or ``"none"`` (no confident match)."""
    target = normalize(name)
    if not target:
        return None, "none"

    response = utils.http_get(ARTIST_VANITY_URL.format(slug=utils.format_filename(name)))
    if not response.ok:
        return None, "none"

    html_text = response.text

    # Confirm the resolved page is actually this artist — guards against
    # fallbacks to the homepage or an unrelated slug.
    og_title = _meta_content(html_text, "og:title")
    if og_title is None or normalize(og_title) != target:
        return None, "none"

    # The numeric id lives in the redirected URL, then the og:url / canonical.
    bit_id = (
        _artist_id(str(response.url))
        or _artist_id(_meta_content(html_text, "og:url") or "")
        or _artist_id(html_text)
    )
    return (bit_id, "ok") if bit_id else (None, "none")


# ---------------------------------------------------------------------------
# Writing the id into the fiche
# ---------------------------------------------------------------------------

def add_bandsintown(text, bit_id):
    """Insert ``bandsintown: "<id>"`` into the fiche's socials block, just before
    the ``youtube`` entry.

    Prefers a minimal textual insertion (block-form socials); falls back to a
    frontmatter round-trip for the rare inline ``socials: { ... }`` form."""
    if re.search(r"^\s*bandsintown:", text, re.MULTILINE):
        return text, False  # already present

    # Preferred: put it right before the youtube line, matching its indentation.
    youtube = re.search(r"^([ \t]+)youtube:.*\n", text, re.MULTILINE)
    if youtube:
        idx = youtube.start()
        indent = youtube.group(1)
        return text[:idx] + f'{indent}bandsintown: "{bit_id}"\n' + text[idx:], True

    # Fallback: no youtube line — insert as the first socials child.
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

    # Throttle every Bandsintown request (including http_get's own retries) to
    # avoid its 416 rate-limit responses.
    utils.MIN_REQUEST_INTERVAL = REQUEST_INTERVAL
    utils.REQUEST_JITTER = REQUEST_JITTER

    # Collect the artists missing a bandsintown id up front, so we can report a
    # total and show per-artist progress.
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
        if socials.get("bandsintown"):
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
    # A full run is long: one throttled request per artist, plus jitter.
    eta = total * (REQUEST_INTERVAL + REQUEST_JITTER / 2) / 3600
    print(
        f"Artists to search on Bandsintown: {total} "
        f"(already have id: {skipped_have}, "
        f"searched within the last week: {skipped_fresh}). "
        f"Mode: {'DRY-RUN' if DRY_RUN else 'WRITE'}. "
        f"Throttle: {REQUEST_INTERVAL}s +0-{REQUEST_JITTER}s jitter "
        f"(~{eta:.1f}h). Interrupting is safe: progress is recorded per artist."
    )
    if total == 0:
        print("Nothing to do.")
        return

    filled = no_match = ambiguous = errors = 0
    for index, (file, name) in enumerate(candidates, start=1):
        prefix = f"[{index}/{total}]"
        try:
            try:
                bit_id, status = find_bandsintown_id(name)
            except utils.CloudflareBlocked as exc:
                # Transient block: cool down and give this artist one more go
                # rather than losing the rest of a multi-hour run.
                print(f"{prefix} ~ {exc}\n{prefix} ~ cooling down {COOLDOWN:.0f}s...")
                sleep(COOLDOWN)
                bit_id, status = find_bandsintown_id(name)
        except utils.CloudflareBlocked as exc:
            print(f"\n{exc}")
            print("Still blocked after the cooldown — stopping here.")
            break
        except Exception:
            print(f"{prefix} ! {name}: {traceback.format_exc().splitlines()[-1]}")
            errors += 1
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
            if not DRY_RUN:
                utils.set_last_update(file, LOOKUP_PROVIDER)
        else:
            print(f"{prefix} - {name}: no match")
            no_match += 1
            if not DRY_RUN:
                utils.set_last_update(file, LOOKUP_PROVIDER)

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
