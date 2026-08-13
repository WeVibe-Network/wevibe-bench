// ─────────────────────────────────────────────────────────────────────────────
// GATE WALL — one served surface: roster + outcomes + live state.
//
// WHY THIS EXISTS (WO-GATE-ROSTER)
//
// The board's GATE WALL is a six-state grid over the FULL gate suite. Four of
// those six states were underivable from what the harness published:
//
//   dashed  not yet tested      needed a roster        — none existed
//   amber   under test now      needed live per-gate   — only phase-level
//   blue    passed in attempt 1 needed pass outcomes   — passes never published
//   green   passed later        needed pass outcomes   — same
//   slate   abandoned mid-test  needed the in-flight set at stop
//   red     tested and failed   `failed_gates`         — the only one wired
//
// Those facts now exist, but in THREE places: the roster artifact, the
// append-only status stream, and the live PROGRESS log. This module merges them
// so the board reads ONE endpoint. A board that stitched three sources would
// have to reimplement the fold — and every disagreement between the two
// implementations would surface as a wrong colour.
//
// THE SERVER DECIDES STATE; THE BOARD DECIDES COLOUR. Nothing here emits
// colours, CSS, or presentation of any kind.
//
// READ-ONLY. Reads three files. Never writes, never spawns, never signals.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join, resolve, sep } from "node:path";
import { promisify } from "node:util";

import { readGateActivity } from "./gate-events.mjs";
import { newestLog, readTail } from "./runstate.mjs";

const execFileAsync = promisify(execFile);

/** How long the enumerator may take before the suite is reported unknown. */
const ROSTER_ENUMERATE_TIMEOUT_MS = 60_000;

/** The contract version the board can assert against. */
export const WALL_CONTRACT_VERSION = 1;

/** The published per-gate state vocabulary. */
export const GATE_STATES = /** @type {const} */ ([
  "resolved",
  "failing",
  "untested",
  "abandoned",
]);

/** The campaign run directory when the caller does not name one. */
export const DEFAULT_RUN_DIR = "cumulative";

/**
 * Resolve `?run_dir=` safely.
 *
 * The value reaches an fs path, so it is confined to a direct child of the runs
 * root: no separators, no traversal, no absolute paths. Returns null for
 * anything that escapes, and the route refuses rather than reading.
 */
export function resolveRunDir(runsRoot, raw) {
  const name = String(raw ?? "").trim() || DEFAULT_RUN_DIR;
  if (name.includes("/") || name.includes("\\") || name.includes("\0") || name === "." || name === "..") {
    return null;
  }
  const full = resolve(join(runsRoot, name));
  const rootWithSep = resolve(runsRoot) + sep;
  if (!full.startsWith(rootWithSep)) return null;
  return { name, path: full };
}

async function readJsonOrNull(path) {
  try {
    return JSON.parse(await fs.readFile(path, "utf8"));
  } catch {
    return null;
  }
}

/**
 * The suite shape, enumerated from the harness on demand.
 *
 * WHY (the ARMED state). The per-run roster is written once at CELL START
 * (`run_cumulative.py:_write_gate_roster`), so between a wipe and the first cell
 * there is no roster anywhere and the wall had no denominator — it fell through
 * to "SUITE SIZE UNKNOWN" and could not draw the board's ARMED state (suite
 * known, nothing evaluated). A wiped bench is exactly when the operator most
 * wants to see what is about to be graded.
 *
 * THE HARNESS REMAINS THE ONLY AUTHOR OF THE SUITE. This shells out to the same
 * `roster.mjs` the harness itself runs, so the count is always whatever the
 * harness enumerates — add or remove a test and this follows automatically,
 * with no number to maintain in the front end. Nothing here defines a suite.
 *
 * EXECUTION-FREE (invariant I-5). `roster.mjs` shells only to `vitest list` and
 * `playwright test --list`; neither executes a test nor binds :8002, so this is
 * safe beside a live cell. Measured on this host: 1.9s.
 *
 * Written to NO file. The per-run roster is a write-once artifact pinned to the
 * run it grades; caching this one to disk would create a second, unpinned copy
 * that could go stale against the suite and silently re-baseline a comparison.
 * Recomputing is cheap and cannot drift.
 */
