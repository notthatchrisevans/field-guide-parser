# Field Guide trip template

Copy this file, fill it in, and hand it to `scripts/parse_guide_md.py`.
Everything above the first `## DAY` header is instructions and is ignored by
the parser — you can leave it in place or delete it once the trip is filled.

## Instructions (for the person or AI filling this in)

You are filling in a travel itinerary that feeds a navigation app. The app is
used on a phone, mid-trip, to get to real places — a wrong "fact" here gets
acted on in the street. These rules are not style preferences; the parser
enforces them and refuses files that break them.

**The one absolute rule: never invent anything.** No fabricated opening
hours, addresses, booking references, prices, or URLs. If you don't know or
can't verify something, leave the field out, or mark the whole stop with a
`review:` line saying what needs checking. A blank is recoverable; a
plausible-looking wrong value is not.

**How to return your answer (for the AI): one fenced code block, raw.**
Reply with the complete file inside a single markdown code fence, starting
at the `---` front-matter line — no prose before or after the fence. This
matters because the person will copy your answer into a file: if you reply
with rendered markdown instead of a code block, the copy destroys the
format (`---` fences become ornaments, `## ` headers lose their hashes,
`[links](urls)` collapse to bare words) and the parser will reject the
file. The person copying: use the code block's copy button, never select
the pretty text.

### Structure

1. **Front matter** (the block between `---` lines): trip id
   (`<place>-<yyyy>-<mm>`), city (display name), IANA timezone
   (`Europe/London`), local currency code, year. `home_currency` defaults
   to USD.
2. **One `##` header per day**: `## FRIDAY, MAY 7` — weekday name, comma,
   month name, day number. The parser checks the weekday against the actual
   date and rejects mismatches, so don't guess dates.
3. After the day header: a `###` title line, then a `>` thesis line (one
   sentence that names what the day is about). Optionally, day-fact lines:
   `Window:`, `Anchors:` (semicolon-separated fixed commitments), `Pace:`,
   `Reset:`, `Route:` (one-line route summary), `Aim:` (one goal per line,
   repeatable), and `Leg: [<label>](<google-maps-directions-url>) — <note>`
   (repeatable — multi-stop walking routes; the URL must be a Maps
   *directions* link with origin= and destination=). Put all of these before
   the day's first stop bullet.
4. **One `- ` bullet per stop**, in time order:

       - TIME | Display Name [Category]
         - where: <Google Maps search string — see below>
         - notes: <free text; repeat the line for more notes>

### Time

Use 24-hour clock. Allowed forms: `14:00`, `14:00–15:15`, `About 09:30`
(approximate), or a daypart/meal word alone: `Morning`, `Midday`,
`Afternoon`, `Evening`, `Breakfast`, `Lunch`, `Dinner`, or `After <thing>`.
Anything else is an error. Do not put a clock time on something that doesn't
truly have one — untimed is a valid state.

### Categories (closed list — anything else is an error)

market, working, residential, waterfront, public, nature, shop,
decay, gallery, performance, temple, transit, airport,
food, airbnb, hotel, friends, spa, lounge

Lodging is one of `airbnb`, `hotel`, or `friends` (you can also write
`[Friend's House]`) — pick the one that's true; there is no generic
lodging tag. `spa` covers hammams, bathhouses, and anything of that kind.

### The `where:` line (required on every stop)

This string IS the place's identity: the same place across multiple visits
must use the *identical* string, and it doubles as the Google Maps search the
app links to. Format: `Venue Name, Town` (add region/country if ambiguous).
It gets geocoded later — precise beats pretty.

### Per-category content rules

- **airbnb / hotel / friends** (the lodging kinds) — REQUIRED `address:`
  line: the street address exactly as you would give it to a cab driver or
  type into Uber. The parser hard-fails on lodging without one.
- **transit** — two kinds. A transit *place* (metro station, ferry terminal,
  interchange you pass through) needs nothing extra. A *ride* (a flight,
  train, ferry, coach you take) MUST carry a journey block, and any journey
  field marks the stop as a ride: `carrier:` (airline/railway/ferry operator)
  is then required, and when the stop has a clock time `departs:` and
  `arrives:` are required too (24h clock, must agree with the stop's own
  time range). On an open-time ride (weather-dependent ferry) they're
  advisories — add them when the sailing is chosen. Add when they exist:
  `service:` (flight/train number, e.g. UA929), `boarding:` (24h clock),
  `seat:` (e.g. Coach K, 22A) — advisories when absent; seats often aren't
  assigned until check-in. NEVER invent any of these; add them to this file
  when they're known. Booking references go in `notes:`.
- **food** (restaurants, cafés, bakeries) — add `links: [Menu](url)` if a
  menu is online. If none exists, omit it — never link a lookalike.
- **performance** (shows, concerts, events) — add `links: [Event](url)`
  pointing at a write-up of the event/show if one exists.
