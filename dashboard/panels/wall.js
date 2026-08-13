// ─────────────────────────────────────────────────────────────────────────────
// PANEL: GATE WALL — the correctness axis
//
// Every conformance gate as a fixed cell in a dense grid, sitting beside the
// TRANSFER CURVE at 50/50. That adjacency IS the hard rule made structural:
// correctness and efficiency are the same size, side by side, and neither is
// folded into the other.
//
// ── THE SUITE IS REAL NOW (2026-08-13) ───────────────────────────────────────
//
// This panel used to render "gates ever observed FAILING", because the harness
// published `failed_gates` and no total. That made four of the design's six
// states underivable, and worse: a gate absent from that list could equally
// mean passed, never ran, or its phase died first — three different facts
// collapsed into one silence whose natural reading is success.
//
// The harness now publishes a write-once roster (71 gates: 56 backend, 14
// frontend, 1 conformance) plus per-gate outcomes per attempt, folded by the
// control plane into GET /api/wall. So the wall finally draws the WHOLE SUITE
// and can answer the question it could never answer before: what has NOT been
// tested. `untested` is a real, common, first-class state — not an absence.
//
// STATE IS DECIDED SERVER-SIDE. `control/wall.mjs foldGateStates()` assigns
// each gate exactly one of resolved/failing/untested/abandoned, which is why
// `totals` sums to `suite.total`. This panel maps state → colour and does not
// re-derive state: two surfaces disagreeing about whether a gate was abandoned
// or merely untested is precisely the class of bug this rebuild removed.
//
// SLOTS NEVER REFLOW. Roster order is the slot order, and the roster is
// write-once, so erosion reads as change-over-time rather than as relayout.
// The grid is the one surface a skeptic can check line-by-line against the
// cell's own gate_results.
//
// THE DENOMINATOR IS THE TRUE ENUMERATED COUNT, or it is null. The design
// comp's 114 does not exist on disk; publishing it would be the exact
// dishonesty this board exists to prevent. `suite.total: null` means UNKNOWABLE
// and renders as a stated reason, never as 0.
//
// NO MOTION DURING A TAKEOVER. A gate flipping green next to a serve that just
// fired would assert causation the data cannot support. Separate surfaces.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, clip } from "../board.js";

// ── LIVE CHOREOGRAPHY ────────────────────────────────────────────────────────
//
//   dashed — not yet tested; a real state, not a missing square
//   amber  — under test right now; carries NO claim about outcome
//   blue   — passed on ATTEMPT 1
//   check  — passed on a later attempt
//   slate  — abandoned mid-test; a stall is not a verdict
//   red    — tested and failing
//
// THE AXIS: ATTEMPT, NOT PHASE — and the legend says so. The design comp reads
// "passed in phase 1", but the harness's three phases (conformance/backend/
// frontend) are a PARTITION of the suite, not a progression over it: a backend
// gate never runs in the frontend phase, so "passed phase 1" is not a fact any
// gate can have. What the comp is actually drawing — solved immediately vs
// solved only after feedback — is the ATTEMPT axis, carried per-gate by
// `resolved_at_attempt` (the FIRST attempt in which the gate passed). Printing
// "phase" over attempt data would restore the imprecision this rebuild removed,
// so entries 3 and 4 of the legend are reworded and everything else is verbatim.
//
// AMBER IS PER-PHASE-SET, NOT PER-TEST, and that is disclosed on the surface.
// `report.mjs` spawns each runner with spawnSync, so per-test output is
// buffered until the phase has already ended and cannot be a live signal. The
// harness instead announces each phase's gate set before spawning it, so every
// still-unresolved gate in the open phase pulses together. An operator who
// believes amber is per-test would misread the wall's resolution.
// ─────────────────────────────────────────────────────────────────────────────

/** Design §9.3: the grid is a FIXED 12 columns at every suite size. */
const WALL_COLUMNS = 12;

/**
 * Server state → visual class. Total, pure, and the ONLY place the mapping
 * exists.
 *
 * `under_test` outranks the resting state because it is the live fact: a gate
 * being re-run right now has no current verdict, whatever it reported last
 * attempt. The server has already narrowed `under_test` to gates that are
 * untested or failing — a resolved gate has its answer and an abandoned one
 * will never get one, so neither may pulse.
 */
