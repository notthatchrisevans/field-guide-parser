"""fg-pictures: pictures from each place's own Wikidata item. Offline: every
network request goes through a stubbed Net.get. Run: py -m pytest tests -q"""
import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fieldguide_parser import pictures as P  # noqa: E402

PIN = {"lat": 40.7614, "lng": -73.9776}


def _place(name, cat, q, coords=PIN):
    return {"name": name, "category": cat, "maps_query": q, "coords": dict(coords) if coords else None}


def _doc():
    places = {
        "moma": _place("MoMA", "museum", "MoMA, New York, NY"),
        "katz": _place("Katz's", "food", "Katz's, New York, NY"),
        "corner": _place("Corner", "public", "Corner, New York, NY"),
        "bridge": _place("Brooklyn Bridge", "public", "Brooklyn Bridge, New York, NY"),
        "hotel": _place("The Evelyn", "hotel", "The Evelyn, New York, NY"),
        "picked": _place("Whitney", "museum", "Whitney, New York, NY"),
        "unpinned": _place("Nowhere", "museum", "Nowhere, New York, NY", coords=None),
    }
    stops = [{"place": p} for p in places]
    stops[5]["images"] = [{"src": "images/nyc/whitney.jpg", "alt": "x"}]
    return {"trip": {"id": "nyc-test", "city": "New York",
                     "currency": {"local": "USD", "home": "USD"}},
            "places": places, "days": [{"date": "2026-09-19", "stops": stops}]}


GEO = {  # fg-geocode's cache: the OSM id of each pin
    "MoMA, New York, NY": {**PIN, "osm": "w1"},
    "Katz's, New York, NY": {**PIN, "osm": "n2"},
    "Corner, New York, NY": {**PIN, "osm": "w3"},
    "Brooklyn Bridge, New York, NY": {**PIN, "osm": "w4"},
    "The Evelyn, New York, NY": {**PIN, "osm": "n5"},
    "Whitney, New York, NY": {**PIN, "osm": "w6"},
}

OSM = {  # Nominatim lookup by id -> extratags
    "W1": {"wikidata": "Q188740", "website": "https://www.moma.org/"},
    "N2": {"website": "https://katzsdelicatessen.com/"},
    "W3": {},
    "W4": {"wikidata": "Q125006"},
    "N5": {"contact:website": "https://www.theevelyn.com/"},
    "W6": {"wikidata": "Q639791"},
}
P18 = {"Q188740": "MoMA.jpg", "Q125006": "Brooklyn Bridge.jpg", "Q60": "NYC skyline.jpg"}


