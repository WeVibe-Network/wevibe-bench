// ─────────────────────────────────────────────────────────────────────────────
// WEVIBE BENCH DASHBOARD — JSON CONTRACT v2.0
//
// This module is the SINGLE definition of what the board consumes. Diff it
// against what the backend actually emits.
//
// THE QUESTION THE BOARD ANSWERS: does a growing memory corpus make the SAME
// local model finish the SAME build in fewer turns, fewer tokens and less time
// — and at what corpus size does that stop being true?
//
// FOUR RULES THAT ARE NOT STYLE CHOICES:
//
//   0. CORRECTNESS AND EFFICIENCY ARE TWO AXES, NEVER ONE NUMBER.
//        correctness  gates passed / total
//        efficiency   turns · tokens · wall time
//      They are reported adjacent, at equal weight, and are NEVER multiplied,
//      averaged, weighted or collapsed into a score. A run can be faster AND
//      worse — that is a real and important outcome, and any presentation that
//      lets faster-and-worse read as a win is a broken presentation.
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

export const CONTRACT_VERSION = "2.0";

/** Gate-level counts cluster within cell. Below this, NO delta and NO CI. */
export const MIN_CELLS_PER_ARM = 3;

/**
 * Phases per cell. THREE, not six.
 *
 *   1  BUILD   harness label `initial`
 *   2  GRADE   verdict-pass-1 / feedback-1
 *   3  GRADE   verdict-pass-2 / feedback-2
 *
 * The six work orders are CHUNKS INTERNAL TO PHASE 1. Rendering "6 phases"
 * (an earlier misreading) makes a cell in phase 2 look 1/6 done when it is
 * 2/3 done.
 */
export const PHASES_PER_CELL = 3;
export const CHUNKS_IN_BUILD = 6;

/**
 * The five states of the transfer curve. The curve renderer consumes this
 * decision; it does not re-derive it. See sources/stack-ledger.mjs.
 *
 *   no_baseline      no OFF cell exists — nothing may be drawn
 *   baseline_pending an OFF cell exists and has not produced a measurement yet
 *                    (not started, or running). NOT a failure — the floor is
 *                    on its way. Kept distinct from baseline_void because
 *                    collapsing them reported a HEALTHY RUNNING CELL as an
 *                    instrument failure (measured 2026-08-13).
 *   baseline_void    an OFF cell exists, TERMINATED, and is void-instrument:
 *                    no valid floor. A claim about a finished cell only.
 *   baseline_only    the floor, labelled n=1, and no ON runs
 *   n1_on            one ON run — a single delta, and NO LINE (two points would
 *                    imply a trend one run cannot support)
 *   curve            n≥2 — the only state where a line is legitimate
 *   regression       the newest ON cell is at or above the floor. Drawn at FULL
 *                    weight — this is the finding the benchmark exists to catch
 */
