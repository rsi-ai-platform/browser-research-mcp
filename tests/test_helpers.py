"""Unit tests for the pure helpers — no browser, no network, no LLM.

Covers the request-body parsing / body-encoding used by the new API-replay
tools, the adaptive recovery-hint logic in act(), and the playbook `api`
field validation / matching / projection.
"""
from __future__ import annotations

from browser_research import playbooks, tools


# --------------------------------------------------------------------------
# tools._parse_request_body — turns a captured XHR post-body into params.
# --------------------------------------------------------------------------

def test_parse_request_body_form():
    out = tools._parse_request_body("financialYear=2023-2024&reportBy=4&pageId=138")
    assert out == {"kind": "form",
                   "data": {"financialYear": "2023-2024", "reportBy": "4",
                            "pageId": "138"}}


def test_parse_request_body_json():
    out = tools._parse_request_body('{"year": "2024-25", "n": 3}')
    assert out["kind"] == "json"
    assert out["data"] == {"year": "2024-25", "n": 3}


def test_parse_request_body_raw_and_empty():
    assert tools._parse_request_body(None) is None
    assert tools._parse_request_body("   ") is None
    raw = tools._parse_request_body("just-some-token-blob")
    assert raw["kind"] == "raw"


# --------------------------------------------------------------------------
# tools._encode_body — dict → form (default) or JSON; str passthrough.
# --------------------------------------------------------------------------

def test_encode_body_form_default():
    wire, ct = tools._encode_body({"a": "1", "b": "2"}, None)
    assert wire in ("a=1&b=2", "b=2&a=1")
    assert ct == "application/x-www-form-urlencoded"


def test_encode_body_json():
    wire, ct = tools._encode_body({"a": 1}, "application/json")
    assert wire == '{"a": 1}'
    assert ct == "application/json"


def test_encode_body_str_passthrough_and_none():
    assert tools._encode_body("x=1", None) == ("x=1", None)
    assert tools._encode_body(None, None) == (None, None)


# --------------------------------------------------------------------------
# tools._recovery_hint — UI step failed but an endpoint was observed.
# --------------------------------------------------------------------------

def test_recovery_hint_fires_on_failed_select():
    steps = [{"step_index": 0, "action": "select", "ok": False,
              "error": "Timeout"}]
    observed = [{"method": "POST",
                 "url": "https://x.gov.in/Ajax/getData",
                 "request_body": {"kind": "form",
                                  "data": {"financialYear": "2024-2025"}}}]
    hint = tools._recovery_hint(steps, observed)
    assert hint and "call_api" in hint and "getData" in hint


def test_recovery_hint_silent_when_all_ok():
    steps = [{"step_index": 0, "action": "select", "ok": True}]
    observed = [{"method": "POST", "url": "https://x/y", "request_body": {}}]
    assert tools._recovery_hint(steps, observed) is None


# --------------------------------------------------------------------------
# playbooks — api field validation, matching, projection.
# --------------------------------------------------------------------------

def test_default_playbooks_validate():
    ok, err = playbooks.validate_playbooks(playbooks.DEFAULT_PLAYBOOKS)
    assert ok, err


def test_validate_accepts_api_only_entry():
    entry = [{"id": "x", "match": {"domain": "x.gov.in"},
              "api": [{"endpoint": "https://x.gov.in/a", "method": "POST",
                       "params": {"y": "1"}}]}]
    ok, err = playbooks.validate_playbooks(entry)
    assert ok, err


def test_validate_rejects_api_without_endpoint():
    entry = [{"id": "x", "match": {"domain": "x.gov.in"},
              "api": [{"method": "POST"}]}]
    ok, err = playbooks.validate_playbooks(entry)
    assert not ok and "endpoint" in err


def test_validate_rejects_bad_api_params():
    entry = [{"id": "x", "match": {"domain": "x.gov.in"},
              "api": [{"endpoint": "https://x/y", "params": "nope"}]}]
    ok, err = playbooks.validate_playbooks(entry)
    assert not ok and "params" in err


