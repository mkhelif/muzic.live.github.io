#!/usr/bin/env python3
from datetime import datetime
from os import listdir
from pathlib import Path

import requests

import utils

# Configure authentication token
ACCESS_TOKEN="Bearer eyJhbGciOiJSUzI1NiIsImtpZCI6Imp3dC1rZXktMjAyMDA0IiwidHlwIjoiSldUIn0.eyJ1c2VySWQiOiI1MTk5MzMzMzAyIiwiYXBwSWQiOjYzMjM4NCwic2NvcGVzIjpbImFsbCJdLCJpc3MiOiJEZWV6ZXIgQXV0aCBTZXJ2aWNlIiwiZXhwIjoxNzc2MTk4ODkyLCJpYXQiOjE3NzYxOTg1MzJ9.hASmtiHKCwAplktV7L0SPP_64wRytq2KVExvX2tDYFVIetmUe7FDMQr7AnPkchXACJrOdM3iac8vynRH3kWGFkHp7KpuQeYtXNQYw9OPWJOCzRea1ev9pWl88iNB6SK9Lp2wD-MzS7e8Pe3mOGQ5n39CwQFk2jV1V1AWcR_BEy22S2mmGt2pcLFhl4_vxDipBPEJEJnlmtELyhLoL0LyHpdAk1oZQO-PJHK3xO18Rt6AFhlHw3HrlqbIadNjsngLngQkRy6E36AZkAAJA5hGVkNsLpGM9x-JBIrx8BvgAcUt6RT5NTDnIgu-djTQW-uwkrAPMRITMZyOPaVFWlMc4Q"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:149.0) Gecko/20100101 Firefox/149.0",
    "Accept": "*/*",
    "Accept-Language": "fr-FR",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Content-Type": "application/json",
    "Referer": "https://www.deezer.com/",
    "Origin": "https://www.deezer.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Authorization": ACCESS_TOKEN,
    "Connection": "keep-alive",
}

COUNTRIES_MAPPING = {
    'FP': 'PF',
}

# Utility functions
def get_artist_concerts(artistId):
    response = requests.post('https://pipe.deezer.com/api',
        headers = DEFAULT_HEADERS,
        json = {
        "operationName": "LiveEventList",
        "variables": {
            "artistId": artistId,
            "liveEventsFirst": 70
        },
        "query": """query LiveEventList($artistId: String!, $liveEventsFirst: Int!) {
  artist(artistId: $artistId) {
    id
    name
    ...ArtistLiveEvents
    __typename
  }
}

fragment ArtistLiveEvents on Artist {
  liveEvents(
    first: $liveEventsFirst
    types: [CONCERT, FESTIVAL]
    statuses: [PENDING]
  ) {
    edges {
      node {
        id
        name
        startDate
        cityName
        countryCode
        types {
          isConcert
          isFestival
          isLivestreamConcert
          isLivestreamFestival
          __typename
        }
        venue
        __typename
      }
      __typename
    }
    pageInfo {
      endCursor
      hasNextPage
      __typename
    }
    __typename
  }
  __typename
}"""
    })
    if not response.ok:
        raise Exception(f"Failed to fetch artist information ({response.status_code}): {response.content}")

    data = response.json()
    #if data['errors'] is not None:
    #    raise Exception(f"Failed to fetch artist information: {data['errors'][0]['message']}")

    # For each concert, load its details
    concerts_list = []
    for concert_info in data['data']['artist']['liveEvents']['edges']:
        if concert_info['node']['countryCode'] is None:
            continue

        concert = get_concert(concert_info['node']['id'], concert_info['node']['countryCode'])
        if concert is not None:
            concerts_list.append(concert)
    return concerts_list

