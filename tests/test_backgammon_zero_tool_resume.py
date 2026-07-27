from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import textwrap
from typing import Any

import pytest

from wevibe_bench.adapters.backgammon import (
    BackgammonRunner,
    MAX_ZERO_TOOL_RESUMES,
    ZERO_TOOL_RESUME_NUDGE,
    _OpencodeRunStats,
)


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def _make_runner(tmp_path: Path, *, progress: Any = None) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="orcarouter/kimi/kimi-k3",
        run_timeout_s=30,
        completion_grace_s=2,
        progress=progress,
    )


def _write_fake_opencode(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "fake_opencode.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    return script


def _run_script(
    runner: BackgammonRunner,
    *,
    script_path: Path,
    events_path: Path,
    run_label: str,
    phase: str,
) -> _OpencodeRunStats:
    return runner._run_opencode(
        cmd=[sys.executable, str(script_path)],
        worktree=script_path.parent,
        events_path=events_path,
        env=os.environ.copy(),
        run_label=run_label,
        phase=phase,
        fallback_session_id=None,
        kill_hook=None,
    )


class _FakeCell:
    def __init__(self) -> None:
        self.inner_calls: list[list[str]] = []

    def exec_argv(self, inner: list[str]) -> list[str]:
        self.inner_calls.append(list(inner))
        return ["fake-docker-exec", *inner]


def _stats(
    *,
    session_id: str | None,
    terminal_zero_tool_turn: bool,
    zero_tool_turns: int = 1,
    truncations: int = 0,
) -> _OpencodeRunStats:
    return _OpencodeRunStats(
        input_tokens=10,
        output_tokens=20,
        reasoning_tokens=5,
        turns=1,
        session_id=session_id,
        killed_reason=None,
        exit_code=0,
        cost_usd=0.2,
        truncations=truncations,
        zero_tool_turns=zero_tool_turns,
        terminal_zero_tool_turn=terminal_zero_tool_turn,
    )


def test_run_opencode_detects_text_only_zero_tool_turn(tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, progress=progress_lines.append)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "sessionID": "sess-text-only", "part": {}})
        emit(
            {
                "type": "step_finish",
                "sessionID": "sess-text-only",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 3, "output": 120, "reasoning": 80},
                },
            }
        )
        """,
    )

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "text-only.events.jsonl",
        run_label="text-only",
        phase="attempt-1",
    )

    assert stats.zero_tool_turns == 1
    assert stats.terminal_zero_tool_turn is True
    assert any("step=zero-tool-turn" in line for line in progress_lines)


def test_run_opencode_does_not_mark_zero_tool_when_tool_use_exists(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "sessionID": "sess-tools", "part": {}})
        emit({"type": "tool_use", "sessionID": "sess-tools", "part": {"tool": "bash"}})
        emit(
            {
                "type": "step_finish",
                "sessionID": "sess-tools",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 2, "output": 8, "reasoning": 1},
                },
            }
        )
        """,
    )

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "tools.events.jsonl",
        run_label="tools",
        phase="attempt-1",
    )

    assert stats.zero_tool_turns == 0
    assert stats.terminal_zero_tool_turn is False