async function enumerateSuite(benchRoot) {
  const script = join(benchRoot, "tasks", "backgammon", "gates", "roster.mjs");
  // Written to a temp file rather than read from stdout: roster.mjs calls
  // process.exit() immediately after its final write (roster.mjs:375-377), which
  // truncates an asynchronously-flushed stdout pipe. Measured 2026-08-13 —
  // spawnSync saw all 39161 bytes while execFile saw 0, and the enumerator
  // still exited 0, so the failure looked exactly like "no suite" rather than
  // like a bug. The --out path writes atomically (tmp + rename) and is the same
  // path the harness itself uses.
  const out = join(
    tmpdir(),
    `wevibe-wall-roster-${process.pid}-${randomUUID()}.json`,
  );
  try {
    if (!(await fs.stat(script)).isFile()) return null;
    await execFileAsync("node", [script, "--out", out], {
      cwd: join(benchRoot, "tasks", "backgammon", "gates"),
      timeout: ROSTER_ENUMERATE_TIMEOUT_MS,
      maxBuffer: 8 * 1024 * 1024,
    });
    const parsed = await readJsonOrNull(out);
    return Array.isArray(parsed?.gates) && parsed.gates.length > 0 ? parsed : null;
  } catch {
    // Never fabricate. An enumerator that cannot run leaves the suite unknown,
    // which the caller reports as unwired rather than as a suite of zero.
    return null;
  } finally {
    await fs.rm(out, { force: true }).catch(() => {});
  }
}

/**
 * Read the append-only status stream.
 *
 * Bounded like every other reader here. A truncated leading line is dropped
 * (it is a fragment of a record whose whole content is unknown) and an
 * unparseable line is skipped rather than aborting the read — a run that died
 * mid-write must still yield every intact record before it.
 */
export async function readStatusRecords(path, { bytes = 4 * 1024 * 1024 } = {}) {
  let text;
  try {
    const st = await fs.stat(path);
    if (!st.isFile()) return null;
    const fh = await fs.open(path, "r");
    try {
      const start = Math.max(0, st.size - bytes);
      const len = st.size - start;
      const buf = Buffer.alloc(len);
      await fh.read(buf, 0, len, start);
      text = buf.toString("utf8");
      if (start > 0) text = text.slice(text.indexOf("\n") + 1);
    } finally {
      await fh.close().catch(() => {});
    }
  } catch {
    return null;
  }

  const out = [];
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t.startsWith("{")) continue;
    try {
      out.push(JSON.parse(t));
    } catch {
      /* fragment — skip, never abort */
    }
  }
  return out;
}

/** Attempt records, oldest first, with a usable attempt number. */
export function attemptRecords(records) {
  return (records ?? [])
    .filter((r) => r?.type === "attempt")
    .map((r) => ({ ...r, attempt: Number.isFinite(Number(r.attempt)) ? Number(r.attempt) : 1 }))
    .sort((a, b) => a.attempt - b.attempt);
}

/**
 * The attempt ceiling the live run is actually using.
 *
 * Read from the harness's own log line (`run_cumulative.pacing max_attempts=3`)
 * rather than assumed: the control plane does not own that setting and a
 * CLI-launched run may not share this process's environment. Returns null when
 * the value is not observable — the board renders "?" rather than a number that
 * might be wrong.
 */
export function maxAttemptsFrom(logText) {
  const m = /\bmax_attempts=(\d+)/.exec(String(logText ?? ""));
  return m ? Number(m[1]) : null;
}

/**
 * Was grading stopped mid-flight, and if so which phase was open?
 *
 * INVARIANT I-3 — a stall is not a verdict. Gates in flight when grading stops
 * are abandoned: never failed (they were never measured) and never passed
 * (absence is not success).
 */
export function inFlightAtStop(grading, runState) {
  if (!grading) return { stopped: false, phase: null };

  // A killed gate is unambiguous: the harness says so.
  if (grading.timed_out) {
    const open = (grading.phases ?? []).find((p) => p.status === "timeout" || p.running);
    return { stopped: true, phase: open?.phase ?? grading.phase ?? null };
  }

  // Otherwise the run must have DECLARED itself over. Only its own terminal
  // status record counts.
  //
  // `runState.state === "failed"` is deliberately NOT accepted here. That state
  // is a heuristic: `readRunState` reports "failed" whenever there is no
  // terminal record, no launcher-owned pid, and a cold log — which is exactly
  // what a CLI-launched run looks like to a control plane that did not spawn
  // it, even while its process is alive and grading. Measured on this host
  // 2026-08-13: a live campaign (pid 26263) with a 976s-silent log read as
  // `failed`, which would have marked all 14 frontend gates abandoned while
  // they were still running.
  //
  // Abandoning a gate is a VERDICT — it says the gate will never be measured.
  // A stall is not that verdict (invariant I-3); it is reported separately as
  // `grading.stalled` so the board can show the stall without resolving it.
  const declaredOver = Boolean(runState?.terminal_status);
  if (grading.grading && declaredOver) {
    return { stopped: true, phase: grading.phase ?? null };
  }
  return { stopped: false, phase: null };
}

