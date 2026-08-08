# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `0ba3be6` ("feat(plugin): per-session funnel seam counters + confirmed-on-chain read (WO-A8)", on top of `a5e6beb` D3 recall gate, `bc854d4` report firing episode, and prior A1/A2/A6/A7 work). This commit alone carries the A8 per-session funnel seam counters.
- PLUS uncommitted working-tree change (C1 export, funnel snapshot): `plugins/funnel-counters.ts` adds `serializeFunnelSnapshot()` and exports `snapshotAll()`, `plugins/wevibe-plugin.ts` adds `writeFunnelSnapshot()` + a `setInterval` (1s, `.unref()`) + `session.idle` flush writing `FUNNEL_SNAPSHOT_FILENAME`, and `plugins/funnel-counters.test.ts` adds tests. **This C1 change is uncommitted in the canonical repo by design (the orchestrator does not commit plugin work); the vendored copy carries it intentionally and it is NOT a committed-hash-only state.**
- Re-vendored 2026-08-08 via `rsync -a --delete` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to the source working tree via `diff -r --brief` (empty diff, after the excluded paths; no hand-edits inside the vendored tree). A8 + C1 feature strings confirmed in vendor: `snapshotAll`, `serializeFunnelSnapshot`, `writeFunnelSnapshot`.
- Prior vendored state (superseded by this revendor): source commit `eb79c89` (outcome event resolution/source, WO-ATTRIB-2), vendored 2026-08-07. Prior worker image: `wevibe-bench-worker:v1` = `sha256:2803131ec16ac9aebaefb7fe821aa20a3051275265d45e45e8b53eeccceee972`. That image predates A8/C1 and is STALE relative to this revendored tree.
- Worker image is NOT rebuilt by this revendor; the A3 follow-on re-establishes hash-match between this vendored tree and the worker image. Any image hash previously recorded no longer corresponds to this tree.
- Typecheck in vendored tree deferred (no vendored `node_modules`); validated during worker image rebuild.

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
