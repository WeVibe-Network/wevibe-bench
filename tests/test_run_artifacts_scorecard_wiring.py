"""WO-STRIP-2a chunk C9c: scorecard wiring against a real tmp manifest.

Exercises the REAL ``CumulativeSequencer`` with a FAKE ``SessionRunner``
against a real tmp manifest path. The campaign is measurement-only now — every
session walks PREPARE_FIXTURE -> RUN_SESSION and there are no extract,
coordinator-review, leader-commit, or index-ready stages — so the fake runner
publishes the same write-once run-manifest + append-only attempt records the
real ``RealSessionRunner`` writes. This proves:

1. ``step_until_done``'s done state sources standings from ``build_scorecard``
   (run-manifest + status stream only) and the run artifacts are created
   alongside the mutable manifest.
2. The done state falls back to the mutable manifest when the run-manifest /
   status stream are missing.

The WO-NIGHT2-1a chunk C (3-state scorecard) and WO-NIGHT2-1b chunk 2
(VOID-INSTRUMENT classification) tests below exercise ``build_scorecard``
directly on the real surface and are kept unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wevibe_bench.cumulative.progress import progress_from_cell_result
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
from wevibe_bench.cumulative.run_artifacts import (
    RunManifest,
    StatusStream,
    build_scorecard,
    default_run_manifest_path,
    default_status_stream_path,
    write_run_manifest,
)
from wevibe_bench.cumulative.types import RosterEntry, SessionRecord


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
    """SessionRunner double that publishes the run artifacts like the real runner.

    In a real run ``RealSessionRunner`` writes the write-once run-manifest and
    the terminal per-attempt record (with ``progress`` == the cell's final
    ProgressVector). ``emit_artifacts=False`` simulates a run that died before
    publishing anything, exercising the done-state fallback.
    """

    def __init__(self, *, manifest_path: Path, emit_artifacts: bool = True) -> None:
        self.manifest_path = manifest_path
        self.emit_artifacts = emit_artifacts

    def prepare_fixture(self, session: SessionRecord) -> None:
        return None

    def run_session(self, session: SessionRecord) -> dict[str, Any]:
        telemetry = _cell_telemetry()
        if self.emit_artifacts:
            self._publish_run_manifest_once(session)
            self._append_attempt_record(session, telemetry)
        return telemetry

    def _publish_run_manifest_once(self, session: SessionRecord) -> None:
        run_manifest_path = default_run_manifest_path(self.manifest_path)
        if Path(run_manifest_path).exists():
            return
        write_run_manifest(
            run_manifest_path,
            RunManifest(
                run_id="run-wiring-test",
                created_at="2026-08-05T12:00:00Z",
                served_model=None,
                requested_model=str(session.model),
                memory_mode=str(session.memory_mode),
                org_id=str(session.org_id),
            ),
        )

    def _append_attempt_record(
        self, session: SessionRecord, telemetry: dict[str, Any]
    ) -> None:
        StatusStream(default_status_stream_path(self.manifest_path)).append(
            {
                "type": "attempt",
                "schema_version": 1,
                "sequence_index": session.sequence_index,
                "memory_mode": str(session.memory_mode),
                "org_id": str(session.org_id),
                "progress": progress_from_cell_result(telemetry).to_dict(),
                "session_fp": str(session.session_fp),
                "session_id": session.session_id,
            }
        )


def _make_sequencer(
    tmp_path: Path,
    *,
    runner: FakeRunner,
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
        org_id="org-wiring-test",
        config_fingerprint="cfg-wiring-test",
        on_budget=1,
    )


def test_done_state_sources_scorecard_and_creates_artifacts(tmp_path: Path) -> None:
    runner = FakeRunner(manifest_path=tmp_path / "manifest.json")
    sequencer = _make_sequencer(tmp_path, runner=runner)

    # Drive the collapsed stage machine to completion; the fake runner
    # publishes the run-manifest + attempt records a real run writes.
    done = sequencer.step_until_done()
    assert done["status"] == "done"

    # The run artifacts sit as siblings of the mutable manifest: the
    # write-once run-manifest and the append-only status stream.
    assert default_run_manifest_path(tmp_path / "manifest.json") == str(
        tmp_path / "manifest.run-manifest.json"
    )
    assert Path(default_run_manifest_path(tmp_path / "manifest.json")).is_file()
    assert Path(default_status_stream_path(tmp_path / "manifest.json")).is_file()

    convergence = done["convergence"]
    assert convergence["sessions_completed"] >= 1
    assert isinstance(convergence["trend_hash"], str)
    assert len(convergence["trend_hash"]) == 8
    int(convergence["trend_hash"], 16)

    # Convergence equals the scorecard's reconstructed trend (built from the
    # run-manifest + status stream only).
    scorecard = build_scorecard(tmp_path / "manifest.json")
    assert scorecard["convergence"] == convergence

    # The scorecard reads the attempt record's terminal progress (equal to the
    # cell's final progress stored in the mutable manifest), so the standings
    # match despite the mutable manifest never being read by the scorecard.
    assert scorecard["stream_records"] >= 1
    assert scorecard["scored_sessions"] >= 1


def test_done_state_falls_back_when_artifacts_missing(tmp_path: Path) -> None:
    runner = FakeRunner(manifest_path=tmp_path / "manifest.json", emit_artifacts=False)
    sequencer = _make_sequencer(tmp_path, runner=runner)

    # Artifacts deliberately absent: run-manifest + status stream never created.
    assert not Path(default_run_manifest_path(tmp_path / "manifest.json")).exists()
    assert not Path(default_status_stream_path(tmp_path / "manifest.json")).exists()

    # The done state must fall back to the mutable manifest without raising.
    done = sequencer.step_until_done()
    assert done["status"] == "done"
    assert isinstance(done["convergence"], dict)
    assert "trend_hash" in done["convergence"]
    assert "sessions_completed" in done["convergence"]


# --- WO-NIGHT2-1a chunk C: 3-state scorecard (scored-pass / scored-fail / ---
# --- not-scored-with-reason) via the fail-closed delivery gate.         ---


def _write_scored_attempt(
    stream: StatusStream,
    *,
    sequence_index: int,
    session_fp: str,
    session_id: str,
    full_green: bool,
) -> None:
    stream.append(
        {
            "type": "attempt",
            "schema_version": 1,
            "sequence_index": sequence_index,
            "memory_mode": "on",
            "org_id": "org-1",
            "progress": {
                "problems_before": 3,
                "problems_after": 1 if full_green else 2,
                "resolved_count": 2 if full_green else 1,
                "remaining_count": 1,
                "full_green": full_green,
                "attempts_to_green": 1,
                "turns": 2,
                "total_tokens": 1000,
                "wall_seconds": 1.0,
                "wall_cost_usd": 0.0,
            },
            "session_fp": session_fp,
            "session_id": session_id,
        }
    )


def _write_delivery(
    stream: StatusStream,
    *,
    sequence_index: int,
    delivery_state: str,
    not_scored_reason: str,
    memory_mode: str = "on",
) -> None:
    stream.append(
        {
            "type": "delivery",
            "schema_version": 1,
            "sequence_index": sequence_index,
            "memory_mode": memory_mode,
            "org_id": "org-1",
            "delivery_state": delivery_state,
            "not_scored_reason": not_scored_reason,
        }
    )


def _write_scorecard_manifest(tmp_path: Path) -> Path:
    run_manifest_path = default_run_manifest_path(tmp_path / "manifest.json")
    write_run_manifest(
        run_manifest_path,
        RunManifest(
            run_id="run-chunk-c",
            created_at="2026-08-06T00:00:00Z",
            served_model=None,
            requested_model="model-a",
            memory_mode="on",
            org_id="org-1",
        ),
    )
    return tmp_path / "manifest.json"


def test_not_scored_cell_excluded_from_scorecard(tmp_path: Path) -> None:
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    # Cell 0: scored (green). Cell 1: scored (fail). Cell 2: attempt EXISTS
    # (from run_session) BUT is excluded by an unverified delivery record.
    _write_scored_attempt(stream, sequence_index=0, session_fp="fp-0", session_id="s-0", full_green=True)
    _write_scored_attempt(stream, sequence_index=1, session_fp="fp-1", session_id="s-1", full_green=False)
    _write_scored_attempt(stream, sequence_index=2, session_fp="fp-2", session_id="s-2", full_green=True)
    _write_delivery(
        stream,
        sequence_index=2,
        delivery_state="unverified",
        not_scored_reason="delivery proof absent after timeout",
        memory_mode="on",
    )

    scorecard = build_scorecard(manifest_path)

    # Cell 2 is EXCLUDED from the scored set despite its attempt record.
    assert scorecard["scored_sessions"] == 2
    assert scorecard["stream_records"] == 4
    assert [p["sequence_index"] for p in scorecard["convergence"]["points"]] == [0, 1]

    # Distinct not-scored-with-reason outcome carries the reason.
    assert scorecard["not_scored"] == [
        {
            "sequence_index": 2,
            "memory_mode": "on",
            "not_scored_reason": "delivery proof absent after timeout",
        }
    ]

    # scored-pass / scored-fail reflect the reduced scored set only.
    assert scorecard["scored_pass"] == 1  # cell 0 green
    assert scorecard["scored_fail"] == 1  # cell 1 fail


def test_no_delivery_record_scores_all_as_today(tmp_path: Path) -> None:
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    _write_scored_attempt(stream, sequence_index=0, session_fp="fp-0", session_id="s-0", full_green=True)
    _write_scored_attempt(stream, sequence_index=1, session_fp="fp-1", session_id="s-1", full_green=False)

    scorecard = build_scorecard(manifest_path)

    # No behavior change: all attempt cells are scored, not_scored is empty.
    assert scorecard["scored_sessions"] == 2
    assert scorecard["not_scored"] == []
    assert scorecard["scored_pass"] == 1
    assert scorecard["scored_fail"] == 1
    assert scorecard["convergence"]["sessions_completed"] == 2


def test_verified_delivery_does_not_exclude(tmp_path: Path) -> None:
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    _write_scored_attempt(stream, sequence_index=0, session_fp="fp-0", session_id="s-0", full_green=True)
    _write_delivery(
        stream,
        sequence_index=0,
        delivery_state="verified",
        not_scored_reason="",
    )

    scorecard = build_scorecard(manifest_path)

    # Only the fail-closed "unverified" disposition excludes a cell.
    assert scorecard["scored_sessions"] == 1
    assert scorecard["not_scored"] == []
    assert scorecard["scored_pass"] == 1
    assert scorecard["convergence"]["sessions_completed"] == 1


def test_mixed_stream_outcome_counts_consistent_with_convergence(tmp_path: Path) -> None:
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    _write_scored_attempt(stream, sequence_index=0, session_fp="fp-0", session_id="s-0", full_green=True)
    _write_scored_attempt(stream, sequence_index=1, session_fp="fp-1", session_id="s-1", full_green=False)
    _write_scored_attempt(stream, sequence_index=2, session_fp="fp-2", session_id="s-2", full_green=True)
    _write_delivery(
        stream,
        sequence_index=2,
        delivery_state="unverified",
        not_scored_reason="delivery proof absent",
    )

    scorecard = build_scorecard(manifest_path)
    convergence = scorecard["convergence"]

    reduced = scorecard["scored_sessions"]
    assert scorecard["scored_pass"] + scorecard["scored_fail"] == reduced
    assert scorecard["scored_pass"] == convergence["sessions_green"]
    assert scorecard["scored_fail"] == (
        convergence["sessions_completed"] - convergence["sessions_green"]
    )


# --- WO-NIGHT2-1b chunk 2: scorecard-level VOID-INSTRUMENT classification ---
# --- tests locking the card's contract (RUNBOOK rule 5.10). Recording-level ---
# --- assertion at test_run_cumulative_run_artifacts.py:284 is PRESERVED; the ---
# --- classification contract (truncated cell -> void_instrument, not scored) ---
# --- is asserted here end-to-end through build_scorecard on the REAL surface. ---


def _write_truncated_attempt(
    stream: StatusStream,
    *,
    sequence_index: int,
    session_fp: str,
    session_id: str,
    full_green: bool,
    memory_mode: str = "on",
    terminal_reason: str | None = None,
    length_truncations: int = 0,
    truncated_turns: int = 0,
    terminal_outcome: bool | None = None,
) -> None:
    """Mirror ``_write_scored_attempt`` but with the per-attempt truncation
    fields that the scorecard's VOID-INSTRUMENT signal reads.

    Defaults carry NO truncation signal (``terminal_reason``/``length_truncations``/
    ``truncated_turns`` absent or zero) so a caller may also use it for a plain
    non-truncated non-green cell.
    """
    record: dict[str, Any] = {
        "type": "attempt",
        "schema_version": 1,
        "sequence_index": sequence_index,
        "memory_mode": memory_mode,
        "org_id": "org-1",
        "progress": {
            "problems_before": 3,
            "problems_after": 1 if full_green else 2,
            "resolved_count": 2 if full_green else 1,
            "remaining_count": 1,
            "full_green": full_green,
            "attempts_to_green": 1,
            "turns": 2,
            "total_tokens": 1000,
            "wall_seconds": 1.0,
            "wall_cost_usd": 0.0,
        },
        "session_fp": session_fp,
        "session_id": session_id,
    }
    if terminal_reason is not None:
        record["terminal_reason"] = terminal_reason
    if length_truncations:
        record["length_truncations"] = length_truncations
    if truncated_turns:
        record["truncated_turns"] = truncated_turns
    if terminal_outcome is not None:
        record["terminal_outcome"] = terminal_outcome
    stream.append(record)


def test_truncated_cell_classified_void_instrument_not_scored(tmp_path: Path) -> None:
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    # Single cell whose terminal attempt is non-green AND died of a transport
    # truncation (full_green=False, terminal_reason="transport_incomplete",
    # truncated_turns=1) — the VOID-INSTRUMENT class per rule 5.10.
    _write_truncated_attempt(
        stream,
        sequence_index=0,
        session_fp="fp-0",
        session_id="s-0",
        full_green=False,
        terminal_reason="transport_incomplete",
        truncated_turns=1,
        terminal_outcome=False,
    )

    scorecard = build_scorecard(manifest_path)

    # Classified VOID-INSTRUMENT with the provider_truncation reason.
    assert scorecard["void_instrument"] == [
        {
            "sequence_index": 0,
            "memory_mode": "on",
            "void_reason": "provider_truncation",
        }
    ]
    assert scorecard["void_instrument"][0]["sequence_index"] == 0
    assert scorecard["void_instrument"][0]["memory_mode"] == "on"
    assert scorecard["void_instrument"][0]["void_reason"] == "provider_truncation"

    # EXCLUDED from the scored set entirely.
    assert scorecard["scored_sessions"] == 0
    assert scorecard["scored_pass"] == 0
    assert scorecard["scored_fail"] == 0
    assert scorecard["convergence"]["sessions_completed"] == 0
    # And NOT mislabelled as a delivery-gate not_scored cell.
    assert scorecard["not_scored"] == []


def test_cell_measured_blind_is_void_instrument_never_a_capability_fail(
    tmp_path: Path,
) -> None:
    """D-SERVE-MESSAGE-500 end-to-end: a blind cell must not be published as FAIL.

    On 2026-08-11 a single HTTP 500 on GET /session/{id}/message ended a cell
    32 minutes in. Gates then ran against a worktree the harness had never
    observed and returned 43 problems. Published as-is that reads as a
    capability FAIL; it is an instrument failure. This pins the distinction on
    the real scorecard surface.
    """
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    record: dict[str, Any] = {
        "type": "attempt",
        "schema_version": 1,
        "sequence_index": 0,
        "memory_mode": "off",
        "org_id": "org-1",
        "terminal_reason": "harness_error",
        # The signal the drive loop now emits when it loses the transcript.
        "observation_lost_turns": 1,
        "progress": {
            "problems_before": None,
            "problems_after": 43,
            "resolved_count": None,
            "remaining_count": 43,
            "full_green": False,
            "turns": 65,
            "total_tokens": 85635,
            "wall_seconds": 1922.87,
            "wall_cost_usd": 0.0,
        },
        "session_fp": "e5037b4b",
        "session_id": "ses_blind",
    }
    stream.append(record)

    scorecard = build_scorecard(manifest_path)

    assert scorecard["void_instrument"][0]["sequence_index"] == 0
    assert scorecard["scored_sessions"] == 0, "a blind cell is never scored"
    assert scorecard["scored_fail"] == 0, (
        "the 43 gate problems must NOT surface as a capability FAIL"
    )


def test_green_with_truncation_still_scored_pass(tmp_path: Path) -> None:
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    # Some turns truncated but the cell still reached green: NOT void, scored PASS.
    _write_truncated_attempt(
        stream,
        sequence_index=0,
        session_fp="fp-0",
        session_id="s-0",
        full_green=True,
        truncated_turns=1,
        terminal_outcome=False,
    )

    scorecard = build_scorecard(manifest_path)

    assert scorecard["void_instrument"] == []
    assert scorecard["scored_sessions"] == 1
    assert scorecard["scored_pass"] == 1
    assert scorecard["scored_fail"] == 0
    assert scorecard["convergence"]["sessions_completed"] == 1


def test_non_green_without_truncation_still_scored_fail(tmp_path: Path) -> None:
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    # Genuine capability failure: non-green terminal attempt with NO truncation
    # signal (all truncation fields absent) — still scored FAIL, not void.
    _write_truncated_attempt(
        stream,
        sequence_index=0,
        session_fp="fp-0",
        session_id="s-0",
        full_green=False,
        terminal_outcome=False,
    )

    scorecard = build_scorecard(manifest_path)

    assert scorecard["void_instrument"] == []
    assert scorecard["scored_sessions"] == 1
    assert scorecard["scored_pass"] == 0
    assert scorecard["scored_fail"] == 1
    assert scorecard["convergence"]["sessions_completed"] == 1


def test_truncation_void_symmetric_across_memory_modes(tmp_path: Path) -> None:
    manifest_path = _write_scorecard_manifest(tmp_path)
    stream = StatusStream(default_status_stream_path(manifest_path))

    # The SAME truncated cell (non-green + transport_incomplete) once under the
    # ON arm and once under the OFF arm. The classification branches on no mode
    # flag, so both land in void_instrument identically.
    _write_truncated_attempt(
        stream,
        sequence_index=0,
        session_fp="fp-0",
        session_id="s-0",
        full_green=False,
        memory_mode="on",
        terminal_reason="transport_incomplete",
        terminal_outcome=False,
    )
    _write_truncated_attempt(
        stream,
        sequence_index=1,
        session_fp="fp-1",
        session_id="s-1",
        full_green=False,
        memory_mode="off",
        terminal_reason="transport_incomplete",
        terminal_outcome=False,
    )

    scorecard = build_scorecard(manifest_path)

    # Both cells voided identically, differing only in their memory_mode label.
    assert scorecard["void_instrument"] == [
        {
            "sequence_index": 0,
            "memory_mode": "on",
            "void_reason": "provider_truncation",
        },
        {
            "sequence_index": 1,
            "memory_mode": "off",
            "void_reason": "provider_truncation",
        },
    ]
    assert scorecard["scored_sessions"] == 0
    assert scorecard["scored_pass"] == 0
    assert scorecard["scored_fail"] == 0