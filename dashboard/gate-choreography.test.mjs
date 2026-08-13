// ─────────────────────────────────────────────────────────────────────────────
// GATE CHOREOGRAPHY — the live wall says "working", never "passing"
//
//     cd wevibe-bench/dashboard && node --test
//
// WHY THIS EXISTS
//
// Ask 3 adds the only looping animation on the board. Motion is the easiest
// thing to get wrong in a way no test catches: the wall still renders, every
// class is still styled, `node --check` is still green — and the squares are
// telling the operator that a gate PASSED when all the grader did was start
// working on it. The distinction is invisible to every other check in the tree,
// so it is pinned here.
//
// WHAT IT PINS
//
//  1. Colour rides the ATTEMPT axis. blue = resolved at attempt 1. It does NOT
//     mean "passed phase 1" — a gate that passes everything is never a square
//     at all, because the harness publishes only gates observed FAILING.
//  2. Amber appears ONLY while the grader is actually running, and never on a
//     gate that is already resolved.
//  3. A stall yields slate on the squares that were under test, and slate is
//     never inferred as a verdict — resolved squares keep their colour.
//  4. `unobserved` never pulses and never goes slate. It was not under test.
//  5. SLOTS NEVER REFLOW: the slot count and order are byte-identical across
//     all five frames.
//  6. A stall does not empty FAILING CLUSTERS — the standing failures survive
//     exactly when the operator needs them most.
//  7. Both motion kill-switches leave a state that is still distinguishable.
//
// THE DOM STUB. `board.js` is both the browser entry point and the module that
// exports `esc`/`nul`/`clip`, so importing any panel executes its listener
// registration and first paint. The stub below is the smallest thing that lets
// a pure string builder be tested in node; it asserts nothing and stands in for
// no behaviour.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// The REAL server-side state machine. Imported so the fixtures cannot drift
// from what /api/wall actually emits.
import { choreographGate } from "../control/choreography.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

const noop = () => {};
/**
 * A stub element.
 *
 * `content` is present because `dom.js patch()` parses into a detached
 * `<template>` and reads `tpl.content`. Importing any panel pulls in board.js,
 * whose module scope calls `poll()` → `render()` → `patch()`, so a stub without
 * `content` throws "Cannot read properties of undefined (reading 'childNodes')"
 * asynchronously, AFTER the assertions have all passed — the file then exits 1
 * while reporting 13/13 ok, which reads as a spurious failure and invites
 * someone to ignore a real one later.
 *
 * This stub predates the move from `root.innerHTML = ...` to morphing
 * (WO-BOARD-FREEZE-DOM-1) and was never updated for it. `childNodes: []` is the
 * whole fix: patchChildren then reconciles against an empty child list and
 * returns without touching anything.
 */
const stubEl = () => ({
  innerHTML: "",
  style: {},
  classList: { add: noop, remove: noop },
  appendChild: noop,
  addEventListener: noop,
  childNodes: [],
  content: { childNodes: [] },
});
globalThis.document = {
  addEventListener: noop,
  getElementById: stubEl,
  createElement: stubEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  body: { appendChild: noop },
};
globalThis.window = {
  addEventListener: noop,
  matchMedia: () => ({ matches: false, addEventListener: noop }),
};
globalThis.setInterval = noop;

const { renderWall } = await import("./panels/wall.js");

// ── fixtures ────────────────────────────────────────────────────────────────
//
// Shaped from the REAL /api/wall payload (control/wall.mjs foldGateStates):
// `id`/`req`/`title` plus a server-decided `state` and `resolved_at_attempt`.
//
// ── WHAT CHANGED, AND WHY IT IS NOT A WEAKENING ─────────────────────────────
//
// These fixtures used to carry per-arm `red`/`green`/`unobserved` derived from
// failure lists, and the PANEL decided that a failing gate became slate when
// the run stalled. That inference now lives in the control plane, which is the
// only tier that can see the run's terminal status: a cold log is not a stop,
// and the panel could not tell those apart. So `abandoned` arrives as state and
// the panel maps it to slate. The invariant is unchanged and still pinned
// below — a stall must never read as a verdict — but it is now asserted against
// the payload that actually decides it.

