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
// Starting runs cannot live there without destroying that. It lives here
// instead: a separate process, a separate port, a separate trust level, and a
// deliberately small surface.
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
//   - ONE RUN AT A TIME. Enforced server-side and refused with a stated
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
  EVENT_RENDER_CAP,
  confirmationToken,
  restatement,
  refuse,
} from "./contract.mjs";
import { readRoster, CONTEXT_CHOICES } from "./roster.mjs";
import { readRunState } from "./runstate.mjs";
import { EventRing, subscribe } from "./events.mjs";
import { readGateActivity } from "./gate-events.mjs";
import { readWall, WALL_CONTRACT_VERSION } from "./wall.mjs";
import { readFeedback, feedbackRows, FEEDBACK_CONTRACT_VERSION } from "./feedback.mjs";
import { readModelsLedger } from "./models-ledger.mjs";
// THE FLOOR'S ONE OWNER — the same module the ledger's gates read and
// /api/baselines serves, so every refusal about a baseline on this server and
// every button on the board are answering from one derivation.
import { readBaselines, baselineFor, collectOffCells } from "./baselines.mjs";
import { TuiMirror } from "./tui.mjs";
import { readHold, releaseHold } from "./hold.mjs";
// WHERE A CELL'S MEASUREMENT LANDS. One campaign directory per model — see the
// module header. Split out so the rule is testable without binding a port.
import { campaignTargetFor } from "./campaign.mjs";
import { readProfiles, createProfile, activeProfile, attachRun } from "./profiles.mjs";
// CLOUD BASELINES. The catalogue is a mirror of the harness's own provider
// block and the key is resolved server-side — see the header of cloud.mjs for
// why no credential ever crosses the wire in either direction.
import { readCloud, readCloudKey, resolveCloudModel, CLOUD_API_KEY_ENV } from "./cloud.mjs";

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

/**
 * WHICH RUN DIRECTORY A RUN-SCOPED READ SHOULD DEFAULT TO.
 *
 * ── THE HOLE THIS FILLS ─────────────────────────────────────────────────────
 *
 * Run-scoped surfaces (`/api/wall`, `/api/feedback`) take `?run_dir=` and fell
 * back to the literal `"cumulative"` when the caller named none. That was true
 * while every cell wrote to `runs/cumulative`, and stopped being true when
 * campaigns became per-model: `campaign.mjs:campaignDirName` now lands a cell in
 * `runs/cumulative-<model>`, and the legacy directory is ARCHIVED on a wipe
 * (RUNBOOK §2) rather than reused.
 *
 * So the default addressed a directory that does not exist. `readWall` handled
 * that exactly as designed — no pinned roster, so it enumerated the live suite,
 * and no `manifest.status.jsonl`, so no gate carried an outcome — and served a
 * TRUE 71-gate denominator with zero results against it. The board rendered
 * what it was sent: `0/71 passing` over 71 empty squares, on a run whose own
 * artifacts recorded 16 passing, 2 failing, 53 not run.
 *
 * THE LOG IS THE AUTHORITY, not a name pattern. `newestLog` (via `readRunState`)
 * resolves the run directory from the harness's own PROGRESS lines and rejects a
 * log whose directory is gone, so this follows a rename, a per-model campaign,
 * and the legacy layout without knowing about any of them — `readGateActivity`
 * already resolves the live run this way.
 *
 * NULL IS A REAL ANSWER. A bench with no cell log has no active run, and this
 * returns null so `resolveRunDir` falls through to `DEFAULT_RUN_DIR` and the
 * reader reports `unwired` with its reason. An invented directory would be the
 * fabrication invariant I-2 forbids.
 */
async function activeRunDir() {
  try {
    const run = await readRunState({ runsRoot: RUNS_ROOT, launcher });
    return run?.run_dir ?? null;
  } catch {
    return null;
  }
}

// ── mutable state: the ONLY things this service owns ─────────────────────────

/** The launcher process this service spawned, if any. */
let launcher = null;

const ring = new EventRing();

// The event subscription runs for the life of the process and reconnects
// forever. A cell's serve dies and restarts across teardown; that is normal and
// must not require an operator action to recover the feed.
subscribe(`${args.serveUrl}/event`, ring);

// The TUI mirror is ON-DEMAND, unlike the event feed. It costs a resident
// `opencode attach` client, so it starts on the first poll and stops itself once
// nothing is reading — polling IS the keepalive. It NEVER writes to the pty, so
// it cannot disturb the live session it is showing.
const tui = new TuiMirror({ serveUrl: args.serveUrl });
process.on("exit", () => tui.shutdown());
process.on("SIGINT", () => { tui.shutdown(); process.exit(0); });
process.on("SIGTERM", () => { tui.shutdown(); process.exit(0); });

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
/**
 * Validate a start request.
 *
 * `requireConfirm` separates the two callers. PREVIEW must run every PARAMETER
 * check (so it can never green-light a run that start would refuse) but cannot
 * require the confirmation token — the token is what preview EXISTS to mint, so
 * demanding it there is circular and refuses every valid preview with
 * `bad_confirmation`. START requires it, and that is what makes the second click
 * meaningful.
 */
