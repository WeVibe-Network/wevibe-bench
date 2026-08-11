// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: status-stream
//
// Reads the append-only per-attempt status stream (RC-5), which is the
// AUTHORITATIVE record for gates, arm, verdict and token accounting.
//
//   <runs_root>/<run_dir>/manifest.status.jsonl
//
// Record shapes actually observed on disk (verified against
// runs/cumulative.rebaseline-contract-20260810/manifest.status.jsonl):
//
//   type:"attempt"       — per attempt. carries memory_mode, failed_gates,
//                          verdict, progress{problems_before/after,
//                          resolved_count, turns, ...}
//   type:"turn_terminal" — per anomalous turn ending. carries phase, terminal,
//                          reason, retried.
//
// CADENCE WARNING (measured, not assumed): attempt records are appended at
// ATTEMPT END — roughly every 30 minutes. This module is the source of truth,
// but it is NOT the live pulse. run-log and opencode-serve carry that.
//
// THE WALL: gate identity comes from `failed_gates`. A gate is:
//   red        — present in this attempt's failed_gates
//   green      — seen red in an EARLIER attempt of this cell, absent now
//   unobserved — never seen in this arm at all
// A gate absent from attempt 1 was never failing, so it is not evidence of a
// resolution and must not be counted as green.
// ─────────────────────────────────────────────────────────────────────────────

import { join } from "node:path";
import { parseGate, int, str, median, finalizeDelta } from "../contract.mjs";
import { readTail, parseJsonl, listDir, statOrNull } from "./_runtime.mjs";

export const id = "status-stream";
export const fields = ["wall", "arm_delta", "run.arm", "history", "honesty.transport"];
export function describe() {
  return "append-only per-attempt status stream (RC-5) — authoritative for gates, arm, verdict";
}

/** Find every manifest.status.jsonl under the runs root, newest first. */
async function findStreams(runsRoot) {
  const out = [];
  for (const ent of await listDir(runsRoot)) {
    if (!ent.isDirectory()) continue;
    const p = join(runsRoot, ent.name, "manifest.status.jsonl");
    const st = await statOrNull(p);
    if (st?.isFile()) out.push({ dir: ent.name, path: p, mtime: st.mtimeMs, size: st.size });
  }
  return out.sort((a, b) => b.mtime - a.mtime);
}

export async function read(ctx) {
  const streams = await findStreams(ctx.runsRoot);
  if (!streams.length) {
    return {
      ok: false,
      reason: "no manifest.status.jsonl yet — appended at attempt end (~30 min)",
    };
  }

  // Every stream contributes cells; the newest contributes live state.
  const cells = [];
  for (const s of streams) {
    const records = parseJsonl(await readTail(s.path));
    for (const cell of cellsFromRecords(records, s.dir)) cells.push(cell);
  }
  if (!cells.length) {
    return { ok: false, reason: "status stream present but carries no attempt records" };
  }

  cells.sort((a, b) => (b.last_seen ?? 0) - (a.last_seen ?? 0));

  const wall = buildWall(cells);
  const arm_delta = buildDelta(cells);
  const history = cells
    .filter((c) => c.complete)
    .map((c) => ({
      cell_label: c.cell_label,
      arm: c.arm,
      started_at: null,
      duration_s: c.wall_seconds,
      segments: segmentsFor(c),
      triggers: [],
      gates_resolved: c.resolved_gates.length,
      verdict: c.verdict,
    }));

  const newest = cells[0];

  return {
    ok: true,
    provenance: { path: streams[0].path, mtime: streams[0].mtime, bytes: streams[0].size },
    patch: {
      run: {
        arm: newest.arm,
        cell_label: newest.cell_label,
        org_id: newest.org_id,
        attempt: { current: newest.attempt, max: 3 },
        turns: newest.turns,
        tokens: {
          input: newest.input_tokens,
          output: newest.output_tokens,
          injected_block: newest.injected_block_est_tokens,
        },
      },
      wall,
      arm_delta,
      history,
      honesty: {
        transport: {
          truncations: sum(cells, "truncated_turns"),
          finalize_timeouts: sum(cells, "finalize_timeouts"),
          finalize_timeout_turns: sum(cells, "finalize_timeout_turns"),
          guard_aborts: sum(cells, "guard_aborted_turns"),
        },
        // The honest cost of transport recovery: turns that really happened,
        // burned real tokens, and are correctly excluded from the measurement.
        // Sourced from RC-5 artifacts here rather than from the PROGRESS log.
        recovered_turns:
          sum(cells, "guard_aborted_turns") + sum(cells, "finalize_timeout_turns"),
        serves: {
          sent: nullSum(cells, "served_attempted"),
          confirmed_on_chain: nullSum(cells, "served_confirmed"),
          rejected: nullSum(cells, "served_failed"),
        },
      },
    },
  };
}