const GATES = [
  { id: "C:a", req: "REQ-STATE", title: "resolved first go", state: "resolved", resolved_at_attempt: 1, under_test: false },
  { id: "C:b", req: "REQ-STATE", title: "resolved late", state: "resolved", resolved_at_attempt: 2, under_test: false },
  { id: "C:c", req: "REQ-MOVE", title: "still failing", state: "failing", resolved_at_attempt: null, under_test: false },
  { id: "C:d", req: "REQ-MOVE", title: "also failing", state: "failing", resolved_at_attempt: null, under_test: false },
  { id: "C:e", req: "REQ-DBL", title: "not yet tested", state: "untested", resolved_at_attempt: null, under_test: false },
];

// ── THE CHOREOGRAPHY IS ATTACHED BY THE REAL SERVER FUNCTION ────────────────
//
// `control/choreography.mjs` is imported rather than restated, so these
// fixtures carry EXACTLY the payload /api/wall emits. A hand-written
// choreography block here would let the panel and the server drift while every
// test stayed green — which is the one failure this file exists to catch.
//
// The history each fixture implies:
//   C:a  pass at 1                → blue   (passed, never failed)
//   C:b  fail at 1, pass at 2     → green  (recovered)
//   C:c  fail at 1, fail at 2     → red
//   C:d  PASS at 1, FAIL at 2     → red + regression star  ← the directive's case
//   C:e  never executed           → none
const HISTORY = {
  "C:a": [{ attempt: 1, status: "pass" }],
  "C:b": [{ attempt: 1, status: "fail" }, { attempt: 2, status: "pass" }],
  "C:c": [{ attempt: 1, status: "fail" }, { attempt: 2, status: "fail" }],
  "C:d": [{ attempt: 1, status: "pass" }, { attempt: 2, status: "fail" }],
  "C:e": [],
};

/** Attach real server-computed choreography to a gate list. */
const choreo = (gates) =>
  gates.map((g) => ({
    ...g,
    choreography: choreographGate({
      history: HISTORY[g.id] ?? [],
      underTest: g.under_test === true,
      abandoned: g.state === "abandoned",
    }),
  }));


/** Mark the two failing gates as under test, the way the server does mid-phase. */
const underTest = (gates) =>
  gates.map((g) => ({ ...g, under_test: g.state === "failing" || g.state === "untested" ? true : false }));

/** The two failing gates abandoned by a stop, as the server would fold them. */
const abandoned = (gates) =>
  gates.map((g) => (g.state === "failing" ? { ...g, state: "abandoned" } : g));

const suiteWith = (gates, grading) => ({
  ok: true,
  contract_version: 1,
  run_dir: "cumulative",
  suite: { total: gates.length, complete: true, fingerprint: "sha256:test", by_phase: null, by_tier: null },
  attempt: { current: 2, max: 3 },
  grading,
  live_signal: { conformance: "per-phase-set", backend: "per-phase-set", frontend: "per-phase-set" },
  gates: choreo(gates),
  totals: {
    resolved: gates.filter((g) => g.state === "resolved").length,
    failing: gates.filter((g) => g.state === "failing").length,
    untested: gates.filter((g) => g.state === "untested").length,
    abandoned: gates.filter((g) => g.state === "abandoned").length,
    regressed: choreo(gates).filter((g) => g.choreography.regressed).length,
  },
  unwired: [],
  unwired_reasons: {},
});

const boardWith = (suite, extra = {}) => ({ suite, events: {}, ...extra });

const ARMED = boardWith(suiteWith(GATES, null));
const RUNNING = boardWith(
  suiteWith(underTest(GATES), { active: true, phase: "backend", stalled: false, timed_out: false, silent_s: 4, phases: [] }),
);
const STALLED = boardWith(
  suiteWith(abandoned(GATES), { active: true, phase: "backend", stalled: true, timed_out: false, silent_s: 900, phases: [] }),
);
const TIMEDOUT = boardWith(
  suiteWith(abandoned(GATES), { active: false, phase: "backend", stalled: false, timed_out: true, silent_s: null, phases: [] }),
);
const TAKEOVER = boardWith(
  suiteWith(underTest(GATES), { active: true, phase: "backend", stalled: false, timed_out: false, silent_s: 4, phases: [] }),
  { recall_moment: { fired_at: Date.now(), failure_key: "k" } },
);

