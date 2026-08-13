// ─────────────────────────────────────────────────────────────────────────────
// TEST RESOLVER — teach Node the browser's import map
//
//     node --import ./test-resolve.mjs --test
//
// ── WHY THIS EXISTS ─────────────────────────────────────────────────────────
//
// The board resolves `preact`, `preact/hooks` and `htm` through an IMPORT MAP
// in index.html, which is a browser feature. Node does not read import maps, so
// every component module would be unloadable under `node --test` — and a panel
// that cannot be tested is a panel whose rules are unpinned.
//
// The alternatives were both worse:
//
//   · Rewrite the vendored libraries' import specifiers to relative paths. That
//     forks a dependency: the edit has to be redone by hand on every upgrade,
//     and a missed one fails silently at runtime in the browser.
//   · Import the vendored files by relative path from our own modules. That
//     spreads `../vendor/preact.mjs` through every panel and hardcodes the
//     layout, so moving a file breaks imports across the tree.
//
// A resolver hook keeps ONE mapping, declared once, matching the import map
// exactly. The specifiers in application code stay bare and identical in both
// environments — which is the property that makes the browser and the test
// runner agree about what they are loading.
// ─────────────────────────────────────────────────────────────────────────────

import { register } from "node:module";
import { pathToFileURL } from "node:url";

register("./test-resolve-hooks.mjs", pathToFileURL("./"));
