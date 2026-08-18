// ─────────────────────────────────────────────────────────────────────────────
// PANEL: GATE WALL — the correctness axis
//
// Every gate in the suite as a fixed square in a dense grid, sitting beside the
// TRANSFER CURVE at 50/50. That adjacency IS the hard rule made structural:
// correctness and efficiency are the same size, side by side, and neither is
// folded into the other.
//
// ── THIS IS A DUMB COMPONENT. IT DECIDES NOTHING. ────────────────────────────
//
// Two colours and an absence:
//
//   green   the gate passed in the last completed test run
//   red     the gate failed in the last completed test run
//   empty   no completed test run has a result for this gate
//
// `control/wall.mjs` assigns every gate exactly one of passing/failing/untested.
// This file maps that word to a class and does nothing else. There is no phase,
// no attempt axis, no live signal, no motion, and no second derivation of any
// state — two surfaces disagreeing about a square is the class of bug this
// panel was rebuilt to remove.
//
// EMPTY IS NOT A WEAKER PASS. An untested gate is drawn as a dashed outline
// with no fill, because "not measured" and "measured and passed" are different
// facts and the difference is the entire honesty of this board.
//
// SLOTS NEVER REFLOW. Roster order is the slot order, and the roster is
// write-once, so change reads as change-over-time rather than as relayout. The
// grid is the one surface a skeptic can check line-by-line against the cell's
// own gate_results.
//
// THE DENOMINATOR IS THE TRUE ENUMERATED COUNT, or it is null. The design
// comp's 114 does not exist on disk; publishing it would be the exact
// dishonesty this board exists to prevent. `suite.total: null` means UNKNOWABLE
// and renders as a stated reason, never as 0.
// ─────────────────────────────────────────────────────────────────────────────

import { esc } from "../board.js";

/** Design §9.3: the grid is a FIXED 12 columns at every suite size. */
const WALL_COLUMNS = 12;

/**
 * Server facts → visual class. Total, pure, and the ONLY place the mapping
 * exists.
 *
 * FOUR VISUALS FROM THREE PUBLISHED FACTS — `state`, `first_pass_attempt`,
 * `ever_failed` — and no others. This file still decides nothing: it does not
 * read attempts, does not fold history, and cannot disagree with the server
 * about whether a gate passes. `state` remains the sole verdict; the trajectory
 * only splits the PASSING square into "green first try" and "green eventually".
 *
 *   green      passed on the first attempt and never broke
 *   recovered  passing now, but not from the start — red rim, green core
 *   red        failing in the last completed test run — carries an ✕
 *   unobserved no result yet; dashed and empty, NOT a weaker pass
 *
 * WHY THE SPLIT EARNS ITS COMPLEXITY. A suite where every gate went green on
 * attempt 1 and one where half needed two rounds of repair produce the SAME
 * wall under a two-colour scheme, and they are not the same result — the number
 * of attempts to green is a headline measurement of this bench, and it was
 * visible only as a per-attempt scalar, never per gate.
 *
 * A MISSING TRAJECTORY DEGRADES TO PLAIN GREEN, never to `recovered`. Runs
 * recorded before these fields existed publish neither, and an absent fact must
 * not be rendered as an adverse one.
 */
export function gateVisual(g) {
  switch (g.state) {
    case "passing":
      return g.ever_failed === true || (g.first_pass_attempt ?? 1) > 1 ? "recovered" : "green";
    case "failing":
      return "red";
    default:
      return "unobserved";
  }
}

export function renderWall(board) {
  const suite = board.suite ?? null;
  const gates = suite?.gates ?? [];

  return `
    <section class="panel wall">
      <div class="phead">
        <span class="ttl">GATE WALL</span>
        ${headline(suite)}
      </div>
      ${gates.length ? grid(gates) : empty(suite)}
      ${gates.length ? legend(suite) : ""}
    </section>`;
}

/**
 * THE HEADLINE — a ratio only when both halves are real.
 *
 * `passing / total` is stated ONLY when the suite size is known. With no roster
 * the total is UNKNOWABLE, not zero, and printing "40/0" or silently
 * substituting the observed count would fabricate the denominator this whole
 * rebuild existed to make honest.
 */
