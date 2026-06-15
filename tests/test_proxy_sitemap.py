"""Tests for the proxy plumbing and the sitemap/robots discovery helpers.
Pure functions only — no real browser, network, or proxy."""
from __future__ import annotations

import pytest

from browser_research import playbooks, tools


# --------------------------------------------------------------------------
# Proxy config parsing.
# --------------------------------------------------------------------------

def test_proxy_opts_none_when_unset(monkeypatch):
    for k in ("BROWSER_PROXY_SERVER", "BROWSER_PROXY_USERNAME",
              "BROWSER_PROXY_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert tools._proxy_opts() is None
    assert tools._httpx_proxy_url() is None


def test_proxy_opts_server_only(monkeypatch):
    monkeypatch.setenv("BROWSER_PROXY_SERVER", "http://gw.proxy.net:7000")
    monkeypatch.delenv("BROWSER_PROXY_USERNAME", raising=False)
    monkeypatch.delenv("BROWSER_PROXY_PASSWORD", raising=False)
    assert tools._proxy_opts() == {"server": "http://gw.proxy.net:7000"}
    # No creds → httpx url is just the server.
    assert tools._httpx_proxy_url() == "http://gw.proxy.net:7000"


def test_proxy_opts_with_auth(monkeypatch):
    monkeypatch.setenv("BROWSER_PROXY_SERVER", "http://gw.proxy.net:7000")
    monkeypatch.setenv("BROWSER_PROXY_USERNAME", "user@acme")
    monkeypatch.setenv("BROWSER_PROXY_PASSWORD", "p:w/d")
    opts = tools._proxy_opts()
    assert opts["username"] == "user@acme" and opts["password"] == "p:w/d"
    # httpx url embeds + percent-encodes the creds.
    url = tools._httpx_proxy_url()
    assert url == "http://user%40acme:p%3Aw%2Fd@gw.proxy.net:7000"


# --------------------------------------------------------------------------
# robots.txt + sitemap parsing.
# --------------------------------------------------------------------------

def test_parse_robots_extracts_sitemaps_and_disallow():
    body = ("User-agent: *\n"
            "Disallow: /admin\n"
            "Disallow: /tmp\n"
            "# a comment\n"
            "Sitemap: https://x.gov.in/sitemap.xml\n"
            "Sitemap: https://x.gov.in/sitemap_news.xml\n")
    out = tools._parse_robots(body)
    assert out["sitemaps"] == ["https://x.gov.in/sitemap.xml",
                               "https://x.gov.in/sitemap_news.xml"]
    assert "/admin" in out["disallow"] and "/tmp" in out["disallow"]


def test_parse_sitemap_xml_locs():
    xml = ("<?xml version='1.0'?><urlset>"
           "<url><loc>https://x.gov.in/a</loc></url>"
           "<url><loc> https://x.gov.in/data/report.xlsx </loc></url>"
           "</urlset>")
    locs = tools._parse_sitemap_xml(xml)
    assert locs == ["https://x.gov.in/a", "https://x.gov.in/data/report.xlsx"]


def test_decode_sitemap_plain_and_gzip():
    import gzip
    plain = tools._decode_sitemap_bytes(b"<urlset><loc>x</loc></urlset>", "u.xml")
    assert "<loc>x</loc>" in plain
    raw = gzip.compress(b"<urlset><url><loc>https://x/a.csv</loc></url></urlset>")
    text = tools._decode_sitemap_bytes(raw, "https://x/sitemap.xml.gz")
    assert tools._parse_sitemap_xml(text) == ["https://x/a.csv"]


def test_is_html_doc_detects_app_shell():
    assert tools._is_html_doc("  <!DOCTYPE html><html data-n-head-ssr>")
    assert tools._is_html_doc("<html lang='en'>")
    assert not tools._is_html_doc(
        "<?xml version='1.0'?><urlset><loc>x</loc></urlset>")


@pytest.mark.parametrize("url,expected", [
    ("https://x.gov.in/data/report.xlsx", True),
    ("https://x.gov.in/feed.json", True),
    ("https://x.gov.in/api/v1/series", True),
    ("https://x.gov.in/stats/api", True),
    ("https://x.gov.in/about-us", False),
    ("https://x.gov.in/news/article-123", False),
])
def test_is_data_like(url, expected):
    assert tools._is_data_like(url) is expected


# --------------------------------------------------------------------------
# Playbook proxy field.
# --------------------------------------------------------------------------

def test_validate_accepts_proxy_bool():
    ok, err = playbooks.validate_playbooks(
        [{"id": "p", "match": {"domain": "x.gov.in"}, "proxy": True}])
    assert ok, err


def test_validate_rejects_non_bool_proxy():
    ok, err = playbooks.validate_playbooks(
        [{"id": "p", "match": {"domain": "x.gov.in"}, "proxy": "yes"}])
    assert not ok and "proxy" in err


def test_pib_playbook_flags_proxy_and_projects_it():
    pb = playbooks.match_playbook(playbooks.DEFAULT_PLAYBOOKS,
                                  "https://pib.gov.in/allRel.aspx")
    assert pb and pb.get("proxy") is True
    assert playbooks.for_agent(pb).get("proxy") is True


# --------------------------------------------------------------------------
# Strategy ladder learned the discovery rung.
# --------------------------------------------------------------------------

def test_strategy_ladder_has_sitemap_rung():
    from browser_research import strategy
    tools_in_ladder = {r["tool"] for r in strategy.RESEARCH_STRATEGY["ladder"]}
    assert any("sitemap_probe" in t for t in tools_in_ladder)
