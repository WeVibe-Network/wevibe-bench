# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `f9f677d` ("feat(outcome): add use-leg E3 harvester — episode tracker + resumable outcome spool wired to mcp outcome-events", on top of `43f6037` accept-drain seam guard and `9ca5107` deny decision-note POST).
- Vendored 2026-07-30 via `rsync -a --delete` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to the source working tree via `diff -r --brief` (empty diff; no hand-edits inside the vendored tree). Feature strings confirmed in vendor: `outcome-events`, `createOutcomeSpool`, `EpisodeTracker`, `episodeTracker`, `outcomeSpool`, and `[outcome] harvested` in `plugins/wevibe-plugin.ts`; outcome spool and episode modules/tests present under `plugins/`.
- Commit range summary (`9ca5107..f9f677d`, 7 files, +1538/-6): accept-drain production inject seam guarded by test (`43f6037`); use-leg E3 outcome harvester wired to MCP `/outcome-events`, with episode tracker, resumable outcome spool, and wiring/spool/episode tests (`f9f677d`). Diff stat: `plugins/outcome-episode.test.ts` +262, `plugins/outcome-episode.ts` +254, `plugins/outcome-spool.test.ts` +293, `plugins/outcome-spool.ts` +354, `plugins/outcome-wiring.test.ts` +256, `plugins/wevibe-plugin.test.ts` +38/-1, `plugins/wevibe-plugin.ts` +87/-5.
- Rationale: carry the E3 outcome harvester into the worker image; without it, benchmark use legs emit zero outcome events and the retirement mechanism has no worked/didn't-work labels to test.
- SUPERSEDED-pending-rebuild worker image from prior revendor: `wevibe-bench-worker:v1` = `sha256:ae49efd1b4c71076ba03f8f30e0c0a9d5c92d8bdf030ee7e33d6a97047dcf976` (292,646,598 B; built 2026-07-26, also recorded in the revendor commit body). Verified post-build: `pytest tests/test_docker_isolation.py` 18/18, `docker_isolation_smoke.py` PASS, full bench `pytest` 631/631, source plugin suite 84/84.
- Worker image after this revendor: `wevibe-bench-worker:v1` = `sha256:8e70de01bb0a48dd625be33332c0727bc4f3e5a07f02f0d76ecd6f443faa73b8` (created 2026-07-31T01:25:11Z). Freshness proven by hashing the plugin inside the built image (`ce994cbb85064c82e2d8990ae13bf4f4c84ec78f7da8e6c6a54afd20b772007f`), byte-identical to canonical `wevibe-opencode-plugin/plugins/wevibe-plugin.ts` at tip `f9f677d`.
- Typecheck in vendored tree deferred (no vendored `node_modules`); validated during worker image rebuild.

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
