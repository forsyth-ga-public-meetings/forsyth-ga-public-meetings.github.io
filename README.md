# Forsyth County, GA meeting-agenda index

A static, JavaScript-free index of public meeting agendas for Forsyth County,
Georgia — built so that search engines and AI assistants can actually read them.

The county's agenda portals are JavaScript applications. Anything that cannot
execute scripts sees an empty shell, so a resident asking an assistant "what's
on the agenda this month" gets stale information or none. This site mirrors the
*index* of those agendas as plain HTML and links back to the county for the
documents themselves. **It is not authoritative. The county is.**

## What it covers

| | |
|---|---|
| Meetings | 68 in a ±60-day window, archived permanently thereafter |
| Boards | 17, including the Board of Education and City of Cumming |
| Item-level agendas | 38 meetings |
| Sources | CivicClerk OData API, forsythco.com, forsyth.k12.ga.us, cityofcumming.net |

Agenda items are flagged where they sit under a **consent agenda** (voted as a
block, normally without discussion), mention a figure at or above **$100,000**,
or suggest a **commitment longer than a year**.

## Quick start

```bash
uv sync
uv run foco fetch                       # pull all sources into data/
uv run foco show --date 2026-08-06 --full
uv run foco build --site-base "https://YOUR-ORG.github.io"
uv run pytest -q
```

Then open `site/index.html`.

## Commands

| Command | Purpose |
|---|---|
| `foco fetch` | Pull every source into `data/meetings.json`, extract PDF text, record changes |
| `foco show` | Print the cache (`--date`, `--body`, `--slug`, `--full`, `--with-agenda`) |
| `foco build` | Render the static site (`--out`, `--site-base`, `--slug`) |
| `foco report` | Print what changed on the last fetch (`--latest-only`) |

Useful flags: `foco fetch --no-pdf` skips PDF extraction; `foco build --slug X`
renders one page without the index, topics or feeds.

## Deploying to GitHub Pages

Six steps, most of them one-time.

1. **The repository is already set up** as an owner-root site, which serves
   from the domain root so `robots.txt`, `llms.txt` and `sitemap.xml` sit where
   crawlers look for them:

   | | |
   |---|---|
   | Org | `forsyth-ga-public-meetings` |
   | Repo | `forsyth-ga-public-meetings.github.io` |
   | Site | `https://forsyth-ga-public-meetings.github.io/` |

   The site uses only relative internal links, so it would also run correctly
   from a subpath or a custom domain — only the absolute URLs in canonical
   tags, `sitemap.xml`, `rss.xml` and `llms.txt` depend on `SITE_BASE`.

2. **Push.**

   ```bash
   git push -u origin main
   ```

   `data/` is committed on purpose: change detection compares each run against
   the previous one, so the cache has to survive between runs. The PDF blobs
   under `data/pdf-cache/` are excluded and restored via Actions cache instead —
   they are ~8 MB of binaries that would otherwise sit in git history forever.

3. **Settings → Pages → Source: GitHub Actions.**

4. **Actions → Update and publish → Run workflow** for a first run without
   waiting for the 11:20 UTC cron.

`SITE_BASE` does not need to be set: the workflow falls back to
`https://<owner>.github.io`, which is already correct for an owner-root repo.
Set it later under **Settings → Secrets and variables → Actions → Variables**
if you adopt a custom domain, and the next run will pick it up.

### What the workflow does

Fetches once daily, runs the tests, builds, commits the refreshed cache and
deploys. Two deliberate choices:

- **Tests gate the publish.** If a vendor changes their markup and the parser
  breaks, the run fails instead of deploying empty agendas. Silent rot is the
  failure mode this project is most exposed to.
- **A concurrency group** prevents overlapping runs. That, not the cron entry
  alone, is what actually enforces one poll per day.

## Politeness

- Descriptive User-Agent with contact details, set in `config/sources.toml`.
- One poll per day, enforced by the workflow's concurrency group.
- Two seconds between requests to the same host; the school district's
  `Crawl-delay: 5` is honoured separately.
- `robots.txt` is honoured. PDFs are re-validated with `ETag` /
  `Last-Modified` and never re-downloaded unchanged.
- No analytics, no tracking, no ads, no newsletter.

## Configuration

Everything tunable lives in `config/`, and neither file requires touching code.

- **`sources.toml`** — User-Agent, delays, the fetch window, per-source
  endpoints, the dollar threshold, and the reference pages linked for legal
  deadlines.
- **`topics.toml`** — keyword and regex clusters for topic tagging, the phrases
  that identify consent and ratification blocks, and the patterns that mark a
  multi-year commitment.

## Design rules

These are constraints, not preferences. Tests enforce most of them.

- **Nothing is invented.** A missing agenda number renders as missing. Dollar
  figures are reproduced, never estimated.
- **No executable JavaScript** in any output. The only `<script>` is
  `application/ld+json`, which browsers do not execute.
- **Permanent URLs.** A slug, once assigned, never changes. The cache is an
  archive, not a window snapshot, so a meeting older than the fetch window keeps
  its page instead of vanishing on the next build.
- **No expiring URLs.** CivicClerk's `GetMeetingFile` returns a signed blob link
  that dies in about a week; the site uses `GetMeetingFileStream`, which returns
  the bytes from a stable URL. A test asserts no signed URL ever reaches output.
- **Machine-readable links are labelled as such.** Meeting pages separate direct
  downloads from JavaScript-only portal pages so an automated reader can tell
  which is worth fetching. Every page has a JSON twin at `index.json`.
- **Parsers fail loudly.** A parser that silently returns empty agendas is worse
  than one that stops the build.

## Known gaps

- **Board of Education agenda contents.** Meeting dates, times and location come
  from the district's own site. The agendas live on Simbli
  (eBOARDsolutions), which sits behind an Imperva WAF that blocks non-browser
  clients and serves a JavaScript anti-bot challenge to everything else. Reading
  it would mean misrepresenting the client and defeating that challenge, so the
  site links out instead. A request for machine-readable access is outstanding
  with the district.
- **Non-CivicClerk agendas are read from PDFs.** Only the Board of Commissioners
  publishes structured data. For every other board the items are extracted from
  the agenda PDF: numbering follows the document, nesting is flattened, and
  anything unreadable is absent. Those pages say so.
- **City of Cumming publishes no meeting times** on its listing pages, so those
  meetings show the time as not published.

## Layout

```
config/          sources.toml, topics.toml
src/foco/
  civicclerk.py  Board of Commissioners OData API
  county.py      forsythco.com listing (the calendar spine)
  boe.py         school district meeting schedule
  cumming.py     City of Cumming board pages
  pdftext.py     PDF text extraction for boards with no API
  cache.py       merge, archive, permanent slugs, change detection
  render.py      meeting pages
  sitegen.py     index, topics, sitemap, RSS, llms.txt
tests/           187 tests, mostly over the parsing layer
```
