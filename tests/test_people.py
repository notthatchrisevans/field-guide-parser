"""People on a trip (Oct 2026): `people:` and `example:` in front matter.

`people:` is who may open the trip (sign-in ids); no line means Chris only.
`example: true` marks the shared example trip. Bad values fail loudly."""

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

HEAD = ("---\ntrip: t-2027-01\ncity: T\ntimezone: Europe/London\n"
        "currency: GBP\nyear: 2027\n")
BODY = ("---\n\n## FRIDAY, JANUARY 1\n### Day\n> Thesis.\n\n"
        "- Morning | A [public]\n  - where: A, T\n")


def parse(tmp_path, front_extra):
    src = tmp_path / "t.md"
    src.write_text(HEAD + front_extra + BODY, encoding="utf-8")
    out = tmp_path / "o.json"
    r = subprocess.run(
        [sys.executable, "-m", "fieldguide_parser.parse_guide_md", str(src),
         "--out", str(out)],
        capture_output=True, text=True, cwd=ROOT)
    doc = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return r, doc


def ok(tmp_path, front_extra):
    r, doc = parse(tmp_path, front_extra)
    assert r.returncode == 0, r.stderr
    return doc["trip"]


def fails(tmp_path, front_extra, needle):
    r, doc = parse(tmp_path, front_extra)
    assert r.returncode == 1, "expected a hard failure"
    assert doc is None
    assert needle in r.stderr, r.stderr


def test_no_people_line_is_chris_only(tmp_path):
    trip = ok(tmp_path, "")
    assert trip["people"] == ["chris"]
    assert "example" not in trip


def test_people_list(tmp_path):
    trip = ok(tmp_path, "people: chris, debby, susie-2\n")
    assert trip["people"] == ["chris", "debby", "susie-2"]


def test_people_everyone(tmp_path):
    assert ok(tmp_path, "people: everyone\n")["people"] == "everyone"


def test_people_comment_and_spacing(tmp_path):
    assert ok(tmp_path, "people:  debby ,chris  # who can open it\n")["people"] \
        == ["debby", "chris"]


def test_example_true(tmp_path):
    trip = ok(tmp_path, "people: everyone\nexample: true\n")
    assert trip["example"] is True and trip["people"] == "everyone"


def test_example_false_is_omitted(tmp_path):
    assert "example" not in ok(tmp_path, "example: false\n")


def test_bad_person_id(tmp_path):
    fails(tmp_path, "people: chris, Debby\n", "not a person id")


def test_person_id_with_space(tmp_path):
    fails(tmp_path, "people: chris, debby smith\n", "not a person id")


def test_semicolons_are_not_separators(tmp_path):
    fails(tmp_path, "people: chris; debby\n", "not a person id")


def test_duplicate_person(tmp_path):
    fails(tmp_path, "people: chris, chris\n", "appears twice")


def test_empty_people(tmp_path):
    fails(tmp_path, "people:\n", "people: is empty")


def test_everyone_in_a_list(tmp_path):
    fails(tmp_path, "people: chris, everyone\n", "stands alone")


def test_bad_example_value(tmp_path):
    fails(tmp_path, "example: maybe\n", "is not true or false")
