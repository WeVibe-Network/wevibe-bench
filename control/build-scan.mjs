// ─────────────────────────────────────────────────────────────────────────────
// BUILD SCAN — the file-population axis
//
// WHY THIS EXISTS (WO-LIVE-GATES)
//
// Between "the agent starts working" and "the grader publishes an attempt" the
// board has NOTHING to say about the artifact. Measured on this host: a real
// attempt ran ~30 min before any gate outcome existed, and for that entire
// window the GATE WALL sat at zero gates. The event feed shows that files are
// being EDITED; it cannot show whether they are being FILLED.
//
// Those are different facts. An agent can edit a file forty times and leave
// every function a stub. This module measures SUBSTANCE, not activity.
//
// ── THE DENOMINATOR IS DERIVED, NEVER HAND-MAINTAINED ────────────────────────
//
// The task scaffold ships each required function as a literal
// `throw new Error("not implemented")`. Measured 2026-08-13:
//
//     scaffold/src/game.ts    12 stubs   121 lines      golden:   0   357
//     scaffold/src/ai.ts       5 stubs    56 lines      golden:   0   257
//     scaffold/src/server.ts  21 stubs   244 lines      golden:   0   484
//     scaffold/public/app.js   0 stubs    12 lines      golden:   0   586
//                            ─────────
//                             38 stubs
//
// So the denominator is a PROPERTY OF THE SCAFFOLD, read at scan time. Add or
// remove a required function and this follows automatically, with no number to
// maintain anywhere. That is the same rule the gate roster follows.
//
// ── TWO METRICS, NAMED, NEVER BLENDED ────────────────────────────────────────
//
// `public/app.js` carries no stubs — it is a comment block the worker replaces
// wholesale. Its fill CANNOT be a stub ratio, so it is measured as a line ratio
// against the golden reference and `metric` says so explicitly. Collapsing the
// two into one unlabelled 0..1 would let a reader believe a line-ratio file had
// its functions implemented. The two are carried as distinct `metric` values
// and the front end is expected to state which it is showing.
//
// ── FILL IS A MEASUREMENT, NOT A PREDICTION ──────────────────────────────────
//
// `fill` says how much of the scaffold's stub surface is gone. It does NOT say
// how correct the code is, and it MUST NOT be rendered as a progress bar toward
// a passing grade — a file can reach fill 1.0 and fail every gate. The gate
// wall is the correctness axis; this is the construction axis. Adjacent, equal,
// never multiplied together.
//
// READ-ONLY: reads files under a snapshot directory. Never writes, never spawns.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { join } from "node:path";

/** The contract version the board can assert against. */
export const BUILD_CONTRACT_VERSION = 1;

/** The published per-file state vocabulary. */
export const BUILD_STATES = /** @type {const} */ ([
  "untouched",
  "stub",
  "partial",
  "complete",
]);

/** The published metric vocabulary — which measurement a file's `fill` IS. */
export const BUILD_METRICS = /** @type {const} */ (["stub-ratio", "line-ratio"]);

/**
 * The stub marker the scaffold uses.
 *
 * Matched as a plain substring on the WHOLE FILE, exactly as the scaffold
 * writes it (`throw new Error("not implemented")`). A looser regex over
 * identifiers would also match prose in CONTRACT.md-style comments and inflate
 * the denominator.
 */
const STUB_MARKER = 'throw new Error("not implemented")';

/** Files whose population is tracked. Derived from what the scaffold ships. */
const TRACKED = ["src/game.ts", "src/ai.ts", "src/server.ts", "public/app.js"];

function countStubs(text) {
  let n = 0;
  let i = 0;
  for (;;) {
    const at = text.indexOf(STUB_MARKER, i);
    if (at < 0) return n;
    n += 1;
    i = at + STUB_MARKER.length;
  }
}

/** Non-blank, non-trivial line count — the line-ratio metric's unit. */
function countLines(text) {
  let n = 0;
  for (const line of text.split("\n")) {
    if (line.trim().length > 0) n += 1;
  }
  return n;
}

async function readOrNull(path) {
  try {
    return await fs.readFile(path, "utf8");
  } catch {
    return null;
  }
}

/**
 * Measure one file against its scaffold baseline and golden reference.
 *
 * RETURNS null WHEN THE FILE IS ABSENT FROM THE TARGET. An absent file is not a
 * file with zero fill — the worker may not have created it yet, or may have
 * renamed it. The caller reports absence as its own state rather than as 0.0,
 * because "not there" and "there but empty" are different facts (invariant I-2).
 */
