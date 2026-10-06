#!/usr/bin/env python3
"""
fg-walkcheck: does each walk make sense on the map?

Runs in the trips build right after the pins (fg-geocode) and pictures. For
every walk (day.routes[*]), in visiting order, it looks at the pinned stops
and writes what it finds onto the walk:

    route["check"] = {"placed": 6, "total": 8, "problems": ["...", ...]}

The problems are plain English, for the planner's Open questions:

  * stops not on the map -- a walk drawn through 2 of its 8 stops is a lie;
  * a long leg -- consecutive pinned stops more than 1.5 km apart
    ("Leica Store -> Chelsea Market: 25.9 km");
  * a zig-zag -- the path is more than 2.5x the straight line between its
    ends (when the ends are over 200 m apart): a stop out of order or in the
    wrong place. When dropping one stop fixes it, that stop is named;
  * a stray stop -- one pinned far from the rest of its walk (more than 3x
    the walk's median leg and more than 1 km from its nearest walk-mate).

It is a check, not a gate: it never changes a pin, never fails the build,
and prints a readable report for the Action log. Exit code is always 0.

Usage:
    fg-walkcheck trips/*/itinerary.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys

from .geocode import haversine_km, save_json

LEG_KM = 1.5
DETOUR = 2.5
DETOUR_MIN_KM = 0.2
STRAY_X = 3.0
STRAY_KM = 1.0


def _km(km: float) -> str:
    return f"{km * 1000:.0f} m" if km < 1 else f"{km:.1f} km"


def _path_km(pts: list[tuple[float, float]]) -> float:
    return sum(haversine_km(a, b) for a, b in zip(pts, pts[1:]))


def _detour(pts: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """(ratio, path km, straight km), or None when the rule doesn't apply:
    fewer than 3 pins, or ends within DETOUR_MIN_KM (a loop)."""
    if len(pts) < 3:
        return None
    straight = haversine_km(pts[0], pts[-1])
    if straight <= DETOUR_MIN_KM:
        return None
    path = _path_km(pts)
    return path / straight, path, straight


def check_walk(stops: list[tuple[str, tuple[float, float] | None]],
               leg_km: float = LEG_KM, detour: float = DETOUR) -> dict:
    """stops: (name, (lat, lng) or None) in visiting order. Returns the
    route's check: {placed, total, problems}."""
    total = len(stops)
    pinned = [(n, p) for n, p in stops if p]
    problems: list[str] = []

    missing = [n for n, p in stops if not p]
    if missing:
        names = ", ".join(missing[:4]) + (f" and {len(missing) - 4} more" if len(missing) > 4 else "")
        problems.append(f"{len(missing)} of {total} stops aren't on the map: {names}.")

    legs = [(a[0], b[0], haversine_km(a[1], b[1])) for a, b in zip(pinned, pinned[1:])]
    for a, b, km in legs:
        if km > leg_km:
            problems.append(f"Long leg: {a} → {b}: {_km(km)}.")

    pts = [p for _, p in pinned]
    d = _detour(pts)
    if d and d[0] > detour:
        ratio, path, straight = d
        msg = (f"Zig-zag: {_km(path)} of walking to end {_km(straight)} from the start "
               f"({ratio:.1f}× the straight line).")
        fixes = []
        for i in range(len(pinned)):
            rest = pts[:i] + pts[i + 1:]
            if len(rest) < 3:
                continue
            r = _detour(rest)
            if r is None or r[0] <= detour:
                fixes.append((r[0] if r else 1.0, pinned[i][0]))
        if fixes:
            msg += f" Removing {min(fixes)[1]} fixes it."
        problems.append(msg)

    if len(pinned) >= 3:
        med = statistics.median(km for _, _, km in legs)
        limit = max(STRAY_X * med, STRAY_KM)
        for i, (name, p) in enumerate(pinned):
            near = min(haversine_km(p, q) for j, (_, q) in enumerate(pinned) if j != i)
            if near > limit:
                problems.append(f"{name} is pinned {_km(near)} from the rest of the walk.")

    return {"placed": len(pinned), "total": total, "problems": problems}


def walks(doc: dict):
    """(day, route_id, route, [(name, pin)]) for every walk, stops in order."""
    places = doc.get("places", {})
    for day in doc.get("days", []):
        stops = day.get("stops", [])
        routes = day.get("routes") or {}
        for rid, route in (routes.items() if isinstance(routes, dict) else []):
            seq = []
            for j in route.get("stops", []):
                if not (isinstance(j, int) and 0 <= j < len(stops)):
                    continue
                stop = stops[j]
                place = places.get(stop.get("place")) or {}
                c = place.get("coords")
                seq.append((stop.get("label_override") or place.get("name") or str(stop.get("place")),
                            (c["lat"], c["lng"]) if c else None))
            yield day, rid, route, seq


def check_doc(doc: dict) -> list[str]:
    """Write route["check"] on every walk; return the report lines."""
    lines = []
    trip = (doc.get("trip") or {}).get("id", "?")
    for day, _rid, route, seq in walks(doc):
        chk = check_walk(seq)
        route["check"] = chk
        head = f"{trip} {day.get('date')} · {route.get('name')}: {chk['placed']}/{chk['total']} on the map"
        lines.append(head + ("" if chk["problems"] else " — ok"))
        lines += [f"    - {p}" for p in chk["problems"]]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("itineraries", nargs="+", help="itinerary.json file(s)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
    n_walks = n_problem = 0
    for path in args.itineraries:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            lines = check_doc(doc)
            save_json(path, doc)
        except Exception as e:  # noqa: BLE001 -- a check never blocks a trip
            print(f"  walkcheck skipped {path}: {type(e).__name__}: {e}")
            continue
        for line in lines:
            print(line)
            if not line.startswith("    "):
                n_walks += 1
                n_problem += not line.endswith("ok")
    print(f"\nwalkcheck: {n_walks} walk(s), {n_problem} with something to look at")
    return 0


if __name__ == "__main__":
    sys.exit(main())
