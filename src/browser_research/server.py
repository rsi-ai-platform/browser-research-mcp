"""FastMCP server — exposes visit + extract over stdio/SSE/streamable-http.

  uvx browser-research                          # stdio (Claude Desktop / Cursor)
  uvx browser-research --transport streamable-http --port 7862    # HTTP
"""
from __future__ import annotations

import logging
import os
import sys
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP

# Aliased: the `strategy` MCP tool below would otherwise shadow this module.
from . import playbooks, tools
from . import strategy as strategy_module

log = logging.getLogger("browser_research")


def _bind(ctx: Context | None) -> None:
    cid = getattr(ctx, "client_id", None) if ctx is not None else None
    tools.set_current_client(cid)


def _attach_next_step(result: dict[str, Any], tool: str) -> dict[str, Any]:
    """Ride the adaptive advisor along with the data: name the recommended next
    rung from the signals in `result`. Best-effort — never break a tool call."""
    try:
        if isinstance(result, dict) and "next_step" not in result:
            ns = strategy_module.diagnose_next(result, tool)
            if ns:
                result["next_step"] = ns
    except Exception as e:  # noqa: BLE001
        log.debug("next_step attach failed: %s", e)
    return result


async def _resolve_proxy(url: str, explicit: bool | None) -> bool:
    """Decide whether a call routes through the residential proxy. An explicit
    use_proxy from the caller wins; otherwise honour the matched playbook's
    `proxy` hint (so domains known to block datacenter egress auto-proxy)."""
    if explicit is not None:
        return explicit
    try:
        pb = await _match_playbook(url)
        return bool(pb and pb.get("proxy"))
    except Exception as e:  # noqa: BLE001
        log.debug("proxy resolve failed: %s", e)
        return False


async def _match_playbook(url: str) -> dict[str, Any] | None:
    if not url:
        return None
    try:
        return await playbooks.match_for_url(url)
    except Exception as e:  # noqa: BLE001
        log.debug("playbook lookup failed: %s", e)
        return None


def _strategy_was_from_playbook(
    tool: str,
    result: dict[str, Any],
    playbook_entry: dict[str, Any] | None,
    *,
    url: str,
) -> bool:
    """Whether this successful run already followed an existing playbook recipe."""
    if not isinstance(playbook_entry, dict):
        return False

    if tool == "smart_fetch":
        return (
            result.get("playbook_id") == playbook_entry.get("id")
            and str(result.get("rung_used", "")).lower() in {"api", "open_data"}
        )

    if tool == "call_api":
        for rec in (playbook_entry.get("api") or []):
            if isinstance(rec, dict) and str(rec.get("endpoint") or "") == url:
                return True
        return False

    if tool == "download_file":
        for rec in (playbook_entry.get("open_data") or []):
            if not isinstance(rec, dict):
                continue
            rec_tool = str(rec.get("tool") or "download_file")
            if rec_tool == "download_file" and str(rec.get("url") or "") == url:
                return True
        return False

    return False


