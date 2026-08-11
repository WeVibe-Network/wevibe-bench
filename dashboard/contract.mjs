// ─────────────────────────────────────────────────────────────────────────────
// WEVIBE BENCH DASHBOARD — JSON CONTRACT v1.0
//
// This module is the SINGLE definition of what the board consumes. Diff it
// against what the backend actually emits.
//
// THREE RULES THAT ARE NOT STYLE CHOICES:
//
//   1. Every field is nullable. `null` means NOT OBSERVED and must render as an
//      explicit state — never as 0, never as absence. Three distinct null-ish
//      states exist and must stay visually distinguishable:
//        unobserved  — the thing was not measured yet
//        unwired     — the data source that would carry it is not connected
//        zero        — it WAS measured and the value is 0 (a real result)
//
//   2. Serve ≠ outcome ≠ causation.
//        serve   claims a memory was injected into context. NOT that it helped.
//        outcome claims the episode resolved or didn't. NOT that memory caused it.
//        delta   is the ONLY causal surface on the board.
//      No panel may present a serve count as a success metric.
//
//   3. Outcome is TRI-STATE: worked | didnt_work | unobserved.
//      Silence is not a vote. `unobserved` is a third thing, not a failure.
//
// ABSENT BY RULING — do not add fields for these, they do not exist upstream:
//   - verification tiers T0–T4   (no such field in the system)
//   - ablation receipts          (not implemented)
//   - shadow recall              (would break arm comparability; killed)
// ─────────────────────────────────────────────────────────────────────────────

export const CONTRACT_VERSION = "1.1";

/** Gate-level counts cluster within cell. Below this, NO delta and NO CI. */
export const MIN_CELLS_PER_ARM = 3;

/**
 * Why a cell was kept out of the arm delta. `scored` is the only value that
 * enters the measurement; everything else is reported, never silently dropped.
 *
 *   void_instrument         provider-side truncation on a non-green terminal
 *                           attempt (RUNBOOK rule 5.10). Never a capability FAIL.
 *   resolution_unmeasurable fewer than 2 attempts, so "resolved" is underivable
 *                           by construction — see cellValidity().
 */
export const CELL_EXCLUSIONS = /** @type {const} */ ([
  "void_instrument",
  "resolution_unmeasurable",
]);

/** Permanent provenance label. Never a badge. Never a tier. */
export const ATTESTATION = "bench-mock/self-declared";

export const EPISODE_STATES = /** @type {const} */ ([
  "red",
  "red-again", // pre-trigger. the tension beat.
  "recall-fired",
  "injected",
  "green",
  "abandoned",
]);

export const GATE_STATES = /** @type {const} */ (["red", "green", "unobserved"]);

export const OUTCOME_STATES = /** @type {const} */ ([
  "worked",
  "didnt_work",
  "unobserved",
]);

export const DISPOSITIONS = /** @type {const} */ ([
  "returned",
  "below_floor",
  "over_budget_unsampled",
]);

/**
 * The empty board. Every source module merges INTO this shape, so a board with
 * zero wired sources still renders — as a deliberate all-null instrument, which
 * is exactly what the first hours of a run look like.
 */
