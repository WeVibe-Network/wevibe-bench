// ─────────────────────────────────────────────────────────────────────────────
// ARM-DELTA CELL VALIDITY — regression coverage for the fabricated-lift defect
//
// Zero dependencies. Stock `node --test`, no install, no build step:
//
//     cd wevibe-bench/dashboard && node --test
//
// WHAT THIS PINS, AND WHY IT EXISTS
//
// The board's arm delta is the ONLY causal surface it has. Before this test the
// aggregation counted every cell that carried gate data, with no validity
// filter at all. Two mechanisms combined into one lie:
//
//   1. A cell aborted by transport failure (VOID-INSTRUMENT, RUNBOOK rule 5.10)
//      entered the control arm as a real observation.
//   2. Any cell with a single attempt was pinned at exactly 0% — `resolvedGates`
//      needs two attempts to derive "resolved", so such a cell contributed 0 to
//      the numerator and its FULL gate count to the denominator.
//
// At MIN_CELLS_PER_ARM the board would then publish its first-ever delta
// showing memory-on beating a control arm that was crippled by arithmetic
// rather than outmatched. These tests drive the REAL source module over REAL
// status-stream records so they fail if that behaviour ever returns.
//
// The fixtures below are the shape actually observed on disk, including the
// live burned cell `cumulative-0000-off-...` that exposed the defect.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { cellValidity, finalizeDelta, MIN_CELLS_PER_ARM } from "./contract.mjs";
import { read } from "./sources/status-stream.mjs";

// ── fixture builders ─────────────────────────────────────────────────────────

/** One attempt record in the shape run_cumulative.py emits. */
function attempt({
  seq = 0,
  attempt: n = 1,
  mode = "off",
  gates = ["[G01] REQ-A — a", "[G02] REQ-B — b"],
  fullGreen = false,
  terminalReason = "gates_failed",
  lengthTruncations = 0,
  truncatedTurns = 0,
  verdict = "FAIL",
  turns = 100,
}) {
  return {
    type: "attempt",
    schema_version: 1,
    sequence_index: seq,
    attempt: n,
    memory_mode: mode,
    org_id: "wevibe-org-0",
    verdict,
    terminal_outcome: false,
    terminal_reason: terminalReason,
    termination_reason: terminalReason,
    length_truncations: lengthTruncations,
    truncated_turns: truncatedTurns,
    failed_gates: gates,
    progress: { turns, wall_seconds: 1000, full_green: fullGreen, failed_gates: gates },
  };
}

