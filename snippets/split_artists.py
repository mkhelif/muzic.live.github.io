#!/usr/bin/env python3
"""Split "combined" artist fiches into their individual artists.

Some fiches were created from an event billing rather than from a real act, e.g.
``Alpha Steppa x Nai-Jah & Awa Fall`` or ``Busy P b2b Boys Noize``. Those are
line-ups, not bands: each component is its own artist and should have its own
fiche, so that events, news and reports reference the individuals.

This script is **table-driven, never heuristic**. Guessing from the title is
unsafe: ``Nick Cave & The Bad Seeds``, ``Kool & The Gang`` or ``Tones & I`` use
the very same separators and must *not* be split. ``SPLITS`` below is a curated
list; nothing outside it is touched.

For every entry it:

1. resolves each component to an existing artist (by title or alias,
   normalised), or creates a **minimal** fiche for it — a fresh uuid, empty
   socials and a ``todo``; never an invented description or social, per the
   repository conventions;
2. rewrites every reference to the combined uuid (``artists:`` lists in events,
   news and reports) into the components' uuids, keeping list order and
   de-duplicating;
3. archives the combined fiche's description, if it had one, then deletes the
   combined fiche.

``DRY_RUN`` is True by default: it reports what it would do and changes nothing.

Run from the repository root::

    python3 snippets/split_artists.py
"""

import re
import shutil
import sys
import uuid as uuidlib
from pathlib import Path

import frontmatter
from unidecode import unidecode

import utils


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DRY_RUN = False

# Where the descriptions of deleted combined fiches are archived, so nothing
# written by hand is lost silently.
ARCHIVE = Path("./split_artists_archive.md")

# Sections whose front matter may reference an artist uuid.
REFERENCING_SECTIONS = ("events", "news", "reports", "albums", "artists")

# The curated splits: combined fiche slug -> components.
# A component is a name, optionally with an explicit `type` for the fiche that
# gets created when it does not exist yet. Only real, verifiable individuals get
# `person`; anything uncertain is left to `unknown`, per the conventions.
SPLITS = {
    # --- one-off billings: x / b2b / feat. -------------------------------
    "alpha-steppa-x-nai-jah-awa-fall": [
        ("Alpha Steppa", None), ("Nai-Jah", None), ("Awa Fall", None),
    ],
    "busy-p-b2b-boys-noize": [("Busy P", None), ("Boys Noize", None)],
    "dawa-hi-fi-x-roots-raid": [("Dawa HiFi", None), ("Roots Raid", None)],
    "e-darta-b2b-mockoff": [("E Darta", None), ("Mockoff", None)],
    "evil-jared-x-krogi": [("EVIL JARED", None), ("KROGI", None)],
    "fanette-b2b-linoa": [("Fanette", None), ("Linoa", None)],
    "no-art-b2b-rem-s-martinez": [("No Art", None), ("Rem's Martinez", None)],
    "shao-x-cinza": [("Shao", None), ("Cinza", None)],
    "sex-pistols-feat-frank-carter": [
        ("Sex Pistols", None), ("Frank Carter", "person"),
    ],

    # --- two established solo artists billed together --------------------
    "cozik-faya-pyd": [("Cozik", None), ("Faya Pyd", None)],
    "djs-kitsch-loren-et-kouen-2-jambon": [
        ("Kitsch Loren", None), ("Kouen 2 Jambon", None),
    ],
    # The 2022 "Dutronc & Dutronc" tour/album: father and son.
    "dutronc-and-dutronc": [
        ("Jacques Dutronc", "person"), ("Thomas Dutronc", "person"),
    ],
    "hubert-felix-thiefaine-and-paul-personne": [
        ("Hubert-Félix Thiéfaine", None), ("Paul Personne", "person"),
    ],
    "isha-et-limsa-d-aulnay": [("ISHA", None), ("Limsa d’Aulnay", None)],
    "laurent-garnier-and-bugge-wesseltoft": [
        ("Laurent Garnier", None), ("Bugge Wesseltoft", "person"),
    ],
    "scylla-furax-barbarossa": [("Scylla", None), ("Furax Barbarossa", None)],
}

# Socials scaffold for a newly created fiche (same keys/order as the others).
SOCIAL_KEYS = (
    "facebook", "instagram", "tiktok", "x", "youtube", "web", "email",
    "amazon", "apple", "deezer", "qobuz", "spotify", "tidal",
)


# ---------------------------------------------------------------------------
# Artist index
# ---------------------------------------------------------------------------

def normalize(value):
    """ascii, lowercase, alphanumerics only — for name comparison."""
    return re.sub(r"[^a-z0-9]", "", unidecode(value or "").lower())


def load_artists():
    """Return ``(by_name, by_slug)`` over ``content/artists/*/index.md``."""
    by_name, by_slug = {}, {}
    for path in sorted(Path("./content/artists").glob("*/index.md")):
        slug = path.parent.name
        try:
            data = frontmatter.loads(path.read_text(encoding="utf-8"))
        except Exception:
            print(f"! {slug}: cannot parse front matter")
            continue
        by_slug[slug] = data
        for name in [data.get("title")] + list(data.get("aliases") or []):
            if name:
                by_name.setdefault(normalize(name), slug)
    return by_name, by_slug


