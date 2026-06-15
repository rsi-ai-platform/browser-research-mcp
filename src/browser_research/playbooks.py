"""Domain playbooks — per-site recipes the agent reads BEFORE attacking a known
hard page, so exploration becomes a lookup.

Effective list = the in-repo DEFAULT_PLAYBOOKS with the GCS object LAYERED ON
TOP, keyed by id:
  * GCS object (PLAYBOOKS_GCS_BUCKET / PLAYBOOKS_GCS_OBJECT) — the live overlay,
    editable WITHOUT a redeploy; hot-reloaded on a short TTL. Overrides/extends
    per-id (and may carry brand-new ids).
  * DEFAULT_PLAYBOOKS below — the in-repo seed. Ids not present in the overlay
    come straight from here, so a NEW code-default playbook auto-surfaces even
    when an overlay exists (no re-seed needed). When GCS is unconfigured or
    unreachable, the defaults stand alone.

An entry:
  {
    "id": "ppac-consumption",
    "match": {"domain": "ppac.gov.in", "path_prefix": "/consumption"},
    "strategy": "...",          # 1-2 lines: how to actually get the data
    "avoid": ["...", ...],      # dead-ends WITH the reason (kills thrash loops)
    "open_data": [              # the escape hatch, as fetchable URLs
        {"tool": "download_file", "url"|"url_pattern": "...", "note": "..."}],
    "act_steps": [ {<single-key action>}, ... ],   # known-good interaction recipe
    "api": [                    # discovered AJAX endpoint(s) — replay via call_api
        {"endpoint": "https://…", "method": "POST",
         "params": {"financialYear": "<FY e.g. 2023-2024>", …},  # null if TBD
         "note": "what it returns; reaches periods the UI never exposes"}],
    "proxy": true,              # route this domain through the residential proxy
                                # (BROWSER_PROXY_* env) — its egress IP is blocked
    "last_verified": "YYYY-MM-DD",
  }
`match` requires `domain`; `path_prefix` and `path_regex` optionally narrow it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("browser_research")


# ============================================================================
# In-repo seed / fallback. Everything verified this session goes here so the
# agent never re-explores PPAC or PIB from scratch.
# ============================================================================

DEFAULT_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "id": "ppac-consumption",
        "match": {"domain": "ppac.gov.in", "path_prefix": "/consumption"},
        "strategy": ("Don't fight the year selector — it's a non-native JS "
                     "widget. Best path: replay the AJAX endpoint the table "
                     "fires (see `api`) via call_api, templating the year. "
                     "Discover its exact params once with inspect_network if "
                     "they drift. The data.gov.in CSV mirror is the simplest "
                     "fully-open fallback; the historical download is "
                     "register+captcha gated."),
        "avoid": [
            "act/select on the year dropdown — it's a custom JS widget, not a "
            "native <select>; select_option will time out",
            "'Download Historical/Current Report' buttons — register + captcha "
            "gated (hard stop for automation)",
            "guessing ppac.gov.in/download.php?file=... paths blindly",
        ],
        "api": [
            {"endpoint": "https://ppac.gov.in/AjaxController/"
                         "getConsumptionPetroleumProductsData",
             "method": "POST",
             "params": None,
             "note": "Form-encoded POST that powers the products-wise table "
                     "(monthly + annual total, '000 MT). Run inspect_network "
                     "on /consumption/products-wise after a year change to "
                     "capture the exact param names, then replay via call_api "
                     "for any FY — including ones absent from the dropdown."},
        ],
        "open_data": [
            {"tool": "download_file",
             "url": "https://www.data.gov.in/resource/monthly-consumption-petroleum-products",
             "note": "Open Govt Data CSV — Month, Year, PRODUCTS, "
                     "Quantity (000 MT), 1998-99 to latest. Cleanest source."},
            {"tool": "download_file",
             "url_pattern": "https://ppac.gov.in/download.php?file=menu/"
                            "<id>_ICR_<Month>_<Year>_compressed.pdf",
             "note": "Monthly Industry Consumption Report (POL & NG) PDF — "
                     "open, no auth, carries current+prior-period comparisons"},
        ],
        "last_verified": "2026-06-16",
    },
    {
        "id": "ppac-natural-gas-consumption",
        "match": {"domain": "ppac.gov.in",
                  "path_prefix": "/natural-gas/consumption"},
        "strategy": ("The year dropdown lists only the last ~2 fiscal years and "
                     "is a custom JS widget. Skip the UI entirely: replay the "
                     "getGasConsumption endpoint (see `api`) via call_api — it "
                     "returns every year on file, including ones the dropdown "
                     "omits."),
        "api": [
            {"endpoint": "https://ppac.gov.in/AjaxController/getGasConsumption",
             "method": "POST",
             "params": {"financialYear": "<FY e.g. 2023-2024>",
                        "reportBy": "4", "pageId": "138"},
             "note": "Form-encoded POST → JSON. result is rows keyed by index; "
                     "each row has april…march + total in MMSCM. Rows: Net "
                     "Production, LNG import, Total Consumption (= Net "
                     "Production + LNG import). Verified for FY2023-24 through "
                     "FY2025-26; FY2023-24 is NOT in the dropdown but the API "
                     "returns it."},
        ],
        "avoid": [
            "driving the year <select> with act — non-native JS widget; "
            "select_option times out",
        ],
        "last_verified": "2026-06-16",
    },
    {
        "id": "cga-monthly-accounts",
        "match": {"domain": "cga.nic.in"},
        "strategy": ("Headline Union-Govt fiscal data (Total Receipts, "
                     "Expenditure, Deficit; actuals vs BE/RE; monthly since "
                     "Apr-2015) lives in ONE downloadable Excel on the Monthly "
                     "Accounts Dashboard — NOT in the homepage month-picker "
                     "(those are ASP.NET __doPostBack controls; don't drive "
                     "them). visit the dashboard page, take the .xlsm "
                     "file_link, then download_file it with a `query` (e.g. "
                     "'February 2026 fiscal deficit') to pull just the rows you "
                     "need rather than the whole workbook."),
        "avoid": [
            "driving the homepage #account-section month/year selectors — "
            "ASP.NET postback widgets, not real links; the data isn't gated "
            "behind them",
            "loading the whole dashboard .xlsm into context — it holds every "
            "month since 2015-16; use download_file(query=...) to grep only the "
            "target period",
            "guessing the .xlsm filename — the suffix varies; read the live "
            "href off the dashboard page",
        ],
        "open_data": [
            {"tool": "visit",
             "url": "https://cga.nic.in/MonthDashboardReport/Published/list.aspx",
             "note": "Monthly Accounts Dashboard — static page; exposes the "
                     "current 'DAMA dashboard <Month> <Year> Data file….xlsm' "
                     "link under /writereaddata/MonthAccount/"
                     "MonthAccountDashboard/. Grab that href, then "
                     "download_file it (with query= for one period)."},
            {"tool": "download_file",
             "url_pattern": "https://cga.nic.in/writereaddata/MonthAccount/"
                            "MonthAccountDashboard/DAMA dashboard <Month> "
                            "<Year> Data file*.xlsm",
             "note": ".xlsm covering all FY/months since 2015-16. Pass query= "
                     "to return only matching rows."},
            {"tool": "web_fetch",
             "url": "https://cga.nic.in/Accountproc.aspx",
             "note": "Financial Reports index (Monthly/Annual Accounts, "
                     "Finance & Appropriation Accounts)."},
            {"tool": "web_fetch",
             "url": "https://cga.nic.in/NSD/Published/list.aspx",
             "note": "National Summary Data Page (IMF SDDS) — key fiscal "
                     "aggregates, server-rendered."},
        ],
        "last_verified": "2026-06-16",
    },
    {
        "id": "pib-allrel",
        "match": {"domain": "pib.gov.in", "path_prefix": "/allRel.aspx"},
        "strategy": ("Date-filtered ASP.NET listing. Needs a NON-datacenter IP "
                     "(Akamai blocks datacenter egress with 'Access Denied'). "
                     "With a clean IP, drive the dropdowns then submit."),
        "act_steps": [
            {"select": {"selector": "#ctl00_ContentPlaceHolder1_ddlday",
                        "value": "<DD>"}},
            {"select": {"selector": "#ctl00_ContentPlaceHolder1_ddlMonth",
                        "value": "<MM>"}},
            {"select": {"selector": "#ctl00_ContentPlaceHolder1_ddlyear",
                        "value": "<YYYY>"}},
            {"click": "#ctl00_ContentPlaceHolder1_btnSubmit"},
        ],
        "avoid": [
            "retrying from a datacenter IP — Akamai returns a challenge_title "
            "'Access Denied' page no matter how the dropdown is driven",
        ],
        # Akamai blocks our datacenter egress; route through the residential
        # proxy (BROWSER_PROXY_* env) when one is configured.
        "proxy": True,
        "open_data": [
            {"note": "Individual releases are static: "
                     "PressReleasePage.aspx?PRID=<id> — fetchable via "
                     "web_fetch/Tavily even when the listing is blocked"},
        ],
        "last_verified": "2026-06-16",
    },
]


# ============================================================================
# Hot-reload cache. GCS is read at most once per _TTL seconds; admin edits to
# the GCS object are picked up within that window on every running instance
# (each Cloud Run instance keeps its own cache — convergence is eventual,
# bounded by _TTL). A save_playbooks() on the serving instance is reflected
# instantly there.
# ============================================================================

_TTL = float(os.environ.get("PLAYBOOKS_TTL_SECONDS", "60"))
_cache: dict[str, Any] = {"ts": 0.0, "data": None, "source": "default"}
_lock = asyncio.Lock()


def _domain(url: str) -> str:
    try:
        net = urlparse(url).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def _gcs_target() -> tuple[str, str] | None:
    bucket = os.environ.get("PLAYBOOKS_GCS_BUCKET")
    if not bucket:
        return None
    return bucket, os.environ.get("PLAYBOOKS_GCS_OBJECT", "config/playbooks.json")


def _load_from_gcs_sync() -> list[dict[str, Any]] | None:
    """Blocking GCS read — always called via asyncio.to_thread. Returns the
    parsed+validated list, or None to fall back to DEFAULT_PLAYBOOKS."""
    tgt = _gcs_target()
    if not tgt:
        return None
    bucket, obj = tgt
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(bucket).blob(obj)
        if not blob.exists():
            return None
        data = json.loads(blob.download_as_text())
        ok, err = validate_playbooks(data)
        if not ok:
            log.warning("playbooks gs://%s/%s invalid, using defaults: %s",
                        bucket, obj, err)
            return None
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("playbooks GCS load failed (%s), using defaults",
                    str(e)[:160])
        return None


def _save_to_gcs_sync(entries: list[dict[str, Any]]) -> None:
    """Blocking GCS write — always called via asyncio.to_thread."""
    tgt = _gcs_target()
    if not tgt:
        raise RuntimeError(
            "PLAYBOOKS_GCS_BUCKET is not set — cannot persist playbooks")
    bucket, obj = tgt
    from google.cloud import storage
    blob = storage.Client().bucket(bucket).blob(obj)
    blob.upload_from_string(
        json.dumps(entries, indent=2, ensure_ascii=False),
        content_type="application/json",
    )


def _merge_playbooks(defaults: list[dict[str, Any]],
                     overlay: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Effective playbooks = code `defaults` with the GCS `overlay` layered on
    top, keyed by id: the overlay overrides/extends per-id, default-only ids
    remain, overlay-only ids are appended. So a new code-default playbook
    auto-surfaces even when an overlay exists (no re-seed), while the overlay
    still wins for any id it defines. id-less overlay entries are kept."""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in defaults:
        eid = e.get("id")
        if eid is None:
            continue
        if eid not in by_id:
            order.append(eid)
        by_id[eid] = e
    extras: list[dict[str, Any]] = []
    for e in (overlay or []):
        eid = e.get("id")
        if eid is None:
            extras.append(e)
            continue
        if eid not in by_id:
            order.append(eid)
        by_id[eid] = e
    return [by_id[i] for i in order] + extras