function headline(suite) {
  if (!suite) return `<span class="tag">GATE SUITE UNAVAILABLE</span>`;

  const total = suite.suite?.total ?? null;
  if (total === null) return `<span class="tag">SUITE SIZE UNKNOWN — NOT ZERO</span>`;

  const passing = suite.totals?.passing ?? null;
  if (passing === null) return `<span class="tag">NO GATE OUTCOMES YET</span>`;

  const failing = suite.totals?.failing ?? 0;
  const untested = suite.totals?.untested ?? 0;

  // A SUITE WITH NO RUN BEHIND IT SAYS SO, IN THE HEADLINE.
  //
  // `suite_source:"enumerated"` means the server found no run at the directory
  // it read and enumerated the live harness suite instead — a true denominator
  // with nothing measured against it. That renders as `0/N passing`, which is
  // indistinguishable from a run that genuinely passed nothing, and it is how a
  // stale run-directory default went unnoticed for three days: the wall showed
  // `0/71 passing` while the run on disk had recorded 16 passing and 2 failing.
  //
  // The server already publishes which it is. This states it rather than
  // deriving it — the panel still decides nothing.
  const norun = suite.suite_source === "enumerated" ? `<span class="tag">NO RUN — SUITE ENUMERATED</span>` : "";

  // A RATIO READS AS A RESULT, SO SAY WHEN IT IS NOT ONE.
  //
  // `gradable:false` means a gate runner aborted and left gates unmeasured for
  // harness reasons — the pass count is a lower bound on an unknown, not a
  // score, and must not be compared against a completed run. The squares
  // already draw those gates as untested; without this the HEADLINE still reads
  // like a verdict. `16/71 passing` on a cell that actually scores 69/71 is the
  // exact reading this prevents.
  const ungradable =
    suite.gradable === false ? `<span class="tag bad">NOT A SCORE — RUNNER ABORTED</span>` : "";

  // A partial enumeration is still a true count of what was enumerated — it is
  // labelled rather than hidden, because the ratio's denominator moved.
  const partial = suite.suite?.complete === false ? `<span class="tag">PARTIAL ENUMERATION</span>` : "";

  return `
    <span class="sub"><span class="bright">${passing}</span>/${total} passing</span>
    ${failing > 0 ? `<span class="tag bad">${failing} FAILING</span>` : ""}
    ${untested > 0 ? `<span class="sub">${untested} not yet tested</span>` : ""}
    ${norun}
    ${ungradable}
    ${partial}`;
}

/**
 * THE EMPTY WALL.
 *
 * Reached when the suite surface carries no gates at all. The reason matters
 * more than the emptiness: "no roster" and "roster present, no outcomes yet"
 * are different facts, and the control plane already names each one in
 * `unwired_reasons`. Rendering them is the point — an unexplained empty grid is
 * what made this panel look broken for the whole grading window.
 */
function empty(suite) {
  if (!suite) {
    return `
      <div class="wall-empty">
        <div class="bright">The gate suite surface is unavailable.</div>
        <div class="note">${esc("GET /api/wall did not answer, so the suite size is unknown — which is not the same as a suite of zero gates. Start the control plane to restore this panel.")}</div>
      </div>`;
  }

  const reasons = suite.unwired_reasons ?? {};
  const keys = suite.unwired ?? [];
  const why = keys.length
    ? `<div class="gphases">${keys
        .map(
          (k) => `
        <div class="gphase">
          <span class="gp-name">${esc(k)}</span>
          <span class="gp-detail">${esc(reasons[k] ?? "unwired")}</span>
        </div>`,
        )
        .join("")}</div>`
    : "";

  return `
    <div class="wall-empty">
      <div class="bright">No gate outcomes published yet.</div>
      <div class="note">${esc("Per-gate results are written when a test run completes, so this is the normal state early in a cell. Nothing here has been evaluated, which is not the same as everything failing.")}</div>
      ${why}
    </div>`;
}

/**
 * The dense grid. FIXED 12 columns at every suite size (design §9.3) — the
 * count is a constant, not a function of the gate count, so the wall reads as
 * the same object from cell to cell and slot N is slot N forever.
 */
