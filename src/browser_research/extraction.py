"""Shared structured-extraction prompts. Lifted verbatim from
authority-web-search-mcp so the agent gets identical-shape responses
regardless of which MCP it called.

The prompt is split into a static block (cacheable via Anthropic's
ephemeral cache_control) and a tiny dynamic block carrying today's date.
"""
from __future__ import annotations


STRUCTURED_EXTRACT_SYSTEM_STATIC = (
    "INDIAN FISCAL-YEAR TABLES — read month columns correctly. When a "
    "table or page is labelled with an Indian fiscal year — '2025-2026', "
    "'2025-26', 'FY26', 'FY 2025-26', 'FY2025-26' — its month columns run "
    "from APRIL of the FIRST year through MARCH of the SECOND year. Map "
    "APRIL, MAY, …, DECEMBER to the FIRST calendar year, and JANUARY, "
    "FEBRUARY, MARCH to the SECOND. Example: in a '2025-2026' table the "
    "APRIL column is April 2025 (NOT April 2026) and the MARCH column is "
    "March 2026. The leftmost month column is the EARLIEST month, not the "
    "most recent. Always emit the period with the correct YEAR — e.g. "
    "'Apr 2025', never a bare 'April'.\n\n"
    "You extract structured facts from web pages — including ones whose "
    "data is rendered client-side (charts, JS-populated tables, AJAX "
    "dropdowns). You may receive a SCREENSHOT alongside the text; if you "
    "see numbers in the screenshot that aren't in the text (because they "
    "were drawn via canvas / SVG), use them. Given the page TEXT and an "
    "extraction FOCUS, return a JSON object exactly matching:\n"
    "{\n"
    '  "title": "<page title or main heading>",\n'
    '  "dateline": "<publication date if present, ISO YYYY-MM-DD>",\n'
    '  "summary": "<2-3 sentences of the page\'s thesis>",\n'
    '  "key_facts": [\n'
    '    {"claim": "<plain-English fact>", "value": "<number or category>", '
    '"unit": "<%, bps, INR cr, etc>", "period": "<when>", "confidence": "high"|"medium"|"low"}\n'
    "  ],\n"
    '  "numeric_values": [{"name": "...", "value": "...", "unit": "...", "period": "..."}],\n'
    '  "dates": ["YYYY-MM-DD", ...],\n'
    '  "tables_summary": ["<one-line summary per table>"]\n'
    "}\n"
    "Return ONLY the JSON. No prose, no fences. If a field is unknown, "
    "use empty string or empty list — never guess."
)


def dynamic_date_block(today_iso: str) -> str:
    return (
        f"Today's date is {today_iso} (UTC). Anchor every temporal phrase "
        f"on the page against this date — do NOT rely on your training "
        f"cutoff."
    )
