import inspect
from pathlib import Path
from typing import Any

import pytest

import wevibe_bench.adapters.backgammon as backgammon_mod
from wevibe_bench.adapters.backgammon import (
    BACKGAMMON_PROMPT,
    BackgammonCellResult,
    BackgammonRunner,
)
from wevibe_bench.backends.base import RecalledMemory


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def _make_runner(tmp_path: Path, *, memory_mode: str = "on") -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="openrouter/anthropic/claude-opus-4.8",
        memory_mode=memory_mode,
        progress=lambda _line: None,
    )


def _sample_injected_memory() -> list[RecalledMemory]:
    return [
        RecalledMemory(
            cid="cid-memory",
            score=1.0,
            vector_score=1.0,
            combined_score=1.0,
            keyword_score=1.0,
            matched_keywords=["backgammon"],
            text="DIRECT_INJECTION_MEMORY_MARKER",
        )
    ]


def test_cumulative_on_prompt_is_base_prompt_without_memory_blob(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, memory_mode="on")
    format_called = {"value": False}

    def _unexpected_format(memories: list[RecalledMemory]) -> str:
        del memories
        format_called["value"] = True
        raise AssertionError("_format_memory must not run for cumulative memory_mode='on'")

    monkeypatch.setattr(backgammon_mod, "_format_memory", _unexpected_format)

    prompt = runner._build_task_prompt(injected_memory=_sample_injected_memory())

    assert prompt.startswith(BACKGAMMON_PROMPT)
    assert "DIRECT_INJECTION_MEMORY_MARKER" not in prompt
    assert "# WEVIBE MEMORY CONTEXT" not in prompt
    assert format_called["value"] is False


def test_run_cell_scored_path_passes_empty_injected_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, memory_mode="on")
    captured: dict[str, Any] = {}

    def _fake_run_cell_impl(
        *,
        run_label: str,
        run_dir: Path,
        task_id: str,
        injected_memory: list[RecalledMemory],
    ) -> BackgammonCellResult:
        captured["run_label"] = run_label
        captured["run_dir"] = run_dir
        captured["task_id"] = task_id
        captured["injected_memory"] = injected_memory
        return BackgammonCellResult(
            verdict="PASS",
            attempts_to_green=0,
            termination_reason="gates_green",
            conformed=True,
            input_tokens=0,
            output_tokens=0,
            turns=0,
            wall_seconds=0.0,
            delivery="N/A",
            failed_gates=[],
            problems_final=[],
            attempt_reports=[],
            worktree=str(run_dir / "worktree"),
            session_id="sid-cumulative-on",
            memory_mode="on",
            model=runner.model,
        )

    monkeypatch.setattr(runner, "_run_cell_impl", _fake_run_cell_impl)

    result = runner.run_cell("run-cumulative-on", tmp_path / "run-cumulative-on")

    assert captured["run_label"] == "run-cumulative-on"
    assert captured["task_id"] == "backgammon"
    assert captured["injected_memory"] == []
    assert result.verdict == "PASS"


def test_backgammon_on_path_has_no_wevibe_memory_file_write_logic() -> None:
    prompt_source = inspect.getsource(BackgammonRunner._build_task_prompt)
    run_cell_source = inspect.getsource(BackgammonRunner.run_cell)

    assert "WEVIBE_MEMORY.md" not in prompt_source
    assert "write_text(" not in prompt_source
    assert "injected_memory=[]" in run_cell_source
