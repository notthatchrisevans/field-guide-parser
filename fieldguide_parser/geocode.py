#!/usr/bin/env python3
"""
Fill place coordinates in an itinerary.json using OpenStreetMap Nominatim.

Moved here from field-guide/scripts/geocode.py (2026-10-06) so the planner's
build pins its trips too: `fg-geocode`. Two additions for that build, both
off by default so the app's behaviour is unchanged:
  --nearest judge each pin by its nearest other pin in the trip (within
            --max-km), not the distance to the whole trip's centre. A trip
            over several cities (Japan: Tokyo and Kyoto are 370 km apart)
            would otherwise flag a whole city as implausible.
  --soft    never fail the build over a pin: an unresolved place stays null,
            an implausible pin is CLEARED back to null (a blank beats a wrong
            pin), both are listed, and the exit code is 0. A trip still
            publishes; its map lists those stops as "not on map".
Since 2026-10-06 (walks), two more, on by default:
  corners   a stop Nominatim can't find whose query names two streets
            ("Seventh Avenue and West 47th Street", "Corner of X and Y", or
            a segment "X to Y" next to a stop naming the cross street) is
            pinned at the OSM node the two streets share, via Overpass
            (intersections.py). coord_confidence "intersection". Off with
            --no-corners.
  per day   a pin more than --day-km (8) from the middle of that day's other
            pins (days with at least 3 others) is implausible -- the nearest
            pin anywhere in the trip can't catch "Chelsea Market" landing in
            Pelham, 26 km out, when the whole trip is one city. 0 = off.

Design rules, from CLAUDE.md:

  * Never fabricate. A place that does not resolve keeps coords=null and is
    reported loudly; the run exits non-zero. A plausible-looking wrong pin in a
    navigation app is worse than a blank, because it gets walked to.
  * Geocode once. Results are cached on disk keyed by the Maps query string
    (the same identity the parser uses to collapse places). Re-parsing the docx
    resets coords to null; a rerun here re-fills them from cache with no network.
  * Be a polite Nominatim citizen: >=1 request/second, a real User-Agent, and
    the public endpoint only. No key needed.
  * Street-level pins are flagged, not hidden. When a query with no building
    number resolves to a road, the pin lands at the street midpoint. That is
    recorded as `coord_warning` on the place so it can be eyeballed once, per
    the note about the lodging address having no building number.
  * Precision is not correctness. The confidence tier describes how sharp a hit
    is, not whether it is the right place -- a same-named market 380 km away
    scores `exact`. So every pin is also checked against the trip's own centre
    of mass, and an implausible one is a hard error. See check_plausible().
  * Human corrections outrank the geocoder. scripts/pin_overrides.json holds
    hand-verified coordinates keyed on the same maps_query; they are applied
    before the cache so that re-parsing (which nulls coords) and re-geocoding
    cannot reintroduce a bad pin.

Usage:
    python3 geocode.py trips/istanbul-2026-08/itinerary.json \\
                       trips/tbilisi-2026-08/itinerary.json

    # optional flags
    --cache PATH        shared cache file (default scripts/geocode_cache.json)
    --overrides PATH    hand-verified pins (default scripts/pin_overrides.json)
    --force             re-geocode even places that already have coords
    --sleep SECONDS     min delay between network requests (default 1.1)
    --max N             cap new network geocodes this run (0 = no cap)
    --max-km N          furthest a pin may sit from the trip centre (default 80)
    --user-agent STR    override the User-Agent sent to Nominatim
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import intersections as X

NOMINATIM = "https://nominatim.openstreetmap.org/search"

# A contactable User-Agent is required by the Nominatim usage policy. Override
# with --user-agent to put a real contact address in it.
DEFAULT_UA = "field-guide-geocoder/1.0 (offline travel itinerary; self-hosted)"

# Bias the search to the right country so "Station Square" can't resolve to the
# wrong continent. Derived from the trailing token of the Maps query, which the
# author always ends with the country. Unknown -> no bias (still honest).
COUNTRY_CODES = {
    "türkiye": "tr", "turkiye": "tr", "turkey": "tr",
    "georgia": "ge",
    "scotland": "gb", "england": "gb", "london": "gb",
    "united kingdom": "gb", "uk": "gb",
    "japan": "jp", "france": "fr", "hong kong": "hk", "china": "cn",
    "usa": "us", "united states": "us", "us": "us", "ny": "us", "new york": "us",
    "thailand": "th", "portugal": "pt", "spain": "es", "italy": "it",
    "greece": "gr", "mexico": "mx", "canada": "ca", "germany": "de",
    "netherlands": "nl", "korea": "kr", "south korea": "kr", "taiwan": "tw",
    "vietnam": "vn", "morocco": "ma", "armenia": "am", "azerbaijan": "az",
}

# Confidence tiers written onto each place:
#   exact  -- a point/building; trust it
#   street -- resolved to a road; pin is the street midpoint, not a door
#   area   -- resolved to a district/neighbourhood/aerodrome; pin is a centroid
# Only street/area carry a human coord_warning, so "verify once" stays a short
# list, not every pin.
STREET_TYPES = {"road", "residential", "street", "living_street", "pedestrian",
                "footway", "path", "service", "track", "unclassified",
                "tertiary", "secondary", "primary", "trunk"}
AREA_TYPES = {"neighbourhood", "suburb", "quarter", "city_district", "district",
              "region", "county", "city", "town", "village", "hamlet",
              "locality", "aerodrome", "administrative"}

# Terminal/station/market qualifiers the author appends to a venue name
# ("Istanbul Airport International Arrivals", "Marjanishvili Metro Station",
# "Tarlabaşı Pazarı"). Nominatim has no POI for the hall/stop/market under that
# full string, only the base venue/place. When the precise and address passes
# fail, retry with the qualifier stripped. Longest first so multi-word
# qualifiers win over their suffixes. This is an explicit list, not a catch-all.
VENUE_QUALIFIERS = [
    "international arrivals", "international departures", "arrivals hall",
    "departures hall", "international terminal", "domestic terminal",
    "arrivals", "departures",
    "metrobus station", "metro station", "metro istasyonu", "metrobüs durağı",
    "tramvay durağı", "tram station", "metrobus", "metro", "station",
    "bit pazarı", "köy pazarı", "salı pazarı", "tarihi salı pazarı",
    "pazar yeri", "central market", "pazarı", "market",
]

# Street-type words that mark an address component ("114 Akaki Tsereteli Avenue",
# "Serdab Sokak No 34"). Used to find the author's own address to geocode when
# the venue name itself isn't in OSM.
STREET_WORDS = ("avenue", "street", "road", "caddesi", "cadde", "sokak",
                "sokağı", "sokagi", "bulvarı", "bulvari", "bulvar", "yolu",
                "meydanı", "prospect", "prospekt")


class GeocodeError(RuntimeError):
    pass


# A trip whose queries end in a city ("Haneda Airport, Tokyo") still gets a
# country bias, from its currency. EUR names no single country: no bias.
CURRENCY_COUNTRY = {"JPY": "jp", "USD": "us", "HKD": "hk", "GBP": "gb", "TRY": "tr",
                    "GEL": "ge", "THB": "th", "CAD": "ca", "MXN": "mx", "KRW": "kr",
                    "TWD": "tw", "VND": "vn", "MAD": "ma", "AMD": "am", "AZN": "az"}
TRIP_COUNTRY: str | None = None      # set per itinerary in main()


def country_code(query: str) -> str | None:
    tail = query.rsplit(",", 1)[-1].strip().lower()
    if tail in COUNTRY_CODES:
        return COUNTRY_CODES[tail]
    if re.fullmatch(r"[a-z]{2} \d{5}(-\d{4})?", tail) and TRIP_COUNTRY == "us":
        return "us"                                       # "NY 10001"
    return TRIP_COUNTRY


def query_candidates(query: str) -> list[str]:
    """
    Nominatim's free-form geocoder rejects the author's full Google-Maps query
    ("Name, Sub-locality, District, City, Country") -- the intermediate admin
    tokens over-constrain it to nothing. Relax progressively, but ALWAYS keep
    the first component (the actual place the author linked) so identity is
    preserved. Dropping "District, City" is disambiguation, not a different
    place; the country bias and the street-level warning keep it honest.
    """
    parts = [p.strip() for p in query.split(",") if p.strip()]
    cands = [query]
    if len(parts) >= 4:
        cands.append(", ".join([parts[0], parts[-2], parts[-1]]))  # name+city+country
    if len(parts) >= 3:
        cands.append(", ".join([parts[0], parts[-1]]))             # name+country
    if len(parts) >= 2:
        cands.append(parts[0])                                     # name only
    seen: set[str] = set()
    out = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def nominatim_once(q: str, cc: str | None, ua: str) -> dict | None:
    """One Nominatim request. Returns the top hit dict, or None if no match."""
    params = {"q": q, "format": "jsonv2", "limit": "1", "addressdetails": "1"}
    if cc:
        params["countrycodes"] = cc
    url = f"{NOMINATIM}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "application/json"})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            return data[0] if data else None
        except urllib.error.HTTPError as e:
            # 429/403 mean slow down / go away. Do not hammer -- stop cleanly.
            if e.code in (429, 403):
                raise GeocodeError(
                    f"Nominatim returned HTTP {e.code} (rate limited / blocked). "
                    f"Slow down or set a contact User-Agent."
                ) from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
        time.sleep(2 * (attempt + 1))
    raise GeocodeError(f"network error for {q!r}: {last_err}")


class Throttle:
    """Enforce >= `sleep` seconds between network calls; count them."""
    def __init__(self, sleep: float):
        self.sleep = sleep
        self.last = 0.0
        self.calls = 0

    def wait(self) -> None:
        gap = self.sleep - (time.monotonic() - self.last)
        if gap > 0:
            time.sleep(gap)
        self.last = time.monotonic()
        self.calls += 1


def strip_qualifier(name: str) -> tuple[str | None, str | None]:
    """
    'Istanbul Airport International Arrivals' -> ('Istanbul Airport', 'international arrivals')
    'Gayrettepe Metro Station M11'           -> ('Gayrettepe', 'metro station')
    Drops a transit line code (M11, T5) anywhere first, then a trailing qualifier.
    """
    n = re.sub(r"\b[MT]\d+\b", " ", name)          # transit line codes
    n = re.sub(r"\s{2,}", " ", n).strip()
    low = n.lower()
    for q in VENUE_QUALIFIERS:
        if low.endswith(q):
            base = n[: len(n) - len(q)].rstrip(" ,-").strip()
            if base:
                return base, q
    if n != name and n:                            # only a line code was dropped
        return n, "line code"
    return None, None


def find_address(query: str) -> str | None:
    """The author's own street-address component ('114 Akaki Tsereteli Avenue')."""
    parts = [p.strip() for p in query.split(",") if p.strip()]
    for c in parts[1:]:                            # never the venue name at [0]
        low = c.lower()
        if any(w in low for w in STREET_WORDS) and any(ch.isdigit() for ch in c):
            return c
    return None


