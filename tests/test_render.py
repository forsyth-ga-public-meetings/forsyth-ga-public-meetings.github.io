"""Tests for the HTML output.

The site's entire purpose is to be readable by a client that cannot execute
JavaScript, so the invariants here are about links actually resolving and the
page carrying no executable script.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from foco import civicclerk, render
from foco.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def meeting(cfg):
    agenda = json.loads((FIXTURES / "civicclerk_agenda_389.json").read_text("utf-8"))
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
    event = next(e for e in events["value"] if e["id"] == 455)
    m = civicclerk.build_meeting(event, agenda, cfg)
    m.slug = "2026-08-06-board-of-commissioners-regular-meeting-public-hearings"
    m.location = "Board of Commissioners Building"
    m.fetched_at = "2026-08-11T16:00:00-04:00"
    m.county_url = "https://www.forsythco.com/meetings/example/"
    return m


@pytest.fixture(scope="module")
def html(meeting, cfg):
    return render.render_meeting(meeting, cfg, site_base="https://example.test")


# ---------------------------------------------------------------------------
# no JavaScript
# ---------------------------------------------------------------------------

def test_no_executable_script_tags(html):
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts, "expected the JSON-LD block"
    for tag in scripts:
        assert 'type="application/ld+json"' in tag


def test_json_ld_is_valid_and_describes_an_event(html):
    block = re.search(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', html, re.S
    )
    data = json.loads(block.group(1))
    assert data["@type"] == "Event"
    assert data["startDate"].startswith("2026-08-06T17:00:00")


# ---------------------------------------------------------------------------
# the bug: topic chips linked to pages that do not exist
# ---------------------------------------------------------------------------

def test_topic_chips_are_not_links_by_default(html):
    """Linking to /topics/... before those pages exist produces dead links."""
    assert 'class="flag flag--topic"' in html
    assert not re.search(r'<a class="flag flag--topic"', html)


def test_topic_chips_become_links_when_requested(meeting, cfg):
    linked = render.render_meeting(meeting, cfg, link_topics=True)
    assert re.search(r'<a class="flag flag--topic" href="\.\./\.\./topics/', linked)


# ---------------------------------------------------------------------------
# the bug: attachments were inert text
# ---------------------------------------------------------------------------

def test_attachments_render_as_real_links(html):
    assert "GetAttachmentFile(fileId=" in html
    links = re.findall(r'<a href="([^"]*GetAttachmentFile[^"]*)"', html)
    assert len(links) > 10, "Aug 6 has many items with attachments"


def test_attachments_are_in_a_details_disclosure(html):
    assert "<details class=\"item__attachments\">" in html
    assert re.search(r"<summary>\d+ supporting documents?</summary>", html)


def test_no_expiring_blob_urls_in_output(html):
    """Signed Azure URLs expire in ~7 days; a static page must never embed one."""
    assert "blob.core.windows.net" not in html
    assert "sig=" not in html


# ---------------------------------------------------------------------------
# document presentation
# ---------------------------------------------------------------------------

def test_direct_downloads_and_portal_pages_are_separated(html):
    assert "Direct downloads" in html
    assert "Portal pages" in html
    assert "these URLs return the file itself" in html


def test_plain_text_agenda_is_surfaced(html):
    assert "plainText=true" in html
    assert "(plain text)" in html


def test_portal_link_is_present_but_marked(html, meeting):
    assert "portal.civicclerk.com" in html
    assert "JavaScript applications" in html


# ---------------------------------------------------------------------------
# content model requirements
# ---------------------------------------------------------------------------

def test_consent_items_are_visually_marked(html):
    assert "item--consent" in html
    assert "flag--consent" in html


def test_large_dollar_item_shows_the_figure(html):
    assert "$42,579,019" in html or "$254,200" in html


def test_real_dates_in_markup(html):
    assert '<time datetime="2026-08-06">' in html
    assert '<time datetime="2026-08-06T17:00:00-04:00">' in html


def test_authoritative_source_is_linked(html):
    assert "authoritative source" in html
    assert "https://www.forsythco.com/meetings/example/" in html


def test_fetched_at_timestamp_with_timezone(html):
    assert "2026-08-11T16:00:00-04:00" in html


def test_item_numbering_is_reproduced(html):
    assert ">VII.1.H<" in html
    assert ">VII<" in html


def test_canonical_uses_site_base(html):
    assert ('<link rel="canonical" href="https://example.test/meetings/'
            '2026-08-06-board-of-commissioners-regular-meeting-public-hearings/">') in html


def test_meeting_without_agenda_renders_a_status_note(cfg):
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
    m = civicclerk.build_meeting(events["value"][0], None, cfg)
    m.slug = "x"
    out = render.render_meeting(m, cfg)
    assert "not published an agenda" in out
    assert "typically" in out


def test_dates_do_not_use_platform_specific_strftime():
    """%-d is Linux-only; CI and local builds must produce identical HTML."""
    assert render._fmt_date("2026-08-06") == "Thursday, August 6, 2026"
    assert render._fmt_date("2026-09-01") == "Tuesday, September 1, 2026"
