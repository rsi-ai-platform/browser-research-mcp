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
        "  - `extract(url, focus)`: visit + Sonnet structured extraction. "
        "    Returns the SAME shape as pdf_fetch_structured / "
        "    web_fetch_structured (title, dateline, summary, key_facts, "
        "    numeric_values, dates, tables_summary). Picks up numbers from "
        "    the screenshot too — useful for chart pages where the values "
        "    are drawn, not text.\n\n"
        "INDIAN FISCAL YEAR: a table labelled '2025-2026' / 'FY26' spans "
        "April 2025 → March 2026 — the April…December columns are the FIRST "
        "year and Jan-March are the SECOND. Never read 'April' as the "
        "current calendar year by default."
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
