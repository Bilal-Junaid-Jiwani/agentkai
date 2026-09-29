---
name: travel
description: Live flight tracking (OpenSky, keyless) plus pluggable flight/hotel search providers you register yourself.
version: 0.1.0
when: flight, flights, flight status, track flight, hotel, hotels, booking, trip, itinerary, where is flight
---

# Travel

Two honest halves:

1. **Flight tracking is real and bundled** — `travel_flight_status`
   queries the OpenSky Network (keyless) for live ADS-B positions.
2. **Booking/availability search is pluggable** — there is no bundled
   flight or hotel booking provider. Public booking APIs need commercial
   keys, so `travel_search_flights` / `travel_search_hotels` only work
   after you register a provider.

Tools:

- `travel_flight_status(icao24, callsign)` — live position, altitude,
  velocity for one aircraft (pass either identifier).
- `travel_search_flights(origin, destination, date, provider)` —
  needs a registered provider.
- `travel_search_hotels(location, checkin, checkout, provider)` —
  needs a registered provider.

## Setup

- Flight tracking: nothing.
- Search providers: register in code:

```python
from agentkai.skills_bundle.travel import tools as travel_tools

def my_flight_search(origin: str, destination: str, date: str) -> list[dict]:
    # return dicts with keys: airline, flight_no, departs, arrives,
    # price, currency, booking_url
    ...

def my_hotel_search(location: str, checkin: str, checkout: str) -> list[dict]:
    # return dicts with keys: name, price_per_night, currency,
    # rating, booking_url
    ...

travel_tools.register_provider(
    "my_travel", flights=my_flight_search, hotels=my_hotel_search)
```

or via skill config: `SkillLoader(config={"travel_providers":
{"my_travel": {"flights": fn, "hotels": fn}}})`.

## Usage notes

- OpenSky tracks aircraft with ADS-B transponders in receiver coverage;
  oceanic/remote legs may vanish mid-flight — report gaps, don't invent.
  Anonymous OpenSky access is rate-limited; don't poll faster than ~10s.
- `icao24` is the 6-hex-digit transponder id (e.g. `4b1815`); `callsign`
  is the flight id broadcast by the aircraft (e.g. `PIA775`).
- Never claim a booking was made unless a provider actually confirmed
  one — search results are quotes, not tickets.

## What's real / what's not

- REAL: live flight positions/altitude/velocity (OpenSky); anything
  your registered provider returns.
- NOT PROVIDED: bundled booking, live fares, seat maps, PNR management.
  The agent never fakes a reservation.
