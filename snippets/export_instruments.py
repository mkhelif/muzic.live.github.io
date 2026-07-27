#!/usr/bin/env python3
"""
Export the exhaustive MusicBrainz instrument list to a role-mapping CSV.

Pages through the MusicBrainz browse API (``/ws/2/instrument/all`` — the
complete instrument tree, ~1050 entries) and writes
``snippets/instrument_roles.csv`` with columns::

    instrument,type,role

``role`` is prefilled with a proposed mapping to the project vocabulary
(``sing``/``guitar``/``bass``/``drums``/``keys``/``other``) based on the
instrument name and its MusicBrainz type (Percussion instrument -> drums,
Keyboard -> keys, ...). Edit the CSV by hand to refine the mapping:
``fill_members.py`` loads it automatically when present and falls back to its
keyword heuristics for anything not listed.

Run from the repository root::

    python3 snippets/export_instruments.py
    python3 snippets/export_instruments.py --insecure-ssl
"""

from __future__ import annotations

import argparse
import csv
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "snippets" / "instrument_roles.csv"
USER_AGENT = "MuzicLiveResearch/1.0 (instrument list export)"

PAGE_SIZE = 100


def propose_role(name: str, mb_type: str) -> str:
    """Best-effort mapping of an instrument to the project role vocabulary."""
    n = name.lower()
    t = (mb_type or "").lower()

    if "vocal" in n or "voice" in n:
        return "sing"
    if "bass" in n and ("guitar" in n or n in ("bass guitar", "electric bass guitar", "acoustic bass guitar")):
        return "bass"
    if n in ("double bass", "contrabass", "upright bass", "bass viol", "bass violin", "washtub bass", "footbass"):
        return "bass"
    if "guitar" in n or n in ("banjo", "mandolin", "banjitar", "bouzouki", "ukulele"):
        return "guitar"
    if "drum" in n or "percussion" in t or "percussion" in n or n in ("cymbal", "cymbals", "timpani", "congas", "bongos", "cajón", "cajon", "tambourine"):
        return "drums"
    if "keyboard" in t or "keyboard" in n or "piano" in n or "organ" in n or "synth" in n or n in (
        "keys", "harpsichord", "clavinet", "celesta", "mellotron", "rhodes piano", "wurlitzer electric piano", "accordion"
    ):
        return "keys"
    return "other"


def fetch_page(offset: int, delay: float, retries: int, ssl_context) -> dict:
    url = "https://musicbrainz.org/ws/2/instrument/all?" + urllib.parse.urlencode(
        {"fmt": "json", "limit": str(PAGE_SIZE), "offset": str(offset)}
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=35, context=ssl_context) as response:
                data = json.load(response)
            time.sleep(delay)
            return data
        except urllib.error.HTTPError as error:
            if error.code in (503, 429):
                time.sleep(10 + attempt * 10)
                continue
            raise
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(5 + attempt * 5)
    raise RuntimeError(f"Could not fetch {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export MusicBrainz instruments to a role-mapping CSV.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"CSV path. Default: {DEFAULT_OUTPUT}")
    parser.add_argument("--delay", type=float, default=1.1, help="Delay between MusicBrainz requests. Default: 1.1")
    parser.add_argument("--retries", type=int, default=3, help="Network retries per request. Default: 3")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable SSL verification if local certificates are broken.")
    args = parser.parse_args()

    ssl_context = ssl._create_unverified_context() if args.insecure_ssl else None

    instruments: dict[str, str] = {}
    offset = 0
    total = None
    while total is None or offset < total:
        data = fetch_page(offset, args.delay, args.retries, ssl_context)
        total = data.get("instrument-count") or data.get("count") or 0
        page = data.get("instruments") or []
        if not page:
            break
        for instrument in page:
            name = (instrument.get("name") or "").strip()
            if name:
                instruments[name] = instrument.get("type") or ""
        offset += PAGE_SIZE
        print(f"fetched {min(offset, total)}/{total}", flush=True)

    if not instruments:
        print("No instruments fetched; aborting without writing.", file=sys.stderr)
        return 1

    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["instrument", "type", "role"])
        for name in sorted(instruments, key=str.casefold):
            mb_type = instruments[name]
            writer.writerow([name, mb_type, propose_role(name, mb_type)])

    print(f"done: {len(instruments)} instruments -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
