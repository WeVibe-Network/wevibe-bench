"""WO-TRUNC-1: truncated turns are first-class recorded outcomes.

A turn that ends as a truncated stream with no terminal signal must be
recorded (turn_terminal anomaly), counted (budget + unmetered accounting),
distinguished from every other turn ending, and linked to its retry — never
silently discarded or misclassified as a zero-tool turn.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import textwrap
from typing import Any

from wevibe_bench.adapters.backgammon import (
    BackgammonRunner,
    TURN_TERMINAL_GUARD_ABORT,
    TURN_TERMINAL_STREAM_DIED_OPEN,
    TURN_TERMINAL_TRANSPORT_ERROR,
    TURN_TERMINAL_TRUNCATED,
    _OpencodeRunStats,
)


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def _make_runner(tmp_path: Path, *, memory_mode: str = "off", progress: Any = None) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="local-llm-proxy/kimi/kimi-k3",
        memory_mode=memory_mode,
        run_timeout_s=30,
        completion_grace_s=2,
        progress=progress,
    )


def _write_fake_opencode(tmp_path: Path, source: str, *, name: str = "fake_opencode.py") -> Path:
    script = tmp_path / name
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


def test_truncated_no_signal_recorded_and_retried_client_auto(tmp_path: Path) -> None:
    """step_finish reason=unknown (zero usage) then a new step = auto-retried burn."""
    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, progress=progress_lines.append)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 1000000, "sessionID": "sess-trunc", "part": {}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 1006000,
                "sessionID": "sess-trunc",
                "part": {
                    "reason": "unknown",
                    "tokens": {"input": 0, "output": 0, "reasoning": 0},
                    "cost": 0,
                },
            }
        )
        emit({"type": "step_start", "timestamp": 1007000, "sessionID": "sess-trunc", "part": {}})
        emit({"type": "tool_use", "timestamp": 1007100, "sessionID": "sess-trunc", "part": {"tool": "edit"}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 1012000,
                "sessionID": "sess-trunc",
                "part": {
                    "reason": "tool-calls",
                    "tokens": {"input": 500, "output": 40, "reasoning": 3},
                    "cost": 0.01,
                },
            }
        )
        """,
    )

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "trunc.events.jsonl",
        run_label="trunc",
        phase="initial",
    )

    # The truncated turn is recorded as itself — never as a zero-tool turn.
    assert stats.zero_tool_turns == 0
    assert stats.terminal_zero_tool_turn is False
    # Both turns count toward the budget: the burn and the retry.
    assert stats.turns == 2
    # The retry's metered tokens land; the burned turn's never arrived.
    assert stats.output_tokens == 40
    assert stats.unmetered_turns == 1
    assert stats.unmetered_turn_wall_s == 6.0

    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_TRUNCATED
    assert anomaly["reason"] == "unknown"
    assert anomaly["tokens_unmetered"] is True
    assert anomaly["wall_seconds"] == 6.0
    assert anomaly["turn_index"] == 1
    assert anomaly["phase"] == "initial"
    # The burned attempt and its retry are both recorded.
    assert anomaly["retried"] is True
    assert anomaly["retry_kind"] == "client_auto"
    assert any("step=TURN-TERMINAL" in line for line in progress_lines)


def test_transport_error_closes_open_step_and_flags_stream_incomplete(tmp_path: Path) -> None:
    """error event 'relay: stream incomplete' kills the open turn; client retries."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 2000000, "sessionID": "sess-err", "part": {}})
        emit(
            {
                "type": "error",
                "timestamp": 2042000,
                "sessionID": "sess-err",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": '"relay: stream incomplete (a03b34fe888a4c739dbb0fb2c122ec25)"'},
                },
            }
        )
        emit({"type": "step_start", "timestamp": 2043000, "sessionID": "sess-err", "part": {}})
        emit({"type": "tool_use", "timestamp": 2043100, "sessionID": "sess-err", "part": {"tool": "write"}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 2049000,
                "sessionID": "sess-err",
                "part": {
                    "reason": "tool-calls",
                    "tokens": {"input": 900, "output": 120, "reasoning": 5},
                    "cost": 0.02,
                },
            }
        )
        """,
    )
    events_path = tmp_path / "err.events.jsonl"

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=events_path,
        run_label="err",
        phase="initial",
    )

    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_TRANSPORT_ERROR
    assert anomaly["reason"] == "stream_incomplete"
    assert anomaly["tokens_unmetered"] is True
    assert anomaly["wall_seconds"] == 42.0
    assert anomaly["retried"] is True
    assert anomaly["retry_kind"] == "client_auto"
    assert stats.unmetered_turns == 1
    assert stats.unmetered_turn_wall_s == 42.0
    # The transport-death signature is what the D-EXIT1 resume path keys on.
    assert runner._detect_stream_incomplete(events_path) is True


def test_loop_guard_error_classified_as_guard_abort(tmp_path: Path) -> None:
    """The proxy StreamLoopGuard trip is distinguishable from a plain drop."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 3000000, "sessionID": "sess-loop", "part": {}})
        emit(
            {
                "type": "error",
                "timestamp": 3005000,
                "sessionID": "sess-loop",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": "relay_loop_detected n=40 limit=3"},
                },
            }
        )
        """,
    )

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "loop.events.jsonl",
        run_label="loop",
        phase="initial",
    )

    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_GUARD_ABORT
    assert anomaly["reason"] == "loop_guard"
    assert anomaly["retried"] is False
    assert anomaly["retry_kind"] is None


