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
    new_geocodes = 0
    resolved = warned = from_cache = overridden = 0
    unresolved: list[tuple[str, str]] = []
    implausible: list[tuple[str, str]] = []
    capped = False

    for itin_path in args.itineraries:
        with open(itin_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        global TRIP_COUNTRY
        cur = (doc.get("trip") or {}).get("currency") or {}
        TRIP_COUNTRY = CURRENCY_COUNTRY.get(str(cur.get("local") or "").upper())
        places = doc.get("places", {})
        changed = False

        for pid, place in places.items():
            query = place.get("maps_query")
            if not query:
                unresolved.append((pid, "no maps_query on place"))
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
            if rec is None:
                if args.max_new and new_geocodes >= args.max_new:
                    capped = True
                    continue
                rec = geocode_query(query, args.user_agent, throttle)
                new_geocodes += 1
                if rec is None:
                    unresolved.append((pid, f"no match for {query!r}"))
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

        if changed:
            save_json(itin_path, doc)
            print(f"  wrote {itin_path}")

        # Validate after writing: successes are still saved, the same way an
        # unresolved place does not discard the pins that did resolve.
        bad = (check_plausible_nearest(doc, args.max_km) if args.nearest
               else check_plausible(places, args.max_km))
        for pid, why in bad:
            implausible.append((f"{doc['trip']['id']}/{pid}", why))
            if args.soft:                      # a blank beats a wrong pin
                places[pid]["coords"] = None
                places[pid]["coord_warning"] = f"pin cleared as implausible: {why}"
        if args.soft and bad:
            save_json(itin_path, doc)

    print(f"\nOK  {resolved} resolved "
          f"({from_cache} from cache, {new_geocodes} newly geocoded via "
          f"{throttle.calls} requests, {overridden} hand-verified), "
          f"{warned} flagged for review")
    if capped:
        print(f"    --max {args.max_new} reached; rerun to finish the rest")

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
