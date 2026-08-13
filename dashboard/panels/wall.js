// ─────────────────────────────────────────────────────────────────────────────
// PANEL: GATE WALL — the correctness axis
//
// Every conformance gate as a fixed cell in a dense grid, sitting beside the
// TRANSFER CURVE at 50/50. That adjacency IS the hard rule made structural:
// correctness and efficiency are the same size, side by side, and neither is
// folded into the other.
//
// THREE STATES, AND THE THIRD ONE MATTERS:
//   red        — failing in the latest attempt
//   green      — was red in an earlier attempt of this cell and is now absent
//   unobserved — never seen failing in this arm at all
//
// A gate absent from attempt 1 was never failing, so it is NOT evidence that
// anything was fixed and must never be counted as green. `unobserved` is drawn
// dashed rather than coloured — it is a third thing, not a weaker pass.
//
// SLOTS NEVER REFLOW. Gate order is fixed for the life of the run so erosion
// reads as change-over-time rather than as relayout. The grid is the one
// surface a skeptic can check line-by-line against the raw failed_gates list.
//
// THE DENOMINATOR IS OBSERVED, NOT THE SUITE SIZE. The harness publishes failed
// gates only (tasks/backgammon/gates/report.mjs writes no total), so every
// ratio here is over gates OBSERVED FAILING and is labelled `obs`. The design
// comp's "90/114" cannot be honoured because 114 does not exist in the data —
// inventing it would be the exact dishonesty this board exists to prevent.
//
// NO MOTION DURING A TAKEOVER. A gate flipping green next to a serve that just
// fired would assert causation the data cannot support. Separate surfaces.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, clip } from "../board.js";

// ── LIVE CHOREOGRAPHY ────────────────────────────────────────────────────────
//
// WHAT THE COLOURS MEAN, AND WHY THEY CANNOT MEAN WHAT THE COMP SAYS.
// The design assumes a fixed suite in which a square that PASSES phase 1 turns
// blue. No such square exists here: the harness publishes only gates observed
// FAILING, so a gate that passes everything is never drawn at all (see the
// denominator note above). Per D3 we ship on the observed universe and restate
// the colours to what the data can actually support:
//
//   blue   — was failing, RESOLVED AT ATTEMPT 1 (NOT "passed phase 1")
//   green  — was failing, resolved at a later attempt
//   amber  — under test right now; carries NO claim about outcome
//   dashed — never observed failing in this arm; a third state, not "pending"
//   red    — tested and still failing
//   slate  — abandoned mid-test; a stall is not a verdict
//
// THE AXIS. Two candidate axes exist and only ONE is per-gate. The harness's
// grading PHASES (conformance/backend/frontend) are published as a single
// run-level status by `gradingStatus()`, so they cannot colour an individual
// square. The ATTEMPT axis already carries per-gate resolution:
// `flipped_at_attempt`, built by `status-stream.mjs flipAttempt()` and already
// on the payload. So COLOUR RIDES ATTEMPTS and the run-level grading status
// drives only motion and the stall. The operator's "phase 1/2/3" is the attempt
// counter (max 3, status-stream.mjs:90), not the three named grading phases.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * The visual state of one gate. Pure: the same gate plus the same live status
 * always yields the same class, so a poll that changes nothing repaints nothing.
 */
