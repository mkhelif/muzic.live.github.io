#!/usr/bin/env python3

from datetime import datetime
from os import listdir
from pathlib import Path
from unidecode import unidecode

import frontmatter
import gettext
import pycountry
import re
import requests

# Configure authentication token
CLIENT_TOKEN=""
ACCESS_TOKEN="Bearer "

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Accept": "application/json",
    "Accept-Language": "en-GB",
    "Accept-Encoding": "application/json",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://open.spotify.com/",
    "app-platform": "WebPlayer",
    "spotify-app-version": "1.2.86.442.gda390418",
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

french = gettext.translation('iso3166-1', pycountry.LOCALES_DIR, languages = ['fr'])
french.install()
_ = french.gettext

COUNTRIES = {}
for country in pycountry.countries:
    COUNTRIES[country.alpha_2.upper()] = _(country.name)

LOCATIONS = {
    'House of Blues Las Vegas ': 'House of Blues',
    'Cournon D Auvergne': 'Cournon d\'Auvergne',
}

ARTISTS = {
    'Carlos Santana': 'Santana',
    'Udo Dirkschneider': 'Dirkschneider'
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

    # For each concert, load its details
    concerts_list = []
    for concert_info in response.json()['data']['artistUnion']['goods']['concerts']['items']:
        concert = get_concert(concert_info['data']['uri'])
        if concert is not None:
            concerts_list.append(concert)
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
        translate(content['data']['concert']['location']['name'].title(), LOCATIONS),
        translate(content['data']['concert']['location']['city'].title(), LOCATIONS),
        COUNTRIES[content['data']['concert']['location']['country']]
    ]
    concert_details["artists"] = []
    concert_details["festival"] = content['data']['concert']['festival']

    # Compute artists list
    if len(content['data']['concert']['artists']['items']) > 5:
        return None

    for concert_artist in content['data']['concert']['artists']['items']:
        concert_details['artists'].append(translate(concert_artist['data']['profile']['name'], ARTISTS))
    concert_details['artists'] = sorted(set(concert_details['artists']))

    return concert_details

def translate(key, hash):
    if key in hash:
        return hash[key]
    else:
        return key

def format_filename(name):
    return re.sub('-{2,}', '-',
           re.sub('[^a-z0-9]', '-',
              unidecode(name).lower()))

#
# The script will go through all artists declared
#
if __name__ == '__main__':
    # Fetch concerts for all artists
    for artist in sorted(listdir('./content/artists')):
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
                date_format = f"{date.year}/{date.month:02d}/{date.day:02d}"
                artists_list = "\n  - ".join(concert['artists'])
                locations_list = "\n  - ".join(concert['locations'])

                if concert["festival"] is True:
                    print(f"  - (festival) {date_format}: {', '.join(concert['artists'])} ({', '.join(concert['locations'])})")
                    continue

                directory = Path(f"./content/events/{date_format}")
                directory.mkdir(parents = True, exist_ok = True)

                filename = "-".join(format_filename(artist) for artist in concert['artists']) + ".md"
                event = Path(f"./content/events/{date_format}/{filename}")
                if not event.exists():
                  event.write_text(f"""\
---
date: "{date.isoformat()}"
artists:
  - {artists_list}
locations:
  - {locations_list}
---
""", encoding = "UTF-8")
        except Exception as e:
            print(e)
