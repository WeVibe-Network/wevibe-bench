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
        model="local-llm-proxy/kimi/kimi-k3",
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
        initial_inner=["opencode", "run", "--model", "local-llm-proxy/kimi/kimi-k3", "--dir", "/work", "--format", "json"],
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


def test_opencode_runs_always_carry_per_cell_config_path() -> None:
    """Every `opencode run` construction must pass --config /work/opencode.json.

    The per-cell opencode.json (written to the worktree and bind-mounted at
    /work/opencode.json) is what routes the worker model to the local :4545
    relay. `docker exec` does not forward host env, so the baked
    OPENCODE_CONFIG env cannot be overridden per attempt; the explicit
    --config flag is the only way the per-cell config gets loaded. A bare
    `opencode run` with no --config would fall back to the container image's
    config and lose the intended local routing.
    """
    repo_root = Path(__file__).resolve().parents[1]
    adapter_path = repo_root / "wevibe_bench" / "adapters" / "backgammon.py"
    payload = adapter_path.read_text(encoding="utf-8")

    # Each construction block is `NAME = [ ... ]`. The list bodies contain no
    # nested ']', so the first closing bracket after the header is the block end.
    def block_body(name: str) -> str:
        match = re.search(rf"{name}\s*=\s*\[(.*?)\]", payload, re.DOTALL)
        assert match is not None, f"{name} construction not found in backgammon.py"
        return match.group(1)

    for name in ("initial_inner", "resume_inner", "feedback_inner"):
        body = block_body(name)
        assert '"--config"' in body, (
            f"{name} construction is missing the explicit '--config' flag; "
            "the per-cell /work/opencode.json (local :4545 routing) would not be loaded."
        )
        assert '"/work/opencode.json"' in body, (
            f"{name} construction is missing the '/work/opencode.json' path for '--config'."
        )


def test_serve_launch_carries_per_cell_config_env() -> None:
    """The serve-drive launch must load the per-cell config via OPENCODE_CONFIG.

    The cell's actual attempt path is serve-drive: `opencode serve` is started
    once per cell and a session is driven through it via serve_client. That
    serve is launched WITHOUT `--config` (opencode serve v1.18.15 has no such
    flag), and `docker exec` does not forward host env, so the OPENCODE_CONFIG
    env var must be injected inline into the serve launch script. Without it
    the serve inherits the container's baked config and the serve-created
    session falls back to the built-in model -> Invalid token -> 0 model turns
    -> cell VOID. The inline env override is the only delivery vector.
    """
    repo_root = Path(__file__).resolve().parents[1]
    adapter_path = repo_root / "wevibe_bench" / "adapters" / "docker_worker.py"
    payload = adapter_path.read_text(encoding="utf-8")
    assert "OPENCODE_CONFIG=/work/opencode.json" in payload, (
        "the serve launch script must set OPENCODE_CONFIG=/work/opencode.json "
        "so the per-cell config (local :4545 routing) is loaded by `opencode serve`."
    )


def test_serve_config_written_before_serve_boots() -> None:
    """The per-cell config must exist before the serve boots.

    `opencode serve` boots once per cell and is reused across all attempts, so
    the per-cell config file (/work/opencode.json) must be written before
    `active_cell.start_serve()` runs. A config written after serve boot would
    never be read by the already-running serve. Guard the call ordering: the
    first `_write_worker_permission_config(worktree=worktree)` occurrence must
    precede the first `active_cell.start_serve()` occurrence in backgammon.py.
    """
    repo_root = Path(__file__).resolve().parents[1]
    adapter_path = repo_root / "wevibe_bench" / "adapters" / "backgammon.py"
    payload = adapter_path.read_text(encoding="utf-8")

    config_write = payload.index("_write_worker_permission_config(worktree=worktree)")
    serve_boot = payload.index("active_cell.start_serve()")
    assert config_write < serve_boot, (
        "the per-cell config write must appear before active_cell.start_serve(); "
        "a config written after serve boot would be missed (serve boots once per cell)."
    )


def test_agents_md_written_after_seed() -> None:
    """/work/AGENTS.md must be written AFTER the scaffold seed, not wiped by it.

    The cell worktree seed is a full overlay of the scaffold tree: any file
    placed in the worktree before the copy runs is silently replaced. The
    2026-08-09 cells booted with no /work/AGENTS.md at all (4 consecutive
    runs) because the write never landed. Guard the call ordering: the
    AGENTS.md write must appear after the scaffold `_copy_tree_contents`
    call in backgammon.py, and the written content must carry the live
    model line so the worker knows what it is running as.
    """
    repo_root = Path(__file__).resolve().parents[1]
    adapter_path = repo_root / "wevibe_bench" / "adapters" / "backgammon.py"
    payload = adapter_path.read_text(encoding="utf-8")
    seed = payload.index('self._copy_tree_contents(self.task_dir / "scaffold", worktree)')
    agents_write = payload.index('(worktree / "AGENTS.md").write_text(')
    assert seed < agents_write, (
        "the /work/AGENTS.md write must appear AFTER the scaffold "
        "_copy_tree_contents(...); written before the seed it is wiped by the "
        "overlay and the worker receives no repo-orientation context."
    )
    assert '"- Model: {self.model}' in payload or "f\"- Model: {self.model}" in payload, (
        "the seeded AGENTS.md must carry the live model/provider line; without it "
        "the worker's model identity exists only in opencode.json, never as "
        "natural-language context."
    )
