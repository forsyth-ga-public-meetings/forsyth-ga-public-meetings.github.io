"""Tests for the county listing parser and the two-source merge."""

from __future__ import annotations

from pathlib import Path

import pytest

from foco import cache, county
from foco.config import load_config
from foco.model import Document, Meeting

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def meetings(cfg):
    html = (FIXTURES / "county_meetings.html").read_text("utf-8")
    return county.parse_listing(html, cfg, "https://www.forsythco.com/meetings")


def _find(meetings, date):
    return next(m for m in meetings if m.date == date)


# ---------------------------------------------------------------------------
# card parsing
# ---------------------------------------------------------------------------

def test_all_cards_parsed(meetings):
    assert len(meetings) == 4
    assert [m.date for m in meetings] == [
        "2026-08-11", "2026-08-13", "2026-08-20", "2026-08-25",
    ]


def test_times_are_converted_to_24h(meetings):
    assert _find(meetings, "2026-08-11").start_time == "14:00"  # 2:00 PM
    assert _find(meetings, "2026-08-13").start_time == "10:00"  # 10:00 AM
    assert _find(meetings, "2026-08-20").start_time == "17:00"  # 5:00 PM


def test_start_iso_carries_eastern_offset(meetings):
    assert _find(meetings, "2026-08-20").start_iso == "2026-08-20T17:00:00-04:00"


def test_location_is_captured(meetings):
    assert _find(meetings, "2026-08-11").location == "Board of Commissioners Building"


def test_body_and_type_split(meetings):
    m = _find(meetings, "2026-08-20")
    assert m.body == "Board of Commissioners"
    assert m.meeting_type == "Regular Meeting / Public Hearings"


def test_time_stripped_from_title(meetings):
    m = _find(meetings, "2026-08-13")
    assert "10:00" not in m.title
    assert m.body == "Civil Service Board"


def test_county_meeting_id_extracted(meetings):
    assert _find(meetings, "2026-08-11").county_meeting_id == 17559


# ---------------------------------------------------------------------------
# documents: agents need URLs they can actually fetch
# ---------------------------------------------------------------------------

def test_pdf_links_are_machine_readable(meetings):
    m = _find(meetings, "2026-08-11")
    pdfs = [d for d in m.documents if d.url.lower().endswith(".pdf")]
    assert pdfs
    assert all(d.machine_readable for d in pdfs)
    assert any(d.kind == "agenda" for d in pdfs)
    assert any(d.kind == "notice" for d in pdfs)


def test_civicclerk_portal_link_marked_not_machine_readable(meetings):
    m = _find(meetings, "2026-08-11")
    portal = [d for d in m.documents if d.kind == "portal"]
    assert portal
    assert all(not d.machine_readable for d in portal)


def test_future_meeting_has_no_agenda_yet(meetings):
    """Aug 20 and Aug 25 are scheduled but their agendas are not posted yet.

    A public notice may already exist -- that is not an agenda, and must not
    cause the meeting to be presented as having one.
    """
    for date in ("2026-08-20", "2026-08-25"):
        m = _find(meetings, date)
        assert not [d for d in m.documents if d.kind in ("agenda", "packet")]
        assert m.agenda_published is False


def test_public_notice_alone_does_not_imply_an_agenda(meetings):
    m = _find(meetings, "2026-08-20")
    assert [d.kind for d in m.documents] == ["notice"]
    assert m.agenda_published is False


# ---------------------------------------------------------------------------
# date and time helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("August 13, 2026", "2026-08-13"),
        ("September 1, 2026", "2026-09-01"),
        ("not a date", None),
        ("", None),
    ],
)
def test_parse_date(text, expected):
    assert county.parse_date(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2:00 PM", "14:00"),
        ("10:00 AM", "10:00"),
        ("12:00 PM", "12:00"),
        ("12:30 AM", "00:30"),
        ("6:30 PM", "18:30"),
        ("no time", None),
    ],
)
def test_parse_time(text, expected):
    assert county.parse_time(text) == expected


# ---------------------------------------------------------------------------
# fail loudly
# ---------------------------------------------------------------------------

def test_missing_card_markup_raises(cfg):
    with pytest.raises(county.ParseError, match="markup has probably changed"):
        county.parse_listing("<html><body><p>nothing</p></body></html>",
                             cfg, "https://example.invalid/")


# ---------------------------------------------------------------------------
# merging the two sources
# ---------------------------------------------------------------------------

def _stub(date, body, mtype, time, **kw):
    return Meeting(
        slug="", body=body, meeting_type=mtype,
        title=f"{body} {mtype}", date=date, start_time=time,
        start_iso=f"{date}T{time}:00-04:00", location=None, **kw
    )


