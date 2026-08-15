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
from wevibe_bench.adapters.docker_worker import ImageFingerprint


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
        model="local-llm-proxy/wevibe-bench-worker",
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
    terminal_zero_tool_turn: bool = False,
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
        terminal_zero_tool_turn=terminal_zero_tool_turn,
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
            # Mirrors the real DockerCell surface (docker_worker.py sets
            # self.container_name = config.container_name at construction).
            self.container_name = config.container_name

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

        def start_serve(self) -> None:
            # Live-view serve is a no-op in tests: never start a real `opencode
            # serve`. The serve-drive session is stubbed to fail closed (see the
            # ServeClient patch below) so the stdout fallback stays authoritative
            # — mirroring production, where a serve-drive failure is never a
            # scored-cell abort.
            pass

    monkeypatch.setattr(backgammon_mod, "DockerCellConfig", _FakeDockerCellConfig)
    monkeypatch.setattr(backgammon_mod, "DockerCell", _FakeDockerCell)
    monkeypatch.setattr(backgammon_mod, "docker_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        backgammon_mod,
        "worker_image_fingerprint",
        lambda: ImageFingerprint(
            image_id="sha256:fake-test-worker",
            created="2026-07-31T01:25:11Z",
        ),
    )

    real_run = backgammon_mod.subprocess.run

    def _run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, list) and cmd and cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No such container")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(backgammon_mod.subprocess, "run", _run)

    # Hermetic serve-drive stub: `_run_cell_impl` constructs a real ServeClient
    # and calls create_session() against 127.0.0.1:<serve_host_port>. That call
    # is wrapped in try/except ServeClientError (never a scored-cell abort), so
    # we make it fail closed here to keep the tests hermetic — no real HTTP
    # connection is ever attempted. The stdout fallback remains authoritative.
    class _FakeServeClient:
        def __init__(self, base_url: str, **kwargs: Any) -> None:
            self.base_url = base_url

        def create_session(self, title: str | None = None) -> str:
            raise backgammon_mod.ServeClientError(
                f"serve unavailable (hermetic test stub): {self.base_url}"
            )

    monkeypatch.setattr(backgammon_mod, "ServeClient", _FakeServeClient)
    return state


