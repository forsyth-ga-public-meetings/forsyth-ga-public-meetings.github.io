"""Forsyth County Schools Board of Education.

Scope note, deliberately narrow: this module reads the *district's own* website,
which is public and crawlable. It does not touch Simbli / eBoardSolutions, where
the agendas live, because that host sits behind an Imperva WAF that blocks
non-browser clients outright and serves a JavaScript anti-bot challenge to the
rest. Reading it would require misrepresenting the client and defeating that
challenge.

So BoE meetings appear in the index with date, time and location, and link out
to Simbli for the agenda itself. Agenda items are not mirrored.

The schedule page's markup differs by year: 2025 uses <ul><li>Tuesday, January
21</li>, 2026 uses a run of <p>January 20</p>. Both are handled; the year comes
from the <h3> that introduces the block.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .config import Config
from .http import PoliteSession
from .model import Document, Meeting
from .topics import clean_text

log = logging.getLogger(__name__)

BODY = "Forsyth County Board of Education"

# "2026 Monthly Regular Meeting Schedule" / "2025 Work Session Meeting Schedule"
_HEADING = re.compile(
    r"(?P<year>20\d\d)\s*(?P<kind>Monthly Regular|Work Session)\s+Meeting Schedule",
    re.I,
)
# "Tuesday, January 21" or "January 20"
_DATE = re.compile(
    r"^(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s*)?"
    r"(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(?P<day>\d{1,2})\*?$",
    re.I,
)
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?\b")

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}


class ParseError(RuntimeError):
    """The district's markup no longer matches. Fail loudly."""


def parse_time_hint(text: str) -> str | None:
    """'5 PM' -> '17:00'. Returns None rather than assuming a default."""
    m = _TIME.search(text or "")
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if hour == 12:
        hour = 0
    if m.group(3).lower() == "p":
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def extract_location(soup: BeautifulSoup) -> str | None:
    # Collapse newlines first: the sentence wraps inside a single <p>, and "."
    # in a regex does not cross a line break.
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    m = re.search(r"Meetings are held at (?:the )?(.{10,140}?)(?:unless otherwise|\.)",
                  text, re.I)
    return clean_text(m.group(1)).rstrip(", ") if m else None


def parse_schedule(html: str, cfg: Config) -> list[Meeting]:
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["nav", "header", "footer", "script", "style"]):
        bad.decompose()

    headings = [h for h in soup.find_all(["h2", "h3", "h4"])
                if _HEADING.search(h.get_text(" ", strip=True).replace("\xa0", " "))]
    if not headings:
        raise ParseError(
            "no '<year> ... Meeting Schedule' headings on the Board of Education "
            "schedule page; the district's markup has probably changed"
        )

    location = extract_location(soup)
    tz = ZoneInfo(cfg.local_tz)
    out: list[Meeting] = []

    for heading in headings:
        label = heading.get_text(" ", strip=True).replace("\xa0", " ")
        m = _HEADING.search(label)
        year = int(m.group("year"))
        kind = m.group("kind").lower()
        mtype = "Work Session" if "work session" in kind else "Regular Meeting"

        start_time: str | None = None
        public_note: str | None = None
        dates: list[tuple[int, int]] = []

        # Walk forward until the next schedule heading.
        for sib in heading.find_all_next():
            if sib in headings and sib is not heading:
                break
            if sib.name not in ("p", "li", "h3", "h4"):
                continue
            text = clean_text(sib.get_text(" ", strip=True))
            if not text:
                continue

            dm = _DATE.match(text)
            if dm:
                dates.append((_MONTHS[dm.group("month").lower()], int(dm.group("day"))))
                continue

            # Time hints appear before the dates, e.g. "5 PM – Executive Session".
            if not dates:
                if re.search(r"public portion", text, re.I):
                    public_note = text
                elif (hint := parse_time_hint(text)) and len(text) < 60:
                    start_time = start_time or hint

        if not dates:
            log.warning("boe: no dates parsed under %r", label)
            continue

        for month, day in dates:
            try:
                date = datetime(year, month, day).date()
            except ValueError:
                log.warning("boe: invalid date %s-%s-%s under %r",
                            year, month, day, label)
                continue

            start_iso = None
            if start_time:
                hh, mm = (int(x) for x in start_time.split(":"))
                start_iso = datetime(year, month, day, hh, mm, tzinfo=tz).isoformat()

            note = (
                "Agendas for Board of Education meetings are published on the "
                "district's Simbli portal, which is not mirrored here. Use the "
                "link below for the agenda."
            )
            if public_note:
                note = f"{public_note}. {note}"

            out.append(Meeting(
                slug="",
                body=BODY,
                meeting_type=mtype,
                title=f"{BODY} {mtype}",
                date=date.isoformat(),
                start_time=start_time,
                start_iso=start_iso,
                location=location,
                sources=["boe"],
                county_url=cfg.sources["boe"]["schedule_url"],
                documents=[
                    Document(
                        label="Board Meeting Agendas (Simbli portal)",
                        url=cfg.sources["boe"]["agenda_portal_url"],
                        kind="portal",
                        source="boe",
                        machine_readable=False,
                    )
                ],
                items=[],
                agenda_published=False,
                agenda_status_note=note,
            ))

    return out


def fetch_all(session: PoliteSession, cfg: Config) -> list[Meeting]:
    url = cfg.sources["boe"]["schedule_url"]
    resp = session.get(url)
    resp.raise_for_status()
    meetings = parse_schedule(resp.text, cfg)

    today = datetime.now(ZoneInfo(cfg.local_tz)).date()
    lo = (today - timedelta(days=cfg.days_back)).isoformat()
    hi = (today + timedelta(days=cfg.days_forward)).isoformat()
    inside = [m for m in meetings if lo <= m.date <= hi]
    log.info("boe: %d meetings parsed, %d in window", len(meetings), len(inside))
    return inside