def test_gas_path_matches_most_specific_playbook():
    pb = playbooks.match_playbook(
        playbooks.DEFAULT_PLAYBOOKS,
        "https://ppac.gov.in/natural-gas/consumption")
    assert pb and pb["id"] == "ppac-natural-gas-consumption"
    assert pb["api"][0]["endpoint"].endswith("getGasConsumption")


def test_consumption_path_matches_products_playbook():
    pb = playbooks.match_playbook(
        playbooks.DEFAULT_PLAYBOOKS,
        "https://ppac.gov.in/consumption/products-wise")
    assert pb and pb["id"] == "ppac-consumption"


def test_for_agent_includes_api():
    pb = playbooks.match_playbook(
        playbooks.DEFAULT_PLAYBOOKS,
        "https://ppac.gov.in/natural-gas/consumption")
    projected = playbooks.for_agent(pb)
    assert "api" in projected and projected["api"][0]["method"] == "POST"


# --------------------------------------------------------------------------
# playbooks._merge_playbooks — GCS overlay layered on code defaults by id.
# --------------------------------------------------------------------------

def test_merge_overlay_overrides_and_extends():
    defaults = [{"id": "a", "match": {"domain": "a.in"}, "strategy": "DA"},
                {"id": "b", "match": {"domain": "b.in"}, "strategy": "DB"}]
    overlay = [{"id": "b", "match": {"domain": "b.in"}, "strategy": "OB"},
               {"id": "c", "match": {"domain": "c.in"}, "strategy": "OC"}]
    merged = playbooks._merge_playbooks(defaults, overlay)
    by = {p["id"]: p for p in merged}
    assert [p["id"] for p in merged] == ["a", "b", "c"]
    assert by["a"]["strategy"] == "DA"   # default-only id stays
    assert by["b"]["strategy"] == "OB"   # overlay wins per-id
    assert by["c"]["strategy"] == "OC"   # overlay-only id appended


def test_merge_empty_overlay_is_defaults():
    assert playbooks._merge_playbooks(playbooks.DEFAULT_PLAYBOOKS, []) == \
        list(playbooks.DEFAULT_PLAYBOOKS)


def test_save_caches_merged_not_raw_overlay(monkeypatch):
    import asyncio
    monkeypatch.setattr(playbooks, "_save_to_gcs_sync", lambda e: None)
    overlay = [{"id": "ppac-consumption",
                "match": {"domain": "ppac.gov.in", "path_prefix": "/consumption"},
                "strategy": "OVERRIDDEN"}]
    asyncio.run(playbooks.save_playbooks(overlay))
    cached = playbooks._cache["data"]
    ids = {p["id"] for p in cached}
    assert "cga-monthly-accounts" in ids   # code-default-only id still served
    assert next(p for p in cached if p["id"] == "ppac-consumption")["strategy"] \
        == "OVERRIDDEN"                      # overlay still wins per-id


# --------------------------------------------------------------------------
# smart_fetch — playbook-aware dispatch (api / open_data / render) + FY fill.
# --------------------------------------------------------------------------

def test_derive_fy():
    assert tools._derive_fy("gas consumption 2023-2024") == "2023-2024"
    assert tools._derive_fy("for 2023-24 please") == "2023-2024"
    assert tools._derive_fy("FY24 numbers") == "2023-2024"
    assert tools._derive_fy("FY2026") == "2025-2026"
    assert tools._derive_fy("just 2024 alone") is None   # ambiguous → None


def test_template_api_params():
    base = {"financialYear": "<FY e.g. 2023-2024>", "reportBy": "4", "pageId": "138"}
    assert tools._template_api_params(base, "natural gas 2023-24") == \
        {"financialYear": "2023-2024", "reportBy": "4", "pageId": "138"}
    assert tools._template_api_params(base, "natural gas") is None   # no FY
    assert tools._template_api_params({"q": "<term>"}, "x") is None  # unfillable
    assert tools._template_api_params({"a": "1"}, "x") == {"a": "1"}  # concrete


