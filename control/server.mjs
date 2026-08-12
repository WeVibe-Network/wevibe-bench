#!/usr/bin/env node
// ─────────────────────────────────────────────────────────────────────────────
// WEVIBE BENCH CONTROL PLANE — SERVER
//
//   node server.mjs                 # http://127.0.0.1:7718
//   node server.mjs --port 8081
//
// ZERO DEPENDENCIES. Node stdlib only. No build step, no npm install.
//
// ── THIS IS THE ONLY PART OF THE BOARD THAT CAN CHANGE THE WORLD ─────────────
//
// The dashboard on :7717 is read-only by construction and MUST STAY THAT WAY:
// GET-only, bench repo mounted `:ro`, no docker socket, uid 1000. Those are
// kernel-enforced properties that make "the dashboard corrupted a run"
// impossible rather than unlikely.
//
// Starting runs and triggering extraction cannot live there without destroying
// that. They live here instead: a separate process, a separate port, a separate
// trust level, and a deliberately small surface.
//
// ── SAFETY PROPERTIES (deliberate, do not weaken) ────────────────────────────
//
//   - BINDS 127.0.0.1 AND HAS NO --host FLAG. The read-only dashboard may be
//     exposed on a LAN as a deliberate act; a control plane may not. There is
//     no code path that binds anything else.
//
//   - NO SHELL, EVER. Every process is spawned with an argv array and
//     `shell:false`. Operator-supplied values (model alias, org id) are argv
//     entries, never shell words, so command injection is impossible by
//     construction rather than by escaping.
//
//   - ONE RUN, ONE EXTRACTION. Enforced server-side and refused with a stated
//     reason, never queued. The campaign is strictly serial (one resident local
//     model, one slot); a queue would let the UI imply a capability the
//     instrument does not have.
//
//   - THE MODEL MUST BE BENCH-ELIGIBLE. The proxy serves Walter's interactive
//     daily-driver aliases on the same endpoint as bench aliases. Starting a
//     benchmark against an interactive slot would contend with live use and
//     produce an indefensible measurement, so it is refused.
//
//   - EVERY REFUSAL CARRIES ITS REASON, VERBATIM, for a human on a stream.
//
//   - THE EVENT PROXY IS READ-ONLY AGAINST THE SERVE. GET /event only. It never
//     posts a prompt, aborts, or summarises — driving the session belongs to
//     the harness alone. A control plane that can inject a turn can corrupt the
//     measurement it displays.
// ─────────────────────────────────────────────────────────────────────────────

import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { open } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  CONTROL_CONTRACT_VERSION,
  RESUME_UNSUPPORTED,
  STALL_THRESHOLD_S,
  EXTRACT_STAGES,
  EVENT_RENDER_CAP,
  confirmationToken,
  restatement,
  refuse,
} from "./contract.mjs";
import { readRoster, CONTEXT_CHOICES } from "./roster.mjs";
import { readRunState } from "./runstate.mjs";
import { EventRing, subscribe } from "./events.mjs";
import { ExtractionTracker } from "./extraction.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const out = {
    port: Number(process.env.WEVIBE_CONTROL_PORT ?? 7718),
    benchRoot: process.env.WEVIBE_CONTROL_BENCH_ROOT ?? resolve(HERE, ".."),
    proxyUrl: process.env.WEVIBE_CONTROL_PROXY_URL ?? "http://127.0.0.1:4545",
    runtimeUrl: process.env.WEVIBE_CONTROL_RUNTIME_URL ?? "http://127.0.0.1:1234",
    serveUrl: process.env.WEVIBE_CONTROL_SERVE_URL ?? "http://127.0.0.1:4096",
    python: process.env.WEVIBE_CONTROL_PYTHON ?? null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--port") out.port = Number(argv[++i]);
    else if (a === "--bench-root") out.benchRoot = String(argv[++i]);
    else if (a === "--proxy-url") out.proxyUrl = String(argv[++i]);
    else if (a === "--runtime-url") out.runtimeUrl = String(argv[++i]);
    else if (a === "--serve-url") out.serveUrl = String(argv[++i]);
    else if (a === "--help" || a === "-h") out.help = true;
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));