- **gallery** and other venues (museums, institutions) — add
  `links: [Venue](url)` pointing at the venue's page.
- Any stop may carry extra links; label them honestly (`Tickets`,
  `Timetable`, `Info`).

Link labels matter: `Menu`, `Venue`, `Website`, `Info` attach to the *place*
(shown on every visit); `Event`, `Tickets` and everything else stay on that
one stop.

### Travelers, choices, routes and images (all optional)

**Travelers.** Add `travelers: Chris; Debbie` to the front matter when more
than one person travels. Then any stop can say `- who: chris` (or
`who: chris, debbie`); a stop without `who:` is for everyone. Only names
from the roster are allowed. Without a roster, leave `who:` out entirely.

**Choices.** Two or more stops in the same day with the same
`- choice: <id>` line are alternatives — pick one, not all. At most one of
them may carry `- default: yes` (the suggestion). A choice is a choice, not
an extra commitment: never put a booked thing inside one.

**Routes.** A walking route is one `[route]` stop whose places are nested
`stop:` lines in walking order. The route line has no `where:`; each
nested stop does. Nested stops are `[public]` unless their name carries a
category; `next:` on a nested stop is how to reach the following stop, in
words. A route's id is its name's slug; add `- id: <slug>` when the route
is part of a choice, so renaming it later cannot detach a decision.

    - Afternoon | Shooting walk: 45th Street to 53rd [route]
      - who: chris
      - choice: monday-afternoon
      - default: yes
      - notes: Two to three hours; shorten freely.
      - stop: 45th Street to Madison
        - where: Vanderbilt Avenue and East 45th Street, Manhattan, NY
        - what: Window layers and people framed by entrances.
        - next: North on Madison toward 49th.
      - stop: Madison Avenue: shop windows
        - where: Madison Avenue and East 45th Street, Manhattan, NY

    - Afternoon | MoMA [gallery]
      - where: 11 West 53rd Street, New York, NY 10019
      - who: chris
      - choice: monday-afternoon
      - links: [Venue](https://www.moma.org/)

**Images.** An `image:` block under any stop (or nested stop). `alt:` is
required and doubles as the caption. `opens:` is where a tap goes: `website`
(the place's Website/Venue link), `maps` (the place's map search),
`show <url>`, `artist <url>`, or a bare url. `website` with no known site
falls back to the map. `source:` is where the picture came from, which is
not necessarily where it opens, and is required for any real file;
`credit:` and `date:` are optional; `example: yes` marks a picture that
illustrates an artist rather than the actual show. `image: none` makes a text tile that only links out — use it
when no picture can be reused honestly. Never invent an image or a url.

      - image: images/nyc-2026-09/moma-entrance.jpg
        - alt: MoMA entrance on West 53rd Street
        - opens: website
        - source: https://press.moma.org/
        - credit: MoMA press office

The parser treats a missing menu/event/venue link as an advisory (listed, not
fatal) because "if available" can legitimately mean "there isn't one" — but a
missing lodging address is an error, because there is always an address.

### Optional stop fields

    - name: <place display name, if different from the stop's display name>
    - what: <one line on what this stop is / why it's here>
    - next: <how to get to the next stop, e.g. "Leave 12:05 (15 min • arr. 12:20)">
    - review: <anything assumed or undecided that a human must confirm>

Every `review:` line survives into the app as a visible ⚠ flag — use it
freely for anything you were not sure about. That is the honest channel.

---
trip: example-2027-01
city: Example
timezone: Europe/London
currency: GBP
year: 2027
---

## FRIDAY, JANUARY 1
### Example Day — replace everything from here down
> One sentence that names what this day is about.
Anchors: Booked Restaurant 19:00; Booked Show 21:00

- Morning | Some Bakery [food]
  - where: Some Bakery, Exampletown
  - links: [Menu](https://example.com/menu)
  - notes: Why this stop is worth it.
  - next: Leave 09:15 (10 min • arr. 09:25)

- 10:00–12:00 | An Exhibition — Some Gallery [gallery]
  - where: Some Gallery, Exampletown
  - name: Some Gallery
  - links: [Venue](https://example.com/gallery)
  - what: What's on and why it made the list.

- 14:30–16:45 | Example Rail — Exampletown → Otherville [transit]
  - where: Exampletown Station, Exampletown
  - carrier: Example Rail
  - service: XR 1234
  - boarding: 14:10
  - departs: 14:30
  - arrives: 16:45
  - seat: Coach B, 41A
  - notes: res ABC123 • sit on the left for the views.

- 19:00 | Booked Restaurant [food]
  - where: Booked Restaurant, Exampletown
  - links: [Menu](https://example.com/dinner)
  - notes: ✅ booked — cancellation terms if any.
  - review: Confirm the table time against the booking email.

- Evening | The Flat [airbnb]
  - where: 1 Example Street, Exampletown
  - address: 1 Example Street, Exampletown EX1 2AB
  - notes: Check-in details.