def test_loop_guard_error_live_message_shape_classified_as_guard_abort(tmp_path: Path) -> None:
    """The live 2026-08-10 relay message shape classifies identically (stdout path)."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 3000000, "sessionID": "sess-loop", "part": {}})
        emit(
            {
                "type": "error",
                "timestamp": 3005000,
                "sessionID": "sess-loop",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": "relay: generation loop detected (<request-id>)"},
                },
            }
        )
        """,
    )

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "loop-live.events.jsonl",
        run_label="loop-live",
        phase="initial",
    )

    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_GUARD_ABORT
    assert anomaly["reason"] == "loop_guard"


def test_finalize_timeout_error_live_shape_reason_on_stdout_path(tmp_path: Path) -> None:
    """RC-4 taxonomy parity: the relay finalize-watchdog message names itself
    stream_finalize_timeout on the stdout path exactly as on the serve path."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 3000000, "sessionID": "sess-fin", "part": {}})
        emit(
            {
                "type": "error",
                "timestamp": 3005000,
                "sessionID": "sess-fin",
                "error": {
                    "name": "UnknownError",
                    "data": {
                        "message": "relay: upstream completed but the stream did not "
                        "finalize within 30000ms (<request-id>)"
                    },
                },
            }
        )
        """,
    )

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "fin.events.jsonl",
        run_label="fin",
        phase="initial",
    )

    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_TRANSPORT_ERROR
    assert anomaly["reason"] == "stream_finalize_timeout"


def test_stream_died_open_on_exit_without_terminal_signal(tmp_path: Path) -> None:
    """Process exits mid-step: no step_finish, no error — the harshest case."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json
        import sys


        print(json.dumps({"type": "step_start", "timestamp": 4000000, "sessionID": "sess-died", "part": {}}), flush=True)
        sys.exit(1)
        """,
    )

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "died.events.jsonl",
        run_label="died",
        phase="initial",
    )

    assert stats.exit_code == 1
    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_STREAM_DIED_OPEN
    assert anomaly["reason"] == "no_terminal_signal"
    assert anomaly["tokens_unmetered"] is True
    assert anomaly["retried"] is False
    # The burned turn consumes budget — recorded, never silent.
    assert stats.turns == 1
    assert stats.unmetered_turns == 1
    assert anomaly["wall_seconds"] is not None and anomaly["wall_seconds"] >= 0
    assert stats.zero_tool_turns == 0


def test_error_then_eof_is_single_unretried_transport_error(tmp_path: Path) -> None:
    """error event then process death: one record, no double stream_died_open."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json
        import sys


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 5000000, "sessionID": "sess-eof", "part": {}})
        emit(
            {
                "type": "error",
                "timestamp": 5010000,
                "sessionID": "sess-eof",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": '"relay: stream incomplete (1kvn560rw3U3KN8RoAZbI)"'},
                },
            }
        )
        sys.exit(1)
        """,
    )
    events_path = tmp_path / "eof.events.jsonl"

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=events_path,
        run_label="eof",
        phase="initial",
    )

    assert stats.exit_code == 1
    assert len(stats.turn_anomalies) == 1
    anomaly = stats.turn_anomalies[0]
    assert anomaly["terminal"] == TURN_TERMINAL_TRANSPORT_ERROR
    assert anomaly["reason"] == "stream_incomplete"
    assert anomaly["retried"] is False
    assert runner._detect_stream_incomplete(events_path) is True


def test_genuine_zero_tool_turn_classification_unchanged(tmp_path: Path) -> None:
    """A real text-only stop is still a zero-tool turn and no anomaly."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 6000000, "sessionID": "sess-zt", "part": {}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 6003000,
                "sessionID": "sess-zt",
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
        events_path=tmp_path / "zt.events.jsonl",
        run_label="zt",
        phase="initial",
    )

    assert stats.zero_tool_turns == 1
    assert stats.terminal_zero_tool_turn is True
    assert stats.turn_anomalies == ()


def test_length_finish_still_metered_truncation_not_anomaly(tmp_path: Path) -> None:
    """finish_reason=length stays the metered truncation class (T3)."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 7000000, "sessionID": "sess-len", "part": {}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 7004000,
                "sessionID": "sess-len",
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
        events_path=tmp_path / "len.events.jsonl",
        run_label="len",
        phase="initial",
    )

    assert stats.truncations == 1
    assert stats.turn_anomalies == ()
    assert stats.output_tokens == 100


def test_turn_anomaly_detection_is_mode_identical(tmp_path: Path) -> None:
    """RC-4: no mode branch — ON and OFF produce byte-identical anomaly ledgers."""
    source = """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 8000000, "sessionID": "sess-mode", "part": {}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 8009000,
                "sessionID": "sess-mode",
                "part": {
                    "reason": "unknown",
                    "tokens": {"input": 0, "output": 0, "reasoning": 0},
                    "cost": 0,
                },
            }
        )
        """
    anomalies_by_mode: dict[str, tuple] = {}
    stats_by_mode: dict[str, _OpencodeRunStats] = {}
    for mode in ("off", "on"):
        mode_dir = tmp_path / mode
        mode_dir.mkdir()
        runner = _make_runner(mode_dir, memory_mode=mode)
        script_path = _write_fake_opencode(mode_dir, source)
        stats = _run_script(
            runner,
            script_path=script_path,
            events_path=mode_dir / "mode.events.jsonl",
            run_label=f"mode-{mode}",
            phase="initial",
        )
        anomalies_by_mode[mode] = stats.turn_anomalies
        stats_by_mode[mode] = stats

    assert anomalies_by_mode["off"] == anomalies_by_mode["on"]
    assert len(anomalies_by_mode["off"]) == 1
    assert stats_by_mode["off"].unmetered_turns == stats_by_mode["on"].unmetered_turns == 1
    assert stats_by_mode["off"].turns == stats_by_mode["on"].turns
