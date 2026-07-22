# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `49ec5a8` ("feat(plugin): feed failing-build/test harvest signals into recall", on top of `0fb7575`).
- Vendored 2026-07-22 via `rsync -a` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to source commit `49ec5a8` via `diff -r` (no hand-edits inside the vendored tree).
- Commit range summary (`0fb7575..49ec5a8`): added `plugins/metrics.test.ts`; updated `plugins/metrics.ts`, `plugins/recall-harvest.test.ts`, `plugins/recall-harvest.ts`, and `plugins/wevibe-plugin.ts` to harvest build/test failing signals (`buildFailing`/`testFailing`) into recall.
- Rationale: D-FIXLOOP-RECALL failing-signal harvest for the 2026-07-22 canon conformance.
- Typecheck in vendored tree deferred (no vendored `node_modules`); validate during worker image rebuild.

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