if (args.help) {
  console.log(`
wevibe bench control plane

  node server.mjs [options]

  --port <n>          default 7718
  --bench-root <dir>  default: the parent of this file
  --proxy-url <url>   default http://127.0.0.1:4545   (model roster)
  --runtime-url <url> default http://127.0.0.1:1234   (residency + context)
  --serve-url <url>   default http://127.0.0.1:4096   (worker event stream)

  Binds 127.0.0.1 only. There is deliberately no --host flag.
`);
  process.exit(0);
}

const BENCH_ROOT = resolve(args.benchRoot);
const RUNS_ROOT = join(BENCH_ROOT, "runs");
const PYTHON = args.python ?? join(BENCH_ROOT, ".venv", "bin", "python");
const RUN_SCRIPT = join(BENCH_ROOT, "scripts", "run_cumulative.py");
const EXTRACT_SCRIPT = join(BENCH_ROOT, "scripts", "backgammon_sxe.py");

// ── mutable state: the ONLY things this service owns ─────────────────────────

/** The launcher process this service spawned, if any. */
let launcher = null;

const ring = new EventRing();
const extraction = new ExtractionTracker();

// The event subscription runs for the life of the process and reconnects
// forever. A cell's serve dies and restarts across teardown; that is normal and
// must not require an operator action to recover the feed.
subscribe(`${args.serveUrl}/event`, ring);

// ── helpers ──────────────────────────────────────────────────────────────────

function sendJson(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(text),
  });
  res.end(text);
}

