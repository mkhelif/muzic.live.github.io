  #!/usr/bin/env python3
"""Strip every ``lastUpdate`` block from the content front matter.

The import scripts record, per artist and per provider, the day they last
looked that artist up (``lastUpdate: {bandsintown: 2026-08-05, ...}``) and skip
anyone refreshed recently. Deleting those stamps forces the next run to
reconsider every artist from scratch.

This is a **reset**, not a removal of the mechanism: the scripts keep writing
new stamps as they go. Which also means it is pointless to run this while an
import is in progress — the run will simply re-stamp the artists it walks past
afterwards. Let it finish first (or stop it), then purge.

Only the ``lastUpdate`` key and its indented children are removed; every other
byte of the file is left untouched, and a fiche is skipped if the edit would
change its id, title or body.

Run from the repository root::

    python3 snippets/purge_last_update.py --dry-run
    python3 snippets/purge_last_update.py
    python3 snippets/purge_last_update.py --providers bandsintown,apple
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import frontmatter

# The whole block: the key plus every indented line under it.
BLOCK = re.compile(r"^lastUpdate:[ \t]*\n(?:[ \t]+\S[^\n]*\n)*", re.MULTILINE)


def drop_providers(text, providers):
    """Remove only the given provider lines, and the block if it ends up empty."""
    match = BLOCK.search(text)
    if not match:
        return text
    lines = match.group(0).splitlines(keepends=True)
    kept = [
        line for line in lines[1:]
        if line.split(":")[0].strip() not in providers
    ]
    replacement = "" if not kept else lines[0] + "".join(kept)
    return text[:match.start()] + replacement + text[match.end():]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=Path("content"),
                        help="Content directory to walk. Default: content")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change; write nothing.")
    parser.add_argument("--providers", default="",
                        help="Comma-separated provider keys to drop "
                             "(e.g. bandsintown,apple). Default: the whole block.")
    args = parser.parse_args()

    providers = {p.strip() for p in args.providers.split(",") if p.strip()}
    seen = Counter()
    purged = skipped = 0

    for path in sorted(args.root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "lastUpdate:" not in text:
            continue

        block = BLOCK.search(text)
        if block:
            for line in block.group(0).splitlines()[1:]:
                seen[line.split(":")[0].strip()] += 1

        new = drop_providers(text, providers) if providers else BLOCK.sub("", text, count=1)
        if new == text:
            continue

        # Never let a "cleanup" damage a fiche: id, title and body must survive.
        try:
            before, after = frontmatter.loads(text), frontmatter.loads(new)
        except Exception:
            print(f"! {path}: would no longer parse — skipped")
            skipped += 1
            continue
        if (after.get("id"), after.get("title"), after.content) != \
           (before.get("id"), before.get("title"), before.content):
            print(f"! {path}: unexpected collateral change — skipped")
            skipped += 1
            continue

        if not args.dry_run:
            path.write_text(new, encoding="utf-8")
        purged += 1

    scope = f"providers {sorted(providers)}" if providers else "the whole block"
    print(f"{'Would purge' if args.dry_run else 'Purged'} {scope} from {purged} fiches"
          f"{f', {skipped} skipped' if skipped else ''}.")
    if seen:
        print("stamps found: " + ", ".join(f"{k}={v}" for k, v in sorted(seen.items())))
    if args.dry_run and purged:
        print("Dry run — drop --dry-run to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
