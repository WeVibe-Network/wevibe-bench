// ─────────────────────────────────────────────────────────────────────────────
// GATE WALL — one served surface: the gate roster folded with test outcomes.
//
// WHAT THIS IS
//
// Two artifacts, one fold:
//
//   gate-roster.json        the suite — every gate that exists (write-once)
//   manifest.status.jsonl   gate_results — pass/fail per gate, per attempt
//
// A gate lands in exactly one of three states:
//
//   passing    the last completed test run passed it
//   failing    the last completed test run failed it
//   untested   no completed test run has a result for it
//
// That is the whole model. `passing` and `failing` are the two colours; the
// third is the absence of a measurement and must never read as either.
//
// ── NO PHASE LOGIC LIVES HERE ────────────────────────────────────────────────
//
// The harness partitions its suite into conformance/backend/frontend phases and
// announces phase boundaries in its log. NONE of that reaches this surface. The
// wall previously tracked the open phase to paint in-flight gates amber and
// stopped-mid-phase gates slate; that produced two more states, a live log
// tailer, and a stall detector, all to describe the GRADER's situation rather
// than the gates'. A gate's phase is not a fact about the gate's result.
//
// ── THE VERDICT IS THE LAST COMPLETED TEST RUN, AND NOTHING ELSE ─────────────
//
// `gate_results` is published when a test run finishes. Between runs the wall
// holds its last state. There is deliberately no live, provisional, or
// in-flight signal: a square either carries a real recorded verdict or it
// carries none.
//
// ── AND, SEPARATELY, THE TRAJECTORY ──────────────────────────────────────────
//
// Each gate also carries `first_pass_attempt` and `ever_failed`, folded across
// ALL attempts rather than only the last. This is not a second verdict and must
// never be read as one — `state` remains the single answer to "does this gate
// pass". The trajectory answers a different question the bench is actually for:
// how many attempts the model needed to get there.
//
// This is a deliberate, narrow reversal of 03a2650 ("make the gate wall dumb
// again"), which removed an attempt axis. What that commit correctly killed was
// a SECOND, DISAGREEING derivation of gate STATE — in-flight ambers and slate
// squares describing the grader's situation rather than the gate's. Two facts
// about recorded history, derived once here and rendered without reinterpretation,
// are not that: no square's pass/fail meaning changes, and nothing here reads a
// live signal. The phase axis (conformance/backend/frontend) stays out entirely;
// a gate's phase is still not a fact about its result.
//
// READ-ONLY. Reads two files. Never writes, never spawns, never signals.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join, resolve, sep } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

/** How long the enumerator may take before the suite is reported unknown. */
const ROSTER_ENUMERATE_TIMEOUT_MS = 60_000;

/** The contract version the board can assert against. */
export const WALL_CONTRACT_VERSION = 2;

/** The published per-gate state vocabulary. Three states, and no more. */
export const GATE_STATES = /** @type {const} */ (["passing", "failing", "untested"]);

/**
 * LAST-RESORT run directory, and nothing more.
 *
 * This is NOT "the current run". Campaigns are per-model
 * (`campaign.mjs:campaignDirName` → `runs/cumulative-<model>`) and the legacy
 * directory is archived on a wipe, so the caller resolves the ACTIVE run from
 * the cell log (`server.mjs:activeRunDir`) and passes it. This constant only
 * covers a bench with no cell log at all, where naming a directory that happens
 * not to exist is the honest outcome: the reader reports `unwired` with its
 * reason rather than inventing results.
 *
 * Serving this as a silent default is what put `0/71 passing` over an empty
 * wall while the run's own artifacts recorded 16 passing and 2 failing.
 */
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
 * WHY. The per-run roster is written once at CELL START
 * (`run_cumulative.py:_write_gate_roster`), so between a wipe and the first cell
 * there is no roster anywhere and the wall has no denominator. A wiped bench is
 * exactly when the operator most wants to see what is about to be graded.
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
 *
 * MEMOIZED IN PROCESS, THOUGH — "recomputing is cheap" was wrong. Measured on
 * this host: 1.903s per call, and this runs on EVERY /api/wall request, which
 * the dashboard polls twice a second. That single call was ~1.9s of the source's
 * 2000ms budget (sources/_runtime.mjs READ_TIMEOUT_MS), so the control-plane
 * read intermittently timed out, `board.control` came back null, and EVERY
 * control on the board went dead — including the one that starts a run. An
 * operator clicking [+ baseline] in that window got
 * `control_plane_unwired: the board does not know where the control plane is`
 * for a control plane that was healthy and answering in 2ms.
 *
 * The cache is in MEMORY and short-lived, which keeps both properties: nothing
 * unpinned is ever written to disk, and the suite still cannot go stale for
 * longer than the TTL. A run's own pinned roster always wins and is never
 * cached here.
 */
