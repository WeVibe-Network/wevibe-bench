// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: stack-ledger
//
// THE ONE SOURCE THAT DELIBERATELY SPANS RUN DIRECTORIES.
//
// `_runtime.activeRun()` exists to stop cross-run aggregation, and its comment
// says plainly: "A source that legitimately spans runs must say so explicitly;
// nothing does today." This module is that source, and this header is that
// explicit statement.
//
// WHY IT MUST SPAN RUNS. The board's whole question is longitudinal:
//
//     does a growing corpus make the SAME model finish the SAME build in fewer
//     turns — and at what corpus size does that stop being true?
//
// A cumulative campaign is one OFF baseline followed by ON runs 1..N feeding
// the same growing corpus, executed strictly serially. Each cell is invoked as
// its own `run` and lands in its OWN `runs/<dir>/`. Scoped to one directory,
// the transfer curve can only ever plot ONE point and the answer is
// unanswerable by construction.
//
// WHY THAT IS NOT THE BUG activeRun() PREVENTS. The defect activeRun() was
// written against was UNIONING GATES from unrelated runs onto one wall — losing
// track of which cell a fact came from. Nothing here does that. Every cell is
// kept SEPARATE and ATTRIBUTED, one ledger row and one curve point each,
// carrying its own run directory. The wall stays scoped to the active run.
// This module aggregates the SEQUENCE; it never merges two cells' facts.
//
// ── STACK MEMBERSHIP: WHAT COUNTS AS "THE SAME EXPERIMENT" ──────────────────
//
// A curve is a lie if it plots cells that were not comparable. Membership is
// keyed on the facts that must hold constant for a delta to mean anything:
//
//     org_id + task + roster_hash + seed
//
// `roster_hash` is the harness's own identity for the model set (drift in it is
// the error the CLI reports as "roster hash drift detected"), so a model swap
// mid-campaign starts a NEW stack rather than silently bending an existing
// curve. Cells that do not match the newest cell's key are EXCLUDED and
// COUNTED, never dropped in silence.
//
// ── TWO HONESTY RULINGS, EACH FORCED BY WHAT THE HARNESS ACTUALLY WRITES ────
//
// 1. GATES HAVE NO DENOMINATOR. Verified at the producer:
//    `tasks/backgammon/gates/report.mjs:596-610` builds its report from
//    `failed_gates` + `problems` and writes NO total. Nothing downstream adds
//    one. The design comp shows "90/114"; 114 DOES NOT EXIST IN THE DATA.
//
//    Fabricating it would be the exact dishonesty this board exists to prevent,
//    so the denominator is reported as what it provably is — the count of
//    DISTINCT GATES EVER OBSERVED FAILING in this stack — and it is labelled
//    `observed` so nobody reads it as the suite size. When a cell has failures
//    but the universe is not yet meaningful, `gates.total` stays null and the
//    board renders "not measured", never "/0" and never a guess.
//
// 2. CORPUS SIZE IS A DELTA, NOT A LEVEL. `manifest.session_records[].
//    corpus_delta` is `len(applied_result.committed_ids)`
//    (`wevibe_bench/cumulative/sequencer.py:452`) — memories COMMITTED BY THAT
//    CELL. No artifact on disk carries the corpus TOTAL at recall time. So the
//    running total is accumulated here from the deltas and marked
//    `basis:"accumulated_deltas"`, with `complete:false` the moment any cell in
//    the chain reports null — because a sum with a hole in it is not a total.
//    The hub DB could carry the true level, but that source ships disabled;
//    when it is on it may supersede this with `basis:"hub_db"`.
//
// Every number below is either read from an artifact or explicitly null.
// ─────────────────────────────────────────────────────────────────────────────

import { int, num, str, parseGate } from "../contract.mjs";
import { readTail, parseJsonl, readJson, listDir, statOrNull } from "./_runtime.mjs";
import { join } from "node:path";

export const id = "stack-ledger";
export const fields = ["stack"];
export function describe() {
  return "cross-run cumulative stack — the longitudinal series behind the transfer curve";
}