/**
 * Fold roster + attempt outcomes + live grading into per-gate state.
 *
 * Each gate lands in exactly ONE state, which is what makes the totals sum to
 * the suite size — an assertion the board can and should check.
 *
 * `resolved_at_attempt` is the FIRST attempt in which the gate passed, which is
 * the whole blue-vs-green distinction: solved on the first try, or solved only
 * after feedback.
 */
export function foldGateStates({ roster, attempts, grading, stopped }) {
  const gates = roster?.gates ?? [];
  const activePhase = grading?.grading ? grading.phase : null;

  // gate id → ordered observations across attempts
  const observed = new Map();
  let anyOutcomesPublished = false;

  for (const record of attempts) {
    const results = Array.isArray(record.gate_results) ? record.gate_results : null;
    if (!results) continue;
    anyOutcomesPublished = true;
    for (const result of results) {
      if (!result?.id) continue;
      if (!observed.has(result.id)) observed.set(result.id, []);
      observed.get(result.id).push({ attempt: record.attempt, status: result.status });
    }
  }

  const out = gates.map((gate) => {
    const history = observed.get(gate.id) ?? [];
    const firstPass = history.find((h) => h.status === "pass");
    const measured = history.filter((h) => h.status === "fail" || h.status === "error");
    const last = history.length > 0 ? history[history.length - 1] : null;

    let state;
    let resolvedAt = null;
    if (firstPass) {
      state = "resolved";
      resolvedAt = firstPass.attempt;
    } else if (measured.length > 0) {
      state = "failing";
    } else if (stopped.stopped && stopped.phase && gate.phase === stopped.phase) {
      // In flight when grading stopped. Never fail, never pass.
      state = "abandoned";
    } else {
      state = "untested";
    }

    return {
      id: gate.id,
      phase: gate.phase,
      req: gate.req ?? null,
      title: gate.title ?? null,
      // Carried so the board can group the several tests that share one
      // requirement token without the roster pretending they are one gate.
      gate_token: gate.gate_token ?? null,
      tier: gate.tier ?? "core",
      state,
      resolved_at_attempt: resolvedAt,
      last_status: last?.status ?? null,
      // PER-PHASE-SET, not per-test: the harness announces the gate set a phase
      // is about to run, so every gate in the open phase is under test as a
      // set. Stated honestly in `live_signal` so the board renders accordingly.
      //
      // Only a gate still awaiting a verdict can be under test. A resolved gate
      // has its answer, and an abandoned one will never get one — showing
      // either as an amber pulse would claim work that is not happening.
      under_test:
        activePhase !== null
        && gate.phase === activePhase
        && (state === "untested" || state === "failing"),
    };
  });

  const tally = (state) => out.filter((g) => g.state === state).length;
  return {
    gates: out,
    totals: {
      resolved: tally("resolved"),
      failing: tally("failing"),
      untested: tally("untested"),
      abandoned: tally("abandoned"),
    },
    outcomes_published: anyOutcomesPublished,
  };
}

/**
 * Assemble GET /api/wall.
 *
 * NEVER 500, NEVER FABRICATE. A run with no roster is a real, expected state
 * (it predates the artifact). It returns ok:true with `suite.total:null`,
 * `suite.complete:false` and `unwired:["gate-roster"]` plus a reason — because
 * absent-because-unwired must stay distinguishable from zero (invariant I-2).
 */
