"""City of Cumming agendas and minutes.

The city publishes one page per board, each a flat list of links whose text
carries the date and, inconsistently, either the meeting type or the document
kind:

    "May 19, 2026 Regular Meeting"                      (City Council)
    "June 16, 2026 Meeting Agenda"                      (Planning Board)
    "March 24, 2026 Meeting Notice & Agenda (DDA Meeting)"

Worse, City Council uses identical link text for the agenda and the minutes of
the same meeting, so the document kind has to come from the URL. Several links
therefore collapse onto one meeting, which is why this module groups by date
rather than emitting one meeting per link.

Note the live domain is cityofcumming.net; cummingga.gov does not resolve.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .config import Config
from .http import PoliteSession
from .model import Document, Meeting, split_body_and_type
from .topics import clean_text

log = logging.getLogger(__name__)

_LINK = re.compile(
    r"^\s*(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(?P<day>\d{1,2}),\s*(?P<year>20\d\d)\s*"
    r"(?P<rest>.*?)\s*$",
    re.I,
)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}

# Words that describe the document rather than the meeting.
_DOC_WORDS = re.compile(
    r"\b(?:meeting\s+)?(?:notice\s*&\s*agenda|agenda\s*&\s*minutes|agenda|minutes|"
    r"notice)\b",
    re.I,
)


class ParseError(RuntimeError):
    """The city's markup no longer matches. Fail loudly."""


def classify(text: str, url: str) -> str:
    """agenda / minutes / notice. City Council reuses link text, so URL wins."""
    low_url = url.lower()
    if re.search(r"minute", low_url):
        return "minutes"
    if re.search(r"agenda", low_url):
        return "agenda"
    low = (text or "").lower()
    if "minute" in low:
        return "minutes"
    if "agenda" in low:
        return "agenda"
    if "notice" in low:
        return "notice"
    return "other"


def meeting_type_from(text: str) -> str | None:
    """Strip document words, then look for a real meeting type."""
    cleaned = _DOC_WORDS.sub(" ", text or "")
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned)      # "(DDA Meeting)"
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—&")
    if not cleaned:
        return None
    _, mtype = split_body_and_type(cleaned)
    if mtype:
        return mtype
    # Anything left that still names a meeting, e.g. "Special Called Meeting
    # with Forsyth County Taxpayer Coalition".
    return cleaned if re.search(r"meeting|session|hearing", cleaned, re.I) else None


def parse_board(html: str, board: dict, cfg: Config, page_url: str) -> list[Meeting]:
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["nav", "header", "footer", "script", "style"]):
        bad.decompose()

    anchors = soup.find_all("a", href=True)
    if not anchors:
        raise ParseError(f"no links at all on {page_url}")

    tz = ZoneInfo(cfg.local_tz)
    grouped: dict[tuple[str, str | None], Meeting] = {}
    matched = 0

    for a in anchors:
        text = clean_text(a.get_text(" ", strip=True))
        m = _LINK.match(text)
        if not m:
            continue
        href = urljoin(page_url, a["href"])
        if not href.lower().endswith(".pdf"):
            continue
        matched += 1

        try:
            date = datetime(int(m.group("year")), _MONTHS[m.group("month").lower()],
                            int(m.group("day"))).date()
        except ValueError:
            log.warning("cumming: invalid date in %r", text)
            continue

        rest = m.group("rest")
        mtype = meeting_type_from(rest)
        kind = classify(rest, href)
        key = (date.isoformat(), mtype)

        meeting = grouped.get(key)
        if meeting is None:
            start_time = board.get("default_time") or None
            start_iso = None
            if start_time:
                hh, mm = (int(x) for x in start_time.split(":"))
                start_iso = datetime(date.year, date.month, date.day, hh, mm,
                                     tzinfo=tz).isoformat()
            meeting = Meeting(
                slug="",
                body=board["body"],
                meeting_type=mtype,
                title=f"{board['body']}" + (f" {mtype}" if mtype else ""),
                date=date.isoformat(),
                # The city does not publish times on these listing pages, so we
                # only assert one when the config supplies the board's standing
                # start time. Otherwise it renders as "not published".
                start_time=start_time,
                start_iso=start_iso,
                location=board.get("location") or None,
                sources=["cumming"],
                county_url=page_url,
                documents=[],
                items=[],
                agenda_published=False,
            )
            grouped[key] = meeting

        if any(d.url == href for d in meeting.documents):
            continue
        label = f"{kind.title()} ({date.isoformat()})" if kind != "other" else text
        meeting.documents.append(Document(
            label=label,
            url=href,
            kind=kind,
            source="cumming",
            machine_readable=True,
            content_type="application/pdf",
        ))

    if matched == 0:
        raise ParseError(
            f"no dated PDF links matched at {page_url}; the city's markup has "
            f"probably changed"
        )

    for meeting in grouped.values():
        meeting.agenda_published = any(
            d.kind == "agenda" for d in meeting.documents
        )
    return list(grouped.values())


def fetch_all(session: PoliteSession, cfg: Config) -> list[Meeting]:
    boards = cfg.sources["cumming"].get("boards", [])
    today = datetime.now(ZoneInfo(cfg.local_tz)).date()
    lo = (today - timedelta(days=cfg.days_back)).isoformat()
    hi = (today + timedelta(days=cfg.days_forward)).isoformat()

    out: list[Meeting] = []
    for board in boards:
        resp = session.get(board["url"])
        if resp.status_code != 200:
            log.warning("cumming: HTTP %d for %s", resp.status_code, board["url"])
            continue
        try:
            meetings = parse_board(resp.text, board, cfg, resp.url)
        except ParseError as exc:
            log.warning("cumming: %s", exc)
            continue
        inside = [m for m in meetings if lo <= m.date <= hi]
        log.info("cumming: %s -> %d meetings, %d in window",
                 board["body"], len(meetings), len(inside))
        out.extend(inside)
    return out
