#!/usr/bin/env python3
"""
Find pictures for a trip's places and for the trip itself: `fg-pictures`.

Runs in the trips build after `fg-geocode` (specs/field-guide-pictures.md,
piece A). For each place it writes, when found:

    place.website = "https://..."            (OpenStreetMap website tag)
    place.picture = {src, credit, license, source: "wikimedia", page}
    trip.picture  = the same shape            (the city's own Wikidata image)

Design rules:

  * A picture only ever comes from the place's identity: its OpenStreetMap
    object -> that object's Wikidata item -> the item's main image (P18).
    Never a name search on Commons for something that looks right. A wrong
    picture is worse than none, because it gets walked to.
  * The OSM object is the one fg-geocode pinned (its cache records the OSM
    id), checked against the place's current coords. A hand-verified pin
    with no OSM id gets a Nominatim search whose hit must sit within
    MATCH_M of that pin, or nothing.
  * Hand-picked wins: a place that a stop already shows a kept `image:` for
    is left alone.
  * Places to photograph, not to recognise (NO_PICTURE categories) get no
    picture -- unless OSM gives the place a Wikidata item, which makes it a
    named landmark.
  * Ask once. Everything is cached in --cache keyed by OSM / Wikidata /
    Commons identity; a miss is remembered for MISS_DAYS; downloaded files
    are kept under images/<trip>/auto/. A rebuild asks nothing new.
  * Polite: a real User-Agent and >= 1 second between requests, to
    Nominatim and to Wikimedia alike. A 429/403 from a host stops asking that
    host for the rest of the run.
  * Soft: every failure is logged and the run moves on. With --soft the exit
    code is always 0, so a picture never blocks a trip.

Usage (from the trips repo root):
    fg-pictures trips/*/itinerary.json --cache scripts/pictures_cache.json \\
                --geocode-cache scripts/geocode_cache.json --soft
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from .geocode import (CURRENCY_COUNTRY, Throttle, haversine_km, load_cache,
                      save_json)

NOMINATIM_LOOKUP = "https://nominatim.openstreetmap.org/lookup"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

DEFAULT_UA = ("field-guide-pictures/1.0 (offline travel itinerary; "
              "https://github.com/notthatchrisevans/field-guide-parser)")

# Places to photograph, not to recognise: no picture unless OSM gives the
# place a Wikidata item.
NO_PICTURE = {"public", "residential", "working", "waterfront", "decay",
              "transit", "airport", "hotel", "airbnb", "friends"}

# An OSM hit that is a whole town or admin area is not the stop itself
# (a stop that only resolved to "New York" must not get the skyline).
NOT_A_PLACE_TYPES = {"city", "town", "village", "hamlet", "municipality",
                     "county", "state", "region", "province", "country",
                     "administrative", "island", "archipelago"}

MATCH_M = 150          # a searched OSM hit must sit this close to the pin
THUMB_WIDTH = 900
MISS_DAYS = 30
TRIP_SLUG = "_trip"
HUMAN_SETTLEMENT = "Q486972"


class Blocked(RuntimeError):
    """A host said slow down / go away; stop asking it this run."""


class Net:
    """All network traffic: one throttle, a User-Agent, blocked hosts."""

    def __init__(self, ua: str, sleep: float):
        self.ua = ua
        self.throttle = Throttle(sleep)
        self.blocked: set[str] = set()

    def get(self, url: str, params: dict | None = None,
            accept: str = "application/json") -> bytes:
        host = urllib.parse.urlparse(url).netloc
        if host in self.blocked:
            raise Blocked(f"{host} blocked earlier this run")
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                   "Accept": accept})
        self.throttle.wait()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 403):
                self.blocked.add(host)
                raise Blocked(f"{host} returned HTTP {e.code}") from e
            raise

    def json(self, url: str, params: dict | None = None,
             accept: str = "application/json"):
        return json.loads(self.get(url, params, accept).decode("utf-8"))


def today() -> str:
    return time.strftime("%Y-%m-%d")


def expired(day: str | None) -> bool:
    if not day:
        return True
    try:
        return time.time() - time.mktime(time.strptime(day, "%Y-%m-%d")) > MISS_DAYS * 86400
    except ValueError:
        return True


def fresh_miss(rec) -> bool:
    return isinstance(rec, dict) and "miss" in rec and not expired(rec["miss"])


def strip_html(s: str | None) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# --------------------------------------------------------------- OpenStreetMap

# Words that say what kind of thing a place is, not which one: "Grand
# Central Terminal" and "Grand Central exterior" share their name, not
# "terminal". Left out when comparing names, along with filler words.
GENERIC_WORDS = {
    "the", "a", "an", "of", "at", "and", "in", "on", "to", "for", "by", "de",
    "la", "le", "les", "du", "des", "el", "st", "saint",
    "museum", "gallery", "terminal", "station", "airport", "international",
    "hotel", "park", "square", "street", "avenue", "road", "market", "bridge",
    "church", "temple", "shrine", "building", "tower", "center", "centre",
    "exterior", "interior", "start", "end", "visit", "walk", "lunch",
    "dinner", "breakfast", "coffee", "optional",
}


def name_words(name: str | None) -> set[str]:
    """A name's significant words: accents and case folded, punctuation
    gone, filler and kind-of-place words dropped."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("'", "").replace("’", "")
    return {w for w in re.split(r"[^0-9a-z]+", s) if w and w not in GENERIC_WORDS}


