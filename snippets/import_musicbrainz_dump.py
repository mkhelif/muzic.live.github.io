#!/usr/bin/env python3
"""Fill ``socials.musicbrainz`` from a local MusicBrainz JSON dump.

The online path (``fill_musicbrainz.py``) is bound by MusicBrainz's ~1
request/second anonymous rate limit: ~25k artists still to resolve means ~8
hours of searching. A dump turns the same job into a local join.

It also matches *better* than the API, because it sees every candidate at
once: when several MusicBrainz artists share a name, this narrows by type
(our ``type: band`` -> Group, ``type: person`` -> Person) before giving up,
where the API search would just return an ambiguous list.

Safety is unchanged: an id is written only on an exact, unambiguous,
normalised name match. Ambiguity is reported, never guessed.

The dump
--------

``artist.tar.xz`` from https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/
holds ``mbdump/artist``: **JSON Lines**, one artist object per line, ~17 GB
uncompressed. Only ``id``, ``name``, ``sort-name``, ``type``,
``disambiguation`` and ``aliases`` are used here.

Rather than indexing 2.5M artists in RAM, this collects the names we actually
need first (~25k) and keeps only the lines that match, so memory stays flat
whatever the dump's size. Expect roughly 10-20 minutes, dominated by xz
decompression.

Usage::

    python3 snippets/import_musicbrainz_dump.py --dump ~/Downloads/artist.tar.xz
    python3 snippets/import_musicbrainz_dump.py --dump ... --write
    python3 snippets/import_musicbrainz_dump.py --dump ... --report ambiguous.tsv
"""

import argparse
import csv
import json
import lzma
import subprocess
import sys
import tarfile
import time
from collections import defaultdict
from os import listdir
from pathlib import Path

import frontmatter

import musicbrainz as mb
import utils

DEFAULT_DUMP = Path.home() / "Downloads" / "artist.tar.xz"
MEMBER = "mbdump/artist"


# ---------------------------------------------------------------------------
# What we are looking for
# ---------------------------------------------------------------------------

def wanted_artists():
    """Return ``({normalised name: [slug]}, {slug: (file, title, type)})`` for
    the fiches that still have no MusicBrainz id."""
    names = defaultdict(list)
    fiches = {}
    have = 0

    for slug in sorted(listdir("./content/artists")):
        file = Path(f"./content/artists/{slug}/index.md")
        if not file.exists():
            continue
        try:
            data = frontmatter.loads(file.read_text(encoding="utf-8"))
        except Exception:
            continue

        title = data.get("title")
        socials = data.get("socials")
        socials = socials if isinstance(socials, dict) else {}
        if not title:
            continue
        if socials.get("musicbrainz"):
            have += 1
            continue

        fiches[slug] = (file, title, data.get("type"))
        # Aliases give a second chance at a match, as they do online.
        for name in [title] + [a for a in (data.get("aliases") or []) if a]:
            key = mb.normalize(name)
            if key and slug not in names[key]:
                names[key].append(slug)

    return names, fiches, have


# ---------------------------------------------------------------------------
# Streaming the dump
# ---------------------------------------------------------------------------

