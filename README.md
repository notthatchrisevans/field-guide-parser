# field-guide-parser

The single source of truth for the Field Guide trip dialect: the
[template](templates/trip-template.md) is the spec, this parser is its
enforcement, and they version together in this repo.

**Fail loudly is the whole philosophy.** This feeds a navigation app used
in the street: an unknown category, a weekday that doesn't match its
date, lodging without an address — hard errors, nothing written. A
missing menu link is an advisory, because forcing it would teach an LLM
to fabricate a URL. Nothing gets invented; anything uncertain carries a
`review:` line instead.

## Install

```
pip install git+https://github.com/notthatchrisevans/field-guide-parser
```

Stdlib only. (`python-docx` is needed only for the retired `.docx` path:
`pip install "fieldguide-parser[docx] @ git+..."`.)

## Use

```
fg-parse source/kyoto-2027-04.md                 # writes trips/<id>/itinerary.json
fg-parse source/kyoto-2027-04.md --out x.json
fg-parse source/kyoto-2027-04.md --strict        # advisories become errors
```

Or `python -m fieldguide_parser.parse_guide_md ...`.

## Consumers

- **field-guide** (private) — the app's build pipeline
- **field-guide-trips** (private) — the planning repo's validating Action
- **Bib** — the household's operations agent, when editing trip files

All three install from here. Vendored copies are how drift happens; the
week of 2026-09-08 synced hand-copies four times before this repo existed.

## For AIs filling in a trip

Fetch [templates/trip-template.md](https://raw.githubusercontent.com/notthatchrisevans/field-guide-parser/main/templates/trip-template.md)
— it carries its own instructions. Return the complete file as ONE raw
markdown code block, never invent facts, and mark anything uncertain with
a `review:` line.
