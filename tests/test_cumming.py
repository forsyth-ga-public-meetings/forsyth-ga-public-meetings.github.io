"""Tests for the City of Cumming board-page parser."""

from __future__ import annotations

import pytest

from foco import cumming
from foco.config import load_config

BOARD = {"body": "Cumming City Council",
         "url": "https://www.cityofcumming.net/city-council-agendas-and-meeting-minutes",
         "location": "Cumming City Hall"}

# Real shapes taken from the city's four board pages. Note City Council uses
# IDENTICAL link text for the agenda and the minutes of the same meeting.
HTML = """
<html><body><main>
<a href="/wp-content/uploads/2026/05/Agenda-May-19-2026-Regular-meeting.pdf">May 19, 2026 Regular Meeting</a>
<a href="/wp-content/uploads/2026/06/5.19.26-Council-Minutes-FINAL.pdf">May 19, 2026 Regular Meeting</a>
<a href="/wp-content/uploads/2026/05/Agenda-May-5-2026-work-session.pdf">May 5, 2026 Work Session</a>
<a href="/wp-content/uploads/2026/06/Planning-Agenda-June-16-26.pdf">June 16, 2026 Meeting Agenda</a>
<a href="/wp-content/uploads/2026/06/6.16.26-Planning-Commission-FINAL.pdf">June 16, 2026 Meeting Minutes</a>
<a href="/wp-content/uploads/2026/03/DDA-Agenda-3.24.26.pdf">March 24, 2026 Meeting Notice &amp; Agenda (DDA Meeting)</a>
<a href="/wp-content/uploads/2025/05/Special-Called-Joint-Meeting-May-9-2025.pdf">May 9, 2025 Special Called Meeting with Forsyth County Taxpayer Coalition</a>
<a href="/some-other-page">Previous Agendas</a>
<a href="/wp-content/uploads/2021/10/BUISNESS-LICENSE-APPLICATION.pdf">Get a Business License</a>
</main></body></html>
"""


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def meetings(cfg):
    return cumming.parse_board(HTML, BOARD, cfg,
                               "https://www.cityofcumming.net/city-council-agendas")


def _on(meetings, date):
    return [m for m in meetings if m.date == date]


def test_undated_links_are_ignored(meetings):
    """'Get a Business License' is a PDF but not a meeting."""
    assert all("LICENSE" not in d.url
               for m in meetings for d in m.documents)


def test_agenda_and_minutes_collapse_into_one_meeting(meetings):
    """City Council reuses link text; only the URL distinguishes the two."""
    may19 = _on(meetings, "2026-05-19")
    assert len(may19) == 1
    kinds = sorted(d.kind for d in may19[0].documents)
    assert kinds == ["agenda", "minutes"]


def test_document_kind_comes_from_the_url_when_text_is_ambiguous(meetings):
    may19 = _on(meetings, "2026-05-19")[0]
    minutes = next(d for d in may19.documents if d.kind == "minutes")
    assert "Minutes" in minutes.url


def test_planning_style_agenda_and_minutes_group(meetings):
    june16 = _on(meetings, "2026-06-16")
    assert len(june16) == 1
    assert sorted(d.kind for d in june16[0].documents) == ["agenda", "minutes"]


def test_meeting_type_extracted(meetings):
    assert _on(meetings, "2026-05-19")[0].meeting_type == "Regular Meeting"
    assert _on(meetings, "2026-05-05")[0].meeting_type == "Work Session"


def test_document_words_are_not_mistaken_for_meeting_type(meetings):
    """'Meeting Agenda' describes the document, not the meeting type."""
    june16 = _on(meetings, "2026-06-16")[0]
    assert june16.meeting_type in (None, "Meeting")


def test_parenthetical_qualifier_stripped(meetings):
    """'Meeting Notice & Agenda (DDA Meeting)' -> a document, not a type."""
    march24 = _on(meetings, "2026-03-24")[0]
    assert march24.documents[0].kind == "agenda"


def test_long_special_called_type_preserved(meetings):
    m = _on(meetings, "2025-05-09")[0]
    assert m.meeting_type is not None
    assert "Special Called Meeting" in m.meeting_type


def test_time_is_not_invented_when_the_city_publishes_none(meetings):
    """These listing pages carry no times; the page must say so."""
    for m in meetings:
        assert m.start_time is None
        assert m.start_iso is None


def test_location_comes_from_config(meetings):
    assert all(m.location == "Cumming City Hall" for m in meetings)


def test_agenda_published_only_when_an_agenda_exists(meetings):
    assert _on(meetings, "2026-05-19")[0].agenda_published is True


def test_relative_urls_are_absolutised(meetings):
    for m in meetings:
        for d in m.documents:
            assert d.url.startswith("https://www.cityofcumming.net/")


def test_duplicate_urls_are_not_repeated(cfg):
    dupe = HTML.replace("</main>",
                        '<a href="/wp-content/uploads/2026/05/'
                        'Agenda-May-19-2026-Regular-meeting.pdf">'
                        "May 19, 2026 Regular Meeting</a></main>")
    meetings = cumming.parse_board(dupe, BOARD, cfg, "https://www.cityofcumming.net/x")
    may19 = _on(meetings, "2026-05-19")[0]
    assert len(may19.documents) == len({d.url for d in may19.documents})


def test_page_without_dated_links_raises(cfg):
    with pytest.raises(cumming.ParseError, match="markup has probably changed"):
        cumming.parse_board("<html><body><a href='/x.pdf'>Nothing</a></body></html>",
                            BOARD, cfg, "https://www.cityofcumming.net/x")


@pytest.mark.parametrize("text,url,expected", [
    ("Regular Meeting", "/uploads/Agenda-May-19-2026.pdf", "agenda"),
    ("Regular Meeting", "/uploads/5.19.26-Council-Minutes-FINAL.pdf", "minutes"),
    ("Meeting Minutes", "/uploads/whatever.pdf", "minutes"),
    ("Meeting Notice & Agenda", "/uploads/DDA-Agenda-3.24.26.pdf", "agenda"),
])
def test_classify(text, url, expected):
    assert cumming.classify(text, url) == expected
