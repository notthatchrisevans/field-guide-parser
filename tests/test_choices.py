"""Participants, choices, routes and images (Sep 2026).

Two promises: files that use none of it parse to byte-identical output
(the snapshot fixtures), and every way to get it wrong fails loudly with a
message that names the mistake."""

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
TEMPLATE = ROOT / "templates" / "trip-template.md"

FRONT = ("---\ntrip: t-2027-01\ncity: T\ntimezone: Europe/London\n"
         "currency: GBP\nyear: 2027\ntravelers: Chris; Debbie\n---\n\n"
         "## FRIDAY, JANUARY 1\n### Day\n> Thesis.\n\n")

ROUTE = (
    "- Afternoon | Shooting walk [route]\n"
    "  - who: chris\n"
    "  - choice: friday-afternoon\n"
    "  - default: yes\n"
    "  - notes: Two hours; shorten freely.\n"
    "  - stop: First corner\n"
    "    - where: First Corner, T\n"
    "    - what: Windows and people.\n"
    "    - next: North two blocks.\n"
    "    - image: images/t-2027-01/corner.jpg\n"
    "      - alt: The corner at dusk\n"
    "      - opens: maps\n"
    "      - source: https://example.com/photo\n"
    "  - stop: The square [public]\n"
    "    - where: The Square, T\n"
    "    - what: Figures against stone.\n"
    "\n"
    "- Afternoon | The Museum [gallery]\n"
    "  - where: The Museum, T\n"
    "  - who: chris\n"
    "  - choice: friday-afternoon\n"
    "  - links: [Venue](https://museum.example/)\n"
    "  - image: images/t-2027-01/museum.jpg\n"
    "    - alt: Museum entrance\n"
    "    - opens: website\n"
    "    - source: https://museum.example/press\n"
    "  - image: none\n"
    "    - alt: Artist example\n"
    "    - opens: artist https://artist.example/\n"
    "    - example: yes\n"
    "\n"
    "- 20:00 | Dinner [food]\n"
    "  - where: Dinner Place, T\n"
    "  - links: [Menu](https://dinner.example/menu)\n")


def run(args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-m", "fieldguide_parser.parse_guide_md", *args],
        capture_output=True, text=True, cwd=cwd)


