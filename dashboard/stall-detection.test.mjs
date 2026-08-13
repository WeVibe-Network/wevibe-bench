// ─────────────────────────────────────────────────────────────────────────────
// HANG DETECTION — a wedged cell must never render as "nothing happening"
//
//     cd wevibe-bench/dashboard && node --test
//
// WHY THIS EXISTS
//
// WO-NUDGE-INF-1 removed the self-termination that used to end a wedged run, on
// purpose, and moved the job elsewhere:
//
//   "a permanently wedged relay is no longer self-terminating, so hang
//    detection is the operator's / poller's job on the status stream, never a
//    nudge cap."  — RUNBOOK.md:329
//
// THE JOB WAS MOVED TO A ROLE THAT DID NOT EXIST. Nothing polled, and nothing
// rendered a stall. MEASURED 2026-08-13: a cell logged
//
//   step=transport-recovery phase=feedback-1 terminal=transport_error
//   action=nudge nudge=1 budget=unbounded
//
// at 11:39:52 and wrote nothing for the next 41 minutes, until the host went
// down at 12:21. Throughout that window `run-log` reported `state:"running"`
// while computing `log_silent_s` on the adjacent line, and the topbar — which
// had no branch for a stalled run — rendered the wedged cell as
// "no run observed". Not merely unreported: reported as nothing happening.
//
// WHAT THIS PINS
//
//  1. Silence past STALL_THRESHOLD_S names the state `stalled`.
//  2. A COMPLETE run is never restated as stalled — a terminal record is a real
//     ending, and silence after one is expected.
//  3. The topbar renders a stall LOUDLY, and never as "no run observed".
//  4. A stall is not a failure claim. It reports the measured silence only.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { STALL_THRESHOLD_S, emptyBoard } from "./contract.mjs";
import { read as readRunLog } from "./sources/run-log.mjs";

const noop = () => {};
const stubEl = () => ({
  innerHTML: "",
  style: {},
  classList: { add: noop, remove: noop },
  appendChild: noop,
  addEventListener: noop,
  childNodes: [],
  content: { childNodes: [] },
});
globalThis.document = {
  addEventListener: noop,
  getElementById: stubEl,
  createElement: stubEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  body: { appendChild: noop },
};
globalThis.window = {
  addEventListener: noop,
  matchMedia: () => ({ matches: false, addEventListener: noop }),
};
globalThis.setInterval = noop;

const { renderTopbar } = await import("./panels/chrome.js");

/**
 * Write a cell log whose MTIME is `silentFor` seconds in the past.
 *
 * MTIME, NOT A TIMESTAMP IN THE TEXT. That is the whole measurement — the
 * harness writes naive local timestamps and parsing them produced a constant
 * phantom 7.1h silence in a UTC container (run-log.mjs:213-224).
 */
function runRootWithLog({ silentFor, lines }) {
  const root = mkdtempSync(join(tmpdir(), "stall-"));
  const runs = join(root, "runs");
  mkdirSync(join(runs, "cumulative"), { recursive: true });
  const log = join(runs, "off-cell-20260813T172000.log");
  writeFileSync(log, lines.join("\n") + "\n");
  const when = new Date(Date.now() - silentFor * 1000);
  utimesSync(log, when, when);
  return { root, runs };
}

// The real shape of the wedged run's tail, verbatim from the 2026-08-13 log.
const WEDGED = [
  "2026-08-13 11:35:10 PROGRESS step=serve-drive-end phase=feedback-1 run_dir=cumulative",
  "2026-08-13 11:39:52 PROGRESS step=transport-recovery phase=feedback-1 "
    + "terminal=transport_error action=nudge nudge=1 budget=unbounded",
];

test("silence past the threshold names the run STALLED", async () => {
  const { root, runs } = runRootWithLog({ silentFor: STALL_THRESHOLD_S + 60, lines: WEDGED });
  try {
    const res = await readRunLog({ runsRoot: runs });
    assert.equal(res.ok, true);
    assert.equal(res.patch.run.state, "stalled");
    assert.ok(res.patch.run.log_silent_s >= STALL_THRESHOLD_S);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("silence UNDER the threshold is still running — a quiet run is not a wedged one", async () => {
  const { root, runs } = runRootWithLog({ silentFor: STALL_THRESHOLD_S - 60, lines: WEDGED });
  try {
    const res = await readRunLog({ runsRoot: runs });
    assert.equal(res.patch.run.state, "running");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("a COMPLETE run is never restated as stalled", async () => {
  // A terminal record is a real ending. Silence after one is expected, and
  // calling it a stall would raise an alarm on every finished cell on disk.
  const { root, runs } = runRootWithLog({
    silentFor: STALL_THRESHOLD_S * 10,
    lines: [...WEDGED, JSON.stringify({ status: "ok", memory_mode: "off" })],
  });
  try {
    const res = await readRunLog({ runsRoot: runs });
    assert.equal(res.patch.run.state, "complete");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

// ── the rendering half: the defect was that this had no branch ──────────────

function topbarFor(run) {
  const board = { ...emptyBoard(), run: { ...emptyBoard().run, ...run } };
  return renderTopbar(board, { stale: false, lastError: null });
}

test("a stalled cell renders LOUDLY, and never as 'no run observed'", () => {
  const html = topbarFor({ state: "stalled", log_silent_s: 2520, model: "qwen3.6-35b-a3b-bench" });
  assert.ok(html.includes("CELL STALLED"), "the wedge must be named on the most-read surface");
  assert.ok(html.includes("danger"), "and carry the danger hue — this is the alarm");
  assert.ok(
    !html.includes("no run observed"),
    "THE MEASURED DEFECT: a wedged cell fell through to 'no run observed'",
  );
});

test("the stall chip reports the measured silence and claims nothing else", () => {
  const html = topbarFor({ state: "stalled", log_silent_s: 2520 });
  // 2520s of silence, stated. The harness may still recover — recovery is
  // unbounded by design — so the chip must not say failed, dead, or aborted.
  assert.ok(/SILENT/.test(html));
  for (const overclaim of ["FAILED", "DEAD", "ABORTED", "CRASHED"]) {
    assert.ok(!html.includes(overclaim), `a stall is not a verdict: "${overclaim}"`);
  }
});

test("running and complete are unchanged by the new branch", () => {
  assert.ok(topbarFor({ state: "running", elapsed_s: 120 }).includes("RUNNING"));
  assert.ok(topbarFor({ state: "complete", terminal_status: "ok" }).includes("CELL COMPLETE"));
  assert.ok(topbarFor({ state: null }).includes("no run observed"));
});

test("the board and the control plane agree on the threshold", async () => {
  // The two tiers cannot import from each other, so the constant is duplicated.
  // A silent divergence means the board says "running" about a run the control
  // plane already calls stalled.
  const src = await import("node:fs/promises").then((fs) =>
    fs.readFile(new URL("../control/contract.mjs", import.meta.url), "utf8"),
  );
  const m = /export const STALL_THRESHOLD_S = (\d+)/.exec(src);
  assert.ok(m, "control/contract.mjs must still declare STALL_THRESHOLD_S");
  assert.equal(Number(m[1]), STALL_THRESHOLD_S);
});
