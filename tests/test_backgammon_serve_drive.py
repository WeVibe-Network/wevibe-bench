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
    _FINALIZE_RECOVERY_NUDGE,
    _LOOP_RECOVERY_NUDGE,
    BackgammonRunner,
    TURN_TERMINAL_GUARD_ABORT,
    TURN_TERMINAL_OBSERVATION_LOST,
    TURN_TERMINAL_TRANSPORT_ERROR,
    TURN_TERMINAL_TRUNCATED,
    bench_session_title,
)
from wevibe_bench.serve_client import ServeClientError, extract_transcript_metrics


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
        self.busy_result: bool = True
        self.busy_grace_s: float | None = None
        self.metrics_result: dict[str, Any] | None = None
        self.metrics_script: list[dict[str, Any]] = []
        self.metrics_baseline: dict[str, Any] = {}
        self._baseline_served: bool = False
        self.assistant_texts: list[str | list[str]] = []
        # Per-drive terminal shapes (popped one per send_prompt, default None):
        # {"info_error": ...}  -> a relay-killed turn as opencode 1.18.x
        #                         persists it (bare step-start, no text, the
        #                         signature in info.error.data.message);
        # {"step_finish": r}   -> the appended assistant message also carries a
        #                         step-finish part with reason r (truncation);
        # {"error_part": ...}  -> the appended assistant message also carries
        #                         an error part (transport error_event).
        # The canned metrics_script drives the CUMULATIVE reads; the windowed
        # classification read (metrics(since=...)) is derived from _messages
        # through the REAL extractor, so anomaly surfaces must exist here.
        self.assistant_terminal_script: list[dict[str, str] | None] = []
        self.send_error: Exception | None = None
        self.metrics_error: Exception | None = None
        # Watermark-windowed marker scan support: a growable message list the
        # chunked loop reads via get_messages/assistant_texts_since. Each
        # send_prompt appends a user message plus one (or a batch of)
        # assistant messages, mirroring the real serve session.
        self._messages: list[dict[str, Any]] = []
        self.poll_interval: float = 0.0
        self.compaction_ready: bool = False
        self.summarize_calls: list[tuple[str, str, str, bool]] = []

    def send_prompt(self, session_id: str, prompt: str) -> None:
        self.sent_prompts.append((session_id, prompt))
        if self.send_error is not None:
            raise self.send_error
        self._messages.append(
            {
                "info": {
                    "role": "user",
                    "model": {
                        "providerID": "local-llm-proxy",
                        "modelID": "kimi/kimi-k3",
                    },
                },
                "parts": [{"type": "text", "text": prompt}],
            }
        )
        terminal = (
            self.assistant_terminal_script.pop(0)
            if self.assistant_terminal_script
            else None
        )
        if terminal and terminal.get("info_error"):
            # Killed turn: no scripted text is consumed — a real kill persists
            # no assistant text (opencode 1.18.x writes the error, not a part).
            self._messages.append(
                {
                    "info": {
                        "role": "assistant",
                        "error": {
                            "name": "UnknownError",
                            "data": {"message": terminal["info_error"]},
                        },
                    },
                    "parts": [{"type": "step-start"}],
                }
            )
            return
        scripted = self.assistant_texts.pop(0) if self.assistant_texts else "CHUNK FINISHED"
        batch = scripted if isinstance(scripted, list) else [scripted]
        for text in batch:
            parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
            if terminal and terminal.get("step_finish"):
                parts.append({"type": "step-finish", "reason": terminal["step_finish"]})
            if terminal and terminal.get("error_part"):
                parts.append({"type": "error", "message": terminal["error_part"]})
            self._messages.append(
                {
                    "info": {"role": "assistant"},
                    "parts": parts,
                }
            )

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._messages)

    def assistant_texts_since(self, session_id: str, watermark: int) -> list[str]:
        return [
            "".join(
                str(part.get("text") or "")
                for part in msg.get("parts", [])
                if isinstance(part, dict) and part.get("type") == "text"
            )
            for msg in self._messages[watermark:]
            if isinstance(msg, dict) and msg.get("info", {}).get("role") == "assistant"
        ]

    def compaction_since(self, session_id: str, watermark: int) -> bool:
        if self.compaction_ready:
            return True
        return any(
            isinstance(part, dict) and part.get("type") == "compaction"
            for msg in self._messages[watermark:]
            if isinstance(msg, dict)
            for part in msg.get("parts", [])
        )

    def session_busy(self, session_id: str) -> bool:
        return False

    def session_model(self, session_id: str) -> tuple[str, str] | None:
        return ("local-llm-proxy", "kimi/kimi-k3")

    def summarize(
        self,
        session_id: str,
        *,
        provider_id: str,
        model_id: str,
        auto: bool = False,
        timeout_s: float = 1800.0,
    ) -> None:
        self.summarize_calls.append((session_id, provider_id, model_id, auto))
        self._messages.append(
            {
                "info": {"role": "user"},
                "parts": [{"type": "compaction"}],
            }
        )

    def abort(self, session_id: str) -> None:
        self.aborted_sessions.append(session_id)
        if self.abort_error is not None:
            raise self.abort_error

    def wait_idle(self, session_id: str, *, timeout_s: float) -> bool:
        self.wait_timeout_s = timeout_s
        return self.wait_result

    def wait_busy(self, session_id: str, *, timeout_s: float) -> bool:
        self.busy_grace_s = timeout_s
        return self.busy_result

    def metrics(self, session_id: str, *, since: int | None = None) -> dict[str, Any]:
        if since is not None:
            # The windowed classification read NEVER consults the canned
            # script: it is derived from the fake transcript through the real
            # extractor, so a persisted kill stays visible (and windowed-out)
            # exactly as the opencode serve transcript behaves.
            return extract_transcript_metrics(self._messages[since:])
        if self.metrics_error is not None:
            raise self.metrics_error
        if self.metrics_script:
            return self.metrics_script.pop(0)
        # The serve-drive captures a baseline BEFORE sending the prompt and
        # meters deltas against it; the default baseline is an empty session
        # (all zeros), so single-phase tests keep absolute-value assertions.
        if not self._baseline_served:
            self._baseline_served = True
            return self.metrics_baseline
        return self.metrics_result

    def last_assistant_text(self, session_id: str) -> str:
        if self.assistant_texts:
            return self.assistant_texts.pop(0)
        return "CHUNK FINISHED"


