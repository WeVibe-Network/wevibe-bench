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
 * ── SUPERSEDED FOR THE CHOREOGRAPHY, KEPT FOR THE LEGEND AND CLUSTERS ───────
 *
 * This function answers "what is this gate's RESTING state" and is still the
 * right answer for the legend tally and the failing-cluster list. The SQUARE is
 * now drawn from `g.choreography` instead (see `cellClasses`), because the
 * directive needs three independent facts — fill, motion, mark — and a single
 * class name cannot carry three axes without the board parsing meaning back out
 * of a compound token.
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

// ── THE CHOREOGRAPHY: fill × motion × mark ──────────────────────────────────
//
// THREE AXES, PUBLISHED SEPARATELY BY THE SERVER, COMPOSED HERE.
// `control/choreography.mjs` decides all three; this maps them to classes and
// decides nothing. The directive in full:
//
//   worked on for the FIRST time ever  →  pulsing WHITE
//   worked on any later time           →  pulse its EXISTING colour
//   passed, never having failed        →  solid BLUE
//   failing                            →  solid RED
//   was passing, now failing           →  RED + a star that persists all run
//   was failing, now passing           →  solid GREEN
//
// WHITE IS RESERVED FOR THE FIRST LOOK. It is the only moment in a gate's life
// when the board has no information at all, and it must not be reachable any
// other way — a white square always means "we are seeing this gate for the very
// first time", never "we are unsure".
//
// THE STAR IS AN OVERLAY, NEVER A COLOUR. A regression keeps whatever fill the
// gate currently earns (red now, green if it recovers) and carries the mark on
// top. Encoding the regression as its own colour would lose the current verdict.

/** Fallback choreography for a control plane that predates the field. */
function choreographyOf(g) {
  if (g.choreography && typeof g.choreography === "object") return g.choreography;
  // DERIVED FROM THE RESTING STATE, never invented: an older server carries no
  // history, so no gate can claim `first_worked` or a regression mark. The wall
  // degrades to its previous behaviour rather than fabricating a star.
  const v = gateVisual(g);
  return {
    fill: v === "testing" ? "white" : v === "unobserved" ? "none" : v,
    motion: v === "testing" ? "pulse" : "still",
    mark: null,
    first_worked: v === "testing",
    ever_ran: v !== "unobserved",
    regressed: false,
  };
}

/**
 * The class list for one square.
 *
 * Emitted as SEPARATE classes (`gf-red gm-pulse gk-regression`) rather than one
 * compound name so the stylesheet can compose them independently — motion is
 * suppressible by the takeover freeze and the reduced-motion query WITHOUT
 * touching the fill, which is the property that carries the verdict.
 */
export function cellClasses(g, live) {
  const c = choreographyOf(g);
  const out = [`gf-${c.fill}`, `gm-${c.motion}`];
  if (c.mark) out.push(`gk-${c.mark}`);
  // The live lane's provisional overlay. An ANNOTATION on the authoritative
  // square, never a replacement for it — the graded colour always wins, and the
  // operator can always tell a provisional reading from a scored one.
  if (live) out.push(`gl-${live}`);
  return out.join(" ");
}

/** Human gloss for a square, assembled from the same three axes. */
function cellTitle(g, live) {
  const c = choreographyOf(g);
  const bits = [`${g.id} ${g.req ?? ""} ${g.title ?? ""}`.trim()];
  bits.push(FILL_WORD[c.fill] ?? c.fill);
  if (c.motion === "pulse") {
    bits.push(c.first_worked ? "under test for the FIRST time" : "under test right now — no outcome claimed");
  }
  if (c.mark === "regression") bits.push("REGRESSED — passed earlier in this run, then broke");
  if (live === "pass" || live === "fail") bits.push(`live lane: ${live} (provisional, not scored)`);
  if (live === "deferred") bits.push("never measured live — see the legend");
  if (live === "not_loaded") bits.push("live lane could not import its spec — the snapshot does not compile");
  return bits.filter(Boolean).join(" — ");
}