export async function readWall({ runsRoot, runDir, launcher = null, runState = null, benchRoot = null }) {
  const target = resolveRunDir(runsRoot, runDir);
  if (!target) {
    return {
      ok: false,
      code: "bad_run_dir",
      reason: `run_dir must be a single directory name under the runs root; got ${JSON.stringify(String(runDir ?? ""))}`,
    };
  }

  // The run's own pinned roster is authoritative: it describes the suite this
  // run was actually graded against. Only when there is no run (a wiped bench,
  // before the first cell) is the suite enumerated live for the ARMED state.
  let roster = await readJsonOrNull(join(target.path, "gate-roster.json"));
  let rosterSource = roster ? "run" : null;
  if (!roster && benchRoot) {
    roster = await enumerateSuite(benchRoot);
    if (roster) rosterSource = "enumerated";
  }
  const records = await readStatusRecords(join(target.path, "manifest.status.jsonl"));
  const attempts = attemptRecords(records);

  let gate = { rows: [], status: null, log: null };
  try {
    gate = await readGateActivity(runsRoot);
  } catch {
    /* live signal is best-effort; its absence is reported below, never thrown */
  }

  const log = gate.log ?? (await newestLog(runsRoot));
  const logText = log ? await readTail(log.path) : "";

  const unwired = [];
  const reasons = {};

  if (!roster || !Array.isArray(roster.gates) || roster.gates.length === 0) {
    unwired.push("gate-roster");
    reasons["gate-roster"] =
      `no readable gate-roster.json in runs/${target.name} and the suite could not be enumerated ` +
      "from the harness — the suite size is unknowable, not zero";
  }

  const stopped = inFlightAtStop(gate.status, runState);
  const folded = roster
    ? foldGateStates({ roster, attempts, grading: gate.status, stopped })
    : { gates: [], totals: null, outcomes_published: false };

  if (roster && !folded.outcomes_published) {
    unwired.push("gate-outcomes");
    reasons["gate-outcomes"] =
      "the suite is known but no attempt record carries gate_results yet — per-gate outcomes land " +
      "in manifest.status.jsonl at attempt end (~30 min), so this is the normal state early in a cell";
  }
  if (!gate.status) {
    unwired.push("gate-live");
    reasons["gate-live"] = "no gate PROGRESS markers in the newest cell log — grading has not started";
  }

  const currentAttempt =
    attempts.length > 0
      ? attempts[attempts.length - 1].attempt
      : Number(gate.status?.attempt) || null;

  // ARMED — the suite is known and NOTHING has been evaluated.
  //
  // Stated by the server rather than inferred by the board, because the board
  // cannot otherwise tell it apart from "everything failed": both render as a
  // grid with zero resolved gates. Armed means the run has not reached the
  // grader, which is the opposite of a result.
  const armed = Boolean(roster) && !folded.outcomes_published && !gate.status;

  return {
    ok: true,
    contract_version: WALL_CONTRACT_VERSION,
    run_dir: target.name,
    armed,
    // Where the suite shape came from. "run" is the roster pinned to this run;
    // "enumerated" is the live harness suite, served when no run exists yet.
    // The board states which, so a suite shown before a cell starts is never
    // mistaken for one a run was actually graded against.
    suite_source: rosterSource,
    suite: {
      // The TRUE enumerated count, or null. Never padded toward a design comp.
      total: roster ? Number(roster.total ?? roster.gates.length) : null,
      fingerprint: roster?.suite_fingerprint ?? null,
      complete: roster ? roster.enumeration?.complete !== false : false,
      incomplete_reason: roster?.enumeration?.incomplete_reason ?? null,
      by_phase: roster?.by_phase ?? null,
      by_tier: roster?.by_tier ?? null,
      captured_at: roster?.captured_at ?? null,
    },
    attempt: {
      current: Number.isFinite(currentAttempt) ? currentAttempt : null,
      // null when not observable — the control plane does not own this setting.
      max: maxAttemptsFrom(logText),
    },
    grading: gate.status
      ? {
          active: Boolean(gate.status.grading),
          phase: gate.status.phase ?? null,
          stalled: Boolean(gate.status.stalled),
          silent_s: gate.status.silent_s ?? null,
          timed_out: Boolean(gate.status.timed_out),
          phases: gate.status.phases ?? [],
        }
      : null,
    // PER-PHASE-SET for every phase. `report.mjs` spawns each runner with
    // `spawnSync`, so per-test output is buffered until the phase has already
    // ended and cannot be a live signal; the harness instead announces each
    // phase's gate set before spawning it. The board must know which of the two
    // it is being given, so it is stated rather than implied.
    live_signal: {
      conformance: "per-phase-set",
      backend: "per-phase-set",
      frontend: "per-phase-set",
    },
    gates: folded.gates,
    totals: folded.totals,
    unwired,
    unwired_reasons: reasons,
  };
}