function gateVisual(g, live) {
  const arm = g.a !== "unobserved" ? "a" : "b";
  const st = g[arm];

  // Never observed failing. It never pulses and never goes slate: it was not
  // under test, so motion here would invent a pending-ness the data denies.
  if (st === "unobserved") return "unobserved";

  if (st === "green") {
    return g[`${arm}_flipped_at_attempt`] === 1 ? "blue" : "green";
  }

  // `red` is the only state a live attempt can still move.
  if (live.stalled) return "slate";
  if (live.grading) return "testing";
  return "red";
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
  const gr = board.events?.grading ?? null;
  if (!gr) return { grading: false, stalled: false, phase: null, attempt: null, silent_s: null };
  return {
    grading: Boolean(gr.grading),
    stalled: Boolean(gr.stalled || gr.timed_out),
    phase: gr.phase ?? null,
    attempt: gr.attempt ?? null,
    silent_s: gr.silent_s ?? null,
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
  const wall = board.wall ?? { gates: [], totals: {} };
  const gates = wall.gates ?? [];
  const live = liveState(board);

  return `
    <section class="panel wall">
      <div class="phead">
        <span class="ttl">GATE WALL</span>
        <span class="sub">correctness — never multiplied into the curve</span>
        ${headline(board, gates)}
      </div>
      ${gates.length ? gradingLine(live) : ""}
      ${gates.length ? grid(board, gates, live) : empty()}
      ${gates.length ? legend(board, gates, live) : ""}
      ${gates.length ? clusters(gates) : ""}
    </section>`;
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

function headline(board, gates) {
  const latest = latestCell(board);
  if (!latest) return `<span class="tag">NO CELL GRADED</span>`;

  const g = latest.gates ?? {};
  if (g.failed === null || g.failed === undefined) {
    // NEVER RAN is not 0/N. The grader has not evaluated anything.
    return `<span class="tag">NEVER RAN — NOT 0/${gates.length}</span>`;
  }

  const total = g.total ?? gates.length;
  const passed = total - g.failed;
  const verdict = latest.verdict;

  return `
    <span class="sub">run ${String(latest.seq).padStart(2, "0")} · <span class="bright">${passed}</span>/${total}<span class="muted"> obs</span> passed</span>
    ${verdict === "FAIL" ? `<span class="tag bad">VERDICT FAIL</span>` : verdict === "PASS" ? `<span class="tag on">VERDICT PASS</span>` : `<span class="tag">${esc(nulWord(verdict))}</span>`}`;
}

function nulWord(v) {
  return v ? String(v) : "NO VERDICT";
}

function latestCell(board) {
  const all = board.stack?.all ?? [];
  const graded = all.filter((r) => r.gates?.failed !== null && r.gates?.failed !== undefined);
  if (graded.length) return graded[graded.length - 1];
  return all.length ? all[all.length - 1] : null;
}

function empty() {
  return `
    <div class="wall-empty">
      <div class="bright">The grader has not run yet.</div>
      <div class="note">Gates are evaluated at phase 2. Nothing has been evaluated, which is not the same as everything failing.</div>
    </div>`;
}

/**
 * The dense grid. Column count adapts to the gate count so the cells stay
 * square-ish without the slots moving between polls.
 */
function grid(board, gates, live) {
  const cols = gates.length <= 24 ? 8 : gates.length <= 60 ? 12 : 19;
  // `still` suppresses the pulse without changing any colour, so a takeover
  // freezes the wall rather than blanking it. SLOTS NEVER REFLOW: the class
  // rides the container, never the order or the count.
  const still = motionAllowed(board) ? "" : " still";
  const cells = gates
    .map((g) => {
      // The wall shows the arm actually being watched. `a` is memory-on and is
      // preferred; `b` is the control. A gate unobserved in both is dashed.
      const st = gateVisual(g, live);
      return `<span class="gcell ${esc(st)}" title="${esc(`${g.id} ${g.req ?? ""} ${g.title ?? ""} — ${VISUAL_WORD[st]}`.trim())}"></span>`;
    })
    .join("");
  return `<div class="gwall${still}" style="grid-template-columns:repeat(${cols},1fr)">${cells}</div>`;
}

/** The tooltip gloss, kept beside the colours so the two cannot drift apart. */
const VISUAL_WORD = {
  blue: "was failing, resolved at attempt 1",
  green: "was failing, resolved at a later attempt",
  testing: "under test right now — no outcome claimed",
  red: "tested and still failing",
  unobserved: "never observed failing in this arm",
  slate: "abandoned mid-test — no verdict",
};

/**
 * The legend carries the MEANING, not just the count. Blue and green differ by
 * WHEN a gate was resolved, which no swatch can convey on its own — and the
 * comp's reading ("passed phase 1") is the one thing this wall must not imply.
 * States with no squares are omitted rather than shown at 0: a permanent
 * "stalled 0" row would read as a status line rather than a key.
 */
function legend(board, gates, live) {
  const t = tally(gates, live);
  const row = (cls, label) =>
    t[cls] ? `<span><span class="gcell ${cls} sm"></span> ${esc(label)} ${t[cls]}</span>` : "";

  return `
    <div class="wall-legend">
      ${row("blue", "resolved at attempt 1")}
      ${row("green", "resolved later")}
      ${row("testing", "under test")}
      ${row("red", "failing")}
      ${row("slate", "no verdict")}
      ${row("unobserved", "never seen failing")}
      <span class="note">${esc(`denominator is the ${gates.length} gates observed failing in this stack — the harness publishes no suite total`)}</span>
    </div>`;
}

function tally(gates, live) {
  const t = { blue: 0, green: 0, testing: 0, red: 0, slate: 0, unobserved: 0 };
  for (const g of gates) t[gateVisual(g, live)] += 1;
  return t;
}

/**
 * FAILING CLUSTERS. A grid of squares says how many; it cannot say WHAT. Gates
 * group by their REQ, and a cluster of failures under one requirement is the
 * actionable fact — printed at full size, never in a tooltip nobody on a stream
 * can hover.
 *
 * THIS READS THE RAW STATE, NOT THE VISUAL ONE, and the difference is deliberate.
 * Live choreography repaints a failing gate as amber or slate to describe the
 * GRADER's situation; it does not revise the recorded result of the last
 * completed attempt. Keying clusters on the visual state would empty this list
 * the moment a run stalled — losing the standing failures precisely when the
 * operator is trying to work out what went wrong.
 */
function clusters(gates) {
  const by = new Map();
  for (const g of gates) {
    const st = g.a !== "unobserved" ? g.a : g.b;
    if (st !== "red") continue;
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
