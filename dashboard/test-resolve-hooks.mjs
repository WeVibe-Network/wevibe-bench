// ─────────────────────────────────────────────────────────────────────────────
// RESOLVER HOOKS — the import map, expressed for Node
//
// MUST STAY IN SYNC WITH THE `<script type="importmap">` BLOCK IN index.html.
// That is not a comment expressing hope: `component-coverage.test.mjs` reads
// both and fails if they disagree, because a divergence would mean the tests
// and the browser load different code — the worst possible failure for a suite
// whose entire job is to tell the truth about what the board does.
// ─────────────────────────────────────────────────────────────────────────────

import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));

/** The bare specifier → vendored file mapping. Mirrors index.html's importmap. */
export const IMPORT_MAP = {
  preact: "vendor/preact.mjs",
  "preact/hooks": "vendor/preact-hooks.mjs",
  htm: "vendor/htm.mjs",
};

export async function resolve(specifier, context, nextResolve) {
  const mapped = IMPORT_MAP[specifier];
  if (mapped) {
    return { url: pathToFileURL(join(HERE, mapped)).href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
