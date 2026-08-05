#!/usr/bin/env python3
"""
Export the exhaustive MusicBrainz instrument list to a role-mapping CSV.

Pages through the MusicBrainz *search* API (``/ws/2/instrument?query=*``) and
writes
``snippets/instrument_roles.csv`` with columns::

    instrument,type,role

``role`` is prefilled with a proposed mapping to the project vocabulary
(``sing``/``guitar``/``bass``/``drums``/``keys``/``other``) based on the
instrument name and its MusicBrainz type (Percussion instrument -> drums,
Keyboard -> keys, ...). Edit the CSV by hand to refine the mapping:
``fill_musicbrainz.py`` loads it automatically when present and falls back to
its keyword heuristics for anything not listed.

Run from the repository root::

    python3 snippets/export_instruments.py
    python3 snippets/export_instruments.py --insecure-ssl
    python3 snippets/export_instruments.py --query 'type:"string instrument"'

Note: MusicBrainz exposes no "list all instruments" endpoint — ``/ws/2/instrument``
supports lookup (by MBID), browse (by collection) and search only, and search
requires a ``query``. That is why an earlier ``/ws/2/instrument/all`` failed with
HTTP 400: ``all`` was parsed as an MBID.
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

# Max allowed by the API.
PAGE_SIZE = 100

# "Give me everything" queries. `query` is mandatory on the search endpoint.
MATCH_ALL_QUERIES = ["*", "*:*"]


# Kept in sync with ROLE_ORDER in musicbrainz.py and with the labels in
# layouts/_partials/artists/member-roles.html.
ROLE_ORDER = [
    "sing",
    "guitar", "bass",
    "drums", "percussion",
    "keys", "accordion",
    "strings", "violin", "harp", "banjo", "mandolin",
    "wind", "flute", "saxophone", "trumpet", "trombone", "harmonica", "bagpipe",
    "dj", "dance",
    "other",
]

# Instruments whose name contains the key map straight to the role. Ordered:
# the first match wins, so put the specific before the generic.
NAME_TO_ROLE = [
    ("harmonica", "harmonica"), ("bagpipe", "bagpipe"), ("accordion", "accordion"),
    ("saxophone", "saxophone"), ("trumpet", "trumpet"), ("trombone", "trombone"),
    ("flute", "flute"), ("violin", "violin"), ("fiddle", "violin"),
    ("harp", "harp"), ("banjo", "banjo"), ("mandolin", "mandolin"),
    ("turntable", "dj"), ("theremin", "keys"),
]


def propose_role(name: str, mb_type: str) -> str:
    """Best-effort mapping of an instrument to the project role vocabulary."""
    n = name.lower()
    t = (mb_type or "").lower()

    if "vocal" in n or "voice" in n:
        return "sing"
    for needle, role in NAME_TO_ROLE:
        if needle in n:
            return role
    if "bass" in n and ("guitar" in n or n in ("bass guitar", "electric bass guitar", "acoustic bass guitar")):
        return "bass"
    if n in ("double bass", "contrabass", "upright bass", "bass viol", "bass violin", "washtub bass", "footbass"):
        return "bass"
    if "guitar" in n or n in ("banjo", "mandolin", "banjitar", "bouzouki", "ukulele"):
        return "guitar"
    if "drum" in n or "percussion" in t or "percussion" in n or n in ("cymbal", "cymbals", "timpani", "congas", "bongos", "cajón", "cajon", "tambourine"):
        return "drums"
    if "keyboard" in t or "keyboard" in n or "piano" in n or "organ" in n or "synth" in n or n in (
        "keys", "harpsichord", "clavinet", "celesta", "mellotron", "rhodes piano", "wurlitzer electric piano"
    ):
        return "keys"
    # MusicBrainz types are the last resort, and map to the family roles.
    if "string" in t:
        return "strings"
    if "wind" in t or "brass" in t or "reed" in t:
        return "wind"
    return "other"


def fetch_page(offset: int, delay: float, retries: int, ssl_context, query: str) -> dict:
    """Fetch one page of the instrument index.

    MusicBrainz has no "list every instrument" endpoint: ``/ws/2/instrument``
    only supports *lookup* (by MBID), *browse* (by collection) and *search*.
    Search it is — and its ``query`` parameter is mandatory, which is why the
    earlier ``/ws/2/instrument/all`` returned HTTP 400: ``all`` was being read
    as an MBID."""
    url = "https://musicbrainz.org/ws/2/instrument?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": str(PAGE_SIZE), "offset": str(offset)}
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
            # Surface what the server actually said instead of a bare traceback.
            try:
                body = error.read().decode("utf-8", "replace")[:300]
            except Exception:
                body = ""
            raise RuntimeError(
                f"MusicBrainz returned HTTP {error.code} for:\n  {url}\n  {body}"
            ) from None
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
    parser.add_argument("--query", default=None, help=f"Override the Lucene query. Default: tries {MATCH_ALL_QUERIES} in order.")
    args = parser.parse_args()

    ssl_context = ssl._create_unverified_context() if args.insecure_ssl else None

    # Match-all queries, in order of preference. Solr's edismax accepts "*",
    # the classic parser wants "*:*"; try both rather than assume.
    queries = [args.query] if args.query else MATCH_ALL_QUERIES

    instruments: dict[str, str] = {}
    used_query = None
    for query in queries:
        offset = 0
        total = None
        while total is None or offset < total:
            data = fetch_page(offset, args.delay, args.retries, ssl_context, query)
            total = data.get("count") or data.get("instrument-count") or 0
            page = data.get("instruments") or []
            if not page:
                break
            for instrument in page:
                name = (instrument.get("name") or "").strip()
                if name:
                    instruments[name] = instrument.get("type") or ""
            offset += PAGE_SIZE
            print(f"[{query}] fetched {min(offset, total)}/{total}", flush=True)
        if instruments:
            used_query = query
            break
        print(f"query {query!r} returned nothing; trying the next one", file=sys.stderr)

    if not instruments:
        print("No instruments fetched; aborting without writing.", file=sys.stderr)
        return 1
    print(f"matched with query {used_query!r}", flush=True)

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
