"""Tests for index, topic pages, sitemap, RSS and llms.txt."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from foco import cache, civicclerk, sitegen
from foco.config import load_config
from foco.model import Meeting

FIXTURES = Path(__file__).parent / "fixtures"
SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
BASE = "https://example.test"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def meetings(cfg):
    agenda = json.loads((FIXTURES / "civicclerk_agenda_389.json").read_text("utf-8"))
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))

    full = civicclerk.build_meeting(
        next(e for e in events["value"] if e["id"] == 455), agenda, cfg
    )
    full.fetched_at = "2026-08-11T16:00:00-04:00"

    pending = Meeting(
        slug="", body="Board of Commissioners",
        meeting_type="Regular Meeting / Public Hearings",
        title="Board of Commissioners Regular Meeting / Public Hearings",
        date="2026-08-20", start_time="17:00",
        start_iso="2026-08-20T17:00:00-04:00",
        location="Board of Commissioners Building", sources=["county"],
    )
    pending.fetched_at = "2026-08-11T16:00:00-04:00"

    ms = [full, pending]
    cache.assign_slugs(ms, {})
    return ms


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def test_index_splits_upcoming_and_past(meetings, cfg):
    html = sitegen.render_index(meetings, cfg, site_base=BASE, today="2026-08-11")
    assert "Upcoming meetings" in html
    assert "Recent meetings" in html
    # The Aug 20 meeting is upcoming; Aug 6 is past.
    up = html.index("Upcoming meetings")
    past = html.index("Recent meetings")
    assert html.index("2026-08-20", up) < past


def test_index_lists_every_meeting(meetings, cfg):
    html = sitegen.render_index(meetings, cfg, site_base=BASE, today="2026-08-11")
    for m in meetings:
        assert f'href="meetings/{m.slug}/"' in html


def test_index_flags_meeting_without_agenda(meetings, cfg):
    html = sitegen.render_index(meetings, cfg, site_base=BASE, today="2026-08-11")
    assert "no agenda published yet" in html


def test_index_summarises_consent_counts(meetings, cfg):
    html = sitegen.render_index(meetings, cfg, site_base=BASE, today="2026-08-11")
    assert "consent</span>" in html


def test_index_has_no_executable_script(meetings, cfg):
    html = sitegen.render_index(meetings, cfg, site_base=BASE)
    assert "<script" not in html


# ---------------------------------------------------------------------------
# topic pages
# ---------------------------------------------------------------------------

def test_taxes_topic_collects_millage_items(meetings, cfg):
    html = sitegen.render_topic("taxes", meetings, cfg, site_base=BASE)
    assert "Millage" in html
    assert "matching agenda items" in html


def test_topic_page_links_back_to_the_meeting(meetings, cfg):
    html = sitegen.render_topic("taxes", meetings, cfg, site_base=BASE)
    assert f'href="../../meetings/{meetings[0].slug}/"' in html


def test_topic_page_carries_a_matching_caveat(meetings, cfg):
    """Keyword matching over- and under-matches; the page must say so."""
    html = sitegen.render_topic("taxes", meetings, cfg, site_base=BASE)
    assert "finding aid" in html


def test_empty_topic_says_so_rather_than_rendering_blank(meetings, cfg):
    html = sitegen.render_topic("schools", meetings, cfg, site_base=BASE)
    assert "No agenda items currently match" in html


def test_every_configured_topic_renders(meetings, cfg):
    for key in cfg.topic_clusters():
        html = sitegen.render_topic(key, meetings, cfg, site_base=BASE)
        assert "<h1>" in html


# ---------------------------------------------------------------------------
# sitemap / robots
# ---------------------------------------------------------------------------

def test_sitemap_is_well_formed_and_absolute(meetings, cfg):
    xml = sitegen.render_sitemap(meetings, cfg, site_base=BASE)
    root = ElementTree.fromstring(xml)
    locs = [u.find(f"{SM_NS}loc").text for u in root.findall(f"{SM_NS}url")]
    assert f"{BASE}/" in locs
    assert all(loc.startswith("https://") for loc in locs)
    for m in meetings:
        assert f"{BASE}/meetings/{m.slug}/" in locs


def test_sitemap_includes_topic_pages(meetings, cfg):
    xml = sitegen.render_sitemap(meetings, cfg, site_base=BASE)
    for key in cfg.topic_clusters():
        assert f"{BASE}/topics/{key}/" in xml


def test_robots_points_at_the_sitemap():
    txt = sitegen.render_robots(BASE)
    assert f"Sitemap: {BASE}/sitemap.xml" in txt
    assert "Allow: /" in txt


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def _changelog():
    return [
        {"at": "2026-08-11T16:00:00-04:00", "kind": "new",
         "slug": "2026-08-20-x", "date": "2026-08-20",
         "title": "Board of Commissioners — Regular Meeting",
         "summary": "Added to the index. No agenda published yet."},
        {"at": "2026-08-10T16:00:00-04:00", "kind": "changed",
         "slug": "2026-08-06-y", "date": "2026-08-06",
         "title": "Board of Commissioners — Work Session",
         "summary": "Agenda published: 59 items."},
    ]


def test_rss_is_well_formed(cfg):
    xml = sitegen.render_rss(_changelog(), cfg, site_base=BASE)
    root = ElementTree.fromstring(xml)
    items = root.findall("./channel/item")
    assert len(items) == 2


def test_rss_distinguishes_new_from_changed(cfg):
    xml = sitegen.render_rss(_changelog(), cfg, site_base=BASE)
    assert "New meeting:" in xml
    assert "Agenda changed:" in xml


def test_rss_links_are_absolute(cfg):
    xml = sitegen.render_rss(_changelog(), cfg, site_base=BASE)
    root = ElementTree.fromstring(xml)
    for item in root.findall("./channel/item"):
        assert item.find("link").text.startswith(f"{BASE}/meetings/")


def test_rss_handles_empty_changelog(cfg):
    xml = sitegen.render_rss([], cfg, site_base=BASE)
    root = ElementTree.fromstring(xml)
    assert root.findall("./channel/item") == []


# ---------------------------------------------------------------------------
# llms.txt
# ---------------------------------------------------------------------------

def test_llms_txt_states_it_is_not_authoritative(meetings, cfg):
    txt = sitegen.render_llms_txt(meetings, cfg, site_base=BASE)
    assert "NOT authoritative" in txt
    assert "Forsyth County is." in txt


def test_llms_txt_warns_off_legal_deadlines(meetings, cfg):
    txt = sitegen.render_llms_txt(meetings, cfg, site_base=BASE)
    assert "legal deadline" in txt
    assert "Do not rely on this site for those dates." in txt


def test_llms_txt_explains_the_js_distinction(meetings, cfg):
    """The whole point: tell an agent which URLs it can actually read."""
    txt = sitegen.render_llms_txt(meetings, cfg, site_base=BASE)
    assert "Direct downloads" in txt
    assert "Portal pages" in txt
    assert "empty shell" in txt


def test_llms_txt_advertises_machine_readable_endpoints(meetings, cfg):
    txt = sitegen.render_llms_txt(meetings, cfg, site_base=BASE)
    assert f"{BASE}/meetings.json" in txt
    assert "index.json" in txt
    assert f"{BASE}/sitemap.xml" in txt


def test_llms_txt_documents_the_flags(meetings, cfg):
    txt = sitegen.render_llms_txt(meetings, cfg, site_base=BASE)
    assert "Consent agenda" in txt
    assert f"${cfg.large_dollar_threshold:,}" in txt


# ---------------------------------------------------------------------------
# change detection
# ---------------------------------------------------------------------------

def test_describe_change_for_new_meeting_with_agenda(meetings):
    text = cache.describe_change(None, meetings[0])
    assert "Added to the index" in text
    assert "consent agenda" in text


def test_describe_change_for_new_meeting_without_agenda(meetings):
    text = cache.describe_change(None, meetings[1])
    assert "No agenda published yet" in text


def test_describe_change_detects_agenda_publication(meetings):
    before = Meeting(**{**meetings[1].to_dict(), "documents": [], "items": []})
    text = cache.describe_change(before, meetings[0])
    assert "Agenda published" in text


def test_describe_change_detects_time_move(meetings):
    before = Meeting.from_dict(meetings[1].to_dict())
    after = Meeting.from_dict(meetings[1].to_dict())
    after.start_iso = "2026-08-20T18:00:00-04:00"
    assert "Meeting time changed" in cache.describe_change(before, after)


def test_changelog_round_trip(tmp_path, meetings):
    changes = {"added": [meetings[1]], "changed": [], "removed": []}
    fresh = cache.append_changelog(tmp_path, changes, "2026-08-11T16:00:00-04:00")
    assert len(fresh) == 1
    stored = cache.load_changelog(tmp_path)
    assert stored[0]["slug"] == meetings[1].slug
    assert stored[0]["kind"] == "new"


def test_changelog_keeps_newest_first(tmp_path, meetings):
    cache.append_changelog(tmp_path, {"added": [meetings[0]], "changed": [],
                                      "removed": []}, "2026-08-10T00:00:00-04:00")
    cache.append_changelog(tmp_path, {"added": [meetings[1]], "changed": [],
                                      "removed": []}, "2026-08-11T00:00:00-04:00")
    stored = cache.load_changelog(tmp_path)
    assert stored[0]["at"] > stored[-1]["at"]