function grid(gates) {
  const cells = gates
    .map((g) => {
      const st = gateVisual(g);
      const label = `${g.id} ${g.req ?? ""} ${g.title ?? ""}`.trim();
      // The attempt number is stated in the tooltip rather than encoded in the
      // square. A rim says "this needed repair"; WHICH round it took is detail,
      // and detail belongs on hover, not in a 12-column grid of 9px cells.
      const when =
        st === "recovered" && Number.isFinite(g.first_pass_attempt)
          ? ` (first passed on attempt ${g.first_pass_attempt})`
          : "";
      return `<span class="gcell ${esc(st)}" title="${esc(`${label} — ${VISUAL_WORD[st]}${when}`)}"></span>`;
    })
    .join("");
  return `<div class="gwall" style="grid-template-columns:repeat(${WALL_COLUMNS},1fr)">${cells}</div>`;
}

/** The tooltip gloss, kept beside the colours so the two cannot drift apart. */
const VISUAL_WORD = {
  green: "passing — green on the first attempt",
  recovered: "passing — but not on the first attempt",
  red: "failing",
  unobserved: "not yet tested",
};

/**
 * THE LEGEND — three entries, always all three.
 *
 * Every state is listed even at zero. This is a KEY, not a status readout: a
 * legend that hides a state until it occurs teaches the operator that it does
 * not exist, so its first appearance is unreadable exactly when it matters.
 */
function legend(suite) {
  const total = suite?.suite?.total ?? null;
  const denom =
    total === null
      ? "suite size is unknown for this run — the roster is written once at cell start, so a run begun before it has none"
      : `denominator is the full enumerated suite of ${total} gates`;

  const attempt = suite?.attempt ?? null;

  return `
    <div class="wall-legend">
      <span><span class="gcell green sm"></span> passed first attempt</span>
      <span><span class="gcell recovered sm"></span> passed on a later attempt</span>
      <span><span class="gcell red sm"></span> failing</span>
      <span><span class="gcell unobserved sm"></span> not yet tested</span>
      <span class="note">${esc(
        attempt === null
          ? "these are the results of the last completed test run"
          : `these are the results of the last completed test run (attempt ${attempt})`,
      )}</span>
      <span class="note">${esc(denom)}</span>
      <span class="note">${esc(provenance(suite))}</span>
      ${suite?.gradable === false ? `<span class="note">${esc(gradabilityNote(suite))}</span>` : ""}
    </div>`;
}

/**
 * WHY THIS ATTEMPT IS NOT A MEASUREMENT.
 *
 * The control plane passes the harness's own sentence through; this renders it
 * and names the runners that aborted. Stating the reason is the whole point — an
 * unexplained "not a score" badge is the same dead end as an unexplained empty
 * grid, which is what this panel keeps having to be rescued from.
 */
function gradabilityNote(suite) {
  const reason = suite?.ungradable_reason ?? null;
  const aborted = Array.isArray(suite?.aborted_runners) ? suite.aborted_runners : [];
  if (reason) return reason;
  return aborted.length
    ? `${aborted.join(", ")} aborted, so gates below were left unmeasured by the harness`
    : "a gate runner aborted, so the pass count is a lower bound rather than a score";
}

/**
 * WHICH RUN THESE SQUARES CAME FROM, in words.
 *
 * ── WHY THE WALL NOW NAMES ITS OWN SOURCE ───────────────────────────────────
 *
 * The payload always carried `run_dir` and `suite_source`; nothing rendered
 * them. So when the control plane's default run directory went stale against a
 * per-model campaign layout, the wall drew a fully enumerated 71-gate suite with
 * every square empty and gave the operator NO WAY TO TELL that it was reading a
 * directory the run had never written to. It read as "the wall is broken" for
 * three days. It was reading the wrong run, and saying which run would have made
 * that the first thing anyone noticed.
 *
 * A panel that shows measurements must be able to say what it measured. This is
 * a straight restatement of two published fields — no derivation, no second
 * opinion about state, consistent with the rest of this file.
 */
function provenance(suite) {
  const dir = suite?.run_dir ?? null;
  if (!dir) return "the control plane did not say which run these results came from";

  switch (suite?.suite_source) {
    case "run":
      // The strong case: a roster pinned to the run it graded.
      return `from runs/${dir}, graded against that run's own pinned roster`;
    case "enumerated":
      // The case that hid the defect. The suite is real and the run is not:
      // these squares are empty because nothing was read, not because nothing
      // passed, and the difference is the whole point of the panel.
      return (
        `no run at runs/${dir} — the suite below is enumerated live from the harness and NO run's ` +
        "outcomes are being shown, so every square is unmeasured rather than failed"
      );
    default:
      return `from runs/${dir}, source unstated`;
  }
}
