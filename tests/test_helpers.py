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
