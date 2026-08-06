from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest

from wevibe_bench.adapters.backgammon import BackgammonRunner, _OpencodeRunStats


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def _make_runner(
    tmp_path: Path,
    *,
    run_timeout_s: int = 1200,
    completion_grace_s: int = 2,
    max_steps_per_attempt: int | None = None,
    progress=None,
) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="openrouter/anthropic/claude-opus-4.8",
        run_timeout_s=run_timeout_s,
        completion_grace_s=completion_grace_s,
        max_steps_per_attempt=max_steps_per_attempt,
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
) -> tuple[_OpencodeRunStats, float]:
    started = time.monotonic()
    stats = runner._run_opencode(
        cmd=[sys.executable, str(script_path)],
        worktree=script_path.parent,
        events_path=events_path,
        env=os.environ.copy(),
        run_label=run_label,
        phase=phase,
        fallback_session_id=None,
        kill_hook=None,
    )
    return stats, time.monotonic() - started


@pytest.mark.slow
def test_clean_exit_after_stop_when_process_hangs(tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, completion_grace_s=2, progress=progress_lines.append)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json
        import time


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit(
            {
                "type": "step_finish",
                "sessionID": "sess-hang-after-stop",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 5, "output": 11, "reasoning": 3},
                },
            }
        )
        time.sleep(30)
        """,
    )

    stats, wall = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "hang.events.jsonl",
        run_label="completion-hang",
        phase="attempt-1",
    )

    assert wall < 15
    assert stats.killed_reason is None
    assert any("step=worker-complete" in line for line in progress_lines)


@pytest.mark.slow
def test_no_premature_exit_when_stop_followed_by_more_work(tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, completion_grace_s=2, progress=progress_lines.append)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json
        import time


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit(
            {
                "type": "step_finish",
                "sessionID": "sess-stop-then-work",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 3, "output": 7, "reasoning": 1},
                },
            }
        )
        time.sleep(1.0)
        emit({"type": "step_start", "sessionID": "sess-stop-then-work", "part": {}})
        emit({"type": "tool_use", "sessionID": "sess-stop-then-work", "part": {"name": "run"}})
        emit(
            {
                "type": "step_finish",
                "sessionID": "sess-stop-then-work",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 9, "output": 13, "reasoning": 2},
                },
            }
        )
        """,
    )

    stats, _ = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "stop-then-work.events.jsonl",
        run_label="stop-then-work",
        phase="attempt-1",
    )

    assert stats.killed_reason is None
    assert stats.exit_code == 0
    assert stats.turns == 2
    assert stats.output_tokens == 20
    assert not any("step=worker-complete" in line for line in progress_lines)


