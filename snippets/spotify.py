#!/usr/bin/env python3

from datetime import datetime
from os import listdir
from pathlib import Path

import frontmatter
import re
import requests

# Configure authentication token
CLIENT_TOKEN="AAAaADEzu28vaiQJU3qtqf5zjBvAPP7xLgVbM7suM0fgFenvp6Q5jjy46++L6+O6G6QM0o7rleu2u2oPMhbJYewM+ZqXqiCAWJOlgCE6hlAgbL7q+qff1fQlooHcvFg3hYtBDyh/Wthhi+a2X/qxvPEmOnM9dzfEoSpFG9NytDQ0xHyIYfjqPeBRhCGzUY0AdXMX2Znld5XfAGfZEr5KlBCOX21LHxZKwNsetPzY2qIzz1dssCTlN+DoLd3MmS4zYlzxlUMtOKMvGVuH98iQmA1TYkRo/gaHvXoKrTjanOqdtRCtFOXmQ2aw3gz6ZOUnUa1hYk4ZwP/6fbSUWg/dDrJbCi3sLG48sw=="
ACCESS_TOKEN="Bearer BQCvzGv22IA48JO2OLoxskvowLvDxba-2NFVDSWCkcqDUFJbsP1O3sN1kyQ6ELaLIjQ7chdUkB6JMoRfA5plW4z-SZkKGJcorAghd3xo_3XJXJ35K1PCyj-mDCtsWMZWnXb74TcEmv0"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Accept": "application/json",
    "Accept-Language": "en-GB",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://open.spotify.com/",
    "app-platform": "WebPlayer",
    "spotify-app-version": "1.2.75.457.g9ae6e679",
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
    "TE": "trailer"
}

# Utility functions
def get_artist_concerts(spotify_id):
    print(f"  - Fetching concerts {spotify_id}")
    response = requests.post(
        'https://api-partner.spotify.com/pathfinder/v2/query',
        json = {
            "variables": {
                "artistUri": f"spotify:artist:{spotify_id}",
                "geoHash": None,
                "includeNearby": False
            },
            "operationName": "ArtistConcerts",
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "ef53c43b865496b9890b7167eab1dc614a8949ef9451b3c41184ea888de8bd2b" # Apparently can remain the same
                }
            }
        },
        headers = DEFAULT_HEADERS
    )
    if not response.ok:
        raise Exception(f"Failed to fetch concerts ({response.status_code})")

    # For each concert, load its details
    concerts_list = []
    for concert_info in response.json()['data']['concerts']['concerts']['items']:
        concerts_list.append(get_concert(concert_info['data']['uri']))
    return concerts_list

def get_concert(concert_uri):
    print(f"  - Fetching concert details {concert_uri}")
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
    concert_details["location"] = content['data']['concert']['location']['name']
    concert_details["artists"] = []

    # Compute artists list
    for concert_artist in content['data']['concert']['artists']['items']:
        concert_details['artists'].append(concert_artist['data']['profile']['name'])

    return concert_details

#
# The script will go through all artists declared
#
if __name__ == '__main__':
    # Fetch concerts for all artists
    for artist in listdir('./content/artists'):
        file = Path(f"./content/artists/{artist}/index.md")
        if not file.exists():
            continue

        data = frontmatter.loads(file.read_text())
        name = data.get('title', None)
        socials = data.get('socials', None)
        spotifyId = socials.get('spotify', None) if socials is not None else None
        if spotifyId is None:
            continue

        print(f"{name}")
        try:
            concerts = get_artist_concerts(spotifyId)
            for concert in concerts:
                date = datetime.fromisoformat(concert['date'])
                artists_list = (""
                                "  - ".join(concert['artists']))

                directory = Path(f"./content/events/{date.year}/{date.month:02d}/{date.day:02d}")
                directory.mkdir(parents = True, exist_ok = True)

                filename = "-".join(re.sub('-{2,}', '-', re.sub('[^a-z0-9]', '-', artist.lower())) for artist in concert['artists']) + ".md"
                event = Path(f"./content/events/{date.year}/{date.month:02d}/{date.day:02d}/{filename}")
                event.write_text(("---"
                   f"eventDate: \"{concert['date']}\""
                    "artists:"
                   f"  - {artists_list}"
                    "locations:"
                   f"  - {concert['location']}"
                    "---"
                    ""), encoding = "UTF-8")
        except Exception as e:
            print(e)
