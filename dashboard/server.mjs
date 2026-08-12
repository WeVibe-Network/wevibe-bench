#!/usr/bin/env node
// ─────────────────────────────────────────────────────────────────────────────
// WEVIBE BENCH DASHBOARD — SERVER
//
//   node server.mjs                 # http://127.0.0.1:7717
//   node server.mjs --port 8080 --runs ../runs
//
// ZERO DEPENDENCIES. Node stdlib only. No build step, no npm install.
//
// SAFETY PROPERTIES (deliberate, do not weaken):
//   - READ-ONLY. Nothing here opens a file for write.
//   - Binds 127.0.0.1 by default. --host must be passed explicitly to expose it.
//   - Every source module runs isolated with a 2s timeout; a module that throws
//     or hangs is reported `unwired` and the board renders without it.
//   - Log reads are tail-bounded (256KB), so a multi-hour run costs the same as
//     a fresh one.
//   - Poll results are cached; concurrent requests share one in-flight refresh.
//   - Serves exactly three routes and no filesystem path traversal is possible:
//     the static file map is a fixed allowlist.
// ─────────────────────────────────────────────────────────────────────────────

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { emptyBoard } from "./contract.mjs";
import { runSource, mergePatch } from "./sources/_runtime.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

// ── args ─────────────────────────────────────────────────────────────────────

function parseArgs(argv) {
  // HOST DEFAULT, and why it is env-derived:
  // On the host, binding 127.0.0.1 is the safe default — the board is not
  // exposed to the network unless someone decides it should be.
  // INSIDE A CONTAINER that default is wrong in a way that looks like a bug:
  // a process bound to the container's loopback is unreachable from a
  // published port, so `-p 7717:7717` would silently serve nothing. The
  // container image sets WEVIBE_DASH_HOST=0.0.0.0 explicitly, which is safe
  // there precisely because the container's network namespace IS the boundary
  // and publishing is still opt-in at `docker run`.
  const out = {
    port: 7717,
    host: process.env.WEVIBE_DASH_HOST ?? "127.0.0.1",
    runs: null,
    config: null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--port") { out.port = Number(argv[++i]); out.portExplicit = true; }
    else if (a === "--host") out.host = String(argv[++i]);
    else if (a === "--runs") out.runs = String(argv[++i]);
    else if (a === "--config") out.config = String(argv[++i]);
    else if (a === "--help" || a === "-h") out.help = true;
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));

if (args.help) {
  console.log(`
wevibe bench dashboard

  node server.mjs [options]

  --port <n>        default 7717
  --host <addr>     default 127.0.0.1 (pass 0.0.0.0 to expose deliberately)
  --runs <dir>      runs root (default: ../runs relative to this file)
  --config <file>   default: ./dashboard.config.json
`);
  process.exit(0);
}

// ── config ───────────────────────────────────────────────────────────────────

const DEFAULT_CONFIG = {
  benchRoot: resolve(HERE, ".."),
  runsRoot: null, // derived from benchRoot when null
  pollMs: 2000,
  opencodeServeUrl: "http://127.0.0.1:4096",
  // The host-side control plane. Opt-in like every other network source: the
  // board must come up with nothing else running.
  //
  // TWO URLs, DELIBERATELY. `controlUrl` is how THIS PROCESS reaches the
  // control plane; `controlPublicUrl` is what the BROWSER is told to POST to.
  // In the container these are different hosts — the server crosses the
  // container boundary via host.docker.internal, while the browser is already
  // on the host and must use a host address. Publishing the server's own URL
  // to the browser would make every write fail with a connection error that
  // looks like the control plane is down when it is running fine.
  controlUrl: "http://127.0.0.1:7718",
  controlPublicUrl: null, // defaults to controlUrl when not set
  sources: {
    "run-manifest": true,
    "status-stream": true,
    "run-log": true,
    truncation: true,
    "funnel-cells": true,
    "plugin-log": true,
    "opencode-serve": true,
    "control-plane": false, // needs the control service — opt in explicitly
    "hub-db": false, // needs a reachable postgres — opt in explicitly
  },
  hubDb: {
    enabled: false,
    host: "wevibe-postgres",
    port: 5432,
    user: "wevibe",
    database: "wevibe_hub",
  },
};

/**
 * Env overrides. These exist so the CONTAINER can be reconfigured without a
 * rebuild and without baking a config file into the image — `docker run -e …`
 * or a compose `environment:` block is the whole interface.
 *
 * Precedence: env > config file > defaults.
 */
