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
| `ANTHROPIC_API_KEY` | for `extract` (not `visit`); also powers the `web_fetch` fallback | — |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-4-6` |
| `TAVILY_API_KEY` | no — enables the 1st fetch fallback when a CDN bot-blocks Chromium | — |
| `BROWSER_ENGINE` | no — `chromium` or `camoufox` (see below) | `chromium` |
| `BROWSER_CHANNEL` | no — e.g. `chrome` for a real Google Chrome binary (must be in the image) instead of bundled Chromium | — |
| `HEADLESS` | no | `true` (`false` headful; `virtual` = Xvfb, camoufox only) |
| `PLAYBOOKS_GCS_BUCKET` | no — GCS bucket for editable, hot-reloaded playbooks | — (uses in-repo defaults) |
| `PLAYBOOKS_GCS_OBJECT` | no — object key for the playbooks JSON | `config/playbooks.json` |
| `PLAYBOOKS_TTL_SECONDS` | no — hot-reload interval | `60` |
| `ADMIN_TOKEN` | no — gates the `/admin/playbooks` API (fail-closed if unset) | — |
| `MCP_TRANSPORT` | no | `stdio` |
| `MCP_HOST` / `PORT` | no | `0.0.0.0` / `7862` |

### Browser engines

`BROWSER_ENGINE` selects the engine launched by `_get_browser`:

- **`chromium`** (default) — patchright-patched Chromium. Set `BROWSER_CHANNEL=chrome`
  to drive a real Google Chrome binary (install it in the image) for a genuine
  Chrome TLS/version fingerprint; unset uses the bundled Chromium.
- **`camoufox`** (optional) — a Firefox fork with engine-level fingerprint
  spoofing. Stronger against fingerprint-based blocks, and unlike a headless-only
  engine it still renders + screenshots (so `extract`'s Sonnet-vision works). To
  enable: `pip install '.[camoufox]'` → `python -m camoufox fetch`, add Firefox's
  system libs (`playwright install-deps firefox`) to the image, then set
  `BROWSER_ENGINE=camoufox`. On a headless host use `HEADLESS=virtual` (needs
  `xvfb`) for best stealth, or `HEADLESS=true`.

Neither engine changes the **egress IP**, which is the dominant signal for
enterprise CDNs (Akamai et al.): a datacenter IP is denied before the fingerprint
is even evaluated. Pair either engine with a non-datacenter IP (residential proxy
/ self-hosted worker) to actually clear those — on Cloud Run alone they only help
fingerprint-gating sites.

### Fetch fallback chain

`visit` / `extract` (and a degraded mode of `act`) fetch with a real Chromium
first. When a CDN (Akamai, Cloudflare, Imperva) bot-blocks our egress IP and
returns a 200-OK "Access Denied" / JS-challenge page, the same URL is re-fetched
from different infrastructure, in order:

1. **Tavily Extract** — needs `TAVILY_API_KEY`; different egress IP, fast.
2. **Anthropic `web_fetch`** — server-side fetch via the Messages API; reuses
   `ANTHROPIC_API_KEY`. Server-rendered HTML + PDFs only (no JS).

Results carry a `source` field (`browser` / `tavily` / `anthropic_web_fetch`).
A page that stays blocked after all fallbacks is returned with a `blocked` flag
rather than being mistaken for empty content. `act` cannot replay interaction
steps through a static fallback, so when the live page is blocked it returns the
static fetch with a `degraded` note.

### Playbooks (per-domain recipes)

Hard sites get solved once, then the knowledge is cached as a **playbook** so the
agent never re-explores. When `visit`/`act`/`extract`/`download_file` hits a URL
matching a playbook, the result carries a `playbook` field — `strategy`, what to
`avoid` (with the reason), an `open_data` source to use instead, and/or the
known-good `act_steps`. The agent is told (in the server instructions) to follow
it before exploring. Two reactive flags compose with it: `blocked` (CDN bot-wall)
and `auth_wall` (login/registration gate) → both mean "stop driving the page,
use the playbook's open source."

**Source of truth:** the GCS object at `gs://$PLAYBOOKS_GCS_BUCKET/$PLAYBOOKS_GCS_OBJECT`
(hot-reloaded every `PLAYBOOKS_TTL_SECONDS`), falling back to the in-repo
`playbooks.py` defaults (seeded with PPAC + PIB) when GCS is unset/unreachable.
Editing the GCS object takes effect **without a redeploy**. The Cloud Run runtime
SA needs `roles/storage.objectAdmin` on the bucket.

**Admin API** (token-gated by `ADMIN_TOKEN`, fail-closed if unset; needs an HTTP
transport + a FastMCP build with `custom_route`):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/playbooks` | current playbooks + `source` (`gcs`/`default`) |
| `PUT` | `/admin/playbooks` | validate + save (body: `{"playbooks": [...]}` or `[...]`) |
| `POST` | `/admin/playbooks/validate` | validate without saving (live UI checks) |
| `GET` | `/admin/playbooks/match?url=…` | preview which playbook a URL hits |
| `POST` | `/admin/playbooks/reload` | force a cache refresh from GCS |

All require header `X-Admin-Token: $ADMIN_TOKEN`. **Recommended:** have your
platform's admin-settings UI call these via its own backend (server-to-server)
so the token never reaches the browser and no CORS is needed. (The left-sidebar
admin UI itself lives in the platform repo, not here.)

## Stack

| Layer | Library |
|---|---|
| Browser engine | `patchright` (patched Playwright) |
| Structured extraction | Anthropic Claude Sonnet 4.6, with vision input |
| MCP transport | `mcp[server]` FastMCP — stdio / SSE / streamable-http |
| Session isolation | One Playwright context per MCP `client_id` |
