// ─────────────────────────────────────────────────────────────────────────────
// GATE CHOREOGRAPHY — the per-square state machine over the ATTEMPT axis
//
// WHY THIS EXISTS (WO-LIVE-GATES)
//
// The wall already decides a gate's RESTING state (resolved / failing /
// untested / abandoned, control/wall.mjs foldGateStates). That is a snapshot:
// it says where a gate stands, not how it got there. The choreography directive
// asks the wall to carry HISTORY —
//
//   - first time a gate is worked on, it must look different from every later
//     time (pulsing white vs pulsing its existing colour);
//   - a gate that was PASSING and is now FAILING must be permanently marked,
//     because a regression is the single most important event on this board and
//     it is invisible in a resting state (it just looks red, like a gate that
//     never passed);
//   - a gate that was FAILING and now PASSES is solid green.
//
// None of that is derivable from the current attempt alone. It is a fold over
// the ORDERED history of attempts, which is exactly what this module computes.
//
// ── THREE INDEPENDENT AXES, DELIBERATELY NOT ONE ENUM ────────────────────────
//
// A single `visual` string was the first design and it is wrong: "pulsing white"
// and "pulsing red" and "solid red with a star" are three combinations of two
// or three separate facts, and an enum forces the front end to parse meaning
// back out of a compound token. So the server publishes the axes SEPARATELY:
//
//   fill    what colour the square IS          none|white|blue|green|red|slate
//   motion  whether and how it moves           still|pulse
//   mark    a persistent overlay glyph         null|regression
//
// The front end renders fill × motion × mark. It never re-derives them, and it
// never needs a lookup table mapping 18 compound names back to 3 facts.
//
// ── WHY `first_worked` IS NOT `attempt === 1` ────────────────────────────────
//
// A gate is "being worked on for the FIRST time" when this is the first attempt
// in which it has EVER been executed. That is not the same as attempt 1: a
// phase can abort before reaching a gate, so a gate may first execute in
// attempt 2. Keying on the attempt NUMBER would paint such a gate as "seen
// before" on the very first look at it. It is keyed on the observation history
// instead, which is the fact the directive is actually about.
//
// ── THE REGRESSION MARK IS PERMANENT, BY DESIGN ──────────────────────────────
//
// Once a gate has passed and later failed, `mark:"regression"` is set for the
// REST OF THE RUN and is never cleared — not by a later pass, not by a later
// attempt. That is the directive ("star persists forever for that run") and it
// is also the honest reading: the fact that this gate is unstable across
// attempts remains true regardless of where it happens to land at the end. A
// mark that cleared on the next pass would hide flapping, which is the exact
// pathology it exists to expose.
//
// PURE. No I/O, no clock, no randomness. Same history in, same choreography out.
// ─────────────────────────────────────────────────────────────────────────────

/** The contract version the board can assert against. */
export const CHOREOGRAPHY_CONTRACT_VERSION = 1;

/** The published fill vocabulary — what colour the square IS. */
export const FILLS = /** @type {const} */ ([
  "none", // never executed: a dashed outline, not a weaker fail
  "white", // being worked on for the very first time
  "blue", // passed on the first attempt that ever ran it
  "green", // passed after having previously failed
  "red", // failing
  "slate", // abandoned mid-test — no verdict, ever
]);

/** The published motion vocabulary. Motion NEVER means "passing". */
export const MOTIONS = /** @type {const} */ (["still", "pulse"]);

/** The published mark vocabulary. A mark is an overlay, never a colour. */
export const MARKS = /** @type {const} */ ([null, "regression"]);

/**
 * Ordered observations for one gate across attempts.
 *
 * `history` is [{ attempt, status }] oldest first, where status is one of
 * pass / fail / error / not_run. `not_run` is FILTERED OUT here rather than
 * treated as a failure: a gate its phase never reached was not measured, and
 * counting it as a fail would fabricate a verdict (invariant I-3).
 */
function executed(history) {
  return (history ?? []).filter((h) => h.status === "pass" || h.status === "fail" || h.status === "error");
}

/**
 * Choreograph ONE gate.
 *
 * `underTest` is the live signal: the grader is running this gate RIGHT NOW.
 * `abandoned` means grading stopped while it was in flight.
 *
 * The order of the branches is the whole specification, so it is stated:
 *   1. abandoned  — a verdict-free terminal state outranks everything
 *   2. under test — the live fact outranks any resting verdict
 *   3. resting    — the fold over completed attempts
 */
