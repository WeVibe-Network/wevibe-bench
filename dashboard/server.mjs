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
      "stack-ledger": true,
      truncation: true,
    "funnel-cells": true,
    "plugin-log": true,
    "opencode-serve": true,
    // Reads the harness's extraction telemetry DB off the read-only bench
    // mount. On by default: it is a local file read with no network and no
    // service dependency, and its absence before the first extraction is a
    // designed state, not a failure.
    "extraction-inventory": true,
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
  "stack-ledger": "./sources/stack-ledger.mjs",
  truncation: "./sources/truncation.mjs",
  "funnel-cells": "./sources/funnel-cells.mjs",
  "plugin-log": "./sources/plugin-log.mjs",
  "opencode-serve": "./sources/opencode-serve.mjs",
  "control-plane": "./sources/control-plane.mjs",
  "extraction-inventory": "./sources/extraction-inventory.mjs",
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
  "extraction-inventory",
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

// ── the live stream ──────────────────────────────────────────────────────────
//
// ── WHY SERVER-SENT EVENTS AND NOT A WEBSOCKET ──────────────────────────────
//
// The ask was "React consumed by events emitted by a web socket". The push
// semantics are what matter and are delivered here; the wire protocol is SSE,
// deliberately, for three reasons that are properties of THIS server:
//
//   1. NODE HAS NO STDLIB WEBSOCKET SERVER. `globalThis.WebSocket` is a CLIENT
//      only. Serving RFC6455 means either a dependency (`ws`) or hand-rolling
//      frame masking, continuation frames, ping/pong and close handshakes. Both
//      break "no dependencies, no build step" — the stated invariant this image
//      is built on (README, Dockerfile: there is no package.json by design).
//   2. A WEBSOCKET IS BIDIRECTIONAL, AND THIS SERVER IS READ-ONLY BY
//      CONSTRUCTION. Every non-GET is 405 (see below) and the bench mount is
//      `:ro`. Opening a duplex channel would hand the browser a write path into
//      a process whose entire safety argument is that it has none. SSE is a
//      GET that never closes — the read-only property is preserved verbatim.
//   3. IT IS ALREADY THE HOUSE IDIOM. The control plane consumes the worker's
//      `opencode serve` over SSE (control/events.mjs), and control/sse-probe.mjs
//      is a live socket test for exactly this framing. One streaming protocol in
//      the tree, not two.
//
// EventSource additionally gives automatic reconnection with backoff, which a
// hand-rolled WebSocket client would have to reimplement — and reconnection is
// the single most important behaviour for a board that must survive a control
// plane restart mid-run without going dark.
//
// ── WHAT IS PUSHED ──────────────────────────────────────────────────────────
//
// `board`  the full payload, MINUS the event ring. Sent on connect and whenever
//          the assembled board changes.
// `events` ONLY rows newer than the client's cursor.
//
// This is the fix for the measured defect: /api/board was 240KB every 2s and
// 82% of it was 400 event rows that had not changed. Measured against the live
// control plane: `?limit=400` = 199KB, `?since=<cursor>` = 493 bytes.
const streamClients = new Set();

/**
 * Broadcast a named SSE frame to every attached client.
 *
 * A client whose socket has gone away is dropped rather than written to — a
 * dead client must never be able to wedge the broadcast loop.
 */
function broadcast(event, data) {
  const frame = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of [...streamClients]) {
    try {
      if (res.writableEnded) streamClients.delete(res);
      else res.write(frame);
    } catch {
      streamClients.delete(res);
    }
  }
}

/**
 * The board minus the event ring.
 *
 * Events are streamed separately and incrementally, so shipping them inside the
 * board payload too would reintroduce exactly the redundancy this exists to
 * remove. The ring's METADATA (counts, connected, cursor, grading) is kept —
 * it is small, it changes meaningfully, and the feed header renders from it.
 */
export function boardWithoutEvents(board) {
  const { events, ...rest } = board;
  if (!events) return { ...rest, events: null };
  const { events: _rows, ...meta } = events;
  return { ...rest, events: { ...meta, events: [] } };
}

/**
 * A stable digest of the board, used to decide whether a push is warranted.
 *
 * `generated_at` is EXCLUDED: it changes on every assembly by construction, so
 * including it would make every board "changed" and turn push back into a poll
 * with extra steps. Per-source `ms` timings are excluded for the same reason.
 */
export function boardSignature(board) {
  const b = boardWithoutEvents(board);
  const { generated_at: _g, sources, ...rest } = b;
  return JSON.stringify({
    ...rest,
    sources: (sources ?? []).map((s) => ({ id: s.id, ok: s.ok, reason: s.reason })),
  });
}

