"""WO-WATCH-1E: hermetic unit tests for BackgammonRunner._run_opencode_serve.

No live server, no docker, no model. A fake ``ServeClient`` stub drives the
serve-drive path and the returned ``_OpencodeRunStats`` is asserted field by
field. ``active_cell`` is a lightweight stand-in exposing ``kill_worker_processes``
(the only ``DockerCell`` surface ``_run_opencode_serve`` touches).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wevibe_bench.adapters.backgammon import (
    BackgammonRunner,
    TURN_TERMINAL_TRANSPORT_ERROR,
    TURN_TERMINAL_TRUNCATED,
)
from wevibe_bench.serve_client import ServeClientError


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


class _FakeCell:
    def __init__(self) -> None:
        self.kill_calls = 0

    def kill_worker_processes(self) -> None:
        self.kill_calls += 1


class _FakeServeClient:
    """Stub that records calls and serves canned responses."""

    def __init__(self) -> None:
        self.sent_prompts: list[tuple[str, str]] = []
        self.wait_result: bool = True
        self.wait_timeout_s: float | None = None
        self.metrics_result: dict[str, Any] | None = None
        self.send_error: Exception | None = None
        self.metrics_error: Exception | None = None

    def send_prompt(self, session_id: str, prompt: str) -> None:
        self.sent_prompts.append((session_id, prompt))
        if self.send_error is not None:
            raise self.send_error

    def wait_idle(self, session_id: str, *, timeout_s: float) -> bool:
        self.wait_timeout_s = timeout_s
        return self.wait_result

    def metrics(self, session_id: str) -> dict[str, Any]:
        if self.metrics_error is not None:
            raise self.metrics_error
        return self.metrics_result


def _make_runner(tmp_path: Path) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="orcarouter/kimi/kimi-k3",
        memory_mode="off",
        run_timeout_s=30,
        completion_grace_s=2,
    )


def test_serve_drive_happy_path(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_result = {
        "turns": 4,
        "input_tokens": 100,
        "output_tokens": 50,
        "reasoning_tokens": 25,
        "cost_usd": 0.012,
        "truncations": 0,
        "error_parts": 0,
    }
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_live_1",
        prompt="build the game",
        run_label="cell-1",
        phase="initial",
        timeout_s=123.0,
    )

    assert client.sent_prompts == [("ses_live_1", "build the game")]
    assert client.wait_timeout_s == 123.0
    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.session_id == "ses_live_1"
    assert stats.turns == 4
    assert stats.input_tokens == 100
    assert stats.output_tokens == 50
    assert stats.reasoning_tokens == 25
    assert stats.cost_usd == 0.012
    assert stats.truncations == 0
    assert stats.turn_anomalies == ()
    assert stats.zero_tool_turn_honest_fail is False
    assert stats.resume_count == 0
    assert stats.unmetered_turns == 0
    assert stats.unmetered_turn_wall_s == 0.0
    assert stats.budget_stop_detected is False
    assert cell.kill_calls == 0


def test_serve_drive_timeout_calls_kill_hook(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.wait_result = False
    client.metrics_result = {
        "turns": 2,
        "input_tokens": 60,
        "output_tokens": 30,
        "reasoning_tokens": 10,
        "cost_usd": 0.0,
        "truncations": 0,
        "error_parts": 0,
    }
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_timeout",
        prompt="p",
        run_label="cell-2",
        phase="initial",
        timeout_s=10.0,
    )

    assert cell.kill_calls == 1
    assert stats.killed_reason == "run_timeout"
    assert stats.exit_code == 1
    assert stats.turns == 2


def test_serve_drive_send_error_returns_exit1_no_raise(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.send_error = ServeClientError("POST ... failed: boom")
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_senderr",
        prompt="p",
        run_label="cell-3",
        phase="initial",
    )

    assert stats.exit_code == 1
    assert stats.killed_reason is None
    assert stats.turns == 0
    assert stats.input_tokens == 0
    assert stats.output_tokens == 0
    assert stats.cost_usd == 0.0
    assert cell.kill_calls == 0


def test_serve_drive_truncation_produces_anomaly(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_result = {
        "turns": 3,
        "input_tokens": 90,
        "output_tokens": 40,
        "reasoning_tokens": 15,
        "cost_usd": 0.0,
        "truncations": 1,
        "error_parts": 0,
    }
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_trunc",
        prompt="p",
        run_label="cell-4",
        phase="initial",
    )

    assert stats.exit_code == 0
    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_TRUNCATED
    assert anomaly["reason"] == "stream-incomplete"
    assert anomaly["session_id"] == "ses_trunc"
    assert anomaly["turn_index"] == 3
    assert anomaly["phase"] == "initial"
    assert anomaly["tool_uses"] == 0
    assert anomaly["file_writes"] == 0


def test_serve_drive_transport_error_produces_anomaly(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_result = {
        "turns": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "truncations": 0,
        "error_parts": 1,
    }
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_tperr",
        prompt="p",
        run_label="cell-5",
        phase="initial",
    )

    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_TRANSPORT_ERROR
    assert anomaly["reason"] == "error_event"


def test_serve_drive_metrics_error_sets_exit1(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_error = ServeClientError("GET ... failed: boom")
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_merr",
        prompt="p",
        run_label="cell-6",
        phase="initial",
    )

    assert stats.exit_code == 1
    assert stats.killed_reason is None
    assert stats.turns == 0
    assert cell.kill_calls == 0