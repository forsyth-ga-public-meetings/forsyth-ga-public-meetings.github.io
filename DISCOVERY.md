# Discovery report — gate 1

Run 2026-08-11. Every URL below was actually requested; nothing here is inferred
from platform documentation.

## Summary

Your guessed CivicClerk endpoint was **right**, and it needs no browser. But two
things you did not anticipate change the plan:

1. The CivicClerk API **only exposes meetings whose publish date has passed**
   (~3–4 days before the meeting). It has no future meetings at all. The
   acceptance-test meetings (Aug 20, Aug 25) are not in it today.
2. The county's own site, **forsythco.com, is server-rendered, robots-permissive,
   first-party, and lists those future meetings** — plus 24 boards, not just the
   BOC. It is a better spine for the calendar than CivicClerk.

One source is blocked outright: **Simbli (Board of Education) sets
`Disallow: /` for all user agents.** Under your "honor robots.txt" rule, the BoE
is off the table as currently specified.

---

## 1. CivicClerk (Board of Commissioners) — confirmed, plain HTTP

Base: `https://forsythcoga.api.civicclerk.com/v1/`
OData v4, no authentication, no API key, no browser. Responds to plain GETs.

Endpoints the portal actually fires (captured via Playwright request logging):

| Endpoint | Purpose |
|---|---|
| `GET /v1/Events?$filter=…&$orderby=…` | event list |
| `GET /v1/Events/{eventId}` | one event |
| `GET /v1/Meetings/{agendaId}` | **agenda item tree** |
| `GET /v1/EventCategories` | categories (field is `categoryDesc`) |
| `GET /v1/Events/GetDaysInMonthWithEvents(date='YYYY-MM-DD',categories=[])` | calendar dots |
| `GET /v1/Meetings/GetMeetingFile(fileId=N,plainText=false)` | agenda / packet PDF |
| `GET /v1/Settings/GetPublicPortalCustomizations` | portal branding |

**The agenda is keyed by `agendaId`, not `eventId`.** `Meetings/455` 404s;
`Meetings/389` is the agenda for event 455. This is the single most important
detail for the fetcher, and the starter script gets it wrong.

`$metadata` is served and describes every field. Useful ones on an agenda item:
`agendaObjectItemOutlineNumber`, `agendaObjectItemName`, `childItems`,
`attachmentsList`, `fiscalImpactSummary`, `level`, `isSection`, `sortOrder`.

### Coverage

- **227 events**, 2021-10-12 → 2026-08-11.
- Filtering `startDateTime gt 2026-08-11` returns **0 rows**. Server-side
  filtering on `publishStart` hides unpublished meetings; there is no parameter
  that reveals them. Confirmed against the portal UI, whose August calendar
  shows only the 6th and 11th.

### Sample — event 455, Aug 6 2026 BOC Regular Meeting (`Meetings/389`)

Structure is exactly what the content model needs:

```
VII.        Consent Agenda
  1           Board Ratification of the following items … Work Session held on July 28
    A           …advertise the FY 2027 Proposed Budget and 2027-2031 CIP
    E           …Change Order #1 … in the amount of $11,333.00
    H           …award RFP 26-60-1630 to Blue Cypress Consulting, LLC … $254,200.00
    K           …award RFP 26-51-5211 to Great Southern Recreation, LLC … $249,982.73
    L           …award Bid 26-47-5211 to Playworx Playsets, LLC … $298,350.00
IX.         Time Set Public Hearing - 6:00 p.m.
  1           Public Hearing regarding proposed Fiscal Year 2026 Millage Rates
XII.        New Business
  3           Board consideration and adoption of the 2026 Millage Rates Resolution
```

18 items sit under one ratification block — precisely the "passes without
discussion" case the site exists to surface. Dollar figures are inline in item
names, so the >$100k rule can be applied by regex over the name.

Published files for that meeting:
`Agenda 8.6.2026` (fileId 5247) and `Agenda Packet 8.6.2026 FINAL` (fileId 5256).

