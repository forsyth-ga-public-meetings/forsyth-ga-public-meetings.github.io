"""Site-level pages: index, topic pages, sitemap, RSS, llms.txt.

Every page here obeys the same rules as the meeting page: no executable
JavaScript, real dates in the markup, and nothing asserted that the county did
not publish.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from xml.sax.saxutils import escape as xesc

from .config import Config
from .model import Meeting
from .render import _fmt_date, _fmt_time

SITE_NAME = "Forsyth County meeting index"
TAGLINE = (
    "An unofficial, plain-HTML index of public meeting agendas for Forsyth "
    "County, Georgia."
)


def _head(title: str, description: str, *, depth: int, canonical: str | None = None,
          extra: str = "") -> str:
    up = "../" * depth
    canon = f'<link rel="canonical" href="{escape(canonical)}">' if canonical else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<meta name="description" content="{escape(description)}">
{canon}
<link rel="alternate" type="application/rss+xml" title="{escape(SITE_NAME)}" href="{up}rss.xml">
<link rel="stylesheet" href="{up}assets/style.css">
{extra}
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="masthead">
  <p class="masthead__site"><a href="{up}">{escape(SITE_NAME)}</a></p>
  <p class="masthead__disclaimer">{escape(TAGLINE)}
  <strong>Not an official county source.</strong></p>
</header>
"""


def _foot(depth: int) -> str:
    up = "../" * depth
    return f"""
<footer class="provenance">
  <p>This site indexes public meeting agendas so they can be read without
  JavaScript and found by search engines. It is not the official record. For
  anything with a legal deadline — appeal windows, comment periods,
  registration cutoffs — use the county's own pages.</p>
  <p><a href="{up}llms.txt">llms.txt</a> ·
     <a href="{up}sitemap.xml">sitemap.xml</a> ·
     <a href="{up}rss.xml">RSS</a> ·
     <a href="{up}meetings.json">meetings.json</a></p>
  <p>Official sources:
     <a href="https://www.forsythco.com/meetings">Forsyth County meetings</a> ·
     <a href="https://forsythcoga.portal.civicclerk.com">CivicClerk portal</a></p>
</footer>
</body>
</html>
"""


def _references(cfg: Config, topic: str | None = None) -> list[dict]:
    section = cfg.sources.get("references", {})
    if not section.get("enabled"):
        return []
    pages = section.get("pages", [])
    if topic is None:
        return list(pages)
    return [p for p in pages if topic in (p.get("topics") or [])]


def _render_references(refs: list[dict], *, heading: str) -> str:
    """Pointers to the authoritative page. Deliberately never paraphrased.

    PROJECT.md forbids presenting this site as authoritative for legal
    deadlines, so these entries carry the county's link and a warning, and no
    restatement of any date.
    """
    if not refs:
        return ""
    rows = "".join(
        f'<li class="ref"><a href="{escape(r["url"])}">{escape(r["title"])}</a>'
        f'<p class="row__meta">{escape(r.get("body", ""))}</p>'
        + (f'<p class="ref__note">{escape(r["note"])}</p>' if r.get("note") else "")
        + "</li>"
        for r in refs
    )
    return (
        f'<section id="references"><h2>{escape(heading)}</h2>'
        '<p class="note">These link straight to the authority. This site does '
        "not restate deadlines, appeal windows or cutoff dates — those come "
        "from the notice you were sent and from the pages below.</p>"
        f'<ul class="refs">{rows}</ul></section>'
    )


