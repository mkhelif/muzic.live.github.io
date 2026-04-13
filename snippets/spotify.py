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
import traceback
import uuid

# Configure authentication token
CLIENT_TOKEN="AABNEmTiwxcNuWsNd+DrJfsKm/EHI0EDmE1ENA415Bahr8I/k97nMnkQLXft3BAeM7yr8rKoxOezmykcZiqh1R5MROl9IfJYUelNp/6+tvm0kwM9Xm8+jTyxpLJlX8bfMEZcnsfycXKQieagqC+CX3qFDUjtu+v7I7DyJytzUpbqoPXokxUbIkFAhxWOKM5wFdeXN5k9OgMAwm7yXOPVannDh3TEdC4wHmQt/GStdlCO+zjf56yMjHgKchM5qzaPiBbHRqcqnYJDBIyR385IlSnU8YMNQN6RRWNFSa43j7wgCJeIUvoMq9OX5Xbu3wUj+dK0sbJelfdMbZCU5UPq"
ACCESS_TOKEN="Bearer BQBQZlJnD-RyjlGfbbRpYMry-xre-gwYVt4320iHYVURT_CpkFo4CF2xtl_8fKDfXj_FkRWrLizVWpz9h5lBKHV5adATZTNpLHk65O2ROd5MjKGW2HoJ5rxgpapZBY8bTb8Nm4Hi9Bg"

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
    COUNTRIES[country.alpha_2.upper()] = {
        "name": _(country.name),
        "code": country.alpha_3,
    }


LOCATIONS = {
    'House of Blues Las Vegas ': 'House of Blues',
    'Cournon D Auvergne': 'Cournon d\'Auvergne',
    'Paris 18': 'Paris'
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

    concert_details["location"] = {
        'country': COUNTRIES[content['data']['concert']['location']['country']],
        'city': translate(content['data']['concert']['location']['city'].title(), LOCATIONS),
        'name': translate(content['data']['concert']['location']['name'].title(), LOCATIONS),
    }
    concert_details["artists"] = []
    concert_details["festival"] = content['data']['concert']['festival']

    # Compute artists list
    if len(content['data']['concert']['artists']['items']) > 5:
        return None

    for concert_artist in content['data']['concert']['artists']['items']:
        concert_details['artists'].append(translate(concert_artist['data']['profile']['name'], ARTISTS))
    concert_details['artists'] = sorted(set(concert_details['artists']))

    return concert_details

def load_frontmatter(file):
    try:
        return frontmatter.loads(file.read_text())
    except Exception as e:
        print(f"Failed to load frontmatter for {file}")
        raise e

def translate(key, hash):
    if key in hash:
        return hash[key]
    else:
        return key

def format_filename(name):
    return re.sub('-{2,}', '-',
           re.sub('[^a-z0-9]', '-',
              unidecode(name).lower()))

def get_or_create_artist(name):
    artist_id = None
    directory = Path(f"./content/artists/{format_filename(name)}")
    directory.mkdir(parents = True, exist_ok = True)
    file = directory.joinpath("index.md")

    if file.exists():
        artist_id = load_frontmatter(file).get('id', None)
    else:
        artist_id = uuid.uuid4()
        file.write_text(f"""\
---
id: "{artist_id}"
title: "{name}"
cover: "cover.jpg"
socials:
  facebook: ""
todo:
  - Add picture
  - Add socials
  - Add description
---
""")
    if artist_id is None:
        raise Exception(f"Could not create artist {name}")
    return str(artist_id)

def get_or_create_location_country(country):
    country_id = None
    directory = Path(f"./content/locations/{format_filename(country['code'])}")
    directory.mkdir(parents = True, exist_ok = True)
    file = directory.joinpath("_index.md")

    if file.exists():
        country_id = load_frontmatter(file).get('id', None)
    else:
        country_id = uuid.uuid4()
        file.write_text(f"""\
---
id: "{country_id}"
title: "{country['name']}"
---
""")
    if country_id is None:
        raise Exception(f"Could not create country {country}")
    return country_id

def get_or_create_location_city(country, city):
    city_id = None
    directory = Path(f"./content/locations/{format_filename(country['code'])}/{format_filename(city)}")
    directory.mkdir(parents = True, exist_ok = True)
    file = directory.joinpath("_index.md")

    if file.exists():
        city_id = load_frontmatter(file).get('id', None)
    else:
        city_id = uuid.uuid4()
        file.write_text(f"""\
---
id: "{city_id}"
locationId: "{get_or_create_location_country(country)}"
title: "{city}"
---
""")
    if city_id is None:
        raise Exception(f"Could not create city {city} - {country}")
    return city_id

def get_or_create_location(location):
    location_id = None
    directory = Path(f"./content/locations/{format_filename(location['country']['code'])}/{format_filename(location['city'])}/{format_filename(location['name'])}")
    directory.mkdir(parents = True, exist_ok = True)
    file = directory.joinpath("index.md")

    if file.exists():
        location_id = load_frontmatter(file).get('id', None)
    else:
        location_id = uuid.uuid4()
        file.write_text(f"""\
---
id: "{location_id}"
locationId: "{get_or_create_location_city(location['country'], location['city'])}"
title: "{location['name']}"
---
""")
    if location_id is None:
        raise Exception(f"Could not create location {location['country']['name']} - {location['city']} - {location['name']}")
    return location_id

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
                if concert['festival'] is True:
                    location = concert['location']['country']['name'] + ', ' + concert['location']['city'] + ', ' + concert['location']['name']
                    print(f"  - (festival) {date_format}: {', '.join(concert['artists'])} ({location})")
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
locationId: "{location_id}"
artists:
  - {artists_list}
---
""", encoding = "UTF-8")
        except Exception:
            print(traceback.format_exc())