const FILL_WORD = {
  none: "not yet tested",
  white: "first look — never tested before",
  blue: "passed, having never failed",
  green: "passed after previously failing",
  red: "tested and failing",
  slate: "abandoned mid-test — no verdict, ever",
};


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
      ${gradingLine(live, suite)}
      ${laneLine(board)}
      ${buildStrip(board)}
      ${gates.length ? grid(board, gates) : empty(live, suite)}
      ${gates.length ? legend(suite, gates, board) : ""}
      ${gates.length ? clusters(gates) : ""}
    </section>`;
}

/**
 * THE LIVE LANE LINE — states what the provisional overlay is, in words.
 *
 * Renders NOTHING when there is no lane. The lane is optional and its absence
 * is the normal case; a permanent "live lane: off" row would imply the board is
 * missing something it is not.
 *
 * When the lane is STALE the line says so and the overlay is dropped from the
 * grid — a stopped lane's last reading must never be presented as current.
 */
function laneLine(board) {
  const live = board.live ?? null;
  if (!live) return "";

  if (live.running !== true) {
    const why = live.stale_reason ?? live.unwired_reasons?.["live-lane"] ?? null;
    // No artifact at all is silence, not a warning: nothing was ever claimed.
    if (!live.stale) return "";
    return `
      <div class="wall-live stalled">
        <span class="gcell gf-slate gm-still sm"></span>
        <span class="bright">LIVE LANE STOPPED</span>
        <span class="note">${esc(why ?? "the live lane is no longer publishing — its last grid is a past measurement and is not shown")}</span>
      </div>`;
  }

  const c = live.lane?.counts ?? null;
  const snap = live.lane?.snapshot ?? null;
  const parsed = snap?.parsed;

  // A snapshot that does not compile is the single most useful thing this line
  // can say: it explains grey squares that would otherwise look like failures.
  if (parsed === false) {
    return `
      <div class="wall-live stalled">
        <span class="gcell gf-none gm-still sm"></span>
        <span class="bright">LIVE LANE — SNAPSHOT DOES NOT COMPILE</span>
        <span class="note">${esc(snap?.stale_reason ?? "the worktree snapshot failed to import; live squares are held and no live verdict is claimed")}</span>
      </div>`;
  }

  const bits = c
    ? `${c.pass} passing · ${c.fail} failing · ${c.deferred} never measured live`
    : "no live counts published";
  const dur = live.lane?.duration_ms ? ` · measured in ${(live.lane.duration_ms / 1000).toFixed(1)}s` : "";

  return `
    <div class="wall-live">
      <span class="gcell gf-blue gm-still sm gl-pass"></span>
      <span class="bright">LIVE LANE — PROVISIONAL</span>
      <span class="note">${esc(`${bits}${dur}. Measured against a SNAPSHOT while the agent is still working — these are not scored, and the graded result at attempt end is the only source of truth.`)}</span>
    </div>`;
}

/**
 * THE BUILD STRIP — the construction axis.
 *
 * Answers the question the wall could never answer during the long pre-grading
 * window: is the artifact being FILLED IN, or merely edited? Measured as the
 * scaffold's stub surface disappearing, so the denominator is a property of the
 * task and needs no maintenance.
 *
 * FILL IS NOT A PROGRESS BAR TOWARD A PASS. A file can reach 100% fill and fail
 * every gate — construction and correctness are separate axes, adjacent and
 * never multiplied. The caption says so.
 */
function buildStrip(board) {
  const build = board.live?.build ?? null;
  if (!build) return "";

  const files = build.files ?? [];
  if (!files.length) return "";

  const rows = files
    .map((f) => {
      const pct = f.fill === null || f.fill === undefined ? null : Math.round(f.fill * 100);
      const detail =
        f.metric === "stub-ratio"
          ? `${f.stubs_initial - f.stubs_remaining}/${f.stubs_initial} stubs implemented`
          : f.metric === "line-ratio"
            ? `${f.lines} lines (reference ${f.reference_lines})`
            : (f.reason ?? "not measurable");
      return `
        <div class="bfile ${esc(f.state)}">
          <span class="bf-name">${esc(f.path)}</span>
          <span class="bf-bar"><i style="width:${pct === null ? 0 : pct}%"></i></span>
          <span class="bf-detail">${esc(pct === null ? "—" : `${pct}%`)} · ${esc(detail)}</span>
        </div>`;
    })
    .join("");

  const t = build.totals ?? {};
  const head =
    t.fill === null || t.fill === undefined
      ? "no stub denominator for this task — fill is unknowable, not zero"
      : `${t.stubs_initial - t.stubs_remaining}/${t.stubs_initial} scaffold stubs implemented`;

  return `
    <div class="wall-build">
      <span class="kick">BUILD — FILE POPULATION</span>
      <div class="note">${esc(`${head}. This is CONSTRUCTION, not correctness: a file at 100% can still fail every gate.`)}</div>
      ${rows}
    </div>`;
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

  // ARMED — the suite is enumerated and nothing has been evaluated. Stated as
  // "0 / N tested", never "0 / N passed": nothing has been tested, so there is
  // no pass rate to report. The server decides this (`armed`); the board does
  // not infer it, because armed and everything-failed look identical in a grid.
  if (suite.armed) {
    return `
      <span class="tag">ARMED</span>
      <span class="sub"><span class="bright">0</span>/${total} tested</span>`;
  }

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
function gradingLine(live, suite) {
  // ARMED — say so in words. A grid of dotted outlines with no caption is the
  // same shape as a grid that failed everything, and the operator should not
  // have to read the colours to tell a run that has not started from a run that
  // lost. No motion: nothing is happening, so nothing moves.
  if (suite?.armed) {
    const total = suite.suite?.total ?? null;
    const src = suite.suite_source === "enumerated"
      ? "enumerated from the harness suite — this is what the next cell will be graded against"
      : "the roster this run was pinned to at cell start";
    return `
      <div class="wall-live">
        <span class="gcell sm"></span>
        <span class="bright">ARMED${total === null ? "" : ` — ${total} gates defined, none evaluated`}</span>
        <span class="note">${esc(`every square is a dotted outline: defined, not yet tested. ${src}.`)}</span>
      </div>`;
  }

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
 *
 * THE LIVE LANE IS AN OVERLAY, KEYED BY ROSTER ID. It annotates squares; it
 * never reorders, adds or removes one. A live id with no roster slot is
 * IGNORED rather than appended — the roster is the only thing that may define
 * the suite, and a lane that disagreed with it must not be able to grow the grid.
 */
function grid(board, gates) {
  // `still` suppresses the pulse without changing any colour, so a takeover
  // freezes the wall rather than blanking it. SLOTS NEVER REFLOW: the class
  // rides the container, never the order or the count.
  const still = motionAllowed(board) ? "" : " still";
  const live = liveById(board);
  const cells = gates
    .map((g) => {
      const lv = live.get(g.id) ?? null;
      return `<span class="gcell ${esc(cellClasses(g, lv))}" title="${esc(cellTitle(g, lv))}"></span>`;
    })
    .join("");
  return `<div class="gwall${still}" style="grid-template-columns:repeat(${WALL_COLUMNS},1fr)">${cells}</div>`;
}

/**
 * gate id → live-lane verdict, or an empty map.
 *
 * EMPTY WHENEVER THE LANE IS STALE. A lane that stopped republishing is
 * describing a past snapshot, and painting its verdicts as current provisional
 * readings would be the same class of lie as showing a graded result for a gate
 * that was never run. The board states the lane is stale instead (see
 * `laneLine`) and drops the overlay entirely.
 */
function liveById(board) {
  const out = new Map();
  const live = board.live ?? null;
  if (!live || live.running !== true) return out;
  for (const g of live.lane?.gates ?? []) {
    if (g?.id) out.set(g.id, g.live);
  }
  return out;
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
function legend(suite, gates, board = {}) {
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

  // REGRESSIONS ARE CALLED OUT IN WORDS, not left to a glyph nobody was taught.
  const regressed = suite?.totals?.regressed ?? 0;
  const live = board.live ?? null;
  const laneOn = live?.running === true;

  return `
    <div class="wall-legend">
      <span><span class="gcell gf-none gm-still sm"></span> ${esc("not yet tested")}${t.unobserved ? ` <span class="bright">${t.unobserved}</span>` : ""}</span>
      <span><span class="gcell gf-white gm-pulse sm"></span> ${esc("first look — being tested for the very first time")}</span>
      <span><span class="gcell gf-blue gm-pulse sm"></span> ${esc("re-testing — pulses its existing colour")}</span>
      <span><span class="gcell gf-blue gm-still sm"></span> ${esc("passed, having never failed")}${t.blue ? ` <span class="bright">${t.blue}</span>` : ""}</span>
      <span><span class="gcell gf-green gm-still sm"></span> ${esc("recovered — was failing, now passes")}${t.green ? ` <span class="bright">${t.green}</span>` : ""}</span>
      <span><span class="gcell gf-red gm-still sm"></span> ${esc("tested and failing")}${t.red ? ` <span class="bright">${t.red}</span>` : ""}</span>
      <span><span class="gcell gf-red gm-still gk-regression sm"></span> ${esc("REGRESSED — passed earlier this run, then broke")}${regressed ? ` <span class="bright">${regressed}</span>` : ""}</span>
      <span><span class="gcell gf-slate gm-still sm"></span> ${esc("abandoned mid-test — no verdict, ever")}${t.slate ? ` <span class="bright">${t.slate}</span>` : ""}</span>
      ${laneOn ? `<span><span class="gcell gf-none gm-still gl-pass sm"></span> ${esc("live-lane provisional reading — not scored")}</span>` : ""}
      ${laneOn ? `<span><span class="gcell gf-none gm-still gl-deferred sm"></span> ${esc("never measured live (owns :8002, or needs a browser)")}</span>` : ""}
      <span class="note">${esc("the star persists for the whole run: a gate that broke once is worth watching even after it recovers.")}</span>
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