def _meeting_row(m: Meeting, *, depth: int) -> str:
    up = "../" * depth
    items = list(m.all_items())
    marks = []
    if items:
        consent = sum(1 for i in items if i.under_consent)
        money = sum(1 for i in items if i.large_dollar)
        marks.append(f"{len(items)} items")
        if consent:
            marks.append(f'<span class="flag flag--consent">{consent} consent</span>')
        if money:
            marks.append(f'<span class="flag flag--money">{money} over threshold</span>')
    elif m.documents:
        n = len([d for d in m.documents if d.machine_readable])
        marks.append(f"{n} document{'s' if n != 1 else ''}, agenda not itemised")
    else:
        marks.append('<span class="missing">no agenda published yet</span>')

    when = _fmt_time(m.start_time)
    time_html = (
        f'<time datetime="{escape(m.start_iso)}">{escape(when)}</time>'
        if m.start_iso and when else '<span class="missing">time not published</span>'
    )
    title = escape(m.body) + (f" — {escape(m.meeting_type)}" if m.meeting_type else "")
    return (
        f'<li class="row">'
        f'<div class="row__date"><time datetime="{escape(m.date)}">'
        f"{escape(_fmt_date(m.date))}</time></div>"
        f'<div class="row__main">'
        f'<a class="row__title" href="{up}meetings/{escape(m.slug)}/">{title}</a>'
        f'<p class="row__meta">{time_html}'
        + (f" · {escape(m.location)}" if m.location else "")
        + f'</p><p class="row__marks">{" · ".join(marks)}</p>'
        f"</div></li>"
    )


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def render_index(meetings: list[Meeting], cfg: Config, *, site_base: str = "",
                 today: str | None = None) -> str:
    today = today or datetime.now().date().isoformat()
    upcoming = sorted([m for m in meetings if m.date >= today],
                      key=lambda m: (m.date, m.start_time or ""))
    past = sorted([m for m in meetings if m.date < today],
                  key=lambda m: (m.date, m.start_time or ""), reverse=True)

    topics = cfg.topic_clusters()
    topic_links = "".join(
        f'<li><a href="topics/{escape(k)}/">{escape(v["label"])}</a> '
        f'<span class="row__meta">{escape(v.get("description", ""))}</span></li>'
        for k, v in topics.items()
    )

    bodies = sorted({m.body for m in meetings})

    out = [_head(
        f"{SITE_NAME} — agendas for {len(bodies)} boards",
        TAGLINE + " Meeting agendas, consent-agenda flags and links to the "
                  "county's official documents.",
        depth=0,
        canonical=f"{site_base.rstrip('/')}/" if site_base else None,
    )]
    out.append('<main id="main">')
    out.append(f"<h1>{escape(SITE_NAME)}</h1>")
    out.append(
        "<p class=\"lede\">Forsyth County publishes its meeting agendas through "
        "JavaScript applications that search engines and automated readers "
        "cannot index. This site mirrors the <em>index</em> of those agendas as "
        "plain HTML and links back to the county for the documents themselves. "
        "It is not a replacement for the county's record.</p>"
    )
    out.append(
        f'<p class="summary">{len(meetings)} meetings across {len(bodies)} boards · '
        f"{len(upcoming)} upcoming</p>"
    )

    out.append('<section id="topics"><h2>By topic</h2>'
               f'<ul class="topic-list">{topic_links}</ul></section>')

    out.append('<section id="upcoming"><h2>Upcoming meetings</h2>')
    if upcoming:
        out.append('<ol class="rows">'
                   + "".join(_meeting_row(m, depth=0) for m in upcoming)
                   + "</ol>")
    else:
        out.append('<p class="empty">No upcoming meetings in the current window.</p>')
    out.append("</section>")

    out.append('<section id="past"><h2>Recent meetings</h2>'
               '<ol class="rows">'
               + "".join(_meeting_row(m, depth=0) for m in past)
               + "</ol></section>")

    out.append('<section id="boards"><h2>Boards covered</h2><ul class="cols">'
               + "".join(f"<li>{escape(b)}</li>" for b in bodies)
               + "</ul></section>")
    out.append(_render_references(_references(cfg),
                                  heading="Deadlines and official pages"))
    out.append("</main>")
    out.append(_foot(0))
    return "".join(out)


# ---------------------------------------------------------------------------
# topic pages
# ---------------------------------------------------------------------------