# The OSM object's names compared with the place's (namedetails keys).
NAME_KEYS = ("name", "name:en", "short_name", "short_name:en", "alt_name",
             "alt_name:en", "official_name", "official_name:en")


def names_match(a: str | None, b: str | None) -> bool:
    """One name's significant words all appear in the other's."""
    wa, wb = name_words(a), name_words(b)
    return bool(wa and wb) and (wa <= wb or wb <= wa)


def can_picture(category: str, qid: str | None) -> bool:
    """A stored picture needs the place's own Wikidata item, whatever the
    category. A NO_PICTURE place (a street corner, a hotel) has no picture
    of its own -- only an item makes it a named landmark worth one. A
    picturable place without an item is left for the live sources."""
    return bool(qid)


def osm_record(hit: dict) -> dict:
    """What we keep of a Nominatim hit with extratags."""
    tags = hit.get("extratags") or {}
    klass = (hit.get("category") or hit.get("class") or "").lower()
    typ = (hit.get("type") or "").lower()
    whole_area = klass == "boundary" or (klass == "place" and typ in NOT_A_PLACE_TYPES)
    nd = hit.get("namedetails") or {}
    names = [hit.get("name")] + [nd.get(k) for k in NAME_KEYS]
    rec = {"names": [n for n in dict.fromkeys(names) if n],
           "wikidata": None if whole_area else (tags.get("wikidata") or None),
           "website": tags.get("website") or tags.get("contact:website") or None,
           "cc": ((hit.get("address") or {}).get("country_code") or "").lower() or None,
           "checked": today()}
    if rec["wikidata"] and not re.fullmatch(r"Q\d+", rec["wikidata"]):
        rec["wikidata"] = None                 # "Q1;Q2" or junk: no identity
    return rec


def osm_id_of(hit: dict) -> str | None:
    t, i = (hit.get("osm_type") or "")[:1].upper(), hit.get("osm_id")
    return f"{t}{i}" if t and i else None


def lookup_osm(osm_id: str, net: Net) -> dict | None:
    data = net.json(NOMINATIM_LOOKUP, {"osm_ids": osm_id, "format": "jsonv2",
                                       "extratags": "1", "addressdetails": "1",
                                       "namedetails": "1"})
    return data[0] if data else None