/**
 * The wall's squares, in order, as their FILL class.
 *
 * Reads `gf-*` only. Fill is the axis that carries the VERDICT, so every
 * assertion about what a square says is an assertion about its fill; motion and
 * mark are read separately by `motions()` and `marks()` below. Splitting them
 * is the point of the three-family class scheme — a single compound token would
 * force every test to re-parse meaning out of a string.
 */
function slots(html) {
  const wall = html.match(/<div class="gwall[^"]*"[^>]*>([\s\S]*?)<\/div>/);
  assert.ok(wall, "no gate wall rendered");
  return [...wall[1].matchAll(/<span class="gcell ([^"]*)"/g)].map(
    (m) => (m[1].match(/gf-(\w+)/) ?? [])[1] ?? "?",
  );
}

/** The MOTION axis per square: "pulse" or "still". */
function motions(html) {
  const wall = html.match(/<div class="gwall[^"]*"[^>]*>([\s\S]*?)<\/div>/);
  assert.ok(wall, "no gate wall rendered");
  return [...wall[1].matchAll(/<span class="gcell ([^"]*)"/g)].map(
    (m) => (m[1].match(/gm-(\w+)/) ?? [])[1] ?? "?",
  );
}

/** The MARK axis per square: "regression" or null. */
function marks(html) {
  const wall = html.match(/<div class="gwall[^"]*"[^>]*>([\s\S]*?)<\/div>/);
  assert.ok(wall, "no gate wall rendered");
  return [...wall[1].matchAll(/<span class="gcell ([^"]*)"/g)].map(
    (m) => (m[1].match(/gk-(\w+)/) ?? [])[1] ?? null,
  );
}

// ── the frames ──────────────────────────────────────────────────────────────

// NOTE ON NAMING: the `ARMED` fixture above predates the server's `armed` flag
// and means IDLE — a graded run with outcomes and no live grader. A genuinely
// armed wall is the one below: every gate untested, nothing evaluated, and the
// server saying so. The two must not be confused; they are opposite states that
// look alike in a grid, which is exactly why the server decides it.

const FRESH_GATES = GATES.map((g) => ({
  ...g,
  state: "untested",
  resolved_at_attempt: null,
  under_test: false,
}));

// A genuinely armed wall has NO history for any gate — nothing has been
// evaluated. `suiteWith` attaches choreography from HISTORY by id, so the armed
// fixture must clear it explicitly; otherwise the squares would carry verdicts
// from a run that, in this frame, has not happened.
const ARMED_FRESH = boardWith({
  ...suiteWith(FRESH_GATES, null),
  gates: FRESH_GATES.map((g) => ({
    ...g,
    choreography: choreographGate({ history: [], underTest: false, abandoned: false }),
  })),
  armed: true,
  suite_source: "enumerated",
});

test("ARMED: a wiped bench shows the suite defined and nothing evaluated", () => {
  const html = renderWall(ARMED_FRESH);
  assert.deepEqual(
    slots(html),
    ["none", "none", "none", "none", "none"],
    "every square is a dotted outline: defined, not yet tested",
  );
  assert.match(html, /ARMED/, "the state must be named, not inferred from colour");
  assert.match(html, /0<\/span>\/5 tested/, "0 of N TESTED — never 'passed', nothing was tested");
  assert.doesNotMatch(html, /GRADING STALLED/, "an armed wall must not report a stall");
  // Scoped to the HEADLINE: the "FAILING CLUSTERS" section is a permanent
  // heading that correctly reads "no gate is currently failing" here, so a
  // document-wide match would fail on honest output.
  assert.doesNotMatch(html, /\d+ FAILING</, "armed must never post a failing count");
});

test("ARMED: the board states where the suite shape came from", () => {
  // A suite enumerated before any cell is NOT a suite a run was graded against,
  // and the operator must be able to tell those apart.
  assert.match(renderWall(ARMED_FRESH), /enumerated from the harness suite/);
  const pinned = boardWith({ ...suiteWith(FRESH_GATES, null), armed: true, suite_source: "run" });
  assert.match(renderWall(pinned), /pinned to at cell start/);
});