def render_topic(key: str, meetings: list[Meeting], cfg: Config, *,
                 site_base: str = "") -> str:
    cluster = cfg.topic_clusters()[key]
    label = cluster["label"]
    description = cluster.get("description", "")

    hits: list[tuple[Meeting, object]] = []
    for m in sorted(meetings, key=lambda x: x.date, reverse=True):
        for item in m.all_items():
            if key in item.topics:
                hits.append((m, item))

    out = [_head(
        f"{label} — {SITE_NAME}",
        f"Forsyth County agenda items about {label.lower()}. {description}",
        depth=2,
        canonical=f"{site_base.rstrip('/')}/topics/{key}/" if site_base else None,
    )]
    out.append('<main id="main">')
    out.append(f'<p class="eyebrow">Topic</p><h1>{escape(label)}</h1>')
    out.append(f'<p class="lede">{escape(description)}</p>')
    out.append(
        '<p class="note">Items are matched by keyword against the county\'s own '
        "agenda text. Matching is deliberately broad, so some items here will be "
        "only loosely related — and an item the keywords miss will not appear. "
        "This is a finding aid, not a complete list.</p>"
    )

    refs = _references(cfg, key)

    if not hits:
        out.append(
            '<p class="empty">No agenda items currently match this topic.</p>'
            if not refs else
            '<p class="empty">No agenda items currently match this topic. The '
            "official pages below cover it.</p>"
        )
    else:
        out.append(f'<p class="summary">{len(hits)} matching agenda items</p>')
        out.append('<ol class="rows">')
        for m, item in hits:
            marks = []
            if item.under_consent:
                marks.append('<span class="flag flag--consent">Consent agenda</span>')
            if item.large_dollar:
                marks.append(
                    f'<span class="flag flag--money">'
                    f"${max(item.dollar_amounts):,}</span>"
                )
            if item.long_term:
                marks.append('<span class="flag flag--term">Multi-year</span>')
            num = escape(item.outline_path) if item.outline_path else "—"
            out.append(
                f'<li class="row"><div class="row__date">'
                f'<time datetime="{escape(m.date)}">{escape(_fmt_date(m.date))}</time>'
                f'<p class="row__meta">{escape(m.body)}</p></div>'
                f'<div class="row__main">'
                f'<p class="row__title"><span class="item__number">{num}</span> '
                f"{escape(item.title)}</p>"
                + (f'<p class="row__marks">{"".join(marks)}</p>' if marks else "")
                + f'<p class="row__meta"><a href="../../meetings/{escape(m.slug)}/">'
                  f"See this item in context →</a></p>"
                f"</div></li>"
            )
        out.append("</ol>")

    out.append(_render_references(refs, heading="Official pages on this topic"))
    out.append("</main>")
    out.append(_foot(2))
    return "".join(out)


# ---------------------------------------------------------------------------
# sitemap
# ---------------------------------------------------------------------------

def render_sitemap(meetings: list[Meeting], cfg: Config, *, site_base: str) -> str:
    base = site_base.rstrip("/")
    urls = [(f"{base}/", None)]
    for key in cfg.topic_clusters():
        urls.append((f"{base}/topics/{key}/", None))
    for m in meetings:
        lastmod = (m.fetched_at or "")[:10] or None
        urls.append((f"{base}/meetings/{m.slug}/", lastmod))

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in urls:
        parts.append("<url>")
        parts.append(f"<loc>{xesc(loc)}</loc>")
        if lastmod:
            parts.append(f"<lastmod>{xesc(lastmod)}</lastmod>")
        parts.append("</url>")
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"


def render_robots(site_base: str) -> str:
    base = site_base.rstrip("/")
    return (
        "User-agent: *\n"
        "Allow: /\n\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def render_rss(changelog: list[dict], cfg: Config, *, site_base: str,
               limit: int = 60) -> str:
    base = site_base.rstrip("/")
    now = format_datetime(datetime.now(timezone.utc))
    items = []
    for entry in changelog[:limit]:
        link = f"{base}/meetings/{entry['slug']}/"
        verb = "New meeting" if entry["kind"] == "new" else "Agenda changed"
        title = f"{verb}: {entry['title']} — {entry['date']}"
        try:
            pub = format_datetime(datetime.fromisoformat(entry["at"]))
        except ValueError:
            pub = now
        items.append(
            "<item>"
            f"<title>{xesc(title)}</title>"
            f"<link>{xesc(link)}</link>"
            f"<guid isPermaLink=\"false\">{xesc(entry['slug'] + ':' + entry['at'])}</guid>"
            f"<pubDate>{xesc(pub)}</pubDate>"
            f"<description>{xesc(entry['summary'])}</description>"
            "</item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n<channel>\n'
        f"<title>{xesc(SITE_NAME)}</title>\n"
        f"<link>{xesc(base)}/</link>\n"
        f'<atom:link href="{xesc(base)}/rss.xml" rel="self" type="application/rss+xml" />\n'
        f"<description>{xesc('New and changed Forsyth County meeting agendas.')}</description>\n"
        f"<lastBuildDate>{xesc(now)}</lastBuildDate>\n"
        "<language>en-us</language>\n"
        + "\n".join(items)
        + "\n</channel>\n</rss>\n"
    )