export function gateVisual(g) {
  if (g.under_test) return "testing";
  switch (g.state) {
    case "resolved":
      return g.resolved_at_attempt === 1 ? "blue" : "green";
    case "failing":
      return "red";
    case "abandoned":
      return "slate";
    default:
      return "unobserved";
  }
}

/**
 * The run-level grading verdict, reduced to the two facts the wall needs.
 *
 * `stalled` IS NOT RECOMPUTED HERE. `gradingStatus()` derives it from the gate
 * log's MTIME against GATE_STALL_THRESHOLD_S for the reason recorded at
 * gate-events.mjs:126-132 — the harness writes naive local timestamps, and
 * parsing them produced a constant phantom 7.1h silence in a UTC container. A
 * second stall detector on this side would reintroduce exactly that bug.
 *
 * A TIMEOUT PRESENTS AS A STALL: both leave squares that were under test with no
 * verdict, which is precisely what slate says.
 */
function liveState(board) {
  const gr = board.suite?.grading ?? board.events?.grading ?? null;
  if (!gr) return { grading: false, stalled: false, phase: null, attempt: null, silent_s: null, phases: [] };
  return {
    grading: Boolean(gr.grading ?? gr.active),
    stalled: Boolean(gr.stalled || gr.timed_out),
    phase: gr.phase ?? null,
    attempt: gr.attempt ?? board.suite?.attempt?.current ?? null,
    silent_s: gr.silent_s ?? null,
    // Per-phase progress published by the harness DURING grading. The
    // authoritative gate list only lands at attempt end, so without this the
    // wall has nothing to say for the whole grading window.
    phases: Array.isArray(gr.phases) ? gr.phases : [],
  };
}

/**
 * NO MOTION DURING A TAKEOVER (see header). A gate pulsing beside a serve that
 * just fired would assert a relation the data cannot support. The takeover owns
 * the whole motion budget for its 6s (recall.js:16-17); the wall holds still and
 * lets colour alone carry the state.
 */
function motionAllowed(board) {
  const fired = board.recall_moment?.fired_at ?? null;
  return !(fired && Date.now() - fired < 6000);
}

export function renderWall(board) {
  const suite = board.suite ?? null;
  const gates = suite?.gates ?? [];
  const live = liveState(board);

  return `
    <section class="panel wall">
      <div class="phead">
        <span class="ttl">GATE WALL — LIVE CHOREOGRAPHY</span>
        <span class="sub">movement is the only thing on this board that means &ldquo;still working&rdquo; — it never means &ldquo;passing&rdquo;</span>
        ${headline(suite)}
      </div>
      ${gradingLine(live)}
      ${gates.length ? grid(board, gates) : empty(live, suite)}
      ${gates.length ? legend(suite, gates) : ""}
      ${gates.length ? clusters(gates) : ""}
    </section>`;
}

/**
 * THE HEADLINE — a ratio only when both halves are real.
 *
 * `resolved / total` is stated ONLY when the suite size is known. With no
 * roster the total is UNKNOWABLE, not zero, and printing "40/0" or silently
 * substituting the observed count would fabricate the denominator this whole
 * rebuild existed to make honest.
 */
function headline(suite) {
  if (!suite) return `<span class="tag">GATE SUITE UNAVAILABLE</span>`;

  const total = suite.suite?.total ?? null;
  const resolved = suite.totals?.resolved ?? null;

  if (total === null) return `<span class="tag">SUITE SIZE UNKNOWN — NOT ZERO</span>`;
  if (resolved === null) return `<span class="tag">NO GATE OUTCOMES YET</span>`;

  // A partial enumeration is still a true count of what was enumerated — it is
  // labelled rather than hidden, because the ratio's denominator moved.
  const partial = suite.suite?.complete === false ? `<span class="tag">PARTIAL ENUMERATION</span>` : "";
  const failing = suite.totals?.failing ?? 0;

  return `
    <span class="sub"><span class="bright">${resolved}</span>/${total} passed</span>
    ${failing > 0 ? `<span class="tag bad">${failing} FAILING</span>` : ""}
    ${partial}`;
}

/**
 * The one line that makes the motion legible. A pulsing wall with no caption
 * forces the operator to guess whether amber means "working" or "failing" — the
 * exact ambiguity ask 3 exists to remove. Stated in words, above the grid.
 */
