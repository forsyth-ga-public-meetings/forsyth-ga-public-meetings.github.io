# Project: Forsyth County, GA meeting-agenda mirror

## What I want you to build

A small, boring, static website that mirrors public meeting agendas for Forsyth
County, Georgia local government, so that they are crawlable by search engines
and readable without executing JavaScript.

The county's agenda portals are JavaScript single-page apps. As a result, almost
nothing about them is indexed — a search today returns agendas that are six
weeks stale, and the current month is invisible. That is the problem. The site
is an *index that points at the county*, not a replacement for it.

Deliverables:

1. A fetcher that pulls meeting metadata and agenda text from the sources below
   into a local JSON/SQLite cache.
2. A static site generator that turns that cache into plain HTML.
3. A change-detection step that reports what's new or modified since last run.

## Sources

**Forsyth County Board of Commissioners**
- Portal (JS SPA): https://forsythcoga.portal.civicclerk.com
- Meetings landing page: https://www.forsythco.com/meetings
- CivicClerk is a React front end over an OData API. The tenant is `forsythcoga`.
  I *believe* the API lives at `https://forsythcoga.api.civicclerk.com/v1/Events`
  with standard OData params (`$filter`, `$orderby`, `$top`, `$expand`), but
  **this is unverified — do not build around it until you have confirmed it.**
- Individual event pages follow:
  `https://forsythcoga.portal.civicclerk.com/event/{id}/files/agenda/{agenda_id}`
- **Important:** agenda PDFs are also served as plain static files at
  `https://www.forsythco.com/portals/0/PNM/Meeting/{meeting_id}/{guid}.pdf`.
  These *are* already indexed by search engines. If the OData route is painful,
  harvesting these PDF URLs from the meetings list is a shorter path. Evaluate
  both and tell me which is more robust before committing.
- Schedule: regular meetings 1st and 3rd Thursdays; work sessions 2nd and 4th
  Tuesdays.

**Forsyth County Board of Education**
- Agendas (Simbli / eBoardSolutions, ASP.NET): https://simbli.eboardsolutions.com/index.aspx?S=4069
- Policies: https://simbli.eboardsolutions.com/Policy/PolicyListing.aspx?S=4069
- Meeting schedule: https://www.forsyth.k12.ga.us/inside-fcs/board-of-education/meeting-schedule
- Press releases (millage/budget notices land here):
  https://www.forsyth.k12.ga.us/district-services/communications/press-release
- Simbli may respond to plain HTTP GETs. Check before reaching for a browser.

**Secondary (lower priority, add only if the above works cleanly)**
- City of Cumming council agendas
- Forsyth County Board of Assessors deadlines: https://www.forsythco.com/government/departments/board-of-assessors/property-assessments/

## Step 1 before anything else

Do not guess at endpoints. For each portal, load it in headless Chromium with
Playwright, log every XHR/fetch request the page fires, and report back what you
found. I have a starter script (`foco_agendas.py`) with a `discover` subcommand
that does exactly this — read it, use it or replace it, your call.

Once you know the real API, prefer plain HTTP over a headless browser everywhere
you can. Browser rendering is the fallback, not the default.

## Content model

One page per meeting. Each meeting page must have:

- Body name, meeting type, date, time, location
- Agenda items with their original numbering preserved
- A clear visual marker on any item that sits under a **consent agenda** or
  ratification block — this is the whole point, these pass without discussion
- A marker on any item with a dollar figure over $100,000 or a term longer than
  one year
- A prominent link to the county's own page for that meeting, labeled as the
  authoritative source
- A "fetched at" timestamp with timezone on every page

Also generate:

- An index page sorted by date, upcoming first
- Topic pages for: taxes/millage/SPLOST, policing & surveillance, schools,
  ballot/referendum
- `sitemap.xml`
- An RSS feed (new meetings + changed agendas)
- A plain-text `llms.txt` at the root summarizing what the site is and where the
  canonical sources live

## Topic tagging

Tag items matching these clusters. Keep the keyword list in a separate config
file so I can edit it without touching code.

- **Taxes:** millage, rollback rate, tax digest, homestead exemption, budget
  resolution, LHOST, FLOST, SPLOST, ESPLOST, TSPLOST, bond, referendum,
  impact fee, assessment appeal
- **Policing & surveillance:** Flock, ALPR, license plate reader, speed camera,
  photo enforcement, red light camera, surveillance, drone, Axon, Motorola,
  data-sharing agreement, interoperability, Sheriff's Office grant, GEMA,
  Homeland Security grant
- **Schools:** curriculum, instructional materials, media center, library
  policy, board policy revision, school calendar, millage
- **Ballot:** referendum, ballot question, special election, qualifying period

## Tech constraints

- Python. Boring stdlib + `requests` + `beautifulsoup4`; Playwright only where
  genuinely required.
- Static output. No server-side rendering, no database at serve time. Must
  deploy to GitHub Pages or Netlify from a directory of files.
- No JS framework. Semantic HTML, one small stylesheet. The entire point is that
  this site works without JavaScript — do not reintroduce the problem.
- Real dates in the markup (`<time datetime="...">`). Schema.org `Event` markup
  where it fits.
- Stable, permanent URLs. Never renumber or reslug an existing meeting page.

## Politeness and legal

- Descriptive User-Agent with contact info. Make it a config value.
- One poll per day maximum. Cache aggressively; never re-fetch an unchanged
  agenda.
- Honor `robots.txt`.
- **Read the terms of service for both CivicClerk and eBoardSolutions and report
  back what they say before we publish anything.** Georgia public records law
  covers the underlying documents; the vendors' ToS is a separate question and I
  want to know the answer up front, not after launch.

## Things not to do

- Do not invent agenda item numbers, dates, or dollar figures. If a field is
  missing from the source, render it as missing.
- Do not present this site as authoritative for legal deadlines (appeal windows,
  comment periods, registration cutoffs). Link to the county for those, every
  time.
- Do not add analytics, tracking, ads, or a newsletter signup.
- Do not build the whole thing before validating the data source. See below.

## How I want you to work

Incrementally, checking in at each gate:

1. **Discovery.** Report what APIs actually exist for both portals, with sample
   responses. Recommend a fetch strategy. Stop and wait for my go-ahead.
2. **Fetcher.** Pull the last 60 days and next 60 days into a local cache.
   Show me the data before generating any HTML.
3. **One page.** Generate a single meeting page. I'll review the layout.
4. **Full generator** + index + topic pages + sitemap + RSS.
5. **Change detection** + a cron-ready entrypoint that emails or writes a diff.

Write tests for the parsing layer — that's where this will silently rot when the
vendor changes their markup. A parser that fails loudly is worth more than one
that quietly returns empty agendas.

## Acceptance test

Fetch the Board of Commissioners work session on Tuesday, August 25, 2026 and
the regular meeting on Thursday, August 20, 2026. The August 20 meeting is
expected to include adoption of the county's proposed $239.4 million 2027
budget. If the generated pages show those items with correct numbering, correct
consent-agenda flagging, and a working link back to the county's page, it works.
