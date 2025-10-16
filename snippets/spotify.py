#!/usr/bin/env python3

from datetime import datetime
from os import listdir
from pathlib import Path

import frontmatter
import gettext
import pycountry
import re
import requests

# Configure authentication token
CLIENT_TOKEN="AAC+Sbtbjv8KVm6JJ7E7s0bZYIGLqZstekNQ0Lu6x/dP6IJB/okAjUdJQdWbLj+6n9LD1MGuDDjSZ9l1sENg6bllIKuzusGu3P8S0EmOSQLz+zmJcudt20g0ovDGHX7gSCuHAQsOtR46YPBF0OYok+uB0QR2D/V1Y+dp/u3CKXGo3w6Zycb46BJH7WNjd99LJrN8yt4TvfWQWAiAWRupnVi2qmdHx9NMfAshpIHOguAG26vuHN0iD9RsN9BcCj7rKUOH3siEviF+MceFlO98zM4o5ZGYu1VNwgsG9H1+v832pVUYc8eojg2mob2AwlS6qGDdJqgIDUST3ABaGTIfh42YWwyKgpw="
ACCESS_TOKEN="Bearer BQDXc2-0SgzAKsyWr8A1SUhrY3qWdJa7GWtYPq4bJul2xLdVmanKTdDGHeo-qaWeEYOQ105fkjrmPYH522sk3wMlzjbPeNMC_MsOHEtOgoRfW-q3SGuS9rDnOKFXBz9c5qoD7PuWQxI"

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

french = gettext.translation('iso3166-1', pycountry.LOCALES_DIR, languages = ['fr'])
french.install()
_ = french.gettext

COUNTRIES = {}
for country in pycountry.countries:
    COUNTRIES[country.alpha_2.upper()] = _(country.name)

# Utility functions
def get_artist_concerts(spotify_id):
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
    concert_details["locations"] = [
        content['data']['concert']['location']['name'],
        content['data']['concert']['location']['city'],
        COUNTRIES[content['data']['concert']['location']['country']]
    ]
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
                artists_list = "\n  - ".join(concert['artists'])
                locations_list = "\n  - ".join(concert['locations'])

                directory = Path(f"./content/events/{date.year}/{date.month:02d}/{date.day:02d}")
                directory.mkdir(parents = True, exist_ok = True)

                filename = "-".join(re.sub('-{2,}', '-', re.sub('[^a-z0-9]', '-', artist.lower())) for artist in concert['artists']) + ".md"
                event = Path(f"./content/events/{date.year}/{date.month:02d}/{date.day:02d}/{filename}")
                if not event.exists():
                  event.write_text(f"""\
---
eventDate: "{date.isoformat()}"
artists:
  - {artists_list}
locations:
  - {locations_list}
---
""", encoding = "UTF-8")
        except Exception as e:
            print(e)