export function emptyBoard() {
  return {
    contract_version: CONTRACT_VERSION,
    generated_at: Date.now(),

    run: {
      org_id: null,
      model: null,
      arm: null, // "on" | "off" | null
      cell_label: null,
      started_at: null,
      elapsed_s: null,
      phase: null,
      chunk: { current: null, total: null },
      attempt: { current: null, max: null },
      turns: null,
      tokens: { input: null, output: null, injected_block: null },
      state: null, // running | complete | aborted | null
    },

    provenance: {
      attestation: ATTESTATION,
      gate_mode: null, // auto-approve | human | null
      policy_version: null,
      policy_anchor_status: null,
      worker_image_fp: null,
      leader_fp: null,
      corpus: "benchmark",
    },

    arm_delta: {
      sufficient: false,
      min_cells_per_arm: MIN_CELLS_PER_ARM,
      a: armSlot(),
      b: armSlot(),
      delta: null,
      ci: null, // stays null: gates cluster within cell (see note below)
      statement: null,
      note: "gate-level results are clustered within cell — 68 gates from one cell are not 68 independent samples. no CI over gate counts.",
    },

    wall: { gates: [], totals: { a: gateTotals(), b: gateTotals() } },

    episodes: [], // newest first

    recall_moment: null,

    honesty: {
      coverage: {
        concluded: null,
        total: null,
        note: "uncovered episodes count as neither positive nor negative",
      },
      unresolved: null,
      guard_detections: {},
      recall_latency_ms: { p50: null, p95: null, n: 0 },

      // ── THE HONEST COST OF THE RUN, IN TWO PARTS ──────────────────────────
      // These are DIFFERENT costs and must not be summed into one number:
      //
      //   wasted_turns    turns burned BEFORE the gated trigger could fire
      //                   (an episode opened but never armed). The price of
      //                   requiring a second failure under the same key.
      //
      //   recovered_turns turns that really happened, burned real tokens, and
      //                   are deliberately EXCLUDED from scoring (guard-killed
      //                   + finalize-killed). Post WO-NUDGE-INF-1 the harness
      //                   nudges these indefinitely rather than dying.
      //
      // Showing only one understates what the run actually cost.
      wasted_turns: null,
      recovered_turns: null,

      serves: {
        sent: null,
        rejected: null,
        confirmed_on_chain: null,
        note: "delivery, not outcome",
      },
      transport: {
        truncations: null,
        finalize_timeouts: null,
        finalize_timeout_turns: null,
        guard_aborts: null,
        // UNBOUNDED post WO-NUDGE-INF-1. A climbing count against a phase that
        // never advances is the wedged-relay signature — the accepted failure
        // mode that now relies on someone watching the stream.
        recovery_nudges: null,
        recoveries: null,
      },
    },

    history: [],

    sources: [],
  };
}

function armSlot() {
  return {
    cells: 0,
    gates_resolved: null,
    gates_total: null,
    resolution_rate: null,
    median_turns_to_green: null,
    // Cells observed for this arm but kept OUT of the numbers above, by reason.
    // An excluded cell must never read as a measured 0 — that is the whole
    // point of the three-kinds-of-nothing rule.
    excluded: excludedSlot(),
  };
}

function excludedSlot() {
  return { total: 0, void_instrument: 0, resolution_unmeasurable: 0 };
}

function gateTotals() {
  return { red: null, green: null, unobserved: null };
}

// ── helpers used by every source module ──────────────────────────────────────