def search_osm(query: str, near: tuple[float, float], net: Net) -> dict | None:
    """A hand-verified pin has no OSM id: search the place's own query and
    accept only a hit sitting on the pin."""
    data = net.json(NOMINATIM_SEARCH, {"q": query, "format": "jsonv2", "limit": "5",
                                       "extratags": "1", "addressdetails": "1",
                                       "namedetails": "1"})
    for hit in data or []:
        try:
            pt = (float(hit["lat"]), float(hit["lon"]))
        except (KeyError, ValueError):
            continue
        if haversine_km(near, pt) * 1000 <= MATCH_M:
            return hit
    return None


def place_osm(place: dict, geo: dict, cache: dict, net: Net) -> dict | None:
    """The place's OSM record ({wikidata, website, cc}), or None."""
    coords = place.get("coords")
    query = place.get("maps_query")
    if not coords or not query:
        return None
    pin = (coords["lat"], coords["lng"])
    osm_cache, q_cache = cache.setdefault("osm", {}), cache.setdefault("query", {})

    osm_id = None
    g = geo.get(query) or {}
    if g.get("osm") and "lat" in g and haversine_km(pin, (g["lat"], g["lng"])) * 1000 <= MATCH_M:
        osm_id = g["osm"][:1].upper() + g["osm"][1:]
    else:
        qrec = q_cache.get(query)
        if fresh_miss(qrec):
            return None
        if qrec and qrec.get("osm"):
            osm_id = qrec["osm"]
        else:
            hit = search_osm(query, pin, net)
            if hit is None or not osm_id_of(hit):
                q_cache[query] = {"miss": today()}
                return None
            osm_id = osm_id_of(hit)
            q_cache[query] = {"osm": osm_id}
            osm_cache[osm_id] = osm_record(hit)

    rec = osm_cache.get(osm_id)
    if fresh_miss(rec):
        return None
    # A record with a Wikidata item stays; one without is re-checked after
    # MISS_DAYS (OSM gets edited).
    if (rec is None or "miss" in rec or "names" not in rec
            or (not rec.get("wikidata") and expired(rec.get("checked")))):
        hit = lookup_osm(osm_id, net)
        if hit is None:
            osm_cache[osm_id] = {"miss": today()}
            return None
        rec = osm_cache[osm_id] = osm_record(hit)
    return rec


# ------------------------------------------------------------ Wikidata/Commons

def best_claim(claims: list) -> dict | None:
    ok = [c for c in claims or [] if c.get("rank") != "deprecated"]
    ok.sort(key=lambda c: c.get("rank") != "preferred")
    return ok[0] if ok else None


def item_entity(qid: str, net: Net) -> tuple[str | None, list[str]]:
    """A Wikidata item's main image (P18) and its English label + aliases."""
    data = net.json(WIKIDATA_API, {"action": "wbgetentities", "ids": qid,
                                   "props": "claims|labels|aliases",
                                   "languages": "en", "format": "json"})
    ent = (data.get("entities") or {}).get(qid) or {}
    names = [((ent.get("labels") or {}).get("en") or {}).get("value")]
    names += [a.get("value") for a in (ent.get("aliases") or {}).get("en") or []]
    names = [n for n in names if n]
    claim = best_claim((ent.get("claims") or {}).get("P18"))
    try:
        return claim["mainsnak"]["datavalue"]["value"], names
    except (TypeError, KeyError):
        return None, names


