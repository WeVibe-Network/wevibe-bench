"""WO-WATCH-1E: truncation/transport evidence is recorded, self-documenting,
and correlation-ready against the local proxy's own ``runs/*.jsonl`` log.

The harness cannot see the proxy's internal trace id at capture time, so every
real truncation/transport-error writes a correlation record (timestamp window +
attempt id + session id + stream counters) that a human or future step matches
against the proxy log by ``ts`` within the recorded window. This test is fully
hermetic: no live server, no docker, no model. It exercises the pure evidence
builder directly and the stdout-path capture end-to-end through a fake worker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import textwrap
from typing import Any

from wevibe_bench.adapters.backgammon import (
    BackgammonRunner,
    TURN_TERMINAL_STREAM_DIED_OPEN,
    TURN_TERMINAL_TRANSPORT_ERROR,
    TURN_TERMINAL_TRUNCATED,
    _OpencodeRunStats,
    _build_truncation_evidence,
    _iso_utc,
)


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()

REQUIRED_FIELDS = {
    "attempt_id",
    "run_label",
    "phase",
    "terminal",
    "reason",
    "ts_start_epoch_ms",
    "ts_end_epoch_ms",
    "wall_seconds",
    "session_id",
    "received_bytes",
    "received_lines",
    "last_event_type",
    "last_event_ts",
    "finish_reason",
    "output_tokens_received",
    "input_tokens_received",
    "reasoning_tokens_received",
    "truncations_seen",
    "correlation",
}


def _make_runner(tmp_path: Path, *, progress: Any = None) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="local-llm-proxy/kimi/kimi-k3",
        memory_mode="off",
        run_timeout_s=30,
        completion_grace_s=2,
        progress=progress,
    )


def _run_script(
    runner: BackgammonRunner,
    *,
    script_path: Path,
    events_path: Path,
    run_label: str,
    phase: str,
    tmp_path: Path,
) -> _OpencodeRunStats:
    # worktree is a per-test SUBDIR of tmp_path so the evidence file lands at
    # ``tmp_path / truncation-evidence.jsonl`` (unique per test; pytest shares
    # tmp_path.parent across tests, so worktree.parent must be the per-test dir).
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    stats = runner._run_opencode(
        cmd=[sys.executable, str(script_path)],
        worktree=worktree,
        events_path=events_path,
        env=os.environ.copy(),
        run_label=run_label,
        phase=phase,
        fallback_session_id=None,
        kill_hook=None,
    )
    return stats


# --------------------------------------------------------------------------
# Pure builder
# --------------------------------------------------------------------------

def test_build_truncation_evidence_all_required_fields_with_match_key() -> None:
    ts_start = 1_700_000_000_000
    ts_end = 1_700_000_042_000
    record = _build_truncation_evidence(
        attempt_id="attempt-abc123def456",
        run_label="bk-01",
        phase="initial",
        terminal=TURN_TERMINAL_TRUNCATED,
        reason="stream-incomplete",
        ts_start_epoch_ms=ts_start,
        ts_end_epoch_ms=ts_end,
        wall_seconds=42.0,
        session_id="sess-xyz",
        received_bytes=1234,
        received_lines=57,
        last_event_type="step_finish",
        last_event_ts=1_700_000_040_000,
        finish_reason="stream-incomplete",
        output_tokens_received=300,
        input_tokens_received=1200,
        reasoning_tokens_received=10,
        truncations_seen=1,
    )

    assert set(record.keys()) == REQUIRED_FIELDS
    assert record["attempt_id"] == "attempt-abc123def456"
    assert record["run_label"] == "bk-01"
    assert record["phase"] == "initial"
    assert record["terminal"] == TURN_TERMINAL_TRUNCATED
    assert record["reason"] == "stream-incomplete"
    assert record["ts_start_epoch_ms"] == ts_start
    assert record["ts_end_epoch_ms"] == ts_end
    assert record["wall_seconds"] == 42.0
    assert record["session_id"] == "sess-xyz"
    assert record["received_bytes"] == 1234
    assert record["received_lines"] == 57
    assert record["last_event_type"] == "step_finish"
    assert record["last_event_ts"] == 1_700_000_040_000
    assert record["finish_reason"] == "stream-incomplete"
    assert record["output_tokens_received"] == 300
    assert record["input_tokens_received"] == 1200
    assert record["reasoning_tokens_received"] == 10
    assert record["truncations_seen"] == 1

    corr = record["correlation"]
    assert corr["proxy_log_dir"] == "runs"
    # ts_window is derived from the epoch-ms fields, in UTC ISO form.
    assert corr["ts_window_utc"] == [_iso_utc(ts_start), _iso_utc(ts_end)]
    assert corr["match_key"] == "bk-01|attempt-abc123def456|sess-xyz"
    # The recorded window must actually bracket the anomaly so a human can line
    # it up against the proxy rows by ts.
    assert _iso_utc(ts_start) <= _iso_utc(ts_end)


def test_build_truncation_evidence_none_session_and_missing_attempt() -> None:
    record = _build_truncation_evidence(
        attempt_id=None,
        run_label="bk-02",
        phase="resume",
        terminal=TURN_TERMINAL_TRANSPORT_ERROR,
        reason="error_event",
        ts_start_epoch_ms=None,
        ts_end_epoch_ms=1_700_000_100_000,
        wall_seconds=None,
        session_id=None,
        received_bytes=None,
        received_lines=None,
        last_event_type=None,
        last_event_ts=None,
        finish_reason=None,
        output_tokens_received=0,
        input_tokens_received=0,
        reasoning_tokens_received=0,
        truncations_seen=0,
    )
    assert record["attempt_id"] is None
    assert record["session_id"] is None
    assert record["ts_start_epoch_ms"] is None
    assert record["wall_seconds"] is None
    assert record["received_bytes"] is None
    assert record["received_lines"] is None
    # Window still has a concrete end; start stays None (unknown).
    assert record["correlation"]["ts_window_utc"][1] == _iso_utc(1_700_000_100_000)
    assert record["correlation"]["ts_window_utc"][0] is None
    assert record["correlation"]["match_key"] == "bk-02|none|none"
    assert record["finish_reason"] is None


# --------------------------------------------------------------------------
# stdout path end-to-end (fake worker emits a truncated turn, then a retry)
# --------------------------------------------------------------------------

def _write_fake_opencode(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "fake_opencode.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    return script


def test_stdout_truncated_turn_writes_evidence_file_with_counters(tmp_path: Path) -> None:
    """A stream-incomplete step_finish must append a complete evidence record."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 1000000, "sessionID": "sess-ev", "part": {}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 1006000,
                "sessionID": "sess-ev",
                "part": {
                    "reason": "stream-incomplete",
                    "tokens": {"input": 0, "output": 0, "reasoning": 0},
                    "cost": 0,
                },
            }
        )
        emit({"type": "step_start", "timestamp": 1007000, "sessionID": "sess-ev", "part": {}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 1012000,
                "sessionID": "sess-ev",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 500, "output": 40, "reasoning": 3},
                    "cost": 0.01,
                },
            }
        )
        """,
    )
    events_path = tmp_path / "ev.events.jsonl"

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=events_path,
        run_label="bk-ev",
        phase="initial",
        tmp_path=tmp_path,
    )

    # The truncation produced an anomaly...
    assert len(stats.turn_anomalies) == 1
    assert stats.turn_anomalies[0]["terminal"] == TURN_TERMINAL_TRUNCATED

    # ...and a matching evidence line in the run's evidence file.
    evidence_path = tmp_path / "truncation-evidence.jsonl"
    lines = [json.loads(l) for l in evidence_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    rec = lines[0]

    assert set(rec.keys()) == REQUIRED_FIELDS
    assert rec["run_label"] == "bk-ev"
    assert rec["phase"] == "initial"
    assert rec["terminal"] == TURN_TERMINAL_TRUNCATED
    assert rec["reason"] == "stream-incomplete"
    assert rec["finish_reason"] == "stream-incomplete"
    assert rec["session_id"] == "sess-ev"
    assert rec["attempt_id"] is not None and rec["attempt_id"].startswith("bk-ev-initial-")
    # Stream counters reflect what the stdout reader consumed AT THE MOMENT
    # the truncation was detected (step_start + the truncated step_finish) —
    # the retry events land after capture.
    assert rec["received_lines"] == 2
    assert rec["received_bytes"] is not None and rec["received_bytes"] > 0
    # The retry's metered usage had NOT landed yet at capture moment (the
    # truncation was detected at the first step_finish, before the retry).
    assert rec["output_tokens_received"] == 0
    assert rec["input_tokens_received"] == 0
    assert rec["reasoning_tokens_received"] == 0
    # The LAST parsed event was the terminating stop step_finish.
    assert rec["last_event_type"] == "step_finish"
    # truncations_seen only counts the "length" reason, not stream-incomplete.
    assert rec["truncations_seen"] == 0

    corr = rec["correlation"]
    assert corr["proxy_log_dir"] == "runs"
    # ts window is populated and the end brackets the start.
    assert corr["ts_window_utc"][0] is not None and corr["ts_window_utc"][1] is not None
    assert corr["match_key"] == f"bk-ev|{rec['attempt_id']}|sess-ev"
    # The recorded anomaly wall_seconds matches the event gap.
    assert rec["wall_seconds"] == 6.0


def test_stdout_transport_error_and_no_evidence_without_anomaly(tmp_path: Path) -> None:
    """transport_error closes the open step AND writes evidence; a clean run writes none."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 2000000, "sessionID": "sess-err2", "part": {}})
        emit(
            {
                "type": "error",
                "timestamp": 2042000,
                "sessionID": "sess-err2",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": '"relay: stream incomplete (a03b34fe888a4c739dbb0fb2c122ec25)"'},
                },
            }
        )
        """,
    )
    events_path = tmp_path / "err2.events.jsonl"

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=events_path,
        run_label="bk-err2",
        phase="initial",
        tmp_path=tmp_path,
    )
    assert stats.turn_anomalies[0]["terminal"] == TURN_TERMINAL_TRANSPORT_ERROR

    evidence_path = tmp_path / "truncation-evidence.jsonl"
    recs = [json.loads(l) for l in evidence_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    # Only the error-run appended here; the transport error wrote ONE record.
    rec = [r for r in recs if r["run_label"] == "bk-err2"][0]
    assert rec["terminal"] == TURN_TERMINAL_TRANSPORT_ERROR
    assert rec["reason"] == "stream_incomplete"
    assert rec["finish_reason"] is None
    assert rec["last_event_type"] == "error"
    assert rec["session_id"] == "sess-err2"


def test_stdout_clean_run_creates_no_evidence_file(tmp_path: Path) -> None:
    """A clean stop with no truncation/transport anomaly must NOT create the file."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 3000000, "sessionID": "sess-clean", "part": {}})
        emit(
            {
                "type": "step_finish",
                "timestamp": 3005000,
                "sessionID": "sess-clean",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 10, "output": 5, "reasoning": 1},
                    "cost": 0.0,
                },
            }
        )
        """,
    )
    events_path = tmp_path / "clean.events.jsonl"

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=events_path,
        run_label="bk-clean",
        phase="initial",
        tmp_path=tmp_path,
    )
    assert stats.turn_anomalies == ()
    assert stats.exit_code in (0, None)
    evidence_path = tmp_path / "truncation-evidence.jsonl"
    assert not evidence_path.exists()


def test_stdout_stream_died_open_writes_evidence(tmp_path: Path) -> None:
    """Process exits mid-step (no step_finish, no error) -> stream_died_open evidence."""
    runner = _make_runner(tmp_path)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "timestamp": 4000000, "sessionID": "sess-die", "part": {}})
        # process exits with the step still open
        """,
    )
    events_path = tmp_path / "die.events.jsonl"

    stats = _run_script(
        runner,
        script_path=script_path,
        events_path=events_path,
        run_label="bk-die",
        phase="initial",
        tmp_path=tmp_path,
    )
    assert len(stats.turn_anomalies) == 1
    assert stats.turn_anomalies[0]["terminal"] == TURN_TERMINAL_STREAM_DIED_OPEN

    evidence_path = tmp_path / "truncation-evidence.jsonl"
    recs = [json.loads(l) for l in evidence_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rec = [r for r in recs if r["run_label"] == "bk-die"][0]
    assert rec["terminal"] == TURN_TERMINAL_STREAM_DIED_OPEN
    assert rec["reason"] == "no_terminal_signal"
    assert rec["last_event_type"] == "step_start"
    assert rec["received_lines"] == 1