test("BLUE is 'passed having never failed'; GREEN is 'recovered'", () => {
  // THE DIRECTIVE'S PHASE-2/3 RULE. Blue and green are not two flavours of
  // pass: blue says the gate was never broken, green says it was broken and is
  // now fixed. Collapsing them loses the recovery, which is the whole point of
  // running more than one attempt.
  const s = slots(renderWall(ARMED));
  assert.equal(s[0], "blue", "passed at its first execution, never failed → blue");
  assert.equal(s[1], "green", "failed then passed → green");
});

test("REGRESSION: a gate that was passing and now fails is red AND starred", () => {
  // THE DIRECTIVE'S MOST IMPORTANT RULE. C:d passed in attempt 1 and failed in
  // attempt 2. In the resting states that is indistinguishable from a gate that
  // never passed — both are simply "failing" — so without the mark the single
  // most alarming event in a run is invisible.
  const html = renderWall(ARMED);
  assert.equal(slots(html)[3], "red", "a regressed gate shows its CURRENT verdict");
  assert.equal(marks(html)[3], "regression", "and carries the persistent star");
  assert.equal(marks(html)[2], null, "a gate that never passed must NOT be starred");
  assert.match(html, /REGRESSED/, "the star must be explained in words, not left as a glyph");
});

test("the star is an OVERLAY, never a colour — it survives a recovery", () => {
  // "star persists forever for that run". A gate that broke and was then fixed
  // is green AND still starred: the fill reports where it stands now, the mark
  // reports that it is unstable. Encoding the regression as a colour would force
  // a choice between those two true facts.
  const c = choreographGate({
    history: [
      { attempt: 1, status: "pass" },
      { attempt: 2, status: "fail" },
      { attempt: 3, status: "pass" },
    ],
  });
  assert.equal(c.fill, "green", "it is passing again");
  assert.equal(c.mark, "regression", "and the run's instability is still recorded");
});

test("idle: a failing gate is red, and nothing pulses", () => {
  const html = renderWall(ARMED);
  assert.deepEqual(slots(html), ["blue", "green", "red", "red", "none"]);
  assert.ok(!motions(html).includes("pulse"), "no motion without a live grader");
});

test("FIRST LOOK is pulsing WHITE, and only a first look can be white", () => {
  // THE DIRECTIVE'S PHASE-1 RULE. A gate being worked on for the very first time
  // has no prior colour to pulse, so it gets white — reserved exclusively for
  // that moment, so a white square always means "we have never seen this gate
  // before" and can never be confused with "we are unsure".
  const html = renderWall(RUNNING);
  const s = slots(html);
  const m = motions(html);
  assert.equal(s[4], "white", "C:e has never executed — its first test is white");
  assert.equal(m[4], "pulse", "and it pulses");
  for (const i of [0, 1, 2, 3]) {
    assert.notEqual(s[i], "white", `slot ${i} has history and must never be white`);
  }
});

test("RE-TESTING pulses the EXISTING colour, never a new one", () => {
  // THE DIRECTIVE'S PHASE-2/3 RULE. A gate under test after the first time keeps
  // the last verdict it earned and animates it. Repainting it amber would throw
  // away the only thing currently known to be true about it.
  const html = renderWall(RUNNING);
  const s = slots(html);
  const m = motions(html);
  assert.equal(s[2], "red", "a failing gate under re-test stays red");
  assert.equal(m[2], "pulse", "and pulses to show work is happening");
  assert.equal(s[3], "red", "the regressed gate also keeps its colour");
  assert.equal(marks(html)[3], "regression", "and keeps its star while pulsing");
  assert.match(html, /GRADING/, "the operator must be told why squares are moving");
  assert.match(html, /makes no claim about the outcome/, "motion must disclaim any verdict");
});

test("a resolved gate NEVER pulses, even mid-phase", () => {
  // The server narrows `under_test` to unresolved gates, but the panel must not
  // depend on that alone: a resolved gate has its answer, and motion over it
  // would claim work that is not happening.
  const contradictory = GATES.map((g) => ({ ...g, under_test: true }));
  const html = renderWall(boardWith(suiteWith(contradictory, { active: true, phase: "backend", stalled: false, phases: [] })));
  assert.equal(motions(html)[0], "pulse", "an under-test flag is honoured");
  assert.equal(slots(html)[0], "blue", "but the resolved colour is preserved, not overwritten");
});

