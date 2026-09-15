"""Field Guide trip parser — the single source of truth for the dialect.

The fail-loud parser behind the Field Guide travel app and its planning
repo: markdown trip files in, itinerary.json out, and hard errors for
anything that would put an invented fact in front of someone navigating
a city. The template in templates/ is the spec; this package is its
enforcement — they version together.

Consumers install from git and call `fg-parse <trip.md>` (or
`python -m fieldguide_parser.parse_guide_md`). Vendored copies are how
drift happens; don't.
"""

from .parse_guide import CATEGORY_MAP, MONTHS, ParseState  # noqa: F401

__version__ = "1.0.0"
