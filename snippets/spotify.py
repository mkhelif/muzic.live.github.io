#!/usr/bin/env python3

import traceback
from datetime import datetime
from os import listdir
from pathlib import Path

import requests

from utils import (
    ARTISTS,
    COUNTRIES,
    VENUES,
    format_filename,
    get_or_create_artist,
    get_or_create_location,
    is_stale,
    load_frontmatter,
    set_last_update,
    translate,
)

# Configure authentication token
CLIENT_TOKEN=""
ACCESS_TOKEN="Bearer"

SPOTIFY_CLIENT_VERSION = "1.2.95.408.g4647020a"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Accept": "application/json",
    "Accept-Language": "en-GB",
    "Accept-Encoding": "application/json",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://open.spotify.com/",
    "app-platform": "WebPlayer",
    "spotify-app-version": SPOTIFY_CLIENT_VERSION,
    "client-token": CLIENT_TOKEN,
    "authorization": ACCESS_TOKEN,
    "Origin": "https://open.spotify.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive",
    "Priority": "u=4",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "TE": "trailers"
}

# Utility functions
def get_artist_concerts(spotify_id):
    response = requests.post(
        'https://api-partner.spotify.com/pathfinder/v2/query',
        json = {
            "variables": {
                "uri": f"spotify:artist:{spotify_id}",
                "preReleaseV2": False,
                "locale": ""
            },
            "operationName": "queryArtistOverview",
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "5b9e64f43843fa3a9b6a98543600299b0a2cbbbccfdcdcef2402eb9c1017ca4c" # Apparently can remain the same
                }
            }
        },
        headers = DEFAULT_HEADERS
    )
    if not response.ok:
        raise Exception(f"Failed to fetch artist information ({response.status_code}): {response.content}")

    # For each concert, load its details.
    data = response.json().get('data') or {}
    artist_union = data.get('artistUnion') or {}
    goods = artist_union.get('goods') or {}
    concerts = goods.get('concerts') or {}
    concert_items = concerts.get('items') or []

    concerts_list = []
    for concert_info in concert_items:
        concert = get_concert((concert_info.get('data') or {}).get('uri'))
        if concert is not None:
            concerts_list.append(concert)
    return concerts_list

def get_concert(concert_uri):
    if not concert_uri:
        return None
    response = requests.post(
        'https://api-partner.spotify.com/pathfinder/v2/query',
        json = {
            "variables": {
                "uri": concert_uri,
                "authenticated": False
            },
            "operationName": "concert",
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "6313561b79fa89c9cd2f0f1c1392a5de6b0c6ab475648ecb176ecb8dc9b43d3a"
                }
            }
        },
        headers = DEFAULT_HEADERS
    )
    if not response.ok:
        raise Exception(f"Failed to fetch concert details ({response.status_code})")

    content = response.json()

    # Retrieve concert information
    concert_details = {}
    concert_details["date"] = content['data']['concert']['startDateIsoString']

    country_code = content['data']['concert']['location']['country']
    concert_details["location"] = {
        'country': COUNTRIES.get(country_code, {"name": country_code, "code": country_code}),
        'city': translate(content['data']['concert']['location']['city'].title(), VENUES),
        'name': translate(content['data']['concert']['location']['name'].title(), VENUES),
    }
    concert_details["artists"] = []
    concert_details["festival"] = content['data']['concert']['festival']
    concert_details["title"] = content['data']['concert'].get('title')

    # Compute artists list
    if len(content['data']['concert']['artists']['items']) > 5:
        return None

    for concert_artist in content['data']['concert']['artists']['items']:
        concert_details['artists'].append(translate(concert_artist['data']['profile']['name'].strip(), ARTISTS))
    concert_details['artists'] = sorted(set(concert_details['artists']))

    return concert_details


#
# The script will go through all artists declared
#
if __name__ == '__main__':
    # Fetch concerts for all artists
    for artist in sorted(listdir('./content/artists')):
        file = Path(f"./content/artists/{artist}/index.md")
        if not file.exists():
            continue

        data = load_frontmatter(file)
        name = data.get('title', None)
        socials = data.get('socials', None)
        spotifyId = socials.get('spotify', None) if socials is not None else None
        if spotifyId is None:
            continue

        # Skip artists already refreshed from Spotify recently.
        if not is_stale(data, "spotify"):
            print(f"{name} (skipped: refreshed recently)")
            continue

        print(f"{name}")
        try:
            concerts = get_artist_concerts(spotifyId)
            for concert in concerts:
                date = datetime.fromisoformat(concert['date'])
                date_format = f"{date.year}/{date.month:02d}/{date.day:02d}"
                artist_ids = [get_or_create_artist(artist) for artist in concert['artists']]
                artists_list = "\n  - ".join(f'"{aid}"' for aid in artist_ids)

                # Create directory structure
                directory = Path(f"./content/events/{date_format}")
                directory.mkdir(parents = True, exist_ok = True)

                # Compute event filename
                filename = "-".join(format_filename(artist) for artist in concert['artists']) + ".md"

                # Festivals are ignored
                if concert['festival'] is True:
                    continue

                # Compute location ID
                location_id = get_or_create_location(concert['location'])
                if location_id is None:
                    raise Exception(f"Could not find or create location: {concert['location']}")

                # Create event file
                event = Path(f"./content/events/{date_format}/{filename}")
                if not event.exists():
                  event.write_text(f"""\
---
date: {date.isoformat()}
venue: "{location_id}"
artists:
  - {artists_list}
---
""", encoding = "UTF-8")

            # Mark this artist as refreshed from Spotify today.
            set_last_update(file, "spotify")
        except Exception:
            print(traceback.format_exc())
