"""Tests for the Board of Education schedule parser.

The district publishes two different markups for the same thing: 2025 uses
<ul><li>Tuesday, January 21</li>, 2026 uses a run of <p>January 20</p>. Both
must yield the same result, with the year taken from the heading.
"""

from __future__ import annotations

import pytest

from foco import boe
from foco.config import load_config

SCHEDULE_HTML = """
<html><body><main>
<h1>Meeting Schedules</h1>
<p>Regular monthly meetings and work sessions of the Forsyth County Board of
Education begin at 5 pm unless otherwise noted.</p>
<p>Meetings are held at the Board of Education/Professional Development Center
at 1120 Dahlonega Highway, Cumming, unless otherwise noted.</p>

<h3>2025&nbsp;Monthly Regular Meeting Schedule</h3>
<ul>
  <li>Tuesday, January 21</li>
  <li>Tuesday, February 18</li>
  <li>Tuesday, December 9</li>
</ul>

<h3>2026&nbsp;Monthly Regular Meeting Schedule</h3>
<p>5 PM &ndash; Executive Session</p>
<p>*Public Portion begins at 6 PM</p>
<p>January 20</p>
<p>August 18</p>
<p>December 8</p>

<h3>2026&nbsp;Work Session Meeting Schedule</h3>
<h3>5 PM</h3>
<p>February 10</p>
<p>August 11</p>
</main></body></html>
"""


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def meetings(cfg):
    return boe.parse_schedule(SCHEDULE_HTML, cfg)


def _on(meetings, date):
    return [m for m in meetings if m.date == date]


def test_both_markup_styles_parse(meetings):
    assert _on(meetings, "2025-01-21"), "2025 <li> style"
    assert _on(meetings, "2026-08-18"), "2026 <p> style"


def test_year_comes_from_the_heading(meetings):
    dates = {m.date for m in meetings}
    assert "2025-12-09" in dates
    assert "2026-12-08" in dates


def test_meeting_types_are_distinguished(meetings):
    regular = _on(meetings, "2026-08-18")[0]
    work = _on(meetings, "2026-08-11")[0]
    assert regular.meeting_type == "Regular Meeting"
    assert work.meeting_type == "Work Session"


def test_start_time_read_from_the_page_not_assumed(meetings):
    assert _on(meetings, "2026-08-18")[0].start_time == "17:00"
    assert _on(meetings, "2026-08-11")[0].start_time == "17:00"


def test_start_iso_carries_eastern_offset(meetings):
    assert _on(meetings, "2026-08-18")[0].start_iso == "2026-08-18T17:00:00-04:00"


def test_public_portion_note_is_preserved(meetings):
    note = _on(meetings, "2026-08-18")[0].agenda_status_note
    assert "Public Portion begins at 6 PM" in note


def test_location_extracted_from_the_page(meetings):
    loc = _on(meetings, "2026-08-18")[0].location
    assert "Professional Development Center" in loc


def test_body_is_consistent(meetings):
    assert {m.body for m in meetings} == {"Forsyth County Board of Education"}


def test_agenda_is_not_claimed(meetings):
    """Agendas live on Simbli and are not mirrored -- pages must say so."""
    for m in meetings:
        assert m.items == []
        assert m.agenda_published is False
        assert "not mirrored here" in m.agenda_status_note


def test_simbli_link_is_marked_not_machine_readable(meetings):
    doc = _on(meetings, "2026-08-18")[0].documents[0]
    assert "simbli" in doc.url.lower()
    assert doc.machine_readable is False
    assert doc.kind == "portal"


def test_missing_headings_raise(cfg):
    with pytest.raises(boe.ParseError, match="markup has probably changed"):
        boe.parse_schedule("<html><body><p>nothing here</p></body></html>", cfg)


@pytest.mark.parametrize("text,expected", [
    ("5 PM", "17:00"),
    ("5 PM – Executive Session", "17:00"),
    ("*Public Portion begins at 6 PM", "18:00"),
    ("12 PM", "12:00"),
    ("no time here", None),
])
def test_parse_time_hint(text, expected):
    assert boe.parse_time_hint(text) == expected


def test_invalid_calendar_date_is_skipped(cfg):
    html = SCHEDULE_HTML.replace("<p>August 18</p>", "<p>February 30</p>")
    meetings = boe.parse_schedule(html, cfg)
    assert not [m for m in meetings if m.date.startswith("2026-02-3")]