async function validateStart(
  payload,
  roster,
  run,
  { requireConfirm = true, profile = null, runsRoot = null } = {},
) {
  const model = typeof payload?.model === "string" ? payload.model.trim() : "";
  const arm = payload?.arm === "on" || payload?.arm === "off" ? payload.arm : null;
  const org = typeof payload?.org === "string" && payload.org.trim() ? payload.org.trim() : null;
  const context = Number.isFinite(payload?.context) ? Number(payload.context) : null;
  // THE SUBSTRATE IS DECLARED, NEVER SNIFFED. A model id could in principle be
  // classified by whether it contains a slash, and that would be a rule the
  // operator cannot see and the roster could break at any time. `kind` is an
  // explicit parameter, it defaults to local (every cell before this existed
  // was local), and anything else is refused by name.
  const kind = payload?.kind === "cloud" ? "cloud" : payload?.kind === "local" || payload?.kind === undefined || payload?.kind === null ? "local" : String(payload.kind);

  if (!run.can_start) {
    return refuse("run_in_flight", run.blocked_reason ?? "a cell is already in flight");
  }
  if (kind !== "local" && kind !== "cloud") {
    return refuse("unknown_kind", `'${kind}' is not a substrate — it is 'local' (the relay proxy) or 'cloud' (a routed vendor API)`);
  }
  if (!arm) {
    return refuse("org_required", "arm must be 'on' (memory) or 'off' (control)");
  }
  if (!model) {
    return refuse("unknown_model", "no model selected");
  }

  // ── CLOUD TAKES A DIFFERENT ROUTE THROUGH EVERY IDENTITY CHECK ──────────
  //
  // and only through the identity checks. A cloud cell is not served by the
  // local proxy, so the roster lookup below cannot find it and residency,
  // declared context and the retired-alias list have nothing to say about it.
  // Everything AFTER this block — the subject rule, the org rule, the baseline
  // gate, the confirmation token — is substrate-blind by construction and
  // applies to a cloud cell exactly as written. That is the reason this returns
  // an `entry` in the roster's shape rather than branching the whole function:
  // two copies of the baseline gate is how a cloud cell eventually launches
  // against no floor.
  if (kind === "cloud") {
    const cloud = resolveCloudModel(model);
    if (!cloud.ok) return refuse(cloud.code, cloud.reason);

    // THE KEY IS CHECKED HERE, NOT AT THE VENDOR. Without it the harness spawns,
    // builds a manifest, reserves spend and dies at the first request — leaving
    // a half-built campaign directory behind for a fault that was knowable
    // before anything was written.
    const key = await readCloudKey({ benchRoot: BENCH_ROOT });
    if (!key.present) {
      return refuse("cloud_key_missing", key.reason, { env: CLOUD_API_KEY_ENV });
    }

    const cloudEntry = {
      id: cloud.key,
      upstream_model: cloud.slug,
      bench_eligible: true,
      purpose: "wevibe-bench",
      resident: null,
      declared_context: cloud.context,
      max_context: cloud.context,
      retired_reason: null,
    };
    return await finishValidate(
      { model, arm, org, context, kind, entry: cloudEntry, cloud },
      { requireConfirm, profile, runsRoot, payload },
    );
  }

  const entry = roster.models.find((m) => m.id === model);
  if (!entry) {
    return refuse(
      "unknown_model",
      `'${model}' is not served by the proxy roster at ${args.proxyUrl}`,
    );
  }
  if (!entry.bench_eligible) {
    // TWO DIFFERENT INELIGIBILITIES, NAMED SEPARATELY. A retired alias carries
    // the proxy's bench purpose and is refused anyway; saying "interactive slot"
    // about it would be false and would send the operator looking at the proxy's
    // labels for a cause that is not there.
    if (entry.retired_reason) {
      return refuse("model_retired", `'${model}' — ${entry.retired_reason}`);
    }
    return refuse(
      "model_not_eligible",
      `'${model}' is an interactive slot (purpose=${entry.purpose ?? "unknown"}), not a bench ` +
        "alias. Running a benchmark on it contends with live daily-driver use and " +
        "produces a measurement that cannot be defended.",
    );
  }

  return await finishValidate(
    { model, arm, org, context, kind, entry, cloud: null },
    { requireConfirm, profile, runsRoot, payload },
  );
}

/**
 * EVERY RULE THAT IS BLIND TO THE SUBSTRATE — which is every rule except
 * identity.
 *
 * Split out when cloud baselines landed, and split rather than branched for one
 * reason: the baseline gate. A cloud cell is a benchmark cell, so an ON cloud
 * cell needs a scorable floor exactly as an ON local cell does, and a second
 * copy of that check written for the cloud path is a second place for it to be
 * forgotten, weakened, or accidentally made conditional. There is one copy and
 * both substrates fall through it.
 *
 * `entry` is the roster row for a local model and a SYNTHESISED row of the same
 * shape for a cloud one, so the context ceiling below reads one field name
 * rather than asking which substrate it is looking at.
 */