/**
 * Per-section digests.
 *
 * ── WHY SECTIONS AND NOT ONE WHOLE-BOARD DIGEST (measured 2026-08-13) ───────
 *
 * A single digest was the first implementation and it is not enough. Measured
 * over 20s of live stream: 8 board frames at ~29KB each, because SOME clock
 * always moves — `run.elapsed_s`, `run.idle_s`, `run.log_silent_s` and
 * `tui.bytes` all tick every assembly. Any one of them flipped the whole-board
 * digest and re-sent every unchanged section with it.
 *
 * The worst offender was `tui`: 12.4KB of the 29KB frame, a full terminal
 * screen, pushed twice a second whether or not the popout was even open.
 *
 * Sectioning means a ticking clock in `run` (486 bytes) costs 486 bytes, not
 * 29KB. Each top-level key is digested and shipped independently, and the
 * client merges by key.
 *
 * `sources[].ms` is normalised out here for the same reason it is excluded from
 * the whole-board digest: it is a timing measurement of the read itself and
 * moves on every assembly without carrying information the board renders.
 */
export function sectionSignatures(board) {
  const b = boardWithoutEvents(board);
  const out = {};
  for (const [k, v] of Object.entries(b)) {
    if (k === "generated_at") continue; // never a reason to push
    if (k === "sources") {
      out[k] = JSON.stringify((v ?? []).map((s) => ({ id: s.id, ok: s.ok, reason: s.reason })));
      continue;
    }
    out[k] = JSON.stringify(v ?? null);
  }
  return out;
}

/**
 * Split a section into its own sub-sections when it is large and only a small
 * part of it moves.
 *
 * ── MEASURED, NOT ASSUMED (2026-08-13) ─────────────────────────────────────
 *
 * After sectioning, patches were still 7.4KB every 2s. Of that, `control` was
 * 6,535 bytes — and the only thing that changed between consecutive patches
 * was a clock nested inside `control.run`. The static 6KB of capabilities and
 * roster rode along on every tick.
 *
 * `control` is the one section big enough and heterogeneous enough to be worth
 * splitting: `capabilities` and `roster` are effectively static for the life of
 * a run, while `run` ticks constantly. Splitting it means a ticking clock costs
 * its own ~300 bytes instead of dragging 6KB of unchanged roster with it.
 *
 * Nothing else is split. A section that is small (`run` at 485b) or that
 * changes as a whole gains nothing from finer granularity, and every split adds
 * a merge rule the client has to honour — complexity that must be paid for by a
 * measurement, not by a guess.
 */
const SPLIT_SECTIONS = { control: ["capabilities", "roster", "run", "notes"] };

/** Flatten split sections into `parent.child` keys; leave everything else. */
export function granularSignatures(board) {
  const b = boardWithoutEvents(board);
  const out = {};
  for (const [k, v] of Object.entries(b)) {
    if (k === "generated_at") continue;
    if (k === "sources") {
      out[k] = JSON.stringify((v ?? []).map((s) => ({ id: s.id, ok: s.ok, reason: s.reason })));
      continue;
    }
    const split = SPLIT_SECTIONS[k];
    if (split && v && typeof v === "object" && !Array.isArray(v)) {
      // The named children each get their own digest; whatever remains is
      // digested together so no field can be silently dropped from the wire.
      const rest = { ...v };
      for (const child of split) {
        if (child in v) {
          out[`${k}.${child}`] = JSON.stringify(v[child] ?? null);
          delete rest[child];
        }
      }
      out[`${k}.__rest`] = JSON.stringify(rest);
      continue;
    }
    out[k] = JSON.stringify(v ?? null);
  }
  return out;
}

/**
 * The TUI screen, reduced to its status when nobody is looking at it.
 *
 * The terminal frame is 12.4KB — by far the largest section on the board — and
 * it changes on every capture. Streaming it to a client whose popout is
 * MINIMIZED spends the entire bandwidth budget on pixels nobody can see.
 *
 * The client declares interest by reconnecting with `?tui=1` (panels/tui.js
 * toggles it). When it has not, the frame is dropped and only the STATUS is
 * sent — which is exactly what the minimized dock bar renders, so the bar stays
 * truthful about whether the mirror is alive.
 *
 * This is a transport optimisation ONLY. Nothing about what the TUI panel may
 * display changes; an expanded popout receives the full frame as before.
 */
export function tuiForClient(section, wantsFrame) {
  if (!section || wantsFrame) return section;
  const { frame: _f, ...status } = section;
  return { ...status, frame: null, frame_withheld: true };
}

