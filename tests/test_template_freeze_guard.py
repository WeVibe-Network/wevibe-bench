"""WO-FREEZE-1 template-freeze guard tests.

Covers:
  (a) the live `tasks/backgammon/scaffold/` hash equals FROZEN_TASK_TEMPLATE_HASH;
  (b) compute_task_template_hash over a DELIBERATELY ALTERED temp copy differs
      from the frozen hash and verify_task_template_frozen RAISES naming the
      expected vs actual hashes;
  (c) the guard is wired into prepare_fixture (fail-closed propagates out of the
      run path against altered bytes).

The altered copy is a pytest ``tmp_path`` directory only — the real
``tasks/backgammon/scaffold/`` files are never modified (verified by (a), which
asserts the live hash still equals the frozen value).
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any

import pytest


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    module = importlib.util.spec_from_file_location(
        "run_cumulative_freeze_test", script_path
    )
    assert module is not None
    loaded = importlib.util.module_from_spec(module)
    assert module.loader is not None
    module.loader.exec_module(loaded)
    return loaded


MODULE = _load_run_cumulative_module()

FROZEN = "08afc8011cde5b81e6e158def2bc040f42372bbc1e32e7ca125382c27031cdb1"
REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_SCAFFOLD = REPO_ROOT / "tasks" / "backgammon" / "scaffold"


def _altered_scaffold_copy(tmp_path: Path) -> Path:
    """Copy the real scaffold into a temp dir and alter one file's bytes."""
    copy = tmp_path / "altered-scaffold"
    shutil.copytree(LIVE_SCAFFOLD, copy)
    target = copy / "src" / "game.ts"
    target.write_bytes(target.read_bytes() + b"// WO-FREEZE-1 altered bytes\n")
    return copy


def test_live_scaffold_hash_matches_frozen() -> None:
    """(a) The live scaffold bytes still produce the frozen WO-FREEZE-1 hash."""
    live_hash = MODULE.compute_task_template_hash(LIVE_SCAFFOLD)
    assert live_hash is not None, "live scaffold must be present"
    assert live_hash == MODULE.FROZEN_TASK_TEMPLATE_HASH
    assert MODULE.FROZEN_TASK_TEMPLATE_HASH == FROZEN


def test_altered_copy_differs_and_guard_raises_with_mismatch_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(b) Altered bytes change the hash and the guard fails closed naming both."""
    altered = _altered_scaffold_copy(tmp_path)
    altered_hash = MODULE.compute_task_template_hash(altered)
    assert altered_hash is not None
    assert altered_hash != MODULE.FROZEN_TASK_TEMPLATE_HASH

    # Drive the real guard's comparison against the altered hash by feeding it
    # the altered copy's digest. verify_task_template_frozen is module-level and
    # hashes the repo scaffold; monkeypatching its hash dependency lets the
    # genuine mismatch-naming raise fire without touching the real scaffold.
    monkeypatch.setattr(MODULE, "compute_task_template_hash", lambda _scaffold: altered_hash)
    with pytest.raises(RuntimeError) as exc:
        MODULE.verify_task_template_frozen()
    message = str(exc.value)
    assert MODULE.FROZEN_TASK_TEMPLATE_HASH in message
    assert altered_hash in message
    assert "scaffold" in message


def test_guard_wired_into_prepare_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(c) prepare_fixture invokes the fail-closed guard against altered bytes.

    The guard is the first statement in prepare_fixture, so it raises before any
    scaffold copy or cell run. We point the guard's hash computation at the
    altered copy's digest (real mismatch) and assert the RuntimeError propagates
    out of prepare_fixture with the mismatch named.
    """
    altered = _altered_scaffold_copy(tmp_path)
    altered_hash = MODULE.compute_task_template_hash(altered)
    assert altered_hash != MODULE.FROZEN_TASK_TEMPLATE_HASH

    runner = MODULE.RealSessionRunner.__new__(MODULE.RealSessionRunner)
    # _task_dir is set so the runner mirrors the real construction shape; the
    # guard raises before _state_for_session / _copy_tree_contents are reached.
    runner._task_dir = tmp_path / "task"

    monkeypatch.setattr(MODULE, "compute_task_template_hash", lambda _scaffold: altered_hash)
    with pytest.raises(RuntimeError) as exc:
        runner.prepare_fixture(_session())
    message = str(exc.value)
    assert MODULE.FROZEN_TASK_TEMPLATE_HASH in message
    assert altered_hash in message


def _session() -> Any:
    from wevibe_bench.cumulative.types import PhaseGroup, SessionRecord

    return SessionRecord(
        sequence_index=1,
        model="local-llm-proxy/x",
        provider_pin="local",
        memory_mode="off",
        phase_group=PhaseGroup.OFF_BASELINE.value,
        phase="RUN_SESSION",
    )