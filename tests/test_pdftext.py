"""Tests for PDF-derived agenda extraction.

The risk here is not crashing -- it is confidently producing wrong structure.
These tests pin the conservative behaviour: numbers are only recorded when the
PDF shows one, continuations are joined, and noise is dropped.
"""

from __future__ import annotations

import pytest

from foco import pdftext
from foco.config import load_config
from foco.model import Document, Meeting


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# Representative of the county's real agenda text, including the wrapping that
# broke the first version of the line joiner.
SAMPLE = """
Forsyth County Planning Commission
AGENDA
August 25, 2026
Page 1 of 4

I. Call meeting to order and Pledge of Allegiance
II. Amend and adopt the agenda
V. Old Business
1. CP260008 - Joseph Mazur - Commission District 1
3. Reduce the setback from the right-of-way to the parking area from 10 ft. to 0 ft. (UDC
Table 17.1).
4. Reduce the landscape strip between the front property line and any vehicular
use area from 10 ft. to 0 ft. (UDC 17-5.7).
VII. Public Hearings
1. CP250013 - Stanislav Prisacari - Commission District 3
- 2 -
"""


def test_numbered_lines_become_items(cfg):
    items = pdftext.build_items_from_text(SAMPLE, cfg)
    numbers = [i.number for i in items]
    assert "I" in numbers and "II" in numbers and "VII" in numbers


def test_unclosed_paren_continuation_is_joined(cfg):
    """'... (UDC' + 'Table 17.1).' must become one item, not a truncated one."""
    items = pdftext.build_items_from_text(SAMPLE, cfg)
    item = next(i for i in items if "right-of-way" in i.title)
    assert item.title.endswith("(UDC Table 17.1).")


def test_lowercase_continuation_is_joined(cfg):
    items = pdftext.build_items_from_text(SAMPLE, cfg)
    item = next(i for i in items if "landscape strip" in i.title)
    assert "vehicular use area" in item.title


def test_page_noise_is_dropped(cfg):
    lines = pdftext.clean_lines(SAMPLE)
    assert not any(line.strip() in ("Page 1 of 4", "- 2 -") for line in lines)


def test_short_fragments_are_not_items(cfg):
    items = pdftext.build_items_from_text("1. ok\n2. also short\n", cfg)
    assert items == []


def test_no_items_invented_from_unnumbered_text(cfg):
    text = "Call meeting to order\nAdoption of agenda\nExecutive session\n"
    assert pdftext.build_items_from_text(text, cfg) == []


def test_derived_items_are_flat(cfg):
    items = pdftext.build_items_from_text(SAMPLE, cfg)
    assert all(i.level == 0 and not i.children for i in items)


def test_consent_context_is_tracked(cfg):
    text = (
        "IV. Consent Agenda\n"
        "1. Award of contract in the amount of $250,000.00 for paving\n"
        "V. New Business\n"
        "2. Rezoning request for parcel 123-456 with no consent status\n"
    )
    items = pdftext.build_items_from_text(text, cfg)
    heading = next(i for i in items if i.title == "Consent Agenda")
    award = next(i for i in items if "paving" in i.title)
    later = next(i for i in items if "Rezoning" in i.title)
    # The heading opens the block but is not itself under it.
    assert heading.under_consent is False
    assert award.under_consent is True
    # "V. New Business" ends the block even though it carries a number.
    assert later.under_consent is False


def test_flags_are_applied_to_derived_items(cfg):
    text = "1. Award of contract in the amount of $250,000.00 for millage study\n"
    item = pdftext.build_items_from_text(text, cfg)[0]
    assert item.large_dollar is True
    assert 250000 in item.dollar_amounts
    assert "taxes" in item.topics


# ---------------------------------------------------------------------------
# document selection
# ---------------------------------------------------------------------------

def _meeting(**docs):
    m = Meeting(slug="s", body="Planning Commission", meeting_type="Work Session",
                title="t", date="2026-08-18", start_time="18:30",
                start_iso="2026-08-18T18:30:00-04:00", location=None,
                sources=["county"])
    m.documents = list(docs.get("documents", []))
    return m


def test_agenda_pdf_preferred_over_notice():
    m = _meeting(documents=[
        Document("Public Notice PDF", "https://x.invalid/notice.pdf", "notice", "county"),
        Document("Agenda PDF", "https://x.invalid/agenda.pdf", "agenda", "county"),
    ])
    doc = pdftext._agenda_document(m)
    assert doc.url.endswith("agenda.pdf")


def test_county_pdf_preferred_over_api_endpoint():
    m = _meeting(documents=[
        Document("Agenda", "https://api.invalid/GetMeetingFileStream(fileId=1,plainText=false)",
                 "agenda", "civicclerk", content_type="application/pdf"),
        Document("Agenda PDF", "https://x.invalid/a.pdf", "agenda", "county"),
    ])
    assert pdftext._agenda_document(m).source == "county"


def test_no_agenda_document_returns_none():
    m = _meeting(documents=[
        Document("Public Notice PDF", "https://x.invalid/n.pdf", "notice", "county"),
    ])
    assert pdftext._agenda_document(m) is None


def test_js_only_document_is_never_selected():
    m = _meeting(documents=[
        Document("Portal", "https://portal.invalid/event/1/files", "portal",
                 "civicclerk", machine_readable=False),
    ])
    assert pdftext._agenda_document(m) is None


# ---------------------------------------------------------------------------
# enrichment never overrides structured data
# ---------------------------------------------------------------------------

def test_enrich_skips_meetings_that_already_have_items(cfg, tmp_path):
    from foco.model import AgendaItem

    m = _meeting(documents=[
        Document("Agenda PDF", "https://x.invalid/a.pdf", "agenda", "county"),
    ])
    m.items = [AgendaItem(number="I", outline_path="I", title="Call to Order")]

    class ExplodingSession:
        def get(self, *a, **kw):
            raise AssertionError("must not fetch when structured items exist")

    assert pdftext.enrich([m], ExplodingSession(), cfg, tmp_path) == 0
    assert len(m.items) == 1
    assert m.agenda_derived_from_pdf is False
