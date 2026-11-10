import pytest
from aiohttp import web

from inbound_agent import postcodes
from inbound_agent.postcodes import (
    BAD_FORMAT,
    FOUND,
    NOT_FOUND,
    UNAVAILABLE,
    lookup_postcode,
    normalise_uk_postcode,
    spoken_postcode,
)


@pytest.mark.parametrize("said,postcode", [
    ("SW1A 1AA", "SW1A 1AA"),
    ("sw1a1aa", "SW1A 1AA"),
    ("s w one a one a a", "SW1A 1AA"),
    ("S W 1 A, 1 A A", "SW1A 1AA"),
    ("S for Sugar W one A, one double A", "SW1A 1AA"),
    ("my postcode is e c one a, one b b", "EC1A 1BB"),
    ("M one one A E", "M1 1AE"),
    ("G two one, three A X", "G21 3AX"),
    ("oh X four four, nine Z Z", "OX44 9ZZ"),   # "oh" heard for the letter O
    ("CRO 2YR", "CR0 2YR"),                     # letter O heard for zero
    ("SW1A IAA", "SW1A 1AA"),                   # letter I heard for one
    ("sierra whiskey one alpha one alpha alpha", "SW1A 1AA"),
])
def test_spoken_postcodes_normalised(said, postcode):
    assert normalise_uk_postcode(said) == postcode


@pytest.mark.parametrize("said", ["", "ten downing street", "B one two three four five",
                                  "QQ1 1AA", "N for nine"])
def test_not_a_postcode(said):
    assert normalise_uk_postcode(said) == ""


def test_spoken_postcode():
    assert spoken_postcode("SW1A 1AA") == "S W 1 A, 1 A A"


@pytest.fixture
async def fake_postcodes_io():
    """A local stand-in for api.postcodes.io with the same response shapes."""
    known = {
        "SW1A1AA": {"postcode": "SW1A 1AA", "country": "England", "region": "London",
                    "admin_district": "Westminster"},
        "JE24WD": {"postcode": "JE2 4WD", "country": "Channel Islands", "region": None,
                   "admin_district": "Channel Islands"},
        "G213AX": {"postcode": "G21 3AX", "country": "Scotland", "region": None,
                   "admin_district": "Glasgow City"},
    }

    async def postcode(request):
        pc = request.match_info["pc"].upper()
        if pc in known:
            return web.json_response({"status": 200, "result": known[pc]})
        return web.json_response({"status": 404, "error": "Postcode not found"}, status=404)

    async def outcode(request):
        if request.match_info["oc"].upper() == "SW1A":
            return web.json_response({"status": 200, "result": {
                "outcode": "SW1A", "admin_district": ["Westminster", "Wandsworth"]}})
        return web.json_response({"status": 404, "error": "Outcode not found"}, status=404)

    app = web.Application()
    app.router.add_get("/postcodes/{pc}", postcode)
    app.router.add_get("/outcodes/{oc}", outcode)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    postcodes._cache.clear()
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()
    postcodes._cache.clear()


async def test_lookup_found(fake_postcodes_io):
    r = await lookup_postcode("s w one a one a a", fake_postcodes_io)
    assert r.status == FOUND and r.postcode == "SW1A 1AA"
    assert r.place == "Westminster, London" and r.in_uk


async def test_lookup_scotland_has_no_region(fake_postcodes_io):
    r = await lookup_postcode("G21 3AX", fake_postcodes_io)
    assert r.place == "Glasgow City, Scotland" and r.in_uk


async def test_lookup_channel_islands_accepted(fake_postcodes_io):
    r = await lookup_postcode("JE2 4WD", fake_postcodes_io)
    assert r.status == FOUND and r.in_uk


async def test_lookup_not_found_gives_outcode_area(fake_postcodes_io):
    r = await lookup_postcode("SW1A 9ZZ", fake_postcodes_io)
    assert r.status == NOT_FOUND and r.outcode_area == "Westminster or Wandsworth"


async def test_lookup_bad_format_skips_the_api(fake_postcodes_io):
    assert (await lookup_postcode("hello", fake_postcodes_io)).status == BAD_FORMAT


async def test_lookup_down_or_disabled_fails_soft():
    postcodes._cache.clear()
    assert (await lookup_postcode("SW1A 1AA", "")).status == UNAVAILABLE
    r = await lookup_postcode("SW1A 1AA", "http://127.0.0.1:9")   # nothing listening
    assert r.status == UNAVAILABLE and r.postcode == "SW1A 1AA"