def commons_info(file_name: str, net: Net) -> dict | None:
    """Thumbnail URL, credit, licence and file page of one Commons file."""
    title = file_name if file_name.lower().startswith("file:") else f"File:{file_name}"
    data = net.json(COMMONS_API, {
        "action": "query", "titles": title, "prop": "imageinfo",
        "iiprop": "url|extmetadata", "iiurlwidth": str(THUMB_WIDTH),
        "iiextmetadatafilter": "Artist|Credit|LicenseShortName",
        "format": "json"})
    for page in ((data.get("query") or {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [None])[0]
        if not info:
            continue
        meta = info.get("extmetadata") or {}
        val = lambda k: (meta.get(k) or {}).get("value")
        credit = strip_html(val("Artist")) or strip_html(val("Credit")) or "Wikimedia Commons"
        return {"file": title[5:], "thumb": info.get("thumburl") or info.get("url"),
                "credit": credit, "license": strip_html(val("LicenseShortName")) or None,
                "page": info.get("descriptionurl")}
    return None


def file_picture(file_name: str, cache: dict, net: Net) -> dict | None:
    fc = cache.setdefault("file", {})
    rec = fc.get(file_name)
    if fresh_miss(rec):
        return None
    if rec is None or "miss" in rec:
        rec = commons_info(file_name, net)
        if rec is None:
            fc[file_name] = {"miss": today()}
            return None
        fc[file_name] = rec
    return rec


def item_record(qid: str, cache: dict, net: Net) -> dict | None:
    """{file, names} of a Wikidata item with a main image, or None."""
    wc = cache.setdefault("wikidata", {})
    rec = wc.get(qid)
    if fresh_miss(rec):
        return None
    if rec is None or "miss" in rec or "names" not in rec:
        name, names = item_entity(qid, net)
        if not name:
            wc[qid] = {"miss": today()}
            return None
        rec = wc[qid] = {"file": name, "names": names}
    return rec


def item_picture(qid: str, cache: dict, net: Net, must_match: str | None = None) -> dict | None:
    """The Commons record of a Wikidata item's main image, or None. With
    must_match, the item's English label or an alias must match that name."""
    rec = item_record(qid, cache, net)
    if rec is None:
        return None
    if must_match is not None and not any(names_match(must_match, n) for n in rec["names"]):
        return None
    return file_picture(rec["file"], cache, net)


def city_item(city: str, cc: str, net: Net) -> str | None:
    """The trip city's Wikidata item: a search by name, kept only if it is a
    human settlement (P31/P279*) in the trip's country (P17, or the item is
    that country's own code, as Hong Kong is). First in search order wins."""
    data = net.json(WIKIDATA_API, {"action": "wbsearchentities", "search": city,
                                   "language": "en", "type": "item",
                                   "limit": "10", "format": "json"})
    ids = [r["id"] for r in data.get("search") or [] if re.fullmatch(r"Q\d+", r.get("id", ""))]
    if not ids:
        return None
    code = cc.upper()
    sparql = f"""SELECT DISTINCT ?item WHERE {{
  VALUES ?item {{ {' '.join('wd:' + q for q in ids)} }}
  {{ ?item wdt:P17/wdt:P297 "{code}" }} UNION {{ ?item wdt:P297 "{code}" }}
  ?item wdt:P31/wdt:P279* wd:{HUMAN_SETTLEMENT} .
}}"""
    res = net.json(WIKIDATA_SPARQL, {"query": sparql, "format": "json"},
                   accept="application/sparql-results+json")
    ok = {b["item"]["value"].rsplit("/", 1)[-1]
          for b in (res.get("results") or {}).get("bindings") or []}
    return next((q for q in ids if q in ok), None)


# ------------------------------------------------------------------- the files

def download(rec: dict, trip_id: str, slug: str, images: str, net: Net) -> str | None:
    """Keep the thumbnail at images/<trip>/auto/<slug>.<ext>; return the src
    (repo-relative, the way hand-picked `image:` paths are written)."""
    url = rec.get("thumb")
    if not url:
        return None
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    ext = ".jpg" if ext in ("", ".jpeg") else ext
    if ext not in (".jpg", ".png", ".webp", ".gif"):
        return None
    rel_dir = os.path.join(images, trip_id, "auto")
    path = os.path.join(rel_dir, slug + ext)
    if not os.path.isfile(path):
        data = net.get(url, accept="image/*")
        os.makedirs(rel_dir, exist_ok=True)
        with open(path + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(path + ".tmp", path)
    return "/".join(["images", trip_id, "auto", slug + ext])


def picture_of(rec: dict, src: str) -> dict:
    return {"src": src, "credit": rec.get("credit"), "license": rec.get("license"),
            "source": "wikimedia", "page": rec.get("page")}


def stop_order(doc: dict) -> list[str]:
    """Place ids in the order the trip visits them."""
    seen: dict[str, None] = {}
    for day in doc.get("days") or []:
        for stop in day.get("stops") or []:
            if stop.get("place"):
                seen.setdefault(stop["place"], None)
    return list(seen)


def hand_picked(doc: dict) -> set[str]:
    """Places a stop already shows a kept `image:` for."""
    out = set()
    for day in doc.get("days") or []:
        for stop in day.get("stops") or []:
            if stop.get("place") and any(i.get("src") for i in stop.get("images") or []):
                out.add(stop["place"])
    return out


# ------------------------------------------------------------------------ main

class Run:
    def __init__(self, args):
        self.args = args
        self.net = Net(args.user_agent, args.sleep)
        self.cache = load_cache(args.cache)
        self.geo = load_cache(args.geocode_cache) if args.geocode_cache else {}
        self.failures: list[str] = []
        self.new = 0
        self.capped = False
        self.counts = Counter()

    def save(self):
        save_json(self.args.cache, self.cache)

    def soft(self, what: str, fn, *a):
        """Call fn; on any failure log it and return None."""
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 -- soft by design
            self.failures.append(f"{what}: {type(e).__name__}: {e}")
            return None

    def place(self, trip_id: str, pid: str, place: dict):
        cat = (place.get("category") or "").lower()
        calls = self.net.throttle.calls
        if self.args.max_new and self.new >= self.args.max_new:
            self.capped = True
            return
        osm = self.soft(f"{trip_id}/{pid} OpenStreetMap", place_osm,
                        place, self.geo, self.cache, self.net)
        # The pin can land on a different object than the stop (an address
        # that is also Times Square). Its name must resemble the place's, or
        # neither its website nor its item belongs to this place.
        if osm and not any(names_match(place.get("name"), n) for n in osm.get("names") or []):
            self.counts["name mismatch"] += 1
            osm = None
        if osm and osm.get("website"):
            place["website"] = osm["website"]
            self.counts["website"] += 1
        qid = (osm or {}).get("wikidata")
        if not can_picture(cat, qid):
            # A picturable place is left for the live sources (website,
            # Google); a NO_PICTURE place simply has none.
            self.counts["left for live sources" if cat not in NO_PICTURE
                        else "none by category"] += 1
        else:
            # A NO_PICTURE place (a hotel, a corner) only gets the item's
            # picture when the item is named for it.
            must = (place.get("name") or "") if cat in NO_PICTURE else None
            rec = self.soft(f"{trip_id}/{pid} Wikidata {qid}", item_picture,
                            qid, self.cache, self.net, must)
            src = rec and self.soft(f"{trip_id}/{pid} download", download,
                                    rec, trip_id, pid, self.args.images, self.net)
            if src:
                place["picture"] = picture_of(rec, src)
                self.counts["picture"] += 1
            else:
                self.counts["wikidata, no picture"] += 1
        if self.net.throttle.calls > calls:
            self.new += 1
        self.save()                               # resumable

    def trip(self, doc: dict):
        trip = doc.get("trip") or {}
        tid = trip.get("id") or "trip"
        places = doc.get("places") or {}
        pic = None
        override = (trip.get("picture_file") or "").strip()
        if override:
            rec = self.soft(f"{tid} trip picture {override!r}", file_picture,
                            override, self.cache, self.net)
            src = rec and self.soft(f"{tid} trip picture download", download,
                                    rec, tid, TRIP_SLUG, self.args.images, self.net)
            pic = src and picture_of(rec, src)
        if not pic and trip.get("city"):
            cc = self.trip_country(doc)
            if cc:
                qid = self.soft(f"{tid} city item", self.city_qid, trip["city"], cc)
                rec = qid and self.soft(f"{tid} city {qid}", item_picture,
                                        qid, self.cache, self.net)
                src = rec and self.soft(f"{tid} trip picture download", download,
                                        rec, tid, TRIP_SLUG, self.args.images, self.net)
                pic = src and picture_of(rec, src)
        if not pic:
            for pid in stop_order(doc):
                p = (places.get(pid) or {}).get("picture")
                if p and p.get("source") == "wikimedia":
                    pic = dict(p)
                    break
        if pic:
            trip["picture"] = pic
            self.counts["trip picture"] += 1
        self.save()

    def trip_country(self, doc: dict) -> str | None:
        """The country the stops are in: most common OSM country code, else
        the trip currency's country."""
        osm = self.cache.get("osm") or {}
        codes = Counter()
        queries = self.cache.get("query") or {}
        for place in (doc.get("places") or {}).values():
            q = place.get("maps_query") or ""
            oid = (self.geo.get(q) or {}).get("osm") or (queries.get(q) or {}).get("osm")
            rec = osm.get(oid[:1].upper() + oid[1:]) if oid else None
            if rec and rec.get("cc"):
                codes[rec["cc"]] += 1
        if codes:
            return codes.most_common(1)[0][0]
        cur = ((doc.get("trip") or {}).get("currency") or {}).get("local") or ""
        return CURRENCY_COUNTRY.get(str(cur).upper())

    def city_qid(self, city: str, cc: str) -> str | None:
        cities = self.cache.setdefault("city", {})
        key = f"{city}|{cc}"
        rec = cities.get(key)
        if fresh_miss(rec):
            return None
        if rec and rec.get("qid"):
            return rec["qid"]
        qid = city_item(city, cc, self.net)
        cities[key] = {"qid": qid} if qid else {"miss": today()}
        return qid


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("itineraries", nargs="+", help="itinerary.json file(s)")
    ap.add_argument("--cache", default="pictures_cache.json")
    ap.add_argument("--geocode-cache", default="geocode_cache.json",
                    help="fg-geocode's cache: the OSM id of each pin")
    ap.add_argument("--images", default="images",
                    help="images root; pictures go to <images>/<trip>/auto/")
    ap.add_argument("--sleep", type=float, default=1.1)
    ap.add_argument("--max", type=int, default=0, dest="max_new",
                    help="cap places needing network this run (0 = no cap)")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--soft", action="store_true",
                    help="never fail: failures are listed, exit 0")
    args = ap.parse_args()

    run = Run(args)
    for itin_path in args.itineraries:
        try:
            with open(itin_path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            run.failures.append(f"{itin_path}: {e}")
            continue
        tid = (doc.get("trip") or {}).get("id") or "trip"
        mine = hand_picked(doc)
        for pid, place in (doc.get("places") or {}).items():
            place.pop("picture", None)            # rebuilt from cache every run
            if pid in mine:
                run.counts["hand-picked"] += 1
                continue
            run.place(tid, pid, place)
        (doc.get("trip") or {}).pop("picture", None)
        run.trip(doc)
        save_json(itin_path, doc)
        print(f"  wrote {itin_path}")

    c = run.counts
    print(f"\nOK  {c['picture']} place pictures, {c['trip picture']} trip pictures, "
          f"{c['website']} websites; {c['hand-picked']} hand-picked, "
          f"{c['left for live sources']} left for the live sources, "
          f"{c['none by category']} none by category, "
          f"{c['name mismatch']} pins on a differently named object, "
          f"{c['wikidata, no picture']} items with no usable image "
          f"({run.net.throttle.calls} requests)")
    if run.capped:
        print(f"    --max {args.max_new} reached; rerun to finish the rest")
    if run.failures:
        print(f"\n  {len(run.failures)} failure(s) -- skipped, nothing guessed:",
              file=sys.stderr)
        for f in run.failures:
            print(f"    {f}", file=sys.stderr)
        return 0 if args.soft else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
