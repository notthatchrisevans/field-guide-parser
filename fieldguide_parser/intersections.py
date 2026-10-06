"""
Street-corner pins for fg-geocode: "Seventh Avenue and West 47th Street".

Nominatim geocodes names and addresses, not corners, so a walk written as a
string of corners came back almost entirely unpinned (NYC, 2026-10-06: 44 of
53 walk stops). OpenStreetMap does know every corner: it is the node two
named streets share. This module finds it with the Overpass API.

  * Only a query that names two streets is tried: "X and Y", "X & Y",
    "X at Y", "Corner of X and Y". A segment, "X to Y", is pinned at its
    START corner, and only when the stop before or after names the street
    that crosses X there -- otherwise it is skipped, not guessed.
  * Names are normalised before they are matched, because the author writes
    "Seventh Avenue" and OSM says "7th Avenue": Avenue/Ave, Street/St,
    West/W, "47th"/"47", "Seventh"/"7th", Saint/St at the front.
  * The search box is local: around the day's other pins, else the query's
    own locality, else the trip's city. A corner is the node shared by both
    named ways inside that box; none shared -> nodes of the two within a few
    metres (a plaza or a split carriageway); still none -> a miss.
  * Polite: a real User-Agent, at most one request a second (fg-geocode's
    Throttle), and every answer -- a miss too, for 30 days -- cached in the
    geocode cache. OSM data is ODbL; storing the pins is fine with the
    attribution the app already carries.
"""

from __future__ import annotations

import json
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

OVERPASS = "https://overpass-api.de/api/interpreter"
RETRY_SLEEP = 5.0

# The key under which a corner is cached in the geocode cache, beside the
# Nominatim answers (which are keyed on the bare maps_query).
CACHE_PREFIX = "intersection: "

STREET_TYPES = {
    "avenue": ["avenue", "ave", "av"], "street": ["street", "st"],
    "place": ["place", "pl"], "road": ["road", "rd"],
    "boulevard": ["boulevard", "blvd"], "drive": ["drive", "dr"],
    "lane": ["lane", "ln"], "square": ["square", "sq"], "plaza": ["plaza", "plz"],
    "parkway": ["parkway", "pkwy"], "terrace": ["terrace", "ter"],
    "court": ["court", "ct"], "highway": ["highway", "hwy"], "alley": ["alley"],
    "slip": ["slip"], "row": ["row"], "walk": ["walk"], "way": ["way"],
    "bowery": ["bowery"],
}
TYPE_OF = {alias: canon for canon, aliases in STREET_TYPES.items() for alias in aliases}
TYPE_OF.pop("bowery")          # "Bowery" is a whole name, not a suffix

DIRECTIONS = {"west": ["west", "w"], "east": ["east", "e"],
              "north": ["north", "n"], "south": ["south", "s"]}
DIRECTION_OF = {a: c for c, al in DIRECTIONS.items() for a in al}

ORDINAL_WORDS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh",
                 "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
                 "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
                 "nineteenth", "twentieth"]
WORD_NUM = {w: i + 1 for i, w in enumerate(ORDINAL_WORDS)}

# Whole-name aliases OSM may use instead of the author's name. Canonical forms.
NAME_ALIASES = {"6th avenue": ["avenue of the americas"],
                "avenue of the americas": ["6th avenue"]}


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s\-]+", name.lower().replace(".", "").replace("'", "")
                                .replace("’", "")) if t]


def canon_street(name: str) -> str:
    """'Seventh Ave' -> '7th avenue'; 'W. 47 St' -> 'west 47th street';
    'St. Marks Place' -> 'saint marks place'."""
    toks = _tokens(name)
    out = []
    for i, t in enumerate(toks):
        last = i == len(toks) - 1
        if t in WORD_NUM:
            out.append(ordinal(WORD_NUM[t]))
        elif re.fullmatch(r"\d+(st|nd|rd|th)?", t):
            out.append(ordinal(int(re.match(r"\d+", t).group())))
        elif i == 0 and t == "st" and len(toks) > 1:
            out.append("saint")
        elif t in DIRECTION_OF and not last and len(toks) > 1:
            out.append(DIRECTION_OF[t])
        elif last and t in TYPE_OF and i > 0:
            out.append(TYPE_OF[t])
        else:
            out.append(t)
    return " ".join(out)


