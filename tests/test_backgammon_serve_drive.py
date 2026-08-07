"""WO-WATCH-1E: hermetic unit tests for BackgammonRunner._run_opencode_serve.

No live server, no docker, no model. A fake ``ServeClient`` stub drives the
serve-drive path and the returned ``_OpencodeRunStats`` is asserted field by
field. ``active_cell`` is a lightweight stand-in exposing ``kill_worker_processes``
(the only ``DockerCell`` surface ``_run_opencode_serve`` touches).
"""

from __future__ import annotations

import json
import tempfile
import uuid
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
        self.aborted_sessions: list[str] = []
        self.abort_error: Exception | None = None
        self.wait_result: bool = True
        self.wait_timeout_s: float | None = None
        self.metrics_result: dict[str, Any] | None = None
        self.send_error: Exception | None = None
        self.metrics_error: Exception | None = None

    def send_prompt(self, session_id: str, prompt: str) -> None:
        self.sent_prompts.append((session_id, prompt))
        if self.send_error is not None:
            raise self.send_error

    def abort(self, session_id: str) -> None:
        self.aborted_sessions.append(session_id)
        if self.abort_error is not None:
            raise self.abort_error

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
    assert client.aborted_sessions == ["ses_timeout"]
    assert stats.killed_reason == "run_timeout"
    assert stats.exit_code == 1
    assert stats.turns == 2


def test_serve_drive_timeout_abort_failure_still_timeout(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.wait_result = False
    client.abort_error = ServeClientError("POST .../abort failed: boom")
    client.metrics_result = {
        "turns": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "truncations": 0,
        "error_parts": 0,
    }
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_timeout_abortfail",
        prompt="p",
        run_label="cell-7",
        phase="initial",
        timeout_s=10.0,
    )

    assert client.aborted_sessions == ["ses_timeout_abortfail"]
    assert cell.kill_calls == 1
    assert stats.killed_reason == "run_timeout"
    assert stats.exit_code == 1
    assert stats.turns == 1


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


def _make_feedback_attempt_kwargs(
    *,
    feedback_text: str,
    phase: str,
    events_path: Path,
    kill_hook: Any,
) -> dict[str, Any]:
    """Assemble the dispatch kwargs for ``_run_cell_attempt`` in serve mode."""
    return {
        "active_cell": _FakeCell(),
        "initial_inner": ["opencode", "run", "--session", "sess", "--dir", "/work", "--format", "json"],
        "pure": False,
        "worktree": events_path.parent,
        "events_path": events_path,
        "env": {},
        "run_label": "cell-fb",
        "phase": phase,
        "fallback_session_id": "sess",
        "prior_cost_usd": 0.0,
        "kill_hook": kill_hook,
        "stdin_text": feedback_text,
    }


def test_run_cell_attempt_serve_driven_feedback_delivered_via_prompt_async(
    tmp_path: Path,
) -> None:
    """WO-WATCH-1F: when a serve session is available, the FEEDBACK attempt is
    delivered over serve via ``send_prompt`` (prompt_async) to the persisted
    cell session id, completion from wait_idle, metering from the transcript,
    with the serve-driven phase ``feedback-N``."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_result = {
        "turns": 2,
        "input_tokens": 40,
        "output_tokens": 20,
        "reasoning_tokens": 5,
        "cost_usd": 0.003,
        "truncations": 0,
        "error_parts": 0,
    }
    cell = _FakeCell()
    # Inject the serve session exactly as `_run_cell_impl` persists it.
    runner._serve_client = client
    runner._cell_session_id = "ses_cell_fb"
    events_path = tmp_path / "fb.events.jsonl"

    feedback = "fix [G02] B — do not explain, just edit."
    kwargs = _make_feedback_attempt_kwargs(
        feedback_text=feedback,
        phase="feedback-1",
        events_path=events_path,
        kill_hook=cell.kill_worker_processes,
    )
    stats = runner._run_cell_attempt(**kwargs)

    # Delivered to the persistent cell session id, never the container-side id.
    assert client.sent_prompts == [("ses_cell_fb", feedback)]
    assert stats.session_id == "ses_cell_fb"
    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.turns == 2
    assert stats.input_tokens == 40
    assert stats.output_tokens == 20
    assert stats.reasoning_tokens == 5
    assert stats.cost_usd == 0.003
    # Serve-driven attempt cannot detect a zero-tool turn (transcript only).
    assert stats.zero_tool_turn_honest_fail is False
    assert stats.terminal_zero_tool_turn is False
    assert stats.zero_tool_resumes == 0
    assert stats.resume_count == 0
    assert cell.kill_calls == 0


def test_run_cell_attempt_serve_driven_pass_verdict_delivered_via_prompt_async(
    tmp_path: Path,
) -> None:
    """WO-WATCH-1F: the PASS-VERDICT attempt is delivered over serve with phase
    ``verdict-pass-N`` via prompt_async to the cell session id."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_result = {
        "turns": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "truncations": 0,
        "error_parts": 0,
    }
    cell = _FakeCell()
    runner._serve_client = client
    runner._cell_session_id = "ses_cell_pv"
    events_path = tmp_path / "pv.events.jsonl"

    pass_verdict = "That fixed it — [G01] A, [G03] C all pass now."
    kwargs = _make_feedback_attempt_kwargs(
        feedback_text=pass_verdict,
        phase="verdict-pass-2",
        events_path=events_path,
        kill_hook=cell.kill_worker_processes,
    )
    stats = runner._run_cell_attempt(**kwargs)

    assert client.sent_prompts == [("ses_cell_pv", pass_verdict)]
    assert stats.session_id == "ses_cell_pv"
    assert stats.exit_code == 0
    assert stats.turns == 1


