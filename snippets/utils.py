import uuid
from pathlib import Path

import frontmatter
import gettext
import pycountry
import re

from unidecode import unidecode

# Load French country names
french = gettext.translation('iso3166-1', pycountry.LOCALES_DIR, languages = ['fr'])
french.install()
_ = french.gettext

# Cache countries code
COUNTRIES = {}
for country in pycountry.countries:
    COUNTRIES[country.alpha_2.upper()] = {
        "name": _(country.name),
        "code": country.alpha_3,
    }

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