function applyEnv(cfg) {
  const env = process.env;
  if (env.WEVIBE_DASH_BENCH_ROOT) cfg.benchRoot = env.WEVIBE_DASH_BENCH_ROOT;
  if (env.WEVIBE_DASH_RUNS_ROOT) cfg.runsRoot = env.WEVIBE_DASH_RUNS_ROOT;
  if (env.WEVIBE_DASH_PORT) cfg.port = Number(env.WEVIBE_DASH_PORT);
  if (env.WEVIBE_DASH_POLL_MS) cfg.pollMs = Number(env.WEVIBE_DASH_POLL_MS);
  if (env.WEVIBE_DASH_OPENCODE_URL) cfg.opencodeServeUrl = env.WEVIBE_DASH_OPENCODE_URL;
  if (env.WEVIBE_DASH_CONTROL_URL) cfg.controlUrl = env.WEVIBE_DASH_CONTROL_URL;
  if (env.WEVIBE_DASH_CONTROL_PUBLIC_URL) cfg.controlPublicUrl = env.WEVIBE_DASH_CONTROL_PUBLIC_URL;

  // Per-source toggles: WEVIBE_DASH_SOURCE_HUB_DB=1, ..._OPENCODE_SERVE=0, etc.
  for (const name of Object.keys(cfg.sources)) {
    const key = `WEVIBE_DASH_SOURCE_${name.replace(/-/g, "_").toUpperCase()}`;
    if (env[key] !== undefined) cfg.sources[name] = /^(1|true|on|yes)$/i.test(env[key]);
  }

  if (env.WEVIBE_DASH_HUBDB === "1") cfg.hubDb.enabled = true;
  if (env.WEVIBE_HUB_DB_HOST) cfg.hubDb.host = env.WEVIBE_HUB_DB_HOST;
  if (env.WEVIBE_HUB_DB_PORT) cfg.hubDb.port = Number(env.WEVIBE_HUB_DB_PORT);
  if (env.WEVIBE_HUB_DB_USER) cfg.hubDb.user = env.WEVIBE_HUB_DB_USER;
  if (env.WEVIBE_HUB_DB_NAME) cfg.hubDb.database = env.WEVIBE_HUB_DB_NAME;
  // Password is read from the environment at query time and is never stored in
  // config, never logged, and never returned by /api/health.
  if (cfg.hubDb.enabled) cfg.sources["hub-db"] = true;
  return cfg;
}

async function loadConfig() {
  const path = args.config ? resolve(args.config) : join(HERE, "dashboard.config.json");
  let user = {};
  try {
    user = JSON.parse(await readFile(path, "utf8"));
  } catch {
    // absent config is the normal out-of-the-box case
  }
  const cfg = applyEnv({
    ...DEFAULT_CONFIG,
    ...user,
    sources: { ...DEFAULT_CONFIG.sources, ...(user.sources ?? {}) },
    hubDb: { ...DEFAULT_CONFIG.hubDb, ...(user.hubDb ?? {}) },
  });
  cfg.benchRoot = resolve(cfg.benchRoot);
  cfg.runsRoot = resolve(args.runs ?? cfg.runsRoot ?? join(cfg.benchRoot, "runs"));
  return cfg;
}

// ── source registry ──────────────────────────────────────────────────────────

const MODULE_FILES = {
  "run-manifest": "./sources/run-manifest.mjs",
  "status-stream": "./sources/status-stream.mjs",
  "run-log": "./sources/run-log.mjs",
  truncation: "./sources/truncation.mjs",
  "funnel-cells": "./sources/funnel-cells.mjs",
  "plugin-log": "./sources/plugin-log.mjs",
  "opencode-serve": "./sources/opencode-serve.mjs",
  "control-plane": "./sources/control-plane.mjs",
  "hub-db": "./sources/hub-db.mjs",
};

/**
 * Load enabled modules. A module that fails to IMPORT is reported as unwired
 * rather than crashing the server — this is what makes the source directory
 * genuinely pluggable: a broken drop-in degrades to a null panel.
 */
async function loadModules(cfg) {
  const mods = [];
  const broken = [];
  for (const [name, enabled] of Object.entries(cfg.sources)) {
    if (!enabled) continue;
    const file = MODULE_FILES[name];
    if (!file) {
      broken.push({ id: name, reason: "unknown source id" });
      continue;
    }
    try {
      mods.push(await import(file));
    } catch (err) {
      broken.push({ id: name, reason: `import failed: ${String(err?.message ?? err).slice(0, 160)}` });
    }
  }
  return { mods, broken };
}

// ── board assembly ───────────────────────────────────────────────────────────

/**
 * MERGE ORDER MATTERS. Later modules win on conflict, and null never
 * overwrites a value (see mergePatch). Ordering rationale:
 *   run-manifest   provenance floor
 *   status-stream  AUTHORITATIVE for gates/arm/verdict (RC-5)
 *   run-log        live pulse, refines phase/turns between attempt records
 *   opencode-serve freshest token counters, last word on liveness
 */
const ORDER = [
  "run-manifest",
  "status-stream",
  "truncation",
  "funnel-cells",
  "plugin-log",
  "hub-db",
  "run-log",
  "opencode-serve",
];

