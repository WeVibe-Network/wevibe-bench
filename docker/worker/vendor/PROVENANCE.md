# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `eb79c89` ("feat(plugin): outcome event resolution/source + drop fabricated negative (WO-ATTRIB-2)", on top of `f9f677d` use-leg E3 harvester, `43f6037` accept-drain seam guard, and `9ca5107` deny decision-note POST).
- Vendored 2026-08-07 via `rsync -a --delete` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to the source working tree via `diff -r --brief` (empty diff; no hand-edits inside the vendored tree). Feature strings confirmed in vendor: `resolution`, `source`, and `unobserved`.
- Commit range summary (`f9f677d..eb79c89`, 6 files, +44/-33): outcome event resolution/source replaces the fabricated negative `worked` boolean; episode expiry emits `unobserved` (no fabricated negative worked); nonce preimage now uses `resolution=`; wevibe-plugin.ts logs `worked=${resolved}` label derived from `resolution === "worked"`. Diff stat: `plugins/outcome-episode.test.ts` +14/-13, `plugins/outcome-episode.ts` +13/-7, `plugins/outcome-spool.test.ts` +4/-3, `plugins/outcome-spool.ts` +9/-6, `plugins/outcome-wiring.test.ts` +2/-2, `plugins/wevibe-plugin.ts` +2/-2.
- Rationale: carry the E3 outcome harvester into the worker image; without it, benchmark use legs emit zero outcome events and the retirement mechanism has no worked/didn't-work labels to test.
- SUPERSEDED-pending-rebuild worker image from prior revendor: `wevibe-bench-worker:v1` = `sha256:ae49efd1b4c71076ba03f8f30e0c0a9d5c92d8bdf030ee7e33d6a97047dcf976` (292,646,598 B; built 2026-07-26, also recorded in the revendor commit body). Verified post-build: `pytest tests/test_docker_isolation.py` 18/18, `docker_isolation_smoke.py` PASS, full bench `pytest` 631/631, source plugin suite 84/84.
- Worker image after this revendor: `wevibe-bench-worker:v1` = `sha256:2803131ec16ac9aebaefb7fe821aa20a3051275265d45e45e8b53eeccceee972` (created 2026-08-08T05:45:56.354876418Z). Freshness proven by hashing the plugin inside the built image (`1eb0b376e7012bb56ef6b55ce72195fd0341ec832d944ed69ab303c866f488a4`), byte-identical to canonical `wevibe-opencode-plugin/plugins/wevibe-plugin.ts` at tip `eb79c89`.
- Typecheck in vendored tree deferred (no vendored `node_modules`); validated during worker image rebuild.

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
