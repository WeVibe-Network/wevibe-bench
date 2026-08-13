// ─────────────────────────────────────────────────────────────────────────────
// STYLE COVERAGE — every class the board emits must have a definition
//
// Zero dependencies. Stock `node --test`, no install, no build step:
//
//     cd wevibe-bench/dashboard && node --test
//
// WHY THIS EXISTS
//
// The board is assembled by nine panel modules that emit `class="..."` strings,
// and a SINGLE stylesheet inside index.html that defines those classes. Nothing
// mechanically connected the two. A rewrite of index.html's token block
// therefore dropped 78 class definitions and 8 custom properties while every
// panel kept emitting them — and the result passed every check the project had:
//
//   · the modules still parsed and imported cleanly (no runtime error)
//   · `node --test` stayed green (no test asserted on styling at all)
//   · redeploy.sh's byte-comparison passed (served bytes DID match disk)
//
// The board rendered with its GATE WALL — one of the two axes the whole
// benchmark exists to present — as a row of invisible, unstyled spans. An
// entire axis of the argument was missing from the screen and every automated
// signal was green.
//
// A missing CSS rule is silent by construction: the browser drops the unknown
// class and renders the element with inherited styles. There is no console
// error to notice. That silence is exactly why this has to be a test.
//
// WHAT IT PINS
//
//   1. Every class emitted by a LIVE panel resolves to a rule in the stylesheet.
//   2. Every `var(--x)` a LIVE panel reads resolves to a declared property.
//   3. The panels the board actually imports are the panels registered in the
//      server's STATIC allowlist — an unregistered panel 404s, and a 404 on an
//      ES module import blanks the entire board.
//
// SCOPE — "LIVE" means reachable from board.js. A file sitting in panels/ that
// nothing imports is NOT covered here, deliberately: dead files must not be
// able to hold the stylesheet hostage. LIVE_PANELS is derived by following the
// real import graph from board.js, so a panel added to the board is covered
// automatically and cannot be forgotten.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

const read = (rel) => readFile(join(HERE, rel), "utf8");

/**
 * Follow the real import graph from board.js.
 *
 * Derived, never hand-listed: a hand-maintained list is a second source of
 * truth that drifts the moment someone adds a panel, which is the exact class
 * of defect this file exists to catch.
 */
async function livePanels() {
  const seen = new Set();
  const queue = ["board.js"];

  while (queue.length) {
    const rel = queue.shift();
    if (seen.has(rel)) continue;
    seen.add(rel);

    let src;
    try {
      src = await read(rel);
    } catch {
      continue; // resolved below by the STATIC test; not this test's job
    }

    for (const m of src.matchAll(/from\s+"(\.[^"]+)"/g)) {
      const target = resolve(dirname(join(HERE, rel)), m[1]);
      const asRel = target.slice(HERE.length + 1);
      if (asRel.endsWith(".js") && !seen.has(asRel)) queue.push(asRel);
    }
  }
  return [...seen];
}

/** The stylesheet is inline in index.html — every <style> block, concatenated. */
async function stylesheet() {
  const html = await read("index.html");
  const blocks = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]);
  assert.ok(blocks.length > 0, "index.html carries no <style> block");
  return blocks.join("\n");
}

/**
 * Blank every `${...}` template hole in a source file, honouring nested braces
 * and quotes.
 *
 * This runs BEFORE the class attribute is matched, and that order is
 * load-bearing. A hole can itself contain double quotes —
 * `class="${metric === id ? "on" : ""}"` in curve.js is real — so matching the
 * attribute first truncates the capture at the INNER quote and leaks fragments
 * of the expression (`id`, `metric`) as if they were class names. The first
 * version of this file did exactly that and reported a phantom unstyled class.
 * A test that cries wolf gets weakened by the next person to see it fail.
 */
function blankTemplateHoles(src) {
  let out = "";
  for (let i = 0; i < src.length; i += 1) {
    if (src[i] === "$" && src[i + 1] === "{") {
      let depth = 1;
      let j = i + 2;
      let quote = null;
      for (; j < src.length && depth > 0; j += 1) {
        const c = src[j];
        if (quote) {
          if (c === "\\") j += 1;
          else if (c === quote) quote = null;
        } else if (c === '"' || c === "'" || c === "`") quote = c;
        else if (c === "{") depth += 1;
        else if (c === "}") depth -= 1;
      }
      out += " ";
      i = j - 1;
      continue;
    }
    out += src[i];
  }
  return out;
}

/**
 * Class tokens in an emitted `class="..."` attribute.
 *
 * Conditional class names living inside a template hole are covered by their
 * own literal occurrences elsewhere (e.g. `class="ph ${state}"` yields `ph`,
 * while `running`/`done`/`pending` appear as literals in the same file). This
 * never invents a name the panel does not literally contain.
 */
