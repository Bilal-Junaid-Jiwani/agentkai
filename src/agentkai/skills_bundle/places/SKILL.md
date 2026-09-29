---
name: places
description: Search places (restaurants, stores, parks, landmarks) and get details like hours and contact info, powered by OpenStreetMap.
version: 0.1.0
when: places, restaurant, cafe, coffee, shop, store, park, hotel nearby, address, directions, opening hours, map
---

# Places

Search the world's places and look up details (address, opening hours,
phone, website) using **OpenStreetMap** data — no API key needed.

Tools:

- `places_search(query, near, limit)` — free-text place search
  (e.g. query `sushi`, near `Karachi`). Returns name, coordinates,
  OSM type/id, and a short address.
- `place_details(osm_type, osm_id)` — full tags for one place:
  opening hours, phone, website, cuisine, address. `osm_type` is
  `node`, `way`, or `relation` (as returned by `places_search`).

## Setup

None. No key, no account. The tools call:

- Nominatim search (`https://nominatim.openstreetmap.org`) for `places_search`
- Overpass API (`https://overpass-api.de/api/interpreter`) for `place_details`

## Usage notes

- Data comes from OpenStreetMap contributors: coverage and opening-hours
  accuracy vary by region. Treat hours as advisory and confirm critical
  ones with the venue.
- **Rate limits are real:** Nominatim's usage policy asks for at most
  1 request/second — the tool enforces a ~1.1s gap between Nominatim
  calls. Overpass is a donated service: keep queries small, don't poll
  it in a loop.
- `osm_id` values are only stable per `osm_type`; always pass both.
- Coordinates are WGS84 (lat/lon).

## What's real / what's not

- REAL: place search, coordinates, addresses, OSM tags (hours/phone/
  website when mappers added them).
- NOT PROVIDED: live table availability, reservations, delivery ordering,
  reviews/ratings, turn-by-turn navigation. For bookings see the `travel`
  skill (pluggable providers); for food products see `shopping`.