def test_opencode_argv_omits_prompt_positional_and_delivers_prompts_via_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=3)
    _patch_fake_docker(monkeypatch)

    task_prompt = "D6 initial prompt marker"
    monkeypatch.setattr(runner, "_load_chunk_prompts", lambda *, injected_memory: [task_prompt])

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
        captured_cmds.append(list(kwargs["initial_inner"]))
        captured_stdin.append(kwargs.get("stdin_text"))
        return _stats(session_id="sess-1", exit_code=0, cost_usd=0.0)

    monkeypatch.setattr(runner, "_run_opencode_with_zero_tool_resumes", _fake_opencode)

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
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=4)
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_load_chunk_prompts", lambda *, injected_memory: ["INITIAL PROMPT"])

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
        if gate_calls["count"] == 3:
            return {
                "verdict": "FAIL",
                "conformed": True,
                "problems": [{"check": "[G02] B"}],
                "failed_gates": ["[G02] B"],
            }
        if gate_calls["count"] == 4:
            return {
                "verdict": "PASS",
                "conformed": True,
                "problems": [],
                "failed_gates": [],
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
        return _stats(session_id="sess-1", exit_code=0, cost_usd=0.0, terminal_zero_tool_turn=False)

    monkeypatch.setattr(runner, "_run_opencode_with_zero_tool_resumes", _fake_opencode)

    result = runner._run_cell_impl(
        run_label="feedback-gap-pass-verdict",
        run_dir=tmp_path / "feedback-gap-pass-verdict",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "PASS"
    # 5 calls: initial + feedback-1 + verdict-pass-2 + feedback-2 + feedback-3
    # Gate [G02] B keeps failing until attempt 4, so there's an extra feedback round
    assert len(calls) == 5

    # WO-FEEDBACK-1: grader tokens are stripped from delivered text.
    pass_verdict = "That fixed it — A, C all pass now."
    failure_feedback = runner._build_feedback_prompt(checks=["[G02] B"], had_pass_verdict=True)
    assert failure_feedback.startswith(
        "The rest are still failing — fix the implementation so they pass. Do not explain, just edit the code."
    )

    phases = [entry["phase"] for entry in calls]
    stdin_texts = [entry["stdin_text"] for entry in calls]
    assert phases == ["initial", "feedback-1", "verdict-pass-2", "feedback-2", "feedback-3"]
    
    # Verify the initial prompt and pass verdict are correct
    assert stdin_texts[0] == "INITIAL PROMPT"
    # Feedback 1 (had_pass_verdict=False because no prior report to compare)
    assert stdin_texts[1] == runner._build_feedback_prompt(checks=["[G02] B"], had_pass_verdict=False)
    # Pass verdict
    assert stdin_texts[2] == pass_verdict
    # Feedback 2 (had_pass_verdict=True because gates A and C now pass)
    assert stdin_texts[3] == failure_feedback

    verdict_idx = phases.index("verdict-pass-2")
    feedback_idx = phases.index("feedback-2")
    assert verdict_idx < feedback_idx
    assert calls[verdict_idx]["stdin_text"] == pass_verdict
    assert calls[feedback_idx]["stdin_text"] == failure_feedback

    sidecar_path = Path(f"{result.worktree}.user-events.jsonl")
    sidecar_rows = [json.loads(line) for line in sidecar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # 5 rows: attempt 1 (initial), attempt 2 (feedback), attempt 3 (pass verdict + feedback), attempt 4 (feedback)
    assert len(sidecar_rows) == 5
    assert sidecar_rows[0]["text"] == "INITIAL PROMPT"
    assert sidecar_rows[1]["attempt"] == 2  # feedback-1
    assert sidecar_rows[2]["text"] == pass_verdict  # verdict-pass-2
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
    monkeypatch.setattr(runner, "_load_chunk_prompts", lambda *, injected_memory: ["INITIAL PROMPT"])

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
        return _stats(session_id="sess-1", exit_code=0, cost_usd=0.0, terminal_zero_tool_turn=False)

    monkeypatch.setattr(runner, "_run_opencode_with_zero_tool_resumes", _fake_opencode)

    result = runner._run_cell_impl(
        run_label="feedback-gap-zero-progress",
        run_dir=tmp_path / "feedback-gap-zero-progress",
        task_id="backgammon",
        injected_memory=[],
    )

    assert result.verdict == "PASS"

    phases = [entry["phase"] for entry in calls]
    # 3 calls: initial + feedback-1 (no pass verdict because gates never improve) + feedback-2
    assert phases == ["initial", "feedback-1", "feedback-2"]
    
    # Verify no pass verdict was injected (zero progress = no newly passing gates)
    for phase in phases:
        assert not phase.startswith("verdict-pass")

    sidecar_path = Path(f"{result.worktree}.user-events.jsonl")
    sidecar_rows = [json.loads(line) for line in sidecar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # 3 rows: initial + feedback-1 + feedback-2
    assert len(sidecar_rows) == 3
    assert sidecar_rows[0]["text"] == "INITIAL PROMPT"


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
    assert result.attempt_reports[-1]["parity_pending"] is True
    assert all(ar["parity_pending"] is True for ar in result.attempt_reports)


def test_harness_limit_kill_does_not_force_budget_stop_and_loop_can_continue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=2)
    docker_state = _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_load_chunk_prompts", lambda *, injected_memory: ["PROMPT"])

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

    monkeypatch.setattr(runner, "_run_opencode_with_zero_tool_resumes", _fake_opencode)

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


@pytest.mark.slow
def test_non_budget_nonzero_worker_exit_classifies_as_harness_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None, max_attempts=2)
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_load_chunk_prompts", lambda *, injected_memory: ["PROMPT"])
    monkeypatch.setattr(
        runner,
        "_run_opencode_with_zero_tool_resumes",
        lambda **kwargs: _stats(
            session_id="sess-1",
            killed_reason=None,
            exit_code=1,
            cost_usd=0.0,
            budget_stop_detected=False,
            terminal_zero_tool_turn=False,
        ),
    )
    monkeypatch.setattr(
        runner,
        "_run_gate_report",
        lambda **kwargs: {
            "verdict": "FAIL",
            "conformed": True,
            "problems": [{"check": "x"}],
            "failed_gates": ["x"],
        },
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
