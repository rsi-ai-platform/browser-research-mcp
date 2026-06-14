"""FastMCP server — exposes visit + extract over stdio/SSE/streamable-http.

  uvx browser-research                          # stdio (Claude Desktop / Cursor)
  uvx browser-research --transport streamable-http --port 7862    # HTTP
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import tools


def _bind(ctx: Context | None) -> None:
    cid = getattr(ctx, "client_id", None) if ctx is not None else None
    tools.set_current_client(cid)


mcp = FastMCP(
    "browser-research",
    instructions=(
        "Browser-based research. Use these tools as the LAST RUNG of the "
        "fetch ladder — only when the cheaper rungs (pdf_fetch, "
        "http_post_form, web_fetch / web_fetch_structured on the "
        "authority-web-search MCP) have all failed. This MCP drives a real "
        "Chromium so it reads JavaScript-rendered tables, charts drawn via "
        "canvas / SVG, login-walled dashboards, and dropdowns whose data "
        "lives only in client-side state.\n\n"
        "TOOLS:\n"
        "  - `visit(url)`: open the page, return DOM text + screenshot. "
        "    Cheap, no LLM call.\n"
        "  - `act(url, steps)`: click / type / select through a flow, then "
        "    Sonnet-extract the final page. For dropdowns + dashboards.\n"
        "  - `extract(url, focus)`: visit + Sonnet structured extraction. "
        "    Returns the SAME shape as pdf_fetch_structured / "
        "    web_fetch_structured (title, dateline, summary, key_facts, "
        "    numeric_values, dates, tables_summary). Picks up numbers from "
        "    the screenshot too — useful for chart pages where the values "
        "    are drawn, not text.\n"
        "  - `download_file(url)`: download a .xlsx/.xlsm/.xls/.csv/.tsv "
        "    or .pdf and parse it end-to-end (openpyxl / xlrd / csv / "
        "    pypdf). Returns sheets + sample rows (spreadsheets) or page "
        "    text (PDFs) plus a classified error_kind on failure. THIS is "
        "    what you call on every entry in the `file_links` array that "
        "    `visit` / `act` surface — DO NOT `visit` a file URL, it will "
        "    just stream binary.\n\n"
        "INDIAN FISCAL YEAR: a table labelled '2025-2026' / 'FY26' spans "
        "April 2025 → March 2026 — the April…December columns are the FIRST "
        "year and Jan-March are the SECOND. Never read 'April' as the "
        "current calendar year by default.\n\n"
        "FILE-AS-DATA WORKFLOW. Gov sites in India often publish the actual "
        "numbers ONLY as Excel / PDF attachments (GST at "
        "gst.gov.in/download/gststatistics, CGA monthly accounts, MoSPI "
        "Excel press kits, RBI circulars). For these:\n"
        "  1. `visit` the index page to surface `file_links`.\n"
        "  2. Pick the entry whose anchor text matches your target period.\n"
        "  3. `download_file` on its href.\n"
        "  4. Read the `sheets[].sample` (or PDF `content`) for the answer.\n"
        "Do not bounce the user to another MCP for file parsing — that is "
        "now this MCP's job too."
    ),
)


@mcp.tool()
async def visit(
    url: str,
    wait_for_selector: str | None = None,
    wait_extra_ms: int = 1500,
    timeout_ms: int = 45000,
    screenshot: bool = True,
    full_page_screenshot: bool = False,
    text_cap: int = 30000,
    return_screenshot_b64: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Open a URL with a real Chromium and return its rendered state.

    Use when the cheaper fetch tools (web_fetch, pdf_fetch, http_post_form)
    fail because the page is a SPA, JS-rendered chart, login-walled, or has
    a dropdown that's not a separate URL.

    Args:
        url: The page URL.
        wait_for_selector: Optional CSS selector to await before reading the
            DOM. Use when data appears only after an AJAX call returns —
            e.g. ".chart svg", "table#monthly tbody tr".
        wait_extra_ms: Extra settle time after the wait fires (default 1500).
        timeout_ms: Hard navigation timeout (default 45s).
        screenshot: Whether to capture a PNG INTERNALLY (default True). Adds
            ~200ms; the bytes are used by extract()/act() for Sonnet vision.
        full_page_screenshot: Scroll-stitch the whole page (default False).
        text_cap: Cap on extracted text length (default 30000).
        return_screenshot_b64: Whether to ECHO the base64 PNG back in the
            response. DEFAULT False — typical screenshots are 700KB-1MB and
            accumulating them across an agent's tool-call history blows the
            1M-token context window in ~3 calls. Only opt in when the caller
            actually consumes the bytes (e.g. a browser-canvas UI).

    Returns:
        {url, title, domain, text, screenshot_bytes, screenshot_b64 (opt-in),
         fetched_at, current_date}
    """
    _bind(ctx)
    return await tools.visit(
        url,
        wait_for_selector=wait_for_selector,
        wait_extra_ms=wait_extra_ms,
        timeout_ms=timeout_ms,
        screenshot=screenshot,
        full_page_screenshot=full_page_screenshot,
        text_cap=text_cap,
        return_screenshot_b64=return_screenshot_b64,
    )


