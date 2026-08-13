// ─────────────────────────────────────────────────────────────────────────────
// GATE WALL — the dumb component stays dumb
//
//     cd wevibe-bench/dashboard && node --test
//
// WHY THIS EXISTS
//
// The wall grew from 124 lines to 681 across five sessions. Every addition was
// individually defensible — an attempt axis, a live amber pulse, an abandoned
// state, a phase-set disclosure, a three-axis choreography — and together they
// made a panel that decided more about a gate than the grader did. This file
// pins it back down.
//
// WHAT IT PINS
//
//  1. THREE STATES REACH THE SCREEN, and only three: passing, failing,
//     untested. Any other `state` the server could ever send renders as
//     untested, never as a pass.
//  2. TWO COLOURS. green and red are the only fills. No blue, no amber, no
//     slate — those were the attempt axis and the live signal, and both are
//     gone.
//  3. NO PHASE ANYWHERE. Not in the markup, not in the classes, not in the
//     prose. A gate's phase is not a fact about its result.
//  4. NO MOTION. The wall never animates, so nothing on it can be read as
//     "still working" — the panel has no live signal to report.
//  5. UNTESTED IS NOT A WEAKER PASS. It renders distinctly from green, and the
//     headline never counts it toward passing.
//  6. THE DENOMINATOR IS NEVER FABRICATED. `total: null` says so in words and
//     never prints 0.
//  7. SLOTS NEVER REFLOW: slot count and order are byte-stable as states change.
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

const { renderWall, gateVisual } = await import("./panels/wall.js");

// ── fixtures ────────────────────────────────────────────────────────────────
//
// Shaped from the REAL /api/wall payload (control/wall.mjs foldGateStates):
// `id`/`req`/`title` plus a server-decided `state`, and nothing else. If a
// fixture here needs a field the server does not send, the panel is deriving.

const GATES = [
  { id: "C:a", req: "REQ-STATE", title: "passing", state: "passing" },
  { id: "C:b", req: "REQ-STATE", title: "also passing", state: "passing" },
  { id: "C:c", req: "REQ-MOVE", title: "failing", state: "failing" },
  { id: "C:d", req: "REQ-MOVE", title: "also failing", state: "failing" },
  { id: "C:e", req: "REQ-DBL", title: "not yet tested", state: "untested" },
];

const suiteWith = (gates, over = {}) => ({
  ok: true,
  contract_version: 2,
  run_dir: "cumulative",
  suite_source: "run",
  suite: { total: gates.length, fingerprint: "fp", complete: true, incomplete_reason: null, captured_at: null },
  attempt: 2,
  gates,
  totals: {
    passing: gates.filter((g) => g.state === "passing").length,
    failing: gates.filter((g) => g.state === "failing").length,
    untested: gates.filter((g) => g.state === "untested").length,
  },
  unwired: [],
  unwired_reasons: {},
  ...over,
});

const boardWith = (suite) => ({ suite });

/** Every `class="..."` value appearing on a grid square. */
function cellClassList(html) {
  return [...html.matchAll(/<span class="gcell ([^"]*)"/g)].map((m) => m[1].trim());
}

// ── 1 & 2. three states, two colours ────────────────────────────────────────

test("passing is green, failing is red, untested is the uncoloured square", () => {
  assert.equal(gateVisual({ state: "passing" }), "green");
  assert.equal(gateVisual({ state: "failing" }), "red");
  assert.equal(gateVisual({ state: "untested" }), "unobserved");
});

test("only three visual classes can ever reach a square", () => {
  const html = renderWall(boardWith(suiteWith(GATES)));
  const classes = new Set(cellClassList(html).map((c) => c.replace(/\s*sm\s*/, "")));
  assert.deepEqual([...classes].sort(), ["green", "red", "unobserved"]);
});

test("the retired states are gone — no blue, no amber, no slate", () => {
  // The whole grid, the legend swatches, and every class string.
  const html = renderWall(boardWith(suiteWith(GATES)));
  for (const dead of ["blue", "testing", "slate", "settled", "gf-", "gm-", "gk-", "gl-"]) {
    assert.ok(!html.includes(dead), `retired visual "${dead}" is still emitted by the wall`);
  }
});

test("an UNKNOWN server state renders as untested, never as a pass", () => {
  // Fail-safe direction. A control plane that grows a fourth state must degrade
  // to "no result", because the alternative is a square claiming a pass nobody
  // measured.
  assert.equal(gateVisual({ state: "abandoned" }), "unobserved");
  assert.equal(gateVisual({ state: "resolved" }), "unobserved");
  assert.equal(gateVisual({}), "unobserved");
});

// ── 3. no phase, anywhere ───────────────────────────────────────────────────

test("the rendered wall never mentions a phase", () => {
  const html = renderWall(boardWith(suiteWith(GATES)));
  assert.ok(!/phase/i.test(html), "the wall must carry no phase distinction of any kind");
});