def _make_runner(tmp_path: Path) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="local-llm-proxy/kimi/kimi-k3",
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


def test_serve_drive_never_busy_is_loud_exit1_not_clean_zero(tmp_path: Path) -> None:
    """A prompt the serve never picks up must NOT meter as a clean 0-turn ok.

    Regression guard for the 2026-08-09 void: prompt_async is fire-and-forget
    and a bare wait_idle raced the serve's busy flag, returning a false idle
    in milliseconds — turns=0/input=0/output=0 while gates ran against a
    worktree the model was still writing. The drive must first confirm busy
    (wait_busy) and treat never-busy-with-empty-transcript as a loud exit 1.
    """
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.busy_result = False
    client.metrics_result = {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "truncations": 0,
        "error_parts": 0,
    }
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_neverbusy",
        prompt="p",
        run_label="cell-nb",
        phase="initial",
    )

    assert stats.exit_code == 1
    assert stats.killed_reason is None
    assert stats.turns == 0
    # Never went busy => the idle wait and the abort/kill path never ran.
    assert client.wait_timeout_s is None
    assert client.aborted_sessions == []
    assert cell.kill_calls == 0


def test_serve_drive_busy_window_raced_turn_is_metered_not_voided(tmp_path: Path) -> None:
    """A turn that completes entirely inside the busy-grace window is metered.

    If wait_busy never observes busy but the transcript already carries turns,
    the work is real — meter it, never void it.
    """
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.busy_result = False
    client.metrics_result = {
        "turns": 2,
        "input_tokens": 40,
        "output_tokens": 20,
        "reasoning_tokens": 5,
        "cost_usd": 0.0,
        "truncations": 0,
        "error_parts": 0,
    }
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_raced",
        prompt="p",
        run_label="cell-race",
        phase="initial",
    )

    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.turns == 2
    assert stats.input_tokens == 40
    assert stats.output_tokens == 20
    assert cell.kill_calls == 0


def test_serve_drive_truncation_produces_anomaly(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [{"step_finish": "stream-incomplete"}]
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
    client.assistant_terminal_script = [{"error_part": "relay: stream boom"}]
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


def test_serve_drive_lost_observation_is_recorded_never_silently_clean(
    tmp_path: Path,
) -> None:
    """D-SERVE-MESSAGE-500: a blind phase must declare itself, not read clean.

    When the transcript read fails past serve_client's transient retries, the
    classification window is empty — so ``classify_transport_anomaly`` returns
    (None, None) and, before this fix, the phase fell through recording NO
    anomaly at all. The cell then ran gates against a worktree nobody had
    observed and reported 43 "problems" as if they were a capability result
    (the 2026-08-11 void). The phase must instead carry an explicit
    observation_lost terminal so the cell is gated VOID-INSTRUMENT.
    """
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_error = ServeClientError(
        "GET /session/ses_x/message failed: HTTP Error 500: Internal Server Error"
    )
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_obs",
        prompt="p",
        run_label="cell-obs",
        phase="initial-chunk-4",
    )

    assert stats.exit_code == 1
    assert stats.observation_lost_turns == 1, "the blind phase must be counted"
    terminals = [a["terminal"] for a in stats.turn_anomalies]
    assert TURN_TERMINAL_OBSERVATION_LOST in terminals, (
        "a phase the harness could not observe must never look like a clean phase"
    )
    lost = next(
        a
        for a in stats.turn_anomalies
        if a["terminal"] == TURN_TERMINAL_OBSERVATION_LOST
    )
    assert lost["tokens_unmetered"] is True, "unobserved tokens are not metered truth"


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
    client.assistant_terminal_script = [{"step_finish": "stream-incomplete"}]
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

