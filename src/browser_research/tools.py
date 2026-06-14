"""Tool implementations — pure async, mirror the authority-web-search-mcp
patterns (shared client singletons, cachetools TTL, JSON timing logs).

Browser engine is patchright (a drop-in patched Playwright). One Chromium
per process; one BrowserContext per MCP client/session for cookie + auth
isolation. Pages are short-lived — open, read, close — so a single Cloud
Run instance can soak many concurrent visits.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from cachetools import TTLCache

# Use patchright — drop-in async Playwright with anti-detection patches.
# All names match upstream Playwright so the swap is one import line.
from patchright.async_api import async_playwright, Browser, BrowserContext, Page

from .extraction import STRUCTURED_EXTRACT_SYSTEM_STATIC, dynamic_date_block

log = logging.getLogger("browser_research")


# ============================================================================
# Module-level singletons. The browser is shared across the process; contexts
# are per-client so cookies / storage / auth state don't leak between tenants.
# ============================================================================

_pw_instance: Any | None = None
_browser: Browser | None = None
_contexts: dict[str, BrowserContext] = {}
_browser_lock = asyncio.Lock()

_anthropic_client: Any | None = None
_anthropic_lock = asyncio.Lock()


_current_client: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_br_current_client", default="anon",
)


def set_current_client(client_id: str | None) -> None:
    _current_client.set(client_id or "anon")


def _anthropic_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _anthropic_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


async def _anthropic():
    if not _anthropic_key():
        return None
    global _anthropic_client
    if _anthropic_client is None:
        async with _anthropic_lock:
            if _anthropic_client is None:
                from anthropic import AsyncAnthropic
                _anthropic_client = AsyncAnthropic(api_key=_anthropic_key())
    return _anthropic_client


async def _get_browser() -> Browser:
    global _pw_instance, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        if _pw_instance is None:
            _pw_instance = await async_playwright().start()
        # Container-friendly Chromium flags. --no-sandbox is required when
        # running as root inside Docker; patchright keeps the stealth
        # patches active regardless.
        _browser = await _pw_instance.chromium.launch(
            headless=os.environ.get("HEADLESS", "true").lower() != "false",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        log.info("Chromium launched (patchright stealth)")
        return _browser


async def _get_context(client_id: str) -> BrowserContext:
    if client_id in _contexts:
        ctx = _contexts[client_id]
        try:
            # Cheap liveness probe — accessing .pages on a closed context
            # raises, which is the signal to recreate it.
            _ = len(ctx.pages)
            return ctx
        except Exception:
            _contexts.pop(client_id, None)
    browser = await _get_browser()
    # India-default geo / language so JS that branches on locale (PPAC, RBI
    # dashboards) renders the Indian build.
    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1440, "height": 900},
        accept_downloads=False,
    )
    _contexts[client_id] = ctx
    return ctx


async def shutdown() -> None:
    """Graceful cleanup. Cloud Run signals SIGTERM ~10s before kill;
    server.py's lifespan hooks call this."""
    global _browser, _pw_instance, _contexts
    for ctx in list(_contexts.values()):
        try:
            await ctx.close()
        except Exception:
            pass
    _contexts.clear()
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw_instance is not None:
        try:
            await _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None


# ============================================================================
# Structured timing log — same JSON schema as authority-web-search.
# ============================================================================

def _emit(tool: str, started: float, *, cache_hit: bool = False,
           extra: dict | None = None) -> None:
    payload = {
        "evt": "tool",
        "tool": tool,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "cache_hit": cache_hit,
    }
    if extra:
        payload.update(extra)
    log.info(json.dumps(payload))


# ============================================================================
# In-process TTL cache for visit() results so repeat reads inside an agent
# loop don't pay the full page-load cost again.
# ============================================================================

_visit_cache: TTLCache = TTLCache(maxsize=128, ttl=180)  # 3 min


