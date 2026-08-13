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
// Shaped from the real payload: `id`/`req`/`title`, per-arm state, and the
// `flipped_at_attempt` that `status-stream.mjs flipAttempt()` already publishes.
// The arm-a-unobserved / arm-b-populated split mirrors a real single-arm stack.

const GATES = [
  { id: "C:a", req: "REQ-STATE", title: "resolved first go", a: "unobserved", b: "green", a_flipped_at_attempt: null, b_flipped_at_attempt: 1 },
  { id: "C:b", req: "REQ-STATE", title: "resolved late", a: "unobserved", b: "green", a_flipped_at_attempt: null, b_flipped_at_attempt: 2 },
  { id: "C:c", req: "REQ-MOVE", title: "still failing", a: "unobserved", b: "red", a_flipped_at_attempt: null, b_flipped_at_attempt: null },
  { id: "C:d", req: "REQ-MOVE", title: "also failing", a: "unobserved", b: "red", a_flipped_at_attempt: null, b_flipped_at_attempt: null },
  { id: "C:e", req: "REQ-DBL", title: "never seen failing", a: "unobserved", b: "unobserved", a_flipped_at_attempt: null, b_flipped_at_attempt: null },
];

const boardWith = (grading, extra = {}) => ({
  wall: { gates: GATES, totals: {} },
  stack: { all: [{ seq: 1, verdict: "FAIL", gates: { failed: 2, total: 5 } }] },
  events: grading === null ? {} : { grading },
  ...extra,
});

const ARMED = boardWith(null);
const RUNNING = boardWith({ grading: true, phase: "backend", attempt: "2", stalled: false, timed_out: false, silent_s: 4 });
const STALLED = boardWith({ grading: true, phase: "backend", attempt: "2", stalled: true, timed_out: false, silent_s: 900 });
const TIMEDOUT = boardWith({ grading: false, phase: "backend", attempt: "3", stalled: false, timed_out: true, silent_s: null });
const TAKEOVER = boardWith(
  { grading: true, phase: "backend", attempt: "2", stalled: false, timed_out: false, silent_s: 4 },
  { recall_moment: { fired_at: Date.now(), failure_key: "k" } },
);

/** The wall's squares, in order, as their state class. */
function slots(html) {
  const wall = html.match(/<div class="gwall[^"]*"[^>]*>([\s\S]*?)<\/div>/);
  assert.ok(wall, "no gate wall rendered");
  return [...wall[1].matchAll(/<span class="gcell ([a-z]+)"/g)].map((m) => m[1]);
}

// ── the frames ──────────────────────────────────────────────────────────────

test("resolution rides ATTEMPTS: blue is attempt 1, green is later", () => {
  const s = slots(renderWall(ARMED));
  assert.equal(s[0], "blue", "flipped_at_attempt 1 must be blue");
  assert.equal(s[1], "green", "flipped at a later attempt must be green");
});

test("blue never claims 'passed phase 1' on the surface", () => {
  const html = renderWall(ARMED);
  assert.match(html, /resolved at attempt 1/, "the legend must state what blue means");
  assert.doesNotMatch(html, /passed phase 1/i, "blue must never be glossed as passing a phase");
});

test("idle: a failing gate is red, and nothing pulses", () => {
  const s = slots(renderWall(ARMED));
  assert.deepEqual(s, ["blue", "green", "red", "red", "unobserved"]);
  assert.doesNotMatch(renderWall(ARMED), /gcell testing/, "no amber without a live grader");
});

test("running: only unresolved gates go amber, and it claims no outcome", () => {
  const html = renderWall(RUNNING);
  assert.deepEqual(slots(html), ["blue", "green", "testing", "testing", "unobserved"]);
  assert.match(html, /GRADING/, "the operator must be told why squares are moving");
  assert.match(html, /makes no claim about the outcome/, "motion must disclaim any verdict");
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

test("unobserved never pulses and never goes slate", () => {
  for (const [name, b] of [["armed", ARMED], ["running", RUNNING], ["stalled", STALLED], ["timeout", TIMEDOUT]]) {
    assert.equal(slots(renderWall(b))[4], "unobserved", `frame ${name} moved an unobserved gate`);
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

test("NO MOTION DURING A TAKEOVER — frozen, not blanked", () => {
  const html = renderWall(TAKEOVER);
  assert.match(html, /class="gwall still"/, "a fresh takeover must freeze the wall");
  // The freeze must not cost information: the amber squares are still amber.
  assert.deepEqual(slots(html), ["blue", "green", "testing", "testing", "unobserved"]);
});

test("a stall does not empty FAILING CLUSTERS", () => {
  const html = renderWall(STALLED);
  assert.match(html, /FAILING CLUSTERS/);
  assert.match(html, /REQ-MOVE/, "standing failures must survive a stall");
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
  // Killing the animation must not fall back to `transparent`, which is the
  // resting frame of the pulse and identical to a plain failing square.
  for (const rule of [
    css.match(/\.gwall\.still\s+\.gcell\.testing\{([^}]*)\}/)?.[1] ?? "",
    reduced.join("\n").match(/\.gcell\.testing\{([^}]*)\}/)?.[1] ?? "",
  ]) {
    assert.match(rule, /background:[^;]*ffb454/, "a stilled amber square must stay visibly amber");
  }
});

test("the slate square never animates", async () => {
  const css = await readFile(join(HERE, "index.html"), "utf8");
  const slate = css.match(/\.gcell\.slate\{([^}]*)\}/)?.[1] ?? "";
  assert.ok(slate, "no .gcell.slate rule");
  assert.doesNotMatch(slate, /animation/, "slate is the STILL colour — motion would imply work in progress");
});
