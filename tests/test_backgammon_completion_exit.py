from __future__ import annotations

import os
from pathlib import Path
import sys
import textwrap
import time

from wevibe_bench.adapters.backgammon import BackgammonRunner, _OpencodeRunStats


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def _make_runner(
    tmp_path: Path,
    *,
    run_timeout_s: int = 1200,
    completion_grace_s: int = 2,
    progress=None,
) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="openrouter/anthropic/claude-opus-4.8",
        run_timeout_s=run_timeout_s,
        completion_grace_s=completion_grace_s,
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
