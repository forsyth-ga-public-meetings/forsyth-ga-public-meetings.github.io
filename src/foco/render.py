"""Static HTML generation.

Constraints that drive every decision here:

* The page must be fully readable with JavaScript disabled. The only <script>
  in the output is an application/ld+json block, which browsers never execute.
* The primary consumer is a crawler that cannot execute JS. So every link that
  leads to real document content is marked as such, and links that lead to a
  JS-only application are marked as such too -- an agent should be able to tell
  them apart without fetching both.
* Nothing is asserted that the source did not say. Missing fields render as
  "not published", never as a plausible-looking placeholder.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .config import Config
from .model import Meeting

ASSET_DIR = Path(__file__).resolve().parents[2] / "assets"


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _fmt_date(iso: str) -> str:
    """'2026-08-06' -> 'Thursday, August 6, 2026'.

    Formatted by hand rather than with strftime: the platform-specific
    no-pad directive ('%-d' on Linux, '%#d' on Windows) would otherwise make
    the generated HTML differ between a local run and CI, churning the diff on
    every build.
    """
    from datetime import date

    y, m, d = (int(x) for x in iso.split("-"))
    dt = date(y, m, d)
    return f"{_WEEKDAYS[dt.weekday()]}, {_MONTHS[m - 1]} {d}, {y}"


def _fmt_time(hhmm: str | None) -> str | None:
    if not hhmm:
        return None
    hour, minute = (int(x) for x in hhmm.split(":"))
    suffix = "a.m." if hour < 12 else "p.m."
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def _money(amount: int) -> str:
    return f"${amount:,}"


# ---------------------------------------------------------------------------
# agenda items
# ---------------------------------------------------------------------------

def _fmt_size(n: int | None) -> str:
    if not n:
        return ""
    for unit, div in (("MB", 1024 * 1024), ("kB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}".replace(".0 ", " ")
    return f"{n} bytes"


def _render_item(item, cfg: Config, *, link_topics: bool) -> str:
    classes = ["item"]
    if item.under_consent:
        classes.append("item--consent")
    if item.large_dollar:
        classes.append("item--money")
    if item.long_term:
        classes.append("item--term")

    number = (
        f'<span class="item__number">{escape(item.outline_path)}</span>'
        if item.outline_path
        else '<span class="item__number item__number--missing">not numbered</span>'
    )

    # Flags carry their meaning as visible text. Nothing important lives in a
    # title attribute: screen readers announce those inconsistently, and they
    # are invisible to keyboard and touch users entirely.
    flags = []
    if item.under_consent:
        flags.append('<span class="flag flag--consent">Consent agenda</span>')
    if item.large_dollar:
        top = _money(max(item.dollar_amounts))
        flags.append(
            f'<span class="flag flag--money">{escape(top)}</span>'
        )
    if item.long_term:
        # Show the phrase that triggered the flag rather than hiding it in a
        # tooltip -- it is the reader's only way to judge the match.
        reason = item.long_term_reason or ""
        label = f"Multi-year: {reason}" if reason else "Multi-year"
        flags.append(f'<span class="flag flag--term">{escape(label)}</span>')
    for topic in item.topics:
        label = cfg.topic_clusters().get(topic, {}).get("label", topic)
        # Only ever link to a topic page that this build actually produces.
        if link_topics:
            flags.append(
                f'<a class="flag flag--topic" href="../../topics/{escape(topic)}/">'
                f"{escape(label)}</a>"
            )
        else:
            flags.append(f'<span class="flag flag--topic">{escape(label)}</span>')

    flag_html = f'<p class="item__flags">{"".join(flags)}</p>' if flags else ""
    fiscal = (
        f'<p class="item__fiscal"><strong>Fiscal impact as stated:</strong> '
        f"{escape(item.fiscal_impact)}</p>"
        if item.fiscal_impact
        else ""
    )

    attachments = ""
    if item.attachments:
        rows = []
        for a in item.attachments:
            meta = " · ".join(
                p for p in (
                    "PDF" if (a.content_type or "").endswith("pdf") else None,
                    _fmt_size(a.size_bytes) or None,
                ) if p
            )
            meta_html = f' <span class="doc__kind">{escape(meta)}</span>' if meta else ""
            rows.append(
                f'<li><a href="{escape(a.url)}" rel="nofollow">{escape(a.label)}</a>'
                f"{meta_html}</li>"
            )
        n = len(item.attachments)
        attachments = (
            f'<details class="item__attachments"><summary>'
            f'{n} supporting document{"s" if n != 1 else ""}</summary>'
            f'<ul class="docs docs--attachments">{"".join(rows)}</ul></details>'
        )

    children = ""
    if item.children:
        children = (
            '<ol class="items items--nested">'
            + "".join(_render_item(c, cfg, link_topics=link_topics)
                      for c in item.children)
            + "</ol>"
        )

    return (
        f'<li class="{" ".join(classes)}">'
        f'<div class="item__body">{number}'
        f'<span class="item__title">{escape(item.title)}</span></div>'
        f"{flag_html}{fiscal}{attachments}{children}</li>"
    )


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

KIND_LABELS = {
    "agenda": "Agenda",
    "packet": "Agenda packet",
    "notice": "Public notice",
    "minutes": "Minutes",
    "portal": "Portal page",
    "attachment": "Attachment",
    "transcript": "Plain text",
    "other": "Document",
}


def _doc_row(d, css_class: str) -> str:
    meta = [KIND_LABELS.get(d.kind, d.kind)]
    if d.content_type == "text/plain":
        meta.append("text/plain")
    elif (d.content_type or "").endswith("pdf") or d.url.lower().endswith(".pdf"):
        meta.append("PDF")
    if d.size_bytes:
        meta.append(_fmt_size(d.size_bytes))
    return (
        f'<li class="doc {css_class}">'
        f'<a href="{escape(d.url)}" rel="nofollow">{escape(d.label)}</a> '
        f'<span class="doc__kind">{escape(" · ".join(meta))}</span></li>'
    )


def _render_documents(m: Meeting) -> str:
    fetchable = [d for d in m.documents if d.machine_readable]
    js_only = [d for d in m.documents if not d.machine_readable]

    out = ['<section id="documents"><h2>Documents at the county</h2>']

    if fetchable:
        out.append(
            '<h3 class="doc-group">Direct downloads '
            '<span class="doc-group__note">— these URLs return the file itself, '
            "so a client that cannot run JavaScript can read them</span></h3>"
            '<ul class="docs">'
        )
        out.extend(_doc_row(d, "doc--fetchable") for d in fetchable)
        out.append("</ul>")

    if js_only:
        out.append(
            '<h3 class="doc-group">Portal pages '
            '<span class="doc-group__note">— these are JavaScript applications; '
            "the links above are the same material in readable form</span></h3>"
            '<ul class="docs">'
        )
        out.extend(_doc_row(d, "doc--js") for d in js_only)
        out.append("</ul>")

    if not m.documents:
        out.append(
            '<p class="empty">The county has not posted any documents for this '
            "meeting yet.</p>"
        )
    out.append("</section>")
    return "".join(out)


# ---------------------------------------------------------------------------
# structured data
# ---------------------------------------------------------------------------

def _json_ld(m: Meeting, cfg: Config, canonical: str) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": f"{m.body}" + (f" — {m.meeting_type}" if m.meeting_type else ""),
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "organizer": {"@type": "GovernmentOrganization", "name": m.body},
        "url": canonical,
    }
    if m.start_iso:
        data["startDate"] = m.start_iso
    else:
        data["startDate"] = m.date
    if m.location:
        data["location"] = {
            "@type": "Place",
            "name": m.location,
            "address": {"@type": "PostalAddress", "addressLocality": "Cumming",
                        "addressRegion": "GA", "addressCountry": "US"},
        }
    if m.county_url:
        data["isBasedOn"] = m.county_url
    return json.dumps(data, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

def render_meeting(
    m: Meeting, cfg: Config, *, site_base: str = "", link_topics: bool = False
) -> str:
    canonical = f"{site_base.rstrip('/')}/meetings/{m.slug}/" if site_base else "./"
    title = f"{m.body}" + (f" — {m.meeting_type}" if m.meeting_type else "")
    pretty_date = _fmt_date(m.date)
    pretty_time = _fmt_time(m.start_time)

    items = list(m.all_items())
    consent_n = sum(1 for i in items if i.under_consent)
    money_n = sum(1 for i in items if i.large_dollar)

    authoritative = m.county_url or m.portal_url or "https://www.forsythco.com/meetings"

    # Summary strip -- what a reader (or an agent) should notice first.
    stats = []
    if items:
        stats.append(f"{len(items)} agenda items")
        if consent_n:
            stats.append(f"<strong>{consent_n} under a consent agenda</strong>")
        if money_n:
            stats.append(f"{money_n} over {_money(cfg.large_dollar_threshold)}")
    summary = (
        f'<p class="summary">{" · ".join(stats)}</p>' if stats else ""
    )

    if items:
        if m.agenda_derived_from_pdf:
            provenance = (
                '<p class="caveat"><strong>Read automatically from a PDF.</strong> '
                "The county publishes this board's agenda only as a PDF, with no "
                "machine-readable structure. Numbering below follows the PDF, but "
                "grouping is flattened and any item the extractor could not read "
                "is missing. Treat this as a finding aid and read the PDF itself "
                "for the complete agenda.</p>"
            )
        else:
            provenance = (
                '<p class="note">Item numbering is reproduced exactly as the county '
                "published it. Items marked <em>Consent agenda</em> are voted on as "
                "a single block and normally pass without individual discussion.</p>"
            )
        agenda_section = (
            '<section id="agenda"><h2>Agenda</h2>'
            + provenance
            + '<ol class="items">'
            + "".join(_render_item(i, cfg, link_topics=link_topics) for i in m.items)
            + "</ol></section>"
        )
    else:
        note = m.agenda_status_note or (
            "The county has not published an agenda for this meeting yet."
        )
        agenda_section = (
            f'<section id="agenda"><h2>Agenda</h2>'
            f'<p class="empty">{escape(note)}</p>'
            f'<p class="note">Agendas for Forsyth County meetings are typically '
            f"posted a few days beforehand. Check the county's page for the "
            f"current status.</p></section>"
        )

    where = escape(m.location) if m.location else '<span class="missing">not published</span>'
    when_time = (
        f'<time datetime="{escape(m.start_iso)}">{escape(pretty_time)}</time>'
        if m.start_iso and pretty_time
        else '<span class="missing">time not published</span>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — {escape(pretty_date)} — Forsyth County agenda index</title>
<meta name="description" content="Agenda index for the {escape(title)} held {escape(pretty_date)} in Forsyth County, Georgia. Links to the county's official documents.">
<link rel="canonical" href="{escape(canonical)}">
<link rel="stylesheet" href="../../assets/style.css">
<script type="application/ld+json">
{_json_ld(m, cfg, canonical)}
</script>
</head>
<body>
<a class="skip" href="#agenda">Skip to agenda</a>

<header class="masthead">
  <p class="masthead__site"><a href="../../">Forsyth County meeting index</a></p>
  <p class="masthead__disclaimer">An unofficial, plain-HTML index of public
  meeting agendas. <strong>Not an official county source.</strong></p>
</header>

<main>
<article>
  <header class="meeting-head">
    <p class="eyebrow">{escape(m.body)}</p>
    <h1>{escape(m.meeting_type or "Meeting")}</h1>
    <dl class="facts">
      <div><dt>Date</dt><dd><time datetime="{escape(m.date)}">{escape(pretty_date)}</time></dd></div>
      <div><dt>Time</dt><dd>{when_time}</dd></div>
      <div><dt>Location</dt><dd>{where}</dd></div>
    </dl>
    {summary}
  </header>

  <aside class="authority">
    <p><strong>The county is the authoritative source.</strong> This page is an
    index. For the official record — and for anything with a legal deadline,
    such as appeal windows, comment periods or registration cutoffs — use the
    county's own page:</p>
    <p class="authority__link"><a href="{escape(authoritative)}">Official county page for this meeting</a></p>
  </aside>

  {agenda_section}

  {_render_documents(m)}

  <footer class="provenance">
    <h2>Provenance</h2>
    <ul>
      <li>Fetched at <time datetime="{escape(m.fetched_at or "")}">{escape(m.fetched_at or "unknown")}</time></li>
      <li>Sources: {escape(", ".join(m.sources) or "none")}</li>
      {"<li>CivicClerk event " + str(m.civicclerk_event_id) + ", agenda " + str(m.civicclerk_agenda_id) + "</li>" if m.civicclerk_event_id else ""}
      <li>Machine-readable copy of this page's data:
        <a href="./index.json">index.json</a></li>
    </ul>
    <p class="note">Dollar figures and item numbers are reproduced from the
    county's published agenda. Where the county published no value, this page
    says so rather than estimating one.</p>
  </footer>
</article>
</main>
</body>
</html>
"""