test("stalled: squares under test hold with no verdict; resolved ones keep theirs", () => {
  const html = renderWall(STALLED);
  assert.deepEqual(slots(html), ["blue", "green", "slate", "slate", "none"]);
  assert.ok(!motions(html).includes("pulse"), "an abandoned square never moves");
  assert.match(html, /GRADING STALLED/);
  assert.match(html, /A stall is not a failure/, "a stall must never read as a verdict");
});

test("a timeout presents as a stall — both leave squares undecided", () => {
  assert.deepEqual(slots(renderWall(TIMEDOUT)), ["blue", "green", "slate", "slate", "none"]);
});

test("untested never pulses on its own and never goes slate", () => {
  for (const [name, b] of [["armed", ARMED], ["stalled", STALLED], ["timeout", TIMEDOUT]]) {
    assert.equal(slots(renderWall(b))[4], "none", `frame ${name} moved an untested gate`);
  }
});

// ── the invariants ──────────────────────────────────────────────────────────

test("SLOTS NEVER REFLOW: count and order are identical across every frame", () => {
  const frames = [ARMED, RUNNING, STALLED, TIMEDOUT, TAKEOVER].map((b) => slots(renderWall(b)));
  for (const f of frames) assert.equal(f.length, GATES.length, "the slot count moved");
  // Same gate, same slot index, in every frame — only the colour may differ.
  const ids = (html) => [...html.matchAll(/title="(C:[a-z])/g)].map((m) => m[1]);
  const order = ids(renderWall(ARMED));
  assert.deepEqual(order, ["C:a", "C:b", "C:c", "C:d", "C:e"]);
  for (const b of [RUNNING, STALLED, TIMEDOUT, TAKEOVER]) {
    assert.deepEqual(ids(renderWall(b)), order, "gate order changed between frames");
  }
});

test("the grid is a FIXED 12 columns at every suite size (design §9.3)", () => {
  // The column count used to adapt to the gate count (8 / 12 / 19), so the wall
  // reshaped itself between runs of different suite sizes and slot N was not
  // slot N across cells.
  for (const b of [ARMED, RUNNING, STALLED]) {
    assert.match(renderWall(b), /grid-template-columns:repeat\(12,1fr\)/);
  }
  const one = boardWith(suiteWith([GATES[0]], null));
  assert.match(renderWall(one), /grid-template-columns:repeat\(12,1fr\)/, "a small suite must not reflow the grid");
});

test("NO MOTION DURING A TAKEOVER — frozen, not blanked", () => {
  const html = renderWall(TAKEOVER);
  assert.match(html, /class="gwall still"/, "a fresh takeover must freeze the wall");
  // The freeze must not cost information: every square still reports the same
  // verdict. Only the motion is suppressed, and it is suppressed in CSS — the
  // markup still carries `gm-pulse` so nothing is lost, it is merely held still.
  assert.deepEqual(slots(html), ["blue", "green", "red", "red", "white"]);
  assert.deepEqual(marks(html)[3], "regression", "the star survives the freeze");
});

test("a stall does not empty FAILING CLUSTERS", () => {
  // Clusters key on the RECORDED state, so a re-run repainting squares amber
  // must not erase the standing failures.
  const html = renderWall(boardWith(suiteWith(underTest(GATES), { active: true, phase: "backend", stalled: false, phases: [] })));
  assert.match(html, /FAILING CLUSTERS/);
  assert.match(html, /REQ-MOVE/, "standing failures must survive a re-run");
  assert.doesNotMatch(html, /no gate is currently failing/);
});

test("the stall threshold is never recomputed on this side", async () => {
  const src = await readFile(join(HERE, "panels/wall.js"), "utf8");
  // COMMENTS ARE STRIPPED FIRST. The header deliberately NAMES
  // GATE_STALL_THRESHOLD_S to record why the threshold lives in gate-events.mjs
  // and not here; matching raw source would fail on the very documentation this
  // test exists to protect.
  const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(
    code,
    /STALL_THRESHOLD|Date\.parse|silent_s\s*[><]/,
    "the wall must consume gradingStatus().stalled, not derive a second stall",
  );
  assert.match(code, /gr\.stalled/, "the wall must read the derived flag");
});

// ── presentation guarantees ─────────────────────────────────────────────────

test("both motion kill-switches leave a distinguishable static state", async () => {
  const css = await readFile(join(HERE, "index.html"), "utf8");
  const reduced = css.match(/@media\s*\(prefers-reduced-motion:reduce\)\s*\{[\s\S]*?\n\}/g) ?? [];
  assert.ok(
    reduced.some((b) => /\.gcell\.testing[\s\S]*?animation:\s*none/.test(b)),
    "prefers-reduced-motion must kill the gate pulse",
  );
  assert.match(css, /\.gwall\.still\s+\.gcell\.testing\{[^}]*animation:\s*none/, "the takeover freeze must kill the pulse");

  // Killing the animation must leave a square that is still visibly amber and
  // still distinguishable from every other state.
  //
  // THE MECHANISM CHANGED WITH THE DESIGN-SPEC REBUILD, THE RULE DID NOT.
  // Amber used to be painted BY the keyframes (the resting frame was
  // `transparent`), so a kill-switch that only stopped the animation left a
  // square identical to a plain failing one — which is what this assertion was
  // written to catch. Amber is now the cell's own background and the pulse
  // modulates opacity, so the base rule carries the colour and the kill-switch
  // must simply not blank it.
  const base = css.match(/\.gcell\.testing\{([^}]*)\}/)?.[1] ?? "";
  assert.match(base, /background:[^;]*ffb454/, "amber must be the cell's own fill, not only a keyframe");

  for (const rule of [
    css.match(/\.gwall\.still\s+\.gcell\.testing\{([^}]*)\}/)?.[1] ?? "",
    reduced.join("\n").match(/\.gcell\.testing\{([^}]*)\}/)?.[1] ?? "",
  ]) {
    assert.doesNotMatch(rule, /background:\s*transparent/, "a stilled amber square must never blank to transparent");
    assert.match(rule, /opacity:\s*\.?\d/, "the static fallback must remain visually distinct from a full-strength square");
  }
});

