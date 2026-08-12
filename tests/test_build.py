"""End-to-end build test.

Builds a complete site from fixtures into a temp directory and asserts the
structural invariants that matter for a static, JS-free, crawlable site.
Self-contained: it never touches data/, so it runs in CI before any fetch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree

import pytest

from foco import cache, civicclerk, cli
from foco.config import load_config
from foco.model import Meeting

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://example.test"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    cfg = load_config()
    agenda = json.loads((FIXTURES / "civicclerk_agenda_389.json").read_text("utf-8"))
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))

    full = civicclerk.build_meeting(
        next(e for e in events["value"] if e["id"] == 455), agenda, cfg
    )
    pending = Meeting(
        slug="", body="Zoning Board of Appeals", meeting_type="Work Session",
        title="Zoning Board of Appeals Work Session", date="2026-09-01",
        start_time="18:30", start_iso="2026-09-01T18:30:00-04:00",
        location="Board of Commissioners Building", sources=["county"],
    )
    meetings = [full, pending]
    cache.assign_slugs(meetings, {})
    cache.stamp(meetings, cfg)

    data_dir = tmp_path_factory.mktemp("data")
    cache.save(meetings, data_dir)
    out = tmp_path_factory.mktemp("site")

    rc = cli.main([
        "--data-dir", str(data_dir), "build",
        "--out", str(out), "--site-base", BASE,
    ])
    assert rc == 0
    return out, meetings


def test_expected_files_exist(built):
    out, meetings = built
    for name in ("index.html", "sitemap.xml", "robots.txt", "rss.xml",
                 "llms.txt", "meetings.json", "assets/style.css"):
        assert (out / name).exists(), f"missing {name}"
    for m in meetings:
        assert (out / "meetings" / m.slug / "index.html").exists()
        assert (out / "meetings" / m.slug / "index.json").exists()


def test_no_broken_internal_links(built):
    """A dead internal link is the failure mode a crawler notices first."""
    out, _ = built
    broken = []
    for page in out.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        for href in re.findall(r'href="([^"]+)"', html):
            if href.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = (page.parent / href).resolve()
            if target.is_dir():
                target = target / "index.html"
            if not target.exists():
                broken.append(f"{page.relative_to(out).as_posix()} -> {href}")
    assert broken == [], f"broken internal links: {broken}"


def test_no_executable_script_anywhere(built):
    out, _ = built
    offenders = [
        p.relative_to(out).as_posix()
        for p in out.rglob("*.html")
        if re.search(r'<script(?! type="application/ld\+json")',
                     p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_no_expiring_urls_anywhere(built):
    """Signed Azure blob URLs expire in ~7 days and must never be published."""
    out, _ = built
    offenders = []
    for p in out.rglob("*"):
        if p.is_file() and p.suffix in (".html", ".json", ".xml", ".txt"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            if "blob.core.windows.net" in text or re.search(r"[?&]sig=", text):
                offenders.append(p.relative_to(out).as_posix())
    assert offenders == []


def test_sitemap_covers_every_generated_page(built):
    out, meetings = built
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    root = ElementTree.parse(out / "sitemap.xml").getroot()
    locs = {u.find(f"{ns}loc").text for u in root.findall(f"{ns}url")}
    for m in meetings:
        assert f"{BASE}/meetings/{m.slug}/" in locs
    assert f"{BASE}/" in locs


def test_every_meeting_json_twin_parses(built):
    out, meetings = built
    for m in meetings:
        data = json.loads(
            (out / "meetings" / m.slug / "index.json").read_text("utf-8")
        )
        assert data["slug"] == m.slug
        assert data["date"] == m.date


def test_dataset_download_matches_page_count(built):
    out, meetings = built
    data = json.loads((out / "meetings.json").read_text("utf-8"))
    assert len(data) == len(meetings)


def test_topic_chips_link_in_a_full_build(built):
    """In a full build the topic pages exist, so chips must be clickable."""
    out, meetings = built
    html = (out / "meetings" / meetings[0].slug / "index.html").read_text("utf-8")
    assert re.search(r'<a class="flag flag--topic" href="\.\./\.\./topics/', html)


def test_single_page_build_skips_site_files(tmp_path):
    """A one-page build must not emit an index that links to unbuilt pages."""
    cfg = load_config()
    agenda = json.loads((FIXTURES / "civicclerk_agenda_389.json").read_text("utf-8"))
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
    m = civicclerk.build_meeting(
        next(e for e in events["value"] if e["id"] == 455), agenda, cfg
    )
    cache.assign_slugs([m], {})
    cache.stamp([m], cfg)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cache.save([m], data_dir)
    out = tmp_path / "site"

    rc = cli.main(["--data-dir", str(data_dir), "build", "--slug", m.slug,
                   "--out", str(out), "--site-base", BASE])
    assert rc == 0
    assert (out / "meetings" / m.slug / "index.html").exists()
    assert not (out / "index.html").exists()
    assert not (out / "sitemap.xml").exists()

    html = (out / "meetings" / m.slug / "index.html").read_text("utf-8")
    assert not re.search(r'<a class="flag flag--topic"', html)
