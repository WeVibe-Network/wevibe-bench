"""WO-STRIP-2a chunk C9c: sequencer tests for the collapsed stage machine.

The cumulative campaign is measurement-only: every scheduled session walks
PREPARE_FIXTURE -> RUN_SESSION, the sequencer advances to the next session,
and the run ends in DONE. Recall during ON cells is auto-injected by the
worker plugin. There are NO extract, coordinator-review, leader-commit, or
index-ready stages and no LeaderClient at all — the ``SessionRunner`` protocol
carries only ``prepare_fixture`` / ``run_session``, and ``step_until_done`` is
the single drive method.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wevibe_bench.cumulative.sequencer import CumulativeSequencer, SessionRunner
from wevibe_bench.cumulative.types import (
    RosterEntry,
    SessionPhase,
    SessionRecord,
    WalkGateName,
    WalkGateVerdict,
    WalkGateVerdictRecord,
)


def _cell_telemetry() -> dict[str, Any]:
    """BackgammonCellResult-shaped telemetry accepted by progress_from_cell_result."""
    return {
        "problems_before": 3,
        "problems_final": ["problem-2", "problem-3"],
        "resolved_count": 1,
        "remaining_count": 2,
        "conformed": False,
        "attempts_to_green": 1,
        "turns": 2,
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "wall_seconds": 0.1,
        "wall_cost_usd": 0.0,
        "termination_reason": "attempt_ceiling_reached",
        "failed_gates": ["tests"],
    }


class FakeRunner:
    """SessionRunner double: traces phase walks, plants walk gates, can crash."""

    def __init__(
        self,
        *,
        walk_gates_by_sequence: dict[int, list[WalkGateVerdictRecord]] | None = None,
        crash_run_sequence_index: int | None = None,
    ) -> None:
        self.walk_gates_by_sequence = walk_gates_by_sequence or {}
        self.crash_run_sequence_index = crash_run_sequence_index

        self.phase_trace: list[str] = []
        self.prepare_calls = 0
        self.run_calls = 0
        self.run_sequence_indexes: list[int] = []

    def prepare_fixture(self, session: SessionRecord) -> None:
        self.phase_trace.append(session.phase)
        self.prepare_calls += 1

    def run_session(self, session: SessionRecord) -> dict[str, Any]:
        self.phase_trace.append(session.phase)
        self.run_calls += 1
        self.run_sequence_indexes.append(session.sequence_index)
        if (
            self.crash_run_sequence_index is not None
            and session.sequence_index == self.crash_run_sequence_index
        ):
            raise RuntimeError(f"simulated crash during session {session.sequence_index}")
        session.walk_gates = list(self.walk_gates_by_sequence.get(session.sequence_index, []))
        return _cell_telemetry()


def _make_sequencer(
    tmp_path: Path,
    *,
    runner: FakeRunner,
    on_budget: int = 1,
) -> CumulativeSequencer:
    manifest_path = tmp_path / "manifest.json"
    return CumulativeSequencer(
        manifest_path,
        runner=runner,
        roster=[
            RosterEntry(
                model="openrouter/model-a",
                role="assistant",
                provider_pin="openrouter",
                config_identity={"slot": 1},
            )
        ],
        seed=17,
        task="backgammon",
        org_id="org-sequencer-test",
        config_fingerprint="cfg-sequencer-test",
        on_budget=on_budget,
    )


def _failing_walk_gate(*, ordinal: int = 2) -> WalkGateVerdictRecord:
    return WalkGateVerdictRecord(
        ordinal=ordinal,
        gate=WalkGateName.WORK.value,
        verdict=WalkGateVerdict.FAIL.value,
        evidence={"full_green": False},
        expected_producer_model_ids=("openrouter/model-a", "openrouter/model-b"),
        observed_producer_model_ids=("openrouter/model-a",),
    )


def _manifest_records(manifest_path: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return list(manifest["session_records"])


def test_step_until_done_walks_all_sessions_to_done(tmp_path: Path) -> None:
    runner = FakeRunner()
    sequencer = _make_sequencer(tmp_path, runner=runner)

    done = sequencer.step_until_done()

    # Every scheduled session (OFF baseline cell, then ON cell) walked.
    assert runner.phase_trace == [
        "PREPARE_FIXTURE",
        "RUN_SESSION",
        "PREPARE_FIXTURE",
        "RUN_SESSION",
    ]
    assert runner.run_sequence_indexes == [0, 1]

    # Done state with a convergence trend sourced via the scorecard fallback
    # (no run artifacts are published by this fake runner).
    assert done["status"] == "done"
    convergence = done["convergence"]
    assert isinstance(convergence, dict)
    assert convergence["sessions_completed"] == 2
    assert isinstance(convergence["trend_hash"], str)
    assert len(convergence["trend_hash"]) == 8
    int(convergence["trend_hash"], 16)

    # Run is finished: no current session, DONE phase, totals drained.
    assert sequencer.current_session() is None
    state = sequencer.state()
    assert state["phase"] == "DONE"
    assert state["totals"] == {"sessions": 2, "completed": 2, "remaining": 0}

    # Every checkpointed session record reached DONE through the walk gate.
    records = _manifest_records(tmp_path / "manifest.json")
    assert [record["phase"] for record in records] == ["DONE", "DONE"]
    assert all(record.get("complete_gate") is True for record in records)


def test_stage_machine_has_no_extract_review_commit_or_index_ready_stages(
    tmp_path: Path,
) -> None:
    # The phase vocabulary itself collapsed to the measurement-only walk.
    assert {phase.value for phase in SessionPhase} == {
        "PREPARE_FIXTURE",
        "RUN_SESSION",
        "HALTED_ON_GATE",
        "DONE",
    }

    runner = FakeRunner()
    sequencer = _make_sequencer(tmp_path, runner=runner)
    done = sequencer.step_until_done()
    assert done["status"] == "done"

    # Only walking phases were ever observed on the session seam, and the
    # checkpointed records carry no deleted stage (extract/review/commit/
    # index_ready) in any form.
    assert set(runner.phase_trace) <= {"PREPARE_FIXTURE", "RUN_SESSION"}
    assert {record["phase"] for record in _manifest_records(tmp_path / "manifest.json")} == {
        "DONE"
    }

    # The SessionRunner seam is exactly prepare_fixture + run_session.
    assert isinstance(runner, SessionRunner)

    class _MissingRunSession:
        def prepare_fixture(self, session: SessionRecord) -> None:
            return None

    assert not isinstance(_MissingRunSession(), SessionRunner)
    with pytest.raises(ValueError, match="runner must implement SessionRunner"):
        CumulativeSequencer(
            tmp_path / "manifest-bad-runner.json",
            runner=_MissingRunSession(),  # type: ignore[arg-type]
            roster=[
                RosterEntry(
                    model="openrouter/model-a",
                    role="assistant",
                    provider_pin="openrouter",
                    config_identity={"slot": 1},
                )
            ],
            seed=17,
            task="backgammon",
            org_id="org-sequencer-test",
            config_fingerprint="cfg-sequencer-test",
            on_budget=1,
        )


def test_failing_walk_gate_halts_on_gate_and_is_resume_safe(tmp_path: Path) -> None:
    runner = FakeRunner(walk_gates_by_sequence={0: [_failing_walk_gate()]})
    sequencer = _make_sequencer(tmp_path, runner=runner)

    halted = sequencer.step_until_done()

    assert halted["status"] == "halted_on_gate"
    assert halted["phase"] == "HALTED_ON_GATE"
    assert halted["sequence_index"] == 0
    assert halted["ordinal"] == 2
    assert halted["model"] == "openrouter/model-a"
    assert halted["gate"] == WalkGateName.WORK.value
    assert halted["verdict"] == WalkGateVerdict.FAIL.value
    assert halted["evidence"] == {"full_green": False}
    assert halted["expected_producer_model_ids"] == [
        "openrouter/model-a",
        "openrouter/model-b",
    ]
    assert halted["observed_producer_model_ids"] == ["openrouter/model-a"]

    # The halted session is checkpointed in the HALTED_ON_GATE side-state.
    records = _manifest_records(tmp_path / "manifest.json")
    assert records[0]["phase"] == "HALTED_ON_GATE"
    current = sequencer.current_session()
    assert current is not None
    assert current.phase == "HALTED_ON_GATE"

    # Resume-safe: stepping again returns the SAME descriptor and re-runs
    # nothing (no prepare_fixture, no run_session, no leader calls exist).
    resumed = sequencer.step_until_done()
    assert resumed == halted
    assert runner.prepare_calls == 1
    assert runner.run_calls == 1


def test_gate_halt_on_later_session_stops_the_walk(tmp_path: Path) -> None:
    runner = FakeRunner(walk_gates_by_sequence={1: [_failing_walk_gate(ordinal=4)]})
    sequencer = _make_sequencer(tmp_path, runner=runner)

    halted = sequencer.step_until_done()

    # Session 0 completed fully before session 1 halted the walk.
    assert runner.phase_trace == [
        "PREPARE_FIXTURE",
        "RUN_SESSION",
        "PREPARE_FIXTURE",
        "RUN_SESSION",
    ]
    assert halted["status"] == "halted_on_gate"
    assert halted["sequence_index"] == 1
    assert halted["ordinal"] == 4

    records = _manifest_records(tmp_path / "manifest.json")
    assert records[0]["phase"] == "DONE"
    assert records[1]["phase"] == "HALTED_ON_GATE"


def test_step_until_done_is_resume_safe_after_run_session_crash(tmp_path: Path) -> None:
    crashing = FakeRunner(crash_run_sequence_index=1)
    sequencer = _make_sequencer(tmp_path, runner=crashing)

    with pytest.raises(RuntimeError, match="simulated crash during session 1"):
        sequencer.step_until_done()

    # Session 0 completed and advanced; session 1 is checkpointed mid-walk at
    # RUN_SESSION (prepare_fixture already done, run_session died).
    records = _manifest_records(tmp_path / "manifest.json")
    assert records[0]["phase"] == "DONE"
    assert records[1]["phase"] == "RUN_SESSION"

    # A fresh sequencer resumes from the checkpoint: session 1 goes straight
    # back to RUN_SESSION (no re-prepare, no re-run of session 0) and the run
    # walks to DONE.
    resuming = FakeRunner()
    resumed_sequencer = _make_sequencer(tmp_path, runner=resuming)
    done = resumed_sequencer.step_until_done()

    assert done["status"] == "done"
    assert resuming.phase_trace == ["RUN_SESSION"]
    assert resuming.run_sequence_indexes == [1]
    assert resumed_sequencer.current_session() is None