test("the slate square never animates", async () => {
  const css = await readFile(join(HERE, "index.html"), "utf8");
  const slate = css.match(/\.gcell\.slate\{([^}]*)\}/)?.[1] ?? "";
  assert.ok(slate, "no .gcell.slate rule");
  assert.doesNotMatch(slate, /animation/, "slate is the STILL colour — motion would imply work in progress");
});

// ── THE WALL DESCRIBES ONE CELL (defect fixed 2026-08-13) ────────────────────
//
// Observed live: the grid correctly said "the grader has not run yet" for the
// live cell while the headline beside it read "run 02 · 34/43 obs passed ·
// VERDICT FAIL" — from a run abandoned the previous day. One panel, two cells.
//
// THE FIX MOVED TIERS, SO THESE ASSERTIONS MOVED WITH IT. The headline used to
// pick a cell out of `stack.all`, which SPANS RUN DIRECTORIES by design, and
// the panel filtered it back down using source provenance. The wall now reads
// `/api/wall`, which the control plane resolves against ONE `run_dir` before it
// ever reaches the browser — so cross-run contamination is structurally
// impossible here rather than filtered out after the fact. What remains
// testable at this tier, and is tested below, is that the panel never invents a
// ratio when the surface does not carry one.

test("WALL: no suite surface means no claim at all", () => {
  const html = renderWall({ suite: null, events: {} });
  assert.match(html, /GATE SUITE UNAVAILABLE/, "an absent surface must be stated");
  assert.doesNotMatch(html, /\d+\/\d+/, "no ratio may be invented from an absent surface");
});

test("WALL: an unknown suite size is stated, NEVER rendered as zero", () => {
  // A run predating the roster artifact. `total: null` means UNKNOWABLE, and
  // the whole point of the roster work is that this never reads as 0/0.
  const html = renderWall({
    suite: {
      ok: true,
      suite: { total: null, complete: false },
      attempt: { current: null, max: null },
      grading: null,
      gates: [],
      totals: null,
      unwired: ["gate-roster"],
      unwired_reasons: { "gate-roster": "no readable gate-roster.json in runs/cumulative" },
    },
    events: {},
  });
  assert.match(html, /SUITE SIZE UNKNOWN — NOT ZERO/);
  assert.doesNotMatch(html, /\b0\/0\b/, "an unknown denominator must never render as zero");
  assert.match(html, /no readable gate-roster\.json/, "the reason must be shown, not swallowed");
});