def create_artist(name, kind):
    """Create a minimal fiche for ``name`` and return ``(slug, uuid)``.

    No description and no socials are invented: the fiche carries a fresh uuid,
    an empty socials block and a todo list, exactly like the other stubs."""
    slug = utils.format_filename(name)
    new_id = str(uuidlib.uuid4())
    lines = [
        "---",
        f'id: "{new_id}"',
        f'title: "{utils.yaml_quote(name)}"',
    ]
    if kind:
        lines.append(f"type: {kind}")
    lines.append("socials:")
    lines += [f'  {key}: ""' for key in SOCIAL_KEYS]
    lines += ["todo:", "  - Add picture", "  - Add socials", "  - Add description", "---", ""]
    text = "\n".join(lines)

    path = Path(f"./content/artists/{slug}/index.md")
    if not DRY_RUN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return slug, new_id


# ---------------------------------------------------------------------------
# Reference rewriting
# ---------------------------------------------------------------------------

def find_references(uuid):
    """Return the content files whose text contains ``uuid``."""
    hits = []
    for section in REFERENCING_SECTIONS:
        root = Path("./content") / section
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            try:
                if uuid in path.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(path)
            except OSError:
                continue
    return hits


def replace_reference(text, old_id, new_ids):
    """Replace a uuid by several uuids inside a YAML list, textually.

    Handles the block form (``  - "uuid"``) and the inline form
    (``artists: ["uuid", ...]``). Uuids already present in the same file are
    not added twice. Returns ``(text, changed, mode)``."""
    # Block list item, e.g. `  - "uuid"`.
    pattern = re.compile(rf'^([ \t]*)-[ \t]+["\']?{re.escape(old_id)}["\']?[ \t]*$',
                         re.MULTILINE)
    match = pattern.search(text)
    if match:
        indent = match.group(1)
        keep = [i for i in new_ids if f'"{i}"' not in text and f"'{i}'" not in text]
        if not keep:  # every component already listed: just drop the combo
            return pattern.sub("", text, count=1).replace("\n\n\n", "\n\n"), True, "block"
        block = "\n".join(f'{indent}- "{i}"' for i in keep)
        return pattern.sub(lambda _: block, text, count=1), True, "block"

    # Inline list, e.g. `artists: ["uuid", "other"]`.
    inline = re.compile(rf'(["\']){re.escape(old_id)}\1')
    if inline.search(text):
        keep = [i for i in new_ids if i not in text]
        repl = ", ".join(f'"{i}"' for i in keep) or '""'
        return inline.sub(repl, text, count=1), True, "inline"

    return text, False, "none"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    by_name, by_slug = load_artists()
    print(f"Splitting {len(SPLITS)} combined fiches. "
          f"Mode: {'DRY-RUN' if DRY_RUN else 'WRITE'}.\n")

    archive, created, rewritten, deleted, problems = [], 0, 0, 0, []

    for slug, components in SPLITS.items():
        data = by_slug.get(slug)
        if data is None:
            problems.append(f"{slug}: fiche not found")
            continue
        combo_id = data.get("id")
        print(f"== {data.get('title')}  ({slug})")

        # 1. Resolve or create every component.
        new_ids = []
        for name, kind in components:
            hit = by_name.get(normalize(name))
            if hit and hit != slug:
                comp_id = by_slug[hit].get("id")
                print(f"   . {name} -> existing {hit}")
            else:
                hit, comp_id = create_artist(name, kind)
                by_name[normalize(name)] = hit
                created += 1
                print(f"   + {name} -> created {hit} ({comp_id})")
            new_ids.append(comp_id)

        # 2. Rewrite the references.
        for path in find_references(combo_id):
            if path.parent.name == slug:
                continue  # the combined fiche itself
            text = path.read_text(encoding="utf-8")
            new_text, changed, mode = replace_reference(text, combo_id, new_ids)
            if not changed:
                problems.append(f"{path}: uuid present but not in a list (members?)")
                continue
            if not DRY_RUN:
                path.write_text(new_text, encoding="utf-8")
            rewritten += 1
            print(f"   ~ {path} ({mode})")

        # 3. Archive the description, then delete the combined fiche.
        body = (data.content or "").strip()
        if body:
            archive.append(f"## {data.get('title')} (`{slug}`)\n\n{body}\n")
            print(f"   ! description archived ({len(body)} chars)")
        if not DRY_RUN:
            shutil.rmtree(Path(f"./content/artists/{slug}"))
        deleted += 1
        print(f"   - deleted {slug}\n")

    if archive and not DRY_RUN:
        ARCHIVE.write_text(
            "# Descriptions of split (deleted) combined artist fiches\n\n"
            "Kept here so they can be reused on the component fiches.\n\n"
            + "\n".join(archive), encoding="utf-8")

    print(f"Done. created={created}, references_rewritten={rewritten}, "
          f"fiches_deleted={deleted}, archived_descriptions={len(archive)}")
    for issue in problems:
        print(f"  ! {issue}")
    if DRY_RUN:
        print("\nDRY_RUN is on — set DRY_RUN = False to apply.")


if __name__ == "__main__":
    main()