async def _attach_playbook(
    result: dict[str, Any],
    url: str,
    tool: str = "",
    *,
    matched_playbook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """If `url` matches a domain playbook, ride the recipe along in the result
    so the agent gets it on its FIRST call — no exploration. Also attach the
    `next_step` advisor. Best-effort: neither lookup may break a tool call."""
    try:
        if isinstance(result, dict):
            pb = matched_playbook or await _match_playbook(url)
            if pb:
                result.setdefault("playbook", playbooks.for_agent(pb))
    except Exception as e:  # noqa: BLE001
        log.debug("playbook attach failed: %s", e)
    if tool:
        _attach_next_step(result, tool)
    await _save_success_strategy(
        url, tool, result, matched_playbook=matched_playbook)
    return result


def _domain(url: str) -> str:
    try:
        net = urlparse(url).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def _strategy_for_success(tool: str, result: dict[str, Any]) -> str | None:
    rung = str(result.get("rung_used", "")).strip().lower()
    if tool == "smart_fetch":
        if rung == "api":
            return ("Prefer API replay (`call_api`) for this site; it is more "
                    "reliable than driving custom JS widgets.")
        if rung == "open_data":
            return ("Use the playbook's `open_data` attachment/source first, "
                    "then parse with `download_file`.")
        if rung == "render":
            return ("No executable API/open-data recipe matched; use rendered "
                    "browser extraction for this site.")
    if tool == "download_file":
        return ("Published attachments are the primary data source on this "
                "site; prefer `download_file` over page rendering.")
    if tool == "call_api":
        return ("Replay this site's data endpoint with `call_api` from the "
                "page origin instead of brittle UI interactions.")
    if tool == "rescue_extract":
        return ("When native browser/API/file rungs fail, run "
                "`rescue_extract` as the final fallback path.")
    return None


def _result_is_success(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    # Bot walls and login gates are not successful retrievals.
    if result.get("blocked") or result.get("auth_wall"):
        return False
    return True


def _playbook_prefers_rescue(entry: dict[str, Any] | None) -> bool:
    return bool(isinstance(entry, dict) and entry.get("prefer_rescue") is True)


def _upsert_api_recipe(entry: dict[str, Any], endpoint: str,
                       params: dict[str, Any] | None = None) -> None:
    if not endpoint:
        return
    api = entry.get("api")
    if not isinstance(api, list):
        api = []
        entry["api"] = api
    for rec in api:
        if isinstance(rec, dict) and rec.get("endpoint") == endpoint:
            if params and rec.get("params") is None:
                rec["params"] = params
            return
    api.append({
        "endpoint": endpoint,
        "method": "POST",
        "params": params if isinstance(params, dict) else None,
        "note": "Auto-captured from a successful run.",
    })


async def _save_success_strategy(
    url: str,
    tool: str,
    result: dict[str, Any],
    *,
    matched_playbook: dict[str, Any] | None = None,
) -> None:
    """Best-effort playbook learning: persist successful strategy/rung."""
    if not tool or not _result_is_success(result):
        return
    pb = matched_playbook
    if pb is None:
        pb = await _match_playbook(url)
    if _strategy_was_from_playbook(tool, result, pb, url=url):
        return
    strategy = _strategy_for_success(tool, result)
    if not strategy:
        return
    dom = _domain(url)
    if not dom:
        return
    try:
        entries = await playbooks.get_playbooks(force=True)
        # Clone shallowly so we do not mutate cache objects in-place.
        updated: list[dict[str, Any]] = [
            dict(e) if isinstance(e, dict) else e for e in entries
        ]
        matched = playbooks.match_playbook(updated, url)
        if not isinstance(matched, dict):
            matched = {
                "id": f"auto-{dom}",
                "match": {"domain": dom},
            }
            updated.append(matched)
        matched["strategy"] = strategy
        matched["last_verified"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if tool == "rescue_extract":
            matched["prefer_rescue"] = True
        elif tool == "smart_fetch":
            # A normal smart_fetch success means native rungs worked;
            # do not keep forcing rescue-first.
            matched["prefer_rescue"] = False

        if tool == "smart_fetch" and result.get("rung_used") == "api":
            _upsert_api_recipe(
                matched,
                str(result.get("api_endpoint") or ""),
                result.get("api_params")
                if isinstance(result.get("api_params"), dict)
                else None,
            )
        await playbooks.save_playbooks(updated)
    except Exception as e:  # noqa: BLE001
        log.debug("playbook strategy save failed: %s", e)


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
        "    just stream binary.\n"
        "  - `sitemap_probe(url)`: read robots.txt + sitemap(s); surfaces "
        "    data_like_urls (.csv/.xlsx/.json/.pdf, /api) you can fetch "
        "    directly. Cheapest discovery step — run it when unsure where the "
        "    data lives.\n"
        "  - `inspect_network(url, steps?)`: open the page (optionally running "
        "    act-style steps) and report the XHR/fetch calls it fires — "
        "    endpoint, method, request params, response sample. The DISCOVERY "
        "    step for JS dashboards.\n"
        "  - `call_api(url, method, body)`: replay a data endpoint directly "
        "    from the page's own origin (cookies / CSRF / referer all match). "
        "    The REPLAY step — reaches data the UI never exposes.\n"
        "  - `smart_fetch(url, focus)`: playbook-AWARE one-call fetch — consults "
        "    the URL's playbook and ACTS on it (replays its `api`, pulls its "
        "    `open_data`, else renders). Prefer it when a site may have a recipe.\n\n"
        "  - `rescue_extract(url, extraction_prompt, ...)`: LAST-RUNG rescue. "
        "    Use only after native rungs fail (blocked/auth_wall/no-progress). "
        "    Creates + starts a Kryptos single-URL job, waits for completion, "
        "    and returns ONLY compact file names + text tree for that job.\n"
        "  - `rescue_fetch(job_name, file_path, ...)`: download exactly one file "
        "    from an existing Kryptos job (data/<job_name>/...) to local disk, "
        "    then use normal file tools on that local path."
        "\n"
        "  - `rescue_wait(job_id, job_name, ...)`: poll progress "
        "    (`/api/progress/{jobId}`) every 10s until terminal (completed/"
        "failed/cancelled), then poll GCS up to ~60s for sync lag."
        "\n\n"
        "FLOW (LEVELS) — prefer this order unless a playbook already gives a "
        "direct recipe:\n"
        "  - Level 0 (anchor): `today` for time grounding, `strategy` when you "
        "need a decision refresher.\n"
        "  - Level 1 (cheap discovery): `sitemap_probe` then `visit` to map the "
        "site and detect whether content is page text, API-fed, or file links.\n"
        "  - Level 2 (UI interaction): `act` for dropdown/tab/button flows when "
        "the data is behind interaction.\n"
        "  - Level 3 (API path): `inspect_network` to discover XHR/fetch calls, "
        "then `call_api` to replay the endpoint directly.\n"
        "  - Level 4 (playbook-aware one-call): `smart_fetch` to execute "
        "playbook recipes (`api`/`open_data`) or render fallback.\n"
        "  - Level 5 (file-as-data): `download_file` for .xlsx/.xls/.csv/.tsv/"
        ".pdf surfaced by `visit`/`act`/`sitemap_probe`.\n"
        "  - Level 6 (rescue): `rescue_extract` whenever normal levels are "
        "blocked/no-progress; use `rescue_wait` to track completion and "
        "`rescue_fetch` to pull a specific artifact.\n\n"
        "API-REPLAY PATTERN — your sharpest tool for JS-dropdown dashboards "
        "(PPAC, RBI, NSE, MoSPI). When a Year/Month/State selector is a custom "
        "JS widget (so `act`'s select/click time out) the table is really fed "
        "by an AJAX endpoint. Instead of fighting the widget:\n"
        "  1. `inspect_network(url, steps=[change the dropdown])` → see the "
        "endpoint + its params.\n"
        "  2. `call_api(endpoint, method, body=<params templated for the period "
        "you want>)` → get the JSON directly. This routinely reaches periods "
        "the dropdown omits (e.g. an older fiscal year).\n"
        "  3. If a playbook carries an `api` recipe, skip step 1 — call_api the "
        "endpoint straight away. `act` also auto-captures: when a UI step fails "
        "it returns `observed_api` + a `recovery_hint` naming the endpoint to "
        "replay.\n\n"
        "INDIAN FISCAL YEAR: a table labelled '2025-2026' / 'FY26' spans "
        "April 2025 → March 2026 — the April…December columns are the FIRST "
        "year and Jan-March are the SECOND. Never read 'April' as the "
        "current calendar year by default.\n\n"
        "FILE-AS-DATA WORKFLOW. Gov sites in India often publish the actual "
        "numbers ONLY as Excel / PDF attachments (GST at "
        "gst.gov.in/download/gststatistics, CGA monthly accounts, MoSPI "
        "Excel press kits, RBI circulars). For these:\n"
        "  1. `visit` (or `sitemap_probe`) the index to surface `file_links` / "
        "data_like_urls.\n"
        "  2. Pick the entry whose anchor text matches your objective — the "
        "right PERIOD (month/quarter/FY), the right SCOPE (state/union/scheme), "
        "and the right FORMAT (xlsx when you need cells, pdf when it's a "
        "report). Don't download all of them; choose the one that answers the "
        "task.\n"
        "  3. `download_file` on its href. For a BIG file where you need a "
        "specific figure, pass `query` (e.g. 'fiscal deficit April 2024') — it "
        "greps the PDF/sheet and returns ONLY matching pages/rows + snippets, "
        "not the whole 200-page document. Economical and faster.\n"
        "  4. Read the matches (or `sheets[].sample` / PDF `content` for a full "
        "parse) for the answer; widen the query or take a full parse if a match "
        "is ambiguous.\n"
        "Do not bounce the user to another MCP for file parsing — that is "
        "now this MCP's job too.\n\n"
        "PLAYBOOKS: a tool result may include a `playbook` field — a verified "
        "recipe for that exact site: what to AVOID, the open-data source to "
        "use instead, the known-good `act` steps, or an `api` endpoint to "
        "replay with call_api. When present, FOLLOW IT "
        "before any exploration — it exists because the site was solved once "
        "already. Also watch for `blocked` (CDN bot-wall) and `auth_wall` "
        "(login/registration gate) flags: both mean STOP driving the page and "
        "pivot to the playbook's open-data source.\n\n"
        "PROXY: every browser tool + download_file takes `use_proxy`. Leave it "
        "unset (None) to auto-honour the matched playbook's `proxy` hint; set "
        "true to force the residential proxy (BROWSER_PROXY_* env) on a "
        "`blocked` retry — a datacenter egress IP is the usual cause of an "
        "enterprise-CDN wall.\n\n"
        "ESCALATION RULE (STANDARD): if Levels 1-5 (`visit`/`act`/"
        "`inspect_network`/`call_api`/`smart_fetch`/`download_file`) still "
        "cannot produce the requested data (e.g. repeated `blocked`, "
        "`auth_wall`, widget dead-end, stale/no data), escalate to "
        "`rescue_extract` before concluding failure. If the rescue job is "
        "already known, call `rescue_fetch` directly; for in-flight jobs, use "
        "`rescue_wait`.\n\n"
        "AKAMAI RULE: always try normal rungs first (visit/act/inspect_network/"
        "call_api/download_file). If the page is blocked by Akamai (e.g. "
        "'Access Denied', challenge page, or repeated `blocked` on this domain), "
        "switch to rescue mode (`rescue_extract`) instead of retrying normal "
        "browser actions.\n\n"
        "FILTER FAILURE RULE: when required filters/selectors/widgets (date, "
        "ministry, language, state, year/month/day) are not accessible, not "
        "stable, or do not change results as expected after a reasonable try, "
        "STOP iterating URL/query/filter permutations and jump to rescue mode "
        "(`rescue_extract`). Do not brute-force all possible combinations.\n\n"
        "RESCUE IS INDEPENDENT: `rescue_extract` is not tied to `smart_fetch` — "
        "you may call it directly from any flow (`visit`, `act`, "
        "`inspect_network`, `call_api`) when normal rungs are not producing the "
        "target data.\n\n"
        "ANTI-THRASH GUARDRAILS:\n"
        "  - If 1-2 filter attempts fail (selector timeout, no-op submit, same "
        "listing/content despite changed filters), escalate to `rescue_extract`.\n"
        "  - If a result shows challenge/block symptoms (`blocked`, "
        "`fallback_reason=challenge_title`, 'Access Denied', recurring 404/500 "
        "error-page text), escalate to rescue; do NOT continue URL guessing.\n"
        "  - If `inspect_network` returns no useful API evidence "
        "(request_count=0 or no relevant requests) after a targeted attempt, "
        "escalate to rescue rather than trying many alternate URL patterns.\n"
        "  - If consecutive calls return low-signal pages (navigation shell, "
        "same title/text pattern, wrong-language generic listing), treat as "
        "no-progress and escalate to rescue.\n\n"
        + strategy_module.STRATEGY_INSTRUCTIONS
    ),
)


@mcp.tool()
async def today() -> dict[str, Any]:
    """Return the SERVER'S CURRENT DATE (IST, Asia/Kolkata). Call this
    FIRST whenever the user mentions a temporal phrase like "latest",
    "current", "today", "yesterday", "this quarter", "this year" — your
    training-data cutoff is NOT a reliable anchor. Use the returned
    `iso_date` and `financial_year_in` to construct concrete queries
    you pass to the other tools.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    ist = _tz(_td(hours=5, minutes=30))
    now = _dt.now(ist)
    current_fy_start = now.year if now.month >= 4 else now.year - 1
    current_fy_end = current_fy_start + 1
    fy_months_elapsed = now.month - 3 if now.month >= 4 else now.month + 9
    fy_quarter_in = (fy_months_elapsed - 1) // 3 + 1
    last_completed_fy_start = current_fy_start - 1
    _IN_MONTHS = ["April", "May", "June", "July", "August", "September",
                   "October", "November", "December", "January", "February", "March"]
    completed_fy_months = _IN_MONTHS[:max(0, fy_months_elapsed - 1)]
    current_fy_month_partial = (
        _IN_MONTHS[fy_months_elapsed - 1] if fy_months_elapsed >= 1 else None
    )
    completed_fy_quarters: list[dict[str, Any]] = []
    for q in range(1, fy_quarter_in):
        qs = _IN_MONTHS[(q - 1) * 3:(q - 1) * 3 + 3]
        completed_fy_quarters.append({"label": f"Q{q}", "months": qs})

    def _fy_label(start: int) -> str:
        return f"FY{str(start + 1)[-2:]}"

    last_3 = [_fy_label(last_completed_fy_start - 2),
               _fy_label(last_completed_fy_start - 1),
               _fy_label(last_completed_fy_start)]
    last_3_ranges = [
        f"{last_completed_fy_start - 2}-04-01 → {last_completed_fy_start - 1}-03-31",
        f"{last_completed_fy_start - 1}-04-01 → {last_completed_fy_start}-03-31",
        f"{last_completed_fy_start}-04-01 → {last_completed_fy_start + 1}-03-31",
    ]
    return {
        "iso_date": now.strftime("%Y-%m-%d"),
        "iso_datetime": now.isoformat(),
        "year": now.year,
        "month": now.strftime("%B"),
        "month_num": now.month,
        "day": now.day,
        "weekday": now.strftime("%A"),
        "calendar_quarter": f"Q{(now.month - 1) // 3 + 1}",
        "timezone": "Asia/Kolkata (IST, UTC+05:30)",
        "financial_year_in": _fy_label(current_fy_start),
        "fy_label": f"{current_fy_start}-{current_fy_end}",
        "fy_quarter_in": f"Q{fy_quarter_in}",
        "fy_month_in": fy_months_elapsed,
        "fy_status": "in-progress",
        "last_completed_fy_in": _fy_label(last_completed_fy_start),
        "last_3_completed_fys_in": last_3,
        "last_3_completed_fy_ranges": last_3_ranges,
        "current_fy_completed_months": completed_fy_months,
        "current_fy_completed_quarters": completed_fy_quarters,
        "current_fy_partial_month": current_fy_month_partial,
        "note": (
            "Indian FY runs April→March. For 'last N years' queries: "
            "cover the last N COMPLETED FYs (last_3_completed_fys_in) AND "
            "the in-progress current FY up to today — completed months "
            "(current_fy_completed_months), completed quarters "
            "(current_fy_completed_quarters), and partial-month / daily / "
            "weekly data right up to iso_date. Data cadence varies — "
            "daily petrol prices, weekly money-supply, monthly CPI / IIP, "
            "quarterly GDP — always look for the freshest data the source "
            "publishes; do NOT cap your query at the previous FY's "
            "March 31."
        ),
    }


@mcp.tool()
async def strategy() -> dict[str, Any]:
    """Return the browser-research DECISION PROCEDURE — the escalation ladder
    (static fetch → visit → act → inspect_network/call_api → download_file →
    pivot), the signal→action table (what `blocked`, `auth_wall`, `file_links`,
    `observed_api`, a timed-out select, etc. each mean and what to do), and the
    core principles (look before you assert, probe before you build, prefer API
    over DOM over OCR, escalate on the signal instead of thrashing, verify, then
    cache the win as a playbook).

    Call this when you're unsure how to approach a page, when a tool result's
    `next_step` advisor points here, or to ground a multi-step plan. It's the
    same method that turns a brittle JS dropdown into a one-shot API call.
    """
    return strategy_module.RESEARCH_STRATEGY


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
    use_proxy: bool | None = None,
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
    pb = await _match_playbook(url)
    result = await tools.visit(
        url,
        wait_for_selector=wait_for_selector,
        wait_extra_ms=wait_extra_ms,
        timeout_ms=timeout_ms,
        screenshot=screenshot,
        full_page_screenshot=full_page_screenshot,
        text_cap=text_cap,
        return_screenshot_b64=return_screenshot_b64,
        use_proxy=await _resolve_proxy(url, use_proxy) if pb is None else (
            use_proxy if use_proxy is not None else bool(pb.get("proxy"))
        ),
    )
    return await _attach_playbook(
        result, url, tool="visit", matched_playbook=pb)


@mcp.tool()
async def act(
    url: str,
    steps: list[dict[str, Any]],
    focus: str = "",
    timeout_ms: int = 60000,
    full_page_screenshot: bool = True,
    use_proxy: bool | None = None,
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
        {"fetch_json": {"url": "…", "method": "POST", "body": "a=b&c=d"}}
                                    // in-page fetch from the page's origin;
                                    // result lands in `fetch_results`

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

    ADAPTIVE: `act` records the page's XHR/fetch while it runs. The result
    carries `observed_api` (the data endpoints the page hit, with params), and
    if a UI step fails on a non-native widget it adds a `recovery_hint` naming
    the endpoint to replay with `call_api` — so a timed-out dropdown becomes a
    one-shot API call instead of a dead end.

    Returns the same shape as `extract` PLUS `step_results` (per-step
    timing + ok/error), `final_url`, `observed_api`, optional `recovery_hint`,
    and `fetch_results` (from any fetch_json steps).

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
    pb = await _match_playbook(url)
    result = await tools.act(
        url, steps,
        focus=focus,
        timeout_ms=timeout_ms,
        full_page_screenshot=full_page_screenshot,
        use_proxy=await _resolve_proxy(url, use_proxy) if pb is None else (
            use_proxy if use_proxy is not None else bool(pb.get("proxy"))
        ),
    )
    return await _attach_playbook(
        result, url, tool="act", matched_playbook=pb)


@mcp.tool()
async def extract(
    url: str,
    focus: str = "",
    wait_for_selector: str | None = None,
    full_page_screenshot: bool = True,
    use_proxy: bool | None = None,
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
    pb = await _match_playbook(url)
    result = await tools.extract(
        url,
        focus=focus,
        wait_for_selector=wait_for_selector,
        full_page_screenshot=full_page_screenshot,
        use_proxy=await _resolve_proxy(url, use_proxy) if pb is None else (
            use_proxy if use_proxy is not None else bool(pb.get("proxy"))
        ),
    )
    return await _attach_playbook(
        result, url, tool="extract", matched_playbook=pb)


@mcp.tool()
async def download_file(
    url: str,
    sheet: str | None = None,
    pages: list[int] | None = None,
    max_rows_per_sheet: int = 200,
    max_pdf_pages: int = 30,
    query: str | None = None,
    max_matches: int = 40,
    use_proxy: bool | None = None,
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
    pb = await _match_playbook(url)
    result = await tools.download_file(
        url,
        sheet=sheet,
        pages=pages,
        max_rows_per_sheet=max_rows_per_sheet,
        max_pdf_pages=max_pdf_pages,
        query=query,
        max_matches=max_matches,
        use_proxy=await _resolve_proxy(url, use_proxy) if pb is None else (
            use_proxy if use_proxy is not None else bool(pb.get("proxy"))
        ),
    )
    return await _attach_playbook(
        result, url, tool="download_file", matched_playbook=pb)


@mcp.tool()
async def sitemap_probe(
    url: str,
    max_urls: int = 200,
    max_sitemaps: int = 6,
    use_proxy: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read a site's robots.txt + sitemap(s) and surface its URL inventory —
    the CHEAPEST discovery step, run BEFORE driving a page.

    robots.txt and sitemap.xml frequently hand you the stable data URLs
    directly: a `data_like_urls` list of .json/.csv/.xlsx/.xml/.pdf and /api
    endpoints you can `download_file` or `call_api` straight away, skipping the
    browser entirely. Also returns the disallow list (where automation is
    unwelcome) and the broader URL inventory for locating the right page.

    Args:
        url: Any URL on the target site (only its origin is used).
        max_urls: Cap on the returned URL inventory.
        max_sitemaps: Cap on sitemap documents fetched (resolves one level of
            sitemap-index).

    Returns:
        {origin, robots_found, robots: {sitemaps[], disallow[]},
         sitemaps_fetched[], url_count, data_like_urls[], urls[], notes, hint?}
    """
    _bind(ctx)
    pb = await _match_playbook(url)
    result = await tools.sitemap_probe(
        url, max_urls=max_urls, max_sitemaps=max_sitemaps,
        use_proxy=await _resolve_proxy(url, use_proxy) if pb is None else (
            use_proxy if use_proxy is not None else bool(pb.get("proxy"))
        ),
    )
    return await _attach_playbook(
        result, url, tool="sitemap_probe", matched_playbook=pb)


@mcp.tool()
async def inspect_network(
    url: str,
    steps: list[dict[str, Any]] | None = None,
    settle_ms: int = 2500,
    url_filter: str | None = None,
    timeout_ms: int = 60000,
    use_proxy: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Open a page and report the XHR/fetch (AJAX) calls it fires — the
    DISCOVERY half of the API-replay pattern.

    Most JS dashboards (PPAC, RBI, NSE, MoSPI) render their tables/charts from
    an AJAX endpoint. When the on-page Year/Month/State selector is a custom JS
    widget, `act`'s select/click can't drive it — but the endpoint behind it is
    plain HTTP. Run this to learn that endpoint and its parameters, then pull
    the data with `call_api` (templating the params for any period you want,
    including ones the dropdown never lists).

    Pass `steps` (the same vocabulary as `act`) to capture the request a
    specific interaction fires — e.g. change the year dropdown and read the
    AJAX call it triggers.

    Args:
        url: Page to open.
        steps: Optional act-style steps to run while recording (e.g.
            [{"select": {"selector": "#year", "value": "2024-2025"}}]).
        settle_ms: Extra wait after load/steps so late XHRs are captured.
        url_filter: Only return requests whose URL contains this substring
            (e.g. "Ajax", "/api/").
        timeout_ms: Navigation/step timeout.

    Returns:
        {url, final_url, request_count, requests: [{method, url,
         resource_type, status, content_type, request_params,
         response_sample, ...}], step_results?}
    """
    _bind(ctx)
    pb = await _match_playbook(url)
    result = await tools.inspect_network(
        url, steps=steps, settle_ms=settle_ms, url_filter=url_filter,
        timeout_ms=timeout_ms,
        use_proxy=await _resolve_proxy(url, use_proxy) if pb is None else (
            use_proxy if use_proxy is not None else bool(pb.get("proxy"))
        ),
    )
    return await _attach_playbook(
        result, url, tool="inspect_network", matched_playbook=pb)


@mcp.tool()
async def call_api(
    url: str,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    page_url: str | None = None,
    content_type: str | None = None,
    use_proxy: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Replay an API/AJAX endpoint directly — the REPLAY half of the pattern.

    Loads a page on the endpoint's origin first (so cookies, CSRF state and
    Origin/Referer match), then issues the request via in-page fetch(). This
    bypasses brittle dropdown widgets entirely and reliably reaches data the
    front-end never surfaces — e.g. a fiscal year missing from a selector.

    Discover the endpoint + params with `inspect_network` first, or read them
    from a matched playbook's `api` recipe.

    Args:
        url: The endpoint URL (absolute).
        method: HTTP method. Default GET.
        body: Request body — a dict (form-encoded by default; sent as JSON if
            content_type is application/json) or a pre-encoded string. Example
            (PPAC gas): {"financialYear": "2023-2024", "reportBy": "4",
            "pageId": "138"}.
        headers: Extra request headers, merged over the XHR defaults
            (X-Requested-With + the right Content-Type).
        page_url: Origin page to load before fetching. Defaults to the
            endpoint's scheme://host/. Set to the real dashboard URL if the
            endpoint validates Referer.
        content_type: Override the request Content-Type.

    Returns:
        {url, page_url, status, ok, content_type, json|text,
         source: "browser_api"}.
    """
    _bind(ctx)
    pb = await _match_playbook(page_url or url)
    result = await tools.call_api(
        url, method=method, body=body, headers=headers, page_url=page_url,
        content_type=content_type,
        use_proxy=await _resolve_proxy(page_url or url, use_proxy) if pb is None else (
            use_proxy if use_proxy is not None else bool(pb.get("proxy"))
        ),
    )
    return await _attach_playbook(
        result, url, tool="call_api", matched_playbook=pb)


@mcp.tool()
async def smart_fetch(
    url: str,
    focus: str = "",
    allow_fallback: bool = True,
    fallback_extraction_prompt: str | None = None,
    fallback_api_base_url: str | None = None,
    fallback_wait_for_completion: bool = True,
    fallback_progress_wait_timeout_s: int = 900,
    fallback_progress_poll_interval_s: int = 10,
    fallback_gcs_wait_timeout_s: int = 60,
    fallback_max_files: int = 50,
    fallback_include_content: bool = False,
    fallback_max_content_bytes: int = 200000,
    use_proxy: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Playbook-aware fetch — the one-call "do the right thing" entry point.

    Consults the URL's playbook and ACTS on it instead of blindly rendering:
      - `api` recipe present → replays the endpoint via call_api (templating the
        params, e.g. the fiscal year, from `focus`), then structures the JSON.
      - `open_data` mirror present → download_file's it (the open CSV/PDF — the
        pivot for auth-walled or JS-dropdown sites).
      - otherwise → falls back to a full browser render (`extract`).

    Returns the `extract` structured shape (title/summary/key_facts/
    numeric_values/dates/tables_summary) PLUS `rung_used` (api|open_data|render)
    and `playbook_id`. Prefer this over `extract` whenever a site might have a
    playbook recipe — it's what the upstream web_fetch escalation calls.

    Args:
        url: The page/endpoint URL.
        focus: What you want (also supplies params like the year for `api`
            recipes, e.g. "natural gas consumption FY2023-24").
    """
    _bind(ctx)
    pb = await _match_playbook(url)
    if allow_fallback:
        if _playbook_prefers_rescue(pb):
            rescue = await tools.external_extract_fallback(
                url,
                fallback_extraction_prompt or focus or
                "Extract the requested facts and figures from this page.",
                api_base_url=fallback_api_base_url,
                wait_for_completion=fallback_wait_for_completion,
                progress_wait_timeout_s=fallback_progress_wait_timeout_s,
                progress_poll_interval_s=fallback_progress_poll_interval_s,
                gcs_wait_timeout_s=fallback_gcs_wait_timeout_s,
                max_files=fallback_max_files,
                include_content=fallback_include_content,
                max_content_bytes=fallback_max_content_bytes,
            )
            if isinstance(rescue, dict) and not rescue.get("error"):
                rescue["fallback_used"] = "rescue_extract"
                rescue["rescue_short_circuit"] = True
                return await _attach_playbook(
                    rescue, url, tool="rescue_extract", matched_playbook=pb)
    result = await tools.smart_fetch(
        url, focus=focus,
        use_proxy=await _resolve_proxy(url, use_proxy) if pb is None else (
            use_proxy if use_proxy is not None else bool(pb.get("proxy"))
        ),
    )
    if (
        allow_fallback
        and isinstance(result, dict)
        and (result.get("error") or result.get("blocked") or result.get("auth_wall"))
    ):
        rescue = await tools.external_extract_fallback(
            url,
            fallback_extraction_prompt or focus or
            "Extract the requested facts and figures from this page.",
            api_base_url=fallback_api_base_url,
            wait_for_completion=fallback_wait_for_completion,
            progress_wait_timeout_s=fallback_progress_wait_timeout_s,
            progress_poll_interval_s=fallback_progress_poll_interval_s,
            gcs_wait_timeout_s=fallback_gcs_wait_timeout_s,
            max_files=fallback_max_files,
            include_content=fallback_include_content,
            max_content_bytes=fallback_max_content_bytes,
        )
        if isinstance(rescue, dict) and not rescue.get("error"):
            rescue["fallback_used"] = "rescue_extract"
            return await _attach_playbook(
                rescue, url, tool="rescue_extract", matched_playbook=pb)
    return await _attach_playbook(
        result, url, tool="smart_fetch", matched_playbook=pb)


@mcp.tool()
async def rescue_extract(
    url: str,
    extraction_prompt: str,
    job_name: str | None = None,
    description: str = "",
    api_base_url: str | None = None,
    start_job: bool = True,
    bucket: str = "single-url-data",
    prefix_root: str = "data",
    wait_for_completion: bool = True,
    progress_wait_timeout_s: int = 900,
    progress_poll_interval_s: int = 10,
    gcs_wait_timeout_s: int = 60,
    max_files: int = 50,
    include_content: bool = False,
    max_content_bytes: int = 200000,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Final-rung rescue extraction when native strategies fail.

    Use this when browser/API/file-based rungs cannot yield the target data:
    - repeated `blocked` / CDN wall
    - `auth_wall` / login gate
    - brittle JS controls with no replayable endpoint
    - source page reachable but required period/data remains unavailable

    Executes Kryptos single-URL flow:
      POST /api/jobs
      POST /api/jobs/{jobId}/start
      GET /api/progress/{jobId} (poll every ~10s until terminal)
    then reads synced artifacts from:
      gs://<bucket>/<prefix_root>/<job_name>/

    Cron scheduling is intentionally ignored here; this tool is a one-shot
    operational rescue path.

    Returns metadata only: compact file names + a text tree for this job,
    plus `last_event_text` when Kryptos reports single-session summary text.
    To retrieve a specific artifact, call
    `rescue_fetch(job_name=..., file_path=...)`.
    """
    _bind(ctx)
    pb = await _match_playbook(url)
    result = await tools.external_extract_fallback(
        url,
        extraction_prompt,
        job_name=job_name,
        description=description,
        api_base_url=api_base_url,
        start_job=start_job,
        bucket=bucket,
        prefix_root=prefix_root,
        wait_for_completion=wait_for_completion,
        progress_wait_timeout_s=progress_wait_timeout_s,
        progress_poll_interval_s=progress_poll_interval_s,
        gcs_wait_timeout_s=gcs_wait_timeout_s,
        max_files=max_files,
        include_content=include_content,
        max_content_bytes=max_content_bytes,
    )
    return await _attach_playbook(
        result, url, tool="rescue_extract", matched_playbook=pb)


@mcp.tool()
async def rescue_fetch(
    job_name: str,
    file_path: str,
    bucket: str = "single-url-data",
    prefix_root: str = "data",
    local_dir: str | None = None,
    overwrite: bool = True,
    wait_for_file: bool = True,
    wait_timeout_s: int = 420,
    poll_interval_s: int = 15,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Fetch one file for an existing rescue extraction job from GCS.

    Expected object prefix:
      gs://<bucket>/<prefix_root>/<job_name>/

    Use this when:
    - you already know the target artifact path under a rescue job
    - you want that single artifact downloaded locally for follow-up tools
    """
    _bind(ctx)
    return await tools.fetch_kryptos_job_file(
        job_name,
        file_path,
        bucket=bucket,
        prefix_root=prefix_root,
        local_dir=local_dir,
        overwrite=overwrite,
        wait_for_file=wait_for_file,
        wait_timeout_s=wait_timeout_s,
        poll_interval_s=poll_interval_s,
    )


@mcp.tool()
async def rescue_wait(
    job_id: str,
    job_name: str | None = None,
    api_base_url: str | None = None,
    poll_interval_s: int = 10,
    wait_timeout_s: int = 900,
    bucket: str = "single-url-data",
    prefix_root: str = "data",
    gcs_wait_timeout_s: int = 60,
    max_files: int = 50,
    include_content: bool = True,
    max_content_bytes: int = 200000,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Wait for a rescue extraction job to complete, then fetch synced files.

    Polls Kryptos:
      GET /api/progress/{jobId}
    every `poll_interval_s` (default 10s) until status is terminal
    (completed/failed/cancelled), then polls GCS up to `gcs_wait_timeout_s`
    (default 60s) to absorb post-completion sync lag.

    Default is metadata-only. Includes `last_event_text` when present.
    Use `rescue_fetch` for file contents.
    """
    _bind(ctx)
    return await tools.wait_kryptos_job_completion(
        job_id,
        job_name=job_name,
        api_base_url=api_base_url,
        poll_interval_s=poll_interval_s,
        wait_timeout_s=wait_timeout_s,
        bucket=bucket,
        prefix_root=prefix_root,
        gcs_wait_timeout_s=gcs_wait_timeout_s,
        max_files=max_files,
        include_content=include_content,
        max_content_bytes=max_content_bytes,
    )


# ============================================================================
# Admin API — read/edit playbooks WITHOUT a redeploy (the platform's admin-
# settings UI calls these). If ADMIN_TOKEN is set, requests must include
# X-Admin-Token. If ADMIN_TOKEN is unset, admin auth is disabled (fail-open).
# Routes exist only on HTTP transports and only if this FastMCP build supports
# custom_route; otherwise edit the GCS object directly (still hot-reloaded).
# Recommended: have the platform BACKEND proxy these server-to-server so the
# admin token never reaches the browser and no CORS is needed.
# ============================================================================

if hasattr(mcp, "custom_route"):
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse

    def _admin_ok(request: Request) -> bool:
        token = (os.environ.get("ADMIN_TOKEN") or "").strip()
        if not token:
            return True
        return request.headers.get("X-Admin-Token") == token

    async def _save_entries(entries: Any) -> tuple[bool, str]:
        ok, err = playbooks.validate_playbooks(entries)
        if not ok:
            return False, err
        try:
            await playbooks.save_playbooks(entries)
        except Exception as e:  # noqa: BLE001
            return False, f"save failed: {e}"
        return True, ""

    def _admin_ui_html() -> str:
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Browser Research Admin</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 16px; background: #fafafa; color: #222; }
    h1 { margin-top: 0; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
    .card { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px; }
    .tabs { display: flex; gap: 8px; margin: 8px 0 12px; }
    .tabs button { padding: 8px 12px; cursor: pointer; }
    .tab { display: none; }
    .tab.active { display: block; }
    input, textarea, select { width: 100%; padding: 8px; box-sizing: border-box; }
    textarea { min-height: 120px; font-family: Consolas, monospace; }
    pre { background: #111; color: #e5e5e5; padding: 10px; overflow: auto; white-space: pre-wrap; }
    .muted { color: #666; font-size: 12px; }
    .ok { color: #0a7a2f; }
    .err { color: #b00020; }
  </style>
</head>
<body>
  <h1>Browser Research Admin</h1>
  <div class="card">
    <div class="row">
      <div style="min-width: 320px; flex: 1;">
        <label>Admin token (sent as X-Admin-Token)</label>
        <input id="token" type="password" placeholder="ADMIN_TOKEN" />
      </div>
    </div>
    <div class="tabs">
      <button onclick="showTab('agent')">Agent Test</button>
      <button onclick="showTab('playbooks')">Playbook Strategies</button>
    </div>
  </div>

  <div id="tab-agent" class="tab active card">
    <h3>Agent Test (script runner)</h3>
    <div class="row">
      <div style="flex: 2; min-width: 360px;">
        <label>Query</label>
        <textarea id="agent-query">Extract the pib releases from PIB website for PM office for 13th June 2025 and summarize each</textarea>
      </div>
      <div style="flex: 1; min-width: 220px;">
        <label>Model (optional)</label>
        <input id="agent-model" placeholder="claude-sonnet-4-6" />
        <label style="margin-top: 8px; display:block;">Max iterations</label>
        <input id="agent-max-iters" type="number" min="1" max="60" value="12" />
        <label style="margin-top: 8px; display:block;">Timeout seconds</label>
        <input id="agent-timeout" type="number" min="30" max="3600" value="900" />
      </div>
    </div>
    <div class="row">
      <button onclick="runAgentTest()">Run Agent Test</button>
      <span id="agent-status" class="muted"></span>
    </div>
    <pre id="agent-output">(no output yet)</pre>
  </div>

  <div id="tab-playbooks" class="tab card">
    <h3>Playbook Strategies</h3>
    <div class="row">
      <button onclick="loadPlaybooks()">Load Playbooks</button>
      <button onclick="saveAllPlaybooks()">Save All (bulk JSON)</button>
      <span id="pb-status" class="muted"></span>
    </div>
    <label>Bulk JSON editor (GET/PUT /admin/playbooks)</label>
    <textarea id="pb-json"></textarea>

    <hr />
    <h4>Upsert Strategy (quick edit)</h4>
    <div class="row">
      <div style="flex: 1; min-width: 240px;">
        <label>Playbook id (optional)</label>
        <input id="pb-id" placeholder="pib-allrel" />
      </div>
      <div style="flex: 2; min-width: 280px;">
        <label>URL (optional, used to match/create by domain)</label>
        <input id="pb-url" placeholder="https://pib.gov.in/allRel.aspx" />
      </div>
      <div style="min-width: 160px;">
        <label><input id="pb-prefer-rescue" type="checkbox" /> prefer_rescue</label>
      </div>
    </div>
    <label>Strategy text</label>
    <textarea id="pb-strategy"></textarea>
    <div class="row">
      <button onclick="upsertStrategy()">Upsert Strategy</button>
    </div>

    <hr />
    <h4>Add/Delete Entries</h4>
    <label>Add entry JSON (single playbook object)</label>
    <textarea id="pb-add-json" placeholder='{"id":"my-playbook","match":{"domain":"example.com"},"strategy":"..."}'></textarea>
    <div class="row">
      <button onclick="addEntry()">Add Entry</button>
      <input id="pb-delete-id" placeholder="playbook id to delete" style="max-width: 260px;" />
      <button onclick="deleteEntry()">Delete Entry By ID</button>
    </div>
  </div>

  <script>
    function tokenHeader() {
      const token = document.getElementById("token").value.trim();
      return token ? { "X-Admin-Token": token } : {};
    }
    function showTab(name) {
      document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
      document.getElementById("tab-" + name).classList.add("active");
    }
    async function api(url, options = {}) {
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        tokenHeader(),
        options.headers || {}
      );
      const res = await fetch(url, Object.assign({}, options, { headers }));
      const text = await res.text();
      let body = {};
      try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }
      if (!res.ok) throw new Error((body && (body.error || body.raw)) || ("HTTP " + res.status));
      return body;
    }
    function setStatus(id, msg, ok) {
      const el = document.getElementById(id);
      el.textContent = msg;
      el.className = ok ? "ok" : "err";
    }
    async function runAgentTest() {
      const out = document.getElementById("agent-output");
      out.textContent = "Running...";
      setStatus("agent-status", "", true);
      try {
        const body = {
          query: document.getElementById("agent-query").value,
          model: document.getElementById("agent-model").value || null,
          max_iters: Number(document.getElementById("agent-max-iters").value || 12),
          timeout_s: Number(document.getElementById("agent-timeout").value || 900)
        };
        const res = await api("/admin/agent-test", { method: "POST", body: JSON.stringify(body) });
        out.textContent = res.stdout || "(no stdout)";
        if (res.stderr) out.textContent += "\\n\\n[stderr]\\n" + res.stderr;
        setStatus("agent-status", "completed (exit " + String(res.exit_code) + ")", res.ok === true);
      } catch (e) {
        out.textContent = String(e);
        setStatus("agent-status", String(e), false);
      }
    }
    async function loadPlaybooks() {
      try {
        const res = await api("/admin/playbooks");
        document.getElementById("pb-json").value = JSON.stringify(res.playbooks || [], null, 2);
        setStatus("pb-status", "loaded " + String((res.playbooks || []).length) + " entries", true);
      } catch (e) {
        setStatus("pb-status", String(e), false);
      }
    }
    async function saveAllPlaybooks() {
      try {
        const parsed = JSON.parse(document.getElementById("pb-json").value || "[]");
        const res = await api("/admin/playbooks", { method: "PUT", body: JSON.stringify({ playbooks: parsed }) });
        setStatus("pb-status", "saved " + String(res.count || 0) + " entries", true);
      } catch (e) {
        setStatus("pb-status", String(e), false);
      }
    }
    async function upsertStrategy() {
      try {
        const body = {
          id: document.getElementById("pb-id").value || null,
          url: document.getElementById("pb-url").value || null,
          strategy: document.getElementById("pb-strategy").value,
          prefer_rescue: document.getElementById("pb-prefer-rescue").checked
        };
        const res = await api("/admin/playbooks/strategy", { method: "POST", body: JSON.stringify(body) });
        setStatus("pb-status", "strategy saved for " + String((res.entry || {}).id || ""), true);
        await loadPlaybooks();
      } catch (e) {
        setStatus("pb-status", String(e), false);
      }
    }
    async function addEntry() {
      try {
        const entry = JSON.parse(document.getElementById("pb-add-json").value);
        const res = await api("/admin/playbooks/entry", { method: "POST", body: JSON.stringify(entry) });
        setStatus("pb-status", "entry added, count " + String(res.count || 0), true);
        await loadPlaybooks();
      } catch (e) {
        setStatus("pb-status", String(e), false);
      }
    }
    async function deleteEntry() {
      try {
        const id = document.getElementById("pb-delete-id").value.trim();
        if (!id) throw new Error("delete id required");
        const res = await api("/admin/playbooks/entry/" + encodeURIComponent(id), { method: "DELETE" });
        setStatus("pb-status", "entry deleted, count " + String(res.count || 0), true);
        await loadPlaybooks();
      } catch (e) {
        setStatus("pb-status", String(e), false);
      }
    }
  </script>
</body>
</html>"""

    @mcp.custom_route("/admin/ui", methods=["GET"])
    async def _admin_ui(_request: Request):
        return HTMLResponse(_admin_ui_html())

    @mcp.custom_route("/admin/agent-test", methods=["POST"])
    async def _admin_agent_test(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        query = str((body or {}).get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "query is required"}, status_code=422)
        model = str((body or {}).get("model") or "").strip()
        max_iters = int((body or {}).get("max_iters") or 8)
        timeout_s = int((body or {}).get("timeout_s") or 900)
        max_iters = max(1, min(60, max_iters))
        timeout_s = max(30, min(3600, timeout_s))

        root = Path(__file__).resolve().parents[2]
        script = root / "scripts" / "test_agent_sonnet.py"
        if not script.exists():
            return JSONResponse(
                {"error": f"script not found: {script}"},
                status_code=500,
            )
        cmd = [
            sys.executable,
            str(script),
            "--max-iters",
            str(max_iters),
            "--query",
            query,
            "--print-assistant-text",
        ]
        if model:
            cmd.extend(["--model", model])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()  # type: ignore[name-defined]
            except Exception:
                pass
            return JSONResponse({"error": "agent test timed out"}, status_code=504)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"agent test failed: {e}"}, status_code=500)
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return JSONResponse({
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": cmd,
        })

    @mcp.custom_route("/admin/playbooks", methods=["GET"])
    async def _admin_get_playbooks(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({
            "playbooks": await playbooks.get_playbooks(force=True),
            "source": playbooks.current_source(),
        })

    @mcp.custom_route("/admin/playbooks", methods=["PUT"])
    async def _admin_put_playbooks(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        entries = body.get("playbooks") if isinstance(body, dict) else body
        ok, err = await _save_entries(entries)
        if not ok:
            return JSONResponse({"error": err}, status_code=422)
        return JSONResponse({"ok": True, "count": len(entries)})

    @mcp.custom_route("/admin/playbooks/entry", methods=["POST"])
    async def _admin_add_playbook_entry(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            entry = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(entry, dict):
            return JSONResponse({"error": "entry must be an object"}, status_code=422)
        entries = await playbooks.get_playbooks(force=True)
        out = [dict(e) if isinstance(e, dict) else e for e in entries]
        if entry.get("id"):
            for idx, e in enumerate(out):
                if isinstance(e, dict) and e.get("id") == entry.get("id"):
                    return JSONResponse({"error": "id already exists"}, status_code=409)
        out.append(entry)
        ok, err = await _save_entries(out)
        if not ok:
            return JSONResponse({"error": err}, status_code=422)
        return JSONResponse({"ok": True, "count": len(out)})

    @mcp.custom_route("/admin/playbooks/entry/{playbook_id}", methods=["DELETE"])
    async def _admin_delete_playbook_entry(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        pid = str(request.path_params.get("playbook_id") or "").strip()
        if not pid:
            return JSONResponse({"error": "playbook_id is required"}, status_code=422)
        entries = await playbooks.get_playbooks(force=True)
        out = [dict(e) if isinstance(e, dict) else e for e in entries]
        kept = [e for e in out if not (isinstance(e, dict) and e.get("id") == pid)]
        if len(kept) == len(out):
            return JSONResponse({"error": "playbook id not found"}, status_code=404)
        ok, err = await _save_entries(kept)
        if not ok:
            return JSONResponse({"error": err}, status_code=422)
        return JSONResponse({"ok": True, "count": len(kept)})

    @mcp.custom_route("/admin/playbooks/strategy", methods=["POST"])
    async def _admin_upsert_strategy(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=422)
        strategy = str(body.get("strategy") or "").strip()
        if not strategy:
            return JSONResponse({"error": "strategy is required"}, status_code=422)
        prefer_rescue = body.get("prefer_rescue")
        if prefer_rescue is not None and not isinstance(prefer_rescue, bool):
            return JSONResponse({"error": "prefer_rescue must be boolean"}, status_code=422)
        pid = str(body.get("id") or "").strip()
        url = str(body.get("url") or "").strip()

        entries = await playbooks.get_playbooks(force=True)
        out = [dict(e) if isinstance(e, dict) else e for e in entries]
        target: dict[str, Any] | None = None
        if pid:
            for e in out:
                if isinstance(e, dict) and e.get("id") == pid:
                    target = e
                    break
        if target is None and url:
            target = playbooks.match_playbook(out, url)
        if target is None:
            dom = _domain(url)
            if not dom:
                return JSONResponse(
                    {"error": "provide either existing id or a URL with domain"},
                    status_code=422,
                )
            new_id = pid or f"auto-{dom}"
            target = {"id": new_id, "match": {"domain": dom}}
            out.append(target)
        target["strategy"] = strategy
        target["last_verified"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if prefer_rescue is not None:
            target["prefer_rescue"] = prefer_rescue
        ok, err = await _save_entries(out)
        if not ok:
            return JSONResponse({"error": err}, status_code=422)
        return JSONResponse({"ok": True, "entry": target, "count": len(out)})

    @mcp.custom_route("/admin/playbooks/validate", methods=["POST"])
    async def _admin_validate(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        entries = body.get("playbooks") if isinstance(body, dict) else body
        ok, err = playbooks.validate_playbooks(entries)
        return JSONResponse({"ok": ok, "error": err})

    @mcp.custom_route("/admin/playbooks/match", methods=["GET"])
    async def _admin_match(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        url = request.query_params.get("url", "")
        return JSONResponse({"url": url,
                             "match": await playbooks.match_for_url(url)})

    @mcp.custom_route("/admin/playbooks/reload", methods=["POST"])
    async def _admin_reload(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        data = await playbooks.get_playbooks(force=True)
        return JSONResponse({"ok": True, "count": len(data),
                             "source": playbooks.current_source()})
else:  # pragma: no cover - depends on installed mcp SDK version
    log.warning("FastMCP build lacks custom_route — playbook admin API "
                "disabled; edit the GCS object directly (still hot-reloaded).")