function gradingLine(live) {
  if (live.stalled) {
    const silent = live.silent_s === null ? "" : ` — silent ${live.silent_s}s`;
    return `
      <div class="wall-live stalled">
        <span class="gcell slate sm"></span>
        <span class="bright">GRADING STALLED${esc(silent)}</span>
        <span class="note">${esc("squares that were under test are held with no verdict. A stall is not a failure — nothing here has been decided.")}</span>
      </div>`;
  }

  if (!live.grading) return "";

  const where = [
    live.attempt === null ? null : `attempt ${live.attempt}`,
    live.phase === null ? null : `phase ${live.phase}`,
  ]
    .filter(Boolean)
    .join(" · ");

  return `
    <div class="wall-live">
      <span class="gcell testing sm"></span>
      <span class="bright">GRADING${where ? ` — ${esc(where)}` : ""}</span>
      <span class="note">${esc("pulsing squares are under test right now. Motion means the grader is working; it makes no claim about the outcome.")}</span>
    </div>`;
}

/**
 * THE EMPTY WALL.
 *
 * Reached when the suite surface carries no gates at all. The reason matters
 * more than the emptiness: "no roster" and "roster present, no outcomes yet"
 * and "grading has not started" are different facts, and the control plane
 * already names each one in `unwired_reasons`. Rendering them is the point —
 * an unexplained empty grid is what made this panel look broken during the
 * whole grading window.
 *
 * The phase list is PROVISIONAL and says so. A phase problem count is NOT a
 * gate identity: it is never drawn as a square and never counted as a gate.
 */
function empty(live, suite) {
  const reasons = suite?.unwired_reasons ?? {};
  const keys = suite?.unwired ?? [];

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

  const phases = live?.phases ?? [];
  const rows = phases
    .map((p) => {
      const state = p.running ? "running" : p.status === "pass" ? "pass" : p.status === "timeout" ? "timeout" : "fail";
      const detail = p.running
        ? "running"
        : p.problems === null || p.problems === undefined
          ? String(p.status ?? "done")
          : `${p.status ?? "done"} · ${p.problems} problem${p.problems === 1 ? "" : "s"}`;
      return `
        <div class="gphase ${state}">
          <span class="gp-name">${esc(p.phase ?? "—")}</span>
          <span class="gp-detail">${esc(detail)}</span>
        </div>`;
    })
    .join("");

  if (!suite) {
    return `
      <div class="wall-empty">
        <div class="bright">The gate suite surface is unavailable.</div>
        <div class="note">${esc("GET /api/wall did not answer, so the suite size is unknown — which is not the same as a suite of zero gates. Start the control plane to restore this panel.")}</div>
      </div>`;
  }

  return `
    <div class="wall-empty">
      <div class="bright">${esc(phases.length ? "Grading in progress — no gate outcomes published yet." : "No gate outcomes published yet.")}</div>
      <div class="note">${esc("Per-gate results are written at ATTEMPT END, so this is the normal state early in a cell. Nothing here has been evaluated, which is not the same as everything failing.")}</div>
      ${why}
      ${rows ? `<div class="gphases">${rows}</div>` : ""}
    </div>`;
}

/**
 * The dense grid. FIXED 12 columns at every suite size (design §9.3) — the
 * count is a constant, not a function of the gate count, so the wall reads as
 * the same object from cell to cell and slot N is slot N forever.
 */
function grid(board, gates) {
  // `still` suppresses the pulse without changing any colour, so a takeover
  // freezes the wall rather than blanking it. SLOTS NEVER REFLOW: the class
  // rides the container, never the order or the count.
  const still = motionAllowed(board) ? "" : " still";
  const cells = gates
    .map((g) => {
      const st = gateVisual(g);
      const label = `${g.id} ${g.req ?? ""} ${g.title ?? ""}`.trim();
      return `<span class="gcell ${esc(st)}" title="${esc(`${label} — ${VISUAL_WORD[st]}`)}"></span>`;
    })
    .join("");
  return `<div class="gwall${still}" style="grid-template-columns:repeat(${WALL_COLUMNS},1fr)">${cells}</div>`;
}