def test_merge_joins_on_date_time_and_body():
    c = _stub("2026-08-06", "Board of Commissioners",
              "Regular Meeting/Public Hearings", "17:00",
              sources=["county"], county_meeting_id=1)
    cc = _stub("2026-08-06", "Board of Commissioners",
               "Regular Meeting / Public Hearings", "17:00",
               sources=["civicclerk"], civicclerk_event_id=455,
               civicclerk_agenda_id=389)
    merged = cache.merge([c], [cc])
    assert len(merged) == 1
    assert merged[0].sources == ["civicclerk", "county"]
    assert merged[0].civicclerk_agenda_id == 389
    assert merged[0].county_meeting_id == 1


def test_merge_does_not_join_different_bodies_at_same_time():
    a = _stub("2026-09-01", "Zoning Board of Appeals", "Work Session", "18:30",
              sources=["county"])
    b = _stub("2026-09-01", "Planning Commission", "Work Session", "18:30",
              sources=["civicclerk"], civicclerk_event_id=9)
    merged = cache.merge([a], [b])
    assert len(merged) == 2


def test_unmatched_civicclerk_event_is_kept_not_dropped():
    cc = _stub("2026-07-01", "Board of Commissioners", "Work Session", "14:00",
               sources=["civicclerk"], civicclerk_event_id=1)
    merged = cache.merge([], [cc])
    assert len(merged) == 1
    assert merged[0].civicclerk_event_id == 1


def test_merge_unions_documents_by_url():
    shared = "https://example.invalid/a.pdf"
    c = _stub("2026-08-06", "Board of Commissioners", "Work Session", "14:00",
              sources=["county"])
    c.documents = [Document("County agenda", shared, "agenda", "county")]
    cc = _stub("2026-08-06", "Board of Commissioners", "Work Session", "14:00",
               sources=["civicclerk"], civicclerk_event_id=2)
    cc.documents = [
        Document("Same file", shared, "agenda", "civicclerk"),
        Document("Packet", "https://example.invalid/b.pdf", "packet", "civicclerk"),
    ]
    merged = cache.merge([c], [cc])
    urls = [d.url for d in merged[0].documents]
    assert sorted(urls) == [shared, "https://example.invalid/b.pdf"]


# ---------------------------------------------------------------------------
# slug permanence
# ---------------------------------------------------------------------------

def test_slug_is_stable_when_title_changes():
    m = _stub("2026-08-20", "Board of Commissioners",
              "Regular Meeting / Public Hearings", "17:00",
              sources=["county"], civicclerk_event_id=456)
    ledger = cache.assign_slugs([m], {})
    original = m.slug
    assert original == "2026-08-20-board-of-commissioners-regular-meeting-public-hearings"

    renamed = _stub("2026-08-20", "Board of Commissioners (Amended)",
                    "Regular Meeting / Public Hearings", "17:00",
                    sources=["county"], civicclerk_event_id=456)
    cache.assign_slugs([renamed], ledger)
    assert renamed.slug == original


def test_distinct_meetings_same_day_get_distinct_slugs():
    a = _stub("2026-04-28", "Board of Commissioners", "Work Session", "14:00",
              sources=["county"], county_meeting_id=1)
    b = _stub("2026-04-28", "Board of Commissioners", "Work Session", "15:30",
              sources=["county"], county_meeting_id=2)
    cache.assign_slugs([a, b], {})
    assert a.slug != b.slug


def test_absorb_unions_documents_from_duplicate_cards():
    """The county lists one meeting twice; only one card carries the PDFs."""
    with_docs = _stub("2026-08-13", "Civil Service Board", "Regular Meeting",
                      "10:00", sources=["county"], county_meeting_id=3860)
    with_docs.documents = [
        Document("Agenda PDF", "https://x.invalid/agenda.pdf", "agenda", "county"),
        Document("Public Notice PDF", "https://x.invalid/notice.pdf", "notice", "county"),
    ]
    empty = _stub("2026-08-13", "Civil Service Board", "Regular Meeting",
                  "10:00", sources=["county"], county_meeting_id=3870)

    # Whichever card the listing shows first, no document may be lost.
    kept = county.absorb(empty, with_docs)
    assert sorted(d.kind for d in kept.documents) == ["agenda", "notice"]
    assert kept.agenda_published is True


