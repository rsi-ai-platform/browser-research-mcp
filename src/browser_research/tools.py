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
# Set only when BROWSER_ENGINE=camoufox — the AsyncCamoufox context manager that
# owns the Firefox process + its own Playwright instance (closed in shutdown()).
_camoufox_mgr: Any | None = None
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
    # Haiku 4.5 supports vision and handles the fixed-schema structured
    # extraction job (title/dateline/summary/key_facts/numeric_values/
    # dates/tables_summary) at ~1/3 the Sonnet cost. The screenshot+text
    # input is high-volume per call, so this is where the spend sat.
    # Override via ANTHROPIC_MODEL=claude-sonnet-4-6 on Cloud Run if a
    # specific page needs the bigger model for stubborn chart parsing.
    return os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")


def _tavily_key() -> str | None:
    return os.environ.get("TAVILY_API_KEY") or None


def _browser_engine() -> str:
    """Which browser engine to launch: "chromium" (default, patchright) or
    "camoufox" (Firefox-based anti-detect; optional, see README). Unknown values
    fall back to chromium."""
    eng = os.environ.get("BROWSER_ENGINE", "chromium").strip().lower()
    return eng if eng in ("chromium", "camoufox") else "chromium"


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
    """Return the shared browser, launching the configured engine on first use."""
    global _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        if _browser_engine() == "camoufox":
            _browser = await _launch_camoufox()
        else:
            _browser = await _launch_chromium()
        return _browser