def test_smart_fetch_acts_on_api_recipe(monkeypatch):
    import asyncio
    gas = next(p for p in playbooks.DEFAULT_PLAYBOOKS
               if p["id"] == "ppac-natural-gas-consumption")
    seen = {}

    async def fake_match(u):
        return gas

    async def fake_call_api(endpoint, **k):
        seen["body"] = k.get("body")
        return {"json": {"result": {"x": 1}}}

    async def fake_sonnet(visited, **k):
        return {"summary": "structured", "key_facts": []}

    async def fake_extract(url, **k):
        seen["render"] = True
        return {"summary": "rendered"}

    monkeypatch.setattr(playbooks, "match_for_url", fake_match)
    monkeypatch.setattr(tools, "call_api", fake_call_api)
    monkeypatch.setattr(tools, "_sonnet_extract", fake_sonnet)
    monkeypatch.setattr(tools, "extract", fake_extract)
    out = asyncio.run(tools.smart_fetch(
        "https://ppac.gov.in/natural-gas/consumption", focus="gas 2023-24"))
    assert out["rung_used"] == "api"
    assert out["playbook_id"] == "ppac-natural-gas-consumption"
    assert seen["body"]["financialYear"] == "2023-2024"   # templated from focus
    assert "render" not in seen                            # did NOT fall back


def test_smart_fetch_render_fallback(monkeypatch):
    import asyncio

    async def fake_match(u):
        return None

    async def fake_extract(url, **k):
        return {"summary": "rendered"}

    monkeypatch.setattr(playbooks, "match_for_url", fake_match)
    monkeypatch.setattr(tools, "extract", fake_extract)
    out = asyncio.run(tools.smart_fetch("https://example.com/x", focus="hi"))
    assert out["rung_used"] == "render"
    assert "playbook_id" not in out


# --------------------------------------------------------------------------
# Firecrawl fallback rung — opt-in, tried first when configured.
# --------------------------------------------------------------------------

def test_firecrawl_disabled_without_key(monkeypatch):
    import asyncio
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert asyncio.run(tools._firecrawl_fetch("https://x.gov.in", text_cap=10)) is None


def test_fallback_tries_firecrawl_first(monkeypatch):
    import asyncio
    order = []

    async def fc(u, *, text_cap):
        order.append("firecrawl")
        return {"text": "FC", "source": "firecrawl", "url": u, "domain": "d",
                "title": "", "fetched_at": "t", "current_date": "d"}

    async def tv(u, *, text_cap):
        order.append("tavily")
        return None

    async def wf(u, *, text_cap):
        order.append("web_fetch")
        return None

    monkeypatch.setattr(tools, "_firecrawl_fetch", fc)
    monkeypatch.setattr(tools, "_tavily_fetch", tv)
    monkeypatch.setattr(tools, "_anthropic_web_fetch", wf)
    out = asyncio.run(tools._fallback_fetch("https://x", text_cap=100, reason="r"))
    assert out["source"] == "firecrawl" and out["fallback_reason"] == "r"
    assert order == ["firecrawl"]   # firecrawl won; cheaper rungs not needed


def test_fallback_skips_firecrawl_when_empty(monkeypatch):
    import asyncio
    order = []

    async def fc(u, *, text_cap):
        order.append("firecrawl")     # no key / empty
        return None

    async def tv(u, *, text_cap):
        order.append("tavily")
        return {"text": "TV", "source": "tavily", "url": u, "domain": "d",
                "title": "", "fetched_at": "t", "current_date": "d"}

    monkeypatch.setattr(tools, "_firecrawl_fetch", fc)
    monkeypatch.setattr(tools, "_tavily_fetch", tv)
    out = asyncio.run(tools._fallback_fetch("https://x", text_cap=100, reason="r"))
    assert out["source"] == "tavily" and order == ["firecrawl", "tavily"]


