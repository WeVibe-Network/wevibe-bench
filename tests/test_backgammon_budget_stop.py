from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from typing import Any

import pytest

import wevibe_bench.adapters.backgammon as backgammon_mod
from wevibe_bench.adapters.backgammon import BackgammonRunner, _OpencodeRunStats


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def _make_runner(
    tmp_path: Path,
    *,
    cost_limit_usd: float | None = None,
    max_attempts: int = 8,
    max_output_tokens: int | None = None,
    max_steps_per_attempt: int | None = None,
    output_price_per_1m: float | None = None,
    mock: str | None = None,
    progress: Any = None,
) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="openrouter/anthropic/claude-opus-4.8",
        cost_limit_usd=cost_limit_usd,
        max_attempts=max_attempts,
        max_output_tokens=max_output_tokens,
        max_steps_per_attempt=max_steps_per_attempt,
        output_price_per_1m=output_price_per_1m,
        mock=mock,
        progress=progress,
    )


def _stats(
    *,
    session_id: str | None = "sess-1",
    killed_reason: str | None = None,
    exit_code: int | None = 0,
    cost_usd: float = 0.0,
    budget_stop_detected: bool = False,
    budget_stop_signature: str | None = None,
) -> _OpencodeRunStats:
    return _OpencodeRunStats(
        input_tokens=10,
        output_tokens=20,
        reasoning_tokens=5,
        turns=1,
        session_id=session_id,
        killed_reason=killed_reason,
        exit_code=exit_code,
        cost_usd=cost_usd,
        budget_stop_detected=budget_stop_detected,
        budget_stop_signature=budget_stop_signature,
    )


def _write_checkpoint(path: Path, *, hard: float, accrued: float, committed: float) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "test-run",
                "model_id": "anthropic/claude-opus-4.8",
                "profile_name": "opus",
                "hard_cap_usd": hard,
                "accrued_actual_usd": accrued,
                "committed_unproven_usd": committed,
                "outstanding": {},
                "updated_at": "2026-07-22T00:00:00+00:00",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _patch_fake_docker(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    state: dict[str, int] = {
        "force_kill_calls": 0,
        "process_kill_calls": 0,
        "container_removed": 0,
    }

    class _FakeDockerCellConfig:
        def __init__(
            self,
            *,
            worktree: Path,
            memory_mode: str,
            container_name: str,
            output_token_max: int | None = None,
        ) -> None:
            self.worktree = worktree
            self.memory_mode = memory_mode
            self.container_name = container_name
            self.output_token_max = output_token_max

    class _FakeDockerCell:
        def __init__(self, config: _FakeDockerCellConfig, progress: Any) -> None:
            self.config = config
            self.progress = progress

        def __enter__(self) -> "_FakeDockerCell":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

        def exec_argv(self, inner: list[str]) -> list[str]:
            return [sys.executable, "-c", "print('fake')", *inner]

        def force_kill(self) -> None:
            state["force_kill_calls"] += 1
            state["container_removed"] = 1

        def kill_worker_processes(self) -> None:
            state["process_kill_calls"] += 1

    monkeypatch.setattr(backgammon_mod, "DockerCellConfig", _FakeDockerCellConfig)
    monkeypatch.setattr(backgammon_mod, "DockerCell", _FakeDockerCell)
    monkeypatch.setattr(backgammon_mod, "docker_available", lambda: (True, "ok"))
    monkeypatch.setattr(backgammon_mod, "image_exists", lambda: True)

    real_run = backgammon_mod.subprocess.run

    def _run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, list) and cmd and cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No such container")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(backgammon_mod.subprocess, "run", _run)
    return state


