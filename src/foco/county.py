"""Fetcher for the county's own meetings listing (www.forsythco.com).

This is the calendar spine. Unlike CivicClerk it is server-rendered, covers all
24 boards rather than just the BOC, and -- critically -- lists meetings whose
agendas have not been published yet.

The markup is Tailwind-generated but the semantic class names
(`board-meetings-list__*`) are stable hooks. If they change, parsing raises
rather than silently returning nothing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .config import Config
from .http import PoliteSession
from .model import Document, Meeting, split_body_and_type
from .topics import clean_text

log = logging.getLogger(__name__)

CARD = ".board-meetings-list__card"
CARD_DATE = ".board-meetings-list__card-date-text"
CARD_TITLE = ".board-meetings-list__card-title"
CARD_TYPE = ".board-meetings-list__card-type"
CARD_DETAIL = ".board-meetings-list__card-detail"

_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?\b")
_CARD_ID_RE = re.compile(r"card-(\d+)")


class ParseError(RuntimeError):
    """The county's markup no longer matches. Fail loudly, never silently."""


def parse_date(text: str) -> str | None:
    """'August 13, 2026' -> '2026-08-13'."""
    text = clean_text(text)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(text: str) -> str | None:
    """'2:00 PM' -> '14:00'. Returns None when absent rather than guessing."""
    m = _TIME_RE.search(text or "")
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if hour == 12:
        hour = 0
    if ampm == "p":
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def strip_trailing_time(title: str) -> str:
    """Some titles embed the time: 'Civil Service Board - Regular Meeting 10:00 AM'."""
    return _TIME_RE.sub("", title or "").strip(" -–—,")


def _classify(url: str, label: str) -> str:
    low = f"{label} {url}".lower()
    if "packet" in low:
        return "packet"
    if "notice" in low:
        return "notice"
    if "minute" in low:
        return "minutes"
    if "agenda" in low:
        return "agenda"
    return "other"


def parse_card(card, cfg: Config, page_url: str) -> Meeting | None:
    """One meeting card -> Meeting. Returns None if it carries no usable date."""
    date_el = card.select_one(CARD_DATE)
    date = parse_date(date_el.get_text(" ", strip=True)) if date_el else None
    if not date:
        return None

    title_el = card.select_one(CARD_TITLE)
    raw_title = clean_text(title_el.get_text(" ", strip=True)) if title_el else ""
    if not raw_title:
        return None
    county_url = None
    if title_el and (a := title_el.find("a", href=True)):
        county_url = urljoin(page_url, a["href"])

    type_el = card.select_one(CARD_TYPE)
    mtype = clean_text(type_el.get_text(" ", strip=True)) if type_el else ""

    # Details: a clock row and a location row, distinguished by their icon class.
    start_time = None
    location = None
    for detail in card.select(CARD_DETAIL):
        icon = detail.find("i")
        icon_cls = " ".join(icon.get("class", [])) if icon else ""
        text = clean_text(detail.get_text(" ", strip=True))
        if "clock" in icon_cls:
            start_time = parse_time(text)
        elif "location" in icon_cls:
            location = text or None
        else:
            if start_time is None and (t := parse_time(text)):
                start_time = t
            elif location is None and text:
                location = text
    if start_time is None:
        start_time = parse_time(raw_title)

    title = strip_trailing_time(raw_title)
    # The card's own type label is authoritative when present, but the title is
    # what carries the body name -- and the two spell the type differently.
    body, derived_type = split_body_and_type(title)
    mtype = derived_type or mtype or None

    start_iso = None
    if start_time:
        start_iso = datetime.fromisoformat(f"{date}T{start_time}:00").replace(
            tzinfo=ZoneInfo(cfg.local_tz)
        ).isoformat()

    county_id = None
    if m := _CARD_ID_RE.search(card.get("id") or ""):
        county_id = int(m.group(1))

    docs: list[Document] = []
    portal_url = None
    for a in card.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        low = href.lower()
        label = clean_text(a.get_text(" ", strip=True))
        if low.endswith(".pdf"):
            docs.append(
                Document(
                    label=label or "Document",
                    url=href,
                    kind=_classify(href, label),
                    source="county",
                    machine_readable=True,
                )
            )
        elif "civicclerk" in urlparse(href).netloc:
            portal_url = href
            docs.append(
                Document(
                    label=label or "CivicClerk portal page",
                    url=href,
                    kind="portal",
                    source="county",
                    machine_readable=False,
                )
            )

    return Meeting(
        slug="",
        body=body,
        meeting_type=mtype or None,
        title=title,
        date=date,
        start_time=start_time,
        start_iso=start_iso,
        location=location,
        sources=["county"],
        county_meeting_id=county_id,
        county_url=county_url,
        portal_url=portal_url,
        documents=docs,
        items=[],
        agenda_published=any(d.kind in ("agenda", "packet") for d in docs),
        agenda_status_note=None,
    )