export function measureFile({ rel, target, baseline, reference }) {
  if (target === null) {
    return {
      path: rel,
      state: "untouched",
      metric: null,
      fill: null,
      stubs_remaining: null,
      stubs_initial: null,
      lines: null,
      reference_lines: reference === null ? null : countLines(reference),
      reason: "file absent from the worktree — not the same as a file with no content",
    };
  }

  const baseStubs = baseline === null ? 0 : countStubs(baseline);
  const lines = countLines(target);
  const referenceLines = reference === null ? null : countLines(reference);

  // ── STUB-RATIO: the precise metric, used wherever the scaffold defines stubs.
  if (baseStubs > 0) {
    const remaining = countStubs(target);
    // Clamped at both ends: a worker that ADDS stubs must not produce a
    // negative fill, and one that deletes the file's stubs plus more must not
    // exceed 1.0. Either would be an out-of-contract number on the wire.
    const fill = Math.min(1, Math.max(0, (baseStubs - remaining) / baseStubs));
    return {
      path: rel,
      // `stub` and `complete` are the two endpoints; anything between is
      // partial. Untouched is reserved for an ABSENT file, so a file that is
      // present and still fully stubbed reads as `stub` — present, not missing.
      state: remaining === baseStubs ? "stub" : remaining === 0 ? "complete" : "partial",
      metric: "stub-ratio",
      fill,
      stubs_remaining: remaining,
      stubs_initial: baseStubs,
      lines,
      reference_lines: referenceLines,
      reason: null,
    };
  }

  // ── LINE-RATIO: the fallback, and it is LABELLED as a different measurement.
  //
  // Used for files the scaffold ships as a comment block rather than as stubs
  // (public/app.js). Without a reference there is no denominator at all, and
  // fill is null rather than guessed.
  if (referenceLines === null || referenceLines === 0) {
    return {
      path: rel,
      state: lines > 0 ? "partial" : "stub",
      metric: null,
      fill: null,
      stubs_remaining: null,
      stubs_initial: null,
      lines,
      reference_lines: null,
      reason: "no golden reference for this file — fill is unknowable, not zero",
    };
  }

  const baselineLines = baseline === null ? 0 : countLines(baseline);
  // Progress is measured from the SCAFFOLD's own line count, not from zero: the
  // scaffold's 12-line comment block is not 2% of the work already done.
  const span = Math.max(1, referenceLines - baselineLines);
  const fill = Math.min(1, Math.max(0, (lines - baselineLines) / span));
  return {
    path: rel,
    state: lines <= baselineLines ? "stub" : fill >= 1 ? "complete" : "partial",
    metric: "line-ratio",
    fill,
    stubs_remaining: null,
    stubs_initial: null,
    lines,
    reference_lines: referenceLines,
    reason: null,
  };
}

/**
 * Scan a worktree (or a snapshot of one) against the scaffold + golden pair.
 *
 * `scaffoldDir` supplies the DENOMINATOR and `goldenDir` the line reference.
 * Both are read fresh on every scan so a change to the task definition is
 * picked up without a cache to invalidate.
 */
export async function scanBuild({ targetDir, scaffoldDir, goldenDir, observedAt = Date.now() }) {
  const files = [];
  for (const rel of TRACKED) {
    const [target, baseline, reference] = await Promise.all([
      readOrNull(join(targetDir, rel)),
      readOrNull(join(scaffoldDir, rel)),
      readOrNull(join(goldenDir, rel)),
    ]);
    files.push(measureFile({ rel, target, baseline, reference }));
  }

  // ── TOTALS ────────────────────────────────────────────────────────────────
  //
  // The headline fill is the STUB TOTAL across every stub-ratio file — one
  // ratio over one population (38 stubs on this task), NOT a mean of per-file
  // fills. A mean would weight a 5-stub file the same as a 21-stub file and
  // report a number that matches no countable thing on disk.
  //
  // Line-ratio files are deliberately EXCLUDED from this total and counted
  // separately. Mixing a line ratio into a stub ratio produces a figure that is
  // neither, and the front end could not honestly label it.
  let stubsRemaining = 0;
  let stubsInitial = 0;
  for (const f of files) {
    if (f.metric !== "stub-ratio") continue;
    stubsRemaining += f.stubs_remaining;
    stubsInitial += f.stubs_initial;
  }

  const tally = (state) => files.filter((f) => f.state === state).length;

  return {
    ok: true,
    contract_version: BUILD_CONTRACT_VERSION,
    observed_at: observedAt,
    // Where the measurement was taken. A snapshot is a CLONE of the worktree,
    // never the live worktree — the agent is mid-edit and a half-written file
    // is noise, not a measurement.
    source: "worktree-snapshot",
    files,
    totals: {
      stubs_remaining: stubsRemaining,
      stubs_initial: stubsInitial,
      // NULL, never 1.0, when the scaffold defines no stubs at all. A task with
      // no denominator has no completion fraction, and 1.0 would read as "done".
      fill: stubsInitial > 0 ? (stubsInitial - stubsRemaining) / stubsInitial : null,
      untouched: tally("untouched"),
      stub: tally("stub"),
      partial: tally("partial"),
      complete: tally("complete"),
      tracked: files.length,
    },
    // Set by the lane from the live event stream; null whenever the stream is
    // unavailable, which is the normal case for a run whose events are not
    // persisted. Nothing on the board may DEPEND on this being present.
    editing_now: null,
  };
}