### Parser hazards (write tests for these)

- `agendaObjectItemOutlineNumberFull` is frequently `null`; the usable value is
  `agendaObjectItemOutlineNumber`, and the full path must be composed from
  ancestry. **Never synthesise a number when both are absent — render as missing.**
- Some item names contain **raw HTML fragments**, e.g.
  `<p style="margin-left:0in;" data-pasted="true">Board approval of Budget Resolution…`.
  Needs sanitising, not escaping.
- Consent/ratification blocks are identified by item *name* plus tree position,
  not by a flag field. This is the fragile part and deserves the most tests.
- `EventCategories` uses `categoryDesc`, not `name`.

---

## 2. forsythco.com — the county's own site

`robots.txt`: `Disallow:` (empty — everything allowed), plus a Yoast sitemap at
`/sitemap_index.xml`. First-party county content, no vendor ToS in the way.

`/meetings` is **server-rendered HTML** — no JS needed — and lists meetings the
CivicClerk API does not have:

```
August 18, 2026  Planning Commission Work Session            6:30 PM
August 20, 2026  BOC Regular Meeting / Public Hearings       5:00 PM
August 25, 2026  BOC Work Session Meeting                    2:00 PM
September 1, 2026 Zoning Board of Appeals …
```

It also covers **24 bodies** — Board of Assessors, Planning Commission, Zoning
Board of Appeals, Impact Fee Advisory Committee, Ethics Panel, E-911, Land Bank
Authority, and more — where CivicClerk carries only the BOC.

WP REST exists (`/wp-json/wp/v2/meeting`, 547 posts, with `meeting_group` /
`meeting_type` / `meeting_location` taxonomies) but **does not expose the meeting
date** — `acf` is empty and `date` is the post date. Detail pages are per-series,
not per-date. So the per-date data only exists in the rendered `/meetings` HTML
(cards carry `id="card-{meeting_id}"`). Parsing that HTML is unavoidable if we
want future meetings; it is static HTML, so `requests` + `beautifulsoup4` is
enough.

**Correction to PROJECT.md:** agenda PDFs are *not* at
`/portals/0/PNM/Meeting/{meeting_id}/{guid}.pdf`. That pattern returns nothing on
the current site. Real pattern is a CDN path:

```
https://forsythco.b-cdn.net/app/uploads/2026/08/AGENDA-August-13-2026.pdf
https://forsythco.b-cdn.net/app/uploads/2026/08/Work-Session-Agenda-8.18.26.pdf
```

Filenames are human-authored and inconsistent (`8.11.2026`, `08.14.26`,
`August-13-2026`), so they must be harvested from links, never constructed.

---

## 3. Board of Education (Simbli) — blocked

```
https://simbli.eboardsolutions.com/robots.txt
User-agent: *
Disallow: /
```

A blanket prohibition on all automated access. Your instruction is to honor
robots.txt, so **I did not fetch any Simbli content** beyond robots.txt itself.

Their ToS (`simbli.eboardsolutions.com/TERMSOFSERVICE.PDF`, dated 2016-08-02) is
a *subscriber* agreement licensing access "solely for internal business
purposes" — written for the school district, not the public.

`www.forsyth.k12.ga.us` is separately crawlable (`Crawl-delay: 5`, with specific
paths disallowed), so BoE **meeting dates and press releases** are reachable even
though agenda contents are not. That is a real but much thinner deliverable.

---

## 4. Terms of service

### CivicPlus / CivicClerk

Verbatim from https://www.civicplus.com/terms-of-use/:

- Automated access — prohibited only above a rate threshold:
  > "Use or launch any automated system, including without limitation, 'robots,'
  > 'spiders,' or 'offline readers,' that accesses the Site or any Solution in a
  > manner that sends more request messages to the CivicPlus servers in a given
  > period of time than a human can reasonably produce in the same period by
  > using a conventional on-line web browser"

  One poll per day is far under this. **This clause is not a problem.**

