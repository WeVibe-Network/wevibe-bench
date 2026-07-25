from __future__ import annotations

from pathlib import Path

import pytest

from wevibe_bench.adapters.backgammon import BackgammonRunner, _scan_cell_delivery


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def _write_plugin_log(worktree: Path, contents: str) -> None:
    log_path = worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(contents, encoding="utf-8")


def _make_runner(tmp_path: Path, *, memory_mode: str = "on") -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="openrouter/anthropic/claude-opus-4.8",
        memory_mode=memory_mode,
        mock="golden",
    )


def test_scan_cell_delivery_returns_yes_when_any_inject_count_is_positive(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=0 chars=10 sid=ses_1 newly_served=0\n"
        "[inject] injected count=2 chars=1207 sid=ses_2 newly_served=2\n",
    )

    assert _scan_cell_delivery(worktree) == "YES"


def test_scan_cell_delivery_returns_no_when_all_inject_counts_are_zero(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=0 chars=100 sid=ses_1 newly_served=0\n"
        "[inject] injected count=0 chars=222 sid=ses_2 newly_served=0\n",
    )

    assert _scan_cell_delivery(worktree) == "NO"


def test_scan_cell_delivery_returns_none_when_log_file_is_missing(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    assert _scan_cell_delivery(worktree) is None


def test_scan_cell_delivery_returns_none_when_no_inject_lines_exist(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[plugin] startup complete\n"
        "[inject] unrelated marker\n",
    )

    assert _scan_cell_delivery(worktree) is None


def test_run_cell_impl_sets_delivery_yes_when_memory_on_and_inject_log_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, memory_mode="on")
    monkeypatch.setattr(runner, "_prepare_memory_mode", lambda *, worktree: False)
    monkeypatch.setattr(
        runner,
        "_run_gate_report",
        lambda **kwargs: {"verdict": "PASS", "conformed": True, "problems": [], "failed_gates": []},
    )

    original_copy_tree_contents = BackgammonRunner._copy_tree_contents

    def _copy_tree_contents_with_log(src_dir: Path, dst_dir: Path) -> None:
        original_copy_tree_contents(src_dir, dst_dir)
        if Path(src_dir).name == "golden":
            _write_plugin_log(
                dst_dir,
                "[inject] injected count=2 chars=1207 sid=ses_delivery newly_served=2\n",
            )

    monkeypatch.setattr(
        BackgammonRunner,
        "_copy_tree_contents",
        staticmethod(_copy_tree_contents_with_log),
    )

    result = runner._run_cell_impl(
        run_label="delivery-on-yes",
        run_dir=tmp_path / "delivery-on-yes",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.delivery == "YES"


def test_run_cell_impl_sets_delivery_not_measured_when_memory_on_without_inject_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, memory_mode="on")
    monkeypatch.setattr(runner, "_prepare_memory_mode", lambda *, worktree: False)
    monkeypatch.setattr(
        runner,
        "_run_gate_report",
        lambda **kwargs: {"verdict": "PASS", "conformed": True, "problems": [], "failed_gates": []},
    )

    result = runner._run_cell_impl(
        run_label="delivery-on-not-measured",
        run_dir=tmp_path / "delivery-on-not-measured",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.delivery == "not_measured"


def test_run_cell_impl_sets_delivery_na_when_memory_off(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, memory_mode="off")

    result = runner._run_cell_impl(
        run_label="delivery-off-na",
        run_dir=tmp_path / "delivery-off-na",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.delivery == "N/A"
