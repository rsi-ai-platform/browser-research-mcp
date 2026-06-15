"""The research methodology, encoded so the server nudges the agent to behave
the same way a careful operator does on ANY page — simple or hard, gov dashboard
or SPA: classify the page, take the cheapest rung that works, and escalate on a
SPECIFIC signal rather than retrying what just failed.

Three things consume this module:
  * `RESEARCH_STRATEGY` — the decision procedure as structured data, returned
    verbatim by the `strategy` MCP tool.
  * `STRATEGY_INSTRUCTIONS` — a compact prose form folded into the server's
    always-loaded instructions.
  * `diagnose_next(result, tool)` — given a tool result, name the recommended
    next move based on the signals present (blocked / auth_wall / file_links /
    observed_api / recovery_hint / sparse DOM). Attached to every browser-tool
    result as `next_step`, so the adaptiveness rides along with the data instead
    of living only in a doc the agent might not read.
"""
from __future__ import annotations

from typing import Any

# ============================================================================
# The decision procedure. Cheapest, most robust rung first; escalate on signal.
# ============================================================================

RESEARCH_STRATEGY: dict[str, Any] = {
    "summary": (
        "Classify the page, take the cheapest rung that works, and escalate "
        "on a specific signal — never retry the move that just failed."
    ),
    "ladder": [
        {"rung": 1, "name": "static fetch",
         "when": "Default. Server-rendered HTML/PDF — the data is in the markup.",
         "tool": "(upstream) web_fetch / pdf_fetch / http_post_form",
         "next_if_fails": "Page is a JS shell or login-walled → rung 2."},
        {"rung": 2, "name": "render",
         "when": "Static fetch returned a shell, a spinner, or 'enable "
                 "JavaScript'. SPA / canvas chart / JS-populated table.",
         "tool": "visit",
         "next_if_fails": "Data is behind a click/dropdown → rung 3; behind a "
                          "file link → rung 5; behind a wall → rung 6."},
        {"rung": 3, "name": "drive the control",
         "when": "Data appears only after an interaction (dropdown, tab, "
                 "'load more', form submit) and the URL doesn't change.",
         "tool": "act",
         "next_if_fails": "A select/click TIMES OUT → the control is a "
                          "non-native JS widget; do NOT keep clicking — go to "
                          "rung 4."},
        {"rung": 4, "name": "discover + replay the API",
         "when": "The widget is brittle, OR you want periods/filters the UI "
                 "never exposes. The table is really fed by an AJAX endpoint.",
         "tool": "inspect_network → call_api",
         "next_if_fails": "Endpoint needs auth you don't have → rung 6's pivot."},
        {"rung": 5, "name": "download the file",
         "when": "The real numbers are in a .xlsx/.xls/.csv/.pdf attachment "
                 "(surfaced as file_links). Common on gov sites.",
         "tool": "download_file",
         "next_if_fails": "HTTP error / html_masquerade → the link is stale; "
                          "pick another file_link or the playbook open_data."},
        {"rung": 6, "name": "pivot around the wall",
         "when": "CDN bot-wall (blocked) or login/registration gate "
                 "(auth_wall). No amount of driving the page gets past it.",
         "tool": "fallback fetch (auto) for bot-walls; playbook.open_data for "
                 "auth gates",
         "next_if_fails": "Still blocked from a datacenter IP → needs a "
                          "non-datacenter egress; surface that to the caller."},
        {"rung": 7, "name": "cache the win",
         "when": "You solved a hard site. Don't make the next run re-explore.",
         "tool": "record a playbook entry (strategy / avoid / api / open_data)",
         "next_if_fails": ""},
    ],
    "signals": [
        {"signal": "loaded page but the target table/value is empty",
         "means": "client-rendered after load",
         "do": "visit with wait_for_selector; if still empty, inspect_network "
               "then call_api"},
        {"signal": "act select/click raised a Timeout",
         "means": "non-native JS widget — not a real <select>",
         "do": "stop driving it; read observed_api / recovery_hint and call_api "
               "the endpoint"},
        {"signal": "blocked",
         "means": "CDN bot-wall (200-OK deny/challenge body)",
         "do": "fallback fetch already ran; if still no content use "
               "playbook.open_data or a non-datacenter IP"},
        {"signal": "auth_wall",
         "means": "data/download gated behind login or registration",
         "do": "do NOT try to log in; pivot to playbook.open_data (open mirror "
               "/ data.gov.in / source PDF)"},
        {"signal": "file_links present",
         "means": "the numbers live in attachments, not the DOM",
         "do": "download_file the entry whose anchor text matches your period; "
               "never visit a file URL"},
        {"signal": "observed_api / recovery_hint present",
         "means": "the page exposed a data endpoint",
         "do": "call_api it, templating params for the exact period/filter you "
               "need"},
        {"signal": "a playbook field is attached",
         "means": "this site was solved before",
         "do": "follow it before any exploration"},
    ],
    "principles": [
        "Look before you assert — inspect/screenshot the actual state; don't "
        "answer from assumptions about the site.",
        "Probe before you build — confirm the real response shape / param names "
        "on a KNOWN-GOOD case before trusting them for an unknown one.",
        "Prefer the most robust layer once found: API JSON > DOM scrape > "
        "screenshot OCR.",
        "Escalate on the signal, don't thrash — one failed rung tells you which "
        "rung is next; repeating the same call is the anti-pattern.",
        "Anchor time explicitly — call today(); read Indian fiscal-year columns "
        "April→March.",
        "Verify before trusting — cross-check totals, reconcile against a second "
        "source or the page's own total row.",
        "Cache the win — turn a solved hard site into a playbook so it never "
        "gets re-explored.",
    ],
}


