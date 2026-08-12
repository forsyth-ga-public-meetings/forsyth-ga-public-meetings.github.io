"""Tests for the CivicClerk parsing layer.

These are the tests that matter: this is where the site rots silently when the
vendor changes their payload. Fixtures are real captured responses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foco import civicclerk
from foco.config import load_config
from foco.model import split_body_and_type
from foco.topics import clean_text, parse_dollar_amounts

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def agenda():
    return json.loads((FIXTURES / "civicclerk_agenda_389.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def items(agenda, cfg):
    return civicclerk.build_items(agenda["items"], cfg)


# ---------------------------------------------------------------------------
# timezone: the single most dangerous field in this API
# ---------------------------------------------------------------------------

def test_fake_utc_is_read_as_eastern_wall_time(cfg):
    """CivicClerk says 17:00:00Z for a meeting the county advertises as 5:00 PM."""
    dt = civicclerk.parse_civicclerk_datetime("2026-08-06T17:00:00Z", cfg)
    assert dt.hour == 17
    assert dt.strftime("%Z") == "EDT"
    assert dt.isoformat() == "2026-08-06T17:00:00-04:00"


def test_winter_meeting_gets_standard_time(cfg):
    dt = civicclerk.parse_civicclerk_datetime("2026-01-15T14:00:00Z", cfg)
    assert dt.isoformat() == "2026-01-15T14:00:00-05:00"


def test_unparseable_datetime_returns_none(cfg):
    assert civicclerk.parse_civicclerk_datetime("not-a-date", cfg) is None
    assert civicclerk.parse_civicclerk_datetime(None, cfg) is None


# ---------------------------------------------------------------------------
# body / type splitting across both vendors' spellings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,body,mtype",
    [
        ("Board of Commissioners Work Session",
         "Board of Commissioners", "Work Session"),
        ("Board of Commissioners Work Session Meeting",
         "Board of Commissioners", "Work Session Meeting"),
        # CivicClerk's spacing
        ("Board of Commissioners Regular Meeting / Public Hearings",
         "Board of Commissioners", "Regular Meeting / Public Hearings"),
        # The county's spacing -- normalises onto the canonical label, so the
        # two vendors produce one body, one type, and therefore one slug.
        ("Board of Commissioners Regular Meeting/Public Hearings",
         "Board of Commissioners", "Regular Meeting / Public Hearings"),
        ("Civil Service Board – Regular Meeting",
         "Civil Service Board", "Regular Meeting"),
        ("Zoning Board of Appeals Work Session",
         "Zoning Board of Appeals", "Work Session"),
    ],
)
def test_split_body_and_type(name, body, mtype):
    got_body, got_type = split_body_and_type(name)
    assert got_body == body
    assert got_type == mtype


def test_generic_meeting_suffix_stripped_when_a_real_body_remains():
    assert split_body_and_type("Parks & Recreation Board Meeting") == (
        "Parks & Recreation Board", "Meeting"
    )
    assert split_body_and_type("Forsyth County Ethics Panel Meeting") == (
        "Forsyth County Ethics Panel", "Meeting"
    )


def test_generic_meeting_suffix_kept_when_it_is_part_of_the_name():
    """'TriPartite' alone is not a board name -- the word belongs to the body."""
    body, mtype = split_body_and_type("TriPartite Meeting")
    assert body == "TriPartite Meeting"
    assert mtype is None


def test_trailing_punctuation_does_not_defeat_the_split():
    """The county publishes '... – Special Called Meeting .' with a stray period."""
    body, mtype = split_body_and_type(
        "Board of Voter Registrations & Elections – Special Called Meeting ."
    )
    assert body == "Board of Voter Registrations & Elections"
    assert mtype == "Special Called Meeting"


def test_unknown_body_is_not_guessed():
    body, mtype = split_body_and_type("Impact Fee Advisory Committee")
    assert body == "Impact Fee Advisory Committee"
    assert mtype is None


def test_both_slash_spellings_agree_on_body():
    a, _ = split_body_and_type("Board of Commissioners Regular Meeting / Public Hearings")
    b, _ = split_body_and_type("Board of Commissioners Regular Meeting/Public Hearings")
    assert a == b == "Board of Commissioners"


# ---------------------------------------------------------------------------
# item names arrive with raw HTML in them
# ---------------------------------------------------------------------------

def test_html_fragments_are_stripped_not_escaped():
    raw = ('<p style="margin-left:0in;" data-pasted="true">Board approval of '
           "Budget Resolution transferring the remaining funds</p>")
    assert clean_text(raw) == (
        "Board approval of Budget Resolution transferring the remaining funds"
    )


def test_zero_width_and_nbsp_are_removed():
    assert clean_text("Eagle Scout Laasya Kuchimanchi ​- County Clerk") == (
        "Eagle Scout Laasya Kuchimanchi - County Clerk"
    )


def test_no_item_title_contains_markup(items):
    for item in _flat(items):
        assert "<" not in item.title, f"unstripped markup in: {item.title!r}"


# ---------------------------------------------------------------------------
# numbering: preserved exactly, never invented
# ---------------------------------------------------------------------------

def test_top_level_numbering_is_preserved(items):
    numbers = [i.number for i in items]
    assert numbers[:5] == ["I", "II", "III", "IV", "V"]
    assert "VII" in numbers


def test_outline_paths_are_built_from_ancestry(items):
    consent = _by_number(items, "VII")
    assert consent.title == "Consent Agenda"
    ratification = consent.children[0]
    assert ratification.outline_path == "VII.1"
    lettered = {c.number: c for c in ratification.children}
    assert lettered["H"].outline_path == "VII.1.H"


def test_missing_number_stays_none_rather_than_invented(cfg):
    raw = [{"id": 1, "agendaObjectItemName": "Untitled item", "sortOrder": 1,
            "agendaObjectItemOutlineNumber": "", "childItems": []}]
    built = civicclerk.build_items(raw, cfg)
    assert built[0].number is None
    assert built[0].outline_path is None


# ---------------------------------------------------------------------------
# consent agenda -- the whole point of the site
# ---------------------------------------------------------------------------

def test_consent_heading_itself_is_not_marked_under_consent(items):
    consent = _by_number(items, "VII")
    assert consent.under_consent is False


def test_children_of_consent_agenda_are_flagged(items):
    consent = _by_number(items, "VII")
    for child in consent.children:
        assert child.under_consent is True
        assert child.consent_reason == "Consent Agenda"


def test_ratification_block_items_are_flagged(items):
    consent = _by_number(items, "VII")
    ratification = consent.children[0]
    assert "Ratification" in ratification.title
    assert len(ratification.children) >= 18
    for grandchild in ratification.children:
        assert grandchild.under_consent is True


def test_items_outside_consent_are_not_flagged(items):
    hearings = _by_number(items, "VIII")
    assert hearings.title.startswith("Public Hearings")
    for child in hearings.children:
        assert child.under_consent is False


# ---------------------------------------------------------------------------
# money and term flags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("in the amount of $254,200.00 for Consultant Services", [254200]),
        ("Change Order #1 in the amount of $11,333.00", [11333]),
        ("the county's proposed $239.4 million 2027 budget", [239_400_000]),
        ("no money here", []),
        ("$1,000 and $2,500,000", [2_500_000, 1000]),
    ],
)
def test_parse_dollar_amounts(text, expected):
    assert parse_dollar_amounts(text) == expected


def test_large_dollar_threshold_applied(items, cfg):
    flagged = [i for i in _flat(items) if i.large_dollar]
    assert flagged, "expected at least one item over the threshold"
    for item in flagged:
        assert max(item.dollar_amounts) >= cfg.large_dollar_threshold


def test_small_dollar_item_not_flagged(items):
    change_order = next(
        i for i in _flat(items) if "11,333" in i.title
    )
    assert change_order.dollar_amounts == [11333]
    assert change_order.large_dollar is False


def test_known_large_award_is_flagged(items):
    award = next(i for i in _flat(items) if "Blue Cypress Consulting" in i.title)
    assert 254200 in award.dollar_amounts
    assert award.large_dollar is True
    assert award.under_consent is True


# ---------------------------------------------------------------------------
# topic tagging
# ---------------------------------------------------------------------------

def test_millage_items_are_tagged_taxes(items):
    millage = [i for i in _flat(items) if "Millage" in i.title]
    assert millage
    for item in millage:
        assert "taxes" in item.topics


def test_precinct_item_is_tagged_ballot(items):
    precinct = next(i for i in _flat(items) if "precinct" in i.title.lower())
    assert "ballot" in precinct.topics


def test_homeland_security_grant_tagged_policing(items):
    gema = next(i for i in _flat(items) if "Emergency Management" in i.title)
    assert "policing" in gema.topics


# ---------------------------------------------------------------------------
# published files become machine-fetchable documents
# ---------------------------------------------------------------------------

def test_attachments_use_the_direct_pdf_endpoint(items):
    """GetAttachmentFile returns PDF bytes; the blob URL in the payload expires."""
    with_atts = [i for i in _flat(items) if i.attachments]
    assert with_atts, "fixture should contain items with attachments"
    for item in with_atts:
        for att in item.attachments:
            assert "GetAttachmentFile(fileId=" in att.url
            assert att.machine_readable is True
            assert att.kind == "attachment"
            # Never bake in the signed Azure blob URL -- it expires in ~7 days.
            assert "blob.core.windows.net" not in att.url
            assert "sig=" not in att.url


def test_attachment_labels_and_sizes_come_from_the_source(items):
    award = next(i for i in _flat(items) if "Blue Cypress Consulting" in i.title)
    assert award.attachments
    first = award.attachments[0]
    assert first.label
    assert first.content_type == "application/pdf"
    assert first.size_bytes and first.size_bytes > 0


def test_attachment_count_reflects_attachments(items):
    for item in _flat(items):
        assert item.attachment_count == len(item.attachments)


def test_meeting_files_use_stream_endpoint_not_json_wrapper(agenda, cfg):
    """GetMeetingFile returns {"blobUri": ...}; GetMeetingFileStream returns bytes."""
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
    event = next(e for e in events["value"] if e["id"] == 455)
    meeting = civicclerk.build_meeting(event, agenda, cfg)

    cc_docs = [d for d in meeting.documents
               if d.source == "civicclerk" and d.kind != "portal"]
    assert cc_docs
    for d in cc_docs:
        assert "GetMeetingFileStream(" in d.url
        assert "GetMeetingFile(" not in d.url.replace("GetMeetingFileStream(", "")


def test_plain_text_agenda_is_offered(agenda, cfg):
    """The most useful URL on the page for a client that cannot read PDFs."""
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
    event = next(e for e in events["value"] if e["id"] == 455)
    meeting = civicclerk.build_meeting(event, agenda, cfg)

    text_docs = [d for d in meeting.documents if d.kind == "transcript"]
    assert text_docs
    assert all("plainText=true" in d.url for d in text_docs)
    assert all(d.content_type == "text/plain" for d in text_docs)


def test_published_files_become_documents(agenda, cfg):
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
    event = next(e for e in events["value"] if e["id"] == 455)
    meeting = civicclerk.build_meeting(event, agenda, cfg)

    kinds = {d.kind for d in meeting.documents}
    assert {"agenda", "packet", "portal", "transcript"} <= kinds

    fetchable = [d for d in meeting.documents if d.machine_readable]
    assert any("GetMeetingFileStream" in d.url for d in fetchable)

    # The SPA page must never be advertised as machine readable.
    portal = next(d for d in meeting.documents if d.kind == "portal")
    assert portal.machine_readable is False


def test_meeting_without_agenda_says_so(cfg):
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
    event = events["value"][0]
    meeting = civicclerk.build_meeting(event, None, cfg)
    assert meeting.items == []
    assert meeting.agenda_published is False
    assert "not published an agenda" in meeting.agenda_status_note


# ---------------------------------------------------------------------------
# fail loudly
# ---------------------------------------------------------------------------

def test_missing_items_key_raises(cfg):
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
    event = events["value"][0]
    with pytest.raises(civicclerk.ParseError, match="no 'items' key"):
        civicclerk.build_meeting(event, {"agendaIsPublish": True}, cfg)


def test_bad_start_datetime_raises(cfg):
    with pytest.raises(civicclerk.ParseError, match="unparseable"):
        civicclerk.build_meeting(
            {"id": 1, "eventName": "X", "startDateTime": "garbage"}, None, cfg
        )


def test_non_odata_envelope_raises(cfg, monkeypatch):
    class FakeSession:
        def get_json(self, *a, **kw):
            return ["not", "an", "envelope"]

    with pytest.raises(civicclerk.ParseError, match="OData envelope"):
        civicclerk.fetch_events(FakeSession(), cfg)


# ---------------------------------------------------------------------------

def _flat(items):
    for i in items:
        yield from i.walk()


def _by_number(items, number):
    return next(i for i in items if i.number == number)
