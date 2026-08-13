// ─────────────────────────────────────────────────────────────────────────────
// DOM-PATCH INVARIANTS
//
// The board polls every 2s. Before this work that poll ran
// `root.innerHTML = ...`, which rebuilt every node and destroyed scroll
// position, focus, caret and text selection with it — the operator could not
// navigate the page while a run was live because every list snapped back to the
// top twice a second.
//
// ── WHAT THIS FILE CAN AND CANNOT PROVE ─────────────────────────────────────
//
// There is no DOM implementation available to this repo (no jsdom, no linkedom,
// no browser driver). Writing a hand-rolled DOM shim to test a DOM patcher
// would test the SHIM — it would pass against a stub that behaves how I imagine
// a browser behaves, which is precisely the assumption under test. That is
// worse than no test, because it reads as proof.
//
// So this file pins the STRUCTURAL invariants that are checkable from source,
// and the behavioural claim is verified on the rebuilt artifact in a real
// browser. Each test below states which of the two it is.
//
// The invariants are chosen because each one, if broken, silently restores the
// original defect — the failure mode is a board that still works and quietly
// stops being navigable.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFile(join(HERE, rel), "utf8");

/** Strip comments so a rule is never satisfied or broken by prose ABOUT it. */
function code(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

test("the board never assigns innerHTML on its render path", async () => {
  const src = code(await read("board.js"));
  assert.equal(
    /\binnerHTML\s*=/.test(src),
    false,
    "board.js assigns innerHTML — the 2s poll would rebuild every node and " +
      "destroy scroll, focus and selection. Use patch() from dom.js.",
  );
});

test("the overlay never assigns innerHTML", async () => {
  const src = code(await read("overlay.js"));
  assert.equal(
    /\binnerHTML\s*=/.test(src),
    false,
    "overlay.js assigns innerHTML — a modal mounted outside #root to survive " +
      "the board swap would then be destroyed by its own re-render instead.",
  );
});

test("both render roots go through patch()", async () => {
  for (const f of ["board.js", "overlay.js"]) {
    const src = code(await read(f));
    assert.match(src, /\bpatch\s*\(/, `${f} does not call patch()`);
    assert.match(src, /from\s+"\.\/dom\.js"|from\s+"\.\.\/dom\.js"/, `${f} does not import dom.js`);
  }
});

test("the event feed opts OUT of morphing", async () => {
  // paintFeed() owns #sc-events: it appends past a seq watermark, trims from
  // the top and compensates scroll by the exact height removed. A morph would
  // fight that and reintroduce the scroll jump the feed already solved.
  const src = await read("panels/live.js");
  assert.match(
    src,
    /id="sc-events"[^>]*data-preserve/,
    "#sc-events must carry data-preserve — paintFeed owns its children",
  );
});

test("dom.js honours the preserve opt-out BEFORE touching children", async () => {
  const src = code(await read("dom.js"));
  const fn = src.slice(src.indexOf("function patchElement"));
  const guard = fn.indexOf("PRESERVE_ATTR");
  const recurse = fn.indexOf("patchChildren");
  assert.notEqual(guard, -1, "patchElement does not check PRESERVE_ATTR");
  assert.notEqual(recurse, -1, "patchElement does not recurse");
  assert.ok(
    guard < recurse,
    "the preserve check must come BEFORE the recursion, or the subtree is " +
      "morphed before the opt-out is honoured",
  );
});

test("a focused field is never overwritten by a poll", async () => {
  // THE CARET RULE. Rewriting the value of a focused input jumps the caret to
  // the end mid-word. The server is not the authority on a field the operator
  // is still typing into.
  const src = code(await read("dom.js"));
  const fn = src.slice(src.indexOf("function patchFormState"));
  assert.match(
    fn,
    /document\.activeElement\s*===\s*oldEl[\s\S]{0,40}return/,
    "patchFormState must return early for the focused element",
  );
});

test("form state is synced as a PROPERTY, not just an attribute", async () => {
  // Setting the `value` attribute on an element the user has typed into does
  // not change what is on screen. A patcher that only synced attributes would
  // look correct and silently fail to update any control.
  const src = code(await read("dom.js"));
  assert.match(src, /oldEl\.value\s*=/, "dom.js never assigns .value");
  assert.match(src, /oldEl\.checked\s*=/, "dom.js never assigns .checked");
});

test("text nodes are compared before assignment", async () => {
  // An unconditional nodeValue write collapses a live text selection, so the
  // operator loses a cid or error string they were mid-copy.
  const src = code(await read("dom.js"));
  assert.match(
    src,
    /oldNode\.nodeValue\s*!==\s*newNode\.nodeValue[\s\S]{0,80}nodeValue\s*=/,
    "text nodes must only be written when they actually differ",
  );
});

test("nodes of a different tag are replaced, not morphed into each other", async () => {
  const src = code(await read("dom.js"));
  assert.match(
    src,
    /nodeName\s*!==\s*newNode\.nodeName[\s\S]{0,160}replaceChild/,
    "a tag change must replace the node — morphing a <span> into a <div> " +
      "would carry stale state across two unrelated elements",
  );
});

test("the parse happens detached from the live document", async () => {
  // Parsing into the live tree would attach a half-built board and cause a
  // visible flash plus layout work on intermediate states.
  const src = code(await read("dom.js"));
  assert.match(src, /createElement\("template"\)/, "dom.js must parse into a <template>");
});

test("every root-level module STATIC serves is COPYd into the image", async () => {
  // THIS CAUGHT A REAL 404. `panels/` and `sources/` are COPYd wholesale, but
  // root-level modules are an EXPLICIT file list in the Dockerfile. Adding
  // dom.js to STATIC and to the imports was not enough — the file was absent
  // from the image, so /dom.js returned 404 and, because board.js imports it,
  // the entire board rendered blank. Compile-green and a passing test suite
  // both looked fine; only the served artifact showed it.
  const dockerfile = await read("Dockerfile");
  const server = await read("server.mjs");

  const copyLine = dockerfile
    .split("\n")
    .find((l) => l.startsWith("COPY") && l.includes("./") && !l.includes("/ ."));
  assert.ok(copyLine, "no root-level COPY line found in the Dockerfile");

  const missing = [];
  for (const m of server.matchAll(/"\/([\w.-]+\.(?:mjs|js))"/g)) {
    const file = m[1];
    if (!copyLine.includes(file)) missing.push(file);
  }
  assert.deepEqual(
    missing,
    [],
    `served by STATIC but never COPYd into the image — these 404 at runtime ` +
      `and blank the board: ${missing.join(", ")}`,
  );
});

// ── CONTROL REACHABILITY ────────────────────────────────────────────────────
// The control plane binds 127.0.0.1 with no --host flag, as a stated safety
// property (control/server.mjs:23-25) — it spawns processes, so it is never
// published on a network. The board may be viewed from the LAN. Those two facts
// together mean a browser can legitimately be unable to reach the control
// plane, and the board must SAY so rather than render controls that fail.

test("isLoopback classifies the addresses that actually occur", async () => {
  const { isLoopback } = await import("./sources/control-plane.mjs");

  for (const u of [
    "http://127.0.0.1:7718",
    "http://localhost:7718",
    "http://127.1.2.3:7718", // all of 127/8 is loopback
    "http://[::1]:7718",
  ]) {
    assert.equal(isLoopback(u), true, `${u} should be loopback`);
  }

  for (const u of [
    "http://192.168.50.14:7718",
    "http://10.0.0.5:7718",
    "http://host.docker.internal:7718",
    "http://bench.local:7718",
  ]) {
    assert.equal(isLoopback(u), false, `${u} should NOT be loopback`);
  }

  // A substring test would call this loopback because it contains "127.0.0.1".
  assert.equal(isLoopback("http://127.0.0.1.evil.example:7718"), false);
  assert.equal(isLoopback("not a url"), false);
});

test("the control-plane source publishes the reachability flag", async () => {
  const src = await read("sources/control-plane.mjs");
  assert.match(
    src,
    /base_url_is_loopback:\s*isLoopback\(publicBase\)/,
    "control.base_url_is_loopback must be derived from the published base url",
  );
});

test("every control write path is gated on reachability", async () => {
  // A path that reads base_url directly bypasses the gate and reintroduces the
  // silent failure: the operator clicks, the fetch dies, nothing is said.
  const src = code(await read("board.js"));
  const paths = ["releaseHold", "detachTui", "freezeProfile", "doArmRun", "doStartRun"];
  for (const name of paths) {
    const start = src.indexOf(`function ${name}`);
    assert.notEqual(start, -1, `${name} not found in board.js`);
    const body = src.slice(start, start + 700);
    assert.match(
      body,
      /controlReachability\(board\)/,
      `${name} does not check controlReachability — it would fail silently ` +
        `when the board is opened from a LAN address`,
    );
  }
});

test("no control path fails silently", async () => {
  // The original defect in one line: `if (!base) return;`. A bare return with
  // no reason is indistinguishable from a dead button.
  const src = code(await read("board.js"));
  assert.equal(
    /if\s*\(!base\)\s*return;/.test(src),
    false,
    "a control path returns without stating a reason",
  );
});

test("the topbar retracts its WRITES claim when writes cannot land", async () => {
  const src = await read("panels/chrome.js");
  assert.match(src, /controlReachability/, "chrome.js does not consult reachability");
  assert.match(
    src,
    /CONTROLS UNAVAILABLE HERE/,
    "the topbar must retract 'WRITES → CONTROL PLANE' when it is false",
  );
});