def geocode_query(query: str, ua: str, throttle: "Throttle") -> dict | None:
    """Three passes, most precise first; first hit wins."""
    cc = country_code(query)
    parts = [p.strip() for p in query.split(",") if p.strip()]
    city = parts[-2] if len(parts) >= 2 else None
    country = parts[-1] if parts else None

    def attempt(cands, via=None):
        for cand in dict.fromkeys(c for c in cands if c):
            throttle.wait()
            res = nominatim_once(cand, cc, ua)
            if res:
                rec = result_to_record(res)
                rec["query_used"] = cand
                if via:
                    rec["matched_via"] = via
                return rec
        return None

    # Pass 1: precise. Relax admin context only; the venue name is untouched.
    rec = attempt(query_candidates(query))
    if rec:
        return rec

    # Pass 2: the author's own street address, when the venue name isn't in OSM.
    # This is the author's data, not our guess -- building precision.
    addr = find_address(query)
    if addr:
        rec = attempt([f"{addr}, {city}, {country}", f"{addr}, {country}", addr],
                      via="address")
        if rec:
            return rec

    # Pass 3: strip a terminal/station/market qualifier and retry the ladder.
    base, qual = strip_qualifier(parts[0])
    if base:
        reduced = ", ".join([base] + parts[1:])
        rec = attempt(query_candidates(reduced), via=f"dropped {qual!r}")
        if rec:
            return rec
    return None