/** Coerce to int, or null. Never NaN, never a silent 0. */
export function int(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

/** Coerce to float, or null. */
export function num(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function str(v) {
  if (typeof v !== "string") return null;
  const t = v.trim();
  return t.length ? t : null;
}

/**
 * Parse a raw gate string from the harness `failed_gates` list into a stable
 * identity. The harness emits two shapes, both real:
 *
 *   "[G04] REQ-MOVES — legal-move generation (blocked points + hits)"
 *   "conformance:REQ-STATE/state.points"
 *
 * `id` must be STABLE across attempts and across arms — it is the grid slot key
 * and the thing a skeptic diffs against the raw log.
 */
export function parseGate(raw) {
  const s = String(raw ?? "").trim();
  if (!s) return null;

  const bracket = s.match(/^\[([A-Z]\d+)\]\s*([A-Z][A-Z0-9-]*)\s*[—–-]?\s*(.*)$/);
  if (bracket) {
    return {
      id: bracket[1],
      req: bracket[2] || null,
      title: bracket[3]?.trim() || null,
      raw: s,
    };
  }

  // "conformance:REQ-STATE/state.winner — /api/state response carries ..."
  // The locator is the stable identity; anything after an em/en dash is prose.
  const conf = s.match(/^conformance:([A-Z][A-Z0-9-]*)\/(.+)$/);
  if (conf) {
    const [locator, ...rest] = conf[2].split(/\s+[—–-]\s+/);
    return {
      id: `C:${locator.trim()}`,
      req: conf[1],
      title: rest.length ? rest.join(" - ").trim() : locator.trim(),
      raw: s,
    };
  }

  return { id: s.slice(0, 48), req: null, title: s, raw: s };
}

/** p50/p95 from a sample array. Returns nulls (not zeros) when empty. */
export function percentiles(samples) {
  const xs = (samples ?? []).filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (!xs.length) return { p50: null, p95: null, n: 0 };
  const at = (q) => xs[Math.min(xs.length - 1, Math.floor(q * (xs.length - 1)))];
  return { p50: at(0.5), p95: at(0.95), n: xs.length };
}

export function median(xs) {
  const a = (xs ?? []).filter((x) => Number.isFinite(x)).sort((p, q) => p - q);
  if (!a.length) return null;
  const m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
}

/**
 * Decide whether a cell may enter the arm delta at all, and why not if not.
 *
 * This MIRRORS the scorecard's canonical rule — it does not invent a second
 * one. The authority is `wevibe_bench/cumulative/run_artifacts.py` (the
 * VOID-INSTRUMENT gate, WO-NIGHT2-1b) implementing RUNBOOK rule 5.10. If that
 * rule changes, this changes with it; two divergent definitions of "does this
 * cell count" is precisely the class of drift the board exists to expose.
 *
 * Returns `{ scored: true }` or `{ scored: false, reason }`.
 *
 * VOID-INSTRUMENT (rule 5.10): a non-green terminal attempt carrying a
 * provider-side truncation signal is an instrument failure, NEVER a capability
 * FAIL. Scoring it as 0% resolution attributes the transport's failure to the
 * model — and on the control arm that manufactures apparent lift for the
 * memory arm, which is the single most damaging thing this board could do.
 * A green terminal attempt is scored regardless of earlier truncation.
 *
 * RESOLUTION_UNMEASURABLE: "resolved" is defined as red in an earlier attempt
 * and absent in the latest, so it is underivable from a single attempt. Such a
 * cell would otherwise contribute 0 to the numerator and its FULL gate count
 * to the denominator — a guaranteed 0% that is an artifact of when the cell
 * stopped, not a measurement of anything. Excluding it is the only coherent
 * choice: the same code that concedes it cannot measure resolution must not
 * then assert a rate.
 */
export function cellValidity(cell) {
  const c = cell ?? {};

  const terminalGreen = c.full_green === true;
  if (!terminalGreen) {
    const truncationSignal =
      str(c.terminal_reason) === "transport_incomplete" ||
      (int(c.length_truncations) ?? 0) > 0 ||
      (int(c.truncated_turns) ?? 0) > 0;
    if (truncationSignal) return { scored: false, reason: "void_instrument" };
  }

  const attempts = c.attempts instanceof Map ? c.attempts.size : int(c.attempt_count) ?? 0;
  if (attempts < 2) return { scored: false, reason: "resolution_unmeasurable" };

  return { scored: true, reason: null };
}

/**
 * Decide whether a delta may be shown at all, and phrase it in words.
 *
 * HARD RULE: below MIN_CELLS_PER_ARM the delta stays null and the board renders
 * "COLLECTING". A number here with n=1 is the thing a skeptical engineer kills
 * you with. `ci` stays null permanently for gate rates — the samples are
 * clustered within cell and a binomial CI over them would be a lie.
 *
 * `cells` counts SCORED cells only. Excluded cells are reported separately on
 * each arm slot and never reach this threshold — otherwise three void cells
 * would unlock a delta computed from nothing.
 */
export function finalizeDelta(delta) {
  const a = delta.a;
  const b = delta.b;
  const sufficient = a.cells >= MIN_CELLS_PER_ARM && b.cells >= MIN_CELLS_PER_ARM;
  delta.sufficient = sufficient;

  if (!sufficient || a.resolution_rate === null || b.resolution_rate === null) {
    delta.delta = null;
    delta.statement = null;
    return delta;
  }

  delta.delta = a.resolution_rate - b.resolution_rate;
  const pct = (x) => `${Math.round(x * 100)}%`;
  delta.statement =
    `memory-on resolves ${pct(a.resolution_rate)} of gates vs ` +
    `${pct(b.resolution_rate)} control, across ${a.cells} and ${b.cells} cells.`;
  return delta;
}
