#!/usr/bin/env python3
"""Fill everything MusicBrainz knows, from a local dump, in a single pass.

The offline twin of ``fill_musicbrainz.py`` + ``musicbrainz.py``. Those two
are bound by MusicBrainz's ~1 request/second anonymous limit: ~28k artists to
resolve plus a lookup each is the better part of a day. This does the same
work as a local join, in the time it takes to decompress the dump.

What it fills, per artist:

- ``socials.musicbrainz`` — resolved by name for fiches that have no id yet,
  exactly like fill_musicbrainz.py does online;
- ``socials.spotify`` / ``deezer`` / ``apple`` / ``tidal`` / ``qobuz`` /
  ``amazon`` / ``songkick`` — from the record's url relations;
- ``members:`` — from "member of band" relations (``type: band``), creating
  minimal ``type: person`` stubs for members we don't have;
- ``lifespan.start`` / ``end`` — from ``life-span`` (``type: person``);
- minimal ``type: band`` stubs for the bands a person belongs to.

It reuses musicbrainz.py's extractors verbatim rather than reimplementing
them: the dump's ``relations`` array has the same shape as the API's, so
``extract_socials`` and ``extract_members`` apply unchanged. One behaviour,
one place to fix.

Why a single pass
-----------------

The dump is ~17 GB uncompressed and decompression dominates the runtime, so
reading it twice would double the cost for nothing. Instead we note upfront
which MBIDs and which names we are after, then apply each matching record to
its fiche as it goes by. Memory stays flat regardless of the dump's size.

Usage::

    python3 snippets/musicbrainz_dump.py --dump ~/Downloads/artist.tar.xz
    python3 snippets/musicbrainz_dump.py --dump ~/Downloads/artist.tar.xz --write
    python3 snippets/musicbrainz_dump.py --dump ... --write --only socials,members
"""

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from os import listdir
from pathlib import Path

import frontmatter

import musicbrainz as mb
import utils
from import_musicbrainz_dump import dump_lines, resolve

DEFAULT_DUMP = Path.home() / "Downloads" / "artist.tar.xz"
FIELDS = ("id", "socials", "members", "lifespan")


# ---------------------------------------------------------------------------
# What we are after
# ---------------------------------------------------------------------------

class Target:
    """A local fiche and what it is still missing."""

    __slots__ = ("file", "title", "type", "mbid", "needs_social",
                 "needs_members", "needs_lifespan")

    def __init__(self, file, data, text):
        self.file = file
        self.title = data.get("title")
        self.type = data.get("type")
        socials = data.get("socials")
        socials = socials if isinstance(socials, dict) else {}
        self.mbid = socials.get("musicbrainz") or None
        self.needs_social = {p for p in mb.SOCIAL_PATTERNS if not socials.get(p)}
        self.needs_members = self.type == "band" and not mb.has_members_block(text)
        self.needs_lifespan = self.type == "person" and not mb.has_lifespan_block(text)

    @property
    def wants_anything(self):
        return bool(self.needs_social or self.needs_members
                    or self.needs_lifespan or not self.mbid)


def collect_targets():
    """Return ``(by_mbid, by_name, targets)``."""
    by_mbid, by_name, targets = {}, defaultdict(list), []

    for slug in sorted(listdir("./content/artists")):
        file = Path(f"./content/artists/{slug}/index.md")
        if not file.exists():
            continue
        try:
            text = file.read_text(encoding="utf-8")
            data = frontmatter.loads(text)
        except Exception:
            continue
        if not data.get("title"):
            continue

        target = Target(file, data, text)
        if not target.wants_anything:
            continue
        targets.append(target)

        if target.mbid:
            by_mbid[str(target.mbid)] = target
        else:
            # Aliases give a second chance at a match, as they do online.
            names = [target.title] + [a for a in (data.get("aliases") or []) if a]
            for name in names:
                key = mb.normalize(name)
                if key:
                    by_name[key].append(target)

    return by_mbid, by_name, targets


# ---------------------------------------------------------------------------
# Applying one record
# ---------------------------------------------------------------------------