@pytest.mark.slow
def test_run_timeout_still_fires_when_never_stops(tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, run_timeout_s=2, completion_grace_s=8, progress=progress_lines.append)
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json
        import time


        def emit(payload):
            print(json.dumps(payload), flush=True)


        emit({"type": "step_start", "sessionID": "sess-never-stop", "part": {}})
        emit({"type": "tool_use", "sessionID": "sess-never-stop", "part": {"name": "run"}})
        emit(
            {
                "type": "step_finish",
                "sessionID": "sess-never-stop",
                "part": {
                    "reason": "tool-calls",
                    "tokens": {"input": 4, "output": 6, "reasoning": 1},
                },
            }
        )
        time.sleep(30)
        """,
    )

    stats, _ = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "never-stop.events.jsonl",
        run_label="never-stop",
        phase="attempt-1",
    )

    assert stats.killed_reason == "run_timeout"
    assert not any("step=worker-complete" in line for line in progress_lines)


@pytest.mark.slow
def test_step_cap_kill_fires_past_cap(tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(
        tmp_path,
        completion_grace_s=8,
        max_steps_per_attempt=2,
        progress=progress_lines.append,
    )
    script_path = _write_fake_opencode(
        tmp_path,
        """
        import json
        import time


        def emit(payload):
            print(json.dumps(payload), flush=True)


        for idx in range(3):
            emit({"type": "step_start", "sessionID": "sess-step-cap", "part": {}})
            emit(
                {
                    "type": "step_finish",
                    "sessionID": "sess-step-cap",
                    "part": {
                        "reason": "tool-calls",
                        "tokens": {"input": 4, "output": 6, "reasoning": 1},
                    },
                }
            )
        time.sleep(30)
        """,
    )

    stats, wall = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / "step-cap.events.jsonl",
        run_label="step-cap",
        phase="attempt-1",
    )

    assert wall < 15
    assert stats.killed_reason == "max_steps_per_attempt"
    assert any(
        "reason=max_steps_per_attempt" in line and "max_steps_per_attempt=2" in line
        for line in progress_lines
    )
    assert not any("step=worker-complete" in line for line in progress_lines)


@pytest.mark.parametrize(
    ("exit_code", "signal_name"),
    (
        (143, "SIGTERM"),
        (137, "SIGKILL"),
    ),
)
@pytest.mark.slow
def test_external_signal_exit_is_logged_with_signal_attribution(
    tmp_path: Path,
    exit_code: int,
    signal_name: str,
) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, completion_grace_s=2, progress=progress_lines.append)
    script_path = _write_fake_opencode(
        tmp_path,
        f"""
        import sys

        sys.exit({exit_code})
        """,
    )

    stats, _ = _run_script(
        runner,
        script_path=script_path,
        events_path=tmp_path / f"external-signal-{exit_code}.events.jsonl",
        run_label=f"external-signal-{exit_code}",
        phase="attempt-1",
    )

    assert stats.exit_code == exit_code
    assert stats.killed_reason is None
    assert any(
        "op=worker.external_exit" in line
        and f"exit={exit_code}" in line
        and f"signal={signal_name}" in line
        and "attribution=external" in line
        and "killed_reason=none" in line
        for line in progress_lines
    )


def test_run_opencode_writes_stdin_text_and_closes_pipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path)

    class _RecordingStdin:
        def __init__(self) -> None:
            self.writes: list[str] = []
            self.flush_calls = 0
            self.closed = False

        def write(self, value: str) -> int:
            self.writes.append(value)
            return len(value)

        def flush(self) -> None:
            self.flush_calls += 1

        def close(self) -> None:
            self.closed = True

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            self.stdin = _RecordingStdin()
            self.returncode = 0

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            return self.returncode

    fake_proc = _FakeProc()
    popen_kwargs: dict[str, object] = {}

    def _fake_popen(cmd: list[str], **kwargs: object) -> _FakeProc:
        popen_kwargs.clear()
        popen_kwargs.update(kwargs)
        return fake_proc

    monkeypatch.setattr("wevibe_bench.adapters.backgammon.subprocess.Popen", _fake_popen)

    payload = "D6-STDIN-UNIT-MARKER"
    stats = runner._run_opencode(
        cmd=[sys.executable, "-c", "print('ok')"],
        worktree=tmp_path,
        events_path=tmp_path / "stdin.events.jsonl",
        env=os.environ.copy(),
        run_label="stdin-unit",
        phase="initial",
        fallback_session_id="sess-fallback",
        kill_hook=None,
        stdin_text=payload,
    )

    assert popen_kwargs.get("stdin") == subprocess.PIPE
    assert fake_proc.stdin.writes == [payload]
    assert fake_proc.stdin.flush_calls == 1
    assert fake_proc.stdin.closed is True
    assert stats.exit_code == 0


@pytest.mark.slow
def test_final_step_finish_survives_when_reader_drain_is_slow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reader thread still draining past the OLD 5s join cap must not drop the
    final step_finish. The blocking join (adapter :2387-2388) waits for the
    reader to finish; the old join(timeout=5) would abandon the daemon reader
    mid-sleep(6.0) and silently drop the trailing event.

    Determinism: poll() returns 0 immediately so the main loop breaks at once
    and reaches the join while the reader is still time.sleep(6.0)-ing. On the
    OLD join(timeout=5) the main thread abandons the reader and returns with
    stats.turns == 1 and no sess-load-final line. On the NEW blocking join the
    main thread waits out the 6s drain, both step_finish events are processed,
    stats.turns == 2 and sess-load-final is present in the events file.
    """
    first_event = {
        "type": "step_finish",
        "sessionID": "sess-load",
        "part": {"reason": "stop", "tokens": {"input": 1, "output": 2, "reasoning": 0}},
    }
    final_event = {
        "type": "step_finish",
        "sessionID": "sess-load-final",
        "part": {"reason": "stop", "tokens": {"input": 3, "output": 4, "reasoning": 1}},
    }
    first_line = json.dumps(first_event) + "\n"
    final_line = json.dumps(final_event) + "\n"

    class _SlowDrainStdout:
        """Yields the first step_finish, then blocks past the old 5s join cap
        (simulating a reader still draining under load), then yields the final
        step_finish. close() is the reader's finally no-op."""

        def __init__(self, first: str, final: str) -> None:
            self._first = first
            self._final = final

        def __iter__(self):
            yield self._first
            time.sleep(6.0)
            yield self._final

        def close(self) -> None:
            pass

    class _FakeSlowProc:
        def __init__(self) -> None:
            self.stdout = _SlowDrainStdout(first_line, final_line)
            self.stderr = io.StringIO("")
            self.stdin = None
            self.returncode = 0

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            return self.returncode

    fake_proc = _FakeSlowProc()

    def _fake_popen(cmd: list[str], **kwargs: object) -> _FakeSlowProc:
        return fake_proc

    monkeypatch.setattr("wevibe_bench.adapters.backgammon.subprocess.Popen", _fake_popen)

    progress_lines: list[str] = []
    runner = _make_runner(tmp_path, progress=progress_lines.append)
    events_path = tmp_path / "slow-drain.events.jsonl"

    stats = runner._run_opencode(
        cmd=[sys.executable, "-c", "print('ok')"],
        worktree=tmp_path,
        events_path=events_path,
        env=os.environ.copy(),
        run_label="slow-drain",
        phase="attempt-1",
        fallback_session_id="sess-fallback",
        kill_hook=None,
    )

    # 1. The final event survived the slow reader drain.
    events_text = events_path.read_text(encoding="utf-8")
    assert '"sess-load-final"' in events_text

    # 2. Turn accounting reflects BOTH step_finish events.
    assert stats.turns == 2

    # 3. The recorded-bound did not falsely fire: both readers hit EOF so the
    #    drain events are complete (no step=reader-drain status=incomplete).
    assert not any(
        "step=reader-drain" in line and "status=incomplete" in line
        for line in progress_lines
    )