def test_absorb_settles_on_the_lower_card_id_for_a_stable_slug():
    a = _stub("2026-08-13", "Civil Service Board", "Regular Meeting", "10:00",
              sources=["county"], county_meeting_id=3870)
    b = _stub("2026-08-13", "Civil Service Board", "Regular Meeting", "10:00",
              sources=["county"], county_meeting_id=3860)
    assert county.absorb(a, b).county_meeting_id == 3860
    c = _stub("2026-08-13", "Civil Service Board", "Regular Meeting", "10:00",
              sources=["county"], county_meeting_id=3860)
    d = _stub("2026-08-13", "Civil Service Board", "Regular Meeting", "10:00",
              sources=["county"], county_meeting_id=3870)
    assert county.absorb(c, d).county_meeting_id == 3860


def test_absorb_never_overwrites_real_data_with_blanks():
    keeper = _stub("2026-08-13", "Civil Service Board", "Regular Meeting", "10:00",
                   sources=["county"], county_meeting_id=1)
    keeper.location = "Juvenile Court Building"
    blank = _stub("2026-08-13", "Civil Service Board", "Regular Meeting", "10:00",
                  sources=["county"], county_meeting_id=2)
    blank.location = None
    assert county.absorb(keeper, blank).location == "Juvenile Court Building"


def test_absorb_fills_gaps_from_the_duplicate():
    keeper = _stub("2026-08-13", "Civil Service Board", "Regular Meeting", "10:00",
                   sources=["county"], county_meeting_id=1)
    keeper.location = None
    other = _stub("2026-08-13", "Civil Service Board", "Regular Meeting", "10:00",
                  sources=["county"], county_meeting_id=2)
    other.location = "Juvenile Court Building"
    assert county.absorb(keeper, other).location == "Juvenile Court Building"


def test_duplicate_cards_in_a_listing_produce_one_meeting_with_all_docs(cfg):
    """End-to-end over the parser, not just the helper."""
    html = (FIXTURES / "county_meetings.html").read_text("utf-8")
    meetings = county.parse_listing(html, cfg, "https://www.forsythco.com/meetings")
    m = next(x for x in meetings if x.date == "2026-08-11")
    dupe = county.parse_listing(html, cfg, "https://www.forsythco.com/meetings")
    twin = next(x for x in dupe if x.date == "2026-08-11")
    twin.documents = []
    merged = county.absorb(twin, m)
    assert len(merged.documents) == len(m.documents) > 0


def test_archive_carries_forward_meetings_outside_the_window():
    """Aged-out meetings must keep their page; CI builds into an empty dir."""
    old = _stub("2026-01-15", "Board of Commissioners", "Work Session", "14:00",
                sources=["county"], county_meeting_id=1)
    old.slug = "2026-01-15-board-of-commissioners-work-session"
    old.fetched_at = "2026-01-16T09:00:00-05:00"

    fresh = _stub("2026-08-11", "Board of Commissioners", "Work Session", "14:00",
                  sources=["county"], county_meeting_id=2)
    fresh.slug = "2026-08-11-board-of-commissioners-work-session"
    fresh.fetched_at = "2026-08-11T16:00:00-04:00"

    kept = cache.archive([old], [fresh])
    slugs = [m.slug for m in kept]
    assert old.slug in slugs and fresh.slug in slugs
    # The archived meeting must not claim to have been re-fetched.
    assert next(m for m in kept if m.slug == old.slug).fetched_at == \
        "2026-01-16T09:00:00-05:00"


def test_archive_prefers_freshly_fetched_data():
    stale = _stub("2026-08-11", "Board of Commissioners", "Work Session", "14:00",
                  sources=["county"])
    stale.slug = "s"
    stale.location = "Old Building"
    fresh = _stub("2026-08-11", "Board of Commissioners", "Work Session", "14:00",
                  sources=["county"])
    fresh.slug = "s"
    fresh.location = "New Building"

    kept = cache.archive([stale], [fresh])
    assert len(kept) == 1
    assert kept[0].location == "New Building"


def test_archive_means_nothing_is_reported_removed():
    old = _stub("2026-01-15", "Board of Commissioners", "Work Session", "14:00",
                sources=["county"])
    old.slug = "old"
    fresh = _stub("2026-08-11", "Board of Commissioners", "Work Session", "14:00",
                  sources=["county"])
    fresh.slug = "new"

    kept = cache.archive([old], [fresh])
    changes = cache.diff([old], kept)
    assert changes["removed"] == []
    assert [m.slug for m in changes["added"]] == ["new"]


def test_content_hash_changes_when_an_item_changes(cfg):
    from foco.model import AgendaItem

    m = _stub("2026-08-06", "Board of Commissioners", "Work Session", "14:00",
              sources=["county"])
    m.items = [AgendaItem(number="I", outline_path="I", title="Call to Order")]
    before = cache.content_hash(m)
    m.items[0].title = "Call Meeting to Order"
    assert cache.content_hash(m) != before