const ROSTER_CACHE_TTL_MS = 30_000;
// KEYED BY benchRoot, never global. A single global slot let one root's result
// answer for a DIFFERENT root — caught by "the suite denominator is never
// fabricated when the harness cannot be reached", which pointed a benchRoot
// with no gates dir at a cache warmed by the real one and got a fabricated
// suite. That test is exactly right: a denominator invented from another tree
// is the fabrication invariant I-2 forbids.
const rosterCache = new Map(); // benchRoot -> { at, value }

async function enumerateSuite(benchRoot) {
  // Serve from memory while fresh. A null result is cached too: an enumerator
  // that cannot run must not be retried at 1.9s a poll — that is the exact cost
  // this cache exists to remove, and the failure is reported as unwired either
  // way.
  const hit = rosterCache.get(benchRoot);
  if (hit && Date.now() - hit.at < ROSTER_CACHE_TTL_MS) return hit.value;
  const value = await enumerateSuiteUncached(benchRoot);
  rosterCache.set(benchRoot, { at: Date.now(), value });
  return value;
}

async function enumerateSuiteUncached(benchRoot) {
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
 * Fold the roster against the outcomes of the LAST COMPLETED test run.
 *
 * THE LATEST RESULT WINS, and only the latest. A gate that failed attempt 1 and
 * passed attempt 2 is passing — the wall reports the current state of the code,
 * not the history of how it got there. Earlier attempts are superseded, not
 * merged: a gate that regressed from pass to fail must read red, which a
 * "passed at least once" fold would hide.
 *
 * A gate with no result in any attempt is `untested`. That is the absence of a
 * measurement, and it is why the totals sum to the suite size — an assertion the
 * board can and should check.
 */
export function foldGateStates({ roster, attempts }) {
  const gates = roster?.gates ?? [];

  // gate id → the most recent status seen, scanning attempts oldest → newest so
  // the last write wins.
  const latest = new Map();
  // ── THE TRAJECTORY, ALONGSIDE THE VERDICT ──────────────────────────────
  //
  // The verdict answers "does this gate pass now". The trajectory answers "how
  // many tries did that take" — a different measurement, and the one the bench
  // exists to make: a gate green on the first attempt and a gate green only
  // after two rounds of repair are not the same result, and `attempts_to_green`
  // has always been recorded per ATTEMPT without ever being visible per GATE.
  //
  // Two facts, both derived here so no surface derives them twice:
  //   first_pass_attempt  the earliest attempt whose result for this gate was
  //                       `pass`, or null if none ever was.
  //   ever_failed         whether any attempt recorded an outright failure.
  //                       `not_run` is EXCLUDED — an unmeasured gate has not
  //                       been shown to fail, and colouring it as damaged would
  //                       repeat the absence-reads-as-a-verdict defect.
  const firstPass = new Map();
  const everFailed = new Map();
  let anyOutcomesPublished = false;

  for (const record of attempts) {
    const results = Array.isArray(record.gate_results) ? record.gate_results : null;
    if (!results) continue;
    anyOutcomesPublished = true;
    for (const result of results) {
      if (!result?.id) continue;
      latest.set(result.id, result.status);

      if (result.status === "pass") {
        if (!firstPass.has(result.id)) firstPass.set(result.id, record.attempt);
      } else if (result.status !== "not_run" && result.status !== undefined && result.status !== null) {
        everFailed.set(result.id, true);
      }
    }
  }

  const out = gates.map((gate) => {
    const status = latest.get(gate.id) ?? null;

    // THE THREE-WAY SPLIT, AND THE TWO WAYS IT CAN GO WRONG.
    //
    // `not_run` means the runner never reached this gate — the phase aborted
    // before executing it. That is the ABSENCE of a measurement and must land in
    // `untested`. Calling it a failure invents a red square nobody measured.
    //
    // `error` means the gate ran and could not complete. It has not been shown
    // to work, so it is a failure. Calling it untested would let a broken gate
    // hide in the "not yet" bucket forever.
    //
    // Anything absent from the results array is untested, for the same reason
    // `not_run` is: silence is not a pass, and this is the defect the roster was
    // built to remove.
    let state;
    if (status === "pass") state = "passing";
    else if (status === null || status === undefined || status === "not_run") state = "untested";
    else state = "failing";

    return {
      id: gate.id,
      req: gate.req ?? null,
      title: gate.title ?? null,
      state,
      // Published for EVERY gate, including failing and untested ones, so the
      // board never has to infer a missing field's meaning.
      first_pass_attempt: firstPass.get(gate.id) ?? null,
      ever_failed: everFailed.get(gate.id) === true,
    };
  });

  const tally = (state) => out.filter((g) => g.state === state).length;
  return {
    gates: out,
    totals: {
      passing: tally("passing"),
      failing: tally("failing"),
      untested: tally("untested"),
    },
    outcomes_published: anyOutcomesPublished,
  };
}

/**
 * Assemble GET /api/wall.
 *
 * NEVER 500, NEVER FABRICATE. A run with no roster is a real, expected state
 * (it predates the artifact). It returns ok:true with `suite.total:null` and
 * `unwired:["gate-roster"]` plus a reason — because absent-because-unwired must
 * stay distinguishable from zero (invariant I-2).
 */
export async function readWall({ runsRoot, runDir, benchRoot = null }) {
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
  // before the first cell) is the suite enumerated live.
  let roster = await readJsonOrNull(join(target.path, "gate-roster.json"));
  let rosterSource = roster ? "run" : null;
  if (!roster && benchRoot) {
    roster = await enumerateSuite(benchRoot);
    if (roster) rosterSource = "enumerated";
  }
  const records = await readStatusRecords(join(target.path, "manifest.status.jsonl"));
  const attempts = attemptRecords(records);

  const unwired = [];
  const reasons = {};

  if (!roster || !Array.isArray(roster.gates) || roster.gates.length === 0) {
    unwired.push("gate-roster");
    reasons["gate-roster"] =
      `no readable gate-roster.json in runs/${target.name} and the suite could not be enumerated ` +
      "from the harness — the suite size is unknowable, not zero";
  }

  const folded = roster
    ? foldGateStates({ roster, attempts })
    : { gates: [], totals: null, outcomes_published: false };

  if (roster && !folded.outcomes_published) {
    unwired.push("gate-outcomes");
    reasons["gate-outcomes"] =
      "the suite is known but no attempt record carries gate_results yet — per-gate outcomes land " +
      "in manifest.status.jsonl when a test run completes, so this is the normal state early in a cell";
  }

  const currentAttempt = attempts.length > 0 ? attempts[attempts.length - 1].attempt : null;

  // ── IS THE RATIO A SCORE, OR A LOWER BOUND ON AN UNKNOWN? ────────────────
  //
  // A gate runner that aborts leaves gates unmeasured for HARNESS reasons. The
  // wall already draws those squares as untested, which is right — but the
  // HEADLINE still reads `16/71 passing`, and a ratio reads as a result.
  //
  // The harness now states this per attempt (`report.mjs:gradability`). It is
  // passed through untouched, exactly like every other verdict on this surface:
  // the wall reports gradability, it does not decide it.
  //
  // `null` means UNKNOWN — an attempt recorded before the field existed. It is
  // not `true`: vouching for runs that nothing checked is the fabrication this
  // file exists to refuse.
  const last = attempts.length > 0 ? attempts[attempts.length - 1] : null;
  const gradable = last ? (last.gradable ?? null) : null;

  return {
    ok: true,
    contract_version: WALL_CONTRACT_VERSION,
    run_dir: target.name,
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
      captured_at: roster?.captured_at ?? null,
    },
    // Which test run these results came from. Context for the squares, not a
    // state of them.
    attempt: Number.isFinite(currentAttempt) ? currentAttempt : null,
    // Whether the last attempt's numbers are a measurement. Three states:
    // true, false, and null for "recorded before anyone asked".
    gradable,
    ungradable_reason: last?.ungradable_reason ?? null,
    aborted_runners: Array.isArray(last?.aborted_runners) ? last.aborted_runners : [],
    gates: folded.gates,
    totals: folded.totals,
    unwired,
    unwired_reasons: reasons,
  };
}
