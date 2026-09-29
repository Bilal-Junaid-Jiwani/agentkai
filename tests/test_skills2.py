"""Tests for skills pack 2: places, image_search, shopping, travel, health,
payments. No network anywhere — all HTTP goes through FakeTransport."""
import json

import pytest

from agentkai.skills import SkillLoader
from agentkai.skills_bundle.health import tools as health_tools
from agentkai.skills_bundle.image_search import tools as image_search_tools
from agentkai.skills_bundle.payments import tools as payments_tools
from agentkai.skills_bundle.places import tools as places_tools
from agentkai.skills_bundle.shopping import tools as shopping_tools
from agentkai.skills_bundle.travel import tools as travel_tools


# ---- fake HTTP ---------------------------------------------------------------

class FakeTransport:
    """Canned responder: routes = [(method, url_part, status, payload)]."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers), "body": body})
        for m, part, status, payload in self.routes:
            if m == method and part in url:
                raw = (payload if isinstance(payload, bytes)
                       else json.dumps(payload).encode())
                return status, {}, raw
        return 404, {}, b'{"message": "not mocked"}'


@pytest.fixture()
def home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTKAI_HOME", str(tmp_path))
    for var in ("GITHUB_TOKEN", "GMAIL_TOKEN", "GCAL_TOKEN",
                "SPOTIFY_TOKEN", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    # places enforces Nominatim's ~1 req/s gap; tests reset the clock.
    monkeypatch.setattr(places_tools, "_last_nominatim_call", 0.0)
    return tmp_path


def by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not found")


def cfg(home, routes):
    return {"home": home, "transport": FakeTransport(routes)}


# ---- discovery ---------------------------------------------------------------

PACK2 = ["places", "image_search", "shopping", "travel", "health",
         "payments"]


def test_pack2_discovered_and_load(home_env):
    loader = SkillLoader(config={"home": home_env,
                                 "transport": FakeTransport([])})
    names = [n for n, _, _ in loader.discover()]
    for skill in PACK2:
        assert skill in names, f"{skill} not discovered"
        loaded = loader.load(skill)
        assert loaded.tools, f"{skill} has no tools"


def test_tool_names_unique(home_env):
    loader = SkillLoader(config={"home": home_env,
                                 "transport": FakeTransport([])})
    tools = loader.tools()
    names = [t.name for t in tools]
    assert len(names) == len(set(names))


def test_risk_levels(home_env):
    loader = SkillLoader(config={"home": home_env,
                                 "transport": FakeTransport([])})
    by_skill: dict[str, list] = {}
    for skill in PACK2:
        by_skill[skill] = [t.risk for t in loader.load(skill).tools]
    assert set(by_skill["payments"]) == {"high"}
    for skill in ("places", "image_search", "shopping", "travel",
                  "health"):
        assert set(by_skill[skill]) == {"low"}, skill
    for skill in PACK2:
        for tool in loader.load(skill).tools:
            assert tool.json_schema.get("type") == "object"
            assert tool.name and tool.description


# ---- places ------------------------------------------------------------------

NOMINATIM_OK = [{"place_id": 1, "name": "Cafe A",
                 "display_name": "Cafe A, Karachi, Pakistan",
                 "lat": "24.8607", "lon": "67.0011",
                 "class": "amenity", "type": "cafe",
                 "osm_type": "node", "osm_id": 111,
                 "address": {"road": "Main St", "city": "Karachi",
                             "country": "Pakistan"}}]

OVERPASS_OK = {"elements": [
    {"type": "node", "id": 111, "lat": 24.8607, "lon": 67.0011,
     "tags": {"name": "Cafe A", "amenity": "cafe",
              "opening_hours": "Mo-Sa 09:00-22:00",
              "phone": "+92 21 1234567",
              "website": "https://example.com",
              "addr:city": "Karachi"}}]}


def test_places_search(home_env):
    tools = places_tools.get_tools(
        cfg(home_env, [("GET", "/search", 200, NOMINATIM_OK)]))
    res = by_name(tools, "places_search").func("cafe", near="Karachi")
    assert isinstance(res, list) and len(res) == 1
    assert res[0]["name"] == "Cafe A"
    assert res[0]["osm_type"] == "node" and res[0]["osm_id"] == 111
    assert "Karachi" in res[0]["address"]


def test_places_search_validation(home_env):
    tools = places_tools.get_tools(cfg(home_env, []))
    assert by_name(tools, "places_search").func("").startswith("ERROR")


def test_places_search_http_error(home_env):
    tools = places_tools.get_tools(
        cfg(home_env, [("GET", "/search", 500, {"message": "boom"})]))
    res = by_name(tools, "places_search").func("cafe")
    assert res.startswith("ERROR: places search failed")


def test_place_details(home_env):
    tools = places_tools.get_tools(
        cfg(home_env, [("POST", "/api/interpreter", 200, OVERPASS_OK)]))
    res = by_name(tools, "place_details").func("node", 111)
    assert res["name"] == "Cafe A"
    assert res["opening_hours"] == "Mo-Sa 09:00-22:00"
    assert res["phone"] == "+92 21 1234567"
    assert res["address"] == {"city": "Karachi"}


def test_place_details_bad_type(home_env):
    tools = places_tools.get_tools(cfg(home_env, []))
    res = by_name(tools, "place_details").func("planet", 1)
    assert res.startswith("ERROR")


def test_place_details_missing(home_env):
    tools = places_tools.get_tools(
        cfg(home_env, [("POST", "/api/interpreter", 200,
                        {"elements": []})]))
    res = by_name(tools, "place_details").func("node", 999)
    assert "no OSM" in res


# ---- image_search ------------------------------------------------------------

OPENVERSE_OK = {"results": [
    {"title": "Fluffy cat", "url": "https://img.example/cat.jpg",
     "foreign_landing_url": "https://example.com/cat",
     "license": "by", "creator": "Jane", "width": 800,
     "height": 600}]}


def test_image_search(home_env):
    tools = __import__(
        "agentkai.skills_bundle.image_search.tools",
        fromlist=["get_tools"]).get_tools(
        cfg(home_env, [("GET", "/v1/images/", 200, OPENVERSE_OK)]))
    res = by_name(tools, "image_search").func("cat", limit=5)
    assert len(res) == 1
    assert res[0]["image_url"] == "https://img.example/cat.jpg"
    assert res[0]["source_page"] == "https://example.com/cat"
    assert res[0]["license"] == "by"


def test_image_search_validation(home_env):
    mod = __import__("agentkai.skills_bundle.image_search.tools",
                     fromlist=["get_tools"])
    tools = mod.get_tools(cfg(home_env, []))
    fn = by_name(tools, "image_search").func
    assert fn("").startswith("ERROR")
    assert fn("cat", license_type="bogus").startswith("ERROR")


def test_image_search_http_error(home_env):
    mod = __import__("agentkai.skills_bundle.image_search.tools",
                     fromlist=["get_tools"])
    tools = mod.get_tools(
        cfg(home_env, [("GET", "/v1/images/", 429, {"message": "slow"})]))
    assert by_name(tools, "image_search").func("cat").startswith(
        "ERROR: image search failed")


# ---- shopping ----------------------------------------------------------------

OFF_OK = {"products": [
    {"code": "123", "product_name": "Oat Milk",
     "brands": "Oatly", "image_url": "https://img.example/oat.jpg",
     "nutriscore_grade": "a"}]}


def test_shopping_openfoodfacts(home_env):
    tools = shopping_tools.get_tools(
        cfg(home_env, [("GET", "search.pl", 200, OFF_OK)]))
    res = by_name(tools, "shopping_search").func("oat milk")
    assert len(res) == 1
    assert res[0]["name"] == "Oat Milk"
    assert res[0]["price"] is None  # honest: OFF rarely has prices
    assert res[0]["product_url"].endswith("/product/123")


def test_shopping_custom_provider_via_config(home_env):
    def demo_search(query, limit):
        assert query == "laptop"
        return [{"name": "Demo Laptop", "price": 999.0,
                 "currency": "usd", "brand": "Demo",
                 "image_url": None, "product_url": "https://x.example/1"}]

    config = cfg(home_env, [])
    config["shopping_providers"] = {"demo": demo_search}
    tools = shopping_tools.get_tools(config)
    res = by_name(tools, "shopping_search").func("laptop",
                                                 provider="demo")
    assert res[0] == {"name": "Demo Laptop", "brand": "Demo",
                      "price": 999.0, "currency": "usd",
                      "image_url": None,
                      "product_url": "https://x.example/1"}


def test_shopping_register_provider(home_env):
    shopping_tools.register_provider(
        "tmp_shop_for_test", lambda q, n: [{"name": "X"}])
    try:
        tools = shopping_tools.get_tools(cfg(home_env, []))
        res = by_name(tools, "shopping_search").func(
            "x", provider="tmp_shop_for_test")
        assert res[0]["name"] == "X"
    finally:
        del shopping_tools._registry["tmp_shop_for_test"]


def test_shopping_unknown_provider(home_env):
    tools = shopping_tools.get_tools(cfg(home_env, []))
    res = by_name(tools, "shopping_search").func("laptop",
                                                 provider="nope")
    assert res.startswith("ERROR: unknown shopping provider")
    assert "openfoodfacts" in res


def test_shopping_bad_provider_shape(home_env):
    config = cfg(home_env, [])
    config["shopping_providers"] = {"bad": lambda q, n: "notalist"}
    tools = shopping_tools.get_tools(config)
    res = by_name(tools, "shopping_search").func("x", provider="bad")
    assert res.startswith("ERROR: provider")


# ---- travel ------------------------------------------------------------------

OPENSKY_OK = {"time": 1700000000, "states": [
    ["4b1815", "PIA775  ", "Pakistan", 1700000000, 1700000000,
     67.1, 24.9, 10000.0, False, 250.0, 90.0, 0.5,
     None, 10100.0, "1234", False, 0]]}


def test_travel_flight_status(home_env):
    tools = travel_tools.get_tools(
        cfg(home_env, [("GET", "states/all", 200, OPENSKY_OK)]))
    res = by_name(tools, "travel_flight_status").func(icao24="4B1815")
    assert res["callsign"] == "PIA775"
    assert res["lat"] == 24.9 and res["lon"] == 67.1
    assert res["altitude_m"] == 10000.0


def test_travel_flight_status_not_found(home_env):
    tools = travel_tools.get_tools(
        cfg(home_env, [("GET", "states/all", 200,
                        {"time": 1, "states": []})]))
    res = by_name(tools, "travel_flight_status").func(callsign="NOPE1")
    assert "no live ADS-B track" in res


def test_travel_flight_status_needs_id(home_env):
    tools = travel_tools.get_tools(cfg(home_env, []))
    assert by_name(tools, "travel_flight_status").func().startswith(
        "ERROR")


def test_travel_search_no_provider(home_env):
    tools = travel_tools.get_tools(cfg(home_env, []))
    res = by_name(tools, "travel_search_flights").func(
        "KHI", "LHR", "2026-12-01")
    assert "no flight search provider" in res
    res = by_name(tools, "travel_search_hotels").func(
        "Karachi", "2026-12-01", "2026-12-05")
    assert "no hotel search provider" in res


def test_travel_search_with_provider(home_env):
    def flights(o, d, dt):
        return [{"airline": "Demo Air", "flight_no": "DA1",
                 "departs": dt, "arrives": dt, "price": 500,
                 "currency": "usd", "booking_url": "https://x.example/b"}]

    travel_tools.register_provider("tmp_travel_for_test",
                                       flights=flights)
    try:
        tools = travel_tools.get_tools(cfg(home_env, []))
        res = by_name(tools, "travel_search_flights").func(
            "KHI", "LHR", "2026-12-01", provider="tmp_travel_for_test")
        assert res[0]["flight_no"] == "DA1"
    finally:
        del travel_tools._registry["tmp_travel_for_test"]


# ---- health ------------------------------------------------------------------

EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData>
 <Record type="HKQuantityTypeIdentifierStepCount" value="4000" startDate="2026-09-28 08:00:00 +0500" endDate="2026-09-28 08:00:00 +0500"/>
 <Record type="HKQuantityTypeIdentifierStepCount" value="6000" startDate="2026-09-29 08:00:00 +0500" endDate="2026-09-29 08:00:00 +0500"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepCore" startDate="2026-09-28 23:00:00 +0500" endDate="2026-09-29 06:30:00 +0500"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" startDate="2026-09-29 07:00:00 +0500" endDate="2026-09-29 07:30:00 +0500" totalEnergyBurned="300"/>
</HealthData>
"""