STRATEGY_INSTRUCTIONS = (
    "APPROACH — how to attack ANY page (simple or hard). Classify it, take the "
    "cheapest rung that works, escalate on a SPECIFIC signal, never retry the "
    "move that just failed:\n"
    "  1. static fetch (upstream web_fetch/pdf_fetch) — server-rendered HTML/PDF.\n"
    "  2. visit — JS shell / SPA / canvas chart / JS-populated table.\n"
    "  3. act — data behind a click/dropdown/tab/submit (URL doesn't change).\n"
    "  4. inspect_network → call_api — the control is a brittle JS widget OR you "
    "want periods the UI never lists. The table is fed by an AJAX endpoint; "
    "discover it, then replay it.\n"
    "  5. download_file — the numbers are in a .xlsx/.csv/.pdf (file_links).\n"
    "  6. pivot — `blocked` (CDN wall, fallback auto-runs) or `auth_wall` "
    "(login gate → use playbook.open_data). Don't keep driving the page.\n"
    "Each tool result may carry a `next_step` advisor naming the recommended "
    "move from the signals it saw — and a `playbook` (follow it first). Probe "
    "before you build (verify params on a known period first); prefer API JSON "
    "over DOM over OCR; verify totals before trusting them. Call `strategy` for "
    "the full ladder + signal table."
)


# ============================================================================
# diagnose_next — the active advisor. Pure function over a tool result dict;
# returns a {recommendation, based_on, tools} hint, or None when nothing about
# the result calls for a particular next move.
# ============================================================================

# Below this much visible text (and no file links), a successfully-loaded page
# is almost certainly a client-rendered shell whose data hasn't populated.
_SPARSE_DOM_CHARS = 400


def diagnose_next(result: dict[str, Any], tool: str) -> dict[str, Any] | None:
    """Recommend the next rung from the signals in `result`. Priority order
    matches the cost of being wrong: walls first (nothing else will work), then
    surfaced endpoints/files, then the SPA-shell fallback."""
    if not isinstance(result, dict):
        return None

    # A hard error already carries its own explanation; don't double up unless
    # it's a wall (handled below via flags, not the error string).
    if result.get("auth_wall"):
        return {
            "recommendation": (
                "Login/registration gate — do not attempt to authenticate. "
                "Pivot to an open source (the matched playbook's open_data, "
                "data.gov.in, or the underlying source PDF)."),
            "based_on": "auth_wall",
            "tools": ["download_file", "strategy"],
        }
    if result.get("blocked"):
        return {
            "recommendation": (
                "CDN bot-wall: the fetch-fallback chain already re-tried from "
                "other infra. If there's still no content, use the playbook's "
                "open_data source or route through a non-datacenter IP — "
                "re-driving this page won't help."),
            "based_on": "blocked",
            "tools": ["download_file", "strategy"],
        }
    if result.get("recovery_hint") or result.get("observed_api"):
        return {
            "recommendation": (
                "The page exposed a data endpoint (see recovery_hint / "
                "observed_api). Replay it with call_api, templating the params "
                "for the exact period/filter you need — this also reaches "
                "values the widget never lists. Faster and more robust than "
                "re-driving the UI."),
            "based_on": ("recovery_hint" if result.get("recovery_hint")
                         else "observed_api"),
            "tools": ["call_api"],
        }
    if result.get("file_links"):
        n = len(result["file_links"])
        return {
            "recommendation": (
                f"{n} downloadable file link(s) found — gov data often lives "
                "ONLY in attachments. download_file the entry whose anchor text "
                "matches your target period; never visit a file URL."),
            "based_on": "file_links",
            "tools": ["download_file"],
        }
    # SPA-shell heuristic: a page that loaded fine but has almost no visible
    # text is rendering client-side and hasn't populated the data yet.
    if tool in ("visit", "extract") and not result.get("error"):
        text = result.get("text")
        summary = result.get("summary")  # extract() shape
        body_len = len(text) if isinstance(text, str) else (
            len(summary) if isinstance(summary, str) else None)
        if body_len is not None and body_len < _SPARSE_DOM_CHARS:
            return {
                "recommendation": (
                    "Very little rendered text — likely a client-rendered shell "
                    "whose data hasn't loaded. Retry visit with a "
                    "wait_for_selector on the data element; if it's behind a "
                    "control use act; if a dropdown feeds it, inspect_network "
                    "then call_api."),
                "based_on": "sparse_dom",
                "tools": ["visit", "act", "inspect_network"],
            }
    return None