def dump_lines(path):
    """Yield the lines of mbdump/artist.

    Shelling out to `tar` is deliberate: its xz decompression is threaded and
    markedly faster than Python's lzma on a 17 GB member. Falls back to the
    pure-Python path when tar is unavailable."""
    path = Path(path).expanduser()
    if not path.exists():
        raise SystemExit(f"Dump not found: {path}")

    try:
        process = subprocess.Popen(
            ["tar", "-xOf", str(path), MEMBER],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for line in process.stdout:
            yield line
        process.wait()
        if process.returncode == 0:
            return
        print("tar failed, falling back to Python", file=sys.stderr)
    except FileNotFoundError:
        pass

    with tarfile.open(path, "r:*") as archive:
        handle = archive.extractfile(MEMBER)
        if handle is None:
            raise SystemExit(f"{MEMBER} not found inside {path}")
        for line in handle:
            yield line


def collect(path, names):
    """Stream the dump, keeping only artists whose name we are looking for.

    Returns ``{normalised name: [(mbid, type, disambiguation, name)]}``."""
    found = defaultdict(list)
    seen = scanned = 0
    started = time.monotonic()

    for raw in dump_lines(path):
        scanned += 1
        if scanned % 250_000 == 0:
            print(f"  {scanned:,} artists scanned, {seen:,} matches "
                  f"({time.monotonic() - started:.0f}s)", flush=True)
        try:
            artist = json.loads(raw)
        except Exception:
            continue

        mbid = artist.get("id")
        if not mbid:
            continue

        # A MusicBrainz artist is reachable by its name, its sort-name and any
        # of its aliases — same surfaces the API search matches on.
        candidates = {artist.get("name"), artist.get("sort-name")}
        for alias in artist.get("aliases") or []:
            candidates.add(alias.get("name"))
            candidates.add(alias.get("sort-name"))

        entry = None
        for candidate in candidates:
            if not candidate:
                continue
            key = mb.normalize(candidate)
            if key not in names:
                continue
            if entry is None:
                entry = (mbid, artist.get("type") or "",
                         artist.get("disambiguation") or "", artist.get("name") or "")
                seen += 1
            if entry not in found[key]:
                found[key].append(entry)

    print(f"  {scanned:,} artists scanned, {seen:,} matches "
          f"in {time.monotonic() - started:.0f}s")
    return found


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(candidates, local_type):
    """Return ``(mbid, status)``; mbid is None when it stays ambiguous.

    Narrowing by type is what the API search cannot do for us: a name shared
    by a band and a person is ambiguous online, but decidable here when the
    fiche says which one it is."""
    unique = list({c[0]: c for c in candidates}.values())
    if len(unique) == 1:
        return unique[0][0], "ok"

    wanted = mb.LOCAL_TYPE_TO_MUSICBRAINZ.get(local_type)
    if wanted:
        narrowed = [c for c in unique if c[1] == wanted]
        if len(narrowed) == 1:
            return narrowed[0][0], "by-type"
    return None, "ambiguous"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dump", default=DEFAULT_DUMP, help=f"Default: {DEFAULT_DUMP}")
    parser.add_argument("--write", action="store_true",
                        help="Actually write the ids. Without it, this is a dry run.")
    parser.add_argument("--report", type=Path, default=None,
                        help="Write the ambiguous names to this TSV for review.")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    names, fiches, have = wanted_artists()
    print(f"Looking for {len(fiches):,} artists ({len(names):,} names with "
          f"aliases). Already have an id: {have:,}.")
    if not fiches:
        print("Nothing to do.")
        return 0

    found = collect(args.dump, names)

    matched = by_type = ambiguous = 0
    rows = []
    for slug, (file, title, local_type) in fiches.items():
        candidates = []
        for key, slugs in ((k, s) for k, s in names.items() if slug in s):
            candidates.extend(found.get(key, []))
        if not candidates:
            continue

        mbid, status = resolve(candidates, local_type)
        if mbid is None:
            ambiguous += 1
            rows.append((title, local_type or "", len(candidates),
                         " | ".join(f"{c[0]} {c[1]} ({c[2]})" for c in candidates[:6])))
            continue

        matched += 1
        by_type += status == "by-type"
        print(f"{'+' if args.write else '[dry-run]'} {title} -> {mbid}"
              f"{' (narrowed by type)' if status == 'by-type' else ''}")
        if args.write:
            text, changed = mb.add_social(
                file.read_text(encoding="utf-8"), "musicbrainz", mbid)
            if changed:
                file.write_text(text, encoding="utf-8")
                utils.set_last_update(file, "musicbrainz-lookup")

    if args.report and rows:
        with args.report.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["title", "type", "candidates", "detail"])
            writer.writerows(rows)
        print(f"\nAmbiguous names written to {args.report}")

    print(f"\n{'Wrote' if args.write else 'Would write'} {matched:,} ids "
          f"({by_type:,} decided by type), ambiguous={ambiguous:,}, "
          f"not in dump={len(fiches) - matched - ambiguous:,}")
    if not args.write and matched:
        print("Dry run — add --write to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