async function readBody(req, cap = 64 * 1024) {
  return await new Promise((resolveBody, rejectBody) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > cap) {
        rejectBody(new Error("request body too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolveBody(Buffer.concat(chunks).toString("utf8")));
    req.on("error", rejectBody);
  });
}

/** Every precondition a start must satisfy, each with its own stated reason. */
async function validateStart(payload, roster, run) {
  const model = typeof payload?.model === "string" ? payload.model.trim() : "";
  const arm = payload?.arm === "on" || payload?.arm === "off" ? payload.arm : null;
  const org = typeof payload?.org === "string" && payload.org.trim() ? payload.org.trim() : null;
  const context = Number.isFinite(payload?.context) ? Number(payload.context) : null;

  if (!run.can_start) {
    return refuse("run_in_flight", run.blocked_reason ?? "a cell is already in flight");
  }
  if (!arm) {
    return refuse("org_required", "arm must be 'on' (memory) or 'off' (control)");
  }
  if (!model) {
    return refuse("unknown_model", "no model selected");
  }

  const entry = roster.models.find((m) => m.id === model);
  if (!entry) {
    return refuse(
      "unknown_model",
      `'${model}' is not served by the proxy roster at ${args.proxyUrl}`,
    );
  }
  if (!entry.bench_eligible) {
    return refuse(
      "model_not_eligible",
      `'${model}' is an interactive slot (purpose=${entry.purpose ?? "unknown"}), not a bench ` +
        "alias. Running a benchmark on it contends with live daily-driver use and " +
        "produces a measurement that cannot be defended.",
    );
  }

  // ON cells extract into an org; OFF cells must not carry one. This mirrors
  // the harness's own argparse contract rather than inventing a new rule.
  if (arm === "on" && !org) {
    return refuse("org_required", "an ON (memory) cell requires --org; extraction has no target without it");
  }
  if (arm === "off" && org) {
    return refuse("org_forbidden", "a CONTROL cell must not carry an org — it extracts nothing");
  }

  if (context !== null) {
    if (!CONTEXT_CHOICES.includes(context)) {
      return refuse(
        "context_unavailable",
        `context ${context} is not one of the offered lengths (${CONTEXT_CHOICES.join(", ")})`,
      );
    }
    if (Number.isFinite(entry.max_context) && context > entry.max_context) {
      return refuse(
        "context_unavailable",
        `context ${context} exceeds the runtime ceiling for ${model} (${entry.max_context})`,
      );
    }
  }

  const expected = confirmationToken({ model, arm, org, context });
  if (payload?.confirm !== expected) {
    return refuse(
      "bad_confirmation",
      "the confirmation did not match these parameters — they changed after the " +
        "preview was shown. Review the restatement and confirm again.",
      { expected_token: expected, restatement: restatement({ model, arm, org, context }) },
    );
  }

  return { ok: true, model, arm, org, context, entry };
}

// ── routes ───────────────────────────────────────────────────────────────────

const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  const path = url.pathname;

  // CORS for the dashboard origin only. The board runs on :7717 and this
  // service on :7718, so a browser treats them as cross-origin.
  res.setHeader("access-control-allow-origin", "*");
  res.setHeader("access-control-allow-headers", "content-type");
  res.setHeader("access-control-allow-methods", "GET,POST,OPTIONS");
  if (req.method === "OPTIONS") {
    res.writeHead(204).end();
    return;
  }

  try {
    // ── GET /api/capabilities ────────────────────────────────────────────
    // What this service can actually DO. The board asks before rendering a
    // control, so it never shows a button for an absent capability.
    if (path === "/api/capabilities" && req.method === "GET") {
      sendJson(res, 200, {
        contract_version: CONTROL_CONTRACT_VERSION,
        start_run: true,
        // Permanently false — the harness has no mid-cell checkpoint.
        resume_run: false,
        resume: RESUME_UNSUPPORTED,
        extract: true,
        events: true,
        select_context: true,
        stall_threshold_s: STALL_THRESHOLD_S,
        bench_root: BENCH_ROOT,
        python_present: existsSync(PYTHON),
        run_script_present: existsSync(RUN_SCRIPT),
        extract_script_present: existsSync(EXTRACT_SCRIPT),
      });
      return;
    }

    // ── GET /api/roster ──────────────────────────────────────────────────
    if (path === "/api/roster" && req.method === "GET") {
      sendJson(res, 200, await readRoster({
        proxyUrl: args.proxyUrl,
        runtimeUrl: args.runtimeUrl,
      }));
      return;
    }

    // ── GET /api/run ─────────────────────────────────────────────────────
    if (path === "/api/run" && req.method === "GET") {
      sendJson(res, 200, await readRunState({ runsRoot: RUNS_ROOT, launcher }));
      return;
    }

    // ── POST /api/run/preview ────────────────────────────────────────────
    // The restatement the UI must show before START. The SERVER composes it so
    // the words the operator reads are the words the server will act on.
    if (path === "/api/run/preview" && req.method === "POST") {
      const payload = JSON.parse((await readBody(req)) || "{}");
      const model = typeof payload.model === "string" ? payload.model : null;
      const arm = payload.arm === "on" || payload.arm === "off" ? payload.arm : null;
      const org = typeof payload.org === "string" && payload.org ? payload.org : null;
      const context = Number.isFinite(payload.context) ? Number(payload.context) : null;
      sendJson(res, 200, {
        ok: true,
        token: confirmationToken({ model, arm, org, context }),
        restatement: restatement({ model, arm, org, context }),
      });
      return;
    }

    // ── POST /api/run/start ──────────────────────────────────────────────
    if (path === "/api/run/start" && req.method === "POST") {
      const payload = JSON.parse((await readBody(req)) || "{}");
      const roster = await readRoster({ proxyUrl: args.proxyUrl, runtimeUrl: args.runtimeUrl });

      if (!roster.proxy_ok) {
        sendJson(res, 503, refuse("upstream_unwired", roster.reason ?? "model proxy unreachable"));
        return;
      }

      const run = await readRunState({ runsRoot: RUNS_ROOT, launcher });
      const check = await validateStart(payload, roster, run);
      if (!check.ok) {
        sendJson(res, 409, check);
        return;
      }

      const { model, arm, org, context } = check;

      // ARGV ARRAY, NO SHELL. Main-parser flags MUST precede the subcommand —
      // argparse exits 2 otherwise (verified 2026-08-10). This ordering is the
      // documented RUNBOOK invocation, reproduced exactly.
      const argv = [RUN_SCRIPT, "--model", model];
      if (org) argv.push("--org", org);
      argv.push("run", "--until-review", "--mode", arm);

      const stamp = new Date()
        .toISOString()
        .replace(/[-:]/g, "")
        .replace(/\.\d+Z$/, "");
      const logPath = join(RUNS_ROOT, `${arm}-cell-${stamp}.log`);

      let fh;
      try {
        fh = await open(logPath, "a");
      } catch (err) {
        sendJson(res, 500, refuse("launcher_failed", `cannot open log ${logPath}: ${err?.message ?? err}`));
        return;
      }

      const env = { ...process.env };
      // Context is passed to the worker through the environment rather than a
      // CLI flag because the harness reads it there; `null` means "registry
      // default" and deliberately sets nothing.
      if (context !== null) env.WEVIBE_BENCH_WORKER_NUM_CTX = String(context);

      let child;
      try {
        child = spawn(PYTHON, argv, {
          cwd: BENCH_ROOT,
          env,
          // stdin from /dev/null is MANDATORY, not cosmetic: without it the
          // process is suspended the instant it touches stdin, stranding a
          // half-built manifest and a live container. Same reason the RUNBOOK
          // requires `< /dev/null` on the shell launch.
          stdio: ["ignore", fh.fd, fh.fd],
          detached: true,
          shell: false,
        });
      } catch (err) {
        await fh.close().catch(() => {});
        sendJson(res, 500, refuse("launcher_failed", String(err?.message ?? err)));
        return;
      }

      child.unref();
      await fh.close().catch(() => {});

      launcher = {
        pid: child.pid,
        model,
        arm,
        org,
        context,
        started_at: Date.now(),
        log_path: logPath,
      };

      sendJson(res, 200, {
        ok: true,
        pid: child.pid,
        log_path: logPath,
        model,
        arm,
        org,
        context,
        restatement: restatement({ model, arm, org, context }),
      });
      return;
    }

    // ── POST /api/run/resume ─────────────────────────────────────────────
    // ALWAYS REFUSES. The route exists so the refusal is discoverable and
    // carries its reason, rather than 404-ing as if the feature were forgotten.
    if (path === "/api/run/resume" && req.method === "POST") {
      sendJson(res, 501, refuse("resume_unsupported", RESUME_UNSUPPORTED.reason, {
        alternative: RESUME_UNSUPPORTED.alternative,
      }));
      return;
    }

    // ── GET /api/events ──────────────────────────────────────────────────
    // Polled snapshot of the mapped ring, OLDEST-FIRST (it is a transcript,
    // not a ticker). `cursor` lets the board fetch only what is new without
    // holding a second SSE connection open.
    if (path === "/api/events" && req.method === "GET") {
      const raw = Number(url.searchParams.get("limit") ?? EVENT_RENDER_CAP);
      const limit = Math.min(EVENT_RENDER_CAP, raw || EVENT_RENDER_CAP);
      const since = Number(url.searchParams.get("since") ?? 0) || 0;
      const kindsRaw = url.searchParams.get("kinds");
      const kinds = kindsRaw ? kindsRaw.split(",").filter(Boolean) : null;
      sendJson(res, 200, ring.snapshot({ limit, kinds, since }));
      return;
    }

    // ── GET /api/extraction ──────────────────────────────────────────────
    if (path === "/api/extraction" && req.method === "GET") {
      sendJson(res, 200, extraction.view());
      return;
    }

    // ── POST /api/extraction/start ───────────────────────────────────────
    if (path === "/api/extraction/start" && req.method === "POST") {
      const payload = JSON.parse((await readBody(req)) || "{}");
      const run = await readRunState({ runsRoot: RUNS_ROOT, launcher });

      // Extraction reads a COMPLETED cell's session DB. Running it against a
      // live cell would read a DB the worker is still writing — the exact
      // class of defect WO-DBVOL-1 closed.
      if (run.state === "running" || run.state === "starting") {
        sendJson(res, 409, refuse(
          "run_in_flight",
          "a cell is still running — extraction reads the completed session DB and " +
            "must not run against a live worker",
        ));
        return;
      }

      const model = typeof payload.model === "string" && payload.model.trim()
        ? payload.model.trim()
        : launcher?.model ?? null;
      if (!model) {
        sendJson(res, 409, refuse("nothing_to_extract", "no model given and no prior run observed"));
        return;
      }

      const runLabel = typeof payload.run_label === "string" ? payload.run_label : null;
      if (!runLabel) {
        sendJson(res, 409, refuse("nothing_to_extract", "run_label is required to locate the session directory"));
        return;
      }

      // `source_mode` is the ARM, and backgammon_sxe.py accepts only off|on
      // (verified: argparse rejects anything else with exit 2). It is NOT a
      // free-form label — defaulting it to a run-directory name produced an
      // immediate argparse failure.
      const sourceMode = payload.source_mode === "on" || payload.source_mode === "off"
        ? payload.source_mode
        : launcher?.arm ?? null;
      if (!sourceMode) {
        sendJson(res, 409, refuse(
          "nothing_to_extract",
          "source_mode must be 'on' or 'off' (the arm of the cell being extracted) " +
            "and no prior run was observed to infer it from",
        ));
        return;
      }

      // An ON cell extracts into an org; the script itself refuses to run
      // without one ("--org-id MUST be explicitly pinned ... no silent
      // default"). Failing here with that reason is better than letting the
      // subprocess die 200ms later with a traceback the UI cannot explain.
      const orgId = typeof payload.org === "string" && payload.org.trim()
        ? payload.org.trim()
        : launcher?.org ?? null;
      if (sourceMode === "on" && !orgId) {
        sendJson(res, 409, refuse(
          "org_required",
          "extracting an ON cell requires an org id — backgammon_sxe.py refuses a silent default",
        ));
        return;
      }

      const started = extraction.start({
        python: PYTHON,
        script: EXTRACT_SCRIPT,
        cwd: BENCH_ROOT,
        runLabel,
        sourceMode,
        orgId,
        model,
      });

      sendJson(res, started.ok ? 200 : 409, started.ok ? { ok: true, model } : started);
      return;
    }

    // ── GET /api/health ──────────────────────────────────────────────────
    if (path === "/api/health" && req.method === "GET") {
      sendJson(res, 200, {
        ok: true,
        contract_version: CONTROL_CONTRACT_VERSION,
        bench_root: BENCH_ROOT,
        runs_root: RUNS_ROOT,
        stages: EXTRACT_STAGES.map((s) => s.id),
        event_feed: { connected: ring.connected, reason: ring.reason, total: ring.total },
      });
      return;
    }

    sendJson(res, 404, refuse("upstream_unwired", `no route ${req.method} ${path}`));
  } catch (err) {
    // Never swallow. The reason reaches the operator verbatim.
    sendJson(res, 500, refuse("launcher_failed", String(err?.message ?? err)));
  }
});

// 127.0.0.1 ONLY. There is deliberately no flag to change this.
server.listen(args.port, "127.0.0.1", () => {
  console.log(`wevibe bench control plane → http://127.0.0.1:${args.port}`);
  console.log(`  bench root : ${BENCH_ROOT}`);
  console.log(`  python     : ${PYTHON}${existsSync(PYTHON) ? "" : "  (MISSING)"}`);
  console.log(`  proxy      : ${args.proxyUrl}`);
  console.log(`  runtime    : ${args.runtimeUrl}`);
  console.log(`  serve      : ${args.serveUrl}  (event stream)`);
});

export { server };
