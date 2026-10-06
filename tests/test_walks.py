"""Walks: street-corner pins (Overpass), per-day plausibility, fg-walkcheck.
Offline: Nominatim and Overpass are stubbed. Run: py -m pytest tests -q"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fieldguide_parser import geocode as G  # noqa: E402
from fieldguide_parser import intersections as X  # noqa: E402
from fieldguide_parser import walkcheck as W  # noqa: E402


# ------------------------------------------------------------ parsing / names

@pytest.mark.parametrize("q, want", [
    ("Seventh Avenue and West 47th Street, Manhattan, NY", ("pair", "Seventh Avenue", "West 47th Street")),
    ("5th Ave & E 26th St", ("pair", "5th Ave", "E 26th St")),
    ("Broadway at W 23rd St, New York", ("pair", "Broadway", "W 23rd St")),
    ("Corner of Mott and Mosco, Manhattan", ("pair", "Mott", "Mosco")),
    ("Hudson Street to Barrow", ("segment", "Hudson Street", "Barrow")),
    ("103 Orchard Street, New York, NY 10002", None),
    ("Chelsea Market, 75 Ninth Avenue", None),
])
def test_parse_streets(q, want):
    assert X.parse_streets(q) == want


def test_names_must_look_like_streets():
    assert X.parse_streets("Bayard to Mott, finish near Canal", strict=True) == ("segment", "Bayard", "Mott")
    assert X.parse_streets("Duffy Square and the red steps", strict=True) is None
    assert X.parse_streets("Evelyn to Madison Square Park", strict=True) is None


@pytest.mark.parametrize("a, b", [
    ("Seventh Avenue", "7th Ave"), ("West 47th Street", "W 47 St"), ("W. 47th St.", "west 47th street"),
    ("Fifth Avenue", "5th Avenue"), ("St. Marks Place", "Saint Marks Pl"), ("Avenue A", "avenue a"),
])
def test_canon_street_folds_spellings(a, b):
    assert X.canon_street(a) == X.canon_street(b)


def test_street_regex_matches_osm_spellings():
    import re
    rx = re.compile(X.street_regex("Seventh Avenue"), re.I)
    assert rx.match("7th Avenue") and rx.match("Seventh Avenue") and rx.match("7th Ave")
    assert not rx.match("17th Avenue")
    rx = re.compile(X.street_regex("W 47 St"), re.I)
    assert rx.match("West 47th Street") and not rx.match("East 47th Street")
    assert re.match(X.street_regex("Sixth Avenue"), "Avenue of the Americas", re.I)
    assert re.match(X.street_regex("Mott"), "Mott Street", re.I)
    assert re.match(X.street_regex("St. Marks Place"), "Saint Mark's Place", re.I)


# ------------------------------------------------------------ Overpass lookup

class Thr:
    calls = 0

    def wait(self):
        Thr.calls += 1


def test_shared_node_is_the_corner_and_split_carriageways_average():
    seen = []

    def fetch(ql, ua):
        seen.append(ql)
        return [{"type": "node", "id": 1, "lat": 40.7590, "lon": -73.9840},
                {"type": "node", "id": 2, "lat": 40.7592, "lon": -73.9842},   # same corner, 25 m
                {"type": "node", "id": 9, "lat": 40.7000, "lon": -73.9000}]   # far: ignored
    rec = X.find_corner("Seventh Avenue", "West 47th Street", (40.7, -74.0, 40.8, -73.9),
                        (40.759, -73.984), "ua", Thr(), fetch=fetch)
    assert rec["confidence"] == "intersection" and rec["source"] == "overpass"
    assert rec["lat"] == pytest.approx(40.7591) and rec["osm_nodes"] == ["n1", "n2"]
    assert len(seen) == 1 and "node.na.nb" in seen[0] and "(40.7,-74.0,40.8,-73.9)" in seen[0]


def test_no_shared_node_tries_nearby_nodes_then_misses():
    seen = []

    def fetch(ql, ua):
        seen.append(ql)
        return []
    assert X.find_corner("A Street", "B Street", (0, 0, 1, 1), (0.5, 0.5), "ua", Thr(), fetch=fetch) is None
    assert len(seen) == 2 and "around.nb:15" in seen[1]


# ------------------------------------------------------------ fg-geocode, end to end

HOTEL = (40.7580, -73.9855)       # Times Square
NEAR = [(40.7560, -73.9860), (40.7550, -73.9830), (40.7570, -73.9800)]
CORNER = {"Seventh Avenue|West 47th Street": (40.7592, -73.9846),
          "Hudson Street|Grove Street": (40.7334, -74.0055)}


def _doc():
    places = {"hotel": {"name": "Hotel", "maps_query": "1535 Broadway, New York, NY"},
              "c1": {"name": "Coffee", "maps_query": "Coffee, New York, NY"},
              "c2": {"name": "Library", "maps_query": "Library, New York, NY"},
              "c3": {"name": "Station", "maps_query": "Station, New York, NY"},
              "corner": {"name": "West 47th silhouettes",
                         "maps_query": "Seventh Avenue and West 47th Street, Manhattan, NY"},
              "far": {"name": "Chelsea Market", "maps_query": "75 Ninth Avenue, New York, NY 10011"},
              "seg": {"name": "Hudson Street to Barrow", "maps_query": "Hudson Street to Barrow"},
              "grove": {"name": "Grove and Bedford", "maps_query": "Grove Street and Bedford Street, Manhattan, NY"},
              "lone": {"name": "Pell Street to Mott", "maps_query": "Pell Street to Mott"}}
    for p in places.values():
        p["coords"] = None
    stops = [{"place": p} for p in ("hotel", "c1", "c2", "c3", "corner", "far", "grove", "seg")]
    return {"trip": {"id": "nyc-test", "city": "New York", "currency": {"local": "USD"}},
            "places": places,
            "days": [{"date": "2026-09-21", "stops": stops,
                      "routes": {"w": {"name": "Walk", "stops": [4, 5, 6, 7]}}},
                     {"date": "2026-09-22", "stops": [{"place": "lone"}]}]}


def _nominatim(seen):
    pins = {"1535 Broadway": HOTEL, "Coffee": NEAR[0], "Library": NEAR[1], "Station": NEAR[2],
            "75 Ninth Avenue": (40.9158, -73.8036)}                     # Pelham: 26 km out
    def geocode_query(query, ua, throttle):
        seen.append(query)
        for k, (lat, lng) in pins.items():
            if query.startswith(k):
                return {"lat": lat, "lng": lng, "confidence": "exact", "source": "nominatim"}
        return None
    return geocode_query


def _overpass(seen):
    def find_corner(a, b, bbox, ref, ua, throttle):
        seen.append((a, b, bbox))
        hit = CORNER.get(f"{a}|{b}")
        if hit:
            return {"lat": hit[0], "lng": hit[1], "confidence": "intersection", "source": "overpass"}
        return None
    return find_corner


MANHATTAN_BOX = [40.50, -74.30, 41.00, -73.70]   # bigger than 12 km: cut


def _area(query, ua, throttle):
    return {"lat": 40.78, "lng": -73.97, "bbox": MANHATTAN_BOX} if "Manhattan" in query else None


def run(tmp_path, monkeypatch, *flags, doc=None):
    itin = tmp_path / "itinerary.json"
    itin.write_text(json.dumps(doc or _doc()), encoding="utf-8")
    nom, ovp = [], []
    monkeypatch.setattr(G, "geocode_query", _nominatim(nom))
    monkeypatch.setattr(X, "find_corner", _overpass(ovp))
    monkeypatch.setattr(G, "area_lookup", _area)
    monkeypatch.setattr(sys, "argv", ["fg-geocode", str(itin), "--cache", str(tmp_path / "c.json"),
                                      "--overrides", str(tmp_path / "o.json"), "--sleep", "0", *flags])
    code = G.main()
    return code, json.loads(itin.read_text(encoding="utf-8")), nom, ovp


def test_corner_pinned_where_nominatim_missed(tmp_path, monkeypatch):
    code, doc, nom, ovp = run(tmp_path, monkeypatch, "--soft", "--nearest")
    p = doc["places"]["corner"]
    assert code == 0
    assert p["coords"] == {"lat": 40.7592, "lng": -73.9846}
    assert p["coord_confidence"] == "intersection" and "coord_warning" not in p
    a, b, bbox = next(o for o in ovp if o[0] == "Seventh Avenue")
    s, w, n, e = bbox                     # kept local: around the day's other pins
    assert s < 40.755 < n and w < -73.985 < e and n - s < 0.1


def test_segment_uses_the_cross_street_named_beside_it(tmp_path, monkeypatch):
    _, doc, _, ovp = run(tmp_path, monkeypatch, "--soft", "--nearest")
    # "Hudson Street to Barrow" follows "Grove Street and Bedford Street": start corner Hudson & Grove
    assert ("Hudson Street", "Grove Street") in [(a, b) for a, b, _ in ovp]
    assert doc["places"]["seg"]["coords"] == {"lat": 40.7334, "lng": -74.0055}
    # "Pell Street to Mott" stands alone on its day: skipped, never guessed
    assert doc["places"]["lone"]["coords"] is None
    assert not any(a == "Pell Street" for a, _, _ in ovp)


def test_per_day_plausibility_clears_the_far_pin(tmp_path, monkeypatch):
    _, doc, _, _ = run(tmp_path, monkeypatch, "--soft", "--nearest")
    far = doc["places"]["far"]
    assert far["coords"] is None
    assert far["coord_warning"].startswith("pin cleared as implausible:")
    assert "2026-09-21" in far["coord_warning"]
    assert doc["places"]["hotel"]["coords"] is not None


def test_per_day_needs_three_others_and_passes_a_stop_fine_on_another_day():
    def pl(lat, lng):
        return {"coords": {"lat": lat, "lng": lng}}
    doc = {"places": {"h": pl(*HOTEL), "a": pl(*NEAR[0]), "b": pl(*NEAR[1]), "c": pl(*NEAR[2]), "x": pl(40.9, -73.8),
                      "t1": pl(41.5, -74.0), "t2": pl(41.501, -74.0), "t3": pl(41.502, -74.0)},
           "days": [{"date": "d1", "stops": [{"place": p} for p in ("h", "a", "x")]},       # 2 others: no rule
                    {"date": "d2", "stops": [{"place": p} for p in ("h", "a", "b", "c")]},
                    {"date": "d3", "stops": [{"place": p} for p in ("h", "t1", "t2", "t3")]}]}  # day trip
    assert G.check_plausible_day(doc) == []        # h fails d3 but passes d2


def test_corner_answers_and_misses_are_cached(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, "--soft", "--nearest")
    cache = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    hit = cache["intersection: 7th avenue & west 47th street | Manhattan, NY"]
    assert hit["confidence"] == "intersection"
    assert "miss" in cache["intersection: bedford street & grove street | Manhattan, NY"]
    # second run: nothing asked again, the corner comes from the cache
    _, doc, nom, ovp = run(tmp_path, monkeypatch, "--soft", "--nearest")
    assert ovp == [] and doc["places"]["corner"]["coord_confidence"] == "intersection"


def test_expired_miss_is_asked_again(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, "--soft", "--nearest")
    c = tmp_path / "c.json"
    cache = json.loads(c.read_text(encoding="utf-8"))
    cache["intersection: bedford street & grove street | Manhattan, NY"] = {"miss": "2020-01-01"}
    c.write_text(json.dumps(cache), encoding="utf-8")
    _, _, _, ovp = run(tmp_path, monkeypatch, "--soft", "--nearest")
    assert {(a, b) for a, b, _ in ovp} == {("Grove Street", "Bedford Street")}


def test_a_miss_near_the_day_gets_a_second_look_in_the_locality(tmp_path, monkeypatch):
    _, _, _, ovp = run(tmp_path, monkeypatch, "--soft", "--nearest")
    boxes = [bbox for a, b, bbox in ovp if a == "Grove Street"]
    assert len(boxes) == 2 and boxes[0][2] - boxes[0][0] < 0.1     # the day's box first
    s, w, n, e = boxes[1]                                         # then Manhattan, cut to 12 km
    assert s > MANHATTAN_BOX[0] and n < MANHATTAN_BOX[2] and w > MANHATTAN_BOX[1]


def test_day_box_ignores_an_airport_on_the_same_day():
    pin = lambda lat, lng: {"coords": {"lat": lat, "lng": lng}}  # noqa: E731
    doc = {"places": {"jfk": pin(40.6413, -73.7781), "hotel": pin(40.7440, -73.9869),
                      "cafe": pin(40.7450, -73.9880), "c": {"coords": None}},
           "days": [{"stops": [{"place": p} for p in ("jfk", "hotel", "cafe", "c")]}]}
    (s, w, n, e), ref = G.day_box(doc, "c")
    assert s < 40.7413 < n and w < -73.9895 < e       # Broadway & 23rd is inside
    assert s > 40.70                                   # JFK is not


def test_a_street_hit_for_a_two_street_query_is_replaced_by_the_corner(tmp_path, monkeypatch):
    doc = _doc()
    doc["places"]["corner"]["maps_query"] = "Seventh Avenue and West 47th Street, Manhattan, NY"
    (tmp_path / "c.json").write_text(json.dumps({
        "Seventh Avenue and West 47th Street, Manhattan, NY":
            {"lat": 35.98, "lng": -83.93, "confidence": "street", "source": "nominatim"}}), encoding="utf-8")
    _, out, _, _ = run(tmp_path, monkeypatch, "--soft", "--nearest", doc=doc)
    assert out["places"]["corner"]["coord_confidence"] == "intersection"


def test_overpass_trouble_is_soft_and_not_cached(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise X.OverpassError("Overpass HTTP 504")
    itin = tmp_path / "itinerary.json"
    itin.write_text(json.dumps(_doc()), encoding="utf-8")
    monkeypatch.setattr(G, "geocode_query", _nominatim([]))
    monkeypatch.setattr(X, "find_corner", boom)
    monkeypatch.setattr(G, "area_lookup", _area)
    monkeypatch.setattr(sys, "argv", ["fg-geocode", str(itin), "--cache", str(tmp_path / "c.json"),
                                      "--overrides", str(tmp_path / "o.json"), "--sleep", "0",
                                      "--soft", "--nearest"])
    assert G.main() == 0
    cache = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert not any(k.startswith("intersection:") for k in cache)


def test_no_corners_flag(tmp_path, monkeypatch):
    _, doc, _, ovp = run(tmp_path, monkeypatch, "--soft", "--nearest", "--no-corners")
    assert ovp == [] and doc["places"]["corner"]["coords"] is None


# ------------------------------------------------------------ fg-walkcheck

def _line(n, step=0.004, lat=40.75, lng=-73.99):
    """n stops walking north, ~450 m apart."""
    return [(f"S{i}", (lat + i * step, lng)) for i in range(n)]


def test_clean_walk_has_no_problems():
    assert W.check_walk(_line(5)) == {"placed": 5, "total": 5, "problems": []}


def test_unplaced_stops_are_counted_and_named():
    chk = W.check_walk(_line(3) + [("Lost corner", None)])
    assert chk["placed"] == 3 and chk["total"] == 4
    assert chk["problems"] == ["1 of 4 stops aren't on the map: Lost corner."]


def test_long_leg():
    stops = [("Leica Store", (40.7403, -74.0065)), ("Chelsea Market", (40.9158, -73.8036))]
    chk = W.check_walk(stops)
    assert "Long leg: Leica Store → Chelsea Market: 25.9 km." in chk["problems"]


def test_zig_zag_names_the_stop_that_causes_it():
    stops = _line(5)
    stops.insert(2, ("Out of order", (40.75 + 4 * 0.004 + 0.010, -73.99)))   # back and forth
    chk = W.check_walk(stops)
    zz = [p for p in chk["problems"] if p.startswith("Zig-zag")]
    assert zz and zz[0].endswith("Removing Out of order fixes it.")


def test_zig_zag_without_a_single_culprit():
    stops = [("A", (40.750, -73.990)), ("B", (40.760, -73.990)), ("C", (40.750, -73.991)),
             ("D", (40.760, -73.991)), ("E", (40.755, -73.990))]
    zz = [p for p in W.check_walk(stops)["problems"] if p.startswith("Zig-zag")]
    assert zz and "Removing" not in zz[0]


def test_loop_is_not_a_zig_zag():
    stops = _line(4) + [("Back home", (40.75, -73.99))]
    assert not any(p.startswith("Zig-zag") for p in W.check_walk(stops)["problems"])


def test_stray_stop():
    stops = _line(4, step=0.002) + [("Faraway", (40.80, -73.99))]
    probs = W.check_walk(stops)["problems"]
    assert any(p.startswith("Faraway is pinned") and "from the rest of the walk" in p for p in probs)


def test_walkcheck_writes_route_check_and_never_fails(tmp_path, capsys):
    doc = {"trip": {"id": "t"},
           "places": {f"p{i}": {"name": f"P{i}", "coords": {"lat": 40.75 + i * 0.004, "lng": -73.99}}
                      for i in range(3)} | {"x": {"name": "X", "coords": None}},
           "days": [{"date": "2026-09-21", "stops": [{"place": p} for p in ("p0", "p1", "p2", "x")],
                     "routes": {"w": {"name": "Morning walk", "stops": [0, 1, 2, 3]}}}]}
    good = tmp_path / "a.json"
    good.write_text(json.dumps(doc), encoding="utf-8")
    bad = tmp_path / "b.json"
    bad.write_text("{not json", encoding="utf-8")
    assert W.main([str(bad), str(good)]) == 0
    out = json.loads(good.read_text(encoding="utf-8"))
    chk = out["days"][0]["routes"]["w"]["check"]
    assert chk["placed"] == 3 and chk["total"] == 4 and len(chk["problems"]) == 1
    log = capsys.readouterr().out
    assert "walkcheck skipped" in log and "Morning walk: 3/4 on the map" in log


def test_overpass_down_stops_asking_after_three_failures(tmp_path, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a[:2])
        raise X.OverpassError("Overpass HTTP 504")
    doc = _doc()
    for i in range(6):     # six more corners on the day
        doc["places"][f"k{i}"] = {"name": f"K{i}", "coords": None,
                                  "maps_query": f"Broadway and West {40 + i}th Street, Manhattan, NY"}
        doc["days"][0]["stops"].append({"place": f"k{i}"})
    itin = tmp_path / "itinerary.json"
    itin.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(G, "geocode_query", _nominatim([]))
    monkeypatch.setattr(X, "find_corner", boom)
    monkeypatch.setattr(G, "area_lookup", _area)
    monkeypatch.setattr(sys, "argv", ["fg-geocode", str(itin), "--cache", str(tmp_path / "c.json"),
                                      "--overrides", str(tmp_path / "o.json"), "--sleep", "0",
                                      "--soft", "--nearest"])
    assert G.main() == 0
    assert len(calls) == G.OVERPASS_GIVE_UP
