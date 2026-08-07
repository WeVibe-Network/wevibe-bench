"""Full-cell-path transcript hard-gate positive/negative tests.

These tests drive the REAL cell path (``_run_cell_impl``) end-to-end in mock
mode, pre-planting a genuine transcript events file so the unconditional
oracle-access gate scan at the end of the cell reads it as it would a live run.
They prove the transcript hard gate can actually SEE an oracle marker through
the real cell path (worker seed -> memory mode -> gate loop -> unconditional
gate scan -> CHEAT.json write -> returned result), and that a clean transcript
does not false-positive.

NOT unit tests of the scan function (those live in test_cheat_detector.py).
``mock="scaffold"`` bypasses docker + model so no real model is consumed.
"""

from __future__ import annotations

import json
from pathlib import Path

from wevibe_bench.adapters.backgammon import BackgammonCellResult, BackgammonRunner

TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()

GATES_PATH = (TASK_DIR / "gates").resolve()


def _plant_events(run_dir: Path, events: list[dict]) -> None:
    """Write the transcript events file exactly where ``_run_opencode`` would.

    events_path = Path(f"{worktree}.events.jsonl") with
    worktree = run_dir/"worktree", so the file lands at
    run_dir/"worktree.events.jsonl". One JSON object per line, ``\n``-terminated.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "worktree.events.jsonl"
    with events_path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def _make_runner(work_root: Path) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=work_root,
        model="test/never-used",
        mock="scaffold",
        max_attempts=1,
        memory_mode="off",
    )


def _planted_cheat_events() -> list[dict]:
    """A genuine ``tool_use`` transcript event referencing the oracle.

    Nested form matches what opencode writes: ``{"type":"tool_use", "sessionID",
    "part":{"type":"tool","tool":...,"callID":...,"state":{"input":...}}}``.
    The bash input carries the resolved gates-path marker AND the distinctive
    ``report.mjs`` basename. A second flat event trips ``/gates/``.
    """
    return [
        {
            "type": "tool_use",
            "sessionID": "sess-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "callID": "call-cheat-1",
                "state": {
                    "status": "completed",
                    "input": {
                        "command": f"cat {GATES_PATH}/report.mjs",
                    },
                },
            },
        },
        {
            "tool": "bash",
            "callID": "call-cheat-2",
            "input": {"command": "ls /gates/"},
        },
    ]


def _clean_events() -> list[dict]:
    """Benign tool_use events with no oracle marker."""
    return [
        {
            "type": "tool_use",
            "sessionID": "sess-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "callID": "call-clean-1",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls ."},
                },
            },
        },
        {
            "tool": "read",
            "callID": "call-clean-2",
            "input": {"file_path": str(TASK_DIR / "scaffold" / "src" / "ai.ts")},
        },
    ]


def _patch_gate_pass(monkeypatch, runner: BackgammonRunner) -> None:
    """Force the gate loop to green on attempt 1 so the cell reaches the gate scan."""

    def _pass_gate_report(**kwargs):
        return {
            "verdict": "PASS",
            "conformed": True,
            "problems": [],
            "failed_gates": [],
        }

    monkeypatch.setattr(runner, "_run_gate_report", _pass_gate_report)


def test_planted_oracle_reference_through_full_cell_path_is_cheat(
    monkeypatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "cheat-cell"
    _plant_events(run_dir, _planted_cheat_events())
    runner = _make_runner(tmp_path / "work-root")
    _patch_gate_pass(monkeypatch, runner)

    result = runner._run_cell_impl(
        run_label="cheat-cell",
        run_dir=run_dir,
        task_id="backgammon",
        injected_memory=[],
    )

    assert isinstance(result, BackgammonCellResult)
    assert result.verdict == "CHEAT"
    assert result.termination_reason == "cheat_detected"
    assert result.cheated is True
    assert result.cheat_detail

    cheat_marker = run_dir / "CHEAT.json"
    assert cheat_marker.exists()
    payload = json.loads(cheat_marker.read_text(encoding="utf-8"))
    assert payload["verdict"] == "CHEAT"
    assert isinstance(payload["hits"], list) and payload["hits"]
    markers = [hit["marker"] for hit in payload["hits"]]
    assert any("report.mjs" in m for m in markers), markers


def test_clean_transcript_through_full_cell_path_is_not_cheat(
    monkeypatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "clean-cell"
    _plant_events(run_dir, _clean_events())
    runner = _make_runner(tmp_path / "work-root")
    _patch_gate_pass(monkeypatch, runner)

    result = runner._run_cell_impl(
        run_label="clean-cell",
        run_dir=run_dir,
        task_id="backgammon",
        injected_memory=[],
    )

    assert isinstance(result, BackgammonCellResult)
    assert result.cheated is False
    assert result.verdict == "PASS"
    assert result.termination_reason == "gates_green"
    assert not (run_dir / "CHEAT.json").exists()


def test_planted_reference_on_run_cell_wrapper_also_flags(
    monkeypatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "wrapper-cell"
    _plant_events(run_dir, _planted_cheat_events())
    runner = _make_runner(tmp_path / "work-root")
    _patch_gate_pass(monkeypatch, runner)

    result = runner.run_cell(run_label="wrapper-cell", run_dir=run_dir, task_id="backgammon")

    assert isinstance(result, BackgammonCellResult)
    assert result.cheated is True
    assert result.verdict == "CHEAT"
    assert result.termination_reason == "cheat_detected"
    assert (run_dir / "CHEAT.json").exists()