@mcp.tool()
async def act(
    url: str,
    steps: list[dict[str, Any]],
    focus: str = "",
    timeout_ms: int = 60000,
    full_page_screenshot: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Drive a real Chromium through a sequence of steps, then run Sonnet
    structured extraction on the final state.

    Use this when the data is BEHIND an interaction — a Year/Month dropdown
    that fires AJAX inline, a tab to click, a "Load more" button, a form
    to submit. `visit` and `extract` only read the page as it loaded;
    `act` clicks/types/selects first.

    Steps are a list of single-key dicts:
        {"click":  "css-selector"}
        {"fill":   {"selector": "#q", "value": "x"}}
        {"select": {"selector": "#year", "value": "2024-2025"}}
        {"press":  {"selector": "#q", "key": "Enter"}}
        {"scroll": {"to": "bottom"|"top"|<int px>}}
        {"wait_for_selector": "css-selector"}
        {"wait_for_load_state": "networkidle"|"load"}
        {"wait_ms": 1500}
        {"goto":   "https://…"}     // mid-flow navigation
        {"screenshot": {"name": "after-select"}}    // logged, not returned

    Example — pull PPAC FY2024-25 monthly consumption (a flow that needs
    the year dropdown change to fire an AJAX request):
        act(
          url="https://ppac.gov.in/consumption/products-wise",
          steps=[
            {"wait_for_selector": "#financialYear"},
            {"select": {"selector": "#financialYear", "value": "2024-2025"}},
            {"wait_for_load_state": "networkidle"},
            {"wait_ms": 2000},
          ],
          focus="FY2024-25 monthly LPG, MS, HSD, ATF consumption",
        )

    Returns the same shape as `extract` PLUS `step_results` (per-step
    timing + ok/error) and `final_url`.

    Args:
        url: Starting page URL.
        steps: Ordered list of action dicts (vocabulary above).
        focus: Extraction focus passed to Sonnet.
        timeout_ms: Per-step navigation / wait timeout.
        full_page_screenshot: Whether the final screenshot is full-page.

    Returns:
        {url, domain, title, dateline, summary, key_facts[],
         numeric_values[], dates[], tables_summary[], step_results[],
         final_url, kind: "browser"}.
    """
    _bind(ctx)
    return await tools.act(
        url, steps,
        focus=focus,
        timeout_ms=timeout_ms,
        full_page_screenshot=full_page_screenshot,
    )


@mcp.tool()
async def extract(
    url: str,
    focus: str = "",
    wait_for_selector: str | None = None,
    full_page_screenshot: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Visit a URL → focused Sonnet structured extraction.

    Sends BOTH rendered text AND a screenshot to Sonnet — so numbers drawn
    via canvas / SVG (chart values on PPAC, RBI, NSE dashboards) that don't
    appear in the DOM still get extracted. Same returned shape as
    pdf_fetch_structured / web_fetch_structured on authority-web-search-mcp.

    Args:
        url: The page URL.
        focus: What to extract, e.g. "monthly LPG, MS, HSD consumption for
               FY2024-25" or "Q4 FY26 EBITDA margin and revenue".
        wait_for_selector: Optional CSS selector to await (see visit).
        full_page_screenshot: Default True so charts below the fold are seen.

    Returns:
        {url, domain, title, dateline, summary, key_facts[], numeric_values[],
         dates[], tables_summary[], kind: "browser"}.
    """
    _bind(ctx)
    return await tools.extract(
        url,
        focus=focus,
        wait_for_selector=wait_for_selector,
        full_page_screenshot=full_page_screenshot,
    )


@mcp.tool()
async def download_file(
    url: str,
    sheet: str | None = None,
    pages: list[int] | None = None,
    max_rows_per_sheet: int = 200,
    max_pdf_pages: int = 30,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Download a file URL and parse its contents end-to-end.

    Supported formats (auto-detected by content-type + magic bytes):
        .xlsx / .xlsm   →  openpyxl, all sheets parsed
        .xls            →  xlrd (legacy Excel)
        .csv / .tsv     →  csv.Sniffer (handles , ; \\t)
        .pdf            →  pypdf, text per page

    Use this on every `file_links` entry that `visit` / `act` surface.
    Sites like gst.gov.in/download/gststatistics, cga.nic.in monthly
    accounts and mospi.gov.in press kits publish their actual numbers
    ONLY as attachments — `visit` will just stream binary at you.

    Args:
        url: Absolute URL of the file.
        sheet: Optional sheet name (.xlsx/.xls). Default: parse all.
        pages: Optional 1-indexed PDF page list. Default: first N pages.
        max_rows_per_sheet: Cap on rows per spreadsheet sheet (default 200).
        max_pdf_pages: Cap on PDF pages parsed when `pages` is None
            (default 30).

    Returns:
        Spreadsheet → {kind: "spreadsheet", url, domain, format,
            sheet_count, sheets[{name, rows, cols, header, sample}],
            content, fetched_at}.
        PDF → {kind: "pdf", url, domain, content, page_count,
            pages_extracted, content_truncated, fetched_at}.
        Error → {error, error_kind, url, domain, …}.
        error_kind ∈ {http_error, html_masquerade, truncated_body,
            invalid_xlsx, parse_error, wrong_content_type, too_large}.
    """
    _bind(ctx)
    return await tools.download_file(
        url,
        sheet=sheet,
        pages=pages,
        max_rows_per_sheet=max_rows_per_sheet,
        max_pdf_pages=max_pdf_pages,
    )