export function choreographGate({ history, underTest = false, abandoned = false }) {
  const runs = executed(history);
  const everRan = runs.length > 0;

  // ── THE REGRESSION FACT, computed over the WHOLE history ─────────────────
  //
  // True as soon as a pass is followed at any later point by a fail. Computed
  // before any branch returns, so every branch can carry it — a gate that
  // regressed and is now being re-run keeps its star while it pulses.
  let sawPass = false;
  let regressed = false;
  for (const r of runs) {
    if (r.status === "pass") sawPass = true;
    else if (sawPass) regressed = true;
  }
  const mark = regressed ? "regression" : null;

  // ── 1. ABANDONED ─────────────────────────────────────────────────────────
  // A stall is not a verdict. Slate, still, and it keeps any star it earned.
  if (abandoned) {
    return {
      fill: "slate",
      motion: "still",
      mark,
      first_worked: false,
      ever_ran: everRan,
      regressed,
      resolved_at_attempt: null,
    };
  }

  // ── 2. UNDER TEST — the live branch, and the heart of the directive ───────
  //
  // FIRST TIME EVER: pulsing WHITE. The square has no prior colour to pulse,
  // and white is reserved for exactly this — it is the only moment in a gate's
  // life when the board has no information about it at all.
  //
  // LATER TIMES: pulse the EXISTING colour. The square keeps whatever verdict it
  // last earned and animates it, so motion says "the grader is working here"
  // without overwriting the last thing known to be true. This is why fill is
  // computed from history even while under test, instead of being forced amber.
  if (underTest) {
    if (!everRan) {
      return {
        fill: "white",
        motion: "pulse",
        mark: null, // a gate that has never run cannot have regressed
        first_worked: true,
        ever_ran: false,
        regressed: false,
        resolved_at_attempt: null,
      };
    }
    const resting = restingFill(runs);
    return {
      fill: resting.fill,
      motion: "pulse",
      mark,
      first_worked: false,
      ever_ran: true,
      regressed,
      resolved_at_attempt: resting.resolved_at_attempt,
    };
  }

  // ── 3. RESTING ───────────────────────────────────────────────────────────
  if (!everRan) {
    return {
      fill: "none",
      motion: "still",
      mark: null,
      first_worked: false,
      ever_ran: false,
      regressed: false,
      resolved_at_attempt: null,
    };
  }

  const resting = restingFill(runs);
  return {
    fill: resting.fill,
    motion: "still",
    mark,
    first_worked: false,
    ever_ran: true,
    regressed,
    resolved_at_attempt: resting.resolved_at_attempt,
  };
}

/**
 * The resting colour, from the LAST executed observation plus the history
 * before it.
 *
 *   currently failing                    → red
 *   passed, and had never failed before  → blue   (clean first pass)
 *   passed, having previously failed     → green  (recovered)
 *
 * BLUE IS "PASSED WITHOUT EVER FAILING", NOT "PASSED IN ATTEMPT 1". Those
 * coincide in the common case and diverge exactly when a phase aborted before
 * reaching the gate — where blue remains the honest reading, because from the
 * measurement's point of view the gate passed the first time it was ever asked.
 */
function restingFill(runs) {
  const last = runs[runs.length - 1];
  if (last.status !== "pass") {
    return { fill: "red", resolved_at_attempt: null };
  }
  const failedBefore = runs.slice(0, -1).some((r) => r.status !== "pass");
  const firstPass = runs.find((r) => r.status === "pass");
  return {
    fill: failedBefore ? "green" : "blue",
    resolved_at_attempt: firstPass?.attempt ?? last.attempt,
  };
}

/**
 * Build gate id → ordered history from the append-only attempt records.
 *
 * Records are assumed already sorted oldest-first (attemptRecords does this).
 * A record whose `gate_results` is null is SKIPPED, not treated as empty: null
 * means the gate runner never published per-gate outcomes for that attempt,
 * which is different from an attempt in which every gate was absent.
 */
export function historiesFrom(attempts) {
  const out = new Map();
  for (const record of attempts ?? []) {
    const results = Array.isArray(record?.gate_results) ? record.gate_results : null;
    if (!results) continue;
    for (const result of results) {
      if (!result?.id) continue;
      if (!out.has(result.id)) out.set(result.id, []);
      out.get(result.id).push({
        attempt: Number.isFinite(Number(record.attempt)) ? Number(record.attempt) : 1,
        status: result.status,
      });
    }
  }
  return out;
}
