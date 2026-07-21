# Vendored plugin provenance

`wevibe-opencode-plugin/` is a byte-for-byte copy of the canonical repo
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-opencode-plugin` (github.com/WeVibe-Network/wevibe-opencode-plugin):

- Source commit: `0fb7575` ("fix(plugin): ship full plugins/ dir in package files field", on top of `d62b4ff2e05866d5ef0fc2b2d11cf6b93dc06523`). The `files`-field fix (`"plugins/wevibe-plugin.ts"` → `"plugins"`) repairs the pack tarball, which was missing the plugin's runtime sibling modules (`metrics.ts`, `recall-harvest.ts`, `binding.ts`, `wevibe-paths.ts`, `org-join-gate.ts`), so `npm i -g <dir>` — the worker Dockerfile's install mechanism — produced a broken install.
- Vendored 2026-07-21 via `rsync -a` excluding `.git`, `.github`, `node_modules`, `.DS_Store` (same exclusions as `docker/worker/.dockerignore`). Verified byte-identical to source commit `0fb7575` via `diff -r` (no hand-edits inside the vendored tree).
- Typecheck green upstream (`npm run typecheck` = `tsc --noEmit`).

Update this file (source commit + diff summary) whenever the vendored copy is refreshed.