# ---------------------------------------------------------------------------
# llms.txt
# ---------------------------------------------------------------------------

def render_llms_txt(meetings: list[Meeting], cfg: Config, *, site_base: str) -> str:
    base = site_base.rstrip("/")
    bodies = sorted({m.body for m in meetings})
    today = datetime.now().date().isoformat()
    upcoming = [m for m in meetings if m.date >= today]
    with_items = [m for m in meetings if list(m.all_items())]

    topic_lines = "\n".join(
        f"- {base}/topics/{k}/ — {v['label']}: {v.get('description','')}"
        for k, v in cfg.topic_clusters().items()
    )
    body_lines = "\n".join(f"- {b}" for b in bodies)

    return f"""# {SITE_NAME}

> {TAGLINE} This site exists because the county's own agenda portals are
> JavaScript applications that cannot be read without executing scripts. It is
> an index that points at the county, not a replacement for it.

## What this is, and what it is not

This site is NOT authoritative. Forsyth County is. Every meeting page links to
the county's own page for that meeting, and to the county's original PDF
documents. For anything with a legal deadline — assessment appeal windows,
public comment periods, voter registration cutoffs — consult the county
directly. Do not rely on this site for those dates.

Agenda item text, numbering and dollar figures are reproduced from the county's
published agendas without alteration. Where the county published no value, this
site says the value is missing rather than estimating one.

## How to read this site without executing JavaScript

Every meeting page is static HTML at:

    {base}/meetings/{{slug}}/

Each meeting page has a machine-readable twin containing the same data as JSON:

    {base}/meetings/{{slug}}/index.json

The complete dataset is a single file:

    {base}/meetings.json

## Getting the underlying documents

Meeting pages separate links into two groups, because the distinction matters
for automated readers:

- "Direct downloads" — URLs that return the file itself (PDF or plain text).
  These are safe to fetch directly.
- "Portal pages" — URLs that serve a JavaScript application. Fetching these
  without a browser returns an empty shell. The direct downloads listed above
  them contain the same material.

Where the county's system offers it, agendas are also linked in a plain-text
rendering, which is usually the cheapest way to read an agenda in full.

## Flags applied to agenda items

- Consent agenda — the item sits under a consent or ratification block and is
  scheduled to pass in a single block vote, normally without discussion.
- Dollar figure — the item's text mentions an amount at or above
  ${cfg.large_dollar_threshold:,}.
- Multi-year — the item's text suggests a commitment longer than one year.

These flags are derived by keyword and pattern matching over the county's text.
They are a finding aid and may both over- and under-match.

## Coverage

- {len(meetings)} meetings, {len(upcoming)} of them upcoming
- {len(bodies)} boards and committees
- {len(with_items)} meetings have item-level agendas; the remainder link to
  agenda PDFs published by the county without item-level structure

## Boards covered

{body_lines}

## Topic pages

{topic_lines}

## Feeds and indexes

- {base}/ — all meetings, upcoming first
- {base}/rss.xml — new meetings and changed agendas
- {base}/sitemap.xml — every page

## Canonical upstream sources

- Forsyth County meetings: https://www.forsythco.com/meetings
- Board of Commissioners agenda portal (JavaScript):
  https://forsythcoga.portal.civicclerk.com
- Board of Commissioners agenda API (JSON, no JavaScript required):
  https://forsythcoga.api.civicclerk.com/v1/Events
- Forsyth County Schools Board of Education:
  https://www.forsyth.k12.ga.us/inside-fcs/board-of-education/meeting-schedule

Generated {today}. Data is refreshed at most once per day.
"""
