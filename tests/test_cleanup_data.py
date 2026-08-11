from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from cleanup_data import cleanup_entries  # noqa: E402


def _age(path: Path, days_ago: float) -> None:
    """Set path mtime to ``days_ago`` days in the past."""
    ts = time.time() - (days_ago * 24 * 3600)
    os.utime(path, (ts, ts))


def _mk_cells(data: Path) -> Path:
    cells = data / "cells"
    cells.mkdir(parents=True, exist_ok=True)
    return cells


def test_deletes_entries_older_than_7_days(tmp_path: Path) -> None:
    data = tmp_path / "data"
    old_dir = _mk_cells(data) / "1700000000-cell"
    old_dir.mkdir()
    _age(old_dir, days_ago=8)
    old_file = data / "extract" / "old.json"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("{}")
    _age(old_file, days_ago=8)

    removed = cleanup_entries(data)

    assert not old_dir.exists()
    assert not old_file.exists()
    assert removed == 2


def test_keeps_entries_newer_than_7_days(tmp_path: Path) -> None:
    data = tmp_path / "data"
    new_dir = _mk_cells(data) / "999999999-cell"
    new_dir.mkdir()
    new_file = data / "extract" / "new.json"
    new_file.parent.mkdir(parents=True)
    new_file.write_text("{}")

    removed = cleanup_entries(data)

    assert new_dir.exists()
    assert new_file.exists()
    assert removed == 0


def test_keeps_gitkeep_and_top_level_structure(tmp_path: Path) -> None:
    data = tmp_path / "data"
    gitkeep = _mk_cells(data) / ".gitkeep"
    gitkeep.write_text("")
    _age(gitkeep, days_ago=9)
    readme = data / "README.md"
    readme.write_text("# data\n")
    _age(readme, days_ago=9)

    cleanup_entries(data)

    assert gitkeep.exists()
    assert readme.exists()
    assert (data / "extract").is_dir()


def test_robust_to_non_timestamp_dir_name(tmp_path: Path) -> None:
    data = tmp_path / "data"
    old_weird = _mk_cells(data) / "not-a-timestamp"
    old_weird.mkdir()
    _age(old_weird, days_ago=9)
    new_weird = data / "extract" / "arbitrary-name"
    new_weird.mkdir(parents=True)

    removed = cleanup_entries(data)

    assert not old_weird.exists()
    assert new_weird.exists()


def test_idempotent(tmp_path: Path) -> None:
    data = tmp_path / "data"
    old = _mk_cells(data) / "1-old"
    old.mkdir()
    _age(old, days_ago=8)

    cleanup_entries(data)
    removed_second = cleanup_entries(data)

    assert removed_second == 0
    assert not old.exists()


def test_dry_run_does_not_delete(tmp_path: Path) -> None:
    data = tmp_path / "data"
    old = _mk_cells(data) / "2-old"
    old.mkdir()
    _age(old, days_ago=8)

    removed = cleanup_entries(data, dry_run=True)

    assert old.exists()
    assert removed == 0


def test_missing_data_dir_created_not_fatal(tmp_path: Path) -> None:
    data = tmp_path / "does-not-exist" / "data"

    cleanup_entries(data)

    assert (data / "cells").is_dir()
    assert (data / "extract").is_dir()