def _stable_key(*parts: Any) -> tuple:
    out: list[Any] = []
    for p in parts:
        if isinstance(p, (list, tuple)):
            out.append(tuple(sorted(str(x) for x in p)))
        elif isinstance(p, dict):
            out.append(tuple(sorted((str(k), str(v)) for k, v in p.items())))
        elif p is None:
            out.append(None)
        else:
            out.append(str(p))
    return tuple(out)


def _parse_relaxed_json(body: str) -> dict[str, Any]:
    """Pull a JSON object out of an LLM response, tolerating truncated input.

    Sonnet sometimes hits max_tokens mid-list. We grab the longest balanced
    `{...}` substring and try to parse it; if that fails, we walk backward
    closing dangling brackets until it parses. Returns {} on total failure.
    """
    s = body.strip()
    if not s:
        return {}
    # Find the first `{`
    start = s.find("{")
    if start < 0:
        return {}
    s = s[start:]
    # Try the whole thing first.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Walk backward from the end, trying every truncation point and patching
    # up unclosed braces / brackets / strings until something parses.
    for end in range(len(s), 0, -1):
        candidate = s[:end]
        # Close any open string literal first.
        if candidate.count('"') % 2 == 1:
            candidate += '"'
        # Close any open arrays / objects.
        opens = candidate.count("[") - candidate.count("]")
        if opens > 0:
            candidate += "]" * opens
        opens = candidate.count("{") - candidate.count("}")
        if opens > 0:
            candidate += "}" * opens
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return {}


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


# ============================================================================
# visit — open a URL, return DOM text + screenshot. The atomic primitive
# everything else builds on.
# ============================================================================

