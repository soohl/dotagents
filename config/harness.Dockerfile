FROM node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash git ripgrep ca-certificates curl build-essential python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY config/harness-package.json ./package.json
COPY config/harness-package-lock.json ./package-lock.json
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN npm ci --omit=dev --no-audit --no-fund \
    && node node_modules/playwright/cli.js install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /data && chown node:node /data
COPY src/harness_runtime/ /app/runtime/
COPY config/harness-policy.json /app/policy.json
RUN node /app/runtime/patch-browser.mjs
RUN node /app/runtime/patch-models.mjs
RUN node /app/runtime/patch-session-model.mjs
COPY config/harness-browser.json /app/browser.json
COPY config/harness-chat-browser.json /app/chat-browser.json
COPY src/harness_runtime/browser-init.js /app/browser-init.js
ENV HOME=/data DSH_HOME=/data/harness PATH=/app/node_modules/.bin:$PATH
WORKDIR /workspace
USER 1000:1000
ENTRYPOINT ["node", "/app/runtime/entry.mjs"]
