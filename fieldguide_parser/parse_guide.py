#!/usr/bin/env python3
"""
Parse a Field Guide .docx into itinerary.json.

Design rule: this parser FAILS LOUDLY. Any cell it cannot decompose is
collected and reported, and the run exits non-zero. A silently dropped
stop in a navigation app is the worst possible failure mode, so we would
rather refuse to build than ship a day with a hole in it.

Usage:
    python3 parse_guide.py GUIDE.docx --trip-id istanbul-2026-08 \\
        --city Istanbul --tz Europe/Istanbul --currency TRY \\
        --out trips/istanbul-2026-08/itinerary.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import parse_qs, unquote_plus, urlparse

try:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
except ModuleNotFoundError:
    # Markdown-only environments (the public planning repo's CI) import this
    # module for CATEGORY_MAP and friends; only the docx path needs python-docx.
    Document = qn = Table = Paragraph = None

# --------------------------------------------------------------------------
# Controlled vocabulary. A category in the doc that is not in this map is an
# error, not a new category -- that keeps the UI's colour/filter set closed.
# --------------------------------------------------------------------------
CATEGORY_MAP = {
    "market": "market",
    "working district": "working",
    "residential life": "residential",
    "decay/architecture": "decay",
    "waterfront": "waterfront",
    "transit": "transit",
    "gallery": "gallery",
    "food": "food",
    "airport": "airport",
    # Lodging split (Sep 2026): what kind of roof matters mid-trip, so
    # lodging became airbnb / hotel / friends. The bare "lodging" tag is
    # deliberately NOT an alias — the parser can't know which of the three
    # it meant, so it fails loudly and the author picks one.
    "airbnb": "airbnb",
    "hotel": "hotel",
    "friends": "friends",
    "friend's house": "friends",
    "friends house": "friends",
    # Japan (Dec 2026): temples-as-contemplative-space needed their own
    # word — gallery was a lie and public too loose. Family: study.
    "temple": "temple",
    # Routes (Sep 2026): one shooting walk is one item that opens into
    # ordered stops. The route line itself has no place; its `stop:` lines
    # do. Family: field.
    "route": "route",
    # Spa consolidation (Sep 2026): hammam and bathhouse were per-trip names
    # for the same kind of stop; "spa" is the canonical, the old names stay
    # as aliases so existing guides parse unchanged.
    "spa": "spa",
    "hammam": "spa",
    "bathhouse": "spa",
    "public bath": "spa",
    # Tbilisi (Aug 2026). "public life" is street/square life as distinct
    # from residential; an airport lounge is a rest space, not navigation.
    # Families (frontend): lounge -> rest, public -> field.
    "public life": "public",
    "airport lounge": "lounge",
    # Scotland (Aug 2026) — a festival/nature trip. Fringe shows, opera and
    # the Tattoo are watched like galleries; parks, glens and village wanders
    # are fieldwork; browsing shops is fieldwork too.
    # Families (frontend): performance -> study, nature/shop -> field.
    "performance": "performance",
    "nature": "nature",
    "shop": "shop",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

DAY_HEADER_RE = re.compile(
    r"^(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s+([A-Z]+)\s+(\d{1,2})$"
)

# The navigation appendix writes its dates in title case ("Friday, August 7")
# rather than the timeline's caps, so it needs its own case-insensitive match.
SEGMENT_DAY_RE = re.compile(
    r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),\s+"
    r"([a-z]+)\s+(\d{1,2})$", re.I
)

# Google Maps travel modes the appendix actually uses. Closed, like every other
# vocabulary here: an unrecognised mode is a doc change worth noticing, not a
# value to pass through.
TRAVEL_MODES = {"walking", "transit", "driving", "bicycling", "two-wheeler"}

# "  |  " in python-docx, "│" in some exporters. Accept both.
SEP_RE = re.compile(r"\s*[|│]\s*")

TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
RANGE_RE = re.compile(r"^(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})$")

MEAL_WORDS = {"breakfast", "lunch", "dinner"}
APPROX_WORDS = {"around", "about", "target", "by", "approx"}

# Timeless-but-valid labels. The guide names some stops by daypart or meal slot
# instead of a clock ("Evening", "Late afternoon", "Early lunch / late
# breakfast", "Check-in"). These are legitimately certainty=open, never treated
# as "now". This is an explicit allow-list, NOT a catch-all: an unrecognised
# time label is still a hard error, because a silently-open stop reads as
# planned-but-untimed rather than what it is -- a parser miss.
DAYPART_WORDS = {"morning", "afternoon", "evening", "night",
                 "midday", "noon", "dawn", "dusk"}
OPEN_PHRASES = {"transfer", "check-in", "checkin"}


@dataclass
class Problem:
    where: str
    detail: str


@dataclass
class ParseState:
    places: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    segments: int = 0

    def fail(self, where: str, detail: str) -> None:
        self.problems.append(Problem(where, detail))


# --------------------------------------------------------------------------
# Block iteration in true document order
# --------------------------------------------------------------------------
def iter_blocks(doc):
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def cell_links(cell):
    """Return [(text, url)] for hyperlinks in a cell, in order."""
    out = []
    for para in cell.paragraphs:
        for hl in para._p.findall(qn("w:hyperlink")):
            rid = hl.get(qn("r:id"))
            if not rid:
                continue
            text = "".join(n.text or "" for n in hl.iter(qn("w:t")))
            try:
                url = cell.part.rels[rid].target_ref
            except KeyError:
                url = None
            if url:
                out.append((text.strip(), url))
    return out


def para_links(para):
    out = []
    for hl in para._p.findall(qn("w:hyperlink")):
        rid = hl.get(qn("r:id"))
        if not rid:
            continue
        text = "".join(n.text or "" for n in hl.iter(qn("w:t")))
        try:
            url = para.part.rels[rid].target_ref
        except KeyError:
            continue
        out.append((text.strip(), url))
    return out


# NFKD decomposes ş ğ ç ö ü, but NOT the dotless/dotted i pair -- those are
# distinct letters in Turkish, not decorated Latin i. Without this table
# "Kadıköy" slugifies to "kad-koy". Extend per language as trips are added;
# non-Latin scripts (e.g. Georgian for Tbilisi) will need romanisation here.
TRANSLIT = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
})


def slugify(value: str) -> str:
    value = value.translate(TRANSLIT)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-")[:48] or "place"


def maps_query_from_url(url: str) -> str | None:
    """Extract the decoded `query=` parameter from a Maps search URL."""
    try:
        qs = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    if "query" in qs:
        return unquote_plus(qs["query"][0])
    return None


# --------------------------------------------------------------------------
# Time parsing
# --------------------------------------------------------------------------
def parse_time(label: str, state: ParseState, where: str) -> dict:
    """
    Times in the guide are heterogeneous by design. Preserve the author's
    label verbatim for display; derive machine values where they exist and
    mark how much to trust them.
    """
    raw = label.strip()
    low = raw.lower()
    out = {"label": raw, "start": None, "end": None, "certainty": "open"}

    for meal in MEAL_WORDS:
        if meal in low:
            out["meal"] = meal

    m = RANGE_RE.match(raw)
    if m:
        out["start"], out["end"] = m.group(1), m.group(2)
        out["certainty"] = "scheduled"
        return out

    times = TIME_RE.findall(raw)
    if len(times) >= 1:
        out["start"] = f"{int(times[0][0]):02d}:{times[0][1]}"
        if len(times) >= 2:
            out["end"] = f"{int(times[1][0]):02d}:{times[1][1]}"
        out["certainty"] = (
            "approximate"
            if any(w in low for w in APPROX_WORDS)
            else "scheduled"
        )
        return out

    # No clock time at all: "After dinner", "Transfer", "Evening",
    # "Late afternoon", "Early lunch / late breakfast", "Check-in".
    words = set(re.findall(r"[a-z-]+", low))
    if (low.startswith("after")
            or low in OPEN_PHRASES
            or "midnight" in low
            or words & DAYPART_WORDS
            or words & MEAL_WORDS):
        out["certainty"] = "open"
        return out

    state.fail(where, f"unrecognised time label: {raw!r}")
    return out


# --------------------------------------------------------------------------
# FIELD STOP cell
# --------------------------------------------------------------------------
def parse_stop_cell(cell, state: ParseState, where: str) -> dict | None:
    text = cell.text.strip()
    if not text:
        state.fail(where, "empty FIELD STOP cell")
        return None

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    head = lines[0]
    body = " ".join(lines[1:])

    m = re.match(r"^(.*?)\s*\[([^\]]+)\]\s*$", head)
    if not m:
        state.fail(where, f"no [Category] tag in head line: {head!r}")
        return None
    name, cat_raw = m.group(1).strip(), m.group(2).strip()

    cat = CATEGORY_MAP.get(cat_raw.lower())
    if cat is None:
        state.fail(where, f"unknown category {cat_raw!r} (add to CATEGORY_MAP)")
        return None

    links = cell_links(cell)
    if not links:
        state.fail(where, f"no hyperlink on stop {name!r}")
        return None
    link_text, url = links[0]
    query = maps_query_from_url(url)
    if not query:
        state.fail(where, f"could not read query= from URL for {name!r}")
        return None

    # Identity is the Maps query, not the display name: "Kadıköy Ferry
    # Terminal" and "Kadıköy Ferry Terminal — Return" are one place.
    place_id = state.places.get(query, {}).get("id")
    if place_id is None:
        base = slugify(link_text or name)
        place_id = base
        taken = {p["id"] for p in state.places.values()}
        n = 2
        while place_id in taken:
            place_id = f"{base}-{n}"
            n += 1
        state.places[query] = {
            "id": place_id,
            "name": link_text or name,
            "category": cat,
            "maps_query": query,
            "maps_url": url,
            "coords": None,
        }

    parts = [p for p in SEP_RE.split(body) if p]
    photo, notes, nxt = None, [], None
    for part in parts:
        if part.lower().startswith("photo:"):
            photo = part.split(":", 1)[1].strip()
        elif part.lower().startswith("next:"):
            nxt = part.split(":", 1)[1].strip()
        else:
            notes.append(part.strip())

    if photo is None:
        state.fail(where, f"missing 'Photo:' segment on {name!r}")

    stop = {
        "place": place_id,
        "photo": photo,
        "notes": notes,
        "next": {"raw": nxt} if nxt else None,
    }
    if name != (link_text or name):
        stop["label_override"] = name
    elif name != state.places[query]["name"]:
        stop["label_override"] = name
    return stop


# --------------------------------------------------------------------------
# Meta 2x2 table (START/FINISH, WALKING/PACE, ANCHORS, EVENING RESET)
# --------------------------------------------------------------------------
def parse_meta_table(tbl) -> dict:
    meta = {}
    for row in tbl.rows:
        for cell in row.cells:
            txt = cell.text.strip()
            if not txt:
                continue
            parts = txt.split("\n", 1)
            key = parts[0].strip().rstrip(":")
            val = parts[1].strip() if len(parts) > 1 else ""
            if not val:
                m = re.match(r"^(START / FINISH|WALKING / PACE|ANCHORS|EVENING RESET)\s+(.*)$", txt, re.S)
                if m:
                    key, val = m.group(1), m.group(2).strip()
            meta[key.upper()] = val
    return meta


# --------------------------------------------------------------------------
# Navigation appendix
# --------------------------------------------------------------------------
def parse_dir_url(url: str) -> dict | None:
    """
    Decompose a Google Maps *directions* URL into its endpoints.

    These are the multi-stop routes the daily place-search links cannot express:
    the Maps URL scheme allows only three waypoints on a mobile browser, which
    is why the author already splits each day into short lettered walks.
    """
    try:
        parts = urlparse(url)
        qs = parse_qs(parts.query)
    except ValueError:
        return None
    if "/maps/dir" not in parts.path:
        return None
    origin = qs.get("origin", [None])[0]
    dest = qs.get("destination", [None])[0]
    if not origin or not dest:
        return None
    raw_wp = qs.get("waypoints", [""])[0]
    waypoints = [unquote_plus(w) for w in raw_wp.split("|") if w] if raw_wp else []
    return {
        "origin": unquote_plus(origin),
        "destination": unquote_plus(dest),
        "waypoints": waypoints,
        "mode": (qs.get("travelmode", [None])[0] or "").lower() or None,
    }


def segment_note(cell_text: str, label: str) -> str | None:
    """
    A bullet may carry a caveat after the link: "<label> - <note>".
    Split on a spaced hyphen so hyphenated labels ("Three-market walk") survive.
    """
    for line in cell_text.split("\n"):
        line = line.strip().lstrip("•").strip()
        if line.startswith(label) and " - " in line:
            return line.split(" - ", 1)[1].strip()
    return None


def attach_nav_segments(doc, days: list, year: int, state: ParseState) -> int:
    """
    Fold the appendix's multi-stop routes onto the days they belong to.

    The day is not inferred: the appendix is a two-column table whose first
    column names the day, so the mapping is stated in the document.
    """
    table = None
    for tbl in doc.tables:
        header = " ".join(c.text for c in tbl.rows[0].cells).upper()
        if "GOOGLE MAPS SEGMENT" in header:
            table = tbl
            break
    if table is None:
        return 0                       # a guide need not have an appendix

    by_date = {d["date"]: d for d in days}
    attached = 0

    for ri, row in enumerate(table.rows[1:], start=1):
        cells = row.cells
        if len(cells) < 2:
            state.fail(f"appendix row {ri}", "expected two columns")
            continue
        day_cell, seg_cell = cells[0], cells[1]

        first_line = next((ln.strip() for ln in day_cell.text.split("\n")
                           if ln.strip()), "")
        m = SEGMENT_DAY_RE.match(first_line)
        if not m:
            state.fail(f"appendix row {ri}",
                       f"cannot read a date from {first_line!r}")
            continue
        month = MONTHS.get(m.group(2).lower())
        if month is None:
            state.fail(f"appendix row {ri}", f"unknown month {m.group(2)!r}")
            continue
        where = date(year, month, int(m.group(3))).isoformat()
        day = by_date.get(where)
        if day is None:
            state.fail(f"appendix row {ri}",
                       f"{where} is not a day in this guide")
            continue

        links = cell_links(seg_cell)
        if not links:
            state.fail(f"appendix {where}", "row has no segment links")
            continue

        for label, url in links:
            parsed = parse_dir_url(url)
            if parsed is None:
                state.fail(f"appendix {where}",
                           f"not a usable directions URL for {label!r}")
                continue
            if parsed["mode"] and parsed["mode"] not in TRAVEL_MODES:
                state.fail(f"appendix {where}",
                           f"unknown travelmode {parsed['mode']!r} on {label!r}")
                continue
            segment = {
                "label": label,
                "mode": parsed["mode"],
                "url": url,
                "origin": parsed["origin"],
                "destination": parsed["destination"],
                "waypoints": parsed["waypoints"],
            }
            note = segment_note(seg_cell.text, label)
            if note:
                segment["note"] = note
            day["nav_segments"].append(segment)
            attached += 1

    covered = sum(1 for d in days if d["nav_segments"])
    if attached and covered < len(days):
        missing = [d["date"] for d in days if not d["nav_segments"]]
        state.fail("appendix", f"no segments for {', '.join(missing)}")
    return attached


def parse_pace(text: str) -> dict:
    out = {"note": text}
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*km", text)
    if m:
        out["km_min"], out["km_max"] = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d+)\s*km", text)
        if m:
            out["km_min"] = out["km_max"] = int(m.group(1))
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def parse(path: str, year: int, state: ParseState) -> list:
    if Document is None:
        raise SystemExit("python-docx is required to parse .docx guides "
                         "(pip install python-docx)")
    doc = Document(path)
    blocks = list(iter_blocks(doc))
    days = []
    i = 0

    while i < len(blocks):
        blk = blocks[i]
        if not isinstance(blk, Paragraph):
            i += 1
            continue
        m = DAY_HEADER_RE.match(blk.text.strip())
        if not m:
            i += 1
            continue

        month = MONTHS.get(m.group(2).lower())
        if month is None:
            state.fail(blk.text, f"unknown month {m.group(2)!r}")
            i += 1
            continue
        d = date(year, month, int(m.group(3)))
        where = d.isoformat()

        day = {"date": where, "title": None, "thesis": None,
               "stops": [], "assignment": [], "nav_segments": []}
        i += 1

        # Heading 1 -> title; following Normal paragraph -> thesis
        while i < len(blocks) and isinstance(blocks[i], Paragraph):
            p = blocks[i]
            txt = p.text.strip()
            if p.style.name == "Heading 1" and day["title"] is None:
                day["title"] = txt
                i += 1
            elif txt.startswith("ROUTE"):
                day["route_summary"] = re.sub(r"^ROUTE\s*", "", txt).strip()
                i += 1
            elif txt and day["thesis"] is None and day["title"]:
                day["thesis"] = txt
                i += 1
            elif not txt:
                i += 1
            else:
                break

        # Tables until the next day header
        while i < len(blocks):
            b = blocks[i]
            if isinstance(b, Paragraph):
                t = b.text.strip()
                if DAY_HEADER_RE.match(t):
                    break
                if t.startswith("ROUTE"):
                    day["route_summary"] = re.sub(r"^ROUTE\s*", "", t).strip()
                elif b.style.name.startswith("Heading"):
                    break
                i += 1
                continue

            header = " ".join(c.text.strip() for c in b.rows[0].cells).upper()

            if "START / FINISH" in header:
                meta = parse_meta_table(b)
                sf = meta.get("START / FINISH", "")
                times = TIME_RE.findall(sf)
                day["window"] = {
                    "label": sf,
                    "start": f"{int(times[0][0]):02d}:{times[0][1]}" if times else None,
                    "finish": f"{int(times[1][0]):02d}:{times[1][1]}" if len(times) > 1 else None,
                    "finish_approx": "about" in sf.lower(),
                }
                day["pace"] = parse_pace(meta.get("WALKING / PACE", ""))
                anchors = meta.get("ANCHORS", "")
                day["anchors"] = [a.strip() for a in anchors.split(";") if a.strip()]
                day["evening_reset"] = meta.get("EVENING RESET") or None

            elif header.startswith("TIME"):
                for ri, row in enumerate(b.rows[1:], start=1):
                    tc, sc = row.cells[0], row.cells[1]
                    loc = f"{where} row {ri}"
                    stop = parse_stop_cell(sc, state, loc)
                    if stop is None:
                        continue
                    stop["time"] = parse_time(tc.text, state, loc)
                    day["stops"].append(stop)

            elif "PHOTOGRAPHIC ASSIGNMENT" in header:
                body = b.rows[0].cells[0].text
                body = re.sub(r"^\s*PHOTOGRAPHIC ASSIGNMENT\s*", "", body)
                items = re.split(r"(?:(?<=^)|(?<=\s))\d+\.\s+", body)
                day["assignment"] = [s.strip() for s in items if s.strip()]

            i += 1

        if not day["stops"]:
            state.fail(where, "day parsed with zero stops")
        days.append(day)

    # A doc with no recognisable day headers must not "succeed" with an empty
    # itinerary -- that is the silent-hole failure mode this parser exists to
    # prevent. (The narrative Scotland doc did exactly this before it was
    # restructured into the dialect.)
    if not days:
        state.fail("document", "no day headers found -- is this doc in the "
                               "field-guide dialect? (expected e.g. 'FRIDAY, AUGUST 7')")

    # The appendix sits after every day, so it is a second pass rather than
    # part of the day loop above.
    state.segments = attach_nav_segments(doc, days, year, state)
    return days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("docx")
    ap.add_argument("--trip-id", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--tz", required=True)
    ap.add_argument("--currency", required=True)
    ap.add_argument("--home-currency", default="USD")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    state = ParseState()
    days = parse(args.docx, args.year, state)

    places = {p["id"]: {k: v for k, v in p.items() if k != "id"}
              for p in state.places.values()}

    doc = {
        "trip": {
            "id": args.trip_id,
            "city": args.city,
            "timezone": args.tz,
            "currency": {"local": args.currency, "home": args.home_currency},
            "start_date": days[0]["date"] if days else None,
            "end_date": days[-1]["date"] if days else None,
        },
        "places": places,
        "days": days,
    }

    if state.problems:
        print(f"\n  {len(state.problems)} PROBLEM(S) — refusing to write output:\n",
              file=sys.stderr)
        for p in state.problems:
            print(f"  [{p.where}] {p.detail}", file=sys.stderr)
        return 1

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)

    stops = sum(len(d["stops"]) for d in days)
    print(f"OK  {len(days)} days, {stops} stops, {len(places)} unique places")
    if state.segments:
        print(f"    {state.segments} navigation segments across "
              f"{sum(1 for d in days if d['nav_segments'])} days")
    else:
        print("    no navigation appendix found -- nav_segments left empty")
    print(f"    -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