async def get_playbooks(force: bool = False) -> list[dict[str, Any]]:
    """Return the effective playbook list, hot-reloading from GCS on TTL."""
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < _TTL:
        return _cache["data"]
    async with _lock:
        now = time.time()
        if (not force and _cache["data"] is not None
                and (now - _cache["ts"]) < _TTL):
            return _cache["data"]
        overlay = await asyncio.to_thread(_load_from_gcs_sync)
        if overlay is not None:
            _cache.update(ts=time.time(),
                          data=_merge_playbooks(DEFAULT_PLAYBOOKS, overlay),
                          source="gcs")
        else:
            _cache.update(ts=time.time(), data=list(DEFAULT_PLAYBOOKS),
                          source="default")
        return _cache["data"]


def current_source() -> str:
    return _cache.get("source", "default")


async def save_playbooks(entries: list[dict[str, Any]]) -> None:
    """Validate + persist to GCS + refresh the local cache immediately."""
    ok, err = validate_playbooks(entries)
    if not ok:
        raise ValueError(err)
    await asyncio.to_thread(_save_to_gcs_sync, entries)
    _cache.update(ts=time.time(), data=entries, source="gcs")


def validate_playbooks(obj: Any) -> tuple[bool, str]:
    """Structural validation for admin writes. Returns (ok, error_message)."""
    if not isinstance(obj, list):
        return False, "top level must be a list of playbook entries"
    seen_ids = set()
    for i, e in enumerate(obj):
        if not isinstance(e, dict):
            return False, f"entry {i} must be an object"
        eid = e.get("id")
        if eid is not None:
            if not isinstance(eid, str):
                return False, f"entry {i}: id must be a string"
            if eid in seen_ids:
                return False, f"duplicate id {eid!r}"
            seen_ids.add(eid)
        m = e.get("match")
        if (not isinstance(m, dict) or not isinstance(m.get("domain"), str)
                or not m["domain"]):
            return False, f"entry {i}: match.domain (non-empty string) required"
        for k in ("path_prefix", "path_regex"):
            if k in m and not isinstance(m[k], str):
                return False, f"entry {i}: match.{k} must be a string"
        if "path_regex" in m:
            try:
                re.compile(m["path_regex"])
            except re.error as ex:
                return False, f"entry {i}: invalid path_regex: {ex}"
        if "api" in e:
            api = e["api"]
            if not isinstance(api, list):
                return False, f"entry {i}: api must be a list"
            for j, rec in enumerate(api):
                if not isinstance(rec, dict):
                    return False, f"entry {i}: api[{j}] must be an object"
                if not isinstance(rec.get("endpoint"), str) or not rec["endpoint"]:
                    return False, (f"entry {i}: api[{j}].endpoint "
                                   "(non-empty string) required")
                if "method" in rec and not isinstance(rec["method"], str):
                    return False, f"entry {i}: api[{j}].method must be a string"
                if ("params" in rec and rec["params"] is not None
                        and not isinstance(rec["params"], dict)):
                    return False, (f"entry {i}: api[{j}].params must be an "
                                   "object or null")
        if "proxy" in e and not isinstance(e["proxy"], bool):
            return False, f"entry {i}: proxy must be a boolean"
        if not any(e.get(k) for k in
                   ("strategy", "avoid", "open_data", "act_steps", "api",
                    "proxy")):
            return False, (f"entry {i}: needs at least one of "
                           "strategy/avoid/open_data/act_steps/api/proxy")
    return True, ""