/**
 * The TUI fast path.
 *
 * ── WHY THE MIRROR NEEDS ITS OWN CADENCE (measured defect 2026-08-13) ───────
 *
 * The terminal was repainting about once every 4 seconds. It was not streaming
 * at all — it was being PULLED through two independent 2s stages that do not
 * share a phase:
 *
 *   pty -> control Capture (continuous)
 *       -> control /api/tui            (pulled only when asked)
 *       -> dashboard control-plane source, inside getBoard()
 *       -> dashboard getBoard CACHE     age < pollMs = 2000ms
 *       -> dashboard push tick          setInterval(pollMs) = 2000ms
 *       -> browser
 *
 * best case 2s, worst case ~4s — which is exactly what was observed.
 *
 * A terminal is not board state. The board's cadence is right for a gate wall
 * that changes every few minutes and wrong for a screen a human is reading. So
 * the mirror gets a DIRECT line: this polls control/api/tui on its own short
 * interval and pushes frames the moment they change, bypassing the board
 * assembly and its cache entirely.
 *
 * IT RUNS ONLY WHILE SOMEONE IS WATCHING. The capture upstream stops itself
 * when nothing reads it (TUI_IDLE_STOP_MS), and polling IS that keepalive — so
 * an interval that ran with no subscriber would hold a pty open against a live
 * benchmark session for no reason.
 */
const TUI_STREAM_MS = 250;

/** Clients whose popout is open, and therefore want frames. */
function tuiSubscribers() {
  return [...streamClients].filter((res) => res.wevibeWantsTui === true);
}

/**
 * Diff two terminal frames to the ROWS that changed.
 *
 * ── WHY (measured 2026-08-13) ──────────────────────────────────────────────
 * Streaming at 250ms made the mirror feel live, and cost 36KB per frame — 80
 * frames in 20s, ~2.8MB, to fix a latency problem. That trade is not worth
 * making: a terminal that is being typed into changes a handful of rows, and a
 * cursor blink changes exactly one.
 *
 * So only changed rows go on the wire, addressed by index. The client splices
 * them into the frame it already holds. A full frame is still sent whenever the
 * geometry changes or the client has no frame to splice into — correctness
 * first, and a resize is rare.
 */
export function diffTuiRows(prev, next) {
  if (!Array.isArray(prev) || !Array.isArray(next) || prev.length !== next.length) return null;
  const rows = [];
  for (let i = 0; i < next.length; i += 1) {
    if (JSON.stringify(prev[i]) !== JSON.stringify(next[i])) rows.push([i, next[i]]);
  }
  return rows;
}

// ── http ─────────────────────────────────────────────────────────────────────