def classify(res: dict) -> str:
    """Bucket a Nominatim hit as exact / street / area from its OSM class/type."""
    klass = (res.get("category") or res.get("class") or "").lower()
    typ = (res.get("type") or "").lower()
    addrtype = (res.get("addresstype") or "").lower()
    if klass == "highway" or typ in STREET_TYPES or addrtype in STREET_TYPES:
        return "street"
    if klass == "boundary" or typ in AREA_TYPES or addrtype in AREA_TYPES:
        return "area"
    return "exact"


def result_to_record(res: dict) -> dict:
    """Reduce a Nominatim hit to what we store, with a confidence tier."""
    try:
        lat = round(float(res["lat"]), 6)
        lon = round(float(res["lon"]), 6)
    except (KeyError, ValueError) as e:
        raise GeocodeError(f"result missing usable lat/lon: {res!r}") from e
    return {"lat": lat, "lng": lon, "confidence": classify(res),
            "source": "nominatim", "display_name": res.get("display_name"),
            "osm": f"{res.get('osm_type','')[:1]}{res.get('osm_id','')}" or None}


def load_cache(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


CONFIDENCE_WARNING = {
    "street": "pin is a street midpoint (no building number) -- verify once",
    "area": "pin is an area/venue centroid, not the exact spot -- verify once",
}


def apply_record(place: dict, rec: dict) -> None:
    """Write coords + confidence (and a warning for non-exact) onto a place."""
    place["coords"] = {"lat": rec["lat"], "lng": rec["lng"]}
    conf = rec.get("confidence", "exact")
    place["coord_confidence"] = conf
    # An override may carry its own warning even at `exact`; otherwise the
    # warning follows from the tier.
    warn = rec.get("warning") or CONFIDENCE_WARNING.get(conf)
    if warn:
        place["coord_warning"] = warn
    else:
        place.pop("coord_warning", None)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between (lat, lng) pairs."""
    r = 6371.0088
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def check_plausible(places: dict, max_km: float) -> list[tuple[str, str]]:
    """
    Catch pins that are precise but in the wrong place.

    The confidence tier grades how sharp a hit is, which says nothing about
    whether it is the right one: Nominatim scored a market in Cankiri `exact`
    for an Istanbul query, and country bias could not help because it is the
    same country. So compare every pin against the trip's own centre of mass.

    The centre is the MEDIAN of the resolved pins, not the mean, so a handful
    of wild outliers cannot drag the reference point out to meet them.

    This deliberately does not try to catch a pin that is wrong but nearby --
    the swapped Bosphorus ferry piers sat 3 km apart, well inside any sane
    radius, and no distance rule would have found them. That class needs eyes.
    """
    pts = {pid: (p["coords"]["lat"], p["coords"]["lng"])
           for pid, p in places.items() if p.get("coords")}
    if len(pts) < 3:                       # too few to establish a centre
        return []
    centre = (statistics.median(lat for lat, _ in pts.values()),
              statistics.median(lng for _, lng in pts.values()))
    bad = []
    for pid, pt in sorted(pts.items()):
        km = haversine_km(centre, pt)
        if km > max_km:
            bad.append((pid, f"{km:,.0f} km from the trip centre "
                             f"({centre[0]:.4f}, {centre[1]:.4f}) -- "
                             f"pin is {pt[0]:.6f}, {pt[1]:.6f}"))
    return bad


def check_plausible_nearest(doc: dict, max_km: float) -> list[tuple[str, str]]:
    """A pin is implausible when every other pin of the trip is more than
    max_km away (trips with at least 3 pins). A trip over several cities
    keeps each city's stops near each other, so each passes; a lone
    departure stop on a travel day is near that city's other stops; a
    geocoder hit in the wrong town sits alone and fails."""
    pts = {pid: (p["coords"]["lat"], p["coords"]["lng"])
           for pid, p in doc.get("places", {}).items() if p.get("coords")}
    if len(pts) < 3:
        return []
    bad = []
    for pid, pt in sorted(pts.items()):
        near = min(haversine_km(pt, q) for o, q in pts.items() if o != pid)
        if near > max_km:
            bad.append((pid, f"{near:,.0f} km from the trip's nearest other stop "
                             f"-- pin is {pt[0]:.6f}, {pt[1]:.6f}"))
    return bad


DAY_KM = 8.0


def _pin(place: dict) -> tuple[float, float] | None:
    c = place.get("coords")
    return (c["lat"], c["lng"]) if c else None


def _median_pt(pts) -> tuple[float, float]:
    pts = list(pts)
    return (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))


def _day_pids(day: dict, places: dict) -> list[str]:
    return list(dict.fromkeys(s.get("place") for s in day.get("stops", [])
                              if s.get("place") in places))


def check_plausible_day(doc: dict, max_km: float = DAY_KM, min_others: int = 3
                        ) -> list[tuple[str, str]]:
    """A pin is implausible when it sits more than max_km from the median of
    its DAY's other pins (days with at least min_others of them) -- on every
    day it appears where the rule applies. A hotel on a day trip still
    passes on its other days; a market geocoded into the next county, on a
    day of walking one neighbourhood, fails."""
    places = doc.get("places", {})
    verdicts: dict[str, list] = {}
    for day in doc.get("days", []):
        pts = {p: _pin(places[p]) for p in _day_pids(day, places) if _pin(places[p])}
        for pid, pt in pts.items():
            others = [q for o, q in pts.items() if o != pid]
            if len(others) < min_others:
                continue
            med = _median_pt(others)
            km = haversine_km(med, pt)
            verdicts.setdefault(pid, []).append((km <= max_km, km, day.get("date"), med, pt))
    bad = []
    for pid, vs in sorted(verdicts.items()):
        if not any(v[0] for v in vs):
            _, km, date, med, pt = min(vs, key=lambda v: v[1])
            bad.append((pid, f"{km:,.1f} km from the middle of {date}'s other stops "
                             f"({med[0]:.4f}, {med[1]:.4f}) -- pin is "
                             f"{pt[0]:.6f}, {pt[1]:.6f}"))
    return bad


# ------------------------------------------------------------ street corners

def _walk_neighbours(doc: dict) -> dict[str, list[str]]:
    """pid -> the places just before and after it, in its walk's order when
    the stop is on a walk, else in the day's order."""
    places = doc.get("places", {})
    out: dict[str, list[str]] = {}
    for day in doc.get("days", []):
        stops = day.get("stops", [])
        routes = day.get("routes") or {}
        for i, stop in enumerate(stops):
            pid = stop.get("place")
            if pid not in places:
                continue
            r = routes.get(stop.get("route")) if isinstance(routes, dict) else None
            seq = [j for j in (r or {}).get("stops", [])
                   if isinstance(j, int) and 0 <= j < len(stops)]
            if i not in seq:
                seq = list(range(len(stops)))
            k = seq.index(i)
            for j in (seq[k - 1] if k > 0 else None, seq[k + 1] if k + 1 < len(seq) else None):
                if j is not None and stops[j].get("place") in places:
                    out.setdefault(pid, []).append(stops[j]["place"])
    return out


def _streets_named(place: dict) -> list[str]:
    found = []
    for parsed in (X.parse_streets(place.get("maps_query") or ""),
                   X.parse_streets(place.get("name") or "", strict=True)):
        if parsed:
            found += [parsed[1], parsed[2]]
    return found


# Overpass is a shared volunteer service and is sometimes overloaded (HTTP
# 504). After this many failed lookups in a row the run stops asking -- a
# build never waits half an hour on it; the next build picks up the rest.
OVERPASS_GIVE_UP = 3


class _Run:
    """What one fg-geocode run shares: the cache, the budget, notes."""
    def __init__(self, args, cache: dict, throttle: "Throttle"):
        self.args, self.cache, self.throttle = args, cache, throttle
        self.new = 0
        self.capped = False
        self.notes: list[str] = []
        self.overpass_fails = 0          # in a row; OVERPASS_GIVE_UP stops asking

    def budget_left(self) -> bool:
        if self.args.max_new and self.new >= self.args.max_new:
            self.capped = True
            return False
        return True


def area_lookup(query: str, ua: str, throttle: "Throttle") -> dict | None:
    """A locality's centre and its own bounding box from Nominatim, as
    {lat, lng, bbox: [s, w, n, e]}, or None."""
    throttle.wait()
    res = nominatim_once(query, country_code(query), ua)
    if not res:
        return None
    rec = {"lat": round(float(res["lat"]), 6), "lng": round(float(res["lon"]), 6)}
    bb = res.get("boundingbox")
    if bb and len(bb) == 4:
        s, n, w, e = (float(v) for v in bb)
        rec["bbox"] = [round(s, 5), round(w, 5), round(n, 5), round(e, 5)]
    return rec


AREA_MAX_HALF_KM = 12.0


def _area_box(query: str, run: _Run):
    """(bbox, centre) of a locality ('Manhattan, NY'), cached as 'area: ...'
    in the geocode cache. A sprawling area is cut to 12 km around its
    centre, so the Overpass query stays small. None when unknown."""
    key = f"area: {query}"
    rec = run.cache.get(key)
    if rec and rec.get("miss"):
        if not miss_expired(rec["miss"]):
            return None
        rec = None
    if rec is None:
        if not run.budget_left():
            return None
        rec = area_lookup(query, run.args.user_agent, run.throttle)
        run.new += 1
        run.cache[key] = rec or {"miss": time.strftime("%Y-%m-%d")}
        save_json(run.args.cache, run.cache)
    if not rec or "lat" not in rec:
        return None
    c = (rec["lat"], rec["lng"])
    cap = _km_box(c, AREA_MAX_HALF_KM)
    bb = rec.get("bbox") or list(_km_box(c, 6.0))
    box = (max(bb[0], cap[0]), max(bb[1], cap[1]), min(bb[2], cap[2]), min(bb[3], cap[3]))
    return box, c


def _km_box(centre: tuple[float, float], half_km: float):
    dlat = half_km / 111.0
    dlng = half_km / (111.0 * max(0.2, math.cos(math.radians(centre[0]))))
    return (centre[0] - dlat, centre[1] - dlng, centre[0] + dlat, centre[1] + dlng)


def day_box(doc: dict, pid: str):
    """(bbox, reference point) around the other pins of the day(s) the place
    is on, or None. The box is built round the densest cluster -- the pin
    with the most others within 3 km, and whatever is within 8 km of it --
    so an airport or a wrong pin on the same day can't drag it away."""
    places = doc.get("places", {})
    pts = []
    for day in doc.get("days", []):
        pids = _day_pids(day, places)
        if pid in pids:
            pts += [_pin(places[o]) for o in pids if o != pid and _pin(places[o])]
    if not pts:
        return None
    med = _median_pt(pts)
    anchor = max(pts, key=lambda p: (sum(haversine_km(p, q) <= 3 for q in pts),
                                     -haversine_km(p, med)))
    pts = [p for p in pts if haversine_km(anchor, p) <= 8]
    s, w, _, _ = _km_box((min(p[0] for p in pts), min(p[1] for p in pts)), 1.5)
    _, _, n, e = _km_box((max(p[0] for p in pts), max(p[1] for p in pts)), 1.5)
    return (s, w, n, e), _median_pt(pts)


def search_boxes(doc: dict, pid: str, run: _Run) -> list:
    """Where to look for a corner, in order: around the day's pins, then the
    query's own locality (or the trip's city). A corner missed in the first
    gets a second chance in the second before a miss is remembered."""
    out = []
    near = day_box(doc, pid)
    if near:
        out.append(near)
    trip = doc.get("trip") or {}
    for q in (X.locality(doc["places"][pid].get("maps_query") or ""), trip.get("city")):
        area = _area_box(q, run) if q else None
        if area:
            out.append((area[0], near[1] if near else area[1]))
            break
    return out


def pin_corners(doc: dict, overrides: dict, run: _Run) -> int:
    """Pin the street corners Nominatim couldn't. Returns how many pinned."""
    places = doc.get("places", {})
    trip = doc.get("trip") or {}
    neighbours = _walk_neighbours(doc)
    pinned = 0
    for pid, place in places.items():
        query = place.get("maps_query") or ""
        if query in overrides:
            continue
        from_query = X.parse_streets(query)
        # A Nominatim hit for a two-street query is one of the streets (or a
        # same-named street in another state), never the corner: replace it.
        if place.get("coords") and not (
                from_query and from_query[0] == "pair"
                and place.get("coord_confidence") in ("street", "area")):
            continue
        parsed = from_query or X.parse_streets(place.get("name") or "", strict=True)
        if not parsed:
            continue
        kind, a, b = parsed
        if kind == "pair":
            pairs = [(a, b)]
        else:
            skip = {X.canon_street(a), X.canon_street(b)}
            cross: list[str] = []
            for nb in neighbours.get(pid, []):
                for z in _streets_named(places[nb]):
                    if X.canon_street(z) not in skip:
                        skip.add(X.canon_street(z))
                        cross.append(z)
            pairs = [(a, z) for z in cross]
            if not pairs:
                run.notes.append(f"{pid}: segment {a!r} to {b!r} -- no stop beside it "
                                 f"names the cross street; skipped")
                continue
        where = X.locality(query) or trip.get("city") or ""
        boxes = None
        for x, y in pairs:
            key = X.cache_key(x, y, where)
            rec = run.cache.get(key)
            if rec and rec.get("miss"):
                if not miss_expired(rec["miss"]):
                    continue
                rec = None
            if rec is None:
                if not run.budget_left() or run.overpass_fails >= OVERPASS_GIVE_UP:
                    break
                if boxes is None:
                    boxes = search_boxes(doc, pid, run)
                if not boxes:
                    run.notes.append(f"{pid}: no area to search for {x!r} & {y!r}")
                    break
                try:
                    for bbox, ref in boxes:
                        rec = X.find_corner(x, y, bbox, ref, run.args.user_agent, run.throttle)
                        if rec:
                            break
                except X.OverpassError as e:
                    run.overpass_fails += 1
                    run.notes.append(f"{pid}: {e} -- not cached; next build retries")
                    if run.overpass_fails == OVERPASS_GIVE_UP:
                        run.notes.append(f"Overpass failed {OVERPASS_GIVE_UP} times in a row: "
                                         "no more corner lookups this run")
                    break
                run.overpass_fails = 0
                run.new += 1
                run.cache[key] = rec or {"miss": time.strftime("%Y-%m-%d")}
                save_json(run.args.cache, run.cache)
            if rec and "lat" in rec:
                apply_record(place, rec)
                pinned += 1
                break
    return pinned


MISS_DAYS = 30


def miss_expired(day: str) -> bool:
    try:
        return time.time() - time.mktime(time.strptime(day, "%Y-%m-%d")) > MISS_DAYS * 86400
    except ValueError:
        return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("itineraries", nargs="+", help="itinerary.json file(s)")
    ap.add_argument("--cache", default=os.path.join(
        os.getcwd(), "geocode_cache.json"))
    ap.add_argument("--overrides", default=os.path.join(
        os.getcwd(), "pin_overrides.json"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.1)
    ap.add_argument("--max", type=int, default=0, dest="max_new")
    ap.add_argument("--max-km", type=float, default=80.0, dest="max_km")
    ap.add_argument("--day-km", type=float, default=DAY_KM, dest="day_km",
                    help="furthest a pin may sit from the middle of its day's other pins (0 = off)")
    ap.add_argument("--no-corners", action="store_true",
                    help="don't pin street corners through Overpass")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--nearest", action="store_true",
                    help="judge each pin by its nearest other pin, for trips over several cities")
    ap.add_argument("--soft", action="store_true",
                    help="never fail: clear implausible pins, list unresolved, exit 0")
    args = ap.parse_args()

    cache = load_cache(args.cache)
    overrides = {k: v for k, v in load_cache(args.overrides).items()
                 if not k.startswith("_")}
    throttle = Throttle(args.sleep)
    run = _Run(args, cache, throttle)
    resolved = warned = from_cache = overridden = corners = 0
    unresolved: list[tuple[str, str]] = []
    implausible: list[tuple[str, str]] = []

    for itin_path in args.itineraries:
        with open(itin_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        global TRIP_COUNTRY
        cur = (doc.get("trip") or {}).get("currency") or {}
        TRIP_COUNTRY = CURRENCY_COUNTRY.get(str(cur.get("local") or "").upper())
        places = doc.get("places", {})
        changed = False
        missing: list[tuple[str, str]] = []

        for pid, place in places.items():
            query = place.get("maps_query")
            if not query:
                missing.append((pid, "no maps_query on place"))
                continue

            # A hand-verified pin wins over anything the geocoder found, and
            # is applied even to a place that already has coords -- that is the
            # whole point, since the bad value is what is sitting there.
            if query in overrides:
                apply_record(place, overrides[query])
                changed = True
                resolved += 1
                overridden += 1
                if place.get("coord_warning"):
                    warned += 1
                continue

            if place.get("coords") is not None and not args.force:
                continue

            rec = cache.get(query)
            # A miss is remembered too, for MISS_DAYS: a rebuild doesn't ask
            # OpenStreetMap the same unanswerable question every time.
            if rec is not None and rec.get("miss"):
                if not miss_expired(rec["miss"]):
                    missing.append((pid, f"no match for {query!r} (remembered miss)"))
                    continue
                rec = None
            if rec is None:
                if not run.budget_left():
                    continue
                rec = geocode_query(query, args.user_agent, throttle)
                run.new += 1
                if rec is None:
                    missing.append((pid, f"no match for {query!r}"))
                    cache[query] = {"miss": time.strftime("%Y-%m-%d")}
                    save_json(args.cache, cache)
                    continue
                cache[query] = rec
                save_json(args.cache, cache)  # persist immediately; resumable
            else:
                from_cache += 1

            apply_record(place, rec)
            changed = True
            resolved += 1
            if rec.get("confidence", "exact") != "exact":
                warned += 1

        # Street corners: what Nominatim couldn't place (or placed on one of
        # the two streets), pinned where the two streets meet.
        if not args.no_corners:
            n = pin_corners(doc, overrides, run)
            corners += n
            changed = changed or n > 0
        unresolved += [(pid, why) for pid, why in missing if not places[pid].get("coords")]

        if changed:
            save_json(itin_path, doc)
            print(f"  wrote {itin_path}")

        # Validate after writing: successes are still saved, the same way an
        # unresolved place does not discard the pins that did resolve.
        bad = (check_plausible_nearest(doc, args.max_km) if args.nearest
               else check_plausible(places, args.max_km))
        if args.day_km:
            seen = {pid for pid, _ in bad}
            bad += [b for b in check_plausible_day(doc, args.day_km) if b[0] not in seen]
        for pid, why in bad:
            implausible.append((f"{doc['trip']['id']}/{pid}", why))
            if args.soft:                      # a blank beats a wrong pin
                places[pid]["coords"] = None
                places[pid]["coord_warning"] = f"pin cleared as implausible: {why}"
        if args.soft and bad:
            save_json(itin_path, doc)

    print(f"\nOK  {resolved + corners} resolved "
          f"({from_cache} from cache, {run.new} new lookups via "
          f"{throttle.calls} requests, {overridden} hand-verified, "
          f"{corners} street corners), {warned} flagged for review")
    if run.capped:
        print(f"    --max {args.max_new} reached; rerun to finish the rest")
    for note in run.notes:
        print(f"    corner: {note}")

    if implausible:
        print(f"\n  {len(implausible)} IMPLAUSIBLE pin(s) -- precise, but not "
              f"where this trip is:", file=sys.stderr)
        for pid, why in implausible:
            print(f"    [{pid}] {why}", file=sys.stderr)
        print("    Fix by adding a hand-verified entry to "
              f"{os.path.basename(args.overrides)}.", file=sys.stderr)

    if unresolved:
        print(f"\n  {len(unresolved)} UNRESOLVED place(s) -- coords left null:",
              file=sys.stderr)
        for pid, why in unresolved:
            print(f"    [{pid}] {why}", file=sys.stderr)
        # Not a catch-all guess: better a blank than a wrong pin. Loud exit so
        # the pipeline notices, but successes above are already written.
        return 0 if args.soft else 1
    return 0 if args.soft or not implausible else 1


if __name__ == "__main__":
    sys.exit(main())
