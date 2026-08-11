# wevibe-bench telemetry sink (`data/`)

This directory is the host-side, retained home for run telemetry produced by the
WeVibe plugin during a bench campaign.

## Purpose
Today the plugin writes its funnel counters and error log container-side under
`~/.wevibe` and they are destroyed at `docker-rm`; OFF-arm cells never write
host-side state at all. `data/` gives the campaign a durable, timestamped,
auto-cleaned home for per-cell observable recall surface (funnel snapshot +
plugin log) so it survives teardown and disk stays bounded.

## Layout
- `data/cells/` — one subdirectory per cell, named `<unix_ts>-<run_label>/`,
  holding that cell's exported `funnel-snapshot.json` and `plugin-errors.log`.
- `data/extract/` — extraction-stage telemetry artifacts.

## How data propagates
At cell end the harness copies the worktree's `.wevibe/state/funnel-snapshot.json`
and `.wevibe/logs/wevibe-plugin-errors.log` into
`data/cells/<unix_ts>-<run_label>/` (`_export_cell_telemetry` in
`wevibe_bench/adapters/backgammon.py`), before the container is torn down. It runs
for BOTH arms — OFF is the baseline ON is compared against. Fail-open: a missing
surface is a no-op, an unwritable sink is logged and swallowed; export never fails
a cell.

## Retention
Entries directly under `data/cells/` and `data/extract/` older than **7 days**
are deleted by `scripts/cleanup_data.py`, which is wired fail-open into the run
entrypoint (`scripts/run_cumulative.py::_handle_run`). Retention runs at the
start of each run and can be skipped with `WEVIBE_BENCH_SKIP_CLEANUP=1`.

## Source of truth
`data/` is a TELEMETRY/RETENTION layer only — NEVER a competing source of truth.
`runs/` remains authoritative for the run manifest and status stream (RC-5).
This directory never touches or duplicates `runs/` content.