def match_playbook(playbooks: list[dict[str, Any]],
                   url: str) -> dict[str, Any] | None:
    """Most-specific match wins: a path_regex or longer path_prefix beats a
    bare-domain entry."""
    dom = _domain(url)
    if not dom:
        return None
    try:
        path = urlparse(url).path or "/"
    except Exception:
        path = "/"
    best: dict[str, Any] | None = None
    best_score = -1
    for e in playbooks:
        m = e.get("match", {})
        d = (m.get("domain") or "").lower()
        if not d or (dom != d and not dom.endswith("." + d)):
            continue
        score = 0
        pp = m.get("path_prefix")
        if pp:
            if not path.startswith(pp):
                continue
            score = len(pp)
        pr = m.get("path_regex")
        if pr:
            try:
                if not re.search(pr, path):
                    continue
            except re.error:
                continue
            score = max(score, 50)
        if score > best_score:
            best, best_score = e, score
    return best


async def match_for_url(url: str) -> dict[str, Any] | None:
    if not url:
        return None
    try:
        return match_playbook(await get_playbooks(), url)
    except Exception as e:  # noqa: BLE001
        log.debug("playbook match failed: %s", e)
        return None


def for_agent(entry: dict[str, Any]) -> dict[str, Any]:
    """The agent-facing projection injected into tool results."""
    return {k: entry[k] for k in
            ("id", "strategy", "avoid", "open_data", "act_steps", "api",
             "proxy", "last_verified")
            if k in entry}
