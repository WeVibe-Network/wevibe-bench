from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_entry(root: Path, name: str, *, is_dir: bool, mtime: float) -> Path:
    path = root / name
    if is_dir:
        path.mkdir()
        (path / "state.json").write_text("{}")
    else:
        path.write_text("log")
    os.utime(path, (mtime, mtime))
    return path


def test_prune_keeps_latest_plus_one_and_live_state(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    live = _make_entry(tmp_path, "cumulative", is_dir=True, mtime=1)
    unrelated_file = _make_entry(tmp_path, "contributor-mcp.log", is_dir=False, mtime=2)
    unrelated_dir = _make_entry(tmp_path, "probe", is_dir=True, mtime=3)
    logs = [
        _make_entry(tmp_path, f"off-cell-2026081{i}T000000.log", is_dir=False, mtime=10 + i)
        for i in range(4)
    ]
    archives = [
        _make_entry(tmp_path, f"cumulative.pre-x-2026081{i}", is_dir=True, mtime=20 + i)
        for i in range(3)
    ]

    summary = module._prune_runs_retention(tmp_path)

    assert live.exists()
    assert unrelated_file.exists()
    assert unrelated_dir.exists()
    # newest 2 logs kept (mtimes 13, 12), older two deleted
    assert logs[3].exists() and logs[2].exists()
    assert not logs[1].exists() and not logs[0].exists()
    # Archived run directories carry session DB extraction substrate. Retention
    # may prune top-level logs, but must never remove these directories.
    assert archives[2].exists() and archives[1].exists() and archives[0].exists()
    assert sorted(summary["deleted"]) == sorted(
        ["off-cell-20260810T000000.log", "off-cell-20260811T000000.log"]
    )


def test_prune_never_deletes_session_db_archives(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    old = _make_entry(tmp_path, "cumulative.failed-20260810", is_dir=True, mtime=1)
    db = old / "sessions" / "cell-0" / "session-db"
    db.mkdir(parents=True)
    (db / "opencode.db").write_text("sqlite bytes")
    for i in range(4):
        _make_entry(tmp_path, f"on-cell-2026081{i}T000000.log", is_dir=False, mtime=10 + i)

    summary = module._prune_runs_retention(tmp_path, keep=1)

    assert old.exists()
    assert (db / "opencode.db").read_text() == "sqlite bytes"
    assert "cumulative.failed-20260810" not in summary["deleted"]


def test_prune_missing_root_is_nonfatal(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    summary = module._prune_runs_retention(tmp_path / "does-not-exist")
    assert summary["skipped_root"] is not None
    assert summary["deleted"] == []


def test_prune_under_keep_threshold_deletes_nothing(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    _make_entry(tmp_path, "cumulative", is_dir=True, mtime=1)
    kept_log = _make_entry(tmp_path, "off-cell-20260810T000000.log", is_dir=False, mtime=10)
    kept_archive = _make_entry(tmp_path, "cumulative.pre-x-20260810", is_dir=True, mtime=20)

    summary = module._prune_runs_retention(tmp_path)

    assert kept_log.exists() and kept_archive.exists()
    assert summary["deleted"] == []
