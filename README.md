# browser-research-mcp

Browser-based research as an MCP server. Drives a real Chromium via
**patched Playwright** (`patchright`) so the agent can read JavaScript-rendered
tables, dynamic charts, login-walled dashboards, and AJAX dropdowns that
the cheaper rungs of the fetch ladder can't reach.

This is the **last rung** of the ladder:

```
web_search → web_fetch → pdf_fetch → http_post_form → browser-research
```

## Tools

| Tool | Purpose |
|---|---|
| `visit(url, …)` | Open a URL with Chromium, return DOM text + screenshot. Cheap, no LLM call. |
| `extract(url, focus, …)` | `visit` + Sonnet structured extraction. Same response shape as `pdf_fetch_structured`. Sends the screenshot to Sonnet so chart values drawn via canvas/SVG get picked up. |

## Why patchright

[patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) is a drop-in
Apache-2.0 patched Playwright that disables the `AutomationControlled` blink
feature, removes `Runtime.enable` leaks, and a few other detection vectors.
Indian government dashboards (PPAC, RBI, MoSPI, SEBI) work fine with this
without paying for residential proxies or a hosted browser SaaS.

## Run locally

```bash
uv tool install browser-research-mcp --python 3.12
# Install Chromium for patchright (one-off):
uv tool run patchright install chromium

# stdio (Claude Desktop / Cursor / desktop clients):
ANTHROPIC_API_KEY=… uvx browser-research

# HTTP (the platform backend):
ANTHROPIC_API_KEY=… uvx browser-research --transport streamable-http --port 7862
```

## Environment

| Var | Required | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | for `extract` (not `visit`) | — |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-4-6` |
| `HEADLESS` | no | `true` (`false` to debug locally) |
| `MCP_TRANSPORT` | no | `stdio` |
| `MCP_HOST` / `PORT` | no | `0.0.0.0` / `7862` |

## Stack

| Layer | Library |
|---|---|
| Browser engine | `patchright` (patched Playwright) |
| Structured extraction | Anthropic Claude Sonnet 4.6, with vision input |
| MCP transport | `mcp[server]` FastMCP — stdio / SSE / streamable-http |
| Session isolation | One Playwright context per MCP `client_id` |
