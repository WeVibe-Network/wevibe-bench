from __future__ import annotations

from pathlib import Path

import pytest

from wevibe_bench.adapters.backgammon import (
    BackgammonRunner,
    _scan_cell_delivery,
    _scan_injected_block_chars,
)


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


def test_scan_injected_block_chars_reads_new_block_chars_shape(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=2 block_chars=2400 sid=ses_new newly_served=2\n",
    )

    assert _scan_injected_block_chars(worktree) == 2400


def test_scan_injected_block_chars_falls_back_to_legacy_chars_shape(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=2 chars=1207 sid=ses_legacy newly_served=2\n",
    )

    assert _scan_injected_block_chars(worktree) == 1207


def test_scan_injected_block_chars_sums_multiple_inject_events(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=1 block_chars=1000 sid=ses_a newly_served=1\n"
        "[inject] injected count=1 block_chars=400 sid=ses_b newly_served=1\n",
    )

    assert _scan_injected_block_chars(worktree) == 1400


def test_scan_injected_block_chars_prefers_block_chars_when_both_present(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=2 block_chars=2400 chars=9999 sid=ses_mix newly_served=2\n",
    )

    assert _scan_injected_block_chars(worktree) == 2400


def test_scan_injected_block_chars_returns_none_when_log_missing_or_without_injects(tmp_path: Path) -> None:
    missing = tmp_path / "missing-worktree"
    missing.mkdir(parents=True, exist_ok=True)
    assert _scan_injected_block_chars(missing) is None

    no_inject = tmp_path / "no-inject-worktree"
    no_inject.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        no_inject,
        "[plugin] startup complete\n"
        "[inject] unrelated marker\n",
    )

    assert _scan_injected_block_chars(no_inject) is None


def test_scan_cell_delivery_with_additive_cadence_fields(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=2 block_chars=2400 cadence=once block_tokens=600 top_k=5 sid=s1 newly_served=2 injected_once=5 budget_remaining=7000\n",
    )

    assert _scan_cell_delivery(worktree) == "YES"


def test_scan_injected_block_chars_with_additive_cadence_fields(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=2 block_chars=2400 cadence=once block_tokens=600 top_k=5 sid=s1 newly_served=2 injected_once=5 budget_remaining=7000\n",
    )

    assert _scan_injected_block_chars(worktree) == 2400


def test_restore_lines_are_not_counted_as_injections(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=1 block_chars=1200 sid=s1 newly_served=1 injected_once=1 budget_remaining=7800 cadence=once\n"
        "[inject] restored count=2 block_chars=2400 sid=s1 cadence=once compaction_restores=1\n",
    )

    assert _scan_cell_delivery(worktree) == "YES"
    assert _scan_injected_block_chars(worktree) == 1200


def test_restore_only_log_is_not_an_injection(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] restored count=2 block_chars=2400 sid=s1 cadence=once compaction_restores=1\n",
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


def test_run_cell_impl_sets_injected_block_est_tokens_from_scanned_chars(
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
                "[inject] injected count=2 block_chars=1207 sid=ses_tokens newly_served=2\n",
            )

    monkeypatch.setattr(
        BackgammonRunner,
        "_copy_tree_contents",
        staticmethod(_copy_tree_contents_with_log),
    )

    result = runner._run_cell_impl(
        run_label="delivery-on-est-tokens",
        run_dir=tmp_path / "delivery-on-est-tokens",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.injected_block_chars == 1207
    assert result.injected_block_est_tokens == round(1207 / 4)


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


@pytest.mark.slow
def test_run_cell_impl_sets_delivery_na_when_memory_off(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, memory_mode="off")

    result = runner._run_cell_impl(
        run_label="delivery-off-na",
        run_dir=tmp_path / "delivery-off-na",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.delivery == "N/A"