def test_run_opencode_records_length_finish_as_truncation(tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, progress=progress_lines.append)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "sessionID": "sess-length", "part": {}})
        emit(
            {
                "type": "step_finish",
                "sessionID": "sess-length",
                "part": {
                    "reason": "length",
                    "tokens": {"input": 3, "output": 100, "reasoning": 55},
                },
            }
        )
        """,
    )

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "length.events.jsonl",
        run_label="length",
        phase="attempt-1",
    )

    assert stats.truncations == 1
    assert any("step=TRUNCATION" in line and "reason=length" in line for line in progress_lines)


def test_zero_tool_resume_is_bounded_and_honest_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, progress=progress_lines.append)
    fake_cell = _FakeCell()

    # Always ends with a zero-tool turn: initial + each bounded resume.
    scripted_stats = [
        _stats(session_id="sess-ztr", terminal_zero_tool_turn=True),
        _stats(session_id="sess-ztr", terminal_zero_tool_turn=True),
        _stats(session_id="sess-ztr", terminal_zero_tool_turn=True),
    ]

    run_calls: list[dict[str, Any]] = []

    def _fake_run_opencode(**kwargs: Any) -> _OpencodeRunStats:
        run_calls.append(kwargs)
        return scripted_stats[len(run_calls) - 1]

    monkeypatch.setattr(runner, "_run_opencode", _fake_run_opencode)

    result = runner._run_opencode_with_zero_tool_resumes(
        active_cell=fake_cell,
        initial_inner=["opencode", "run", "--model", "orcarouter/kimi/kimi-k3", "--dir", "/work", "--format", "json"],
        pure=False,
        worktree=tmp_path,
        events_path=tmp_path / "events.jsonl",
        env=os.environ.copy(),
        run_label="bounded",
        phase="initial",
        fallback_session_id=None,
        prior_cost_usd=0.0,
        kill_hook=None,
        stdin_text="BIG ORIGINAL PROMPT",
    )

    assert result.zero_tool_resumes == MAX_ZERO_TOOL_RESUMES
    assert result.zero_tool_turn_honest_fail is True
    assert result.terminal_zero_tool_turn is True
    assert len(run_calls) == MAX_ZERO_TOOL_RESUMES + 1
    assert any("step=zero-tool-turn-resume" in line for line in progress_lines)
    assert any("outcome=honest-fail" in line for line in progress_lines)

    # Resume invocations are session-bound and nudged, never original prompt.
    resume_calls = run_calls[1:]
    assert all(call["stdin_text"] == ZERO_TOOL_RESUME_NUDGE for call in resume_calls)
    assert all(call["stdin_text"] != "BIG ORIGINAL PROMPT" for call in resume_calls)

    assert len(fake_cell.inner_calls) == MAX_ZERO_TOOL_RESUMES + 1
    for idx, argv in enumerate(fake_cell.inner_calls[1:], start=1):
        assert argv[:2] == ["opencode", "run"]
        assert "--session" in argv
        assert "sess-ztr" in argv
        assert "--dir" in argv and "/work" in argv
        assert "--format" in argv and "json" in argv
        assert "--model" not in argv
        assert "--agent" not in argv
        assert argv.count("--session") == 1, f"resume call {idx} malformed: {argv}"


def test_zero_tool_resume_not_used_when_turn_has_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    fake_cell = _FakeCell()

    run_calls: list[dict[str, Any]] = []

    def _fake_run_opencode(**kwargs: Any) -> _OpencodeRunStats:
        run_calls.append(kwargs)
        return _stats(session_id="sess-ok", terminal_zero_tool_turn=False, zero_tool_turns=0)

    monkeypatch.setattr(runner, "_run_opencode", _fake_run_opencode)

    result = runner._run_opencode_with_zero_tool_resumes(
        active_cell=fake_cell,
        initial_inner=["opencode", "run", "--session", "sess-ok", "--dir", "/work", "--format", "json"],
        pure=False,
        worktree=tmp_path,
        events_path=tmp_path / "events.jsonl",
        env=os.environ.copy(),
        run_label="no-resume",
        phase="feedback-1",
        fallback_session_id="sess-ok",
        prior_cost_usd=0.0,
        kill_hook=None,
        stdin_text="feedback prompt",
    )

    assert len(run_calls) == 1
    assert len(fake_cell.inner_calls) == 1
    assert result.zero_tool_resumes == 0
    assert result.zero_tool_turn_honest_fail is False


def test_tool_choice_required_guard_absent_in_harness_llm_sources() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    llm_call_sources = [
        repo_root / "wevibe_bench" / "adapters" / "backgammon.py",
        repo_root / "wevibe_bench" / "adapters" / "openrouter_proxy.py",
        repo_root / "wevibe_bench" / "adapters" / "openrouter_proxy_server.py",
    ]
    pattern = re.compile(r"[\"']tool_choice[\"']\s*:\s*[\"']required[\"']")

    offenders: list[str] = []
    for path in llm_call_sources:
        payload = path.read_text(encoding="utf-8")
        if pattern.search(payload):
            offenders.append(str(path))

    assert not offenders, f"tool_choice='required' is banned in harness LLM call paths: {offenders}"