/** Write records to a temp runs root and read them through the real module. */
async function boardFrom(records) {
  const root = await mkdtemp(join(tmpdir(), "wevibe-dash-test-"));
  try {
    const dir = join(root, "cumulative");
    await mkdir(dir, { recursive: true });
    await writeFile(
      join(dir, "manifest.status.jsonl"),
      records.map((r) => JSON.stringify(r)).join("\n") + "\n",
      "utf8",
    );
    const res = await read({ runsRoot: root, benchRoot: root, config: {} });
    assert.equal(res.ok, true, `source should read: ${res.reason ?? ""}`);
    return res.patch;
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

/** A cell that legitimately scores: 2 attempts, one gate fixed, no truncation. */
function scoredCell(seq, mode) {
  return [
    attempt({ seq, attempt: 1, mode, gates: ["[G01] REQ-A — a", "[G02] REQ-B — b"] }),
    attempt({ seq, attempt: 2, mode, gates: ["[G02] REQ-B — b"] }),
  ];
}

// ── cellValidity, in isolation ───────────────────────────────────────────────

test("cellValidity: a truncated non-green cell is VOID-INSTRUMENT, not a 0%", () => {
  const v = cellValidity({
    full_green: false,
    terminal_reason: "harness_error",
    truncated_turns: 11,
    length_truncations: 151,
    attempt_count: 3,
  });
  assert.equal(v.scored, false);
  assert.equal(v.reason, "void_instrument");
});

test("cellValidity: transport_incomplete alone is a truncation signal", () => {
  const v = cellValidity({
    full_green: false,
    terminal_reason: "transport_incomplete",
    attempt_count: 3,
  });
  assert.equal(v.scored, false);
  assert.equal(v.reason, "void_instrument");
});

test("cellValidity: a GREEN terminal attempt scores despite earlier truncation", () => {
  // Rule 5.10 is explicit: a green terminal attempt is scored regardless of
  // earlier truncation. Voiding a success because the transport hiccuped
  // mid-run would discard a real result.
  const v = cellValidity({
    full_green: true,
    terminal_reason: "gates_green",
    truncated_turns: 9,
    length_truncations: 40,
    attempt_count: 2,
  });
  assert.equal(v.scored, true);
});

test("cellValidity: a single-attempt cell is resolution_unmeasurable", () => {
  const v = cellValidity({ full_green: false, terminal_reason: "gates_failed", attempt_count: 1 });
  assert.equal(v.scored, false);
  assert.equal(v.reason, "resolution_unmeasurable");
});

test("cellValidity: a clean multi-attempt cell scores", () => {
  const v = cellValidity({ full_green: false, terminal_reason: "gates_failed", attempt_count: 2 });
  assert.equal(v.scored, true);
  assert.equal(v.reason, null);
});

// ── the incident, replayed through the real source module ────────────────────

test("THE INCIDENT: the burned harness_error cell never enters the control arm", async () => {
  // Verbatim shape of runs/cumulative/manifest.status.jsonl record 0, the cell
  // that was live in arm B as `cells:1, resolution_rate:0` when this was found.
  const patch = await boardFrom([
    attempt({
      seq: 0,
      attempt: 1,
      mode: "off",
      gates: Array.from({ length: 23 }, (_, i) => `[G${String(i + 1).padStart(2, "0")}] REQ-X — g`),
      terminalReason: "harness_error",
      lengthTruncations: 151,
      truncatedTurns: 11,
      turns: 158,
    }),
    { type: "extraction", sequence_index: 0, memory_mode: "off", extraction_state: "invoked_cut_off" },
  ]);

  const b = patch.arm_delta.b;
  assert.equal(b.cells, 0, "a void-instrument cell must not count as an observation");
  assert.equal(b.resolution_rate, null, "must be unobserved, NOT a measured 0%");
  assert.equal(b.gates_total, null);
  assert.equal(b.excluded.total, 1);
  assert.equal(b.excluded.void_instrument, 1);
});

test("a single-attempt cell is excluded rather than scored 0%", async () => {
  const patch = await boardFrom([attempt({ seq: 0, attempt: 1, mode: "off" })]);
  const b = patch.arm_delta.b;
  assert.equal(b.cells, 0);
  assert.equal(b.resolution_rate, null, "0/2 gates from one attempt is an artifact, not a rate");
  assert.equal(b.excluded.resolution_unmeasurable, 1);
});

test("a legitimate two-attempt cell still scores, and the rate is real", async () => {
  const patch = await boardFrom(scoredCell(0, "off"));
  const b = patch.arm_delta.b;
  assert.equal(b.cells, 1);
  assert.equal(b.gates_resolved, 1, "G01 went red -> absent");
  assert.equal(b.gates_total, 2);
  assert.equal(b.resolution_rate, 0.5);
  assert.equal(b.excluded.total, 0);
});

// ── the reason the WO exists: no fabricated lift ─────────────────────────────

test("FABRICATED LIFT: void control cells cannot unlock a delta", async () => {
  // Three ON cells that genuinely score, and three OFF cells that are all
  // void-instrument. Pre-fix this published a delta of +50 points against a
  // control arm crippled by arithmetic. Post-fix the control arm has zero
  // scored cells, so the board must stay in COLLECTING.
  const records = [];
  for (const seq of [0, 1, 2]) records.push(...scoredCell(seq, "on"));
  for (const seq of [3, 4, 5]) {
    records.push(
      attempt({
        seq,
        attempt: 1,
        mode: "off",
        terminalReason: "harness_error",
        truncatedTurns: 7,
      }),
    );
  }

  const patch = await boardFrom(records);
  const d = patch.arm_delta;

  assert.equal(d.a.cells, 3, "the ON arm legitimately reached threshold");
  assert.equal(d.b.cells, 0, "every control cell was void");
  assert.equal(d.b.excluded.total, 3);
  assert.equal(d.sufficient, false, "threshold must count SCORED cells only");
  assert.equal(d.delta, null, "no delta may be published against an empty control arm");
  assert.equal(d.statement, null);
});

test("MIN_CELLS_PER_ARM still gates a delta built from valid cells", () => {
  const under = finalizeDelta({
    a: { cells: MIN_CELLS_PER_ARM - 1, resolution_rate: 0.9 },
    b: { cells: MIN_CELLS_PER_ARM, resolution_rate: 0.3 },
  });
  assert.equal(under.sufficient, false);
  assert.equal(under.delta, null);

  const at = finalizeDelta({
    a: { cells: MIN_CELLS_PER_ARM, resolution_rate: 0.9 },
    b: { cells: MIN_CELLS_PER_ARM, resolution_rate: 0.3 },
  });
  assert.equal(at.sufficient, true);
  assert.ok(Math.abs(at.delta - 0.6) < 1e-9);
  assert.equal(at.ci, undefined, "no CI is ever computed over gate counts");
});

// ── D4: non-attempt records must not be folded in as attempts ────────────────

test("an extraction record does not corrupt cell state", async () => {
  const patch = await boardFrom([
    ...scoredCell(0, "off"),
    {
      type: "extraction",
      sequence_index: 0,
      memory_mode: "off",
      extraction_state: "invoked_cut_off",
      extraction_candidate_count: null,
      verdict: "SHOULD-BE-IGNORED",
    },
  ]);
  const b = patch.arm_delta.b;
  assert.equal(b.cells, 1, "the cell still scores");
  assert.equal(b.resolution_rate, 0.5, "the extraction record changed nothing");
});

test("an archived record carrying no `type` is still read as an attempt", async () => {
  // First-generation records predate the `type` field. Dropping them would
  // silently discard historical cells.
  const [a1, a2] = scoredCell(0, "off");
  delete a1.type;
  delete a2.type;
  const patch = await boardFrom([a1, a2]);
  assert.equal(patch.arm_delta.b.cells, 1);
  assert.equal(patch.arm_delta.b.resolution_rate, 0.5);
});

// ── the wall is deliberately NOT filtered ────────────────────────────────────

test("the wall still shows a void cell's gates — it is display, not measurement", async () => {
  // The wall claims "these gates were red", which is TRUE for a void cell and
  // checkable line-by-line against the log. Only the causal surface (the delta)
  // requires validity. Blanking the wall would hide a real observation.
  const patch = await boardFrom([
    attempt({ seq: 0, attempt: 1, mode: "off", terminalReason: "harness_error", truncatedTurns: 11 }),
  ]);
  assert.equal(patch.wall.totals.b.red, 2);
  assert.equal(patch.arm_delta.b.cells, 0);
});
