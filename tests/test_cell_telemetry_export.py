"""Cell-end telemetry export into the `data/` sink.

The plugin's observable recall surface lives INSIDE the cell worktree under
`.wevibe/` and is destroyed at `docker rm`. `_export_cell_telemetry` copies it
host-side into `data/cells/<unix_ts>-<run_label>/` before teardown.

Contract under test:
  - both artifacts are copied when present, under a `<unix_ts>-<run_label>` dir
  - a partial surface (one artifact) still exports
  - an absent surface is a no-op returning None (NOT an error)
  - export is FAIL-OPEN: an unwritable destination returns None, never raises
  - it writes only under `data/`, never `runs/` (RC-5 stays authoritative)
"""

from __future__ import annotations

import json
from pathlib import Path

from wevibe_bench.adapters.backgammon import _export_cell_telemetry

_SNAPSHOT = {"ses_abc": {"recall_fired": 2, "gate_decision_ms": 41}}
_LOG_LINE = "recall_fired trigger=repeat_failure sid=ses_abc\n"


def _seed_surface(
    worktree: Path,
    *,
    snapshot: bool = True,
    plugin_log: bool = True,
) -> None:
    if snapshot:
        state = worktree / ".wevibe" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "funnel-snapshot.json").write_text(json.dumps(_SNAPSHOT), encoding="utf-8")
    if plugin_log:
        logs = worktree / ".wevibe" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "wevibe-plugin-errors.log").write_text(_LOG_LINE, encoding="utf-8")


def test_export_copies_both_artifacts_into_timestamped_cell_dir(
    tmp_path: Path, monkeypatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _seed_surface(worktree)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("WEVIBE_BENCH_DATA_DIR", str(data_dir))

    dest = _export_cell_telemetry(worktree, "cumulative-0000-off-local")

    assert dest is not None
    assert dest.parent == data_dir / "cells"
    assert dest.name.endswith("-cumulative-0000-off-local")
    # <unix_ts>-<run_label>: the timestamp prefix must be a real epoch int.
    assert dest.name.split("-", 1)[0].isdigit()
    assert json.loads((dest / "funnel-snapshot.json").read_text(encoding="utf-8")) == _SNAPSHOT
    assert (dest / "plugin-errors.log").read_text(encoding="utf-8") == _LOG_LINE


def test_export_survives_partial_surface(tmp_path: Path, monkeypatch) -> None:
    """A cell that produced only a plugin log still exports what exists."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _seed_surface(worktree, snapshot=False)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("WEVIBE_BENCH_DATA_DIR", str(data_dir))

    dest = _export_cell_telemetry(worktree, "run-partial")

    assert dest is not None
    assert (dest / "plugin-errors.log").is_file()
    assert not (dest / "funnel-snapshot.json").exists()


def test_export_is_noop_when_no_surface_exists(tmp_path: Path, monkeypatch) -> None:
    """An OFF cell with no plugin substrate is a silent no-op, not a failure."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("WEVIBE_BENCH_DATA_DIR", str(data_dir))

    assert _export_cell_telemetry(worktree, "run-empty") is None
    assert not (data_dir / "cells").exists()


def test_export_is_fail_open_when_destination_unwritable(
    tmp_path: Path, monkeypatch
) -> None:
    """Telemetry export must NEVER fail a scored cell."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _seed_surface(worktree)
    # Point the sink at a path blocked by an existing FILE, so mkdir raises.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("WEVIBE_BENCH_DATA_DIR", str(blocker))

    assert _export_cell_telemetry(worktree, "run-blocked") is None


def test_export_never_writes_into_runs(tmp_path: Path, monkeypatch) -> None:
    """RC-5: `runs/` stays authoritative; `data/` is a retention layer only."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _seed_surface(worktree)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("WEVIBE_BENCH_DATA_DIR", str(data_dir))

    dest = _export_cell_telemetry(worktree, "run-isolation")

    assert dest is not None
    assert list(runs_dir.iterdir()) == []
    assert data_dir in dest.parents