// Fixed allowlist — there is no dynamic path resolution anywhere in this
// server, so directory traversal is impossible by construction rather than by
// sanitising user input.
const STATIC = {
  "/": { file: "index.html", type: "text/html; charset=utf-8" },
  "/index.html": { file: "index.html", type: "text/html; charset=utf-8" },
  "/board.js": { file: "board.js", type: "text/javascript; charset=utf-8" },
  "/overlay.js": { file: "overlay.js", type: "text/javascript; charset=utf-8" },
  "/dom.js": { file: "dom.js", type: "text/javascript; charset=utf-8" },
  "/panels/chrome.js": { file: "panels/chrome.js", type: "text/javascript; charset=utf-8" },
  "/panels/curve.js": { file: "panels/curve.js", type: "text/javascript; charset=utf-8" },
  "/panels/ledger.js": { file: "panels/ledger.js", type: "text/javascript; charset=utf-8" },
  "/panels/create.js": { file: "panels/create.js", type: "text/javascript; charset=utf-8" },
  "/panels/live.js": { file: "panels/live.js", type: "text/javascript; charset=utf-8" },
  "/panels/hold.js": { file: "panels/hold.js", type: "text/javascript; charset=utf-8" },
  "/panels/profile.js": { file: "panels/profile.js", type: "text/javascript; charset=utf-8" },
  "/panels/tui.js": { file: "panels/tui.js", type: "text/javascript; charset=utf-8" },
  "/panels/wall.js": { file: "panels/wall.js", type: "text/javascript; charset=utf-8" },
  "/panels/recall.js": { file: "panels/recall.js", type: "text/javascript; charset=utf-8" },
  "/panels/rail.js": { file: "panels/rail.js", type: "text/javascript; charset=utf-8" },
  "/panels/runstart.js": { file: "panels/runstart.js", type: "text/javascript; charset=utf-8" },
  "/panels/startup.js": { file: "panels/startup.js", type: "text/javascript; charset=utf-8" },
  "/panels/popout.js": { file: "panels/popout.js", type: "text/javascript; charset=utf-8" },
  "/panels/extraction.js": { file: "panels/extraction.js", type: "text/javascript; charset=utf-8" },

  // ── VENDORED RUNTIME ──────────────────────────────────────────────────
  // Preact + hooks + htm, served from the image. Part of the fixed allowlist
  // like every other file: there is still no dynamic path resolution anywhere
  // in this server, so traversal remains impossible by construction.
  "/vendor/preact.mjs": { file: "vendor/preact.mjs", type: "text/javascript; charset=utf-8" },
  "/vendor/preact-hooks.mjs": { file: "vendor/preact-hooks.mjs", type: "text/javascript; charset=utf-8" },
  "/vendor/htm.mjs": { file: "vendor/htm.mjs", type: "text/javascript; charset=utf-8" },
  "/ui.js": { file: "ui.js", type: "text/javascript; charset=utf-8" },
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

    // ── GET /api/stream ──────────────────────────────────────────────────
    // The push channel. A GET that never ends, so the read-only property of
    // this server is preserved exactly (see the SSE rationale above).
    //
    // The client sends its event cursor as `?since=`. On connect it receives
    // the full board and every event newer than that cursor, so a reconnect
    // after a dropped connection resumes without a gap and without refetching
    // the whole ring.
    if (url.pathname === "/api/stream") {
      res.writeHead(200, {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-store",
        connection: "keep-alive",
        // Defeats proxy buffering, which otherwise holds frames until a buffer
        // fills and makes a live stream look dead.
        "x-accel-buffering": "no",
      });
      // Tell EventSource to back off to 2s on reconnect rather than its
      // 3s default — this is a local socket, and a run in flight should not
      // wait longer than the old poll interval to recover.
      res.write("retry: 2000\n\n");

      streamClients.add(res);
      const drop = () => streamClients.delete(res);
      req.on("close", drop);
      req.on("error", drop);
      res.on("error", drop);

      // Does this client's TUI popout want full terminal frames? The frame is
      // the largest section on the board and is withheld unless asked for.
      res.wevibeWantsTui = url.searchParams.get("tui") === "1";

      try {
        const board = await getBoard(cfg, mods, broken);
        const since = Number(url.searchParams.get("since") ?? 0) || 0;
        const rows = (board.events?.events ?? []).filter((e) => (e.seq ?? -1) > since);
        // The cursor this client has been brought up to. The push loop sends
        // each client only what is newer than ITS OWN cursor, so a client that
        // connected mid-run is never replayed rows it already has, and a
        // reconnecting client is never skipped past a gap.
        res.wevibeCursor = rows.length ? (rows[rows.length - 1].seq ?? since) : since;
        const full = boardWithoutEvents(board);
        full.tui = tuiForClient(full.tui, res.wevibeWantsTui);
        res.write(`event: board\ndata: ${JSON.stringify(full)}\n\n`);
        res.write(`event: events\ndata: ${JSON.stringify({ events: rows, cursor: board.events?.cursor ?? null })}\n\n`);
      } catch (err) {
        // Never swallow: the client is told why its first frame is missing.
        res.write(`event: error\ndata: ${JSON.stringify({ reason: String(err?.message ?? err) })}\n\n`);
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

  // ── THE PUSH LOOP ────────────────────────────────────────────────────────
  //
  // The server polls the SOURCES on the same cadence the browser used to, but
  // it does so ONCE for every attached client and pushes only what changed.
  // That is the whole win: the sources are files and a local HTTP service, so
  // something must read them on an interval — the defect was never the polling,
  // it was that every browser refetched 240KB of mostly-identical payload and
  // rebuilt the board from it.
  //
  // ONLY CHANGED SECTIONS ARE SENT. Per-section digests (see
  // sectionSignatures) mean a ticking `run.elapsed_s` costs 486 bytes rather
  // than re-sending the 12.4KB TUI screen beside it. A quiet run costs one
  // heartbeat comment every 15s and nothing else.
  let lastSections = null;
  let lastHeartbeat = 0;

  const tick = async () => {
    if (!streamClients.size) return; // nobody attached: do no work at all
    let board;
    try {
      board = await getBoard(cfg, mods, broken);
    } catch (err) {
      broadcast("error", { reason: String(err?.message ?? err) });
      return;
    }

    const sections = granularSignatures(board);
    if (lastSections === null) {
      // First tick with a client attached: they were already sent a full board
      // on connect, so this only primes the comparison.
      lastSections = sections;
    } else {
      const patch = {};
      let changed = 0;
      for (const [k, sig] of Object.entries(sections)) {
        if (lastSections[k] !== sig) {
          patch[k] = JSON.parse(sig);
          changed += 1;
        }
      }
      // A section that DISAPPEARED is a real change and must reach the client,
      // otherwise a panel keeps rendering state the server no longer has.
      for (const k of Object.keys(lastSections)) {
        if (!(k in sections)) {
          patch[k] = null;
          changed += 1;
        }
      }
      lastSections = sections;

      if (changed) {
        // THE TUI IS OWNED BY THE FAST PATH. It is dropped from the slow
        // patch entirely: the board loop reads a value that is already up to
        // 2s stale by the time it assembles, so letting it through would
        // overwrite a fresh 250ms frame with an older one and make the mirror
        // stutter backwards. One writer per section.
        delete patch.tui;
        if (!Object.keys(patch).length) return;
        const shared = JSON.stringify(patch);
        for (const res of [...streamClients]) {
          try {
            res.write(`event: patch\ndata: ${shared}\n\n`);
          } catch {
            streamClients.delete(res);
          }
        }
      }
    }

    // Per-client event deltas. Each client is at its own cursor, so this is a
    // per-socket write rather than a broadcast.
    const rows = board.events?.events ?? [];
    const cursor = board.events?.cursor ?? null;
    for (const res of [...streamClients]) {
      const since = res.wevibeCursor ?? 0;
      const fresh = rows.filter((e) => (e.seq ?? -1) > since);
      if (!fresh.length) continue;
      res.wevibeCursor = fresh[fresh.length - 1].seq ?? since;
      try {
        res.write(`event: events\ndata: ${JSON.stringify({ events: fresh, cursor })}\n\n`);
      } catch {
        streamClients.delete(res);
      }
    }

    // A comment frame keeps intermediaries from reaping an idle connection.
    // It is not data and the client ignores it — but its ABSENCE is how a
    // silent board becomes a dead board behind a proxy.
    const now = Date.now();
    if (now - lastHeartbeat >= 15000) {
      lastHeartbeat = now;
      for (const res of [...streamClients]) {
        try {
          res.write(`: heartbeat ${now}\n\n`);
        } catch {
          streamClients.delete(res);
        }
      }
    }
  };

  const loop = setInterval(() => void tick(), cfg.pollMs);
  // Never hold the process open for the sake of the timer.
  loop.unref?.();

  // ── THE TUI FAST PATH ────────────────────────────────────────────────────
  // A dedicated short-interval poll straight to the control plane, pushing
  // frames to subscribed clients only. See TUI_STREAM_MS above for why the
  // mirror cannot ride the board's cadence.
  let lastTuiFrame = null;
  let lastTuiRows = null;
  let tuiInFlight = false;

  const tuiTick = async () => {
    const subs = tuiSubscribers();
    if (!subs.length) {
      // Nobody is watching. Drop the memo so the next subscriber is guaranteed
      // a full frame rather than being diffed against a stale one.
      lastTuiFrame = null;
      return;
    }
    // Never let a slow control plane stack requests on top of each other.
    if (tuiInFlight) return;
    tuiInFlight = true;
    try {
      const base = cfg.controlUrl ?? "http://127.0.0.1:7718";
      const res = await fetch(`${base}/api/tui`, {
        signal: AbortSignal.timeout(2000),
        headers: { accept: "application/json" },
      });
      if (!res.ok) return;
      const data = await res.json();
      const sig = JSON.stringify(data);
      if (sig === lastTuiFrame) return; // an unchanged terminal sends nothing
      lastTuiFrame = sig;

      // ROW DIFF. The full ~36KB screen is sent only when there is nothing to
      // splice against or the geometry moved; otherwise only the rows that
      // actually changed, addressed by index. A cursor blink is one row.
      const rows = diffTuiRows(lastTuiRows, data.frame);
      const { frame: _f, ...meta } = data;
      const body =
        rows === null
          ? JSON.stringify({ tui: data })
          : JSON.stringify({ tui_rows: { rows, meta } });
      lastTuiRows = data.frame ?? null;

      for (const r of subs) {
        try {
          r.write(`event: patch\ndata: ${body}\n\n`);
        } catch {
          streamClients.delete(r);
        }
      }
    } catch {
      // A blink of the control plane must not kill the loop; the next tick
      // retries and the panel keeps its last good frame on screen.
    } finally {
      tuiInFlight = false;
    }
  };

  const tuiLoop = setInterval(() => void tuiTick(), TUI_STREAM_MS);
  tuiLoop.unref?.();

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
    console.log(`  stream     : GET /api/stream (SSE, push)`);
    if (broken.length) console.log(`  unwired    : ${broken.map((b) => b.id).join(", ")}`);
  });
};

main();