# --------------------------------------------------------------------------
# Headful retry rung — real-window fallback for content/fingerprint blocks,
# skipped for IP-level network resets (same egress IP → same failure).
# --------------------------------------------------------------------------

def test_headful_disabled_by_env(monkeypatch):
    import asyncio
    monkeypatch.setenv("HEADFUL_RETRY", "false")
    # Returns before touching Xvfb / launching a browser.
    assert asyncio.run(tools._headful_fetch("https://x", text_cap=10)) is None


def test_fallback_skips_headful_on_network_error(monkeypatch):
    import asyncio
    called = []

    async def hf(u, *, text_cap):
        called.append("headful")
        return None

    async def fc(u, *, text_cap):
        called.append("firecrawl")
        return None

    async def tv(u, *, text_cap):
        called.append("tavily")
        return None

    async def wf(u, *, text_cap):
        called.append("web_fetch")
        return None

    monkeypatch.setattr(tools, "_headful_fetch", hf)
    monkeypatch.setattr(tools, "_firecrawl_fetch", fc)
    monkeypatch.setattr(tools, "_tavily_fetch", tv)
    monkeypatch.setattr(tools, "_anthropic_web_fetch", wf)
    asyncio.run(tools._fallback_fetch(
        "https://x", text_cap=10,
        reason="goto:Page.goto: net::ERR_CONNECTION_RESET at https://x"))
    assert "headful" not in called          # IP-level block → headful skipped
    assert called[0] == "firecrawl"         # straight to the off-box rungs


def test_fallback_skips_headful_on_goto_timeout(monkeypatch):
    import asyncio
    called = []

    async def hf(u, *, text_cap):
        called.append("headful")
        return None

    async def fc(u, *, text_cap):
        called.append("firecrawl")
        return {"text": "FC", "source": "firecrawl", "url": u, "domain": "d",
                "title": "", "fetched_at": "t", "current_date": "d"}

    monkeypatch.setattr(tools, "_headful_fetch", hf)
    monkeypatch.setattr(tools, "_firecrawl_fetch", fc)
    # A goto timeout is a network failure too — headful (same egress IP) would
    # just burn its own 25s goto, so the positive content-block gate skips it.
    out = asyncio.run(tools._fallback_fetch(
        "https://x", text_cap=10, reason="goto:Timeout 45000ms exceeded"))
    assert "headful" not in called and out["source"] == "firecrawl"


def test_fallback_runs_headful_on_marker_block(monkeypatch):
    import asyncio
    called = []

    async def hf(u, *, text_cap):
        called.append("headful")
        return {"text": "HF", "source": "headful", "url": u, "domain": "d",
                "title": "", "fetched_at": "t", "current_date": "d"}

    monkeypatch.setattr(tools, "_headful_fetch", hf)
    # marker:… reasons (from _looks_blocked) are content blocks → headful runs.
    out = asyncio.run(tools._fallback_fetch(
        "https://x", text_cap=10, reason="marker:access denied"))
    assert called == ["headful"] and out["source"] == "headful"


def test_fallback_tries_headful_first_on_content_block(monkeypatch):
    import asyncio
    called = []

    async def hf(u, *, text_cap):
        called.append("headful")
        return {"text": "HF", "source": "headful", "url": u, "domain": "d",
                "title": "", "fetched_at": "t", "current_date": "d"}

    async def fc(u, *, text_cap):
        called.append("firecrawl")
        return None

    monkeypatch.setattr(tools, "_headful_fetch", hf)
    monkeypatch.setattr(tools, "_firecrawl_fetch", fc)
    out = asyncio.run(tools._fallback_fetch(
        "https://x", text_cap=10, reason="challenge_title"))
    assert out["source"] == "headful" and out["fallback_reason"] == "challenge_title"
    assert called == ["headful"]            # headful won; off-box rungs not needed