test("the wall SOURCE carries no phase logic", async () => {
  const src = await readFile(join(HERE, "panels/wall.js"), "utf8");
  const code = src
    .split("\n")
    .filter((l) => !l.trim().startsWith("//") && !l.trim().startsWith("*") && !l.trim().startsWith("/*"))
    .join("\n");
  for (const banned of ["under_test", "live_signal", "grading", "stalled", "armed", "resolved_at_attempt"]) {
    assert.ok(!code.includes(banned), `panels/wall.js still reads "${banned}" — that is phase/live logic`);
  }
});

// ── 4. no motion ────────────────────────────────────────────────────────────

test("the wall emits no animation hook and the stylesheet defines none for it", async () => {
  const html = renderWall(boardWith(suiteWith(GATES)));
  assert.ok(!html.includes("still"), "the takeover freeze is gone because there is nothing to freeze");

  const css = await readFile(join(HERE, "index.html"), "utf8");
  const wallRules = css
    .split("\n")
    .filter((l) => /^\.(gwall|gcell|wall-)/.test(l.trim()));
  assert.ok(wallRules.length > 0, "the wall must still be styled");
  for (const rule of wallRules) {
    assert.ok(!/animation/.test(rule), `the wall must not animate: ${rule}`);
  }
});

// ── 5. untested is not a weaker pass ────────────────────────────────────────

test("untested renders distinctly from passing and is never counted as passing", () => {
  const html = renderWall(boardWith(suiteWith(GATES)));
  assert.ok(html.includes(`<span class="bright">2</span>/5 passing`), "2 of 5 pass; the untested one is not one of them");
  assert.ok(html.includes("1 not yet tested"), "the untested count is stated, not swallowed");
  assert.ok(html.includes("not yet tested"), "and named in the legend");
});

test("a wall with nothing tested reports zero passing, not an empty grid", () => {
  const gates = GATES.map((g) => ({ ...g, state: "untested" }));
  const html = renderWall(boardWith(suiteWith(gates)));
  assert.ok(html.includes(`<span class="bright">0</span>/5 passing`));
  assert.equal(cellClassList(html).filter((c) => c === "unobserved").length, 5);
});

// ── 6. the denominator is never fabricated ──────────────────────────────────

test("an unknown suite size says so and never prints 0", () => {
  const suite = suiteWith([], {
    suite: { total: null, fingerprint: null, complete: false, incomplete_reason: null, captured_at: null },
    totals: null,
    unwired: ["gate-roster"],
    unwired_reasons: { "gate-roster": "the suite size is unknowable, not zero" },
  });
  const html = renderWall(boardWith(suite));
  assert.ok(html.includes("SUITE SIZE UNKNOWN — NOT ZERO"));
  assert.ok(!/\/0\b/.test(html), "a null denominator must never render as 0");
});

test("an absent suite surface states the reason rather than rendering empty", () => {
  const html = renderWall(boardWith(null));
  assert.ok(html.includes("GATE SUITE UNAVAILABLE"));
  assert.ok(html.includes("not the same as a suite of zero gates"));
});

test("a roster with no outcomes yet explains itself", () => {
  const suite = suiteWith([], {
    totals: null,
    unwired: ["gate-outcomes"],
    unwired_reasons: { "gate-outcomes": "no attempt record carries gate_results yet" },
  });
  const html = renderWall(boardWith(suite));
  assert.ok(html.includes("No gate outcomes published yet"));
  assert.ok(html.includes("not the same as everything failing"));
  assert.ok(html.includes("gate_results"), "the control plane's reason is shown, not swallowed");
});

// ── 7. slots never reflow ───────────────────────────────────────────────────

test("SLOTS NEVER REFLOW: slot count and order survive every state change", () => {
  const frames = [
    GATES.map((g) => ({ ...g, state: "untested" })),
    GATES,
    GATES.map((g) => ({ ...g, state: "failing" })),
    GATES.map((g) => ({ ...g, state: "passing" })),
  ].map((gates) => renderWall(boardWith(suiteWith(gates))));

  const counts = frames.map((h) => cellClassList(h).filter((c) => !c.includes("sm")).length);
  assert.deepEqual(counts, [5, 5, 5, 5], "the slot count is fixed for the life of the run");

  const ids = frames.map((h) => [...h.matchAll(/title="(C:[a-e])/g)].map((m) => m[1]).join(","));
  assert.equal(new Set(ids).size, 1, `slot ORDER moved across frames: ${JSON.stringify(ids)}`);
});

test("the grid is a fixed 12 columns at every suite size", () => {
  for (const n of [1, 5, 71]) {
    const gates = Array.from({ length: n }, (_, i) => ({ ...GATES[0], id: `C:${i}` }));
    const html = renderWall(boardWith(suiteWith(gates)));
    assert.ok(html.includes("repeat(12,1fr)"), `suite size ${n} must still draw 12 columns`);
  }
});
