#!/usr/bin/env python3
"""
Mark artist / album / festival names in content bodies as **bold**.

`layouts/_partials/content.html` turns every `**Nom**` into a link to the
matching fiche. Bold is the explicit, opt-in selector: the template used to
match bare names anywhere in the text, which was both very slow (~2h of the
build) and prone to false positives on common words.

This script walks the content sections and wraps known names in `**...**`.

Safety rules (it edits prose, so it is deliberately conservative):
- only names of the sections the template can link (artists, albums, festivals);
- case-sensitive exact match by default, so "Pat" never matches "pat";
- names shorter than --min-length are skipped (too risky in running prose);
- a page never marks its own title (that would self-link);
- longest names first, so "Bigflo & Oli" wins over "Bigflo";
- never touches front matter, headings, code, existing bold/italic, Markdown
  links, HTML tags or shortcodes;
- a name split across a hard-wrapped line break is rejoined onto one line,
  because Goldmark would otherwise emit `<strong>Nom\\nsuite</strong>` and the
  template only matches single-line `<strong>Nom</strong>`.

Examples:
  python3 snippets/mark_names.py --dry-run
  python3 snippets/mark_names.py --dry-run --sections festivals --limit 20
  python3 snippets/mark_names.py            # writes
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content"

# Sections whose bodies get marked.
DEFAULT_SECTIONS = ["artists", "albums", "festivals", "venues"]

# Sections whose names the template can link (mirrors index/linkable.html).
LINKABLE_SECTIONS = ["artists", "albums", "festivals"]

# Names below this length are too risky to auto-mark inside prose.
DEFAULT_MIN_LENGTH = 5

# Never mark these, even if long enough. They are real artist names, but they
# are also ordinary words / places / first names, so matching them in running
# prose is wrong far more often than it is right ("l'un des plus grands
# festivals d'Europe", "Claude Nobs", "un bel héritage", ...).
# Run with --report to find new offenders, then add them here.
STOPLIST = {
    # common words
    "Album", "Concert", "Festival", "Live", "Music", "Musique", "Groupe",
    "Nouvelle", "Premier", "Single", "Tour", "Various Artists",
    "Ensemble", "Héritage", "Nothing", "Heritage", "Portrait", "Passion",
    # places
    "Europe", "France", "Paris", "Boston", "Texas", "Jersey", "Amsterdam",
    "Chicago", "America", "Berlin", "Asia", "Kansas", "Nazareth", "Santa",
    # first names
    "James", "Vincent", "Camille", "Arthur", "Claude", "Milla", "Louise",
    "Marguerite", "Suzanne", "Julien", "Antoine",
}

# Word characters, including accented letters, used for the boundary checks.
WORD = r"0-9A-Za-zÀ-ɏ"

# Regions that must never be touched.
PROTECTED = re.compile(
    r"```.*?```"                    # fenced code
    r"|`[^`\n]*`"                   # inline code
    r"|\*\*.*?\*\*"                 # existing bold
    r"|\*[^*\n]+\*"                 # existing italic
    r"|\[[^\]]*\]\([^)]*\)"         # markdown link
    r"|!\[[^\]]*\]\([^)]*\)"        # image
    r"|<[^>\n]+>"                   # html tag
    r"|\{\{.*?\}\}"                 # shortcode
    r"|^[ \t]*#{1,6}[ \t].*$"       # heading line
    r"|^[ \t]*>.*$",                # blockquote line
    re.DOTALL | re.MULTILINE,
)


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return (frontmatter_including_delimiters, body) or None."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return f"---{parts[1]}---", parts[2]


def frontmatter_value(frontmatter: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*?)\s*$", frontmatter, re.M)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def frontmatter_aliases(frontmatter: str) -> list[str]:
    match = re.search(r"^aliases:\s*\n((?:[ \t]+-[^\n]*\n)+)", frontmatter, re.M)
    if not match:
        return []
    out = []
    for line in match.group(1).splitlines():
        value = line.strip().lstrip("-").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            out.append(value)
    return out


def collect_names(min_length: int, min_words: int = 1) -> dict[str, str]:
    """Return {name: owning fiche path} for every linkable title and alias."""
    names: dict[str, str] = {}
    for section in LINKABLE_SECTIONS:
        for path in sorted((CONTENT / section).glob("**/index.md")):
            text = path.read_text(encoding="utf-8")
            split = split_frontmatter(text)
            if not split:
                continue
            frontmatter, _ = split
            owner = str(path)
            for name in [frontmatter_value(frontmatter, "title")] + frontmatter_aliases(frontmatter):
                name = (name or "").strip()
                if len(name) < min_length or name in STOPLIST:
                    continue
                if len(name.split()) < min_words:
                    continue
                names.setdefault(name, owner)
    return names


def build_pattern(names: list[str], ignore_case: bool) -> re.Pattern:
    """One alternation, longest first, tolerating hard-wrapped line breaks."""
    alternatives = []
    for name in sorted(names, key=len, reverse=True):
        parts = [re.escape(part) for part in name.split()]
        alternatives.append(r"\s+".join(parts))
    pattern = rf"(?<![{WORD}])(?:{'|'.join(alternatives)})(?![{WORD}])"
    return re.compile(pattern, re.IGNORECASE if ignore_case else 0)


def free_spans(body: str) -> list[tuple[int, int]]:
    """Spans of the body that are safe to edit (outside protected regions)."""
    spans, cursor = [], 0
    for match in PROTECTED.finditer(body):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(body):
        spans.append((cursor, len(body)))
    return spans


def mark_body(body: str, pattern: re.Pattern, own_title: str,
              tally=None) -> tuple[str, int]:
    """Wrap known names in **...**; returns (new_body, count)."""
    out, count, cursor = [], 0, 0

    for start, end in free_spans(body):
        out.append(body[cursor:start])
        segment = body[start:end]

        def replace(match: re.Match) -> str:
            nonlocal count
            matched = match.group(0)
            name = " ".join(matched.split())
            # A page must not link to itself.
            if name == own_title:
                return matched
            count += 1
            if tally is not None:
                tally[name] += 1
            # Rejoin names split by the hard wrap: the template only matches
            # single-line <strong>Nom</strong>.
            return f"**{name}**"

        out.append(pattern.sub(replace, segment))
        cursor = end

    out.append(body[cursor:])
    return "".join(out), count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wrap artist/album/festival names in **bold** so content.html can link them."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    parser.add_argument("--sections", default=",".join(DEFAULT_SECTIONS),
                        help=f"Comma-separated sections to process. Default: {','.join(DEFAULT_SECTIONS)}")
    parser.add_argument("--min-length", type=int, default=DEFAULT_MIN_LENGTH,
                        help=f"Skip names shorter than this. Default: {DEFAULT_MIN_LENGTH}")
    parser.add_argument("--ignore-case", action="store_true",
                        help="Match names case-insensitively (riskier).")
    parser.add_argument("--min-words", type=int, default=1,
                        help="Skip names with fewer words than this. 2 = only multi-word "
                             "names, the safest setting. Default: 1")
    parser.add_argument("--report", action="store_true",
                        help="List the most frequently matched names (candidates for STOPLIST) "
                             "instead of reporting per file.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N files.")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    names = collect_names(args.min_length, args.min_words)
    if not names:
        print("No names collected; aborting.", file=sys.stderr)
        return 1
    print(f"linkable names: {len(names)} "
          f"(min length {args.min_length}, min words {args.min_words})")
    pattern = build_pattern(list(names), args.ignore_case)

    files = []
    for section in [s.strip() for s in args.sections.split(",") if s.strip()]:
        files.extend(sorted((CONTENT / section).glob("**/*.md")))
    if args.limit:
        files = files[: args.limit]
    print(f"files to scan: {len(files)}")
    print(f"dry_run={args.dry_run}")

    from collections import Counter
    matched_names: Counter[str] = Counter()

    changed_files = marks = skipped_empty = 0
    for index, path in enumerate(files, start=1):
        text = path.read_text(encoding="utf-8")
        split = split_frontmatter(text)
        if not split:
            continue
        frontmatter, body = split
        if not body.strip():
            skipped_empty += 1
            continue

        own_title = frontmatter_value(frontmatter, "title")
        new_body, count = mark_body(body, pattern, own_title, matched_names)
        if count and new_body != body:
            changed_files += 1
            marks += count
            if not args.dry_run:
                path.write_text(frontmatter + new_body, encoding="utf-8")
            if not args.report:
                print(f"+ {path.relative_to(ROOT)} ({count} marks)")

        if index % 2000 == 0:
            print(f"  progress {index}/{len(files)}", flush=True)

    if args.report:
        print("\nMost frequently matched names "
              "(review these; add false positives to STOPLIST):")
        for name, hits in matched_names.most_common(40):
            print(f"  {hits:6}  {name}")

    print("\ndone")
    print(f"files changed={changed_files} marks={marks} empty_bodies_skipped={skipped_empty}")
    if args.dry_run and changed_files:
        print("dry-run: nothing written. Re-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
