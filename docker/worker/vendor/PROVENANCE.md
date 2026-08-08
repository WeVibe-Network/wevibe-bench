# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `195c367` ("C1 funnel-snapshot export"), the canonical repo committed tip as of this revendor. This includes the A8 per-session funnel seam counters, the `outcome-spool.ts`/`outcome-episode.ts` resolution, and the C1 funnel-snapshot export (`plugins/funnel-counters.ts` `serializeFunnelSnapshot()`/`snapshotAll()`, `plugins/wevibe-plugin.ts` `writeFunnelSnapshot()` + `setInterval` + `session.idle` flush, `plugins/funnel-counters.test.ts`). The C1 funnel-snapshot export is now COMMITTED in canonical at `195c367`; the vendored copy is byte-identical to that committed tip (not an uncommitted working-tree state).
- Re-vendored 2026-08-08 via `rsync -a --delete` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to the source working tree via `diff -r --brief` (empty diff, after the excluded paths; no hand-edits inside the vendored tree). A8 + C1 feature strings confirmed in vendor: `snapshotAll`, `serializeFunnelSnapshot`, `writeFunnelSnapshot`.
- Prior vendored state (superseded by this revendor): source commit `eb79c89` (outcome event resolution/source, WO-ATTRIB-2), vendored 2026-08-07. Prior worker image: `wevibe-bench-worker:v1` = `sha256:2803131ec16ac9aebaefb7fe821aa20a3051275265d45e45e8b53eeccceee972`. That image predates A8/C1 and is STALE relative to this revendored tree.
- Current worker image: `wevibe-bench-worker:v1` = `sha256:eb96fc0cf3f50f40358ad878723bc7086cba156c3af71a2b346e81007ac1f082`, created 2026-08-08T21:57:22Z. Rebuilt 2026-08-08 (WO-TRIGGER-BUILD A11 full pass) from this vendored tree; the 3-way vendored↔canonical↔image sha256 match is re-established (wevibe-plugin.ts / funnel-counters.ts / outcome-spool.ts / outcome-episode.ts agree byte-for-byte across all three).
- Typecheck in vendored tree deferred (no vendored `node_modules`); validated during worker image rebuild.

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
