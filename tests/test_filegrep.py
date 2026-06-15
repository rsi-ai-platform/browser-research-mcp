"""Tests for economical query-targeted file reading (the pdfgrep-style helpers)
and the CGA playbook. Pure / in-memory — no network."""
from __future__ import annotations

import io

from browser_research import playbooks, tools


# --------------------------------------------------------------------------
# Token matching + snippet.
# --------------------------------------------------------------------------

def test_query_tokens():
    assert tools._query_tokens("  Fiscal   Deficit ") == ["fiscal", "deficit"]
    assert tools._query_tokens("") == []


def test_text_matches_is_and_and_case_insensitive():
    assert tools._text_matches("April 2024 Fiscal Deficit",
                               ["fiscal", "deficit", "2024"])
    # missing one token → no match
    assert not tools._text_matches("April 2024 Receipts", ["fiscal", "deficit"])
    # empty tokens never match
    assert not tools._text_matches("anything", [])


def test_snippet_windows_around_match():
    text = "x" * 500 + " FISCAL DEFICIT figure 17.8 lakh crore " + "y" * 500
    snip = tools._snippet(text, ["fiscal", "deficit"], ctx=80)
    assert "FISCAL DEFICIT" in snip
    assert snip.startswith("…") and snip.endswith("…")
    assert len(snip) < 200


# --------------------------------------------------------------------------
# CSV + Excel grep return only matching rows.
# --------------------------------------------------------------------------

def test_grep_csv_returns_only_matches():
    csv_bytes = (b"Month,Item,Value\n"
                 b"April 2024,Fiscal Deficit,100\n"
                 b"May 2024,Total Receipts,200\n"
                 b"April 2024,Total Receipts,300\n")
    g = tools._grep_csv_sync(csv_bytes, "april fiscal deficit")
    assert g["match_count"] == 1
    m = g["matches"][0]
    assert m["row_index"] == 2 and m["header"] == ["Month", "Item", "Value"]
    assert m["row"] == ["April 2024", "Fiscal Deficit", "100"]


def test_grep_excel_returns_only_matches():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Month", "Item", "Value"])
    ws.append(["April 2024", "Fiscal Deficit", "100"])
    ws.append(["May 2024", "Total Receipts", "200"])
    buf = io.BytesIO()
    wb.save(buf)
    g = tools._grep_excel_sync(buf.getvalue(), "fiscal deficit", None)
    assert g["match_count"] == 1
    assert g["matches"][0]["row_index"] == 2
    assert g["matches"][0]["sheet"] == ws.title


def test_grep_csv_no_match_is_empty():
    g = tools._grep_csv_sync(b"a,b\n1,2\n", "nonexistent token")
    assert g["match_count"] == 0 and g["matches"] == []


# --------------------------------------------------------------------------
# CGA playbook is seeded and points at the dashboard .xlsm.
# --------------------------------------------------------------------------

def test_cga_playbook_matches_domain():
    pb = playbooks.match_playbook(playbooks.DEFAULT_PLAYBOOKS,
                                  "https://cga.nic.in/index.aspx")
    assert pb and pb["id"] == "cga-monthly-accounts"


def test_cga_playbook_surfaces_dashboard_xlsm():
    pb = playbooks.match_playbook(playbooks.DEFAULT_PLAYBOOKS,
                                  "https://cga.nic.in/index.aspx")
    proj = playbooks.for_agent(pb)
    blob = str(proj["open_data"])
    assert "MonthDashboardReport" in blob and ".xlsm" in blob


def test_default_playbooks_still_valid_with_cga():
    ok, err = playbooks.validate_playbooks(playbooks.DEFAULT_PLAYBOOKS)
    assert ok, err