async def _launch_chromium() -> Browser:
    """Patchright-patched Chromium — the default engine. Set BROWSER_CHANNEL=chrome
    to drive a real Google Chrome binary (must be installed in the image) for a
    genuine Chrome TLS/version fingerprint; unset uses the bundled Chromium."""
    global _pw_instance
    if _pw_instance is None:
        _pw_instance = await async_playwright().start()
    channel = os.environ.get("BROWSER_CHANNEL") or None
    # Container-friendly flags. --no-sandbox is required when running as a
    # non-root user inside Docker; patchright keeps the stealth patches active
    # regardless.
    browser = await _pw_instance.chromium.launch(
        headless=os.environ.get("HEADLESS", "true").lower() != "false",
        **({"channel": channel} if channel else {}),
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    log.info("Chromium launched (patchright stealth, channel=%s)",
              channel or "bundled")
    return browser


async def _launch_camoufox() -> Browser:
    """Camoufox — a Firefox fork with engine-level fingerprint spoofing. Optional
    and lazily imported, so it's a no-op unless BROWSER_ENGINE=camoufox AND the
    package is installed (`pip install 'camoufox[geoip]'` + `python -m camoufox
    fetch`). Renders + screenshots like Chromium, so the Sonnet-vision path is
    unaffected."""
    global _camoufox_mgr
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError as e:  # pragma: no cover - only hit when opted in
        raise RuntimeError(
            "BROWSER_ENGINE=camoufox but the 'camoufox' package isn't installed. "
            "Run `pip install 'camoufox[geoip]'` and `python -m camoufox fetch`, "
            "add Firefox's system libs to the image, then redeploy. "
            "See README -> Browser engines."
        ) from e
    headless_env = os.environ.get("HEADLESS", "true").lower()
    # "virtual" -> Camoufox auto-manages an Xvfb display (most stealthy on a
    # headless host; needs xvfb installed). "false" -> headful. else headless.
    headless: Any = ("virtual" if headless_env == "virtual"
                     else False if headless_env == "false" else True)
    # Kept minimal to stay launch-safe across versions. Tune later: humanize=True
    # (human cursor), os=..., proxy=..., geoip=True (auto-aligns tz/locale/WebGL
    # to a proxy's exit IP — enable once a residential proxy is wired).
    mgr = AsyncCamoufox(headless=headless, locale="en-IN")
    browser = await mgr.__aenter__()
    _camoufox_mgr = mgr
    log.info("Camoufox (Firefox) launched (headless=%s)", headless)
    return browser


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
    ctx_opts: dict[str, Any] = {
        "locale": "en-IN",
        "timezone_id": "Asia/Kolkata",
        "viewport": {"width": 1440, "height": 900},
        "accept_downloads": False,
    }
    # Only pin a Chrome UA for the Chromium engine. Camoufox generates its own
    # coherent Firefox fingerprint at launch — forcing a Chrome UA onto it would
    # be a glaring inconsistency that defeats the point.
    if _browser_engine() != "camoufox":
        ctx_opts["user_agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    ctx = await browser.new_context(**ctx_opts)
    _contexts[client_id] = ctx
    return ctx


async def shutdown() -> None:
    """Graceful cleanup. Cloud Run signals SIGTERM ~10s before kill;
    server.py's lifespan hooks call this."""
    global _browser, _pw_instance, _contexts, _camoufox_mgr
    for ctx in list(_contexts.values()):
        try:
            await ctx.close()
        except Exception:
            pass
    _contexts.clear()
    # Camoufox owns its browser + its own Playwright instance via the context
    # manager, so exit that instead of closing _browser / _pw_instance directly.
    if _camoufox_mgr is not None:
        try:
            await _camoufox_mgr.__aexit__(None, None, None)
        except Exception:
            pass
        _camoufox_mgr = None
        _browser = None
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
# Fallback fetch chain. The primary path is a real Chromium (visit/act), but
# gov / enterprise CDNs (Akamai, Cloudflare, Imperva) routinely bot-block our
# Cloud Run egress IP and serve a 200-OK "Access Denied" / JS-challenge page
# instead of content. When that happens we re-fetch the SAME url from different
# infrastructure:
#   1) Tavily Extract     — cheap, fast, different egress IP. Needs TAVILY_API_KEY.
#   2) Anthropic web_fetch — server-side fetch via the Messages API. Reuses
#      ANTHROPIC_API_KEY (already required for extraction). Server-rendered HTML
#      + PDFs only — no JS rendering.
# Both return a visit()-shaped dict (plus a `source` tag) so the rest of the
# pipeline — caching, _sonnet_extract, the MCP response — is unchanged.
# ============================================================================

# CDN bot-walls return HTTP 200 with a tiny deny/challenge body. Match the
# common shapes so we fall through instead of handing the agent a useless page.
_BLOCK_MARKERS = (
    "access denied",
    "you don't have permission to access",
    "attention required",               # Cloudflare
    "just a moment",                    # Cloudflare JS challenge / DDoS-Guard
    "checking your browser",
    "enable javascript and cookies to continue",
    "request unsuccessful. incapsula",  # Imperva
    "/cdn-cgi/",                        # Cloudflare challenge assets
    "reference #",                      # Akamai deny reference id
)


def _looks_blocked(title: str, text: str) -> str | None:
    """Return a short reason string if (title, text) look like a CDN bot-wall
    or JS challenge rather than real page content, else None."""
    t = (title or "").lower()
    if any(m in t for m in ("access denied", "attention required",
                            "just a moment", "forbidden")):
        return "challenge_title"
    body = (text or "").strip()
    if len(body) < 32:
        return "empty_body"
    # Real pages occasionally mention "access denied" in prose, so only treat a
    # marker as a block when the whole body is short (deny pages are tiny).
    if len(body) < 2000:
        low = body.lower()
        for m in _BLOCK_MARKERS:
            if m in low:
                return f"marker:{m.strip()}"
    return None


# Login/registration gates on a *download or data view*. Distinct from a bot
# wall: no fetch-fallback gets you past auth, so the right move is to pivot to
# an open source (which the domain's playbook surfaces). Phrases are specific
# enough that an ordinary "Log in" nav link won't trip them.
_AUTHWALL_MARKERS = (
    "register with us to download",
    "register to download",
    "please register to",
    "login to download",
    "log in to download",
    "sign in to download",
    "login to access",
    "log in to access",
    "please log in to view",
    "subscription required",
    "subscribe to download",
    "members only",
    "registered users only",
)


def _looks_authwalled(text: str) -> str | None:
    """Return a reason if the page gates its data/download behind login or
    registration, else None."""
    low = (text or "").lower()
    for m in _AUTHWALL_MARKERS:
        if m in low:
            return f"auth_wall:{m}"
    return None


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` from an SDK object or a plain dict — web_fetch result blocks
    surface as either depending on the installed anthropic SDK version."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def _tavily_fetch(url: str, *, text_cap: int) -> dict[str, Any] | None:
    """Re-fetch `url` via Tavily Extract. Returns a visit()-shaped dict, or None
    when no key is set / transport fails / the body is empty."""
    key = _tavily_key()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(
                "https://api.tavily.com/extract",
                headers={"Authorization": f"Bearer {key}"},
                json={"urls": [url], "extract_depth": "advanced",
                      "format": "markdown"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("tavily fallback failed for %s: %s", url, str(e)[:120])
        return None
    results = data.get("results") or []
    raw = (results[0].get("raw_content") or "").strip() if results else ""
    if not raw:
        return None
    return {
        "url": url,
        "title": "",
        "domain": _domain(url),
        "text": raw[:text_cap],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "current_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "tavily",
    }


async def _anthropic_web_fetch(url: str, *, text_cap: int) -> dict[str, Any] | None:
    """Last-resort fetch via Anthropic's server-side web_fetch tool. The url is
    placed in the user turn because web_fetch only retrieves URLs already
    present in the conversation. Server-rendered HTML only (no JS); PDFs come
    back base64 and are skipped here (use download_file for those). Returns a
    visit()-shaped dict or None."""
    client = await _anthropic()
    if client is None:
        return None
    try:
        resp = await client.messages.create(
            model=_anthropic_model(),
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": (
                    "Use the web_fetch tool to fetch this URL, then reply "
                    f"'done': {url}"
                ),
            }],
            tools=[{
                "type": "web_fetch_20250910",
                "name": "web_fetch",
                "max_uses": 1,
                "max_content_tokens": 100_000,
            }],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("anthropic web_fetch failed for %s: %s", url, str(e)[:120])
        return None

    for block in resp.content:
        if _field(block, "type") != "web_fetch_tool_result":
            continue
        result = _field(block, "content")
        rtype = _field(result, "type")
        if rtype == "web_fetch_tool_error":
            log.warning("anthropic web_fetch error for %s: %s", url,
                        _field(result, "error_code", "?"))
            return None
        if rtype != "web_fetch_result":
            continue
        fetched_url = _field(result, "url", url) or url
        doc = _field(result, "content")          # the document content block
        src = _field(doc, "source")
        if _field(src, "type") != "text":        # base64 PDF — not handled here
            return None
        text = (_field(src, "data", "") or "").strip()
        if not text:
            return None
        return {
            "url": fetched_url,
            "title": (_field(doc, "title", "") or "")[:300],
            "domain": _domain(fetched_url),
            "text": text[:text_cap],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "current_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "source": "anthropic_web_fetch",
        }
    return None


async def _fallback_fetch(url: str, *, text_cap: int,
                          reason: str) -> dict[str, Any] | None:
    """Run the fallback chain (Tavily → Anthropic web_fetch) for a url the
    Chromium path couldn't read. Returns the first success (visit()-shaped,
    tagged with `source` + `fallback_reason`) or None if all are unavailable."""
    t0 = time.perf_counter()
    out = await _tavily_fetch(url, text_cap=text_cap)
    if out is None:
        out = await _anthropic_web_fetch(url, text_cap=text_cap)
    if out is not None:
        out["fallback_reason"] = reason
    _emit("fallback_fetch", t0, extra={
        "ok": out is not None,
        "via": (out or {}).get("source"),
        "reason": reason,
        "chars": len((out or {}).get("text") or ""),
    })
    return out


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

    def _finalize_fb(fb: dict[str, Any]) -> dict[str, Any]:
        # Cache + shape a fallback result exactly like a normal visit() return.
        if fb.get("text"):
            _visit_cache[cache_key] = dict(fb)
        _emit("visit", t0, extra={"chars": len(fb.get("text") or ""),
                                   "via": fb.get("source")})
        if return_screenshot_b64:
            return fb
        return {k: v for k, v in fb.items() if k != "screenshot_b64"}

    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded",
                             timeout=timeout_ms)
        except Exception as e:  # noqa: BLE001
            # Network-level failure (timeout, DNS, connection reset). Try the
            # alternate fetch paths before giving up.
            fb = await _fallback_fetch(url, text_cap=text_cap,
                                        reason=f"goto:{str(e)[:40]}")
            if fb is not None:
                return _finalize_fb(fb)
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

        # Bot-wall / challenge detection. The page loaded with HTTP 200 but the
        # body is a CDN deny notice, not content — re-fetch from other infra.
        block_reason = _looks_blocked(title, text)
        if block_reason:
            fb = await _fallback_fetch(url_final, text_cap=text_cap,
                                        reason=block_reason)
            if fb is not None:
                return _finalize_fb(fb)
            # Fallbacks unavailable/failed — fall through and return the blocked
            # page, but tag it (below) so it isn't mistaken for real content.

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
        if block_reason:
            # Fallbacks were unavailable or also failed; surface the block so
            # the agent treats this as a wall rather than empty content.
            out["blocked"] = block_reason
        auth_wall = _looks_authwalled(text)
        if auth_wall:
            # Login/registration gate — no fetch-fallback helps; the matched
            # playbook (attached at the server layer) points to an open source.
            out["auth_wall"] = auth_wall
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

        # If the final page is a CDN bot-wall, the interaction results are
        # meaningless. Re-fetch the URL from other infra so the agent still gets
        # the page's content — but flag clearly that the steps were NOT applied
        # (a plain fetch can't replay dropdown/click/filter actions).
        block_reason = _looks_blocked(title, text)
        if block_reason:
            fb = await _fallback_fetch(final_url, text_cap=20_000,
                                        reason=block_reason)
            if fb is not None and fb.get("text"):
                out = await _sonnet_extract(fb, focus=focus)
                out["step_results"] = step_results
                out["final_url"] = fb.get("url", final_url)
                out["degraded"] = (
                    f"Live page was blocked ({block_reason}); the interaction "
                    f"steps could NOT be applied. Returned a static fetch of the "
                    f"URL via {fb.get('source')} — any dropdown/click/filter "
                    f"results are not reflected. The target site must be "
                    f"reachable from the browser to capture post-interaction state."
                )
                _emit("act", t0, extra={"steps": len(steps),
                                         "via": fb.get("source"),
                                         "degraded": True})
                return out
            # Fallbacks unavailable/failed — return the blocked extraction but
            # tag it (below).

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
        if block_reason:
            out["blocked"] = block_reason
        auth_wall = _looks_authwalled(text)
        if auth_wall:
            out["auth_wall"] = auth_wall
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
        # Provenance: "browser" (Chromium), "tavily", or "anthropic_web_fetch".
        "source": visited.get("source") or "browser",
    }
    if visited.get("blocked"):
        base["blocked"] = visited["blocked"]
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


# ============================================================================
# download_file — fetch + parse spreadsheets and PDFs.
#
# Port of the battle-tested fetch+parse pipeline from authority-web-search.
# One tool, format auto-detected by content-type + magic bytes, parsed by
# openpyxl / xlrd / csv / pypdf, returns the same classified-error shape so
# the calling model can navigate identically across both MCPs.
#
# This closes the loop on URLs that the Chromium-based visit/act/extract
# tools surface as `file_links` but can't read themselves — gov-site Excel
# bulletins (gst.gov.in, cga.nic.in, mospi.gov.in), PDF circulars (RBI,
# SEBI), CSV data dumps (data.gov.in).
# ============================================================================

_EXCEL_DOWNLOAD_CAP_BYTES = 16 * 1024 * 1024   # 16 MB
_PDF_DOWNLOAD_CAP_BYTES = 24 * 1024 * 1024     # 24 MB
_EXCEL_TEXT_CAP = 80_000                       # chars
_PDF_TEXT_CAP = 80_000                         # chars


def _is_excel_url(url: str) -> bool:
    if not url:
        return False
    try:
        u = urlparse(url.lower())
    except Exception:
        return False
    for ext in (".xlsx", ".xlsm", ".xls", ".csv", ".tsv"):
        if u.path.endswith(ext) or ext in (u.query or ""):
            return True
    return False


def _is_pdf_url(url: str) -> bool:
    if not url:
        return False
    try:
        u = urlparse(url.lower())
    except Exception:
        return False
    return u.path.endswith(".pdf") or ".pdf" in (u.query or "")


def _parse_excel_sync(raw_bytes: bytes, sheet: str | None,
                       max_rows_per_sheet: int) -> dict[str, Any]:
    import io as _io
    sheets: list[dict[str, Any]] = []
    fmt = "xlsx"
    try:
        from openpyxl import load_workbook
    except Exception as e:  # noqa: BLE001
        return {"error": f"openpyxl unavailable: {e}"}
    try:
        wb = load_workbook(filename=_io.BytesIO(raw_bytes), read_only=True,
                            data_only=True)
        sheet_names = wb.sheetnames
        targets = [sheet] if sheet and sheet in sheet_names else sheet_names
        chunks: list[str] = []
        total_chars = 0
        for sn in targets:
            ws = wb[sn]
            rows_out: list[list[str]] = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                vals = [("" if v is None else str(v)) for v in row]
                while vals and not vals[-1].strip():
                    vals.pop()
                if not vals:
                    continue
                rows_out.append(vals)
                if i >= max_rows_per_sheet:
                    break
            header = rows_out[0] if rows_out else []
            sample = rows_out[1:11]
            sheets.append({
                "name": sn,
                "rows": len(rows_out),
                "cols": max((len(r) for r in rows_out), default=0),
                "header": header,
                "sample": sample,
            })
            chunk = f"\n\n=== Sheet: {sn} ===\n" + "\n".join(
                "\t".join(r) for r in rows_out
            )
            if total_chars + len(chunk) > _EXCEL_TEXT_CAP:
                chunk = chunk[: _EXCEL_TEXT_CAP - total_chars]
                chunks.append(chunk)
                break
            chunks.append(chunk)
            total_chars += len(chunk)
        return {"sheets": sheets, "content": "".join(chunks).strip(),
                "format": fmt, "sheet_count": len(sheet_names)}
    except Exception as e:  # noqa: BLE001
        # Fall back to xlrd for legacy .xls before giving up.
        try:
            import xlrd  # type: ignore
            book = xlrd.open_workbook(file_contents=raw_bytes)
            chunks: list[str] = []
            total_chars = 0
            for s_idx in range(book.nsheets):
                ws = book.sheet_by_index(s_idx)
                rows_out: list[list[str]] = []
                for i in range(ws.nrows):
                    vals = [str(ws.cell_value(i, j)) for j in range(ws.ncols)]
                    while vals and not vals[-1].strip():
                        vals.pop()
                    if not vals:
                        continue
                    rows_out.append(vals)
                    if i >= max_rows_per_sheet:
                        break
                header = rows_out[0] if rows_out else []
                sample = rows_out[1:11]
                sheets.append({
                    "name": ws.name, "rows": len(rows_out),
                    "cols": max((len(r) for r in rows_out), default=0),
                    "header": header, "sample": sample,
                })
                chunk = f"\n\n=== Sheet: {ws.name} ===\n" + "\n".join(
                    "\t".join(r) for r in rows_out
                )
                if total_chars + len(chunk) > _EXCEL_TEXT_CAP:
                    chunk = chunk[: _EXCEL_TEXT_CAP - total_chars]
                    chunks.append(chunk)
                    break
                chunks.append(chunk)
                total_chars += len(chunk)
            return {"sheets": sheets, "content": "".join(chunks).strip(),
                    "format": "xls", "sheet_count": book.nsheets}
        except Exception as e2:  # noqa: BLE001
            return {"error": f"could not open Excel: {e}; xls fallback: {e2}"}


def _parse_csv_sync(raw_bytes: bytes, max_rows: int) -> dict[str, Any]:
    import csv as _csv
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        dialect = _csv.Sniffer().sniff(text[:4096])
    except Exception:
        dialect = _csv.excel
    reader = _csv.reader(text.splitlines(), dialect=dialect)
    rows: list[list[str]] = []
    for i, r in enumerate(reader):
        rows.append([c.strip() for c in r])
        if i >= max_rows:
            break
    header = rows[0] if rows else []
    sample = rows[1:11]
    content = "\n".join("\t".join(r) for r in rows)[: _EXCEL_TEXT_CAP]
    return {"sheets": [{"name": "CSV", "rows": len(rows),
                          "cols": max((len(r) for r in rows), default=0),
                          "header": header, "sample": sample}],
            "content": content, "format": "csv", "sheet_count": 1}


def _parse_pdf_sync(raw_bytes: bytes, pages: list[int] | None,
                    max_pages: int) -> dict[str, Any]:
    import io as _io
    try:
        from pypdf import PdfReader
    except Exception as e:  # noqa: BLE001
        return {"error": f"pypdf unavailable: {e}"}
    try:
        reader = PdfReader(_io.BytesIO(raw_bytes))
        n_pages = len(reader.pages)
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not open PDF: {e}"}
    wanted = ([i - 1 for i in pages if 1 <= i <= n_pages] if pages
              else list(range(min(n_pages, max_pages))))
    chunks: list[str] = []
    extracted: list[int] = []
    total_chars = 0
    truncated = False
    for i in wanted:
        try:
            txt = reader.pages[i].extract_text() or ""
        except Exception:  # noqa: BLE001
            txt = ""
        if not txt:
            continue
        chunk = f"\n\n--- Page {i + 1} ---\n{txt}"
        if total_chars + len(chunk) > _PDF_TEXT_CAP:
            chunk = chunk[: _PDF_TEXT_CAP - total_chars]
            chunks.append(chunk)
            extracted.append(i + 1)
            truncated = True
            break
        chunks.append(chunk)
        extracted.append(i + 1)
        total_chars += len(chunk)
    return {
        "content": "".join(chunks).strip(),
        "page_count": n_pages,
        "pages_extracted": extracted,
        "content_truncated": truncated or len(wanted) < n_pages,
    }


async def download_file(
    url: str,
    *,
    sheet: str | None = None,
    pages: list[int] | None = None,
    max_rows_per_sheet: int = 200,
    max_pdf_pages: int = 30,
) -> dict[str, Any]:
    """Download a URL and parse it as a spreadsheet (.xlsx/.xlsm/.xls/.csv/
    .tsv) or PDF. Format is auto-detected via content-type + magic bytes.

    Returns one of:
      • Spreadsheet → {kind: "spreadsheet", url, domain, format,
                       sheets[], content, sheet_count, fetched_at}
      • PDF         → {kind: "pdf", url, domain, content, page_count,
                       pages_extracted, content_truncated, fetched_at}
      • Error       → {error, error_kind, url, domain, ...}

    error_kind values: http_error, html_masquerade, truncated_body,
    invalid_xlsx, parse_error, wrong_content_type, too_large.
    """
    t0 = time.perf_counter()
    domain = _domain(url)
    # Stealth-ish headers so gov-CDNs that block "python-httpx/x.y" still
    # serve us. Same UA shape as the Playwright contexts.
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/127.0.0.0 Safari/537.36"),
        "Accept": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, "
                   "application/vnd.ms-excel, application/pdf, text/csv, */*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True,
                                       headers=headers) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        out = {"error": f"download failed: {e}", "error_kind": "http_error",
                "url": url, "domain": domain}
        _emit("download_file", t0, extra={"err": "transport"})
        return out

    ct = (r.headers.get("content-type") or "").lower()
    body_head = r.content[:512]
    looks_like_html = (
        body_head[:5].lower() in (b"<html", b"<!doc")
        or b"<html" in body_head[:200].lower()
    )

    if r.status_code >= 400:
        _emit("download_file", t0, extra={"err": "http", "status": r.status_code})
        return {
            "error": (f"HTTP {r.status_code} from server — the URL is broken "
                       f"or you don't have access. NOT a parsing issue; pick a "
                       f"different file."),
            "error_kind": "http_error",
            "http_status": r.status_code,
            "url": url, "domain": domain,
        }
    if looks_like_html and not _is_pdf_url(url):
        _emit("download_file", t0, extra={"err": "html"})
        return {
            "error": ("Server returned an HTML page (likely a 404 redirect "
                       "or login wall), not a file. The URL probably "
                       "redirected somewhere else."),
            "error_kind": "html_masquerade",
            "url": url, "domain": domain,
            "body_head": body_head[:200].decode("utf-8", errors="replace"),
        }
    if len(r.content) < 256:
        _emit("download_file", t0, extra={"err": "truncated"})
        return {
            "error": (f"Server returned only {len(r.content)} bytes — too "
                       f"small to be a real file. The URL is almost certainly "
                       f"broken."),
            "error_kind": "truncated_body",
            "url": url, "domain": domain,
            "body_head": body_head.decode("utf-8", errors="replace"),
        }

    # Magic-byte detection: xlsx = PK (zip); xls = OLE; pdf = %PDF-.
    is_xlsx = r.content[:2] == b"PK"
    is_xls = r.content[:8].startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    is_pdf = (r.content[:5] == b"%PDF-"
              or r.content[:8].lstrip().startswith(b"%PDF-"))
    is_csv = (
        ("csv" in ct) or url.lower().endswith(".csv")
        or (not is_xlsx and not is_xls and not is_pdf
            and url.lower().endswith(".tsv"))
    )

    if is_pdf or "pdf" in ct or _is_pdf_url(url):
        if len(r.content) > _PDF_DOWNLOAD_CAP_BYTES:
            _emit("download_file", t0, extra={"err": "too_large",
                                                "bytes": len(r.content)})
            return {
                "error": (f"PDF too large ({len(r.content) // (1024*1024)} MB, "
                           f"cap {_PDF_DOWNLOAD_CAP_BYTES // (1024*1024)} MB)"),
                "error_kind": "too_large",
                "url": url, "domain": domain,
            }
        parsed = await asyncio.to_thread(_parse_pdf_sync, r.content,
                                           pages, max_pdf_pages)
        if "error" in parsed:
            _emit("download_file", t0, extra={"err": "pdf_parse"})
            return {**parsed, "error_kind": "parse_error",
                    "url": url, "domain": domain}
        out = {
            "kind": "pdf",
            "url": url, "domain": domain,
            "content": parsed["content"],
            "page_count": parsed["page_count"],
            "pages_extracted": parsed["pages_extracted"],
            "content_truncated": parsed["content_truncated"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        _emit("download_file", t0, extra={"fmt": "pdf",
                                            "pages": parsed["page_count"]})
        return out

    if len(r.content) > _EXCEL_DOWNLOAD_CAP_BYTES:
        _emit("download_file", t0, extra={"err": "too_large",
                                            "bytes": len(r.content)})
        return {
            "error": (f"spreadsheet too large "
                       f"({len(r.content) // (1024*1024)} MB, cap "
                       f"{_EXCEL_DOWNLOAD_CAP_BYTES // (1024*1024)} MB)"),
            "error_kind": "too_large",
            "url": url, "domain": domain,
        }

    if is_csv:
        parsed = await asyncio.to_thread(_parse_csv_sync, r.content,
                                           max_rows_per_sheet)
    elif (is_xlsx or is_xls or _is_excel_url(url)
            or "excel" in ct or "spreadsheet" in ct):
        parsed = await asyncio.to_thread(_parse_excel_sync, r.content,
                                           sheet, max_rows_per_sheet)
    else:
        _emit("download_file", t0, extra={"err": "wrong_ct", "ct": ct})
        return {"error": f"not a supported file format (content-type: "
                          f"{ct or 'unknown'}). download_file handles "
                          f".xlsx/.xls/.csv/.tsv/.pdf.",
                "error_kind": "wrong_content_type",
                "url": url, "domain": domain}

    if "error" in parsed:
        msg = parsed["error"]
        if "[Content_Types].xml" in msg or "Unknown ZIP file" in msg:
            _emit("download_file", t0, extra={"err": "invalid_xlsx"})
            return {
                "error": ("File downloaded but is not a valid xlsx — the URL "
                           "probably points to a corrupt or stub file. (Parse "
                           f"error: {msg[:120]})"),
                "error_kind": "invalid_xlsx",
                "url": url, "domain": domain,
            }
        _emit("download_file", t0, extra={"err": "parse"})
        return {**parsed, "error_kind": "parse_error",
                "url": url, "domain": domain}

    out = {
        "kind": "spreadsheet",
        "url": url, "domain": domain,
        "format": parsed["format"],
        "sheet_count": parsed["sheet_count"],
        "sheets": parsed["sheets"],
        "content": parsed["content"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _emit("download_file", t0, extra={"fmt": parsed["format"],
                                        "sheets": parsed["sheet_count"]})
    return out
