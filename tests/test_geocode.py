"""fg-geocode: the planner build's pins. Offline: the geocoder is stubbed.
Run: py -m pytest tests -q"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fieldguide_parser import geocode as G  # noqa: E402

TOKYO = (35.68, 139.76)
KYOTO = (35.01, 135.77)
WRONG = (43.06, 141.35)          # Sapporo: a same-named hit in the wrong town


def _doc(currency="JPY"):
    places, stops = {}, []
    def add(pid, q):
        places[pid] = {"name": pid, "maps_query": q, "coords": None}
    for i in range(3):
        add(f"t{i}", f"Tokyo stop {i}, Tokyo")
    for i in range(3):
        add(f"k{i}", f"Kyoto stop {i}, Kyoto")
    add("bad", "Nishiki Market, Kyoto")
    add("none", "Nowhere Place, Kyoto")
    days = [
        {"date": "2026-12-18", "stops": [{"place": p} for p in ("t0", "t1", "t2")]},
        {"date": "2026-12-19", "stops": [{"place": p} for p in ("t2", "k0", "k1")]},   # travel day
        {"date": "2026-12-20", "stops": [{"place": p} for p in ("k0", "k1", "k2", "bad", "none")]},
    ]
    return {"trip": {"id": "japan-test", "currency": {"local": currency, "home": "USD"}},
            "places": places, "days": days}


def _fake(seen):
    def geocode_query(query, ua, throttle):
        seen.append((query, G.country_code(query)))
        if query.startswith("Nowhere"):
            return None
        lat, lng = WRONG if query.startswith("Nishiki") else TOKYO if "Tokyo" in query else KYOTO
        k = int(query.split()[2]) if query.split()[2].isdigit() else 0
        return {"coords": {"lat": lat + k * 0.003, "lng": lng + k * 0.003}, "confidence": "exact"}
    return geocode_query


def run(tmp_path, monkeypatch, *flags, currency="JPY"):
    itin = tmp_path / "itinerary.json"
    itin.write_text(json.dumps(_doc(currency)), encoding="utf-8")
    seen = []
    monkeypatch.setattr(G, "geocode_query", _fake(seen))
    monkeypatch.setattr(G, "apply_record", lambda place, rec: place.update(coords=rec["coords"]))
    monkeypatch.setattr(sys, "argv", ["fg-geocode", str(itin), "--cache", str(tmp_path / "c.json"),
                                      "--overrides", str(tmp_path / "o.json"), "--sleep", "0", *flags])
    code = G.main()
    return code, json.loads(itin.read_text(encoding="utf-8")), seen


def test_soft_nearest_clears_a_wrong_pin_keeps_travel_days_and_never_fails(tmp_path, monkeypatch):
    code, doc, seen = run(tmp_path, monkeypatch, "--soft", "--nearest")
    assert code == 0
    p = doc["places"]
    assert p["bad"]["coords"] is None and "implausible" in p["bad"]["coord_warning"]
    assert p["none"]["coords"] is None
    assert all(p[x]["coords"] for x in ("t0", "t1", "t2", "k0", "k1", "k2"))   # Tokyo↔Kyoto day passes


def test_without_soft_the_old_strict_exit_stands(tmp_path, monkeypatch):
    code, doc, seen = run(tmp_path, monkeypatch, "--nearest")
    assert code == 1 and doc["places"]["bad"]["coords"] is not None     # strict: reported, not cleared


def test_country_bias_from_the_trip_currency(tmp_path, monkeypatch):
    _, _, seen = run(tmp_path, monkeypatch, "--soft", "--nearest")
    assert {cc for _, cc in seen} == {"jp"}
    _, _, seen = run(tmp_path, monkeypatch, "--soft", "--nearest", currency="EUR")
    assert {cc for _, cc in seen} == {None}


def test_us_zip_tail():
    G.TRIP_COUNTRY = "us"
    assert G.country_code("Katz's Delicatessen, 205 E Houston St, New York, NY 10002") == "us"
    assert G.country_code("Somewhere, Japan") == "jp"
    G.TRIP_COUNTRY = None
