# syntax=docker/dockerfile:1.7
FROM node:22.12.0-bookworm-slim@sha256:35531c52ce27b6575d69755c73e65d4468dba93a25644eed56dc12879cae9213

ARG OPENCODE_VERSION=1.18.1

ENV DEBIAN_FRONTEND=noninteractive \
    XDG_CONFIG_HOME=/etc/xdg \
    OPENCODE_CONFIG_DIR=/etc/xdg/opencode \
    OPENCODE_CONFIG=/etc/xdg/opencode/opencode.json \
    NPM_CONFIG_UPDATE_NOTIFIER=false

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    npm i -g "opencode-ai@${OPENCODE_VERSION}"; \
    opencode --version

RUN set -eux; \
    if id -u worker >/dev/null 2>&1; then \
      :; \
    elif id -u node >/dev/null 2>&1 && [ "$(id -u node)" = "1000" ]; then \
      groupmod -n worker node; \
      usermod -l worker -d /home/worker -m node; \
    else \
      useradd --uid 1000 --create-home --shell /bin/bash worker; \
    fi; \
    mkdir -p /work /etc/xdg/opencode; \
    chmod 0777 /work /etc/xdg/opencode

# Attempt vendored plugin wiring when docker/worker/vendor/wevibe-opencode-plugin is provided.
# If the vendored plugin is absent, image isolation remains intact; ON-cell plugin validation is
# handled separately by bench smoke (out of scope for this chunk).
COPY . /opt/docker-worker-context

RUN set -eux; \
    plugin_path=""; \
    if [ -f /opt/docker-worker-context/vendor/wevibe-opencode-plugin/package.json ]; then \
      npm i -g /opt/docker-worker-context/vendor/wevibe-opencode-plugin; \
      plugin_path="$(npm root -g)/wevibe-opencode-plugin/plugins/wevibe-plugin.ts"; \
      test -f "${plugin_path}"; \
    else \
      echo "NOTE: vendored wevibe-opencode-plugin missing; opencode plugin remains disabled in-image."; \
    fi; \
    WEVIBE_PLUGIN_PATH="${plugin_path}" node - <<'NODE'
const fs = require("node:fs")
const pluginPath = (process.env.WEVIBE_PLUGIN_PATH || "").trim()
const config = {
  $schema: "https://opencode.ai/config.json",
}
if (pluginPath) {
  config.plugin = [pluginPath]
  config.mcp = {
    wevibe: {
      "//": "disabled by design: use external WEVIBE_MCP_HTTP_URL, do not auto-spawn local MCP",
      enabled: false,
    },
  }
}
fs.mkdirSync("/etc/xdg/opencode", { recursive: true })
fs.writeFileSync("/etc/xdg/opencode/opencode.json", `${JSON.stringify(config, null, 2)}\n`, "utf8")
NODE

RUN set -eux; \
    chmod 0666 /etc/xdg/opencode/opencode.json; \
    rm -rf /opt/docker-worker-context

WORKDIR /work
USER worker