function sum(cells, key) {
  return cells.reduce((acc, c) => acc + (c[key] ?? 0), 0);
}

/** Sum that stays null when NO cell ever reported the field (unobserved ≠ 0). */
function nullSum(cells, key) {
  const seen = cells.filter((c) => c[key] !== null && c[key] !== undefined);
  if (!seen.length) return null;
  return seen.reduce((acc, c) => acc + c[key], 0);
}

/**
 * Fold a stream's records into per-cell state, keyed by sequence_index.
 * A cell's gate history is ordered by attempt so red→green is derivable.
 */
function cellsFromRecords(records, dirName) {
  const by = new Map();

  for (const r of records) {
    const seq = int(r.sequence_index) ?? 0;
    const key = `${dirName}#${seq}`;
    if (!by.has(key)) {
      by.set(key, {
        cell_label: `${dirName}-${String(seq).padStart(4, "0")}`,
        seq,
        arm: null,
        org_id: null,
        attempt: null,
        attempts: new Map(), // attempt -> Set(gate id)
        gateMeta: new Map(), // gate id -> parsed
        verdict: null,
        complete: false,
        turns: null,
        wall_seconds: null,
        input_tokens: null,
        output_tokens: null,
        injected_block_est_tokens: null,
        truncated_turns: 0,
        guard_aborted_turns: 0,
        finalize_timeout_turns: 0,
        finalize_timeouts: 0,
        served_attempted: null,
        served_confirmed: null,
        served_failed: null,
        problems_before: null,
        problems_after: null,
        last_seen: 0,
        resolved_gates: [],
      });
    }
    const c = by.get(key);

    if (r.type === "turn_terminal") {
      if (str(r.reason) === "stream_finalize_timeout") c.finalize_timeouts += 1;
      continue;
    }

    // attempt record (the archived first-generation records carry no `type`)
    const gates = Array.isArray(r.failed_gates)
      ? r.failed_gates
      : Array.isArray(r.progress?.failed_gates)
        ? r.progress.failed_gates
        : null;

    const attempt = int(r.attempt);
    if (attempt !== null && gates) {
      const set = new Set();
      for (const raw of gates) {
        const g = parseGate(raw);
        if (!g) continue;
        set.add(g.id);
        if (!c.gateMeta.has(g.id)) c.gateMeta.set(g.id, g);
      }
      c.attempts.set(attempt, set);
      c.attempt = Math.max(c.attempt ?? 0, attempt);
    }

    c.arm = str(r.memory_mode) ?? c.arm;
    c.org_id = str(r.org_id) ?? c.org_id;
    c.verdict = str(r.verdict) ?? c.verdict;
    if (r.terminal_outcome === true || str(r.terminal_reason)) c.complete = true;

    const p = r.progress ?? {};
    c.turns = int(p.turns) ?? c.turns;
    c.wall_seconds = Math.round(Number(p.wall_seconds ?? 0)) || c.wall_seconds;
    c.input_tokens = int(r.work_input_tokens) ?? int(p.input_tokens) ?? c.input_tokens;
    c.output_tokens = int(r.work_output_tokens) ?? int(p.output_tokens) ?? c.output_tokens;
    c.injected_block_est_tokens = int(r.injected_block_est_tokens) ?? c.injected_block_est_tokens;
    c.truncated_turns = Math.max(c.truncated_turns, int(r.truncated_turns) ?? 0);
    c.guard_aborted_turns = Math.max(c.guard_aborted_turns, int(r.guard_aborted_turns) ?? 0);
    // WO-NUDGE-INF-1 wiring fix: the second scoring-turn subtrahend is now
    // carried on the authoritative status stream, so the exclusion is
    // reconstructable from RC-5 artifacts alone rather than only from a log
    // line. Absent on records written before that fix — stays 0, never null.
    c.finalize_timeout_turns = Math.max(
      c.finalize_timeout_turns,
      int(r.finalize_timeout_turns) ?? 0,
    );
    c.served_attempted = int(p.served_attempted) ?? c.served_attempted;
    c.served_confirmed = int(p.served_confirmed) ?? c.served_confirmed;
    c.served_failed = int(p.served_failed) ?? c.served_failed;
    c.problems_before = int(p.problems_before) ?? c.problems_before;
    c.problems_after = int(p.problems_after) ?? c.problems_after;
    c.last_seen += 1;
  }

  for (const c of by.values()) c.resolved_gates = resolvedGates(c);
  return [...by.values()];
}

