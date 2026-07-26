# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `9ca5107` ("feat(plugin): fire decision-note POST from deny branch of drainDecisions", on top of `1689297` need-gated recall firing).
- Vendored 2026-07-26 via `rsync -a --delete` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to the source working tree via `diff -r --brief` (empty diff; no hand-edits inside the vendored tree). Feature strings confirmed in vendor: `assessRecallNeed` (2× `plugins/wevibe-plugin.ts`, 1× `plugins/metrics.ts`), decision-note POST path (4× `plugins/wevibe-plugin.ts`), `plugins/gen-spool-v1-fixture.ts` present (72033 B `wevibe-plugin.ts` both sides).
- Commit range summary (`35c0ea7..9ca5107`, 9 files, +1234/-17): need-gated recall firing at `tool.execute.after` — failure-signal-driven `assessRecallNeed` gate with dedup via `recallInFlight` + `lastRecalledQuery`, plus recall-funnel measurement lines (`1689297`); decision-note POST fired from the deny branch of `drainDecisions` (`9ca5107`); SPOOL-V1 producer fixture generator `plugins/gen-spool-v1-fixture.ts` (`a90b82d`); sidecar typing tightened, honest gstv/log-sink logging, `@opencode-ai/sdk` declared (`1e29547`).
- Rationale: carry the need-gated firing + recall funnel measurement + deny decision-note seams into the worker image as a smoke precondition (BENCHMARK-DIARY §4A item 8 — image fingerprint must match the commit that carries the seams).
- Worker image after this revendor: `wevibe-bench-worker:v1` = `sha256:ae49efd1b4c71076ba03f8f30e0c0a9d5c92d8bdf030ee7e33d6a97047dcf976` (292,646,598 B; built 2026-07-26, also recorded in the revendor commit body). Verified post-build: `pytest tests/test_docker_isolation.py` 18/18, `docker_isolation_smoke.py` PASS, full bench `pytest` 631/631, source plugin suite 84/84.
- Typecheck in vendored tree deferred (no vendored `node_modules`); validated during worker image rebuild.

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
