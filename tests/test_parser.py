"""Smoke tests: the template's own example day is the happy-path fixture,
and the rendered-copy diagnostics fire with their teaching messages."""

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "trip-template.md"


def run(args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-m", "fieldguide_parser.parse_guide_md", *args],
        capture_output=True, text=True, cwd=cwd)


def test_default_output_lands_in_cwd(tmp_path):
    # Regression: with the package installed, the default out path must be
    # caller-cwd-relative, not package-relative (2026-09-15).
    r = run([str(TEMPLATE)], cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "trips" / "example-2027-01" / "itinerary.json").exists()


def test_template_example_parses(tmp_path):
    out = tmp_path / "itinerary.json"
    r = run([str(TEMPLATE), "--out", str(out)])
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["trip"]["id"] == "example-2027-01"
    assert len(doc["days"]) == 1
    assert len(doc["days"][0]["stops"]) == 5


def test_rendered_copy_is_diagnosed(tmp_path):
    # Simulate a chat reply copied from its rendered view: fences become
    # an ornament, day headers lose their hashes.
    broken = tmp_path / "broken.md"
    broken.write_text(
        "⸻\ntrip: x-2027-01\ncity: X\ntimezone: Europe/London\n"
        "currency: GBP\nyear: 2027\n\nFRIDAY, JANUARY 1\n",
        encoding="utf-8")
    r = run([str(broken), "--out", str(tmp_path / "o.json")])
    assert r.returncode == 1
    assert "RENDERED" in r.stderr


def test_star_bullets_accepted(tmp_path):
    src = tmp_path / "stars.md"
    src.write_text(
        "---\ntrip: y-2027-01\ncity: Y\ntimezone: Europe/London\n"
        "currency: GBP\nyear: 2027\n---\n\n## FRIDAY, JANUARY 1\n"
        "### Day\n> Thesis.\n\n"
        "* Morning | Somewhere [public]\n"
        "  * where: Somewhere, Y\n",
        encoding="utf-8")
    r = run([str(src), "--out", str(tmp_path / "o.json")])
    assert r.returncode == 0, r.stderr