def _token_regex(tok: str, first: bool, last: bool) -> str:
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", tok)
    if m:
        n = int(m.group(1))
        words = [ORDINAL_WORDS[n - 1]] if n <= len(ORDINAL_WORDS) else []
        return "(" + "|".join([f"{n}(st|nd|rd|th)?"] + words) + ")"
    if first and tok == "saint":
        return "(saint|st[.]?)"
    if tok in DIRECTIONS and not last:
        full, short = DIRECTIONS[tok]
        return f"({full}|{short}[.]?)"
    if last and tok in STREET_TYPES and not first:
        return "(" + "|".join(f"{a}[.]?" if a != tok else a for a in STREET_TYPES[tok]) + ")"
    lit = re.escape(tok)
    if len(tok) > 2 and tok.endswith("s"):
        lit = lit[:-1] + "'?s"                     # Marks / Mark's
    return lit


def street_regex(name: str) -> str:
    """A case-insensitive Overpass (POSIX ERE) regex for the ways that carry
    this street's name, in any of the spellings canon_street folds together.
    A bare name with no street type ("Mott") accepts any type after it."""
    variants = []
    for c in [canon_street(name)] + NAME_ALIASES.get(canon_street(name), []):
        toks = c.split()
        parts = [_token_regex(t, i == 0, i == len(toks) - 1) for i, t in enumerate(toks)]
        rx = " +".join(parts)
        if toks and (toks[-1] not in STREET_TYPES or len(toks) == 1) and toks[-1] != "bowery":
            rx += "( +(" + "|".join(sorted(TYPE_OF)) + ")[.]?)?"
        variants.append(rx)
    return "^(" + "|".join(variants) + ")$"


# ------------------------------------------------------------------ parsing

_PAIR_SEPS = r"\s+(?:and|&|at|@)\s+|\s*&\s*"


def _looks_like_street(side: str, strict: bool) -> bool:
    side = side.strip()
    if not side or len(side.split()) > 5:
        return False
    toks = _tokens(side)
    if re.fullmatch(r"\d+[a-z]?", toks[0]) and len(toks) > 1 and toks[1] not in TYPE_OF:
        return False                               # "103 Orchard Street": an address
    if re.match(r"(?i)(the|a|an|my|our)\b", side) or not re.match(r"[A-Za-z0-9]", side):
        return False
    if not strict:
        return True
    # A place NAME must look like a street: it ends in a street type, or is
    # one or two capitalised words ("Mott", "Bowery", "Avenue A").
    return (toks[-1] in TYPE_OF or toks[0] in TYPE_OF or toks[-1] == "bowery"
            or (len(toks) <= 2 and all(w[:1].isupper() or w[:1].isdigit()
                                         for w in side.split())))


def parse_streets(text: str, strict: bool = False) -> tuple[str, str, str] | None:
    """('pair' | 'segment', street_a, street_b) from the first component of a
    query or name, or None. strict=True is for a stop's NAME (prose), which
    must look like streets on both sides, not a sentence."""
    first = (text or "").split(",")[0].strip()
    first = re.sub(r"(?i)^(the\s+)?corner\s+of\s+", "", first).strip()
    if not first:
        return None
    m = re.split(r"(?i)\s+to\s+", first, maxsplit=1)
    if len(m) == 2 and not re.search(r"(?i)\s+(and|&|at|@)\s+", m[0]):
        a, b = m[0].strip(), m[1].strip()
        if _looks_like_street(a, strict) and _looks_like_street(b, strict):
            return ("segment", a, b)
        return None
    parts = re.split(f"(?i){_PAIR_SEPS}", first, maxsplit=1)
    if len(parts) == 2:
        a, b = parts[0].strip(), parts[1].strip()
        if _looks_like_street(a, strict) and _looks_like_street(b, strict):
            return ("pair", a, b)
    return None