/** Phases per cell. Not 6 — chunks are internal to phase 1. */
const PHASES_PER_CELL = 3;

export async function read(ctx) {
  const runs = await collectRuns(ctx.runsRoot);
  if (!runs.length) {
    return { ok: false, reason: "no run directories with a manifest under runs root" };
  }

  const cells = [];
  for (const r of runs) cells.push(...cellsInRun(r));
  if (!cells.length) {
    return { ok: false, reason: "run directories present but none carry a scheduled cell" };
  }

  // Newest first by the cell's own clock, so "the stack" is anchored on the
  // most recent thing the operator actually ran.
  cells.sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));

  const anchor = cells[0];
  const key = stackKey(anchor);
  const mine = [];
  const excluded = { total: 0, different_experiment: 0 };
  for (const c of cells) {
    if (stackKey(c) === key) mine.push(c);
    else {
      excluded.total += 1;
      excluded.different_experiment += 1;
    }
  }

  // Oldest first: this is a SEQUENCE, and the curve is read left to right.
  mine.sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0));

  const universe = gateUniverse(mine);
  let corpus = 0;
  let corpusComplete = true;

  const rows = mine.map((c, i) => {
    // Corpus is the level BEFORE this cell ran — what it could actually recall.
    // Its own commits land after it, so adding them first would credit a cell
    // with memories it never saw.
    const corpusAtRecall = corpusComplete ? corpus : null;
    if (c.corpus_delta === null) corpusComplete = false;
    else corpus += c.corpus_delta;

    return {
      seq: i,
      sequence_index: c.sequence_index,
      run_dir: c.run_dir,
      arm: c.arm,
      model: c.model,
      org_id: c.org_id,
      created_at: c.created_at,

      phases: { done: c.phases_done, total: PHASES_PER_CELL },
      chunk: c.chunk,

      // EFFICIENCY — never combined with correctness below.
      turns: c.turns,
      tokens: c.total_tokens,
      wall_seconds: c.wall_seconds,

      // CORRECTNESS — never combined with efficiency above.
      gates: {
        failed: c.failed_now,
        // The observed universe, NOT the suite size. See ruling 1.
        total: universe.size || null,
        basis: universe.size ? "observed_failures_in_stack" : null,
        resolved: c.resolved,
      },

      corpus: {
        // Memories recallable when this cell ran.
        at_recall: corpusAtRecall,
        // Memories this cell committed.
        delta: c.corpus_delta,
        basis: "accumulated_deltas",
        complete: corpusAtRecall !== null,
      },

      verdict: c.verdict,
      state: c.state,
      terminal_reason: c.terminal_reason,
      // Truncation-driven instrument failure. A void cell is NOT a capability
      // result and must never be plotted as one.
      void_instrument: c.void_instrument,
    };
  });

  // THE FLOOR. Every Δ on the board is measured against this one cell, so
  // WHICH OFF cell is chosen is a measurement decision, not a display detail.
  //
  // A void-instrument cell is a transport failure, not a capability result
  // (RUNBOOK 5.10). Anchoring the floor on one would make every ON cell's Δ a
  // comparison against a number the harness itself refuses to score — and
  // because a void cell typically stops early with FEWER turns, it would
  // manufacture a fake regression across the entire curve.
  //
  // So: the newest OFF cell that actually completed and is not void. When no
  // such cell exists the newest OFF cell is still surfaced — carrying its own
  // `void_instrument` flag — and `baseline_scorable` says plainly that the
  // stack has no floor worth measuring against. Hiding it would look like "no
  // baseline ran"; scoring it would be worse.
  const offs = rows.filter((r) => r.arm === "off");
  const scorable = offs.filter((r) => !r.void_instrument && r.state === "complete");
  const baseline = scorable.length
    ? scorable[scorable.length - 1]
    : (offs.length ? offs[offs.length - 1] : null);
  const on = rows.filter((r) => r.arm === "on");

  return {
    ok: true,
    provenance: {
      path: ctx.runsRoot,
      runs: mine.length,
      spans_runs: true,
      stack_key: key,
    },
    patch: {
      stack: {
        id: key,
        // n=1 BY DESIGN and labelled so at the line, never in a footnote.
        baseline,
        baseline_n: baseline ? 1 : 0,
        // Whether the floor may carry a Δ at all. False = a baseline cell
        // exists but is void-instrument, so no delta on this board is valid.
        baseline_scorable: Boolean(baseline && !baseline.void_instrument && baseline.state === "complete"),
        // Every OFF cell ever run in this stack. More than one is not an
        // error — a re-baseline is a real, sanctioned event — but it IS a fact
        // the operator must see rather than have silently resolved for them.
        baseline_candidates: offs.length,
        runs: on,
        all: rows,
        excluded,
        gate_universe: universe.size || null,
        gate_universe_note:
          "denominator is the count of distinct gates ever observed failing in this stack. " +
          "the harness publishes failed gates only — no suite total exists on disk.",
        corpus_complete: corpusComplete,
        state: stackState(baseline, on),
        phases_per_cell: PHASES_PER_CELL,
      },
    },
  };
}