def _write_health(home, xml=True, steps_csv=False):
    hdir = home / "health"
    hdir.mkdir(parents=True, exist_ok=True)
    if xml:
        (hdir / "export.xml").write_text(EXPORT_XML, encoding="utf-8")
    if steps_csv:
        (hdir / "steps.csv").write_text(
            "date,steps\n2026-09-27,8000\n", encoding="utf-8")


def test_health_summary_xml(home_env):
    _write_health(home_env)
    tools = health_tools.get_tools({"home": home_env})
    res = by_name(tools, "health_summary").func(days=30)
    assert res["avg_daily_steps"] == 5000.0  # (4000+6000)/2 days w/ data
    assert res["days_with_step_data"] == 2
    assert res["workouts_in_window"] == 1
    assert res["avg_sleep_hours"] == 7.5
    assert "export.xml" in res["files_read"]


def test_health_summary_csv_merge(home_env):
    _write_health(home_env, steps_csv=True)
    tools = health_tools.get_tools({"home": home_env})
    res = by_name(tools, "health_summary").func(days=30)
    assert res["days_with_step_data"] == 3  # xml 2 days + csv 1 day
    assert "steps.csv" in res["files_read"]


def test_health_summary_empty(home_env):
    tools = health_tools.get_tools({"home": home_env})
    res = by_name(tools, "health_summary").func()
    assert isinstance(res, str) and "No health data found" in res
    assert by_name(tools, "health_workouts").func().startswith(
        "No health data found")


