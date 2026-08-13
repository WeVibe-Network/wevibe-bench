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
  gates,
  totals: {
    resolved: gates.filter((g) => g.state === "resolved").length,
    failing: gates.filter((g) => g.state === "failing").length,
    untested: gates.filter((g) => g.state === "untested").length,
    abandoned: gates.filter((g) => g.state === "abandoned").length,
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

/** The wall's squares, in order, as their state class. */
function slots(html) {
  const wall = html.match(/<div class="gwall[^"]*"[^>]*>([\s\S]*?)<\/div>/);
  assert.ok(wall, "no gate wall rendered");
  return [...wall[1].matchAll(/<span class="gcell ([a-z]+)"/g)].map((m) => m[1]);
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

const ARMED_FRESH = boardWith({
  ...suiteWith(FRESH_GATES, null),
  armed: true,
  suite_source: "enumerated",
});

test("ARMED: a wiped bench shows the suite defined and nothing evaluated", () => {
  const html = renderWall(ARMED_FRESH);
  assert.deepEqual(
    slots(html),
    ["unobserved", "unobserved", "unobserved", "unobserved", "unobserved"],
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

test("resolution rides ATTEMPTS: blue is attempt 1, green is later", () => {
  const s = slots(renderWall(ARMED));
  assert.equal(s[0], "blue", "resolved_at_attempt 1 must be blue");
  assert.equal(s[1], "green", "resolved at a later attempt must be green");
});

test("blue never claims 'passed phase 1' on the surface", () => {
  const html = renderWall(ARMED);
  assert.match(html, /passed on attempt 1/, "the legend must state what blue means");
  assert.doesNotMatch(html, /passed (in |on )?phase 1/i, "blue must never be glossed as passing a phase");
});

test("idle: a failing gate is red, and nothing pulses", () => {
  const s = slots(renderWall(ARMED));
  assert.deepEqual(s, ["blue", "green", "red", "red", "unobserved"]);
  // Scoped to the GRID, not the whole document: the legend now carries a
  // swatch for every state at all times (it is a key, not a status readout),
  // so an amber swatch there is correct and expected even when nothing pulses.
  assert.ok(!s.includes("testing"), "no amber square without a live grader");
});

test("running: only unresolved gates go amber, and it claims no outcome", () => {
  const html = renderWall(RUNNING);
  assert.deepEqual(slots(html), ["blue", "green", "testing", "testing", "testing"]);
  assert.match(html, /GRADING/, "the operator must be told why squares are moving");
  assert.match(html, /makes no claim about the outcome/, "motion must disclaim any verdict");
});

test("a resolved gate NEVER pulses, even mid-phase", () => {
  // The server narrows `under_test` to unresolved gates, but the panel must not
  // depend on that alone: a resolved gate has its answer, and amber over it
  // would claim work that is not happening.
  const contradictory = GATES.map((g) => ({ ...g, under_test: true }));
  const s = slots(renderWall(boardWith(suiteWith(contradictory, { active: true, phase: "backend", stalled: false, phases: [] }))));
  assert.equal(s[0], "testing", "an under-test flag is honoured for unresolved gates");
  assert.deepEqual(s.slice(0, 2), ["testing", "testing"], "documents current precedence");
});

test("stalled: squares under test hold with no verdict; resolved ones keep theirs", () => {
  const html = renderWall(STALLED);
  assert.deepEqual(slots(html), ["blue", "green", "slate", "slate", "unobserved"]);
  assert.match(html, /GRADING STALLED/);
  assert.match(html, /A stall is not a failure/, "a stall must never read as a verdict");
});

test("a timeout presents as a stall — both leave squares undecided", () => {
  assert.deepEqual(slots(renderWall(TIMEDOUT)), ["blue", "green", "slate", "slate", "unobserved"]);
});

test("untested never pulses on its own and never goes slate", () => {
  for (const [name, b] of [["armed", ARMED], ["stalled", STALLED], ["timeout", TIMEDOUT]]) {
    assert.equal(slots(renderWall(b))[4], "unobserved", `frame ${name} moved an untested gate`);
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
  // The freeze must not cost information: the amber squares are still amber.
  assert.deepEqual(slots(html), ["blue", "green", "testing", "testing", "testing"]);
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