/**
 * Which of the five curve states the stack is in. The curve renderer must not
 * re-derive this — one definition, one place.
 */
function stackState(baseline, on) {
  if (!baseline) return "no_baseline";
  // A void floor cannot anchor a comparison, so the stack reads as having no
  // usable baseline even though a cell exists. The panel says which.
  if (baseline.void_instrument || baseline.turns === null) return "baseline_void";
  const plottable = on.filter((r) => !r.void_instrument && r.turns !== null);
  if (!plottable.length) return "baseline_only";
  if (plottable.length === 1) return "n1_on";
  // Regression = the newest plottable ON cell is at or above the floor it was
  // meant to beat. Stated on turns, the curve's default metric.
  const newest = plottable[plottable.length - 1];
  if (newest.turns >= baseline.turns) return "regression";
  return "curve";
}

function stackKey(c) {
  return [c.org_id ?? "-", c.task ?? "-", c.roster_hash ?? "-", c.seed ?? "-"].join("|");
}

/** Every distinct gate id ever seen failing across the stack's cells. */
function gateUniverse(cells) {
  const u = new Set();
  for (const c of cells) for (const g of c.gates_ever) u.add(g);
  return u;
}

async function collectRuns(runsRoot) {
  const out = [];
  for (const ent of await listDir(runsRoot)) {
    if (!ent.isDirectory()) continue;
    const dir = join(runsRoot, ent.name);
    const manifestPath = join(dir, "manifest.json");
    if (!(await statOrNull(manifestPath))?.isFile()) continue;
    const manifest = await readJson(manifestPath);
    if (!manifest) continue;
    const statusPath = join(dir, "manifest.status.jsonl");
    const statusStat = await statOrNull(statusPath);
    const status = statusStat?.isFile()
      ? parseJsonl(await readTail(statusPath))
      : [];
    out.push({ name: ent.name, dir, manifest, status });
  }
  return out;
}

/**
 * A run directory holds one or more scheduled cells. Identity comes from the
 * SCHEDULE (written at run start, so a cell that never produced a status record
 * is still a real, visible, unmeasured cell) and measurement from the status
 * stream folded by sequence_index.
 */