function emittedClasses(src) {
  const out = new Set();
  for (const m of blankTemplateHoles(src).matchAll(/class="([^"]*)"/g)) {
    for (const tok of m[1].split(/\s+/)) {
      if (/^[a-zA-Z][\w-]*$/.test(tok)) out.add(tok);
    }
  }
  return out;
}

function definedClasses(css) {
  return new Set([...css.matchAll(/\.([a-zA-Z][\w-]*)/g)].map((m) => m[1]));
}

function readVars(src) {
  return new Set([...src.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1]));
}

function declaredVars(css) {
  return new Set([...css.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
}

// ── the guards ───────────────────────────────────────────────────────────────

test("every class a live panel emits has a CSS definition", async () => {
  const css = await stylesheet();
  const defined = definedClasses(css);

  /** @type {Record<string,string[]>} */
  const missing = {};
  for (const rel of await livePanels()) {
    const gaps = [...emittedClasses(await read(rel))].filter((c) => !defined.has(c)).sort();
    if (gaps.length) missing[rel] = gaps;
  }

  assert.deepEqual(
    missing,
    {},
    `unstyled classes reach the DOM — a missing rule renders SILENTLY, with no ` +
      `console error, so the panel looks structurally present while carrying no ` +
      `geometry at all:\n${JSON.stringify(missing, null, 2)}`,
  );
});

test("every CSS variable a live panel reads is declared", async () => {
  const css = await stylesheet();
  const declared = declaredVars(css);

  /** @type {Record<string,string[]>} */
  const missing = {};
  for (const rel of await livePanels()) {
    const gaps = [...readVars(await read(rel))].filter((v) => !declared.has(v)).sort();
    if (gaps.length) missing[rel] = gaps;
  }

  assert.deepEqual(
    missing,
    {},
    `panels read custom properties that no rule declares. An undeclared var() ` +
      `with no fallback resolves to nothing and the declaration is DROPPED — ` +
      `silently, exactly like a missing class:\n${JSON.stringify(missing, null, 2)}`,
  );
});

test("the Gate Wall's cell states are all styled", async () => {
  // Named explicitly because this is the failure that shipped. The wall is one
  // of the two axes the board exists to present; wall.js sets the grid's column
  // count inline but relies on the stylesheet for the cells themselves, so an
  // unstyled .gcell renders the whole correctness axis as empty spans.
  const css = await stylesheet();
  const defined = definedClasses(css);
  for (const c of ["gwall", "gcell", "green", "red", "unobserved"]) {
    assert.ok(defined.has(c), `.${c} is unstyled — the gate wall renders invisible`);
  }

  // A BASE rule, not merely the name appearing somewhere.
  //
  // Injected-drift finding: deleting `.gcell{...}` while leaving `.gcell.green`
  // in place left the NAME resolvable, so the coverage test above passed while
  // the cells lost every metric that gives them a visible box — aspect-ratio,
  // border, radius. The name test is necessary but not sufficient; the geometry
  // has to be asserted where it actually lives.
  const base = css.match(/(?:^|\n)\.gcell\{([^}]*)\}/);
  assert.ok(base, ".gcell has no base rule — modifiers alone give the cell no box");
  for (const prop of ["aspect-ratio", "border", "background"]) {
    assert.match(
      base[1],
      new RegExp(prop),
      `.gcell base rule lacks ${prop} — without it the cell has no visible extent`,
    );
  }

  // The three states must be visually DISTINCT, not merely present: the board
  // has to survive a greyscale screenshot, so they cannot differ by hue alone.
  for (const rule of [/\.gcell\.red\{[^}]*\}/, /\.gcell\.unobserved\{[^}]*\}/]) {
    const m = css.match(rule);
    assert.ok(m, `missing a distinct rule for ${rule}`);
    assert.ok(
      /background|border/.test(m[0]),
      `${m[0]} distinguishes by colour alone — it must also differ in fill or border`,
    );
  }
});

test("the honesty rail is a multi-column grid, not a stack", async () => {
  // The rail inherits `flex-direction:column` from the shared panel rule. When
  // its own grid rule went missing it silently became six full-width blocks
  // running down the page instead of the artifact's six-across strip — a
  // layout regression with no error anywhere.
  const css = await stylesheet();
  const rule = css.match(/(?:^|\n)\.rail\{([^}]*)\}/);
  assert.ok(rule, ".rail has no own rule — it inherits the panel's column flex");
  assert.match(
    rule[1],
    /display:grid/,
    ".rail must be a grid; as a flex column it stacks six boxes down the page",
  );
  assert.match(rule[1], /grid-template-columns:repeat\(6/, ".rail must be six across at full width");
});

