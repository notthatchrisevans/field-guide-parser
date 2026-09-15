#!/usr/bin/env python3
"""
Parse a Markdown trip file (templates/trip-template.md dialect) into the same
itinerary.json the docx parser emits. Markdown is the authoring surface for
new trips (decided 2026-08-04): it is what LLMs emit reliably, it diffs, and
the template file carries its own spec so any assistant can fill it.

Same philosophy as parse_guide.py: FAIL LOUDLY. Structural problems, unknown
categories, unparseable time labels, weekday/date mismatches, out-of-order
days and lodging (airbnb/hotel/friends) without an address are hard errors — the run exits non-zero
and writes nothing. Missing "if available" content (a menu link on food, an
event link on a performance, a venue link on a gallery) is an ADVISORY:
listed, not fatal, because forcing it would teach an LLM to fabricate a URL.
`--strict` promotes advisories to errors.

Usage:
    python3 parse_guide_md.py source/skye-2027-05.md
        [--out trips/<trip>/itinerary.json]   (defaults from front matter)
        [--strict]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from urllib.parse import quote_plus

try:
    from .parse_guide import (CATEGORY_MAP, MONTHS, TRAVEL_MODES, ParseState,
                              parse_dir_url, parse_pace, parse_time, slugify)
except ImportError:                       # run directly as a script
    from parse_guide import (CATEGORY_MAP, MONTHS, TRAVEL_MODES, ParseState,
                             parse_dir_url, parse_pace, parse_time, slugify)

# Guide text is full of non-Latin-1 characters (ı, ş, ō); a Windows console
# or pipe defaulting to cp1252 must not be able to crash a successful parse.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")

DAY_RE = re.compile(
    r"^##\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*,\s*"
    r"([a-z]+)\s+(\d{1,2})\s*$", re.I)
TITLE_RE = re.compile(r"^###\s+(.+?)\s*$")
THESIS_RE = re.compile(r"^>\s+(.+?)\s*$")
DAYFACT_RE = re.compile(r"^(window|anchors|pace|reset)\s*:\s*(.+?)\s*$", re.I)
# Day-level content lines (photography-era trips use all three):
#   Route: <one-line route summary>          -> day.route_summary
#   Aim: <one assignment item>               -> day.assignment[] (repeatable)
#   Leg: [<label>](<maps dir url>) — <note>  -> day.nav_segments[] (repeatable)
ROUTE_RE = re.compile(r"^route\s*:\s*(.+?)\s*$", re.I)
AIM_RE = re.compile(r"^aim\s*:\s*(.+?)\s*$", re.I)
LEG_RE = re.compile(r"^leg\s*:\s*\[([^\]]+)\]\((\S+)\)(?:\s+—\s+(.+?))?\s*$", re.I)
# `-` or `*`: both are legal markdown bullets, and AIs use both freely.
STOP_RE = re.compile(r"^[-*]\s+(.+?)\s*\|\s*(.+?)\s*\[([^\]]+)\]\s*$")
SUB_RE = re.compile(r"^\s+[-*]\s+([a-z_]+)\s*:\s*(.*?)\s*$")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

FRONT_REQUIRED = ("trip", "city", "timezone", "currency", "year")

# Structured journey fields, allowed only on [transit] stops. A [transit]
# stop is either a RIDE (a flight, train, ferry -- carries a journey block)
# or a PLACE (a metro station, ferry terminal, interchange you move through
# -- carries none; the photography trips are full of these). Any journey
# field present marks the stop as a ride and the block must then be
# complete: carrier always; departs/arrives whenever the stop itself has a
# clock time (and they must agree with it) -- on an open-time ride (a
# weather-dependent ferry) they are advisories, as are service / boarding /
# seat until known. A bare [transit] stop gets a single advisory asking
# whether it is secretly a ride. --strict promotes all advisories.
TRANSIT_KEYS = ("carrier", "service", "boarding", "departs", "arrives", "seat")
TRANSIT_TIME_KEYS = ("boarding", "departs", "arrives")
CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

SUB_KEYS = {"where", "name", "address", "what", "notes", "next", "links",
            "review", *TRANSIT_KEYS}

# Link labels that describe the venue rather than the visit: these attach to
# the place and render on every stop there. Everything else stays on the stop.
PLACE_LINK_LABELS = {"menu", "venue", "website", "info"}

# "Required if available" content per category -> advisory when absent.
ADVISE_PLACE_LINK = {
    "food": "Menu",
    "gallery": "Venue",
}
ADVISE_STOP_LINK = {
    "performance": "Event",
}

# The lodging kinds (airbnb / hotel / friends) all require an `address:` —
# there is always an address, whatever kind of roof it is.
LODGING_CATS = {"airbnb", "hotel", "friends"}

MAPS = "https://www.google.com/maps/search/?api=1&query="

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")

TZ_RE = re.compile(r"^[A-Za-z_]+/[A-Za-z_+\-]+$")

# Signatures of markdown copied from a chat window's rendered view: fences
# and rules become ornaments, headers lose their hashes. We never repair
# silently — but the error should name the actual mistake.
RENDERED_COPY_RE = re.compile(r"^[⸻—–_]{1,}$")
BARE_DAY_RE = re.compile(r"^(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|"
                         r"SATURDAY|SUNDAY), [A-Z]+ \d{1,2}$")


def parse_front_matter(lines: list[str], state: ParseState) -> tuple[dict, int]:
    """Front matter is the FIRST `---` block in the file; everything before
    it is instructions and is skipped."""
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "---")
    except StopIteration:
        msg = "no `---` front-matter block found"
        if any(RENDERED_COPY_RE.match(ln.strip()) for ln in lines) or \
           any(ln.strip().startswith("trip:") for ln in lines):
            msg += (" — this looks like a chat reply copied from its RENDERED "
                    "view (the --- fences, ## headers, and [links](urls) get "
                    "destroyed by that). Ask the AI to put the file in a code "
                    "block and copy THAT, raw.")
        state.fail("front matter", msg)
        return {}, 0
    meta = {}
    for j in range(start + 1, len(lines)):
        ln = lines[j].strip()
        if ln == "---":
            for key in FRONT_REQUIRED:
                if key not in meta:
                    state.fail("front matter", f"missing {key!r}")
            if "timezone" in meta and not TZ_RE.match(meta["timezone"]):
                state.fail("front matter",
                           f"timezone {meta['timezone']!r} is not Area/City form")
            if "year" in meta:
                try:
                    meta["year"] = int(meta["year"])
                except ValueError:
                    state.fail("front matter", f"year {meta['year']!r} is not a number")
            return meta, j + 1
        if not ln or ln.startswith("#"):
            continue
        if ":" not in ln:
            state.fail("front matter", f"not a key: value line: {ln!r}")
            continue
        key, val = ln.split(":", 1)
        meta[key.strip().lower()] = val.split("#", 1)[0].strip()
    state.fail("front matter", "front-matter block never closed with `---`")
    return meta, len(lines)


def md_links(raw: str, where: str, state: ParseState) -> list[dict]:
    found = LINK_RE.findall(raw)
    leftover = LINK_RE.sub("", raw).strip(" •·,;")
    if not found:
        state.fail(where, f"links line has no [Label](url): {raw!r}")
    if leftover:
        state.fail(where, f"links line has stray text {leftover!r} — "
                          f"only [Label](url) entries, separated by spaces or •")
    out = []
    for label, url in found:
        if not url.startswith(("http://", "https://")):
            state.fail(where, f"link {label!r} has a non-http url: {url!r}")
            continue
        out.append({"label": label.strip(), "url": url})
    return out


def get_place(state: ParseState, query: str, name: str, cat: str) -> str:
    """Same identity rule as the docx parser: the Maps query string IS the
    place. First sighting names it and sets its category."""
    existing = state.places.get(query)
    if existing:
        return existing["id"]
    base = slugify(name)
    taken = {p["id"] for p in state.places.values()}
    pid, n = base, 2
    while pid in taken:
        pid, n = f"{base}-{n}", n + 1
    state.places[query] = {
        "id": pid, "name": name, "category": cat,
        "maps_query": query, "maps_url": MAPS + quote_plus(query),
        "coords": None,
    }
    return pid


def parse_md(text: str, state: ParseState):
    lines = text.splitlines()
    meta, body_start = parse_front_matter(lines, state)
    year = meta.get("year") if isinstance(meta.get("year"), int) else None

    days: list[dict] = []
    day = None
    stop = None
    stop_place_query = None

    def close_stop():
        nonlocal stop, stop_place_query
        if stop is None:
            return
        where = stop.pop("_where", None)
        loc = stop.pop("_loc")
        display = stop.pop("_display")
        cat = stop.pop("_cat")
        if not where:
            state.fail(loc, f"stop {display!r} has no `where:` line")
            stop = None
            return
        place_name = stop.pop("_name", None) or display
        pid = get_place(state, where, place_name, cat)
        place = state.places[where]
        addr = stop.pop("_pending_address", None)
        if addr:
            place["address"] = addr
        if cat in LODGING_CATS and not place.get("address"):
            state.fail(loc, f"{cat} {display!r} has no `address:` line "
                            f"(the cab-driver address is required; put it on "
                            f"the place's first stop)")

        transit = stop.pop("_transit", {})
        if transit and cat != "transit":
            state.fail(loc, f"{display!r} is [{cat}] but carries journey "
                            f"fields ({', '.join(sorted(transit))}) — those "
                            f"belong on [transit] stops only")
        elif cat == "transit" and transit:
            if not transit.get("carrier"):
                state.fail(loc, f"transit {display!r} has journey fields but "
                                f"no `carrier:` — required on every ride")
            if stop["time"].get("start"):
                for key in ("departs", "arrives"):
                    if not transit.get(key):
                        state.fail(loc, f"transit {display!r} has a scheduled "
                                        f"time but no `{key}:` — required on "
                                        f"clocked rides")
            for key in TRANSIT_TIME_KEYS:
                if key in transit:
                    m = CLOCK_RE.match(transit[key])
                    if not m:
                        state.fail(loc, f"transit {display!r}: `{key}:` must "
                                        f"be a 24h clock time, got "
                                        f"{transit[key]!r}")
                    else:
                        transit[key] = f"{int(m.group(1)):02d}:{m.group(2)}"
            # the stop's own time label must not tell a different story
            t = stop["time"]
            if t.get("start") and transit.get("departs") \
                    and t["start"] != transit["departs"]:
                state.fail(loc, f"transit {display!r}: stop time starts "
                                f"{t['start']} but `departs:` says "
                                f"{transit['departs']}")
            if t.get("end") and transit.get("arrives") \
                    and t["end"] != transit["arrives"]:
                state.fail(loc, f"transit {display!r}: stop time ends "
                                f"{t['end']} but `arrives:` says "
                                f"{transit['arrives']}")
            if transit:
                stop["transit"] = transit
        stop["place"] = pid
        if display != place["name"]:
            stop["label_override"] = display
        # venue-type links live on the place; visit-type links on the stop
        stop_links = []
        for link in stop.pop("_links", []):
            if link["label"].lower() in PLACE_LINK_LABELS:
                plinks = place.setdefault("links", [])
                if link["url"] not in {l["url"] for l in plinks}:
                    plinks.append(link)
            else:
                stop_links.append(link)
        if stop_links:
            stop["links"] = stop_links
        if not stop.get("notes"):
            stop["notes"] = []
        stop.setdefault("photo", None)
        stop.setdefault("next", None)
        day["stops"].append(stop)
        stop = None
        stop_place_query = None

    for i, raw in enumerate(lines[body_start:], start=body_start + 1):
        line = raw.rstrip()
        loc = f"line {i}"

        m = DAY_RE.match(line)
        if m:
            close_stop()
            weekday, month_name, dom = m.group(1).lower(), m.group(2).lower(), int(m.group(3))
            month = MONTHS.get(month_name)
            if month is None or year is None:
                state.fail(loc, f"unknown month {month_name!r}" if month is None
                           else "no usable year in front matter")
                day = {"date": None, "title": None, "thesis": None, "stops": [],
                       "assignment": [], "nav_segments": []}
                days.append(day)
                continue
            d = date(year, month, dom)
            # A trip can cross New Year (Dec -> Jan). If this day would land
            # before the previous one and moving it a year later fixes that,
            # roll the trip's working year forward. The weekday check below
            # independently confirms the roll, since the same date lands on
            # a different weekday in different years.
            if days and days[-1]["date"]:
                prev = date.fromisoformat(days[-1]["date"])
                if d <= prev and date(year + 1, month, dom) > prev:
                    year += 1
                    d = date(year, month, dom)
            actual = WEEKDAYS[d.weekday()]
            if actual != weekday:
                state.fail(loc, f"{d.isoformat()} is a {actual.capitalize()}, "
                                f"not {weekday.capitalize()} — check the date")
            if days and days[-1]["date"] and days[-1]["date"] >= d.isoformat():
                state.fail(loc, f"day {d.isoformat()} is not after "
                                f"{days[-1]['date']} — days must be in order")
            day = {"date": d.isoformat(), "title": None, "thesis": None,
                   "stops": [], "assignment": [], "nav_segments": []}
            days.append(day)
            continue

        if day is None:
            continue                      # instructions / preamble

        m = TITLE_RE.match(line)
        if m and day["title"] is None and stop is None:
            day["title"] = m.group(1)
            continue

        m = THESIS_RE.match(line)
        if m and day["thesis"] is None and stop is None:
            day["thesis"] = m.group(1)
            continue

        m = ROUTE_RE.match(line)
        if m and stop is None:
            day["route_summary"] = m.group(1)
            continue

        m = AIM_RE.match(line)
        if m and stop is None:
            day["assignment"].append(m.group(1))
            continue

        m = LEG_RE.match(line)
        if m and stop is None:
            label, url, note = m.group(1), m.group(2), m.group(3)
            parsed = parse_dir_url(url)
            if parsed is None:
                state.fail(loc, f"Leg {label!r}: not a Google Maps directions "
                                f"URL (needs origin= and destination=)")
                continue
            if parsed["mode"] and parsed["mode"] not in TRAVEL_MODES:
                state.fail(loc, f"Leg {label!r}: unknown travelmode "
                                f"{parsed['mode']!r}")
                continue
            segment = {"label": label, "mode": parsed["mode"], "url": url,
                       "origin": parsed["origin"],
                       "destination": parsed["destination"],
                       "waypoints": parsed["waypoints"]}
            if note:
                segment["note"] = note
            day["nav_segments"].append(segment)
            continue

        m = DAYFACT_RE.match(line)
        if m and stop is None:
            key, val = m.group(1).lower(), m.group(2)
            if key == "window":
                times = TIME_RE.findall(val)
                day["window"] = {
                    "label": val,
                    "start": f"{int(times[0][0]):02d}:{times[0][1]}" if times else None,
                    "finish": f"{int(times[1][0]):02d}:{times[1][1]}" if len(times) > 1 else None,
                    "finish_approx": "about" in val.lower(),
                }
            elif key == "anchors":
                day["anchors"] = [a.strip() for a in val.split(";") if a.strip()]
            elif key == "pace":
                day["pace"] = parse_pace(val)
            elif key == "reset":
                day["evening_reset"] = val
            continue

        m = STOP_RE.match(line)
        if m:
            close_stop()
            time_label, display, cat_raw = m.groups()
            low = cat_raw.strip().lower()
            cat = CATEGORY_MAP.get(low) or (low if low in CATEGORY_MAP.values() else None)
            if cat is None:
                state.fail(loc, f"unknown category {cat_raw!r} (closed list — "
                                f"see the template)")
                continue
            stop = {"_loc": loc, "_display": display.strip(), "_cat": cat,
                    "_links": [], "notes": [],
                    "time": parse_time(time_label, state, loc)}
            continue

        m = SUB_RE.match(line)
        if m and stop is not None:
            key, val = m.group(1).lower(), m.group(2)
            if key not in SUB_KEYS:
                state.fail(loc, f"unknown stop field {key!r} "
                                f"(allowed: {', '.join(sorted(SUB_KEYS))})")
                continue
            if key == "where":
                stop["_where"] = val
            elif key == "name":
                stop["_name"] = val
            elif key == "address":
                # belongs to the place; lifted onto it when the stop closes
                stop["_pending_address"] = val
            elif key == "what":
                stop["photo"] = val
            elif key == "notes":
                stop["notes"].append(val)
            elif key == "next":
                stop["next"] = {"raw": val}
            elif key == "links":
                stop["_links"].extend(md_links(val, loc, state))
            elif key == "review":
                stop["notes"].append("⚠ REVIEW: " + val)
            elif key in TRANSIT_KEYS:
                stop.setdefault("_transit", {})[key] = val
            continue

        stripped = line.strip()
        if stripped[:1] in "-*" and len(stripped) > 1:
            # a bullet that matched neither STOP_RE nor SUB_RE: never silent
            state.fail(loc, f"unparseable bullet: {stripped!r} — expected "
                            f"'- TIME | Name [Category]' or an indented "
                            f"'- field: value' under a stop")
        # other free text inside a day is tolerated, like docx extras

    close_stop()
    return meta, days


def collect_advisories(days, state) -> list[str]:
    out = []
    for place in state.places.values():
        want = ADVISE_PLACE_LINK.get(place["category"])
        if want:
            labels = {l["label"].lower() for l in place.get("links", [])}
            if not labels & PLACE_LINK_LABELS:
                out.append(f"{place['id']}: no {want} link (add if one exists)")
    for day in days:
        for stop in day["stops"]:
            place = next(p for p in state.places.values() if p["id"] == stop["place"])
            name = stop.get("label_override") or place["name"]
            want = ADVISE_STOP_LINK.get(place["category"])
            if want and not stop.get("links") and not place.get("links"):
                out.append(f"{day['date']} {name}: no {want} link "
                           f"(add a write-up if one exists)")
            if place["category"] == "transit":
                transit = stop.get("transit", {})
                if not transit:
                    out.append(f"{day['date']} {name}: no journey block — "
                               f"fine for a station/terminal stop; a ride "
                               f"needs carrier: (+ departs/arrives when "
                               f"clocked)")
                    continue
                for key, why in (("service", "flight/train number"),
                                 ("boarding", "boarding time"),
                                 ("seat", "seat")):
                    if not transit.get(key):
                        out.append(f"{day['date']} {name}: no `{key}:` "
                                   f"({why} — add once assigned/known)")
                if not stop["time"].get("start"):
                    for key in ("departs", "arrives"):
                        if not transit.get(key):
                            out.append(f"{day['date']} {name}: open-time "
                                       f"ride with no `{key}:` — add when "
                                       f"the sailing/departure is chosen")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("markdown", help="filled trip .md in the template dialect")
    ap.add_argument("--out", default=None,
                    help="output path (default trips/<trip>/itinerary.json)")
    ap.add_argument("--strict", action="store_true",
                    help="advisories (missing if-available links) become errors")
    args = ap.parse_args()

    with open(args.markdown, encoding="utf-8") as fh:
        text = fh.read()

    state = ParseState()
    meta, days = parse_md(text, state)

    if not days:
        msg = "no day headers found (expected e.g. '## FRIDAY, MAY 7')"
        if any(BARE_DAY_RE.match(ln.strip()) for ln in text.splitlines()):
            msg += (" — day lines exist but lost their `## `, which is the "
                    "signature of a chat reply copied from its RENDERED view. "
                    "Ask the AI to put the file in a code block and copy THAT, "
                    "raw.")
        state.fail("document", msg)
    for day in days:
        if not day["stops"]:
            state.fail(day["date"] or "?", "day parsed with zero stops")

    advisories = collect_advisories(days, state)
    if args.strict:
        for a in advisories:
            state.fail("strict", a)
        advisories = []

    if state.problems:
        print(f"\n  {len(state.problems)} PROBLEM(S) -- refusing to write output:\n",
              file=sys.stderr)
        for p in state.problems:
            print(f"  [{p.where}] {p.detail}", file=sys.stderr)
        return 1

    places = {p["id"]: {k: v for k, v in p.items() if k != "id"}
              for p in state.places.values()}
    doc = {
        "trip": {
            "id": meta["trip"],
            "city": meta["city"],
            "timezone": meta["timezone"],
            "currency": {"local": meta["currency"].upper(),
                         "home": meta.get("home_currency", "USD").upper()},
            "start_date": days[0]["date"],
            "end_date": days[-1]["date"],
        },
        "places": places,
        "days": days,
    }

    import os
    # Default output is relative to the CALLER's working directory — never
    # to this file, which lives in site-packages once installed. (The
    # vendored-script era resolved against __file__, and the extraction
    # silently sent output into the package tree; found 2026-09-15.)
    out = args.out or os.path.join(
        os.getcwd(), "trips", meta["trip"], "itinerary.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)

    stops = sum(len(d["stops"]) for d in days)
    print(f"OK  {len(days)} days, {stops} stops, {len(places)} unique places")
    print(f"    -> {out}")
    if advisories:
        print(f"\n  {len(advisories)} advisory(ies) -- missing if-available "
              f"content, not fatal:")
        for a in advisories:
            print(f"    {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
