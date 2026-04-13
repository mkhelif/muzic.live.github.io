#!/usr/bin/env python3
"""Remove duplicate events where an artist appears multiple times on the same day.

For each pair of events on the same day sharing at least one artist,
the event with fewer artists is deleted. When both have the same count,
the festival event is preferred. If neither or both are festivals,
the one with the longer filename is kept.
"""

import os
from collections import defaultdict
from pathlib import Path

import frontmatter

EVENTS_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / ".." / "content" / "events"


def parse_event(filepath):
    """Parse an event file and return (date_str, artists_set, has_festival)."""
    post = frontmatter.load(filepath)

    date_val = post.get("date")
    if not date_val:
        return None, set(), False

    date_str = str(date_val)[:10]

    artists = post.get("artists", [])
    if not artists:
        return None, set(), False

    has_festival = bool(post.get("festivals"))

    return date_str, set(artists), has_festival


def pick_winner(fp_i, artists_i, fest_i, fp_j, artists_j, fest_j):
    """Return (loser, winner) between two overlapping events.

    Priority: 1) has festival, 2) more artists, 3) longer filename.
    """
    if fest_i != fest_j:
        if fest_i:
            return fp_j, fp_i
        return fp_i, fp_j

    if len(artists_i) != len(artists_j):
        if len(artists_i) > len(artists_j):
            return fp_j, fp_i
        return fp_i, fp_j

    if len(fp_i.name) >= len(fp_j.name):
        return fp_j, fp_i
    return fp_i, fp_j


def main():
    events = []
    for filepath in sorted(EVENTS_DIR.rglob("*.md")):
        date_str, artists, has_festival = parse_event(filepath)
        if date_str and artists:
            events.append((filepath, date_str, artists, has_festival))

    print(f"Parsed {len(events)} events")

    by_date = defaultdict(list)
    for filepath, date_str, artists, has_festival in events:
        by_date[date_str].append((filepath, artists, has_festival))

    to_delete = set()
    kept_over = []

    for date_str, day_events in sorted(by_date.items()):
        if len(day_events) < 2:
            continue

        for i in range(len(day_events)):
            fp_i, artists_i, fest_i = day_events[i]
            if fp_i in to_delete:
                continue

            for j in range(i + 1, len(day_events)):
                fp_j, artists_j, fest_j = day_events[j]
                if fp_j in to_delete:
                    continue

                shared = artists_i & artists_j
                if not shared:
                    continue

                loser, winner = pick_winner(fp_i, artists_i, fest_i, fp_j, artists_j, fest_j)
                to_delete.add(loser)
                kept_over.append((loser, winner, shared))
                if loser == fp_i:
                    break

    # Report and delete
    print(f"\nFound {len(to_delete)} duplicates to delete:\n")
    for deleted, kept, shared in sorted(kept_over, key=lambda x: str(x[0])):
        if deleted in to_delete:  # Only report actual deletions
            rel_del = deleted.relative_to(EVENTS_DIR)
            rel_kept = kept.relative_to(EVENTS_DIR)
            print(f"  DELETE {rel_del}")
            print(f"    KEPT {rel_kept}  (shared: {', '.join(sorted(shared))})")

    # Perform deletions
    for fp in sorted(to_delete):
        os.remove(fp)

    # Clean up empty directories
    for dirpath, dirnames, filenames in os.walk(EVENTS_DIR, topdown=False):
        if not dirnames and not filenames:
            os.rmdir(dirpath)

    print(f"\nDeleted {len(to_delete)} files")


if __name__ == "__main__":
    main()