test("WALL: a known suite with no outcomes yet says so, and states why", () => {
  const html = renderWall({
    suite: {
      ok: true,
      suite: { total: 71, complete: true },
      attempt: { current: 1, max: 3 },
      grading: null,
      gates: [],
      totals: null,
      unwired: ["gate-outcomes"],
      unwired_reasons: { "gate-outcomes": "per-gate outcomes land in manifest.status.jsonl at attempt end" },
    },
    events: {},
  });
  assert.match(html, /NO GATE OUTCOMES YET/);
  assert.match(html, /at attempt end/, "the normal early-cell state must be explained");
});

test("WALL: the ratio is over the TRUE suite size, not the observed count", () => {
  // The defect this whole rebuild removed: the denominator used to be "gates
  // observed failing", so a wall of 5 squares claimed a suite of 5.
  const html = renderWall(ARMED);
  assert.match(html, /<span class="bright">2<\/span>\/5 passed/, "2 resolved of a 5-gate suite");
  assert.doesNotMatch(html, /\bobs\b/, "the observed-only denominator label must be gone");
});

// ── THE LIVE LANE OVERLAY ───────────────────────────────────────────────────
//
// The lane is PROVISIONAL. Everything below pins the property that makes it
// safe to show at all: it annotates the authoritative grid, and can never be
// mistaken for it, replace it, or grow it.

/** A board carrying both an authoritative suite and a live-lane overlay. */
const withLive = (gates, live) => ({
  suite: suiteWith(gates, null),
  events: {},
  live,
});

const LANE_OK = {
  ok: true,
  running: true,
  stale: false,
  age_s: 2,
  lane: {
    provisional: true,
    snapshot: { parsed: true, stale_reason: null, content_hash: "sha256:x" },
    duration_ms: 2100,
    counts: { pass: 3, fail: 1, deferred: 1, not_loaded: 0, unmeasured: 0, total: 5 },
    gates: [
      { id: "C:a", live: "pass" },
      { id: "C:b", live: "pass" },
      { id: "C:c", live: "fail" },
      { id: "C:d", live: "deferred", deferred_reason: "owns :8002" },
      { id: "C:e", live: "pass" },
    ],
  },
  build: null,
};

test("LIVE: the overlay annotates squares and NEVER changes their verdict", () => {
  // THE INVARIANT THAT MAKES THIS SAFE. The authoritative fill is decided by
  // the graded fold; the lane may only add a ring on top. If a live "pass"
  // could repaint a red square green, the board would be showing an unscored
  // result as a scored one — the exact two-sources-of-truth failure RC-5 forbids.
  const html = renderWall(withLive(GATES, LANE_OK));
  assert.deepEqual(slots(html), ["blue", "green", "red", "red", "none"], "fills are untouched by the lane");
  const cls = [...html.match(/<div class="gwall[^"]*"[^>]*>([\s\S]*?)<\/div>/)[1]
    .matchAll(/<span class="gcell ([^"]*)"/g)].map((m) => m[1]);
  assert.match(cls[0], /gl-pass/, "a live pass adds its own overlay class");
  assert.match(cls[2], /gl-fail/, "and a live fail adds a different one");
  assert.ok(!/gl-/.test(cls[3]) || /gl-deferred/.test(cls[3]), "a deferred gate carries no verdict ring");
});

test("LIVE: the panel says PROVISIONAL in words, not just in colour", () => {
  const html = renderWall(withLive(GATES, LANE_OK));
  assert.match(html, /LIVE LANE — PROVISIONAL/);
  assert.match(html, /not scored/i, "the operator must be told these numbers do not count");
  assert.match(html, /3 passing · 1 failing · 1 never measured live/);
});