async def visit(
    url: str,
    *,
    wait_for_selector: str | None = None,
    wait_extra_ms: int = 1500,
    timeout_ms: int = 45_000,
    screenshot: bool = True,
    full_page_screenshot: bool = False,
    text_cap: int = 30_000,
    return_screenshot_b64: bool = False,
) -> dict[str, Any]:
    """Navigate to URL with a real Chromium and return its rendered state.

    Use when pdf_fetch / http_post_form / web_fetch all fail — the page is
    a SPA whose data lives in JavaScript, or behind a login, or hidden behind
    a dropdown that's not a separate URL.

    Args:
        url: The page URL.
        wait_for_selector: Optional CSS selector to await before reading the
            DOM. Useful when the data appears only after an AJAX call returns
            (e.g. wait for ".chart svg" on a chart page).
        wait_extra_ms: Extra settle time after the wait condition fires.
        timeout_ms: Hard timeout for the whole navigation.
        screenshot: Whether to capture a PNG screenshot internally. Even
            when True, the base64 is NOT returned in the response by default
            (see return_screenshot_b64) — extract() and act() use the
            captured screenshot in-process to feed Sonnet vision.
        full_page_screenshot: If True, scroll-stitches the whole page.
        text_cap: Cap on extracted text length (innerText).
        return_screenshot_b64: If True, echo the base64 PNG back in the
            response. Defaults to False because a typical screenshot is
            ~700KB-1MB and accumulating them across an agent's tool-call
            history blows the 1M-token context window. Tools or UIs that
            actually need the bytes (e.g. a browser-canvas pane) can opt in.

    Returns:
        {url, title, domain, text, screenshot_bytes?, screenshot_b64?,
         fetched_at, current_date}
    """
    t0 = time.perf_counter()
    if not url:
        return {"error": "url is required"}

    cache_key = _stable_key(
        "visit", url, wait_for_selector, full_page_screenshot, text_cap,
    )
    if cached := _visit_cache.get(cache_key):
        _emit("visit", t0, cache_hit=True,
               extra={"chars": len(cached.get("text") or "")})
        # Strip the base64 from cache hits too unless the caller wants it.
        # The cache keeps it because extract()/act() pull from the same
        # cache via _sonnet_extract.
        if return_screenshot_b64:
            return cached
        return {k: v for k, v in cached.items() if k != "screenshot_b64"}

    client_id = _current_client.get() or "anon"
    ctx = await _get_context(client_id)
    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded",
                             timeout=timeout_ms)
        except Exception as e:  # noqa: BLE001
            _emit("visit", t0, extra={"error": f"goto: {str(e)[:80]}"})
            return {"error": f"navigation failed: {e}", "url": url}

        if wait_for_selector:
            try:
                await page.wait_for_selector(wait_for_selector,
                                              timeout=min(timeout_ms, 15_000))
            except Exception:
                # Not fatal — selector might just be slow; we proceed with
                # whatever's already on the page.
                pass
        else:
            # Best-effort: wait for network to idle, but don't block the
            # whole call if a single tracker pixel hangs.
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass

        if wait_extra_ms > 0:
            await page.wait_for_timeout(wait_extra_ms)

        title = (await page.title()) or ""
        url_final = page.url
        try:
            text = await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            text = ""

        # Scan the rendered DOM for download-shaped links. We CANNOT parse
        # these — Chromium just returns bytes and our toolkit has no Excel /
        # CSV reader on the browser-research side. But surfacing the URLs
        # lets the agent recommend the right tool downstream (e.g. the
        # excel_fetch_structured tool on authority-web-search-mcp) or hand
        # the link back to the user. Far better than the agent spinning
        # through visit() calls on a page whose data is all in attachments.
        file_links: list[dict[str, str]] = []
        try:
            file_links = await page.evaluate(r"""
                () => {
                  const exts = ['.xlsx','.xlsm','.xls','.csv','.tsv','.pdf','.zip','.7z','.docx','.pptx'];
                  const seen = new Set();
                  const out = [];
                  for (const a of document.querySelectorAll('a[href]')) {
                    const href = (a.href || '').trim();
                    if (!href || seen.has(href)) continue;
                    const path = (new URL(href, document.baseURI)).pathname.toLowerCase();
                    const ext = exts.find(e => path.endsWith(e));
                    if (!ext) continue;
                    seen.add(href);
                    const text = (a.innerText || a.textContent || '').replace(/\s+/g,' ').trim();
                    out.push({href, text: text.slice(0, 200), format: ext.slice(1)});
                    if (out.length >= 40) break;
                  }
                  return out;
                }
            """)
        except Exception as e:  # noqa: BLE001
            log.debug("file_links scan failed: %s", e)

        out: dict[str, Any] = {
            "url": url_final,
            "title": title[:300],
            "domain": _domain(url_final),
            "text": (text or "")[:text_cap],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "current_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        if file_links:
            out["file_links"] = file_links
            # Per-format counts so the agent can scan at a glance.
            from collections import Counter
            out["file_links_summary"] = dict(
                Counter(fl["format"] for fl in file_links)
            )

        # Capture the screenshot bytes internally regardless — extract() and
        # act() depend on them for Sonnet vision. We only put the base64 into
        # the *returned* dict if the caller asked for it; the cache keeps the
        # full payload so an extract() call right after visit() doesn't pay
        # twice. The agent-facing tool response gets `screenshot_bytes` so it
        # knows a screenshot was taken without carrying the 1MB blob.
        shot_b64: str | None = None
        if screenshot:
            try:
                png = await page.screenshot(
                    type="png",
                    full_page=full_page_screenshot,
                )
                shot_b64 = base64.b64encode(png).decode("ascii")
                out["screenshot_bytes"] = len(png)
            except Exception as e:  # noqa: BLE001
                log.warning("screenshot failed: %s", e)

        if out["text"]:
            # Cache the full payload (including base64) so chained extract()/
            # act() calls can use it. The base64 is stripped from the returned
            # dict below.
            cache_entry = dict(out)
            if shot_b64:
                cache_entry["screenshot_b64"] = shot_b64
            _visit_cache[cache_key] = cache_entry

        if return_screenshot_b64 and shot_b64:
            out["screenshot_b64"] = shot_b64

        _emit("visit", t0,
               extra={"chars": len(out["text"]),
                      "shot_kb": round(out.get("screenshot_bytes", 0) / 1024)})
        return out
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ============================================================================
# extract — visit + Sonnet structured extraction. Same response shape as
# authority-web-search-mcp's pdf_fetch_structured so the agent treats it
# identically.
# ============================================================================

async def act(
    url: str,
    steps: list[dict[str, Any]],
    *,
    focus: str = "",
    timeout_ms: int = 60_000,
    full_page_screenshot: bool = True,
    include_screenshot_in_response: bool = False,
) -> dict[str, Any]:
    """Drive a real Chromium through a sequence of steps on a page, then run
    structured extraction on the final state.

    Use this when the data lives behind an interaction — a Year/Month
    dropdown that fires AJAX inline, a tab click that reveals a table, a
    "Load more" button, a sort header, a form submit. Anything that
    `visit` can't reach because the URL doesn't change.

    Steps are dicts; each one MUST have a single key naming the action:

      {"goto":   "https://…"}                       navigate
      {"click":  "selector"}                         left-click an element
      {"fill":   {"selector": "#q", "value": "x"}}   type into an input
      {"select": {"selector": "#year", "value": "2024-2025"}}
                                                     pick a <select> option
      {"press":  {"selector": "#q", "key": "Enter"}} press a key
      {"scroll": {"to": "bottom"|"top"|<int px>}}    scroll the page
      {"wait_for_selector": "selector"}              wait for it to exist
      {"wait_for_load_state": "networkidle"|"load"}  wait for nav state
      {"wait_ms": 1500}                              hard sleep
      {"screenshot": {"name": "after-select"}}       capture an intermediate
                                                     screenshot (returned in
                                                     `step_results`)

    The first non-`goto` step is preceded by an implicit `goto(url)`.
    The final extraction runs on the LAST state of the page; screenshot
    + Sonnet vision get the chart values drawn after every step finished.

    Args:
        url: The starting page URL.
        steps: Ordered list of action dicts.
        focus: Extraction focus passed to Sonnet (same as `extract`).
        timeout_ms: Per-step navigation/wait timeout.
        full_page_screenshot: Whether the final screenshot is full-page.
        include_screenshot_in_response: Echo the final screenshot back in
            the MCP response (default False — agents don't need 700KB
            blobs in their context).

    Returns:
        Same shape as `extract` plus:
          step_results: [{step_index, action, ok, error?, duration_ms}, …]
          final_url: The page URL after all steps ran.
    """
    t0 = time.perf_counter()
    if not url:
        return {"error": "url is required"}
    if not steps or not isinstance(steps, list):
        return {"error": "steps must be a non-empty list"}

    client_id = _current_client.get() or "anon"
    ctx = await _get_context(client_id)
    page = await ctx.new_page()
    step_results: list[dict[str, Any]] = []
    try:
        # Initial navigation.
        try:
            await page.goto(url, wait_until="domcontentloaded",
                             timeout=timeout_ms)
        except Exception as e:  # noqa: BLE001
            return {"error": f"initial navigation failed: {e}",
                    "url": url, "step_results": []}

        for idx, step in enumerate(steps):
            if not isinstance(step, dict) or len(step) != 1:
                step_results.append({
                    "step_index": idx, "action": "invalid",
                    "ok": False,
                    "error": "each step must be {action: arg} with exactly one key",
                })
                continue
            action, arg = next(iter(step.items()))
            s_t0 = time.perf_counter()
            try:
                await _run_step(page, action, arg, timeout_ms)
                step_results.append({
                    "step_index": idx, "action": action, "ok": True,
                    "duration_ms": round((time.perf_counter() - s_t0) * 1000),
                })
            except Exception as e:  # noqa: BLE001
                step_results.append({
                    "step_index": idx, "action": action, "ok": False,
                    "duration_ms": round((time.perf_counter() - s_t0) * 1000),
                    "error": str(e)[:200],
                })
                # Don't bail — the agent may have provided a permissive script.

        # Final state.
        final_url = page.url
        title = (await page.title()) or ""
        try:
            text = await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            text = ""
        try:
            png = await page.screenshot(type="png",
                                          full_page=full_page_screenshot)
            shot_b64 = base64.b64encode(png).decode("ascii")
            shot_bytes = len(png)
        except Exception as e:
            log.warning("act screenshot failed: %s", e)
            shot_b64 = None
            shot_bytes = 0

        # Run the same Sonnet extraction as extract() — reuse the helper
        # by hand-constructing the `visited` dict shape.
        synthetic_visited = {
            "url": final_url,
            "title": title[:300],
            "domain": _domain(final_url),
            "text": (text or "")[:20_000],
            "screenshot_b64": shot_b64,
            "screenshot_bytes": shot_bytes,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        out = await _sonnet_extract(synthetic_visited, focus=focus)
        out["step_results"] = step_results
        out["final_url"] = final_url
        if include_screenshot_in_response and shot_b64:
            out["screenshot_b64"] = shot_b64
        _emit("act", t0,
               extra={"steps": len(steps), "shot_kb": round(shot_bytes / 1024)})
        return out
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _run_step(page: Page, action: str, arg: Any, timeout_ms: int) -> None:
    """Dispatch a single step from act()'s steps[] to Playwright. Each branch
    is intentionally narrow — anything else is rejected so the agent learns
    the supported vocabulary."""
    bounded = lambda v, default: min(int(v or default), timeout_ms)  # noqa: E731

    if action == "goto":
        await page.goto(str(arg), wait_until="domcontentloaded",
                          timeout=timeout_ms)
    elif action == "click":
        await page.click(str(arg), timeout=bounded(None, 15_000))
    elif action == "fill":
        if not isinstance(arg, dict):
            raise ValueError("fill requires {selector, value}")
        await page.fill(arg["selector"], str(arg.get("value", "")),
                          timeout=bounded(None, 15_000))
    elif action == "select":
        if not isinstance(arg, dict):
            raise ValueError("select requires {selector, value}")
        await page.select_option(arg["selector"], str(arg["value"]),
                                    timeout=bounded(None, 15_000))
    elif action == "press":
        if not isinstance(arg, dict):
            raise ValueError("press requires {selector, key}")
        await page.press(arg["selector"], str(arg["key"]),
                          timeout=bounded(None, 15_000))
    elif action == "scroll":
        to = (arg or {}).get("to") if isinstance(arg, dict) else arg
        if to in ("bottom", "end"):
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        elif to in ("top", "start"):
            await page.evaluate("() => window.scrollTo(0, 0)")
        else:
            try:
                px = int(to)
            except Exception:
                raise ValueError(f"scroll 'to' must be 'bottom'|'top'|<int>, got: {to!r}")
            await page.evaluate(f"() => window.scrollTo(0, {px})")
    elif action == "wait_for_selector":
        await page.wait_for_selector(str(arg), timeout=bounded(None, 15_000))
    elif action == "wait_for_load_state":
        await page.wait_for_load_state(str(arg or "networkidle"),
                                          timeout=bounded(None, 15_000))
    elif action == "wait_ms":
        await page.wait_for_timeout(int(arg or 0))
    elif action == "screenshot":
        # Intermediate screenshot — captured to logs but not returned
        # (keeping the MCP response small). The act() caller still gets
        # the FINAL screenshot fed into Sonnet vision.
        name = (arg or {}).get("name", "step") if isinstance(arg, dict) else "step"
        png = await page.screenshot(type="png", full_page=False)
        log.info(json.dumps({"evt": "step_screenshot", "name": name,
                              "bytes": len(png)}))
    else:
        raise ValueError(f"unsupported action: {action!r}")


async def _sonnet_extract(visited: dict[str, Any], *, focus: str = "") -> dict[str, Any]:
    """Shared LLM extraction over a `visited`-shaped payload. Used by extract()
    and act() so both produce identical output schemas."""
    base = {
        "url": visited["url"],
        "domain": visited.get("domain", ""),
        "fetched_at": visited["fetched_at"],
        "kind": "browser",
    }
    text = visited.get("text") or ""
    shot_b64 = visited.get("screenshot_b64")

    client = await _anthropic()
    if client is None:
        return {
            **base, "title": visited.get("title", ""),
            "dateline": "", "summary": text[:400],
            "key_facts": [], "numeric_values": [], "dates": [],
            "tables_summary": [], "raw_content": text[:8000],
            "note": "ANTHROPIC_API_KEY not set — returning raw content only.",
        }

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_content: list[dict[str, Any]] = []
    if shot_b64:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                         "data": shot_b64},
        })
    user_content.append({
        "type": "text",
        "text": (
            f"FOCUS: {focus or '(general — extract the main facts and tables)'}\n\n"
            f"PAGE TITLE: {visited.get('title', '')}\n"
            f"PAGE URL: {visited.get('url', '')}\n\n"
            f"PAGE TEXT (innerText, truncated):\n{text[:16000]}"
        ),
    })

    try:
        resp = await client.messages.create(
            model=_anthropic_model(),
            max_tokens=3000,
            system=[
                {"type": "text",
                 "text": STRUCTURED_EXTRACT_SYSTEM_STATIC,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text",
                 "text": dynamic_date_block(today_iso)},
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        body = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("act/extract LLM call failed: %s", e)
        return {**base, "title": visited.get("title", ""),
                "error_extract": str(e)[:200], "raw_content": text[:8000]}

    body = re.sub(r"^```(?:json)?\s*", "", body)
    body = re.sub(r"\s*```$", "", body)
    parsed = _parse_relaxed_json(body)

    out = {
        **base, "focus": focus,
        "title": parsed.get("title", visited.get("title", "")) or visited.get("title", ""),
        "dateline": parsed.get("dateline", ""),
        "summary": parsed.get("summary", ""),
        "key_facts": parsed.get("key_facts", []) or [],
        "numeric_values": parsed.get("numeric_values", []) or [],
        "dates": parsed.get("dates", []) or [],
        "tables_summary": parsed.get("tables_summary", []) or [],
    }
    if not parsed:
        out["raw_content"] = text[:8000]
        out["note"] = "Structured extraction failed; returning raw content."
    if shot_b64:
        out["screenshot_bytes"] = visited.get("screenshot_bytes")
    return out


async def extract(
    url: str,
    *,
    focus: str = "",
    wait_for_selector: str | None = None,
    full_page_screenshot: bool = True,
    include_screenshot_in_response: bool = False,
) -> dict[str, Any]:
    """Visit URL with Chromium → Sonnet structured extraction.

    Sends both the rendered text AND a full-page screenshot to Sonnet, so
    numbers drawn via canvas / SVG (charts on PPAC, RBI, NSE dashboards)
    that don't appear in the DOM text still get picked up.

    Returns the same shape as authority-web-search's pdf_fetch_structured /
    web_fetch_structured: {title, dateline, summary, key_facts,
    numeric_values, dates, tables_summary}.
    """
    t0 = time.perf_counter()
    visited = await visit(
        url,
        wait_for_selector=wait_for_selector,
        screenshot=True,
        full_page_screenshot=full_page_screenshot,
        text_cap=20_000,
        # We NEED the bytes for Sonnet vision — extract() always asks for them.
        # _sonnet_extract reads `screenshot_b64`; the final response strips it
        # back out unless the caller explicitly asked for it via
        # include_screenshot_in_response.
        return_screenshot_b64=True,
    )
    if "error" in visited:
        return visited

    out = await _sonnet_extract(visited, focus=focus)
    if include_screenshot_in_response and visited.get("screenshot_b64"):
        out["screenshot_b64"] = visited["screenshot_b64"]
    _emit("extract", t0,
           extra={"shot_kb": round((visited.get("screenshot_bytes", 0)) / 1024)})
    return out