/**
 * Gates that were red in an earlier attempt and are absent in the latest.
 * This is the ONLY defensible definition of "resolved" from this stream: a gate
 * never observed red cannot be evidence that anything was fixed.
 */
function resolvedGates(cell) {
  const attempts = [...cell.attempts.keys()].sort((a, b) => a - b);
  if (attempts.length < 2) return [];
  const first = cell.attempts.get(attempts[0]);
  const last = cell.attempts.get(attempts[attempts.length - 1]);
  return [...first].filter((g) => !last.has(g));
}

/** Every gate id ever observed, in first-seen order, so grid slots never move. */
function buildWall(cells) {
  const meta = new Map();
  const order = [];
  for (const c of [...cells].reverse()) {
    for (const [gid, g] of c.gateMeta) {
      if (!meta.has(gid)) {
        meta.set(gid, g);
        order.push(gid);
      }
    }
  }

  const arms = { on: pickCell(cells, "on"), off: pickCell(cells, "off") };
  const gates = order.map((gid) => {
    const g = meta.get(gid);
    return {
      id: g.id,
      req: g.req,
      title: g.title,
      a: gateState(arms.on, gid),
      b: gateState(arms.off, gid),
      a_flipped_at_attempt: flipAttempt(arms.on, gid),
      b_flipped_at_attempt: flipAttempt(arms.off, gid),
    };
  });

  return {
    gates,
    totals: { a: tally(gates, "a"), b: tally(gates, "b") },
  };
}

function pickCell(cells, arm) {
  return cells.find((c) => c.arm === arm) ?? null;
}

function gateState(cell, gid) {
  if (!cell || !cell.attempts.size) return "unobserved";
  const attempts = [...cell.attempts.keys()].sort((a, b) => a - b);
  const last = cell.attempts.get(attempts[attempts.length - 1]);
  if (last.has(gid)) return "red";
  const everRed = attempts.some((a) => cell.attempts.get(a).has(gid));
  return everRed ? "green" : "unobserved";
}

function flipAttempt(cell, gid) {
  if (!cell) return null;
  const attempts = [...cell.attempts.keys()].sort((a, b) => a - b);
  let wasRed = false;
  for (const a of attempts) {
    const red = cell.attempts.get(a).has(gid);
    if (wasRed && !red) return a;
    wasRed = wasRed || red;
  }
  return null;
}

function tally(gates, side) {
  const t = { red: 0, green: 0, unobserved: 0 };
  for (const g of gates) t[g[side]] += 1;
  return t;
}

/**
 * Per-arm resolution: resolved gates / gates ever red, aggregated over cells.
 * Cells are the unit; `cells` is reported at equal weight to the rate, because
 * gates cluster within cell (see contract note).
 */
function buildDelta(cells) {
  const build = (arm) => {
    const mine = cells.filter((c) => c.arm === arm && c.attempts.size);
    if (!mine.length) {
      return { cells: 0, gates_resolved: null, gates_total: null, resolution_rate: null, median_turns_to_green: null };
    }
    let resolved = 0;
    let total = 0;
    for (const c of mine) {
      const attempts = [...c.attempts.keys()].sort((a, b) => a - b);
      const everRed = new Set();
      for (const a of attempts) for (const g of c.attempts.get(a)) everRed.add(g);
      resolved += c.resolved_gates.length;
      total += everRed.size;
    }
    return {
      cells: mine.length,
      gates_resolved: resolved,
      gates_total: total,
      resolution_rate: total > 0 ? resolved / total : null,
      median_turns_to_green: median(mine.filter((c) => c.verdict === "PASS").map((c) => c.turns)),
    };
  };

  return finalizeDelta({
    sufficient: false,
    min_cells_per_arm: 3,
    a: build("on"),
    b: build("off"),
    delta: null,
    ci: null,
    statement: null,
    note: "gate-level results are clustered within cell — 68 gates from one cell are not 68 independent samples. no CI over gate counts.",
  });
}

/** Build phase vs error phase, from what the stream actually distinguishes. */
function segmentsFor(cell) {
  const total = cell.wall_seconds ?? 0;
  if (!total) return [];
  const attempts = [...cell.attempts.keys()].length || 1;
  const build = total / attempts;
  return [
    { kind: "build", from_s: 0, to_s: Math.round(build) },
    { kind: "error", from_s: Math.round(build), to_s: total },
  ];
}
