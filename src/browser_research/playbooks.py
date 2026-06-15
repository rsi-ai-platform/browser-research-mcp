"""Domain playbooks — per-site recipes the agent reads BEFORE attacking a known
hard page, so exploration becomes a lookup.

Source of truth, in order:
  1. GCS object (PLAYBOOKS_GCS_BUCKET / PLAYBOOKS_GCS_OBJECT) — editable WITHOUT
     a redeploy; hot-reloaded on a short TTL so admin edits take effect live.
  2. DEFAULT_PLAYBOOKS below — the in-repo seed / fallback when GCS is
     unconfigured or unreachable.

An entry:
  {
    "id": "ppac-consumption",
    "match": {"domain": "ppac.gov.in", "path_prefix": "/consumption"},
    "strategy": "...",          # 1-2 lines: how to actually get the data
    "avoid": ["...", ...],      # dead-ends WITH the reason (kills thrash loops)
    "open_data": [              # the escape hatch, as fetchable URLs
        {"tool": "download_file", "url"|"url_pattern": "...", "note": "..."}],
    "act_steps": [ {<single-key action>}, ... ],   # known-good interaction recipe
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
        "strategy": ("Do NOT drive the page UI. The year selector is a "
                     "non-native JS widget (select_option/click time out) and "
                     "the historical download is behind register+captcha. "
                     "Fetch the open mirrors instead."),
        "avoid": [
            "act/select on the year dropdown — it's a custom JS widget, not a "
            "native <select>; select_option will time out",
            "'Download Historical/Current Report' buttons — register + captcha "
            "gated (hard stop for automation)",
            "guessing ppac.gov.in/download.php?file=... paths blindly",
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
        data = await asyncio.to_thread(_load_from_gcs_sync)
        if data is not None:
            _cache.update(ts=time.time(), data=data, source="gcs")
        else:
            _cache.update(ts=time.time(), data=DEFAULT_PLAYBOOKS, source="default")
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
        if not any(e.get(k) for k in
                   ("strategy", "avoid", "open_data", "act_steps")):
            return False, (f"entry {i}: needs at least one of "
                           "strategy/avoid/open_data/act_steps")
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
            ("id", "strategy", "avoid", "open_data", "act_steps", "last_verified")
            if k in entry}