def test_run_opencode_detects_402_budget_stop_error_event(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    script_path = tmp_path / "fake_opencode_budget_stop.py"
    script_path.write_text(
        textwrap.dedent(
            """
            import json

            event = {
                "type": "error",
                "sessionID": "ses-budget-stop",
                "error": {
                    "name": "APIError",
                    "data": {
                        "statusCode": 402,
                        "message": "reservation would exceed hard cap",
                        "responseBody": "{\\\"error\\\":{\\\"type\\\":\\\"insufficient_quota\\\",\\\"code\\\":\\\"budget_exceeded\\\"}}",
                    },
                },
            }
            print(json.dumps(event), flush=True)
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )

    stats = runner._run_opencode(
        cmd=[sys.executable, str(script_path)],
        worktree=tmp_path,
        events_path=tmp_path / "budget-stop.events.jsonl",
        env=os.environ.copy(),
        run_label="budget-stop-detect",
        phase="initial",
        fallback_session_id=None,
        kill_hook=None,
    )

    assert stats.exit_code == 1
    assert stats.budget_stop_detected is True
    assert stats.budget_stop_signature is not None
    assert "status_code=402" in stats.budget_stop_signature


def test_pre_attempt_budget_exhaustion_returns_budget_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(
        tmp_path,
        cost_limit_usd=2.4,
        max_attempts=8,
        max_output_tokens=10000,
        max_steps_per_attempt=10,
        output_price_per_1m=25.0,
        progress=progress_lines.append,
    )
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: "INITIAL PROMPT")

    checkpoint = tmp_path / "proxy-checkpoint.json"
    _write_checkpoint(checkpoint, hard=2.4, accrued=1.8, committed=0.5)
    monkeypatch.setenv("WEVIBE_BENCH_PROXY_CHECKPOINT", str(checkpoint))

    call_count = {"count": 0}

    def _unexpected_opencode(**kwargs: Any) -> _OpencodeRunStats:
        call_count["count"] += 1
        raise AssertionError("_run_opencode must not run when attempt 1 is budget-exhausted")

    monkeypatch.setattr(runner, "_run_opencode", _unexpected_opencode)

    result = runner._run_cell_impl(
        run_label="budget-precheck-stop",
        run_dir=tmp_path / "budget-precheck-stop",
        task_id="backgammon",
        injected_memory=[],
    )

    assert call_count["count"] == 0
    assert result.verdict == "BUDGET_STOP"
    assert result.termination_reason == "attempts_exhausted_by_budget"
    assert result.attempts_to_green == "BUDGET_STOP"
    assert result.attempt_reports == []
    assert any("step=budget-decision" in line and "decision=budget_stop" in line for line in progress_lines)


def test_mid_attempt_402_maps_to_budget_stop_and_writes_user_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=2.4, max_attempts=8)
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: "INITIAL PROMPT")

    checkpoint = tmp_path / "proxy-checkpoint.json"
    _write_checkpoint(checkpoint, hard=2.4, accrued=0.1, committed=0.0)
    monkeypatch.setenv("WEVIBE_BENCH_PROXY_CHECKPOINT", str(checkpoint))

    gate_calls = {"count": 0}

    def _fake_gate(**kwargs: Any) -> dict[str, Any]:
        gate_calls["count"] += 1
        if gate_calls["count"] == 1:
            return {
                "verdict": "FAIL",
                "conformed": True,
                "problems": [{"check": "gate-check"}],
                "failed_gates": ["gate-check"],
            }
        raise AssertionError("should stop before second gate when feedback hits 402")

    monkeypatch.setattr(runner, "_run_gate_report", _fake_gate)

    opencode_calls = {"count": 0}

    def _fake_opencode(**kwargs: Any) -> _OpencodeRunStats:
        opencode_calls["count"] += 1
        if opencode_calls["count"] == 1:
            return _stats(session_id="sess-1", exit_code=0, cost_usd=0.6)
        if opencode_calls["count"] == 2:
            return _stats(
                session_id="sess-1",
                exit_code=1,
                cost_usd=0.0,
                budget_stop_detected=True,
                budget_stop_signature="status_code=402 error_code=budget_exceeded",
            )
        raise AssertionError("unexpected extra _run_opencode call")

    monkeypatch.setattr(runner, "_run_opencode", _fake_opencode)

    result = runner._run_cell_impl(
        run_label="mid-attempt-402",
        run_dir=tmp_path / "mid-attempt-402",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "BUDGET_STOP"
    assert result.termination_reason == "budget_stop_mid_attempt"
    assert result.attempts_to_green == "BUDGET_STOP"
    assert result.attempt_reports[-1]["termination_reason"] == "budget_stop_mid_attempt"

    sidecar_path = Path(f"{result.worktree}.user-events.jsonl")
    sidecar_lines = [json.loads(line) for line in sidecar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["attempt"] for row in sidecar_lines] == [1, 2]
    assert sidecar_lines[0]["text"] == "INITIAL PROMPT"
    assert sidecar_lines[1]["text"] == runner._build_feedback_prompt(checks=["gate-check"])


def test_opencode_argv_omits_prompt_positional_and_delivers_prompts_via_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=2)
    _patch_fake_docker(monkeypatch)

    task_prompt = "D6 initial prompt marker"
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: task_prompt)

    gate_calls = {"count": 0}

    def _fake_gate(**kwargs: Any) -> dict[str, Any]:
        gate_calls["count"] += 1
        if gate_calls["count"] == 1:
            return {
                "verdict": "FAIL",
                "conformed": True,
                "problems": [{"check": "gate-check"}],
                "failed_gates": ["gate-check"],
            }
        return {
            "verdict": "PASS",
            "conformed": True,
            "problems": [],
            "failed_gates": [],
        }

    monkeypatch.setattr(runner, "_run_gate_report", _fake_gate)

    captured_cmds: list[list[str]] = []
    captured_stdin: list[str | None] = []

    def _fake_opencode(**kwargs: Any) -> _OpencodeRunStats:
        captured_cmds.append(list(kwargs["cmd"]))
        captured_stdin.append(kwargs.get("stdin_text"))
        return _stats(session_id="sess-1", exit_code=0, cost_usd=0.0)

    monkeypatch.setattr(runner, "_run_opencode", _fake_opencode)

    result = runner._run_cell_impl(
        run_label="stdin-delivery",
        run_dir=tmp_path / "stdin-delivery",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "PASS"
    assert len(captured_cmds) == 2

    feedback_prompt = runner._build_feedback_prompt(checks=["gate-check"])
    initial_cmd, feedback_cmd = captured_cmds
    initial_inner = initial_cmd[initial_cmd.index("opencode") :]
    feedback_inner = feedback_cmd[feedback_cmd.index("opencode") :]

    assert task_prompt not in initial_cmd
    assert feedback_prompt not in feedback_cmd
    assert "" not in initial_cmd
    assert "" not in feedback_cmd
    assert initial_inner[:3] == ["opencode", "run", "--model"]
    assert feedback_inner[:3] == ["opencode", "run", "--session"]
    assert captured_stdin == [task_prompt, feedback_prompt]


def test_feedback_gap_injects_pass_verdict_before_failure_feedback_with_sidecar_fidelity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=3)
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: "INITIAL PROMPT")

    gate_calls = {"count": 0}

    def _fake_gate(**kwargs: Any) -> dict[str, Any]:
        gate_calls["count"] += 1
        if gate_calls["count"] == 1:
            return {
                "verdict": "FAIL",
                "conformed": True,
                "problems": [{"check": "[G02] B"}],
                "failed_gates": ["[G01] A", "[G02] B", "[G03] C"],
            }
        if gate_calls["count"] == 2:
            return {
                "verdict": "FAIL",
                "conformed": True,
                "problems": [{"check": "[G02] B"}],
                "failed_gates": ["[G02] B"],
            }
        return {
            "verdict": "PASS",
            "conformed": True,
            "problems": [],
            "failed_gates": [],
        }

    monkeypatch.setattr(runner, "_run_gate_report", _fake_gate)

    calls: list[dict[str, Any]] = []

    def _fake_opencode(**kwargs: Any) -> _OpencodeRunStats:
        calls.append({"phase": kwargs.get("phase"), "stdin_text": kwargs.get("stdin_text")})
        return _stats(session_id="sess-1", exit_code=0, cost_usd=0.0)

    monkeypatch.setattr(runner, "_run_opencode", _fake_opencode)

    result = runner._run_cell_impl(
        run_label="feedback-gap-pass-verdict",
        run_dir=tmp_path / "feedback-gap-pass-verdict",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "PASS"
    assert len(calls) == 4

    pass_verdict = "That fixed it — [G01] A, [G03] C all pass now."
    failure_feedback = runner._build_feedback_prompt(checks=["[G02] B"], had_pass_verdict=True)
    assert failure_feedback.startswith(
        "The rest are still failing — fix the implementation so they pass. Do not explain, just edit the code."
    )

    phases = [entry["phase"] for entry in calls]
    stdin_texts = [entry["stdin_text"] for entry in calls]
    assert phases == ["initial", "feedback-1", "verdict-pass-2", "feedback-2"]
    assert stdin_texts == [
        "INITIAL PROMPT",
        runner._build_feedback_prompt(checks=["[G02] B"], had_pass_verdict=False),
        pass_verdict,
        failure_feedback,
    ]

    verdict_idx = phases.index("verdict-pass-2")
    feedback_idx = phases.index("feedback-2")
    assert verdict_idx < feedback_idx
    assert calls[verdict_idx]["stdin_text"] == pass_verdict
    assert calls[feedback_idx]["stdin_text"] == failure_feedback

    sidecar_path = Path(f"{result.worktree}.user-events.jsonl")
    sidecar_rows = [json.loads(line) for line in sidecar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["attempt"] for row in sidecar_rows] == [1, 2, 3, 3]
    assert [row["text"] for row in sidecar_rows[:3]] == [
        "INITIAL PROMPT",
        runner._build_feedback_prompt(checks=["[G02] B"], had_pass_verdict=False),
        pass_verdict,
    ]
    assert sidecar_rows[2]["text"] == pass_verdict
    assert sidecar_rows[3]["text"].startswith(
        "The rest are still failing — fix the implementation so they pass. Do not explain, just edit the code."
    )

    assert [row["text"] for row in sidecar_rows] == stdin_texts


def test_zero_progress_gap_has_no_pass_verdict_and_uses_false_header(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=3)
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: "INITIAL PROMPT")

    gate_calls = {"count": 0}

    def _fake_gate(**kwargs: Any) -> dict[str, Any]:
        gate_calls["count"] += 1
        if gate_calls["count"] in {1, 2}:
            return {
                "verdict": "FAIL",
                "conformed": True,
                "problems": [{"check": "[G02] B"}],
                "failed_gates": ["[G02] B"],
            }
        return {
            "verdict": "PASS",
            "conformed": True,
            "problems": [],
            "failed_gates": [],
        }

    monkeypatch.setattr(runner, "_run_gate_report", _fake_gate)

    calls: list[dict[str, Any]] = []

    def _fake_opencode(**kwargs: Any) -> _OpencodeRunStats:
        calls.append({"phase": kwargs.get("phase"), "stdin_text": kwargs.get("stdin_text")})
        return _stats(session_id="sess-1", exit_code=0, cost_usd=0.0)

    monkeypatch.setattr(runner, "_run_opencode", _fake_opencode)

    result = runner._run_cell_impl(
        run_label="feedback-gap-zero-progress",
        run_dir=tmp_path / "feedback-gap-zero-progress",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "PASS"

    phases = [entry["phase"] for entry in calls]
    assert phases == ["initial", "feedback-1", "feedback-2"]
    assert all(phase != "verdict-pass-1" for phase in phases)
    assert all(phase != "verdict-pass-2" for phase in phases)

    expected_false_header_prompt = runner._build_feedback_prompt(checks=["[G02] B"], had_pass_verdict=False)
    assert calls[1]["stdin_text"] == expected_false_header_prompt
    assert calls[2]["stdin_text"] == expected_false_header_prompt

    sidecar_path = Path(f"{result.worktree}.user-events.jsonl")
    sidecar_rows = [json.loads(line) for line in sidecar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["text"] for row in sidecar_rows] == [
        "INITIAL PROMPT",
        expected_false_header_prompt,
        expected_false_header_prompt,
    ]
    assert all(
        row["text"].startswith(
            "These are still failing — fix the implementation so they pass. Do not explain, just edit the code."
        )
        for row in sidecar_rows[1:]
    )


@pytest.mark.parametrize(
    ("conformed", "expected_attempts_to_green"),
    [
        (True, "FAIL"),
        (False, "DID_NOT_CONFORM"),
    ],
)
def test_hard_attempt_ceiling_sets_fail_termination_label(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    conformed: bool,
    expected_attempts_to_green: str,
) -> None:
    runner = _make_runner(tmp_path, mock="scaffold", max_attempts=2)

    monkeypatch.setattr(
        runner,
        "_run_gate_report",
        lambda **kwargs: {
            "verdict": "FAIL",
            "conformed": conformed,
            "problems": [{"check": "x"}],
            "failed_gates": ["x"],
        },
    )

    result = runner._run_cell_impl(
        run_label="ceiling",
        run_dir=tmp_path / "ceiling",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "FAIL"
    assert result.termination_reason == "attempt_ceiling_reached"
    assert result.attempts_to_green == expected_attempts_to_green
    assert result.attempt_reports[-1]["termination_reason"] == "attempt_ceiling_reached"


def test_harness_limit_kill_does_not_force_budget_stop_and_loop_can_continue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=2)
    docker_state = _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: "PROMPT")

    gate_calls = {"count": 0}

    def _fake_gate(**kwargs: Any) -> dict[str, Any]:
        gate_calls["count"] += 1
        if gate_calls["count"] == 1:
            return {
                "verdict": "FAIL",
                "conformed": True,
                "problems": [{"check": "gate-check"}],
                "failed_gates": ["gate-check"],
            }
        return {
            "verdict": "PASS",
            "conformed": True,
            "problems": [],
            "failed_gates": [],
        }

    monkeypatch.setattr(runner, "_run_gate_report", _fake_gate)

    opencode_calls = {"count": 0}

    def _fake_opencode(**kwargs: Any) -> _OpencodeRunStats:
        opencode_calls["count"] += 1
        if opencode_calls["count"] == 1:
            kill_hook = kwargs.get("kill_hook")
            assert callable(kill_hook)
            kill_hook()
            return _stats(session_id="sess-1", killed_reason="run_timeout", exit_code=137, cost_usd=0.4)
        if docker_state["container_removed"]:
            return _stats(session_id="sess-1", killed_reason=None, exit_code=1, cost_usd=0.0)
        return _stats(session_id="sess-1", killed_reason=None, exit_code=0, cost_usd=0.1)

    monkeypatch.setattr(runner, "_run_opencode", _fake_opencode)

    result = runner._run_cell_impl(
        run_label="harness-limit-continue",
        run_dir=tmp_path / "harness-limit-continue",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "PASS"
    assert result.termination_reason == "gates_green"
    assert result.attempts_to_green == 1
    assert docker_state["process_kill_calls"] == 1
    assert docker_state["force_kill_calls"] == 0


def test_non_budget_nonzero_worker_exit_classifies_as_harness_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=2)
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: "PROMPT")
    monkeypatch.setattr(
        runner,
        "_run_opencode",
        lambda **kwargs: _stats(
            session_id="sess-1",
            killed_reason=None,
            exit_code=1,
            cost_usd=0.0,
            budget_stop_detected=False,
        ),
    )

    result = runner._run_cell_impl(
        run_label="harness-error",
        run_dir=tmp_path / "harness-error",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "FAIL"
    assert result.termination_reason == "harness_error"
    assert result.attempts_to_green == "FAIL"
    assert result.attempt_reports == []
