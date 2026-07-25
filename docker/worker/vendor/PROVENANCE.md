# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `35c0ea7` ("feat(plugin): M0 passive GSTV sensor spool + dormant goal hooks (CO-GSTV-MEASURE WO-1)", on top of `854e45a`).
- Vendored 2026-07-25 via `rsync -a --delete` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to the source working tree via `diff -r` (no hand-edits inside the vendored tree).
- Commit range summary (`854e45a..35c0ea7`, 6 files, +1521/-21): added SPOOL-V1 passive event spool (`plugins/gstv-spool.ts` + tests) — fire-and-forget JSONL at `<scoped state dir>/spool/spool-v1.jsonl` with pinned schema (spec: `wevibe-meta/workspace/docs/SPOOL-V1.md`); added dormant GSTV goal hooks (`plugins/gstv-client.ts`, `plugins/gstv-hooks.ts`) — `GET /v1/gstv/goal` with 404=honest-absence, `gstv.attach.attempt` on session.created, sealed predicate run once post-session.idle via plugin `$` shell (D-GSTV-BOUNDARY-STAMP) into `gstv.boundary.run`; wired both plus a `tool.execute.before` sensor into `plugins/wevibe-plugin.ts`; extended `[inject]` lines with `cadence=once` + `block_tokens` + `top_k` (DECISIONS §23) and added `[inject] restored … compaction_restores=n`; added `[serve] receipt failed` logging (R-37; D-C counted, not resolved); added per-memory matched-keyword terms (`kw=[...]`) to the per-memory `[inject]` line; purged dead `statusDirty` (P-1). Tests 48 -> 67 (additive only; tsc --noEmit clean at source).
- Rationale: M0 passive GSTV sensor surface (CO-GSTV-MEASURE WO-1); zero behavior delta while no open goal exists; re-vendor as R2 precondition (M0 sensors live in the worker image).
- Typecheck in vendored tree deferred (no vendored `node_modules`); validate during worker image rebuild.

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
