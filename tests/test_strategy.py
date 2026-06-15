"""Tests for the adaptive advisor — diagnose_next routes a tool result to the
right next rung based on the signal present. Pure; no browser/network."""
from __future__ import annotations

from browser_research import strategy


def test_auth_wall_wins():
    ns = strategy.diagnose_next({"auth_wall": "auth_wall:register to download"},
                                "visit")
    assert ns["based_on"] == "auth_wall"
    assert "download_file" in ns["tools"]


def test_blocked_routes_to_open_data():
    ns = strategy.diagnose_next({"blocked": "challenge_title"}, "visit")
    assert ns["based_on"] == "blocked"


def test_recovery_hint_routes_to_call_api():
    ns = strategy.diagnose_next(
        {"recovery_hint": "… replay with call_api …",
         "observed_api": [{"method": "POST", "url": "x"}]}, "act")
    assert ns["based_on"] == "recovery_hint"
    assert ns["tools"] == ["call_api"]


def test_observed_api_without_hint():
    ns = strategy.diagnose_next(
        {"observed_api": [{"method": "POST", "url": "x"}]}, "act")
    assert ns["based_on"] == "observed_api"
    assert ns["tools"] == ["call_api"]


def test_file_links_routes_to_download():
    ns = strategy.diagnose_next(
        {"text": "x" * 5000,
         "file_links": [{"href": "a.xlsx", "format": "xlsx"}]}, "visit")
    assert ns["based_on"] == "file_links"
    assert "download_file" in ns["tools"]


def test_sparse_dom_flags_spa_shell():
    ns = strategy.diagnose_next({"text": "Loading..."}, "visit")
    assert ns["based_on"] == "sparse_dom"
    assert "inspect_network" in ns["tools"]


def test_healthy_page_returns_none():
    assert strategy.diagnose_next({"text": "x" * 5000}, "visit") is None


def test_walls_take_priority_over_files():
    # auth_wall present alongside file_links → wall wins (nothing else works).
    ns = strategy.diagnose_next(
        {"auth_wall": "auth_wall:members only",
         "file_links": [{"href": "a.pdf", "format": "pdf"}]}, "visit")
    assert ns["based_on"] == "auth_wall"


def test_research_strategy_shape():
    rs = strategy.RESEARCH_STRATEGY
    assert {"summary", "ladder", "signals", "principles"} <= set(rs)
    assert len(rs["ladder"]) >= 6
    assert all("tool" in rung for rung in rs["ladder"])
    assert strategy.STRATEGY_INSTRUCTIONS.strip()