def apply(target, artist, fields, write):
    """Apply a dump record to a fiche. Returns a list of change descriptions."""
    changes = []
    relations = artist.get("relations") or []
    text = target.file.read_text(encoding="utf-8")

    if "id" in fields and not target.mbid:
        new, changed = mb.add_social(text, "musicbrainz", artist["id"])
        if changed:
            text = new
            target.mbid = artist["id"]
            changes.append(f"musicbrainz={artist['id']}")

    if "socials" in fields and target.needs_social:
        found = mb.extract_socials(relations)
        for provider in sorted(target.needs_social):
            value, status = found.get(provider, (None, "none"))
            if status != "ok":
                continue
            new, changed = mb.add_social(text, provider, value)
            if changed:
                text = new
                changes.append(f"{provider}={value}")

    if "members" in fields and target.needs_members:
        members = mb.extract_members(relations)
        if members:
            ids, created = {}, 0
            for member in members:
                person_id, was_new = mb.get_or_create_person(member["name"])
                ids[member["name"]] = person_id
                created += was_new
            new = mb.insert_members(text, mb.render_members(members, ids))
            if new:
                text = new
                suffix = f", created {created} fiches" if created else ""
                changes.append(f"members={len(members)}{suffix}")

    if "lifespan" in fields and target.needs_lifespan:
        span = artist.get("life-span") or {}
        begin = (span.get("begin") or "").strip()
        finish = (span.get("end") or "").strip()
        start = begin if mb._DATE_RE.match(begin) else None
        end = finish if mb._DATE_RE.match(finish) else None
        if start or end:
            new = mb.insert_lifespan(text, start, end)
            if new:
                text = new
                changes.append(f"lifespan={start or '-'}/{end or '-'}")

    # A person's forward "member of band" relations tell us about bands we may
    # not have yet — the mirror of what members does for a band.
    if "members" in fields and target.type == "person":
        made = [name for name in mb.extract_band_memberships(relations)
                if mb.get_or_create_band(name)[1]]
        if made:
            changes.append(f"created_bands={len(made)}")

    if changes and write:
        target.file.write_text(text, encoding="utf-8")
        utils.set_last_update(target.file, "musicbrainz")
    return changes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dump", default=DEFAULT_DUMP, help=f"Default: {DEFAULT_DUMP}")
    parser.add_argument("--write", action="store_true",
                        help="Actually write. Without it, this is a dry run.")
    parser.add_argument("--only", default=",".join(FIELDS),
                        help=f"Comma-separated subset of {','.join(FIELDS)}.")
    parser.add_argument("--report", type=Path, default=None,
                        help="Write ambiguous names to this TSV for review.")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    fields = {f.strip() for f in args.only.split(",") if f.strip()}
    unknown = fields - set(FIELDS)
    if unknown:
        raise SystemExit(f"Unknown field(s): {', '.join(sorted(unknown))}")

    by_mbid, by_name, targets = collect_targets()
    print(f"{len(targets):,} fiches need something "
          f"({len(by_mbid):,} already have an mbid, "
          f"{len(by_name):,} names to resolve). Filling: {', '.join(sorted(fields))}. "
          f"Mode: {'WRITE' if args.write else 'DRY-RUN'}.\n")
    if not targets:
        print("Nothing to do.")
        return 0

    # Name matches are gathered first, then resolved: a name is only usable
    # once we know it maps to exactly one MusicBrainz artist.
    pending = defaultdict(list)
    stats = Counter()
    started = time.monotonic()
    scanned = 0

    for raw in dump_lines(args.dump):
        scanned += 1
        if scanned % 250_000 == 0:
            print(f"  {scanned:,} scanned, {stats['applied']:,} fiches updated "
                  f"({time.monotonic() - started:.0f}s)")
        try:
            artist = json.loads(raw)
        except Exception:
            continue
        mbid = artist.get("id")
        if not mbid:
            continue

        target = by_mbid.get(mbid)
        if target is not None:
            changes = apply(target, artist, fields, args.write)
            if changes:
                stats["applied"] += 1
                print(f"  {'+' if args.write else '~'} {target.title}: {', '.join(changes)}")
            continue

        if not by_name:
            continue
        keys = {mb.normalize(artist.get("name") or ""),
                mb.normalize(artist.get("sort-name") or "")}
        for alias in artist.get("aliases") or []:
            keys.add(mb.normalize(alias.get("name") or ""))
        for key in keys:
            if key and key in by_name:
                pending[key].append(artist)
                break

    print(f"  {scanned:,} scanned in {time.monotonic() - started:.0f}s\n")

    # Now resolve the name matches, and apply the unambiguous ones.
    rows = []
    for key, records in pending.items():
        candidates = [(r["id"], r.get("type") or "",
                       r.get("disambiguation") or "", r.get("name") or "")
                      for r in records]
        for target in by_name[key]:
            if target.mbid:
                continue
            mbid, status = resolve(candidates, target.type)
            if mbid is None:
                stats["ambiguous"] += 1
                rows.append((target.title, target.type or "", len(candidates),
                             " | ".join(f"{c[0]} {c[1]} ({c[2]})" for c in candidates[:6])))
                continue
            record = next(r for r in records if r["id"] == mbid)
            changes = apply(target, record, fields, args.write)
            if changes:
                stats["applied"] += 1
                stats["by-type"] += status == "by-type"
                print(f"  {'+' if args.write else '~'} {target.title}: {', '.join(changes)}")

    if args.report and rows:
        with args.report.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["title", "type", "candidates", "detail"])
            writer.writerows(rows)
        print(f"\nAmbiguous names written to {args.report}")

    print(f"\n{'Updated' if args.write else 'Would update'} {stats['applied']:,} fiches "
          f"({stats['by-type']:,} matched by type), ambiguous={stats['ambiguous']:,}, "
          f"untouched={len(targets) - stats['applied'] - stats['ambiguous']:,}")
    if not args.write and stats["applied"]:
        print("Dry run — add --write to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
