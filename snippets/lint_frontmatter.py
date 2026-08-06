#!/usr/bin/env python3
"""Find (and optionally repair) duplicate front-matter keys.

A duplicated top-level key is invalid YAML: Hugo fails the page with
"mapping key already defined", and ``frontmatter``/PyYAML silently keeps only
the last one — so the damage is easy to miss until a build breaks.

It happens when two import runs overlap. The scripts decide what a fiche is
missing once, up front (``needs_dates``, ``needs_members``), but a full run
takes hours, and ``musicbrainz.py`` and ``fill_musicbrainz.py`` both write
through the same code. Two runs in flight each hold a stale "no lifespan yet"
and each insert a block. The writers are now idempotent — they re-check the
current text before inserting — but this catches anything already on disk.

``--fix`` keeps the **first** occurrence of a duplicated key and drops the
later ones, which is the conservative choice: the first block is the one that
was there before the race.

Run from the repository root::

    python3 snippets/lint_frontmatter.py
    python3 snippets/lint_frontmatter.py --fix
    python3 snippets/lint_frontmatter.py --key lifespan --fix
"""

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

# A top-level front-matter key: starts at column 0, before the closing "---".
KEY = re.compile(r"^([A-Za-z][\w-]*):", re.MULTILINE)


def split_frontmatter(text):
    """Return ``(frontmatter, rest)`` or ``None`` when there is no front matter."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    return text[4:end + 1], text[end + 1:]


def duplicate_keys(front):
    counts = Counter(m.group(1) for m in KEY.finditer(front))
    return {k: n for k, n in counts.items() if n > 1}


def drop_later_blocks(front, key):
    """Keep the first ``key:`` block, remove the following ones.

    A block is the key line plus every indented line under it, up to the next
    top-level key."""
    block = re.compile(rf"^{re.escape(key)}:[ \t]*[^\n]*\n(?:[ \t]+[^\n]*\n)*",
                       re.MULTILINE)
    seen = {"first": True}

    def replace(match):
        if seen["first"]:
            seen["first"] = False
            return match.group(0)
        return ""

    return block.sub(replace, front)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=Path("content"))
    parser.add_argument("--fix", action="store_true",
                        help="Keep the first occurrence, drop the later ones.")
    parser.add_argument("--key", default=None,
                        help="Only consider this key (e.g. lifespan).")
    args = parser.parse_args()

    offenders = defaultdict(list)
    fixed = 0

    for path in sorted(args.root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parts = split_frontmatter(text)
        if parts is None:
            continue
        front, rest = parts

        dupes = duplicate_keys(front)
        if args.key:
            dupes = {k: n for k, n in dupes.items() if k == args.key}
        if not dupes:
            continue

        for key, count in dupes.items():
            offenders[key].append((path, count))

        if args.fix:
            for key in dupes:
                front = drop_later_blocks(front, key)
            if duplicate_keys(front):
                print(f"! {path}: still duplicated after fix — left alone")
                continue
            path.write_text("---\n" + front + rest, encoding="utf-8")
            fixed += 1

    if not offenders:
        print("No duplicate front-matter keys.")
        return 0

    total = sum(len(v) for v in offenders.values())
    print(f"{total} fiche(s) with a duplicated key:\n")
    for key, files in sorted(offenders.items(), key=lambda kv: -len(kv[1])):
        print(f"  {key}: {len(files)} fiche(s)")
        for path, count in files[:5]:
            print(f"    x{count}  {path}")
        if len(files) > 5:
            print(f"    ... and {len(files) - 5} more")

    if args.fix:
        print(f"\nRepaired {fixed} fiche(s) (kept the first occurrence).")
    else:
        print("\nRun with --fix to keep the first occurrence of each.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