- Mirroring — the real friction:
  > "Mirror or frame the Site, Solutions or any part of the foregoing on any
  > other web site or web page."

- Redistribution:
  > "Sell, assign, sublicense, distribute, commercially exploit, grant a security
  > interest in or otherwise transfer any right in, or make available to a third
  > party, the Site Content or Solutions"

- "Site Content" is defined broadly: *"information, writings, images and/or other
  works that you see, hear or otherwise experience on the Site."*
- There is **no carve-out for public records or government-owned content.**

**My read, and you should get a lawyer's if this matters to you:** the mirroring
and redistribution clauses are written to protect CivicPlus's platform, but as
drafted they sweep in the county's agenda text. The counter-argument is that the
agendas are Georgia public records authored and owned by Forsyth County, not by
CivicPlus, and CivicPlus cannot license away the county's records — but that
argument has not been tested and the ToU gives you nothing explicit to stand on.

Practical risk reduction, in order of effectiveness:

1. Prefer **forsythco.com** (first-party, permissive robots, no restrictive ToU)
   as the source of record wherever it carries the same fact.
2. Publish **item titles, numbering and flags** — the index — and link out for
   full text, rather than reproducing agendas wholesale. This is also what you
   described wanting: "an index that points at the county."
3. Do not re-host their PDFs; link to them.
4. Keep the descriptive User-Agent and daily cap.

### eBoardSolutions

Subscriber-only license, and robots.txt forbids crawling regardless. Moot unless
you decide to override the robots rule.

---

## Recommended fetch strategy

**Hybrid, with forsythco.com as the calendar spine and CivicClerk as the detail
source.** Neither alone is sufficient.

| Layer | Source | Method |
|---|---|---|
| Meeting calendar, all 24 bodies, incl. future | `forsythco.com/meetings` | `requests` + `bs4` over static HTML |
| Agenda item tree, numbering, consent flags (BOC) | `…api.civicclerk.com/v1/Meetings/{agendaId}` | plain HTTP JSON |
| Agenda PDF links | both | harvest links, never construct |
| BoE dates / press releases | `forsyth.k12.ga.us` | `requests`, crawl-delay 5 |
| BoE agenda contents | — | **blocked by robots.txt** |

Join key: match a forsythco.com meeting to a CivicClerk event on
(body, date, start time). Both agree on Aug 6 and Aug 11, so the join is testable
today.

**Playwright is not needed for production** — only for re-running discovery when
something breaks. Every production URL above works with plain `requests`.

---

## Consequence for the acceptance test

The Aug 20 regular meeting and Aug 25 work session are **real and scheduled** —
both appear on forsythco.com and both match your stated cadence (3rd Thursday,
4th Tuesday). But their **agendas are not published yet**. CivicClerk will
expose them around **Aug 17** and **Aug 21** respectively.

So today the site can correctly show those two meetings as *scheduled, agenda not
yet posted*, and the item-level assertions in your acceptance test — numbering,
consent flagging, the $239.4M FY2027 budget adoption — can only be checked after
those dates. I am not going to fabricate the items to make the test pass.

There is corroborating signal already visible in the Aug 6 agenda: item VII.1.A,
*"Board authorization to advertise the FY 2027 Proposed Budget and 2027-2031
Capital Improvement Program (CIP)"*, which is the statutory advertisement step
that precedes adoption. That is consistent with a budget adoption on Aug 20, but
it is not proof, and I have not asserted the dollar figure anywhere.

Suggested substitute for validating the pipeline now: run the acceptance test
against **Aug 6 (event 455 / agenda 389)** and **Jul 28 (event 433 / agenda 367)**
— same bodies, same structure, agendas fully published, and the Aug 6 agenda
contains a large ratification block plus millage items that exercise every flag.
Then re-run the real Aug 20 / Aug 25 test after the 21st.