async function buildBoard(cfg, mods, broken) {
  const board = emptyBoard();
  const ctx = { benchRoot: cfg.benchRoot, runsRoot: cfg.runsRoot, config: cfg };

  const ordered = [...mods].sort(
    (a, b) => ORDER.indexOf(a.id) - ORDER.indexOf(b.id),
  );

  const results = [];
  for (const mod of ordered) {
    const r = await runSource(mod, ctx);
    results.push(r);
    if (r.ok) mergePatch(board, r.patch);
  }

  board.sources = [
    ...results.map((r) => ({
      id: r.id,
      ok: r.ok,
      fields: r.fields,
      reason: r.reason,
      provenance: r.provenance,
      ms: r.ms,
    })),
    ...broken.map((b) => ({ id: b.id, ok: false, fields: [], reason: b.reason, provenance: null, ms: 0 })),
  ];

  board.generated_at = Date.now();
  return board;
}

// ── poll cache ───────────────────────────────────────────────────────────────

let cached = null;
let cachedAt = 0;
let inFlight = null;

async function getBoard(cfg, mods, broken) {
  const age = Date.now() - cachedAt;
  if (cached && age < cfg.pollMs) return cached;
  if (inFlight) return inFlight; // share one refresh across concurrent clients
  inFlight = buildBoard(cfg, mods, broken)
    .then((b) => {
      cached = b;
      cachedAt = Date.now();
      return b;
    })
    .catch((err) => {
      // Even total assembly failure must render something honest.
      const b = emptyBoard();
      b.sources = [{ id: "server", ok: false, fields: [], reason: String(err?.message ?? err), provenance: null, ms: 0 }];
      return b;
    })
    .finally(() => {
      inFlight = null;
    });
  return inFlight;
}

// ── http ─────────────────────────────────────────────────────────────────────

// Fixed allowlist — there is no dynamic path resolution anywhere in this
// server, so directory traversal is impossible by construction rather than by
// sanitising user input.
const STATIC = {
  "/": { file: "index.html", type: "text/html; charset=utf-8" },
  "/index.html": { file: "index.html", type: "text/html; charset=utf-8" },
  "/board.js": { file: "board.js", type: "text/javascript; charset=utf-8" },
  "/panels/chrome.js": { file: "panels/chrome.js", type: "text/javascript; charset=utf-8" },
  "/panels/wall.js": { file: "panels/wall.js", type: "text/javascript; charset=utf-8" },
  "/panels/hero.js": { file: "panels/hero.js", type: "text/javascript; charset=utf-8" },
  "/panels/recall.js": { file: "panels/recall.js", type: "text/javascript; charset=utf-8" },
  "/panels/ticker.js": { file: "panels/ticker.js", type: "text/javascript; charset=utf-8" },
  "/panels/rail.js": { file: "panels/rail.js", type: "text/javascript; charset=utf-8" },
};

const main = async () => {
  const cfg = await loadConfig();
  const { mods, broken } = await loadModules(cfg);

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");

    if (req.method !== "GET") {
      res.writeHead(405, { "content-type": "text/plain" }).end("read-only");
      return;
    }

    if (url.pathname === "/api/board") {
      try {
        const board = await getBoard(cfg, mods, broken);
        const body = JSON.stringify(board);
        res.writeHead(200, {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
          "content-length": Buffer.byteLength(body),
        });
        res.end(body);
      } catch (err) {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: String(err?.message ?? err) }));
      }
      return;
    }

    if (url.pathname === "/api/health") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          ok: true,
          benchRoot: cfg.benchRoot,
          runsRoot: cfg.runsRoot,
          sources: mods.map((m) => m.id),
          disabled: Object.entries(cfg.sources).filter(([, v]) => !v).map(([k]) => k),
        }),
      );
      return;
    }

    const entry = STATIC[url.pathname];
    if (entry) {
      try {
        const body = await readFile(join(HERE, entry.file));
        res.writeHead(200, { "content-type": entry.type, "cache-control": "no-store" });
        res.end(body);
      } catch {
        res.writeHead(404, { "content-type": "text/plain" }).end("not found");
      }
      return;
    }

    res.writeHead(404, { "content-type": "text/plain" }).end("not found");
  });

  // Precedence: explicit CLI flag > env/config > default. An earlier revision
  // had `cfg.port ?? args.port`, which let an env var silently win over a flag
  // the operator typed — the opposite of what a flag means.
  const port = args.portExplicit ? args.port : (cfg.port ?? args.port);
  server.listen(port, args.host, () => {
    const port = server.address().port;
    console.log(`wevibe bench dashboard → http://${args.host}:${port}`);
    console.log(`  bench root : ${cfg.benchRoot}`);
    console.log(`  runs root  : ${cfg.runsRoot}`);
    console.log(`  sources    : ${mods.map((m) => m.id).join(", ") || "(none)"}`);
    if (broken.length) console.log(`  unwired    : ${broken.map((b) => b.id).join(", ")}`);
  });
};

main();
