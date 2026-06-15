# Multi-stage build for the browser-research MCP server.
#
# patchright's `install chromium` ships an entire Chromium build (~280 MB on
# disk) plus a handful of shared libs. We stage them in the builder layer and
# carry only the final layout into the runtime image so the published image
# stays around 700-800 MB.

# ---- Stage 1: build the Python wheel + install Chromium ---------------------
FROM python:3.12-slim AS builder
WORKDIR /app

# System deps Chromium needs at runtime. Most of these come from
# `patchright install --with-deps` but we list them explicitly so the
# image works on a stripped-down debian:slim without surprises.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    wget \
    xdg-utils \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

# Download Chromium into the Playwright cache. We pin the cache location so
# the runtime stage can copy it across without bringing all of /root.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN mkdir -p $PLAYWRIGHT_BROWSERS_PATH \
 && patchright install --with-deps chromium

# ---- Optional: Camoufox engine (BROWSER_ENGINE=camoufox) --------------------
# OFF by default to keep the image lean. To enable: uncomment this block AND the
# matching runtime block below, rebuild, and deploy with BROWSER_ENGINE=camoufox.
# Camoufox is a Firefox fork, so it pulls a one-time browser download (~150 MB).
#
# ENV CAMOUFOX_HOME=/opt/camoufox
# RUN uv pip install --system --no-cache ".[camoufox]" \
#  && mkdir -p $CAMOUFOX_HOME \
#  && HOME=$CAMOUFOX_HOME python -m camoufox fetch


# ---- Stage 2: slim runtime image -------------------------------------------
FROM python:3.12-slim AS runtime
WORKDIR /app

# Same system libs as the builder — runtime Chromium needs them.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r -u 1000 -ms /bin/bash app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    PORT=7862 \
    HEADLESS=true \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    # Trust X-Forwarded-* headers from any IP. Cloud Run terminates TLS at
    # its frontend and forwards the request internally; without this,
    # uvicorn sees a plain-HTTP connection from the proxy and won't honour
    # the original https scheme. Belt-and-suspenders with the FastMCP
    # transport_security relaxation in __main__.py — together they make
    # the streamable-http transport reachable through Cloud Run's edge.
    FORWARDED_ALLOW_IPS="*"

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/browser-research /usr/local/bin/browser-research
COPY --from=builder /opt/playwright-browsers /opt/playwright-browsers
RUN chown -R app:app /opt/playwright-browsers

# ---- Optional: Camoufox runtime deps (pairs with the builder block above) ---
# Uncomment to enable BROWSER_ENGINE=camoufox. Firefox needs a slightly
# different system-lib set than Chromium, plus xvfb for HEADLESS=virtual. The
# Python package rides along in site-packages (copied above); add native libs +
# the fetched browser. Runs before `USER app`, i.e. still as root.
#
# RUN apt-get update && apt-get install -y --no-install-recommends \
#       libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 xvfb \
#  && rm -rf /var/lib/apt/lists/*
# COPY --from=builder /opt/camoufox /opt/camoufox
# RUN chown -R app:app /opt/camoufox
# ENV HOME=/opt/camoufox

USER app
EXPOSE 7862
CMD ["browser-research"]