async function finishValidate(
  { model, arm, org, context, kind, entry, cloud },
  { requireConfirm, profile, runsRoot, payload },
) {
  // THE SUBJECT RULE — ON CELLS ONLY. A profile freezes ONE subject model, and
  // an ON cell must be that model: it is scored against the OFF floor, so an ON
  // cell on another model produces a Δ measuring A-vs-B capability rather than
  // memory lift. The harness catches that eventually and expensively — a model
  // swap changes the roster hash, which invalidates the manifest (RUNBOOK §0,
  // archive-and-rerun) — i.e. after the cell has already burned hours. Refusing
  // at launch turns that into a one-line refusal.
  //
  // AN OFF CELL IS NOT COVERED BY THIS RULE, and applying it to one was a
  // defect. A baseline is not measured against anything — it IS the floor, one
  // per model (control/models-ledger.mjs, "BASELINE OWNERSHIP: PER MODEL"). A
  // profile on model A therefore has no bearing on whether model B may have a
  // floor measured. Enforced here, the first frozen profile silently locked
  // [+ baseline] on every OTHER bench model, so a four-model bench could never
  // acquire its second baseline — and the ledger, which mirrors this rule,
  // drew four permanently disabled buttons to match.
  //
  // Enforced ONLY when a profile exists. A cell with no profile is unattributed
  // but legitimate (the CLI case), and inventing a subject for it would be
  // worse than having none.
  if (arm === "on" && profile?.subject_model && model !== profile.subject_model) {
    return refuse(
      "model_not_subject",
      `profile ${profile.id} froze '${profile.subject_model}' as its subject model, and an ON ` +
        `cell must be that model — an ON cell on '${model}' measured against an OFF floor on ` +
        `'${profile.subject_model}' yields a delta between two models' capabilities, not the ` +
        "memory lift this stack exists to measure. To benchmark " +
        `'${model}', run its baseline (that is always allowed) and freeze a profile with it as ` +
        "the subject; the memory roster can be the same.",
      { subject_model: profile.subject_model, profile_id: profile.id },
    );
  }

  // ON cells write memories into an org, so a cell needs an org id; OFF cells
  // must not carry one. This mirrors the harness's own argparse contract rather
  // than inventing a new rule.
  if (arm === "on" && !org) {
    return refuse("org_required", "an ON (memory) cell requires --org; it needs an org id to write into");
  }
  if (arm === "off" && org) {
    return refuse("org_forbidden", "a CONTROL cell must not carry an org — it writes no memories");
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

  // ── THE BASELINE GATE ──────────────────────────────────────────────────
  //
  // An ON cell measures memory lift as a Δ against that model's OFF floor. With
  // no valid floor there is nothing to subtract from, so the cell burns ~3h to
  // produce a number that cannot be interpreted — and the failure is silent,
  // because the cell itself succeeds. Refusing here turns hours into a sentence.
  //
  // OFF cells are exempt BY DEFINITION: an OFF cell IS the baseline, and gating
  // it on a baseline would make the first one impossible to run.
  //
  // VOID IS NOT A FLOOR. A void-instrument OFF cell produced numbers, which is
  // precisely why it must be rejected explicitly — nothing downstream can tell
  // an instrument artifact from a real measurement.
  // `runsRoot` is passed by the caller rather than read from module scope so
  // this function stays testable against a fixture directory. When it is absent
  // the gate CANNOT be evaluated, and an unevaluable safety gate must fail
  // closed — silently skipping it would let the exact cell it guards against
  // through.
  if (arm === "on") {
    if (!runsRoot) {
      return refuse(
        "baseline_required",
        "the baseline gate could not be evaluated (no runs root supplied), and an ON cell must " +
          "never launch on an unverified floor",
      );
    }
    const offCells = await collectOffCells(runsRoot);
    const baseline = baselineFor(model, offCells);
    if (!baseline.scorable) {
      return refuse("baseline_required", baseline.reason, { model, subject_model: model });
    }
  }

  const expected = confirmationToken({ model, arm, org, context, kind });
  if (requireConfirm && payload?.confirm !== expected) {
    return refuse(
      "bad_confirmation",
      "the confirmation did not match these parameters — they changed after the " +
        "preview was shown. Review the restatement and confirm again.",
      { expected_token: expected, restatement: restatement({ model, arm, org, context, kind, cloud }) },
    );
  }

  return { ok: true, model, arm, org, context, kind, entry, cloud };
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
        events: true,
        select_context: true,
        // The GATE WALL surface. Advertised so the board can tell "this control
        // plane predates /api/wall" from "the wall has nothing to show".
        wall: true,
        wall_contract_version: WALL_CONTRACT_VERSION,
        // The verbatim graded text the model was handed as user turns.
        feedback: true,
        feedback_contract_version: FEEDBACK_CONTRACT_VERSION,
        // Profiles are STORED here (durably, frozen) but never ENFORCED — no
        // recall request carries a producer-model allowlist. The two are
        // separate booleans on purpose: a UI that reads one capability would
        // otherwise imply the other.
        profiles: true,
        profile_enforcement: false,
        // CLOUD BASELINES. Advertised as a capability of this SERVICE, which is
        // a different question from whether a cloud cell can start right now —
        // that needs a key, and the answer lives in /api/cloud beside the
        // catalogue it applies to. A board that read one for the other would
        // either hide a working feature or offer an unauthenticated launch.
        cloud_baselines: true,
        stall_threshold_s: STALL_THRESHOLD_S,
        bench_root: BENCH_ROOT,
        python_present: existsSync(PYTHON),
        run_script_present: existsSync(RUN_SCRIPT),
      });
      return;
    }

    // ── GET /api/cloud ───────────────────────────────────────────────────
    //
    // The cloud catalogue, the router, the spend ceiling, and WHETHER A KEY
    // RESOLVES — never the key. What comes back about the credential is
    // `{present, source, fingerprint}`: enough to tell an operator that a cloud
    // launch will authenticate and where the key came from, and worth nothing
    // to anyone who reads it off the wire.
    //
    // GET ONLY, and there is deliberately no companion POST. A route that
    // accepted a key would put a live credential in a browser, in a request
    // body, and in the browser's autofill store — to configure a file that sits
    // beside this service on the same disk.
    if (path === "/api/cloud" && req.method === "GET") {
      sendJson(res, 200, await readCloud({ benchRoot: BENCH_ROOT }));
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

    // ── GET /api/tui ─────────────────────────────────────────────────────
    // A frame of the operator's attached view, reconstructed from a read-only
    // pty capture. This route is the keepalive: the capture starts on the first
    // poll and stops itself when polling stops, so a closed drawer does not
    // leave a client attached to a live benchmark session.
    //
    // The session is resolved from run state rather than taken from the query
    // string — a caller-supplied session id would let the board attach a client
    // to an arbitrary session, and the mirror should only ever show the cell
    // that is actually running.
    if (path === "/api/tui" && req.method === "GET") {
      const run = await readRunState({ runsRoot: RUNS_ROOT, launcher });
      sendJson(res, 200, {
        ...tui.poll(run.session_id),
        // Stated on the surface: this is a second client, not a screen-share.
        note: "second attach client — same session, independent scroll position",
      });
      return;
    }

    if (path === "/api/tui/detach" && req.method === "POST") {
      tui.shutdown();
      sendJson(res, 200, { ok: true, reason: "tui mirror detached and closed" });
      return;
    }

    // ── GET /api/hold ────────────────────────────────────────────────────
    // null means no hold file exists. If the file vanishes while being read,
    // that is the release success path, not an error.
    if (path === "/api/hold" && req.method === "GET") {
      sendJson(res, 200, await readHold({ runsRoot: RUNS_ROOT }));
      return;
    }

    // ── POST /api/hold/release ───────────────────────────────────────────
    // Release means create release.path from hold-ui.json. It is idempotent;
    // posting when nothing is held is harmless and returns ok.
    if (path === "/api/hold/release" && req.method === "POST") {
      const result = await releaseHold({ runsRoot: RUNS_ROOT });
      sendJson(res, result.ok ? 200 : 409, result);
      return;
    }

    // ── GET /api/profiles ────────────────────────────────────────────────
    // The frozen memory profiles. `active` is the newest; `prior` are the
    // earlier ones, measured under a different allowlist. No board panel draws
    // `prior` today — the BASELINES card lists every profile under the baseline it measures against
    // model — but the split is served because it is the fact a curve overlay
    // would need, and it costs nothing to keep stated.
    if (path === "/api/profiles" && req.method === "GET") {
      sendJson(res, 200, await readProfiles(RUNS_ROOT));
      return;
    }

    // ── POST /api/profiles/create ────────────────────────────────────────
    // Freeze an allowlist. THIS IS THE WHOLE OPERATION.
    //
    // It writes one file and does NOTHING else: it does not arm a cell, open a
    // session, or attach a TUI. That is the answer to the operator report "I
    // created a memory profile and nothing happened" — nothing was supposed to
    // happen, but the board never said so and the profile was not even stored.
    // Storage is fixed here; the silence is fixed in the UI, which states
    // CREATION SIDE EFFECTS — NONE and puts run start on its own control.
    if (path === "/api/profiles/create" && req.method === "POST") {
      const payload = JSON.parse((await readBody(req)) || "{}");

      // ── THE BASELINE GATE, SECOND ENFORCEMENT POINT ────────────────────
      //
      // A profile exists to be RUN, and every run under it is scored against
      // its subject model's OFF floor. Freezing a profile against a model with
      // no valid floor creates a record that can never produce an
      // interpretable result — and profiles are frozen forever, so the mistake
      // is permanent rather than correctable.
      //
      // This duplicates the rule /api/run/start applies, deliberately: the two
      // gates guard different damage (an uninterpretable cell vs a permanently
      // unusable profile) and both read `baselineFor`, so they cannot disagree
      // about what a valid floor is.
      const subject = typeof payload?.subject_model === "string" ? payload.subject_model.trim() : null;
      if (subject) {
        const baseline = baselineFor(subject, await collectOffCells(RUNS_ROOT));
        if (!baseline.scorable) {
          sendJson(res, 409, {
            ok: false,
            code: "baseline_required",
            reason: baseline.reason,
            subject_model: subject,
          });
          return;
        }
      }

      const result = await createProfile(RUNS_ROOT, {
        // TWO axes, named separately on the wire. The old single `models` field
        // conflated the measurement with the experiment variable; it is gone
        // rather than aliased, because an alias would let a caller freeze a
        // profile with no subject and never learn it.
        subjectModel: payload?.subject_model,
        memoryModels: payload?.memory_models,
        stackId: typeof payload?.stack_id === "string" ? payload.stack_id : null,
        note: typeof payload?.note === "string" ? payload.note : null,
      });
      sendJson(res, result.ok ? 200 : 409, result);
      return;
    }

    // ── POST /api/profiles/pin-log ───────────────────────────────────────
    // Durable record of the client-side subject-model pin event: the pin in
    // the run-start panel overwrote the operator's chosen model and disarmed
    // the run. `disarm()` is purely local — no network, no trace — so without
    // this endpoint the failure is silent and undiagnosable. One line per
    // event, appended to the runs root so it survives wipes.
    //
    // BEST-EFFORT BY DESIGN: missing fields log as "-"; only an unparseable
    // body refuses. The parse is wrapped so a malformed client can never
    // crash the control plane.
    if (path === "/api/profiles/pin-log" && req.method === "POST") {
      let payload;
      try {
        payload = JSON.parse((await readBody(req)) || "{}");
      } catch {
        sendJson(res, 400, { ok: false, error: "bad_body" });
        return;
      }
      const line =
        `${new Date().toISOString()} profile_pin` +
        ` arm=${payload?.arm ?? "-"} from=${payload?.from ?? "-"} to=${payload?.to ?? "-"}` +
        " — subject-model pin overwrote the chosen model and disarmed the run\n";
      const fh = await open(join(RUNS_ROOT, "profile-pin.log"), "a");
      try {
        await fh.appendFile(line);
      } finally {
        await fh.close().catch(() => {});
      }
      sendJson(res, 200, { ok: true });
      return;
    }

    // ── POST /api/run/preview ────────────────────────────────────────────
    // The restatement the UI must show before START. The SERVER composes it so
    // the words the operator reads are the words the server will act on.
    if (path === "/api/run/preview" && req.method === "POST") {
      const payload = JSON.parse((await readBody(req)) || "{}");
      const roster = await readRoster({ proxyUrl: args.proxyUrl, runtimeUrl: args.runtimeUrl });

      // PREVIEW RUNS THE SAME VALIDATION AS START.
      // It previously minted a token for ANY payload, so an ON cell with no org
      // returned 200 and the UI armed a confirm button for a run the server
      // would then refuse. A preview that can green-light an impossible run is
      // worse than no preview: it moves the refusal to after the operator has
      // committed.
      //
      // The serial gate is deliberately EXCLUDED — `can_start` is a fact about
      // right now, not about these parameters, and an operator must be able to
      // review what they intend to run next while a cell is still in flight.
      const run = await readRunState({ runsRoot: RUNS_ROOT, launcher });
      // The subject rule is checked at PREVIEW too. A preview that green-lights
      // a model the start will refuse moves the refusal to after the operator
      // has committed — the same defect the org check was moved here to fix.
      const check = await validateStart(
        payload,
        roster,
        { ...run, can_start: true, blocked_reason: null },
        { requireConfirm: false, profile: await activeProfile(RUNS_ROOT), runsRoot: RUNS_ROOT },
      );
      if (check.ok === false) {
        sendJson(res, 400, check);
        return;
      }

      const { model, arm, org, context, kind, cloud } = check;
      sendJson(res, 200, {
        ok: true,
        token: confirmationToken({ model, arm, org, context, kind }),
        restatement: restatement({ model, arm, org, context, kind, cloud }),
        // What the operator is committing to, in machine form beside the prose.
        // The confirmation card states the substrate and — for a cloud cell —
        // the vendor and the per-cell spend ceiling, and it must state the same
        // ones the token was minted for rather than the ones the form still has
        // on screen.
        kind,
        cloud: cloud ? { provider: cloud.provider, model: cloud.model, slug: cloud.slug, name: cloud.name } : null,
        // Stated so the UI can show the operator that the serial rule will
        // block this run, WITHOUT pretending the parameters are invalid.
        blocked_now: run.can_start === true ? null : (run.blocked_reason ?? "a cell is already in flight"),
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

      // Read ONCE and reuse for both the subject check and the attribution
      // below. Reading twice would let the two disagree if a profile were
      // frozen in between — the cell would be validated against one profile and
      // recorded against another.
      //
      // A read failure is NOT fatal here: it is carried and reported in the
      // attribution, because failing to read a profile must not block a run
      // the operator is entitled to start.
      let activeProf = null;
      let activeProfErr = null;
      try {
        activeProf = await activeProfile(RUNS_ROOT);
      } catch (err) {
        activeProfErr = String(err?.message ?? err);
      }

      const check = await validateStart(payload, roster, run, { profile: activeProf, runsRoot: RUNS_ROOT });
      if (!check.ok) {
        sendJson(res, 409, check);
        return;
      }

      const { model, arm, org, context, kind, cloud } = check;

      // ARGV ARRAY, NO SHELL. Main-parser flags MUST precede the subcommand —
      // argparse exits 2 otherwise (verified 2026-08-10). This ordering is the
      // documented RUNBOOK invocation, reproduced exactly.
      //
      // ── THE CLOUD INVOCATION IS THE HARNESS'S OWN, NOT A NEW ONE ──────────
      //
      // `--cloud --provider <vendor> --model <model>` is exactly what
      // `_compose_cloud_slug` in run_cumulative.py consumes: it joins them into
      // `{router}/{provider}/{model}` and refuses anything absent from the
      // OrcaRouter provider block. `--model` therefore carries the MODEL HALF of
      // the key on this path, not the whole key — passing `anthropic/claude-…`
      // to `--model` would compose `orcarouter/anthropic/anthropic/claude-…`
      // and be refused by the harness with a message about a model that is not
      // the one the operator picked.
      const argv = [RUN_SCRIPT];
      if (kind === "cloud") {
        argv.push("--cloud", "--provider", cloud.provider, "--model", cloud.model);
      } else {
        argv.push("--model", model);
      }
      if (org) argv.push("--org", org);
      // A cell writes to ITS MODEL'S campaign, not to whichever campaign the
      // default path happens to hold. Omitted when the default is already this
      // model's, so the live campaign's invocation is unchanged.
      //
      // The target is resolved BEFORE the spawn because it is also the join key
      // recorded in the attribution below — which directory and which cell
      // inside it this launch is about to write. Afterwards it is unknowable.
      const target = await campaignTargetFor(model, RUNS_ROOT);
      if (target.manifest_arg) argv.push("--manifest", target.manifest_arg);
      // `run` ACCEPTS EXACTLY --mode, --proxy-base-url, --proxy-token-file.
      // `--until-review` was removed from the harness by ba2947a (2026-08-14)
      // and kept here, so argparse rejected the whole invocation before the
      // harness did anything: the child exited on a usage error, the log held
      // nothing but that error, and the board — which infers "running" from the
      // log's existence — reported BUSY over a process that was already dead.
      // Every board-launched cell failed this way. Flags here must be checked
      // against the run subparser, not against memory.
      argv.push("run", "--mode", arm);

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
        kind,
        started_at: Date.now(),
        log_path: logPath,
      };

      // ATTRIBUTION, recorded from what this service actually did. The run is
      // attached to whichever profile is active at launch, which is the only
      // moment the association is knowable — a cell launched at the CLI has no
      // profile and must never be swept into one after the fact.
      //
      // BEST-EFFORT AND NON-FATAL: the process is already running. Failing the
      // response now would tell the operator the run did not start when it did,
      // which is the more damaging error. The failure is REPORTED in the
      // payload, never swallowed.
      let attribution = null;
      try {
        const active = activeProf;
        if (activeProfErr) {
          throw new Error(`profile store unreadable at launch: ${activeProfErr}`);
        }
        if (active) {
          const att = await attachRun(RUNS_ROOT, active.id, {
            log_name: logPath.split("/").pop(),
            arm,
            model,
            org,
            context,
            kind,
            // ── THE JOIN KEY ────────────────────────────────────────────
            //
            // Which campaign directory this cell writes to, and which slot in
            // its schedule it is. Recorded here because HERE IS THE ONLY PLACE
            // THEY ARE KNOWN: after the spawn, a directory holding four cells
            // cannot say which of them a given log produced, which is why every
            // measurement column on every profile row has been null since the
            // ledger shipped.
            run_dir: target.run_dir,
            sequence_index: target.sequence_index,
            pid: child.pid,
            started_at: launcher.started_at,
          });
          attribution = att.ok
            ? { profile_id: active.id, recorded: true, reason: null }
            : { profile_id: active.id, recorded: false, reason: att.reason };
        } else {
          attribution = {
            profile_id: null,
            recorded: false,
            reason: "no profile exists — this cell is unattributed and will not appear in any profile's history",
          };
        }
      } catch (err) {
        attribution = { profile_id: null, recorded: false, reason: String(err?.message ?? err) };
      }

      sendJson(res, 200, {
        ok: true,
        pid: child.pid,
        log_path: logPath,
        model,
        arm,
        org,
        context,
        kind,
        cloud: cloud ? { provider: cloud.provider, model: cloud.model, slug: cloud.slug } : null,
        // Where this cell will land, echoed back. The operator can check it
        // against the row that appears on the board a tick later, and a
        // mismatch is then visible rather than being a silent misattribution.
        run_dir: target.run_dir,
        sequence_index: target.sequence_index,
        attribution,
        restatement: restatement({ model, arm, org, context, kind, cloud }),
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
    //
    // HARNESS GRADING ROWS ARE MERGED IN HERE (WO-GRADE-VIS-1). They come from
    // a different source than every other row — the harness's own PROGRESS
    // lines in the run log, not the worker's SSE stream — because during
    // grading the worker is idle BY DESIGN and its stream says nothing. Without
    // them the feed goes silent for the length of a grade (measured at 32
    // minutes on 2026-08-12) and a working run is indistinguishable from a
    // wedged one.
    //
    // They are APPENDED rather than interleaved by timestamp: grading happens
    // between agent turns, so appending preserves true chronology, and the
    // harness's naive local timestamps cannot be compared against the worker's
    // epoch times without reintroducing the timezone defect documented at
    // contract.mjs STALL_THRESHOLD_S.
    if (path === "/api/events" && req.method === "GET") {
      const raw = Number(url.searchParams.get("limit") ?? EVENT_RENDER_CAP);
      const limit = Math.min(EVENT_RENDER_CAP, raw || EVENT_RENDER_CAP);
      const since = Number(url.searchParams.get("since") ?? 0) || 0;
      const kindsRaw = url.searchParams.get("kinds");
      const kinds = kindsRaw ? kindsRaw.split(",").filter(Boolean) : null;
      // THE SNAPSHOT IS TAKEN BELOW, AFTER the out-of-ring rows are admitted —
      // otherwise a row admitted on this request would not appear until the
      // next poll, and `cursor` would advance past it in the meantime.

      // Never let a log-read failure take the agent feed down: grading rows are
      // additive instrumentation, and the feed must degrade to exactly its
      // previous behaviour if they are unavailable.
      let gate = { rows: [], status: null };
      try {
        gate = await readGateActivity(RUNS_ROOT);
      } catch (err) {
        gate = { rows: [], status: null, error: String(err?.message ?? err) };
      }

      // NO `kinds` FILTER HERE ANY MORE. These rows now go into the ring, and
      // `ring.snapshot()` applies the filter to everything uniformly — filtering
      // before admission would permanently withhold a row from the ring merely
      // because a filter chip happened to be off when it first appeared.

      // GRADED TEXT ROWS ARE MERGED IN TOO (WO-FEEDBACK-1). The harness renders
      // gate results into prose and hands it to the model AS A USER TURN. That
      // message is the single most consequential input the model receives and
      // it appeared nowhere in this feed — the worker's SSE stream shows only
      // what the model did with it, never what it was given.
      //
      // They carry `kind:"user"` deliberately: on this board they ARE user
      // turns, which is exactly the fiction under test. Labelling them
      // "harness" would quietly answer the question the operator opened the
      // feed to judge.
      let feedback = { rows: [], error: null };
      try {
        const fb = await readFeedback({ runsRoot: RUNS_ROOT, runDir: await activeRunDir(), limit });
        feedback = { rows: fb.ok ? feedbackRows(fb.messages) : [], error: null };
      } catch (err) {
        // Additive instrumentation: a read failure must degrade the feed to its
        // previous behaviour, never take the agent stream down with it.
        feedback = { rows: [], error: String(err?.message ?? err) };
      }

      // ── ADMIT THE OUT-OF-RING ROWS ONCE, THEN LET THE RING DO EVERYTHING ───
      //
      // THE ORIGINAL DEFECT: gate rows and feedback rows are built OUTSIDE
      // EventRing, so they never passed through `push()` — the only thing that
      // assigns `seq`. They reached the client with `seq: undefined`, and the
      // renderer appends incrementally with
      //   rows.filter((e) => (e.seq ?? -1) > renderedSeq)     [live.js]
      // so every one of them scored -1 and NOTHING WAS EVER APPENDED.
      //
      // THE DEFECT THAT FIX INTRODUCED, AND THIS ONE CLOSES: numbering them at
      // request time from `snapshot.cursor` made the seq a function of a MOVING
      // base. These rows are rebuilt from files on every poll, so the same row
      // was re-sequenced every time, cleared the append gate again, and was
      // appended again. Measured on a live run: one `task chunk (attempt 1)`
      // came back as seq 706, then 713, then higher, and the operator saw it
      // repeated down the whole feed.
      //
      // Admitting each row ONCE, by identity, fixes both at the source: the row
      // takes a seq from the ring's own counter (so it cannot collide with a
      // real upstream seq), and `since` / `cursor` / `capped` need no special
      // case here at all. See EventRing.admit().
      //
      // Admission happens BEFORE the snapshot is taken, so a newly-admitted row
      // appears in this very response rather than one poll later.
      for (const r of [...gate.rows, ...feedback.rows]) ring.admit(r);
      const snapshot = ring.snapshot({ since, limit, kinds });

      // Counts must reflect what the operator can filter on, including the
      // grading rows — a chip whose count is always 0 reads as "never happens".
      // `snapshot.counts` initialises only the five agent kinds and skips any
      // others, so `harness` and `user` are still tallied here; they are counted
      // from the RING (not from the freshly-read files) so the number describes
      // the same population the filter chips actually select from.
      const counts = { ...snapshot.counts };
      for (const r of ring.items) {
        if (r.kind === "harness" || r.kind === "user") {
          counts[r.kind] = (counts[r.kind] ?? 0) + 1;
        }
      }

      sendJson(res, 200, {
        ...snapshot,
        counts,
        // The live grading verdict: which phase is open, how long it has been
        // silent, and whether that exceeds the alarm threshold.
        grading: gate.status,
      });
      return;
    }

    // ── GET /api/feedback ────────────────────────────────────────────────
    // The graded text, VERBATIM — exactly what the model was told a user sent.
    //
    // This is the surface for judging the fiction: the harness renders gate
    // results into prose and delivers it as a user turn, and until this existed
    // nobody could read those bytes. It does not summarise or re-render; a
    // surface that prettified the text would answer a different question than
    // the one an operator is asking when they open it.
    //
    //   ?run_dir=<name>   default: the ACTIVE run (see `activeRunDir`)
    //   ?cell=<name>      default: the most recently written cell
    //   ?limit=<n>        default 50, newest-last
    //   ?text=0           omit bodies (index only)
    if (path === "/api/feedback" && req.method === "GET") {
      const limitRaw = Number(url.searchParams.get("limit"));
      const result = await readFeedback({
        runsRoot: RUNS_ROOT,
        runDir: url.searchParams.get("run_dir") ?? (await activeRunDir()),
        cell: url.searchParams.get("cell"),
        limit: Number.isFinite(limitRaw) && limitRaw > 0 ? limitRaw : 50,
        includeText: url.searchParams.get("text") !== "0",
      });
      sendJson(res, result.ok ? 200 : 400, result);
      return;
    }

    // ── GET /api/wall ────────────────────────────────────────────────────
    // The GATE WALL's single source: the gate roster folded with the per-gate
    // outcomes of the last completed test run. The board must not stitch the
    // two artifacts together — a second implementation of this fold would
    // disagree with the first, and every disagreement shows up as a wrong
    // colour on a square.
    //
    // The server decides `state`; the board decides colour. Nothing here emits
    // colours, CSS, or presentation. NO LIVE SIGNAL AND NO PHASE: a square
    // carries a recorded verdict or it carries none.
    //
    // Never 500s on a missing roster: that is a real state (the run predates
    // the artifact), reported as ok:true + unwired + a reason.
    if (path === "/api/wall" && req.method === "GET") {
      // An explicit ?run_dir= always wins — that is how an operator inspects an
      // archived run. With none, the ACTIVE run is the answer, resolved from the
      // cell log rather than from a directory name this file would have to keep
      // in step with the campaign layout. See `activeRunDir`.
      const wall = await readWall({
        runsRoot: RUNS_ROOT,
        runDir: url.searchParams.get("run_dir") ?? (await activeRunDir()),
        benchRoot: BENCH_ROOT,
      });
      sendJson(res, wall.ok ? 200 : 400, wall);
      return;
    }


    // ── GET /api/baselines ───────────────────────────────────────────────
    // THE FLOOR, ON ITS OWN. Every model's baseline, resolved by the single
    // owner (baselines.mjs) and published to runs/baselines.json on the way
    // out. The ledger carries the same index inline, so a board needs no extra
    // call — this endpoint exists for everything that wants the floors WITHOUT
    // the launch gates: a script, a report, a second surface, an operator with
    // curl. One derivation, several readers, no second definition.
    if (path === "/api/baselines" && req.method === "GET") {
      const roster = await readRoster({ proxyUrl: args.proxyUrl, runtimeUrl: args.runtimeUrl });
      const out = await readBaselines({
        runsRoot: RUNS_ROOT,
        models: roster.ok ? (roster.bench_models ?? []) : [],
      });
      // A roster that could not be read is stated rather than silently yielding
      // an empty index — "no models answered" and "no model has a floor" are
      // different facts.
      sendJson(res, 200, {
        ...out,
        roster_ok: roster.ok,
        roster_reason: roster.ok ? null : roster.reason,
      });
      return;
    }

    // ── GET /api/models-ledger ───────────────────────────────────────────
    // One row per bench-eligible model with its profiles nested, and every
    // launch gate already resolved. The board renders this and decides
    // nothing: a button's enabled state and the refusal /api/run/start would
    // actually apply are computed from the same place, so they cannot drift.
    if (path === "/api/models-ledger" && req.method === "GET") {
      const runState = await readRunState({ runsRoot: RUNS_ROOT, launcher });
      const roster = await readRoster({ proxyUrl: args.proxyUrl, runtimeUrl: args.runtimeUrl });
      const ledger = await readModelsLedger({
        runsRoot: RUNS_ROOT,
        benchModels: roster.ok ? (roster.bench_models ?? []) : [],
        runInFlight: runState.can_start !== true,
        blockedReason: runState.blocked_reason,
        // THE CLOUD HALF OF THE MODEL UNIVERSE. Passed in rather than read
        // inside the ledger so this route owns every I/O boundary it crosses,
        // and so the ledger stays a pure assembly over what it is handed —
        // which is what makes it testable against a fixture directory.
        cloud: await readCloud({ benchRoot: BENCH_ROOT }),
      });
      // The roster is the model universe; without it there are no rows to
      // draw, and saying so is not the same as saying "no models exist".
      if (!roster.ok) {
        sendJson(res, 200, {
          ...ledger,
          models: [],
          unwired: ["roster"],
          unwired_reason: roster.reason ?? "model proxy unreachable",
        });
        return;
      }
      sendJson(res, 200, ledger);
      return;
    }

    // ── GET /api/health ──────────────────────────────────────────────────
    if (path === "/api/health" && req.method === "GET") {
      sendJson(res, 200, {
        ok: true,
        contract_version: CONTROL_CONTRACT_VERSION,
        bench_root: BENCH_ROOT,
        runs_root: RUNS_ROOT,
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
