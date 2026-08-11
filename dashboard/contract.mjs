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

export const CONTRACT_VERSION = "1.0";

/** Gate-level counts cluster within cell. Below this, NO delta and NO CI. */
export const MIN_CELLS_PER_ARM = 3;

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
  };
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
 * Decide whether a delta may be shown at all, and phrase it in words.
 *
 * HARD RULE: below MIN_CELLS_PER_ARM the delta stays null and the board renders
 * "COLLECTING". A number here with n=1 is the thing a skeptical engineer kills
 * you with. `ci` stays null permanently for gate rates — the samples are
 * clustered within cell and a binomial CI over them would be a lie.
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