test("the feed row height is constant and stated", async () => {
  // live.js does sticky-bottom arithmetic against BOTTOM_EPS=24 and assumes
  // rows never change height. A row that grows breaks autoscroll silently, so
  // the fixed height is a contract between the CSS and the scroll logic.
  const css = await stylesheet();
  const rule = css.match(/(?:^|\n)\.evrow\{([^}]*)\}/);
  assert.ok(rule, ".evrow has no rule — the feed loses its constant row height");
  // Anchored so `min-height`/`max-height` cannot satisfy it. Injected-drift
  // finding: a bare /height:34px/ matched `min-height:34px`, which is exactly
  // the change that makes a row GROW with its content and breaks autoscroll.
  assert.match(
    rule[1],
    /(^|;)\s*height:34px/,
    ".evrow must set a FIXED height:34px — min-height lets a row grow and breaks sticky-bottom",
  );

  const live = await read("panels/live.js");
  const eps = live.match(/BOTTOM_EPS\s*=\s*(\d+)/);
  assert.ok(eps, "live.js no longer declares BOTTOM_EPS");
  assert.ok(
    Number(eps[1]) < 34,
    `BOTTOM_EPS (${eps[1]}) must stay below the 34px row height or sticky-bottom can never settle`,
  );
});

test("the one animation touches only background and inset shadow", async () => {
  // The board is on screen for hours; the entire motion budget is one settling
  // flash on a new row. Animating height/margin/transform would reflow the feed
  // mid-scroll and fight the sticky-bottom logic.
  const css = await stylesheet();
  const kf = css.match(/@keyframes evflash\{([\s\S]*?)\}\s*\n/);
  assert.ok(kf, "the event-row flash keyframes are gone");
  for (const banned of ["height", "margin", "transform", "padding"]) {
    assert.ok(
      !new RegExp(`(^|[;{\\s])${banned}\\s*:`).test(kf[1]),
      `evflash animates ${banned} — that reflows the feed and breaks sticky-bottom`,
    );
  }
  assert.match(css, /prefers-reduced-motion[\s\S]{0,220}\.evrow\.fresh\{animation:none/, "reduced-motion must drop the flash to a static rule");
});

test("every live panel is registered in the server's STATIC allowlist", async () => {
  // A panel that board.js imports but the server does not serve returns 404,
  // and a 404 on an ES module import blanks the ENTIRE board — this has already
  // caused one black-screen incident.
  const server = await read("server.mjs");
  const missing = [];
  for (const rel of await livePanels()) {
    if (!server.includes(`"/${rel}"`)) missing.push(rel);
  }
  assert.deepEqual(
    missing,
    [],
    `imported but not served — these 404 and blank the whole board: ${missing.join(", ")}`,
  );
});

test("STATIC does not advertise panels that no longer exist", async () => {
  // The mirror of the test above. A stale entry is a promise the image cannot
  // keep: the Dockerfile COPYs panels/ wholesale, so a deleted panel still
  // listed in STATIC is a route that 404s at runtime.
  const server = await read("server.mjs");
  const dead = [];
  for (const m of server.matchAll(/"\/(panels\/[\w.-]+\.js)"/g)) {
    try {
      await read(m[1]);
    } catch {
      dead.push(m[1]);
    }
  }
  assert.deepEqual(dead, [], `STATIC lists files that do not exist: ${dead.join(", ")}`);
});

test("no panel is served, styled and tested but reachable from nothing", async () => {
  // ── THE THIRD DIRECTION ────────────────────────────────────────────────
  //
  // The two tests above check served-but-missing and imported-but-unserved.
  // The third case — a file that EXISTS, is SERVED, and is imported by nothing
  // — was not asserted, and it is the one that is invisible to every other
  // check in the tree: the panel still parses, its tests still pass, its
  // stylesheet rules are still there, and it never executes in a browser.
  //
  // index.html loads exactly ONE module (`<script type="module"
  // src="./board.js">`). If a panel is not in board.js's transitive import
  // graph it cannot run, however healthy it looks.
  //
  // TRANSITIVE, NOT DIRECT. `livePanels()` already walks the whole graph, and
  // that distinction is load-bearing: an audit that checked only board.js's own
  // import list concluded `panels/startup.js` was dead code and nearly deleted
  // it. It is reached through `panels/tui.js`, which board.js imports. A
  // one-level check answers a different question than the one being asked.
  //
  // Scope note: `style-coverage`'s own rule that "a file sitting in panels/ that
  // nothing imports is NOT covered here" is about the STYLESHEET — dead files
  // must not hold rules hostage. Reachability is a different question and this
  // is where it is answered.
  const live = new Set(await livePanels());
  const orphans = [];
  for (const f of await readdir(join(HERE, "panels"))) {
    if (!f.endsWith(".js")) continue;
    if (!live.has(`panels/${f}`)) orphans.push(`panels/${f}`);
  }
  assert.deepEqual(
    orphans,
    [],
    "unreachable from board.js — built, served, and it can never render: "
      + `${orphans.join(", ")}. Import it or delete it.`,
  );
});