def locality(query: str) -> str:
    """'Seventh Avenue and West 47th Street, Manhattan, NY' -> 'Manhattan, NY'."""
    parts = [p.strip() for p in (query or "").split(",") if p.strip()]
    return ", ".join(parts[1:])


def cache_key(a: str, b: str, where: str) -> str:
    x, y = sorted([canon_street(a), canon_street(b)])
    return f"{CACHE_PREFIX}{x} & {y} | {where}".rstrip(" |")


# ------------------------------------------------------------------ Overpass

NAME_KEYS = ("name", "alt_name", "official_name", "old_name", "short_name")


def _ways(street: str, into: str) -> str:
    rx = street_regex(street)
    alts = " ".join(f'way.h["{k}"~"{rx}",i];' for k in NAME_KEYS)
    return f"({alts})->.{into};\n"


def overpass_ql(a: str, b: str, bbox: tuple[float, float, float, float], near_m: int = 0) -> str:
    """Named highways in the box first (cheap), then the two streets among
    them by any of their names, then the node(s) they share -- or, with
    near_m, nodes of one within near_m metres of the other."""
    s, w, n, e = (round(v, 5) for v in bbox)
    head = f'[out:json][timeout:25];\nway["highway"]["name"]({s},{w},{n},{e})->.h;\n'
    sets = _ways(a, "a") + _ways(b, "b") + "node(w.a)->.na;\nnode(w.b)->.nb;\n"
    pick = "node.na.nb;\n" if not near_m else f"node.na(around.nb:{near_m});\n"
    return f"{head}{sets}{pick}out skel;"


class OverpassError(RuntimeError):
    pass


def overpass_once(ql: str, ua: str) -> list[dict]:
    """POST one query; the node elements it returns. Errors raise
    OverpassError (the caller treats that as 'not now', never as a miss)."""
    data = urllib.parse.urlencode({"data": ql}).encode()
    req = urllib.request.Request(OVERPASS, data=data, headers={
        "User-Agent": ua, "Accept": "application/json"})
    err = None
    for attempt in range(3):          # 429/504 are "busy": back off, retry
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                doc = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            err = OverpassError(f"Overpass HTTP {e.code}")
            if e.code not in (429, 502, 503, 504):
                raise err from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            err = OverpassError(f"Overpass network error: {e}")
        time.sleep(RETRY_SLEEP * (attempt + 1))
    else:
        raise err
    return [el for el in doc.get("elements", []) if el.get("type") == "node"
            and "lat" in el and "lon" in el]


def corner_from_nodes(nodes: list[dict], ref: tuple[float, float]) -> dict | None:
    """The corner: the node nearest the reference point, averaged with any
    node within ~80 m of it (a split carriageway has two or four)."""
    from .geocode import haversine_km
    if not nodes:
        return None
    best = min(nodes, key=lambda nd: haversine_km(ref, (nd["lat"], nd["lon"])))
    near = [nd for nd in nodes
            if haversine_km((best["lat"], best["lon"]), (nd["lat"], nd["lon"])) <= 0.08]
    return {"lat": round(statistics.mean(nd["lat"] for nd in near), 6),
            "lng": round(statistics.mean(nd["lon"] for nd in near), 6),
            "nodes": sorted(f"n{nd['id']}" for nd in near if "id" in nd)}


def find_corner(a: str, b: str, bbox, ref, ua: str, throttle, fetch=None) -> dict | None:
    """The corner of streets a and b inside bbox, as a geocode-cache record,
    or None. Shared node first; failing that, nodes within 15 m."""
    fetch = fetch or overpass_once
    for near_m in (0, 15):
        throttle.wait()
        corner = corner_from_nodes(fetch(overpass_ql(a, b, bbox, near_m), ua), ref)
        if corner:
            return {"lat": corner["lat"], "lng": corner["lng"], "confidence": "intersection",
                    "source": "overpass", "streets": [a, b], "osm_nodes": corner["nodes"],
                    **({"matched_via": "nodes within 15 m"} if near_m else {})}
    return None