export const STACK_STATES = /** @type {const} */ ([
  "no_baseline",
  "baseline_pending",
  "baseline_void",
  "baseline_only",
  "n1_on",
  "curve",
  "regression",
]);

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
      session_id: null,
      log_silent_s: null,
      terminal_status: null,
    },

    // ── THE LONGITUDINAL SERIES ───────────────────────────────────────────
    // Owned solely by sources/stack-ledger.mjs — the ONE source that spans run
    // directories, because a cumulative campaign writes each cell to its own.
    //
    // TWO AXES, ADJACENT AND EQUAL, NEVER BLENDED:
    //   turns/tokens/wall_seconds  EFFICIENCY
    //   gates{failed,total}        CORRECTNESS
    // Nothing in this contract multiplies, averages or ranks them together. An
    // ON cell CAN be faster and worse; that must read as exactly that.
    stack: {
      id: null,
      state: null, // STACK_STATES
      baseline: null, // the ONE OFF cell. n=1 BY DESIGN, never a distribution
      baseline_n: 0,
      baseline_scorable: false,
      baseline_candidates: 0,
      runs: [], // ON cells, oldest first — the curve reads left to right
      all: [],
      excluded: { total: 0, different_experiment: 0 },
      // The denominator is the count of gates OBSERVED FAILING, not the suite
      // size. The harness publishes failed gates only (report.mjs writes no
      // total), so a suite size does not exist on disk and is never invented.
      gate_universe: null,
      gate_universe_note: null,
      // Corpus is accumulated from per-cell commit deltas. False = a cell in
      // the chain reported null, so the running total has a hole in it and is
      // not a total.
      corpus_complete: false,
      phases_per_cell: PHASES_PER_CELL,
    },

    // ── HOLD FOR REVIEW ───────────────────────────────────────────────────
    // null = no hold file. The panel renders NOTHING — not an empty box, not a
    // spinner. Absence is a specified state, not an omission.
    // RELEASE IS NEVER BLOCKED BY A DEAD UI: when ui_healthy is false no link
    // is shown (handing over a dead URL wastes the operator's time twice) but
    // the release control is always present.
    hold: null,

    // ── MEMORY PROFILE ────────────────────────────────────────────────────
    // One per ON stack, frozen at creation, never editable.
    //
    // TWO AXES, FROZEN SEPARATELY, NEVER CONFLATED (WO-BOARD-PROFILE-2):
    //
    //   subject_model   the OFF→ON pair — THE MEASUREMENT. Both arms are
    //                   always this one model. An ON cell measured against an
    //                   OFF floor on a DIFFERENT model yields a delta between
    //                   two models' capabilities, which is not the claim.
    //                   ENFORCED at /api/run/start.
    //   memory_models   producer models eligible for injection — THE
    //                   EXPERIMENT VARIABLE. NOT enforced (see below).
    //
    // `transfer` is DERIVED from those two on every read and is never stored:
    // `self` (roster == [subject] — the base measurement running today),
    // `cross`, or `mixed`. Its `direction` is `same` for self and `unranked`
    // for everything else. There is no direction picker anywhere in the UI —
    // ranking two models requires a measured floor for each, and this board
    // ranks nothing by declaration.
    //
    // `enforced` refers ONLY to the memory roster and is hardcoded false: no
    // recall request carries a producer-model allowlist today, so the roster is
    // declared and not applied. The badge disappears the day the filter ships.
    //
    // DURABLE SINCE WO-BOARD-PROFILE-1. This group is populated by
    // sources/control-plane.mjs from the control plane's on-disk profile store.
    // It was previously written ONLY by the browser's create handler and was
    // overwritten by the next poll — which is why creating a profile appeared
    // to do nothing and did not survive a refresh.
    //
    // `runs` carries the cells the control plane launched while this profile
    // was active. A cell launched at the CLI is real but UNATTRIBUTED and is
    // deliberately absent: sweeping it in would inflate the history with cells
    // never run under this allowlist.
    profile: {
      exists: false,
      id: null,
      subject_model: null,
      memory_models: [],
      transfer: null, // { kind, direction, self, foreign[], note } — derived
      created_at: null,
      enforced: false,
      stack_id: null,
      runs: [],
    },

    // Every frozen profile — `active` plus `prior`. The inspector draws prior
    // profiles as a hollow overlay that is NEVER joined by a line to the active
    // series: they were measured under a different allowlist, so connecting
    // them would draw a trend across two different experiments.
    profiles: null,

    // ── THE UNIFIED EXTRACTION QUEUE ──────────────────────────────────────
    //
    // EVERY extraction on this machine, oldest trimmed — NOT the single job the
    // control plane holds in memory, and deliberately NOT scoped to
    // `board.profile`. A profile is a READ filter over one stack; extraction is
    // a WRITE into one shared corpus, and the sessions that feed it are not
    // profile-shaped. Scoping the queue to the loaded profile hides rows that
    // are changing the very corpus the loaded profile reads from.
    //
    // Owned by sources/extraction-inventory.mjs, which reads the harness's
    // telemetry DB. `null` = no extraction has ever run on this machine (the
    // DB is created by the first one), which is a designed state and renders as
    // such — not as an error.
    //
    // SERIAL EXECUTION IS UNCHANGED. This is a unified VIEW; the control plane
    // still runs exactly one extraction at a time (control/extraction.mjs:106 —
    // two concurrent extractions against one org interleave submissions into
    // the shared corpus). Rows waiting behind the running one render PENDING.
    extraction_queue: null,

    // ── THE DEDUP DECISION VIEW ───────────────────────────────────────────
    //
    // Near-duplicate candidates, FLAGGED AND KEPT. The invariant this surface
    // exists to make visible is that flagging never drops a memory: every row
    // here was submitted. There is no mechanism anywhere in this path to
    // discard one, and the panel states that in words rather than implying it.
    //
    // `distribution` is the actual tuning instrument — it spans every scored
    // candidate, not just the flagged ones, so the question "is the threshold
    // in the right place" is answerable rather than only "what did the current
    // threshold catch".
    //
    // Carries FINGERPRINTS AND SCORES, never memory bodies: the board is a
    // streaming surface and everything on it is public forever. The bodies are
    // in the telemetry DB on the machine, and the panel names the query.
    dedup: null,

    provenance: {
      attestation: ATTESTATION,
      gate_mode: null, // auto-approve | human | null
      gate_mode_source: null,
      policy_version: null,
      policy_anchor_status: null,
      worker_image_fp: null,
      leader_fp: null,
      corpus: "benchmark",
      seed: null,
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

    // ── THE GATE SUITE, AS THE HARNESS ITSELF PUBLISHES IT ────────────────────
    //
    // Served whole by the control plane's GET /api/wall (control/wall.mjs), which
    // folds three artifacts the board must NOT stitch itself: the write-once
    // gate-roster.json, the per-attempt gate_results, and the live phase signal.
    //
    // WHY THIS IS SEPARATE FROM `wall` ABOVE. `wall` is derived locally from the
    // status stream and can only ever describe gates OBSERVED FAILING — the old
    // harness published no suite total, so a gate that passed was never drawn at
    // all. `suite` is the true enumerated universe, so it can finally answer
    // "what has NOT been tested", which no amount of client-side derivation
    // could produce from failure lists.
    //
    // NULL IS A DESIGNED STATE. `total: null` means the suite size is UNKNOWABLE
    // (a run predating the roster artifact) and must never render as 0. The
    // reason lives in `unwired_reasons` and is meant to be shown, not swallowed.
    suite: null,

    // ── THE LIVE LANE — PROVISIONAL, AND NEVER THE SCORE ──────────────────────
    //
    // Served whole by GET /api/live (control/live-surface.mjs). Two axes from
    // one worktree snapshot, taken WHILE THE AGENT IS STILL WORKING:
    //
    //   `lane`   per-gate live pass/fail for the 55 gates the lane can measure
    //   `build`  per-file population — how much of the scaffold's stub surface
    //            has actually been replaced with code
    //
    // ── THIS IS NOT A SECOND SOURCE OF TRUTH, AND THE SHAPE ENFORCES IT ──────
    //
    // RC-5 names ONE scored source. That is `suite` above, folded from
    // manifest.status.jsonl. The lane's numbers are measured off a SNAPSHOT,
    // exclude 16 gates by construction, and may be taken mid-edit — so they live
    // in their own group, carry `provisional: true` on every payload, and are
    // NEVER merged into `suite.gates[].state`. A renderer that copies a live
    // result into a suite square has broken the invariant, not fixed a gap.
    //
    // NULL IS THE NORMAL STATE. The lane is an optional instrument that the
    // operator starts by hand; most runs will not have one. Null means "no live
    // lane", which is not "everything failed" and not "nothing was tested" —
    // the authoritative wall is completely unaffected by its absence.
    //
    // STALENESS IS PUBLISHED, NOT INFERRED. `running:false` + `age_s` say the
    // lane stopped and this grid is a PAST measurement. The board must show that
    // rather than presenting a frozen grid as live.
    live: null,

    // ── THE MODEL LEDGER — one row per bench-eligible model ───────────────────
    //
    // Served whole by GET /api/models-ledger (control/models-ledger.mjs), which
    // resolves EVERY launch gate server-side: whether a baseline may be run,
    // whether a profile may be frozen, whether a cell may start under it.
    //
    // The board renders these verdicts and derives none of them. A button whose
    // enabled state disagreed with the refusal /api/run/start would actually
    // apply is worse than no button at all.
    //
    // NULL MEANS THE GATES COULD NOT BE EVALUATED, which is not the same as
    // "nothing is allowed" — the panel says so and draws no buttons rather than
    // drawing ungated ones.
    models_ledger: null,

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
