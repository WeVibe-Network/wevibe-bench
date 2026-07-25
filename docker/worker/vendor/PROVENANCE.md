# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `854e45a` ("docs(plugin): document inject_char_budget configuration", on top of `dd8a52d`).
- Vendored 2026-07-25 via `rsync -a --delete` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to the source working tree via `diff -r` (no hand-edits inside the vendored tree).
- Commit range summary (`49ec5a8..854e45a`): rewrote `plugins/wevibe-plugin.ts` (+242/-26) for inject-once memory cadence with a bounded served set and verbatim compaction restore (DECISIONS §23); added `plugins/wevibe-plugin.test.ts` (new, +310); added a `## Configuration` section to `README.md` documenting `inject_char_budget` (default 8000).
- Rationale: inject-once memory cadence (DECISIONS §23); re-vendor the bench worker image's plugin copy as an R2 precondition.
- Typecheck in vendored tree deferred (no vendored `node_modules`); validate during worker image rebuild.

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