def parse(tmp_path, text):
    src = tmp_path / "t.md"
    src.write_text(text, encoding="utf-8")
    out = tmp_path / "o.json"
    r = run([str(src), "--out", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return r, doc


def _norm(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_template_output_unchanged(tmp_path):
    out = tmp_path / "o.json"
    r = run([str(TEMPLATE), "--out", str(out)])
    assert r.returncode == 0, r.stderr
    assert _norm(out) == _norm(FIX / "template-expected.json")


def test_real_trip_output_unchanged(tmp_path):
    out = tmp_path / "o.json"
    r = run([str(FIX / "nyc-2026-09.md"), "--out", str(out)])
    assert r.returncode == 0, r.stderr
    assert _norm(out) == _norm(FIX / "nyc-expected.json")


def test_happy_path_shape(tmp_path):
    r, doc = parse(tmp_path, FRONT + ROUTE)
    assert r.returncode == 0, r.stderr
    assert doc["trip"]["travelers"] == [{"id": "chris", "name": "Chris"},
                                        {"id": "debbie", "name": "Debbie"}]
    day = doc["days"][0]
    # flat stops, in order: two route stops, the museum, dinner
    names = [doc["places"][s["place"]]["name"] for s in day["stops"]]
    assert names == ["First corner", "The square", "The Museum", "Dinner"]
    route = day["routes"]["shooting-walk"]
    assert route["stops"] == [0, 1]
    assert route["who"] == ["chris"] and route["choice"] == "friday-afternoon"
    assert route["notes"] == ["Two hours; shorten freely."]
    s0, s1, museum, dinner = day["stops"]
    assert s0["route"] == "shooting-walk" and s0["who"] == ["chris"]
    assert s0["choice"] == "friday-afternoon"
    assert s0["time"]["label"] == "Afternoon"
    assert s0["next"] == {"raw": "North two blocks."}
    assert doc["places"][s1["place"]]["category"] == "public"
    assert doc["places"][s0["place"]]["category"] == "public"   # inherited default
    assert "route" not in dinner and "who" not in dinner and "choice" not in dinner
    choice = day["choices"]["friday-afternoon"]
    assert choice["default"] == "shooting-walk"
    assert choice["who"] == ["chris"]
    assert [o["kind"] for o in choice["options"]] == ["route", "stop"]
    assert choice["options"][1]["id"] == museum["place"]
    assert choice["options"][1]["ref"] == 2
    # images
    img = s0["images"][0]
    assert img["opens"] == "maps" and img["url"] == doc["places"][s0["place"]]["maps_url"]
    assert img["source"] == "https://example.com/photo"
    m0, m1 = museum["images"]
    assert m0["opens"] == "website" and m0["url"] == "https://museum.example/"
    assert m1 == {"src": None, "alt": "Artist example", "opens": "artist",
                  "url": "https://artist.example/", "example": True}


def test_who_everyone_is_omitted(tmp_path):
    text = FRONT + ("- Morning | A [public]\n  - where: A, T\n"
                    "  - who: Chris, Debbie\n")
    r, doc = parse(tmp_path, text)
    assert r.returncode == 0, r.stderr
    assert "who" not in doc["days"][0]["stops"][0]


def _fails(tmp_path, text, needle):
    r, _ = parse(tmp_path, text)
    assert r.returncode == 1, "expected a hard failure"
    assert needle in r.stderr, r.stderr


def test_unknown_traveler(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n  - who: Mary\n",
           "not in travelers")


def test_who_without_roster(tmp_path):
    text = FRONT.replace("travelers: Chris; Debbie\n", "") + \
        "- Morning | A [public]\n  - where: A, T\n  - who: chris\n"
    _fails(tmp_path, text, "needs a `travelers:` roster")


def test_choice_with_one_option(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n  - choice: x\n",
           "only one option")


def test_two_defaults(tmp_path):
    text = FRONT + ("- Morning | A [public]\n  - where: A, T\n  - choice: x\n  - default: yes\n"
                    "- Morning | B [public]\n  - where: B, T\n  - choice: x\n  - default: yes\n")
    _fails(tmp_path, text, "at most one")


def test_default_without_choice(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n  - default: yes\n",
           "has no `choice:`")


def test_choice_reused_across_days(tmp_path):
    text = FRONT + ("- Morning | A [public]\n  - where: A, T\n  - choice: x\n"
                    "- Morning | B [public]\n  - where: B, T\n  - choice: x\n"
                    "\n## SATURDAY, JANUARY 2\n### Day\n> T.\n"
                    "- Morning | C [public]\n  - where: C, T\n  - choice: x\n"
                    "- Morning | D [public]\n  - where: D, T\n  - choice: x\n")
    _fails(tmp_path, text, "already used on 2027-01-01")


def test_route_without_stops(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | Walk [route]\n  - notes: hm\n",
           "has no `stop:` lines")


def test_route_stop_without_where(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | Walk [route]\n  - stop: A\n    - what: x\n",
           "has no `where:` line")


def test_route_with_where(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | Walk [route]\n  - where: X, T\n  - stop: A\n    - where: A, T\n",
           "has no place of its own")


def test_stop_under_non_route(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n  - stop: B\n    - where: B, T\n",
           "belong under a [route]")


def test_image_without_alt(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n  - image: x.jpg\n    - opens: maps\n",
           "no `alt:`")


def test_image_website_falls_back_to_maps(tmp_path):
    r, doc = parse(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n"
                   "  - image: none\n    - alt: a\n    - opens: website\n")
    assert r.returncode == 0, r.stderr
    img = doc["days"][0]["stops"][0]["images"][0]
    assert img["opens"] == "maps" and img["url"].startswith("https://www.google.com/maps/")


def test_image_file_needs_source(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n  - image: x.jpg\n    - alt: a\n    - opens: maps\n",
           "no `source:`")


def test_route_explicit_id(tmp_path):
    r, doc = parse(tmp_path, FRONT + "- Morning | Walk [route]\n  - id: monday-walk\n"
                   "  - stop: A\n    - where: A, T\n")
    assert r.returncode == 0, r.stderr
    assert list(doc["days"][0]["routes"]) == ["monday-walk"]
    assert doc["days"][0]["stops"][0]["route"] == "monday-walk"


def test_route_id_must_be_slug_and_unique(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | Walk [route]\n  - id: Not A Slug\n  - stop: A\n    - where: A, T\n",
           "must be a slug")
    _fails(tmp_path, FRONT + ("- Morning | Walk [route]\n  - id: w\n  - stop: A\n    - where: A, T\n"
                              "- Afternoon | Walk two [route]\n  - id: w\n  - stop: B\n    - where: B, T\n"),
           "already used")


def test_id_on_plain_stop_rejected(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n  - id: a\n",
           "is for a [route]")


def test_image_unknown_field(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | A [public]\n  - where: A, T\n  - image: x.jpg\n    - alt: a\n    - opens: maps\n    - caption: no\n",
           "unknown image field")


def test_route_stop_unknown_category(tmp_path):
    _fails(tmp_path, FRONT + "- Morning | Walk [route]\n  - stop: A [lodging]\n    - where: A, T\n",
           "unknown category")