def test_health_workouts(home_env):
    _write_health(home_env)
    tools = health_tools.get_tools({"home": home_env})
    res = by_name(tools, "health_workouts").func(limit=5)
    assert len(res) == 1
    assert res[0]["type"] == "run"
    assert res[0]["duration_min"] == 30.0
    assert res[0]["calories"] == 300.0


# ---- payments ----------------------------------------------------------------

PI_OK = {"id": "pi_test123", "amount": 1000, "currency": "usd",
         "status": "requires_payment_method",
         "description": "Test", "client_secret": "pi_test123_secret_x",
         "created": 1700000000}


def _stripe_cfg(home, routes, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    return {"home": home, "transport": FakeTransport(routes)}


def test_payments_no_key(home_env):
    tools = payments_tools.get_tools({"home": home_env})
    res = by_name(tools, "payments_create_intent").func(1000)
    assert "not configured" in res
    assert "STRIPE_SECRET_KEY" in res


def test_payments_create_intent(home_env, monkeypatch):
    config = _stripe_cfg(
        home_env, [("POST", "payment_intents", 200, PI_OK)], monkeypatch)
    tools = payments_tools.get_tools(config)
    res = by_name(tools, "payments_create_intent").func(
        1000, currency="usd", description="Test")
    assert res["id"] == "pi_test123"
    assert res["status"] == "requires_payment_method"
    assert res["client_secret"] == "pi_test123_secret_x"
    # form-encoded body, secret key used as Basic auth (never in URL/body)
    transport = config["transport"]
    call = transport.calls[0]
    body = call["body"].decode()
    assert "amount=1000" in body and "currency=usd" in body
    assert call["headers"]["Content-Type"] == \
        "application/x-www-form-urlencoded"
    assert call["headers"]["Authorization"].startswith("Basic ")
    assert "sk_test_fake" not in body and "sk_test_fake" not in call["url"]


def test_payments_create_validation(home_env, monkeypatch):
    config = _stripe_cfg(home_env, [], monkeypatch)
    tools = payments_tools.get_tools(config)
    fn = by_name(tools, "payments_create_intent").func
    assert fn(0).startswith("ERROR: amount must be a positive")
    assert fn(-5).startswith("ERROR: amount must be a positive")
    assert fn("abc").startswith("ERROR: amount must be an integer")
    assert fn(100, currency="usdd").startswith("ERROR: currency")


def test_payments_create_api_error(home_env, monkeypatch):
    config = _stripe_cfg(
        home_env,
        [("POST", "payment_intents", 401,
          {"error": {"message": "Invalid API Key"}})], monkeypatch)
    tools = payments_tools.get_tools(config)
    res = by_name(tools, "payments_create_intent").func(100)
    assert res.startswith("ERROR: create payment intent failed")
    assert "sk_test" not in res  # key never leaks into errors


def test_payments_get_intent(home_env, monkeypatch):
    config = _stripe_cfg(
        home_env, [("GET", "v1/payment_intents/pi_test123", 200, PI_OK)],
        monkeypatch)
    tools = payments_tools.get_tools(config)
    res = by_name(tools, "payments_get_intent").func("pi_test123")
    assert res["id"] == "pi_test123"
    assert res["amount"] == 1000


def test_payments_get_intent_bad_id(home_env, monkeypatch):
    config = _stripe_cfg(home_env, [], monkeypatch)
    tools = payments_tools.get_tools(config)
    assert by_name(tools, "payments_get_intent").func("nope").startswith(
        "ERROR: payment_intent_id")
