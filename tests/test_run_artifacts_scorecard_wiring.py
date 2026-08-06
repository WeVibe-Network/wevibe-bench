"""WO-RUNSTATUS-1 chunk B2: scorecard wiring + extraction-attempt observability.

Exercises the REAL ``CumulativeSequencer`` with a FAKE ``SessionRunner`` +
FAKE ``LeaderClient`` + real ``PrivateReviewCard`` against a real tmp manifest
path, to prove:

1. ``_done_state`` sources standings from ``build_scorecard`` (run-manifest +
   status stream only) and the run artifacts are created alongside the mutable
   manifest.
2. An ``extraction`` stream record with ``extraction_state=="invoked_completed"``
   is appended after the EXTRACT phase.
3. A gate-halted session appends ``extraction_state=="never_invoked"``.
4. A cut-off (non-no-candidate RuntimeError from ``extract``) appends an
   ``extraction_state=="invoked_cut_off"`` record AND re-raises (behaviour
   unchanged).
5. ``_done_state`` falls back to the mutable manifest when the run-manifest /
   status stream are missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wevibe_bench.cumulative.catalog import PrivateCatalog, PrivateReviewCard
from wevibe_bench.cumulative.decision import VERIFY
from wevibe_bench.cumulative.leader_client import LeaderClient
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
from wevibe_bench.cumulative.run_artifacts import (
    RunManifest,
    StatusStream,
    build_scorecard,
    default_run_manifest_path,
    default_status_stream_path,
    write_run_manifest,
)
from wevibe_bench.cumulative.types import (
    RosterEntry,
    SessionRecord,
    WalkGateName,
    WalkGateVerdict,
    WalkGateVerdictRecord,
)


class FakeLeader:
    ed_pubkey_hex = "f00df00d"

    @staticmethod
    def ed_pub_fp() -> str:
        return "leaderfp8"


class FakeM2Proof:
    def leader_verify_and_commit(
        self,
        org_id: str,
        submission_hash: str,
        keywords: list[str],
        producer_model_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "cid": f"cid-{submission_hash}",
            "commit_status": {
                "submissions": [
                    {
                        "submission_hash": submission_hash,
                        "status": "committed",
                        "cid": f"cid-{submission_hash}",
                    }
                ]
            },
        }


class FakeHubClient:
    def deny_submission(
        self,
        leader: Any,
        org_id: str,
        submission_hash: str,
        reason: str,
    ) -> dict[str, str]:
        return {"status": "denied"}


class FakeRunner:
    def __init__(
        self,
        *,
        manifest_path: Path,
        halt_on_gate: bool = False,
        extract_error: Exception | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self.halt_on_gate = halt_on_gate
        self.extract_error = extract_error

    def prepare_fixture(self, session: SessionRecord) -> None:
        return None

    def run_session(self, session: SessionRecord) -> dict[str, Any]:
        if self.halt_on_gate:
            session.walk_gates = [
                WalkGateVerdictRecord(
                    ordinal=2,
                    gate=WalkGateName.WORK.value,
                    verdict=WalkGateVerdict.FAIL.value,
                    evidence={"full_green": False},
                    expected_producer_model_ids=("model-a", "model-b"),
                    observed_producer_model_ids=("model-a",),
                )
            ]
        else:
            session.walk_gates = []
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

    def extract(self, session: SessionRecord) -> dict[str, Any]:
        if self.extract_error is not None:
            raise self.extract_error
        index = session.sequence_index
        return {
            "candidate_refs": [
                {
                    "submission_hash": f"sub-{index}-a",
                    "text": f"alpha synthetic memory {index}",
                    "keywords": ["alpha", "backgammon"],
                    "memory_type": "memory",
                    "producer_model": "model-a",
                }
            ],
            "extraction_job_id": f"job-{index}",
            "session_id": f"session-{index}",
            "extraction_candidate_count": 1,
        }

    def index_ready(self, session: SessionRecord) -> bool:
        return True


def _make_sequencer(
    tmp_path: Path,
    *,
    runner: FakeRunner,
) -> CumulativeSequencer:
    manifest_path = tmp_path / "manifest.json"
    catalog = PrivateCatalog(str(tmp_path / "private.catalog.jsonl"))
    review_card = PrivateReviewCard(str(tmp_path / "private.review.jsonl"))
    leader_client = LeaderClient(
        FakeM2Proof(),
        FakeHubClient(),
        FakeLeader(),
        catalog,
        safe_ledger_path=str(tmp_path / "safe-ledger.jsonl"),
        idempotency_ledger_path=str(tmp_path / "idempotency.json"),
        review_card=review_card,
    )
    return CumulativeSequencer(
        manifest_path,
        runner=runner,
        leader_client=leader_client,
        review_card=review_card,
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


def _decision_payload(
    session: SessionRecord,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "manifest_id": f"decision-{session.sequence_index}",
        "created_at": "2026-08-05T12:00:00Z",
        "sequence_index": session.sequence_index,
        "org_id": session.org_id,
        "coordinator_identity": "coordinator-test",
        "integrity": {
            "job_id": session.extraction_job_id,
            "session_fp": session.session_fp,
            "resolved_problem_count": 1,
            "emitted_memory_count": 1,
            "invariant_violation": False,
            "integrity_record_seen": True,
            "log_path": "/tmp/fake.integrity.log",
        },
        "candidates": [
            {
                "candidate_ref": str(session.candidate_refs[0]["submission_hash"]),
                "verdict": VERIFY,
                "reason": "valuable memory",
                "evidence": {},
                "duplicate_refs": [],
            }
        ],
    }


def _extraction_records(tmp_path: Path) -> list[dict[str, Any]]:
    stream_path = default_status_stream_path(tmp_path / "manifest.json")
    return [
        record
        for record in StatusStream(stream_path).records()
        if record.get("type") == "extraction"
    ]


def _simulate_b1_artifacts(tmp_path: Path, sequencer: CumulativeSequencer) -> None:
    """Write the B1 runtime artifacts a real ``RealSessionRunner`` produces.

    In a real run B1's ``RealSessionRunner`` writes the write-once run-manifest
    and the terminal per-attempt record (with ``progress`` == the cell's final
    ProgressVector). With a FAKE runner those writes never happen, so we write
    them here to exercise the B2 scorecard wiring against real artifacts.
    """
    session = sequencer.current_session()
    assert session is not None

    run_manifest_path = default_run_manifest_path(tmp_path / "manifest.json")
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

    StatusStream(default_status_stream_path(tmp_path / "manifest.json")).append(
        {
            "type": "attempt",
            "schema_version": 1,
            "sequence_index": session.sequence_index,
            "memory_mode": str(session.memory_mode),
            "org_id": str(session.org_id),
            "progress": dict(session.progress),
            "session_fp": str(session.session_fp),
            "session_id": session.session_id,
        }
    )


def test_done_state_sources_scorecard_and_creates_artifacts(tmp_path: Path) -> None:
    runner = FakeRunner(manifest_path=tmp_path / "manifest.json")
    sequencer = _make_sequencer(tmp_path, runner=runner)

    paused = sequencer.step_until_review()
    assert paused["status"] == "awaiting_extract"
    paused = sequencer.extract_current()
    assert paused["status"] == "awaiting_coordinator_review"

    # The status stream is created as a sibling of the mutable manifest (by the
    # B2 extraction record append). The run-manifest is a B1 artifact written by
    # RealSessionRunner; simulate it so the scorecard path can be exercised.
    assert default_run_manifest_path(tmp_path / "manifest.json") == str(
        tmp_path / "manifest.run-manifest.json"
    )
    assert Path(default_status_stream_path(tmp_path / "manifest.json")).is_file()

    _simulate_b1_artifacts(tmp_path, sequencer)
    assert Path(default_run_manifest_path(tmp_path / "manifest.json")).is_file()

    # Call _done_state directly (the schedule has multiple cells, so stepping to
    # DONE would need every cell committed; the scorecard wiring is what we're
    # verifying here).
    done = sequencer._done_state()
    assert done["status"] == "done"

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


def test_extraction_completed_record_appended(tmp_path: Path) -> None:
    runner = FakeRunner(manifest_path=tmp_path / "manifest.json")
    sequencer = _make_sequencer(tmp_path, runner=runner)

    paused = sequencer.step_until_review()
    assert paused["status"] == "awaiting_extract"
    paused = sequencer.extract_current()
    assert paused["status"] == "awaiting_coordinator_review"

    records = _extraction_records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["type"] == "extraction"
    assert rec["schema_version"] == 1
    assert rec["extraction_state"] == "invoked_completed"
    assert rec["extraction_candidate_count"] == 1
    assert rec["sequence_index"] == 0
    assert rec["memory_mode"] == "off"  # roster default memory_mode
    assert rec["org_id"] == "org-wiring-test"
    assert rec["extraction_error"] is None
    assert rec["session_fp"] == session_fp_of("session-0")
    assert rec["session_id"] == "session-0"


def session_fp_of(session_id: str) -> str:
    return SessionRecord.session_fp_of(session_id)


def test_never_invoked_record_on_gate_halt(tmp_path: Path) -> None:
    runner = FakeRunner(
        manifest_path=tmp_path / "manifest.json",
        halt_on_gate=True,
    )
    sequencer = _make_sequencer(tmp_path, runner=runner)

    halted = sequencer.step_until_review()
    assert halted["status"] == "halted_on_gate"

    records = _extraction_records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["extraction_state"] == "never_invoked"
    assert rec["extraction_candidate_count"] is None
    assert rec["sequence_index"] == 0
    assert rec["extraction_error"] is None


def test_cut_off_record_appended_and_reraises(tmp_path: Path) -> None:
    runner = FakeRunner(
        manifest_path=tmp_path / "manifest.json",
        extract_error=RuntimeError("extract timed out after 120s"),
    )
    sequencer = _make_sequencer(tmp_path, runner=runner)

    pending = sequencer.step_until_review()
    assert pending["status"] == "awaiting_extract"
    with pytest.raises(RuntimeError, match="extract timed out"):
        sequencer.extract_current()

    records = _extraction_records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["extraction_state"] == "invoked_cut_off"
    assert rec["extraction_candidate_count"] is None
    assert "extract timed out" in rec["extraction_error"]
    assert rec["sequence_index"] == 0


def test_done_state_falls_back_when_artifacts_missing(tmp_path: Path) -> None:
    runner = FakeRunner(manifest_path=tmp_path / "manifest.json")
    sequencer = _make_sequencer(tmp_path, runner=runner)

    # Artifacts deliberately absent: run-manifest + status stream never created.
    assert not Path(default_run_manifest_path(tmp_path / "manifest.json")).exists()
    assert not Path(default_status_stream_path(tmp_path / "manifest.json")).exists()

    # _done_state must fall back to the mutable manifest without raising.
    done = sequencer._done_state()
    assert done["status"] == "done"
    assert isinstance(done["convergence"], dict)
    assert "trend_hash" in done["convergence"]
    assert "sessions_completed" in done["convergence"]