function cellsInRun(run) {
  const m = run.manifest;
  const schedule = Array.isArray(m.schedule) ? m.schedule : [];
  if (!schedule.length) return [];

  const created = Date.parse(str(m.created_at) ?? "") || null;
  const task = str(m.task);
  const rosterHash = str(m.roster_hash);
  const seed = int(m.seed);
  const orgId = str(m.org_id);

  const records = new Map(); // sequence_index -> folded measurement
  for (const r of run.status) {
    if (r.type !== undefined && r.type !== "attempt") continue;
    const seq = int(r.sequence_index) ?? 0;
    if (!records.has(seq)) {
      records.set(seq, {
        attempts: new Map(),
        gates_ever: new Set(),
        failed_now: null,
        verdict: null,
        turns: null,
        total_tokens: null,
        wall_seconds: null,
        terminal: false,
        terminal_reason: null,
        full_green: false,
        length_truncations: 0,
        truncated_turns: 0,
      });
    }
    const c = records.get(seq);
    const p = r.progress ?? {};

    const raw = Array.isArray(r.failed_gates)
      ? r.failed_gates
      : Array.isArray(p.failed_gates)
        ? p.failed_gates
        : null;
    const attempt = int(r.attempt);
    if (attempt !== null && raw) {
      const set = new Set();
      for (const g of raw) {
        const parsed = parseGate(g);
        if (!parsed) continue;
        set.add(parsed.id);
        c.gates_ever.add(parsed.id);
      }
      c.attempts.set(attempt, set);
    }

    c.verdict = str(r.verdict) ?? c.verdict;
    c.turns = int(p.turns) ?? c.turns;
    // total_tokens is the operator's number (scorecard.py). Falls back to the
    // work_* pair only when the aggregate is absent — never silently summed
    // from a partial pair, which would under-report.
    c.total_tokens =
      int(p.total_tokens) ??
      int(r.work_total_tokens) ??
      c.total_tokens;
    const wall = num(p.wall_seconds);
    if (wall !== null) c.wall_seconds = Math.round(wall);
    if (r.terminal_outcome === true || str(r.terminal_reason)) c.terminal = true;
    c.terminal_reason = str(r.terminal_reason) ?? c.terminal_reason;
    c.full_green = p.full_green === true;
    c.length_truncations = Math.max(c.length_truncations, int(r.length_truncations) ?? 0);
    c.truncated_turns = Math.max(c.truncated_turns, int(r.truncated_turns) ?? 0);
  }

  // corpus_delta lives on the session record, keyed by the same index.
  const sessions = new Map();
  for (const s of Array.isArray(m.session_records) ? m.session_records : []) {
    sessions.set(int(s.sequence_index) ?? 0, s);
  }

  return schedule.map((s) => {
    const seq = int(s.sequence_index) ?? 0;
    const meas = records.get(seq) ?? null;
    const sess = sessions.get(seq) ?? null;

    const attempts = meas ? [...meas.attempts.keys()].sort((a, b) => a - b) : [];
    const lastSet = attempts.length ? meas.attempts.get(attempts[attempts.length - 1]) : null;
    const firstSet = attempts.length ? meas.attempts.get(attempts[0]) : null;

    return {
      run_dir: run.name,
      sequence_index: seq,
      created_at: created,
      task,
      roster_hash: rosterHash,
      seed,
      org_id: str(s.org_id) ?? orgId,
      arm: str(s.memory_mode) ?? str(sess?.memory_mode),
      model: str(s.provider_pin) ?? str(s.model) ?? str(sess?.model),

      // PHASES, from what the stream proves happened. A cell with no attempt
      // record has run no gradeable phase — that is 0, and it is measured.
      phases_done: meas ? Math.min(attempts.length, PHASES_PER_CELL) : 0,
      chunk: { current: null, total: 6 },

      turns: meas?.turns ?? null,
      total_tokens: meas?.total_tokens ?? null,
      wall_seconds: meas?.wall_seconds ?? null,

      failed_now: lastSet ? lastSet.size : null,
      resolved:
        firstSet && lastSet && attempts.length >= 2
          ? [...firstSet].filter((g) => !lastSet.has(g)).length
          : null,
      gates_ever: meas ? meas.gates_ever : new Set(),

      corpus_delta: int(sess?.corpus_delta),
      verdict: meas?.verdict ?? null,
      state: !meas ? "not_started" : meas.terminal ? "complete" : "running",
      terminal_reason: meas?.terminal_reason ?? null,

      // RUNBOOK rule 5.10, mirrored — a non-green terminal attempt carrying a
      // provider-side truncation signal is an INSTRUMENT failure, never a
      // capability FAIL, and must not be plotted as a data point.
      void_instrument: meas
        ? !meas.full_green &&
          (meas.terminal_reason === "transport_incomplete" ||
            meas.length_truncations > 0 ||
            meas.truncated_turns > 0)
        : false,
    };
  });
}