@pytest.fixture(autouse=True)
def _no_compaction_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunked-loop tests never wait out the real self-compaction grace window."""
    monkeypatch.setenv("WEVIBE_BENCH_COMPACT_GRACE_S", "0")


_ZERO_METRICS = {
    "turns": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "reasoning_tokens": 0,
    "cost_usd": 0.0,
    "truncations": 0,
    "error_parts": 0,
}


def _metrics(
    turns: int,
    inp: int,
    out: int,
    guard_aborted: int = 0,
    finalize: int = 0,
) -> dict[str, Any]:
    # Session-CUMULATIVE read: the kill counts persist in the transcript, so a
    # post-recovery read carries them forward (a real session never forgets).
    return {
        "turns": turns,
        "input_tokens": inp,
        "output_tokens": out,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "truncations": 0,
        "error_parts": 0,
        "guard_aborted_turns": guard_aborted,
        "finalize_timeouts": finalize,
    }


def test_chunked_pass_sends_all_chunks_in_order_and_meters_deltas(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    # Per chunk: baseline (pre-send) then end-of-phase metrics. Baseline for
    # chunk 2 is the cumulative after chunk 1 (session metrics are cumulative).
    client.metrics_script = [
        dict(_ZERO_METRICS),          # chunk-1 baseline
        _metrics(2, 10, 5),           # chunk-1 end
        _metrics(2, 10, 5),           # chunk-2 baseline
        _metrics(5, 40, 15),          # chunk-2 end
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve_chunked(
        active_cell=cell,
        serve_client=client,
        session_id="ses_chunks",
        prompts=["CHUNK ONE", "CHUNK TWO"],
        run_label="cell-chunks",
    )

    assert [text for _, text in client.sent_prompts] == ["CHUNK ONE", "CHUNK TWO"]
    assert stats.exit_code == 0
    assert stats.turns == 5            # 2 + 3, deltas summed across chunks
    assert stats.input_tokens == 40    # 10 + 30
    assert stats.output_tokens == 15   # 5 + 10
    assert len(stats.chunk_reports) == 2
    assert all(r["marker"] for r in stats.chunk_reports)
    assert all(not r["nudged"] for r in stats.chunk_reports)
    # Inter-chunk compaction: backstop fired once (after chunk 1 only — there
    # is no chunk to protect after the last), with the session's own model.
    assert client.summarize_calls == [
        ("ses_chunks", "local-llm-proxy", "kimi/kimi-k3", False)
    ]
    assert stats.chunk_reports[0]["compaction"] == "backstop"
    assert stats.chunk_reports[1]["compaction"] is None


def test_chunked_pass_marker_missing_nudges_until_the_marker_lands(tmp_path: Path) -> None:
    """WO-NUDGE-INF-1 (Walter 2026-08-11): a missing chunk marker is a stall,
    not a verdict. The harness re-nudges with the chunking reminder past the
    old budget of 3 until the marker lands, and the attempt is never failed
    for having needed nudges."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_script = [
        dict(_ZERO_METRICS), _metrics(1, 5, 2),   # chunk-1 baseline + end
        _metrics(1, 5, 2), _metrics(2, 6, 3),     # nudge-1 baseline + end
        _metrics(2, 6, 3), _metrics(3, 7, 4),     # nudge-2 baseline + end
        _metrics(3, 7, 4), _metrics(4, 8, 5),     # nudge-3 baseline + end
        _metrics(4, 8, 5), _metrics(5, 9, 6),     # nudge-4 (past old budget)
        _metrics(5, 9, 6), _metrics(6, 10, 7),    # chunk-2 baseline + end
    ]
    client.assistant_texts = [
        "done but no marker",
        "still no marker",
        "no marker again",
        "refuses to mark",
        "CHUNK FINISHED",      # lands on the 4th nudge
        "CHUNK FINISHED",      # chunk 2
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve_chunked(
        active_cell=cell,
        serve_client=client,
        session_id="ses_nomarker",
        prompts=["CHUNK ONE", "CHUNK TWO"],
        run_label="cell-nomarker",
    )

    # Chunk 1 + four nudges (one past the old budget) + chunk 2 — no failure.
    sent = [text for _, text in client.sent_prompts]
    assert sent[0] == "CHUNK ONE"
    assert "CHUNK TWO" in sent
    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.chunk_reports[0]["nudges"] == 4
    assert stats.chunk_reports[0]["marker"] is True
    assert all(r["marker"] for r in stats.chunk_reports)


def test_chunked_pass_marker_detected_before_or_after_discovery_block(
    tmp_path: Path,
) -> None:
    """The durability fix: the worker may emit CHUNK FINISHED and its
    WEVIBE_DISCOVERY block in either order across SEPARATE assistant
    messages. The watermark-windowed scan must catch both orders without
    nudging."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_script = [
        dict(_ZERO_METRICS), _metrics(1, 5, 2),   # chunk-1 baseline + end
        _metrics(1, 5, 2), _metrics(2, 9, 4),     # chunk-2 baseline + end
    ]
    client.assistant_texts = [
        ["CHUNK FINISHED", "WEVIBE_DISCOVERY: scorer maps to pure functions"],
        ["WEVIBE_DISCOVERY: redis pub/sub fan-out", "done. CHUNK FINISHED"],
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve_chunked(
        active_cell=cell,
        serve_client=client,
        session_id="ses_discovery",
        prompts=["CHUNK ONE", "CHUNK TWO"],
        run_label="cell-discovery",
    )

    assert stats.exit_code == 0
    assert [text for _, text in client.sent_prompts] == ["CHUNK ONE", "CHUNK TWO"]
    assert all(r["marker"] for r in stats.chunk_reports)
    assert all(r["nudges"] == 0 for r in stats.chunk_reports)


def test_chunked_pass_prior_chunk_marker_never_satisfies_later_chunk(
    tmp_path: Path,
) -> None:
    """The watermark isolates each chunk: chunk 1's marker is in the
    transcript forever, but chunk 2 must produce its OWN marker — chunk 1's
    marker never satisfies it, so chunk 2 is nudged until it marks itself."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_script = [
        dict(_ZERO_METRICS), _metrics(1, 5, 2),   # chunk-1 baseline + end
        _metrics(1, 5, 2), _metrics(2, 7, 3),     # chunk-2 baseline + end
        _metrics(2, 7, 3), _metrics(3, 9, 4),     # nudge-1 baseline + end
        _metrics(3, 9, 4), _metrics(4, 11, 5),    # nudge-2 baseline + end
    ]
    client.assistant_texts = [
        "scaffold done. CHUNK FINISHED",
        "no marker from chunk two",
        "still no marker",
        "ok now: CHUNK FINISHED",
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve_chunked(
        active_cell=cell,
        serve_client=client,
        session_id="ses_prior",
        prompts=["CHUNK ONE", "CHUNK TWO"],
        run_label="cell-prior",
    )

    # Chunk 1's marker did NOT satisfy chunk 2: chunk 2 needed two nudges of
    # its own before its own marker landed.
    assert stats.exit_code == 0
    assert stats.chunk_reports[0]["marker"] is True
    assert stats.chunk_reports[1]["nudges"] == 2
    assert stats.chunk_reports[1]["marker"] is True


def test_chunked_pass_self_fired_compaction_skips_backstop(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_script = [
        dict(_ZERO_METRICS), _metrics(1, 5, 2),   # chunk-1 baseline + end
        _metrics(1, 5, 2), _metrics(2, 9, 4),     # chunk-2 baseline + end
    ]
    client.compaction_ready = True   # the worker's armed self_compact fired
    cell = _FakeCell()

    stats = runner._run_opencode_serve_chunked(
        active_cell=cell,
        serve_client=client,
        session_id="ses_selfcompact",
        prompts=["CHUNK ONE", "CHUNK TWO"],
        run_label="cell-selfcompact",
    )

    assert stats.exit_code == 0
    assert client.summarize_calls == []
    assert stats.chunk_reports[0]["compaction"] == "self"
    assert stats.chunk_reports[1]["compaction"] is None


def test_chunked_pass_compaction_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEVIBE_BENCH_CHUNK_COMPACT", "0")
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_script = [
        dict(_ZERO_METRICS), _metrics(1, 5, 2),   # chunk-1 baseline + end
        _metrics(1, 5, 2), _metrics(2, 9, 4),     # chunk-2 baseline + end
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve_chunked(
        active_cell=cell,
        serve_client=client,
        session_id="ses_nocompact",
        prompts=["CHUNK ONE", "CHUNK TWO"],
        run_label="cell-nocompact",
    )

    assert stats.exit_code == 0
    assert client.summarize_calls == []
    assert all(r["compaction"] is None for r in stats.chunk_reports)


def test_attempt_boundary_compaction_fires_backstop(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()

    outcome = runner._compact_attempt_boundary(
        serve_client=client,
        session_id="ses_boundary",
        run_label="cell-boundary",
        attempt=1,
    )

    assert outcome == "backstop"
    assert client.summarize_calls == [
        ("ses_boundary", "local-llm-proxy", "kimi/kimi-k3", False)
    ]


def test_attempt_boundary_compaction_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEVIBE_BENCH_CHUNK_COMPACT", "0")
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()

    outcome = runner._compact_attempt_boundary(
        serve_client=client,
        session_id="ses_boundary",
        run_label="cell-boundary",
        attempt=1,
    )

    assert outcome == "disabled"
    assert client.summarize_calls == []


def test_attempt_boundary_compaction_skips_without_serve(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()

    assert (
        runner._compact_attempt_boundary(
            serve_client=None,
            session_id="ses_boundary",
            run_label="cell-boundary",
            attempt=1,
        )
        == "skipped_no_serve"
    )
    assert (
        runner._compact_attempt_boundary(
            serve_client=client,
            session_id=None,
            run_label="cell-boundary",
            attempt=1,
        )
        == "skipped_no_serve"
    )
    assert client.summarize_calls == []


def test_chunked_pass_nudge_recovers_and_advances(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.metrics_script = [
        dict(_ZERO_METRICS), _metrics(1, 5, 2),   # chunk-1 baseline + end
        _metrics(1, 5, 2), _metrics(1, 6, 2),     # nudge baseline + end
        _metrics(1, 6, 2), _metrics(3, 9, 6),     # chunk-2 baseline + end
    ]
    client.assistant_texts = ["no marker here"]   # nudge + chunk-2 use default marker text
    cell = _FakeCell()

    stats = runner._run_opencode_serve_chunked(
        active_cell=cell,
        serve_client=client,
        session_id="ses_recover",
        prompts=["CHUNK ONE", "CHUNK TWO"],
        run_label="cell-recover",
    )

    assert [text for _, text in client.sent_prompts][:1] == ["CHUNK ONE"]
    assert [text for _, text in client.sent_prompts][-1] == "CHUNK TWO"
    assert len(client.sent_prompts) == 3          # chunk1 + nudge + chunk2
    assert stats.exit_code == 0
    assert stats.chunk_reports[0]["nudged"] is True
    assert stats.chunk_reports[0]["nudges"] == 1
    assert stats.chunk_reports[0]["marker"] is True
    assert stats.chunk_reports[0]["compaction"] == "backstop"
    assert stats.chunk_reports[1]["nudged"] is False


def test_serve_drive_zero_delta_phase_is_loud_not_clean_zero(tmp_path: Path) -> None:
    """A phase that ends with the SAME cumulative metrics as its baseline
    produced nothing (discarded message / dead stream) — loud exit 1 with a
    silent_phase anomaly, never a clean zero-turn ok (2026-08-09 feedback void).
    """
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    stale = _metrics(6, 23354, 24822)
    client.metrics_baseline = dict(stale)
    client.metrics_result = dict(stale)
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_stale",
        prompt="fix the 47 problems",
        run_label="cell-stale",
        phase="feedback-1",
    )

    assert stats.exit_code == 1
    assert stats.turns == 0
    assert stats.input_tokens == 0
    assert any(a["terminal"] == "silent_phase" for a in stats.turn_anomalies)
    assert cell.kill_calls == 0


# ---------------------------------------------------------------------------
# WO-LOOPREC-1: loop-guard recovery on the serve path
# ---------------------------------------------------------------------------
def test_chunk_prompts_carry_the_write_chunking_directive() -> None:
    """Walter 2026-08-10: the finalize kills were oversized single generations
    (whole-file writes); every chunk prompt must carry the write-in-chunks
    directive AND the marker instruction — a prompt edit that drops either
    re-opens the stream_finalize_exhausted cell death."""
    for index in range(1, 7):
        text = (TASK_DIR / "prompts" / f"chunk-0{index}.md").read_text(encoding="utf-8")
        assert "~150 lines" in text, f"chunk-0{index}.md lost the chunking directive"
        assert "CHUNK FINISHED" in text, f"chunk-0{index}.md lost the marker instruction"


_LOOP_SIG = "relay: generation loop detected (<request-id>)"
_FIN_SIG = (
    "relay: upstream completed but the stream did not finalize "
    "within 30000ms (<request-id>)"
)

_LOOP_METRICS = {
    # The guard-killed read as the persisted transcript reports it: the relay
    # loop signature survives in info.error text (opencode 1.18.x writes NO
    # error part), and the looped turn's tokens are metered.
    "turns": 5,
    "input_tokens": 100,
    "output_tokens": 40,
    "reasoning_tokens": 10,
    "cost_usd": 0.0,
    "truncations": 0,
    "error_parts": 0,
    "info_errors": 1,
    "guard_aborted_turns": 1,
    "finalize_timeouts": 0,
    "error_texts": [
        # Live-observed shape (2026-08-10 runs); per-request trace id elided.
        _LOOP_SIG
    ],
}

_FINALIZE_METRICS = {
    # The relay 30s stream-finalize watchdog kill (WO-FINALIZE-REC-1): the
    # turn's tokens burned, the kill text lands in info.error, and the turn is
    # EXCLUDED from scoring turns (WO-NUDGE-INF-1 — same treatment as a guard
    # kill, so unbounded recovery cannot inflate the measurement).
    "turns": 5,
    "input_tokens": 100,
    "output_tokens": 40,
    "reasoning_tokens": 10,
    "cost_usd": 0.0,
    "truncations": 0,
    "error_parts": 0,
    "info_errors": 1,
    "guard_aborted_turns": 0,
    "finalize_timeouts": 1,
    "error_texts": [
        _FIN_SIG
    ],
}


def test_serve_drive_loop_guard_kill_recovers_with_anti_repetition_nudge(
    tmp_path: Path,
) -> None:
    """The 2026-08-10 defect, fixed: a loop kill on the serve path is classified
    guard_abort/loop_guard and re-driven with the anti-repetition nudge (never
    the original prompt) instead of counting the looped turn as completed work.
    """
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [{"info_error": _LOOP_SIG}]
    client.metrics_script = [
        dict(_ZERO_METRICS),   # phase baseline
        dict(_LOOP_METRICS),   # loop-killed read
        _metrics(8, 160, 70, guard_aborted=1),  # post-nudge read (session-cumulative)
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_loop",
        prompt="fix the gates",
        run_label="cell-loop",
        phase="feedback-1",
    )

    sent = [text for _, text in client.sent_prompts]
    assert sent == ["fix the gates", _LOOP_RECOVERY_NUDGE]
    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.recovery_nudges == 1
    # Looped turn + recovery turn both metered (delta vs baseline)…
    assert stats.output_tokens == 70
    # …but the guard-killed turn is EXCLUDED from scoring turns (WO-TURNACCT-1:
    # 8 metered - 1 guard-aborted), and the exclusion is carried, never silent.
    assert stats.turns == 7
    assert stats.guard_aborted_turns == 1
    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_GUARD_ABORT
    assert anomaly["reason"] == "loop_guard"
    # A harness-fired recovery is retry-linked — never retried:false.
    assert anomaly["retried"] is True
    assert anomaly["retry_kind"] == "harness_resume"
    assert cell.kill_calls == 0


def test_serve_drive_recovered_loop_kill_stale_error_not_reclassified(
    tmp_path: Path,
) -> None:
    """The 2026-08-10 live-cell defect: a guard-killed message's info.error
    persists in the transcript FOREVER, so every cumulative read after the
    kill still carries the signature (error_texts, info_errors,
    guard_aborted_turns). After a successful recovery nudge the phase MUST
    classify only the window produced since the nudge — re-reading the stale
    kill nudged a completed, CHUNK FINISHED-landing drive again and again
    until loop_guard_exhausted killed the cell."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [{"info_error": _LOOP_SIG}]
    stale_post_nudge = _metrics(8, 160, 70, guard_aborted=1)
    # The real post-recovery cumulative read: the kill's error text is STILL
    # there (the transcript never forgets), alongside the recovered work.
    stale_post_nudge.update(info_errors=1, error_texts=[_LOOP_SIG])
    client.metrics_script = [
        dict(_ZERO_METRICS),   # phase baseline
        dict(_LOOP_METRICS),   # loop-killed read
        stale_post_nudge,      # post-nudge read (stale error carried forward)
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_loop_stale",
        prompt="fix the gates",
        run_label="cell-loop-stale",
        phase="feedback-1",
    )

    sent = [text for _, text in client.sent_prompts]
    assert sent == ["fix the gates", _LOOP_RECOVERY_NUDGE]  # never a 3rd prompt
    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.recovery_nudges == 1
    assert stats.turns == 7
    assert stats.guard_aborted_turns == 1
    assert len(stats.turn_anomalies) == 1
    assert stats.turn_anomalies[0]["terminal"] == TURN_TERMINAL_GUARD_ABORT
    assert cell.kill_calls == 0


def test_serve_drive_stale_loop_error_from_prior_phase_not_reclassified(
    tmp_path: Path,
) -> None:
    """Cross-phase staleness: a kill classified and recovered in an earlier
    phase is still in the transcript when a LATER phase runs. The later
    phase's window starts at its own baseline, so the old kill cannot poison
    its classification."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [{"info_error": _LOOP_SIG}]
    stale_end = _metrics(8, 160, 70, guard_aborted=1)
    stale_end.update(info_errors=1, error_texts=[_LOOP_SIG])
    client.metrics_script = [
        dict(_ZERO_METRICS),   # phase-1 baseline
        dict(_LOOP_METRICS),   # phase-1 loop-killed read
        stale_end,             # phase-1 post-nudge read
        stale_end,             # phase-2 baseline (cumulative: unchanged)
        _metrics(11, 200, 95, guard_aborted=1),  # phase-2 end (stale too, via window)
    ]
    cell = _FakeCell()

    first = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_stale_phase",
        prompt="chunk one",
        run_label="cell-stale-phase",
        phase="initial-chunk-1",
    )
    second = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_stale_phase",
        prompt="chunk two",
        run_label="cell-stale-phase",
        phase="initial-chunk-2",
    )

    assert first.exit_code == 0 and first.recovery_nudges == 1
    assert second.exit_code == 0
    assert second.killed_reason is None
    assert second.recovery_nudges == 0
    assert second.turn_anomalies == ()
    assert second.turns == 3
    assert [text for _, text in client.sent_prompts] == [
        "chunk one",
        _LOOP_RECOVERY_NUDGE,
        "chunk two",
    ]
    assert cell.kill_calls == 0


def test_serve_drive_loop_guard_nudges_are_unbounded(
    tmp_path: Path,
) -> None:
    """WO-NUDGE-INF-1 (Walter 2026-08-11): a guard kill that keeps repeating is
    nudged for as long as it repeats — no budget, no exhaustion kill. The old
    behaviour (2 nudges then loud exit 1 / loop_guard_exhausted) voided a run
    for behaviour that is normal under measurement."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [
        {"info_error": _LOOP_SIG},
        {"info_error": _LOOP_SIG},
        {"info_error": _LOOP_SIG},
        {"info_error": _LOOP_SIG},
    ]
    client.metrics_script = [
        dict(_ZERO_METRICS),   # baseline
        dict(_LOOP_METRICS),   # loop kill -> nudge 1
        dict(_LOOP_METRICS),   # re-loops -> nudge 2 (old budget ended HERE)
        dict(_LOOP_METRICS),   # re-loops -> nudge 3
        dict(_LOOP_METRICS),   # re-loops -> nudge 4
        _metrics(9, 200, 90, guard_aborted=1),  # finally recovers
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_loop_x",
        prompt="fix the gates",
        run_label="cell-loop-x",
        phase="feedback-1",
    )

    # Four nudges past the old budget of 2, then a clean phase.
    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.recovery_nudges == 4
    sent = [text for _, text in client.sent_prompts]
    assert sent == ["fix the gates"] + [_LOOP_RECOVERY_NUDGE] * 4
    # Every killed turn is retry-linked — none left dangling as an unretried
    # anomaly, which is what an exhaustion exit used to produce.
    assert len(stats.turn_anomalies) == 4
    assert all(a["retried"] is True for a in stats.turn_anomalies)
    assert all(a["retry_kind"] == "harness_resume" for a in stats.turn_anomalies)
    # Tokens stay fully metered across unbounded recovery (real burn shown).
    assert stats.output_tokens == 90
    # …and the nudges never inflate the measurement: 9 metered turns less the
    # excluded guard-killed turn.
    assert stats.turns == 8
    assert stats.guard_aborted_turns == 1


def test_serve_drive_nudges_never_inflate_scoring_turns(
    tmp_path: Path,
) -> None:
    """WO-NUDGE-INF-1: unbounded nudging must not buy the model turns. Every
    recovered (guard- or finalize-killed) turn is subtracted from scoring turns,
    so a phase nudged N times scores exactly what an un-nudged phase scores —
    while its tokens stay on the meter."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [
        {"info_error": _FIN_SIG},
        {"info_error": _LOOP_SIG},
    ]
    client.metrics_script = [
        dict(_ZERO_METRICS),      # baseline
        dict(_FINALIZE_METRICS),  # finalize kill -> nudge
        dict(_LOOP_METRICS),      # guard kill   -> nudge
        _metrics(10, 220, 100, guard_aborted=1, finalize=1),  # recovered
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_noinflate",
        prompt="fix the gates",
        run_label="cell-noinflate",
        phase="feedback-1",
    )

    assert stats.exit_code == 0
    assert stats.recovery_nudges == 2
    # 10 metered turns - 1 guard-killed - 1 finalize-killed = 8 scoring turns.
    assert stats.turns == 8
    assert stats.guard_aborted_turns == 1
    assert stats.finalize_timeout_turns == 1
    # Exclusions are reported, never silent; tokens are never hidden.
    assert stats.output_tokens == 100
    assert stats.input_tokens == 220


def test_run_cell_attempt_loop_guard_recovery_on_repair_leg(tmp_path: Path) -> None:
    """The repair leg (feedback attempt) gets the same recovery — RC-4: the
    drive is arm-identical regardless of phase."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [{"info_error": _LOOP_SIG}]
    client.metrics_script = [
        dict(_ZERO_METRICS),   # baseline
        dict(_LOOP_METRICS),   # loop kill
        _metrics(7, 150, 60, guard_aborted=1),  # post-nudge
    ]
    cell = _FakeCell()
    runner._serve_client = client
    runner._cell_session_id = "ses_repair_loop"
    events_path = tmp_path / "fb-loop.events.jsonl"

    feedback = "These are still failing — fix the implementation so they pass."
    kwargs = _make_feedback_attempt_kwargs(
        feedback_text=feedback,
        phase="feedback-1",
        events_path=events_path,
        kill_hook=cell.kill_worker_processes,
    )
    stats = runner._run_cell_attempt(**kwargs)

    assert [text for _, text in client.sent_prompts] == [feedback, _LOOP_RECOVERY_NUDGE]
    assert stats.exit_code == 0
    assert stats.recovery_nudges == 1
    assert stats.turn_anomalies[0]["terminal"] == TURN_TERMINAL_GUARD_ABORT
    assert stats.turn_anomalies[0]["retried"] is True
    assert cell.kill_calls == 0


def test_serve_drive_loop_recovery_zero_delta_stays_loud(tmp_path: Path) -> None:
    """A loop kill that produced nothing is recovered, but if the recovered
    phase STILL produced nothing the silent-phase guard fires — never nudged
    into looking healthy."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [
        {"info_error": "relay_loop_detected n=40 limit=3"}
    ]
    loop_zero = dict(_ZERO_METRICS)
    loop_zero.update(
        info_errors=1,
        error_texts=["relay_loop_detected n=40 limit=3"],
    )
    client.metrics_script = [
        dict(_ZERO_METRICS),  # baseline
        loop_zero,            # loop kill, zero deltas
        dict(_ZERO_METRICS),  # post-nudge: clean, still zero
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_loop_silent",
        prompt="fix the gates",
        run_label="cell-loop-silent",
        phase="feedback-1",
    )

    assert stats.exit_code == 1
    assert stats.recovery_nudges == 1
    terminals = [a["terminal"] for a in stats.turn_anomalies]
    assert TURN_TERMINAL_GUARD_ABORT in terminals
    assert "silent_phase" in terminals
    assert cell.kill_calls == 0


def test_chunked_pass_loop_guard_recovery_inside_chunk(tmp_path: Path) -> None:
    """The building leg recovers in-chunk: the loop kill is nudged, the
    recovered drive lands the marker, the chunk plan advances, and the chunk
    report carries the recovery-nudge + guard-excluded counts.

    This is also the 2026-08-10 live-cell incident replay: every cumulative
    read after the kill STILL carries the kill's error text (a persisted
    info.error never leaves the transcript). The post-nudge classification
    must read only the window produced since the nudge — never re-classify
    the stale kill (that misread nudged a CHUNK FINISHED drive twice more and
    killed the cell loop_guard_exhausted)."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [{"info_error": _LOOP_SIG}]
    stale_post_nudge = _metrics(6, 120, 50, guard_aborted=1)
    stale_post_nudge.update(info_errors=1, error_texts=[_LOOP_SIG])
    stale_chunk2_end = _metrics(9, 200, 90, guard_aborted=1)
    stale_chunk2_end.update(info_errors=1, error_texts=[_LOOP_SIG])
    client.metrics_script = [
        dict(_ZERO_METRICS),     # chunk-1 baseline
        dict(_LOOP_METRICS),     # chunk-1 loop-killed read
        stale_post_nudge,        # chunk-1 post-nudge read (stale error carried)
        _metrics(6, 120, 50, guard_aborted=1),    # chunk-2 baseline
        stale_chunk2_end,        # chunk-2 end read (stale error carried)
    ]
    client.assistant_texts = [
        # The killed chunk-1 drive persists no text (info_error shape above).
        "recovered work. CHUNK FINISHED",     # loop-recovery nudge
        "chunk two done. CHUNK FINISHED",     # chunk-2 drive
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve_chunked(
        active_cell=cell,
        serve_client=client,
        session_id="ses_chunk_loop",
        prompts=["CHUNK ONE", "CHUNK TWO"],
        run_label="cell-chunk-loop",
    )

    sent = [text for _, text in client.sent_prompts]
    assert sent == ["CHUNK ONE", _LOOP_RECOVERY_NUDGE, "CHUNK TWO"]
    assert stats.exit_code == 0
    assert stats.recovery_nudges == 1
    # Chunk 1: 6 metered - 1 guard-aborted = 5 scoring; chunk 2: 3 scoring.
    assert stats.turns == 8
    assert stats.guard_aborted_turns == 1
    assert stats.chunk_reports[0]["recovery_nudges"] == 1
    assert stats.chunk_reports[0]["guard_aborted_turns"] == 1
    assert stats.chunk_reports[0]["marker"] is True
    assert stats.chunk_reports[0]["nudged"] is False
    assert stats.chunk_reports[1]["recovery_nudges"] == 0
    assert stats.chunk_reports[1]["marker"] is True


# ---------------------------------------------------------------------------
# WO-FINALIZE-REC-1 (Walter 2026-08-10): finalize-watchdog kills get recovery
# too — with the RESUME nudge, never the anti-repetition nudge.
# WO-NUDGE-INF-1 (Walter 2026-08-11): that recovery is unbounded.
# ---------------------------------------------------------------------------
def test_serve_drive_finalize_timeout_recovers_with_resume_nudge(tmp_path: Path) -> None:
    """A relay finalize-watchdog kill is classified
    transport_error/stream_finalize_timeout and re-driven with the resume
    nudge (the turn was cut off, NOT looping — the anti-repetition nudge
    would be the wrong instruction). The killed turn is excluded from scoring
    turns (WO-NUDGE-INF-1), exactly as a guard-killed turn is."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [{"info_error": _FIN_SIG}]
    client.metrics_script = [
        dict(_ZERO_METRICS),      # phase baseline
        dict(_FINALIZE_METRICS),  # finalize-killed read
        _metrics(8, 160, 70, finalize=1),  # post-nudge read (session-cumulative)
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_fin",
        prompt="fix the gates",
        run_label="cell-fin",
        phase="feedback-1",
    )

    sent = [text for _, text in client.sent_prompts]
    assert sent == ["fix the gates", _FINALIZE_RECOVERY_NUDGE]
    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.recovery_nudges == 1
    # WO-NUDGE-INF-1: the finalize-killed turn is EXCLUDED from scoring turns
    # (8 metered - 1 finalize-killed), so recovery cannot inflate the
    # measurement; the exclusion is carried, never silent.
    assert stats.turns == 7
    assert stats.finalize_timeout_turns == 1
    assert stats.guard_aborted_turns == 0
    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_TRANSPORT_ERROR
    assert anomaly["reason"] == "stream_finalize_timeout"
    assert anomaly["retried"] is True
    assert anomaly["retry_kind"] == "harness_resume"
    assert cell.kill_calls == 0


def test_serve_drive_finalize_timeout_exhaustion_never_kills_the_phase(
    tmp_path: Path,
) -> None:
    """WO-NUDGE-INF-1 replays the 2026-08-11 incident: phase initial-chunk-6
    took THREE consecutive finalize kills. Under the old budget of 2 the third
    exhausted recovery -> stream_finalize_exhausted -> gates ran on partial work
    and the run died. Unbounded now: the third kill is nudged like the first."""
    runner = _make_runner(tmp_path)
    client = _FakeServeClient()
    client.assistant_terminal_script = [
        {"info_error": _FIN_SIG},
        {"info_error": _FIN_SIG},
        {"info_error": _FIN_SIG},
    ]
    client.metrics_script = [
        dict(_ZERO_METRICS),      # baseline
        dict(_FINALIZE_METRICS),  # kill 1 -> nudge 1
        dict(_FINALIZE_METRICS),  # kill 2 -> nudge 2 (old budget ended HERE)
        dict(_FINALIZE_METRICS),  # kill 3 -> nudge 3 (was: exhausted, exit 1)
        _metrics(9, 200, 90, finalize=1),  # recovers
    ]
    cell = _FakeCell()

    stats = runner._run_opencode_serve(
        active_cell=cell,
        serve_client=client,
        session_id="ses_fin_x",
        prompt="fix the gates",
        run_label="cell-fin-x",
        phase="initial-chunk-6",
    )

    assert stats.exit_code == 0
    assert stats.killed_reason is None
    assert stats.recovery_nudges == 3
    assert len(stats.turn_anomalies) == 3
    assert all(a["reason"] == "stream_finalize_timeout" for a in stats.turn_anomalies)
    assert all(a["retried"] is True for a in stats.turn_anomalies)
    # True burn is never hidden, and the nudges bought no scoring turns.
    assert stats.output_tokens == 90
    assert stats.turns == 8
    assert stats.finalize_timeout_turns == 1


def test_bench_session_title_format_is_deterministic(tmp_path: Path) -> None:
    """WO-STRIP-2b: ``wevibe-bench-<org>-<arm>-<cell_ts>``, identifiably."""
    title = bench_session_title("wevibe-org-0", "off", 1786777435)
    assert title == "wevibe-bench-wevibe-org-0-off-1786777435"
    assert bench_session_title("wevibe-org-0", "on", 1786777435) == (
        "wevibe-bench-wevibe-org-0-on-1786777435"
    )


def test_bench_session_title_sanitizes_and_falls_back(tmp_path: Path) -> None:
    """org_id is folded to [A-Za-z0-9-]; empty/none -> literal ``org``."""
    assert bench_session_title("wevibe/org_0", "off", 1786777435) == (
        "wevibe-bench-wevibe-org-0-off-1786777435"
    )
    assert bench_session_title("", "on", 7) == "wevibe-bench-org-on-7"
    assert bench_session_title(None, "off", 7) == "wevibe-bench-org-off-7"