def test_run_cell_attempt_serve_send_error_returns_exit1_no_stdout_rerun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """WO-WATCH-1F: a send error over serve is caught internally by
    ``_run_opencode_serve`` (returns exit_code=1 stats, never raises), so the
    stdout path is NOT re-run (the transport decision is left to the caller)."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.send_error = ServeClientError("POST ... prompt_async failed: boom")
    cell = _FakeCell()
    runner._serve_client = client
    runner._cell_session_id = "ses_cell_fb_err"
    events_path = tmp_path / "fb-err.events.jsonl"

    fallback_calls: list[dict[str, Any]] = []

    def _fake_zero_tool(**kwargs: Any) -> Any:
        fallback_calls.append(kwargs)
        from wevibe_bench.adapters.backgammon import _OpencodeRunStats

        return _OpencodeRunStats(
            input_tokens=1,
            output_tokens=1,
            reasoning_tokens=0,
            turns=1,
            session_id="sess-stdout",
            killed_reason=None,
            exit_code=0,
            cost_usd=0.0,
        )

    monkeypatch.setattr(runner, "_run_opencode_with_zero_tool_resumes", _fake_zero_tool)

    feedback = "fix the gates"
    kwargs = _make_feedback_attempt_kwargs(
        feedback_text=feedback,
        phase="feedback-1",
        events_path=events_path,
        kill_hook=cell.kill_worker_processes,
    )
    stats = runner._run_cell_attempt(**kwargs)

    # `_run_opencode_serve` catches the send error internally and returns
    # exit_code=1 stats (never raises), so the stdout fallback is NOT re-run:
    # the transport decision is left to the caller. Assert the serve outcome.
    assert client.sent_prompts == [("ses_cell_fb_err", feedback)]
    assert stats.exit_code == 1
    assert stats.killed_reason is None
    assert stats.turns == 0
    assert fallback_calls == []
    assert cell.kill_calls == 0


def test_run_cell_attempt_no_serve_session_uses_stdout_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """WO-WATCH-1F: when no serve session is available (fake cells whose
    create_session fails -> ``_cell_session_id is None``), the feedback attempt
    routes to the stdout subprocess path, unchanged (the zero-tool resume loop
    stays authoritative)."""
    runner = _make_runner(tmp_path)
    # No serve session: `_serve_client` may be set but `_cell_session_id` is None.
    runner._serve_client = _FakeServeClient()
    runner._cell_session_id = None
    events_path = tmp_path / "fb-no-serve.events.jsonl"

    fallback_calls: list[dict[str, Any]] = []

    def _fake_zero_tool(**kwargs: Any) -> Any:
        fallback_calls.append(kwargs)
        from wevibe_bench.adapters.backgammon import _OpencodeRunStats

        return _OpencodeRunStats(
            input_tokens=1,
            output_tokens=1,
            reasoning_tokens=0,
            turns=1,
            session_id="sess-stdout",
            killed_reason=None,
            exit_code=0,
            cost_usd=0.0,
        )

    monkeypatch.setattr(runner, "_run_opencode_with_zero_tool_resumes", _fake_zero_tool)

    feedback = "fix the gates"
    kwargs = _make_feedback_attempt_kwargs(
        feedback_text=feedback,
        phase="feedback-2",
        events_path=events_path,
        kill_hook=_FakeCell().kill_worker_processes,
    )
    stats = runner._run_cell_attempt(**kwargs)

    assert len(fallback_calls) == 1
    assert fallback_calls[0]["phase"] == "feedback-2"
    assert fallback_calls[0]["stdin_text"] == feedback
    assert fallback_calls[0]["fallback_session_id"] == "sess"
    assert stats.session_id == "sess-stdout"
    assert stats.exit_code == 0


def test_run_cell_attempt_serve_driven_resume_truncation_writes_evidence(
    tmp_path: Path,
) -> None:
    """WO-WATCH-1F ITEM 2 (b): the truncation capture fires on a RESUME attempt
    as it does on an initial one — proven by a deliberately induced truncation
    on a RESUME (serve-drive phase ``feedback-1``, attempt 2), not by
    inspection. The serve-path evidence write is gated on
    ``classify_transport_anomaly`` returning a terminal; here ``truncations: 1``
    forces ``(TERMINAL_TRUNCATED, "stream-incomplete")``, so the anomaly entry
    AND the truncation-evidence.jsonl record must both be produced. A hermetic
    ``_FakeCell`` has no ``.config`` worktree, so the evidence falls back to
    the system temp dir."""
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
    # Unique per-run cell session id (uuid) + run_label keep the evidence line
    # unambiguous in the shared, ever-appending temp file across test runs.
    cell_session_id = f"ses_cell_resume_trunc_{uuid.uuid4().hex[:8]}"
    runner._serve_client = client
    runner._cell_session_id = cell_session_id
    events_path = tmp_path / "fb-resume-trunc.events.jsonl"

    feedback = "resume: the truncated turn needs a corrective edit."
    kwargs = _make_feedback_attempt_kwargs(
        feedback_text=feedback,
        phase="feedback-1",
        events_path=events_path,
        kill_hook=cell.kill_worker_processes,
    )
    kwargs["run_label"] = "cell-resume-trunc"
    stats = runner._run_cell_attempt(**kwargs)

    # 1) The stats carry the truncated anomaly for the RESUME phase.
    assert stats.session_id == cell_session_id
    assert stats.exit_code == 0
    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_TRUNCATED
    assert anomaly["reason"] == "stream-incomplete"
    assert anomaly["phase"] == "feedback-1"
    assert anomaly["session_id"] == cell_session_id

    # 2) The truncation evidence capture FIRED: a record for THIS resume attempt
    #    was appended to the temp-dir evidence file.
    evidence_path = Path(tempfile.gettempdir()) / "truncation-evidence.jsonl"
    assert evidence_path.exists()
    lines = [
        json.loads(ln)
        for ln in evidence_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    matches = [
        rec
        for rec in lines
        if rec.get("phase") == "feedback-1"
        and rec.get("session_id") == cell_session_id
    ]
    assert len(matches) == 1, f"expected exactly one resume-trunc evidence record, got {len(matches)}"
    rec = matches[0]
    assert rec["terminal"] == TURN_TERMINAL_TRUNCATED
    assert rec["reason"] == "stream-incomplete"
    assert rec["session_id"] == cell_session_id
    assert rec["run_label"] == "cell-resume-trunc"
    corr = rec["correlation"]
    ts_window = corr["ts_window_utc"]
    assert isinstance(ts_window, list) and len(ts_window) == 2
    assert all(isinstance(t, str) and t for t in ts_window)
    # First element may be None when ts_start is missing; end must be ISO UTC.
    assert ts_window[1].endswith("+00:00") or ts_window[1].endswith("Z")
    # match_key is "{run_label}|{attempt_id}|{session_id}" (the serve path always
    # carries a real attempt_id, so it is never "none").
    assert corr["match_key"].startswith("cell-resume-trunc|")
    assert "cell-resume-trunc" in corr["match_key"]
    assert corr["match_key"].endswith(f"|{cell_session_id}")
    assert cell.kill_calls == 0