class FakeNet:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, net, url, params=None, accept="application/json"):
        self.calls.append((url, params))
        for f in self.fail:
            if f in url or f in json.dumps(params or {}):
                raise OSError(f"boom {f}")
        if url == P.NOMINATIM_LOOKUP:
            oid = params["osm_ids"]
            tags = OSM.get(oid)
            hit = [] if tags is None else [{"osm_type": "way", "osm_id": oid[1:],
                                           "class": "tourism", "type": "museum",
                                           "extratags": tags,
                                           "address": {"country_code": "us"}}]
            return json.dumps(hit).encode()
        if url == P.WIKIDATA_API and params["action"] == "wbgetentities":
            q = params["ids"]
            claims = {"P18": [{"rank": "normal", "mainsnak": {"datavalue": {"value": P18[q]}}}]} \
                if q in P18 else {}
            return json.dumps({"entities": {q: {"claims": claims}}}).encode()
        if url == P.WIKIDATA_API and params["action"] == "wbsearchentities":
            return json.dumps({"search": [{"id": "Q1384"}, {"id": "Q60"}]}).encode()
        if url == P.WIKIDATA_SPARQL:
            return json.dumps({"results": {"bindings": [
                {"item": {"value": "http://www.wikidata.org/entity/Q60"}}]}}).encode()
        if url == P.COMMONS_API:
            name = params["titles"][5:]
            slug = urllib.parse.quote(name.replace(" ", "_"))
            return json.dumps({"query": {"pages": {"1": {"imageinfo": [{
                "thumburl": f"https://upload.wikimedia.org/thumb/{slug}/900px-{slug}",
                "url": f"https://upload.wikimedia.org/{slug}",
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{slug}",
                "extmetadata": {
                    "Artist": {"value": '<a href="//commons.wikimedia.org/wiki/User:Jo">Jo &amp; Al</a>'},
                    "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}}}).encode()
        if url.startswith("https://upload.wikimedia.org/"):
            return b"\xff\xd8 fake jpeg"
        raise AssertionError(f"unexpected request {url} {params}")


def run(tmp_path, monkeypatch, fake=None, doc=None, *flags):
    fake = fake or FakeNet()
    monkeypatch.setattr(P.Net, "get", lambda net, *a, **k: fake(net, *a, **k))
    itin = tmp_path / "itinerary.json"
    itin.write_text(json.dumps(doc or _doc()), encoding="utf-8")
    geo = tmp_path / "geo.json"
    geo.write_text(json.dumps(GEO), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["fg-pictures", str(itin),
                                      "--cache", str(tmp_path / "pc.json"),
                                      "--geocode-cache", str(geo),
                                      "--images", str(tmp_path / "images"),
                                      "--sleep", "0", *flags])
    code = P.main()
    return code, json.loads(itin.read_text(encoding="utf-8")), fake


def test_wikidata_to_thumbnail_to_credit(tmp_path, monkeypatch):
    code, doc, _ = run(tmp_path, monkeypatch)
    assert code == 0
    pic = doc["places"]["moma"]["picture"]
    assert pic == {"src": "images/nyc-test/auto/moma.jpg", "credit": "Jo & Al",
                   "license": "CC BY-SA 4.0", "source": "wikimedia",
                   "page": "https://commons.wikimedia.org/wiki/File:MoMA.jpg"}
    assert (tmp_path / "images" / "nyc-test" / "auto" / "moma.jpg").read_bytes().startswith(b"\xff\xd8")


def test_category_filter(tmp_path, monkeypatch):
    _, doc, _ = run(tmp_path, monkeypatch)
    p = doc["places"]
    assert "picture" not in p["corner"]          # a street corner: nothing
    assert "picture" not in p["hotel"]           # a stay without an item: nothing
    assert p["bridge"]["picture"]["src"].endswith("bridge.jpg")   # [public], but a landmark
    assert "picture" not in p["katz"]            # no item: left for the live sources


def test_hand_picked_and_unpinned_are_left_alone(tmp_path, monkeypatch):
    _, doc, fake = run(tmp_path, monkeypatch)
    assert "picture" not in doc["places"]["picked"]
    assert "picture" not in doc["places"]["unpinned"]
    asked = [p.get("osm_ids") for u, p in fake.calls if u == P.NOMINATIM_LOOKUP]
    assert "W6" not in asked


def test_website_recorded(tmp_path, monkeypatch):
    _, doc, _ = run(tmp_path, monkeypatch)
    assert doc["places"]["katz"]["website"] == "https://katzsdelicatessen.com/"
    assert doc["places"]["hotel"]["website"] == "https://www.theevelyn.com/"   # contact:website
    assert "website" not in doc["places"]["corner"]


def test_cache_hit_means_no_network(tmp_path, monkeypatch):
    _, first, _ = run(tmp_path, monkeypatch)
    _, again, fake = run(tmp_path, monkeypatch, FakeNet(fail=["http"]))
    assert fake.calls == []
    assert again["places"]["moma"]["picture"] == first["places"]["moma"]["picture"]
    assert again["trip"]["picture"] == first["trip"]["picture"]


def test_miss_remembered(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    cache = json.loads((tmp_path / "pc.json").read_text(encoding="utf-8"))
    assert "miss" not in cache["osm"]["N2"]           # found, no item: re-checked later
    assert cache["osm"]["N2"]["wikidata"] is None
    # an item with no image is a remembered miss
    OSM["W3"] = {"wikidata": "Q999"}
    try:
        (tmp_path / "pc.json").unlink()
        run(tmp_path, monkeypatch)
        cache = json.loads((tmp_path / "pc.json").read_text(encoding="utf-8"))
        assert "miss" in cache["wikidata"]["Q999"]
        _, _, fake = run(tmp_path, monkeypatch)
        assert not any(p and p.get("ids") == "Q999" for _, p in fake.calls)
    finally:
        OSM["W3"] = {}


def test_old_miss_is_asked_again(tmp_path, monkeypatch):
    (tmp_path / "pc.json").write_text(json.dumps(
        {"wikidata": {"Q188740": {"miss": "2020-01-01"}}}), encoding="utf-8")
    _, doc, _ = run(tmp_path, monkeypatch)
    assert doc["places"]["moma"]["picture"]


def test_failure_is_soft(tmp_path, monkeypatch, capsys):
    fake = FakeNet(fail=["Q188740"])                 # Wikidata fails for MoMA only
    code, doc, _ = run(tmp_path, monkeypatch, fake, None, "--soft")
    assert code == 0
    assert "picture" not in doc["places"]["moma"]
    assert doc["places"]["bridge"]["picture"]       # the rest carried on
    assert "moma" in capsys.readouterr().err
    cache = json.loads((tmp_path / "pc.json").read_text(encoding="utf-8"))
    assert "Q188740" not in cache.get("wikidata", {})   # a failure is not a miss
    code, _, _ = run(tmp_path, monkeypatch, FakeNet(fail=["Q188740"]))
    assert code == 1                                 # without --soft it says so


def test_blocked_host_is_not_hammered(tmp_path, monkeypatch):
    import urllib.error
    calls = []

    def urlopen(req, timeout=0):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, None)
    monkeypatch.setattr(P.urllib.request, "urlopen", urlopen)
    itin = tmp_path / "itinerary.json"
    itin.write_text(json.dumps(_doc()), encoding="utf-8")
    geo = tmp_path / "geo.json"
    geo.write_text(json.dumps(GEO), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["fg-pictures", str(itin), "--cache", str(tmp_path / "pc.json"),
                                      "--geocode-cache", str(geo), "--sleep", "0", "--soft"])
    assert P.main() == 0
    assert sum("nominatim" in u for u in calls) == 1


def test_trip_picture_is_the_city(tmp_path, monkeypatch):
    _, doc, fake = run(tmp_path, monkeypatch)
    pic = doc["trip"]["picture"]
    assert pic["src"] == "images/nyc-test/auto/_trip.jpg"
    assert pic["page"].endswith("NYC_skyline.jpg")
    sparql = [p["query"] for u, p in fake.calls if u == P.WIKIDATA_SPARQL][0]
    assert '"US"' in sparql and "wd:Q60" in sparql


def test_trip_picture_falls_back_to_first_stop(tmp_path, monkeypatch):
    fake = FakeNet(fail=["wbsearchentities"])
    _, doc, _ = run(tmp_path, monkeypatch, fake, None, "--soft")
    assert doc["trip"]["picture"] == doc["places"]["moma"]["picture"]


def test_trip_picture_override(tmp_path, monkeypatch):
    d = _doc()
    d["trip"]["picture_file"] = "Chosen view.jpg"
    _, doc, fake = run(tmp_path, monkeypatch, None, d)
    assert doc["trip"]["picture"]["page"].endswith("Chosen_view.jpg")
    assert not any(u == P.WIKIDATA_SPARQL for u, _ in fake.calls)


def test_a_city_hit_is_not_a_stop_picture():
    rec = P.osm_record({"class": "place", "type": "city",
                        "extratags": {"wikidata": "Q60"}})
    assert rec["wikidata"] is None


def test_a_searched_hit_must_sit_on_the_pin(tmp_path, monkeypatch):
    """A hand-verified pin (no OSM id in the geocode cache) only takes a
    search hit within MATCH_M of the pin."""
    d = _doc()
    d["places"] = {"moma": d["places"]["moma"]}
    d["days"][0]["stops"] = [{"place": "moma"}]
    far = {"osm_type": "way", "osm_id": 1, "lat": "40.80", "lon": "-73.95",
           "class": "tourism", "type": "museum", "extratags": {"wikidata": "Q188740"}}
    fake = FakeNet()
    base = fake.__call__

    def route(net, url, params=None, accept="application/json"):
        if url == P.NOMINATIM_SEARCH:
            fake.calls.append((url, params))
            return json.dumps([far]).encode()
        return base(net, url, params, accept)
    monkeypatch.setitem(GEO, "MoMA, New York, NY", {"lat": 1.0, "lng": 1.0, "osm": "w1"})
    _, doc, _ = run(tmp_path, monkeypatch, route, d, "--soft")
    assert "picture" not in doc["places"]["moma"]
    cache = json.loads((tmp_path / "pc.json").read_text(encoding="utf-8"))
    assert "miss" in cache["query"]["MoMA, New York, NY"]


def test_strip_html():
    assert P.strip_html('<span>A <b>B</b></span>&nbsp;C') == "A B C"


def test_parser_carries_the_chosen_trip_picture():
    from fieldguide_parser import parse_guide_md as M
    text = (ROOT / "tests" / "fixtures" / "nyc-2026-09.md").read_text(encoding="utf-8")
    assert "picture_file" not in M.build(text)["doc"]["trip"]
    text = text.replace("\n---", "\npicture: Manhattan from the Top of the Rock.jpg\n---", 1)
    out = M.build(text)
    assert out["doc"]["trip"]["picture_file"] == "Manhattan from the Top of the Rock.jpg"