test("LIVE: a STALE lane drops its overlay entirely and says why", () => {
  // A lane that stopped is describing a past snapshot. Continuing to paint its
  // rings would present a dead instrument's last reading as a current one.
  const stale = { ...LANE_OK, running: false, stale: true, stale_reason: "not republished for 90s" };
  const html = renderWall(withLive(GATES, stale));
  assert.match(html, /LIVE LANE STOPPED/);
  const grid = html.match(/<div class="gwall[^"]*"[^>]*>([\s\S]*?)<\/div>/)[1];
  assert.ok(!/gl-pass|gl-fail/.test(grid), "a stale lane must not paint a single verdict ring");
});

test("LIVE: a snapshot that does not compile is stated, not shown as failures", () => {
  // The most useful thing the line can say: it explains grey squares that would
  // otherwise be read as the model breaking things.
  const broken = {
    ...LANE_OK,
    lane: {
      ...LANE_OK.lane,
      snapshot: { parsed: false, stale_reason: "3 spec file(s) failed to import", content_hash: "x" },
    },
  };
  const html = renderWall(withLive(GATES, broken));
  assert.match(html, /SNAPSHOT DOES NOT COMPILE/);
  assert.match(html, /failed to import/);
});

test("LIVE: a PAUSED lane never reports its held counts as a current measurement", () => {
  // `parsed:null` + a reason is what the lane publishes when its pause gate
  // blocked the cycle (control/live-lane.mjs:446) — the grader has an open phase
  // or :8002 is bound. It HOLDS the prior grid, so `counts` is the LAST
  // measurement. Reporting those as PROVISIONAL would present a paused
  // instrument's stale reading as current, during grading, which is precisely
  // when the pause gate is active and an operator is watching.
  const paused = {
    ...LANE_OK,
    lane: {
      ...LANE_OK.lane,
      ran_at: null,
      duration_ms: null,
      snapshot: {
        parsed: null,
        stale_reason: ":8002 is bound — the lane's own freePort() would SIGKILL whatever holds it",
        content_hash: "x",
      },
    },
  };
  const html = renderWall(withLive(GATES, paused));
  assert.match(html, /LIVE LANE — PAUSED/);
  assert.match(html, /:8002 is bound/, "the operator must be told WHY it paused");
  assert.ok(
    !/LIVE LANE — PROVISIONAL/.test(html),
    "a paused lane must not claim a provisional measurement it did not take",
  );
  assert.ok(
    !/3 passing · 1 failing/.test(html),
    "the held counts are a past measurement and must not be presented as current",
  );
});

test("LIVE: absent lane renders NOTHING — an optional instrument is silent", () => {
  // `live: null` is the normal state for every run that does not start the lane.
  // A permanent "live lane: off" row would imply the board is missing something.
  const html = renderWall({ suite: suiteWith(GATES, null), events: {} });
  assert.doesNotMatch(html, /LIVE LANE/);
  assert.doesNotMatch(html, /BUILD — FILE POPULATION/);
});

test("LIVE: a live id with no roster slot can NEVER grow the grid", () => {
  // The roster is the only thing permitted to define the suite. A lane that
  // disagreed with it must not be able to add a square.
  const rogue = {
    ...LANE_OK,
    lane: { ...LANE_OK.lane, gates: [...LANE_OK.lane.gates, { id: "GHOST", live: "pass" }] },
  };
  assert.equal(slots(renderWall(withLive(GATES, rogue))).length, GATES.length);
});

test("BUILD STRIP: renders the construction axis and disclaims correctness", () => {
  const withBuild = {
    ...LANE_OK,
    build: {
      files: [
        { path: "src/game.ts", state: "partial", metric: "stub-ratio", fill: 0.5, stubs_remaining: 6, stubs_initial: 12, lines: 200, reference_lines: 357 },
        { path: "public/app.js", state: "stub", metric: "line-ratio", fill: 0, stubs_remaining: null, stubs_initial: null, lines: 12, reference_lines: 586 },
      ],
      totals: { stubs_remaining: 6, stubs_initial: 12, fill: 0.5 },
    },
  };
  const html = renderWall(withLive(GATES, withBuild));
  assert.match(html, /BUILD — FILE POPULATION/);
  assert.match(html, /6\/12 scaffold stubs implemented/);
  assert.match(html, /can still fail every gate/, "fill must never read as a pass rate");
  // The two metrics are visibly different measurements.
  assert.match(html, /stubs implemented/);
  assert.match(html, /lines \(reference 586\)/);
});