/** The tooltip gloss, kept beside the colours so the two cannot drift apart. */
const VISUAL_WORD = {
  blue: "passed on attempt 1",
  green: "passed on a later attempt",
  testing: "under test right now — no outcome claimed",
  red: "tested and failed",
  unobserved: "not yet tested",
  slate: "abandoned mid-test — no verdict, ever",
};

/**
 * THE LEGEND — seven entries, in the design's order, always all of them.
 *
 * Every state is listed even at zero. This is a KEY, not a status readout: a
 * legend that hides `abandoned` until something is abandoned teaches the
 * operator that slate does not exist, so its first appearance is unreadable
 * exactly when it matters most. Counts are appended where non-zero.
 *
 * Entries 3 and 4 say ATTEMPT where the comp says phase. The harness's phases
 * partition the suite rather than sequencing it, so "passed in phase 1" is not
 * a fact any gate can hold; `resolved_at_attempt` is what the data carries and
 * what the blue/check split actually distinguishes.
 */
function legend(suite, gates) {
  const t = tally(gates);
  const row = (cls, label) =>
    `<span><span class="gcell ${cls} sm"></span> ${esc(label)}${t[cls] ? ` <span class="bright">${t[cls]}</span>` : ""}</span>`;

  const total = suite?.suite?.total ?? null;
  const denom =
    total === null
      ? "suite size is unknown for this run — the roster is written once at cell start, so a run begun before it has none"
      : `denominator is the full enumerated suite of ${total} gates`;

  // Per-phase-set amber is a real limit of the signal, not a detail. An
  // operator who reads amber as per-test will misread the whole wall.
  const sig = Object.values(suite?.live_signal ?? {});
  const setwise = sig.length > 0 && sig.every((v) => v === "per-phase-set");

  return `
    <div class="wall-legend">
      ${row("unobserved", "not yet tested")}
      ${row("testing", "under test — pulses amber, 1.4s")}
      ${row("blue", "passed on attempt 1")}
      ${row("green", "passed on a later attempt")}
      ${row("slate", "abandoned mid-test — no verdict, ever")}
      ${row("red", "tested and failed")}
      <span class="note">${esc("blue and slate are borrowed from the sanctioned midnight palette — the only two off-hue signals besides error red.")}</span>
      <span class="note">${esc(denom)}</span>
      ${setwise ? `<span class="note">${esc("amber marks every unresolved gate in the OPEN PHASE, not one test — the grader buffers per-test output until its phase ends, so a finer live signal does not exist.")}</span>` : ""}
    </div>`;
}

function tally(gates) {
  const t = { blue: 0, green: 0, testing: 0, red: 0, slate: 0, unobserved: 0 };
  for (const g of gates) t[gateVisual(g)] += 1;
  return t;
}

/**
 * FAILING CLUSTERS. A grid of squares says how many; it cannot say WHAT. Gates
 * group by their REQ, and a cluster of failures under one requirement is the
 * actionable fact — printed at full size, never in a tooltip nobody on a stream
 * can hover.
 *
 * THIS READS THE RECORDED STATE, NOT THE VISUAL ONE, and the difference is
 * deliberate. Live choreography repaints a failing gate as amber while it is
 * being re-run; that describes the GRADER's situation and does not revise the
 * recorded result of the last completed attempt. Keying clusters on the visual
 * state would empty this list the moment a re-run began — losing the standing
 * failures precisely when the operator is trying to work out what went wrong.
 */
function clusters(gates) {
  const by = new Map();
  for (const g of gates) {
    if (g.state !== "failing") continue;
    const key = g.req ?? g.title ?? g.id;
    by.set(key, (by.get(key) ?? 0) + 1);
  }

  const top = [...by.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
  if (!top.length) {
    return `
      <div class="wall-clusters">
        <span class="kick">FAILING CLUSTERS</span>
        <div class="null">${esc("no gate is currently failing")}</div>
      </div>`;
  }

  return `
    <div class="wall-clusters">
      <span class="kick">FAILING CLUSTERS — TOP ${top.length}</span>
      ${top.map(([k, n]) => `
        <div class="cl"><span>${esc(clip(k, 46))}</span><span class="danger">${n} gate${n === 1 ? "" : "s"}</span></div>`).join("")}
    </div>`;
}