def parse_listing(html: str, cfg: Config, page_url: str) -> list[Meeting]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(CARD)
    if not cards:
        raise ParseError(
            f"no '{CARD}' elements at {page_url}. The county's meetings markup "
            f"has probably changed -- re-run discovery before trusting output."
        )
    out = []
    for card in cards:
        meeting = parse_card(card, cfg, page_url)
        if meeting:
            out.append(meeting)
    return out


def absorb(keeper: Meeting, other: Meeting) -> Meeting:
    """Fold a duplicate listing card into the one we are keeping.

    Documents are unioned by URL. Scalar fields are filled only where the keeper
    has nothing, so we never overwrite real data with a blank. The identity id
    settles on the lower card number so the resulting slug is stable no matter
    which duplicate the listing happens to show first.
    """
    by_url = {d.url: d for d in keeper.documents}
    for d in other.documents:
        by_url.setdefault(d.url, d)
    keeper.documents = list(by_url.values())

    for field in ("start_time", "start_iso", "location", "county_url",
                  "portal_url", "meeting_type"):
        if getattr(keeper, field, None) in (None, "") and getattr(other, field, None):
            setattr(keeper, field, getattr(other, field))

    if other.county_meeting_id is not None:
        if (keeper.county_meeting_id is None
                or other.county_meeting_id < keeper.county_meeting_id):
            keeper.county_meeting_id = other.county_meeting_id

    keeper.agenda_published = any(
        d.kind in ("agenda", "packet") for d in keeper.documents
    )
    return keeper


def fetch_all(session: PoliteSession, cfg: Config) -> list[Meeting]:
    base = cfg.sources["county"]["meetings_url"].rstrip("/")
    max_pages = int(cfg.sources["county"].get("max_pages", 8))
    today = datetime.now(ZoneInfo(cfg.local_tz)).date()
    lo = (today - timedelta(days=cfg.days_back)).isoformat()
    hi = (today + timedelta(days=cfg.days_forward)).isoformat()
    seen: dict[tuple, Meeting] = {}

    for view in ("upcoming", "past"):
        for page in range(1, max_pages + 1):
            url = base + ("/" if page == 1 else f"/page/{page}/")
            resp = session.get(url, params={"view": view})
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            try:
                meetings = parse_listing(resp.text, cfg, resp.url)
            except ParseError:
                if page == 1:
                    raise
                break

            fresh = 0
            for m in meetings:
                if not (lo <= m.date <= hi):
                    continue
                # The listing repeats the same meeting under different card ids,
                # and the duplicates are not equivalent: one card may carry the
                # agenda and notice while its twin carries nothing. Picking
                # either one silently loses documents, so absorb instead.
                key = (m.date, m.title.lower(), m.start_time)
                if key in seen:
                    absorb(seen[key], m)
                else:
                    seen[key] = m
                    fresh += 1
            log.info("county: %s page %d -> %d cards (%d in window, new)",
                     view, page, len(meetings), fresh)

            # The listing is date-ordered outward from today, so once an entire
            # page falls outside the window we are done with this direction.
            dates = [m.date for m in meetings]
            if dates and all(d < lo for d in dates):
                break
            if dates and all(d > hi for d in dates):
                break
            if fresh == 0 and page > 1:
                break

    return list(seen.values())