def get_concert(eventId, country):
    response = requests.post("https://pipe.deezer.com/api",
        headers = DEFAULT_HEADERS,
        json = {
        "operationName": "LiveEvent",
        "variables": {
            "contributorsFirst": 12,
            "albumFirst": 12,
            "eventId": eventId
        },
        "query": """query LiveEvent($eventId: String!, $contributorsFirst: Int = 12, $albumFirst: Int = 12) {
  liveEvent(liveEventId: $eventId) {
    id
    name
    startDate
    status
    venue
    cityName
    hasSubscribedToNotification
    sources {
      coBranding {
        logoAsset {
          lightThemeUIAsset {
            id
            urls(uiAssetRequest: {width: 730, height: 182})
            __typename
          }
          darkThemeUIAsset {
            id
            urls(uiAssetRequest: {width: 730, height: 182})
            __typename
          }
          __typename
        }
        __typename
      }
      defaultUrl
      __typename
    }
    live {
      id
      externalUrl {
        url
        __typename
      }
      __typename
    }
    types {
      isConcert
      isFestival
      isLivestreamConcert
      isLivestreamFestival
      __typename
    }
    videos(types: [TRAILER]) {
      edges {
        node {
          id
          externalUrl {
            url
            __typename
          }
          type
          __typename
        }
        __typename
      }
      __typename
    }
    contributors(first: $contributorsFirst) {
      edges {
        concertContributorMetadata {
          roles {
            isMain
            isSupport
            __typename
          }
          performanceOrder
          __typename
        }
        cursor
        node {
          ... on Artist {
            id
            name
            isFavorite
            fansCount
            albums(
              types: [ALBUM]
              order: RELEASE_DATE
              mode: OFFICIAL
              roles: [MAIN]
              first: $albumFirst
              after: null
            ) {
              edges {
                cursor
                node {
                  id
                  displayTitle
                  releaseDate
                  cover {
                    md5
                    ...PictureSmall
                    ...PictureMedium
                    ...PictureLarge
                    __typename
                  }
                  __typename
                }
                __typename
              }
              __typename
            }
            picture {
              md5
              ...PictureSmall
              ...PictureMedium
              ...PictureLarge
              __typename
            }
            url {
              webUrl
              deepLink
              __typename
            }
            isFavorite
            fansCount
            __typename
          }
          __typename
        }
        __typename
      }
      pageInfo {
        hasNextPage
        startCursor
        hasPreviousPage
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment PictureSmall on Picture {
  id
  small: urls(pictureRequest: {height: 100, width: 100})
  explicitStatus
  __typename
}

fragment PictureMedium on Picture {
  id
  medium: urls(pictureRequest: {width: 264, height: 264})
  explicitStatus
  __typename
}

fragment PictureLarge on Picture {
  id
  large: urls(pictureRequest: {width: 500, height: 500})
  explicitStatus
  __typename
}"""
    })
    if not response.ok:
        raise Exception(f"Failed to fetch concert information ({response.status_code}): {response.content}")

    content = response.json()
    event = content['data']['liveEvent']

    # Retrieve concert information
    concert_details = {
        "date": event['startDate'],
        "location": {
            'country': utils.COUNTRIES[utils.translate(country, COUNTRIES_MAPPING)],
            'city': event['cityName'],
            'name': event['venue'],
        },
        "artists": [],
        "festival": event['types']['isFestival'] or event['types']['isLivestreamFestival']
    }

    # Compute artists list
    #if len(event['contributors']['edges']) > 5:
    #    return None

    for concert_artist in event['contributors']['edges']:
        concert_details['artists'].append(concert_artist['node']['name'])
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

        data = utils.load_frontmatter(file)
        name = data.get('title', None)
        socials = data.get('socials', None)
        deezerId = socials.get('deezer', None) if socials is not None else None
        if deezerId is None:
            continue

        print(f"{name}")
        try:
            concerts = get_artist_concerts(deezerId)
            for concert in concerts:
                date = datetime.fromisoformat(concert['date'])
                date_format = f"{date.year}/{date.month:02d}/{date.day:02d}"
                artist_ids = [utils.get_or_create_artist(artist) for artist in concert['artists']]
                artists_list = "\n  - ".join(f'"{aid}"' for aid in artist_ids)

                # Create directory structure
                directory = Path(f"./content/events/{date_format}")
                directory.mkdir(parents = True, exist_ok = True)

                # Compute event filename
                filename = "-".join(utils.format_filename(artist) for artist in concert['artists']) + ".md"
                if concert['festival'] is True:
                    location = concert['location']['name'] + ', ' + concert['location']['city'] + ', ' + concert['location']['country']['name']
                    print(f"  - (festival) {date_format}: {', '.join(concert['artists'])} ({location})")
                    continue

                # Compute location ID
                location_id = utils.get_or_create_location(concert['location'])
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
