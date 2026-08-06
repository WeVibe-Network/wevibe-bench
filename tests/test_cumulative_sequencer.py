import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import wevibe_bench.cumulative.sequencer as sequencer_module
from wevibe_bench.cumulative.catalog import PrivateCatalog, PrivateReviewCard
from wevibe_bench.cumulative.decision import DENY_FINAL, VERIFY
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
    SessionPhase,
    SessionRecord,
    WalkGateName,
    WalkGateVerdict,
    WalkGateVerdictRecord,
)


class FakeRunner:
    def __init__(
        self,
        *,
        manifest_path: Path,
        index_ready_plan: list[bool] | None = None,
        walk_gates_by_sequence: dict[int, list[WalkGateVerdictRecord]] | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self._index_ready_plan = list(index_ready_plan or [True])
        self.walk_gates_by_sequence = walk_gates_by_sequence or {}

        self.phase_trace: list[str] = []
        self.prepare_calls = 0
        self.run_calls = 0
        self.extract_calls = 0
        self.extract_sequence_indexes: list[int] = []
        self.extract_memory_modes: list[str] = []

        self.index_ready_calls = 0
        self.index_ready_history: list[bool] = []
        self.not_ready_snapshots: list[dict[str, Any]] = []

    def prepare_fixture(self, session: SessionRecord) -> None:
        self.phase_trace.append(session.phase)
        self.prepare_calls += 1

    def run_session(self, session: SessionRecord) -> dict[str, Any]:
        self.phase_trace.append(session.phase)
        self.run_calls += 1
        session.walk_gates = list(self.walk_gates_by_sequence.get(session.sequence_index, []))
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
        self.phase_trace.append(session.phase)
        self.extract_calls += 1
        self.extract_sequence_indexes.append(session.sequence_index)
        self.extract_memory_modes.append(session.memory_mode)

        index = session.sequence_index
        return {
            "candidate_refs": [
                {
                    "submission_hash": f"sub-{index}-a",
                    "text": f"alpha synthetic memory {index}",
                    "keywords": ["alpha", "backgammon"],
                    "memory_type": "memory",
                    "producer_model": "model-a",
                },
                {
                    "submission_hash": f"sub-{index}-b",
                    "text": f"beta synthetic memory {index}",
                    "keywords": ["beta", "backgammon"],
                    "memory_type": "memory",
                    "producer_model": "model-a",
                },
            ],
            "extraction_job_id": f"job-{index}",
            "session_id": f"session-{index}",
            "extraction_candidate_count": 2,
        }

    def index_ready(self, session: SessionRecord) -> bool:
        self.index_ready_calls += 1
        ready = self._index_ready_plan.pop(0) if self._index_ready_plan else True
        self.index_ready_history.append(ready)

        if not ready and self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self.not_ready_snapshots.append(
                {
                    "current_index": manifest["current_index"],
                    "phase": manifest["session_records"][session.sequence_index]["phase"],
                }
            )

        return ready


class FakeM2Proof:
    def __init__(self) -> None:
        self.verify_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def leader_verify_and_commit(
        self,
        org_id: str,
        submission_hash: str,
        keywords: list[str],
        producer_model_id: str | None = None,
    ) -> dict[str, Any]:
        assert producer_model_id is not None
        self.verify_calls.append((org_id, submission_hash, tuple(keywords)))
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
    def __init__(self) -> None:
        self.deny_calls: list[tuple[str, str, str]] = []

    def deny_submission(
        self,
        leader: Any,
        org_id: str,
        submission_hash: str,
        reason: str,
    ) -> dict[str, str]:
        self.deny_calls.append((org_id, submission_hash, reason))
        return {"status": "denied"}


class FakeLeader:
    ed_pubkey_hex = "f00df00d"

    @staticmethod
    def ed_pub_fp() -> str:
        return "leaderfp8"


@dataclass
class Harness:
    sequencer: CumulativeSequencer
    runner: FakeRunner
    m2proof: FakeM2Proof
    hub_client: FakeHubClient
    catalog: PrivateCatalog
    manifest_path: Path


def _make_harness(
    tmp_path: Path,
    *,
    index_ready_plan: list[bool] | None = None,
    index_ready_max_polls: int = 3,
    require_delivery_verification: bool = True,
    on_budget: int = 1,
    walk_gates_by_sequence: dict[int, list[WalkGateVerdictRecord]] | None = None,
) -> Harness:
    manifest_path = tmp_path / "manifest.json"
    catalog = PrivateCatalog(str(tmp_path / "private.catalog.jsonl"))
    review_card = PrivateReviewCard(str(tmp_path / "private.review.jsonl"))

    m2proof = FakeM2Proof()
    hub_client = FakeHubClient()
    leader_client = LeaderClient(
        m2proof,
        hub_client,
        FakeLeader(),
        catalog,
        safe_ledger_path=str(tmp_path / "safe-ledger.jsonl"),
        idempotency_ledger_path=str(tmp_path / "idempotency.json"),
        review_card=review_card,
    )

    runner = FakeRunner(
        manifest_path=manifest_path,
        index_ready_plan=index_ready_plan,
        walk_gates_by_sequence=walk_gates_by_sequence,
    )

    sequencer = CumulativeSequencer(
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
        org_id="org-sequencer-test",
        config_fingerprint="cfg-sequencer-test",
        on_budget=on_budget,
        index_ready_max_polls=index_ready_max_polls,
        require_delivery_verification=require_delivery_verification,
    )

    return Harness(
        sequencer=sequencer,
        runner=runner,
        m2proof=m2proof,
        hub_client=hub_client,
        catalog=catalog,
        manifest_path=manifest_path,
    )


def _decision_payload(
    session: SessionRecord,
    *,
    candidate_verdicts: list[tuple[str, str, str]],
    integrity_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "manifest_id": f"decision-{session.sequence_index}",
        "created_at": "2026-07-23T12:00:00Z",
        "sequence_index": session.sequence_index,
        "org_id": session.org_id,
        "coordinator_identity": "coordinator-test",
        "integrity": {
            "job_id": session.extraction_job_id,
            "session_fp": session.session_fp,
            "resolved_problem_count": 1,
            "emitted_memory_count": len(candidate_verdicts),
            "invariant_violation": False,
            "integrity_record_seen": True,
            "log_path": "/tmp/fake.integrity.log",
        },
        "candidates": [
            {
                "candidate_ref": candidate_ref,
                "verdict": verdict,
                "reason": reason,
                "evidence": {},
                "duplicate_refs": [],
            }
            for candidate_ref, verdict, reason in candidate_verdicts
        ],
    }
    if integrity_override is not None:
        payload["integrity"] = integrity_override
    return payload


def _write_decision(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _await_review(harness: Harness) -> dict[str, Any]:
    pending = harness.sequencer.step_until_review()
    assert pending["status"] == "awaiting_extract"
    paused = harness.sequencer.extract_current()
    assert paused["status"] == "awaiting_coordinator_review"
    return paused


def _session_candidate_refs(session: SessionRecord) -> list[str]:
    return [str(candidate["submission_hash"]) for candidate in session.candidate_refs]


def _verify_first_candidate_and_advance(harness: Harness, tmp_path: Path) -> dict[str, Any]:
    session = harness.sequencer.current_session()
    assert session is not None
    first_ref = _session_candidate_refs(session)[0]
    decision_path = _write_decision(
        tmp_path / f"decision-session-{session.sequence_index}.json",
        _decision_payload(
            session,
            candidate_verdicts=[(first_ref, VERIFY, "valuable memory")],
        ),
    )
    return harness.sequencer.resume_with_decision(decision_path)


def _failing_walk_gate(*, ordinal: int = 2) -> WalkGateVerdictRecord:
    return WalkGateVerdictRecord(
        ordinal=ordinal,
        gate=WalkGateName.WORK.value,
        verdict=WalkGateVerdict.FAIL.value,
        evidence={"full_green": False},
        expected_producer_model_ids=("openrouter/model-a", "openrouter/model-b"),
        observed_producer_model_ids=("openrouter/model-a",),
    )


def _not_evaluated_walk_gate(*, ordinal: int = 3) -> WalkGateVerdictRecord:
    return WalkGateVerdictRecord.not_evaluated(
        ordinal=ordinal,
        gate=WalkGateName.LIFT,
        evidence={"reason": "earlier gate failed"},
    )


def test_step_until_review_phase_walk_stops_before_leader_calls(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    pending = harness.sequencer.step_until_review()
    assert pending["status"] == "awaiting_extract"
    assert pending["sequence_index"] == 0
    assert harness.runner.phase_trace == ["PREPARE_FIXTURE", "RUN_SESSION"]

    paused = harness.sequencer.extract_current()
    assert paused["status"] == "awaiting_coordinator_review"
    assert harness.runner.phase_trace == [
        "PREPARE_FIXTURE",
        "RUN_SESSION",
        "EXTRACT_NORMAL_PIPELINE",
    ]
    assert harness.m2proof.verify_calls == []
    assert harness.hub_client.deny_calls == []

    manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    assert manifest["session_records"][0]["phase"] == "AWAIT_COORDINATOR_REVIEW"




def test_resume_with_decision_commits_one_denies_one_and_advances(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, index_ready_plan=[True])
    _await_review(harness)

    session = harness.sequencer.current_session()
    assert session is not None
    first_ref, second_ref = _session_candidate_refs(session)

    decision_payload = _decision_payload(
        session,
        candidate_verdicts=[
            (first_ref, VERIFY, "valuable memory"),
            (second_ref, DENY_FINAL, "duplicate"),
        ],
    )
    decision_path = _write_decision(tmp_path / "decision-verify-deny.json", decision_payload)

    resumed = harness.sequencer.resume_with_decision(decision_path)

    assert resumed["status"] == "session_committed"
    assert len(resumed["committed_ids"]) == 1
    assert resumed["denied_refs"] == [second_ref]
    assert resumed["all_denied"] is False
    assert resumed["next_index"] == 1
    assert len(harness.m2proof.verify_calls) == 1
    assert len(harness.hub_client.deny_calls) == 1


def test_resume_with_decision_all_deny_advances_without_abort(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, index_ready_plan=[True])
    _await_review(harness)

    session = harness.sequencer.current_session()
    assert session is not None
    first_ref, second_ref = _session_candidate_refs(session)

    decision_payload = _decision_payload(
        session,
        candidate_verdicts=[
            (first_ref, DENY_FINAL, "not useful"),
            (second_ref, DENY_FINAL, "duplicate"),
        ],
    )
    decision_path = _write_decision(tmp_path / "decision-all-deny.json", decision_payload)

    resumed = harness.sequencer.resume_with_decision(decision_path)

    assert resumed["status"] == "session_committed"
    assert resumed["committed_ids"] == []
    assert set(resumed["denied_refs"]) == {first_ref, second_ref}
    assert resumed["all_denied"] is True
    assert resumed["next_index"] == 1
    assert harness.m2proof.verify_calls == []
    assert len(harness.hub_client.deny_calls) == 2




def test_private_catalog_is_cumulative_across_sessions(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, index_ready_plan=[True])
    _await_review(harness)

    session0 = harness.sequencer.current_session()
    assert session0 is not None
    first_ref, second_ref = _session_candidate_refs(session0)

    decision_payload = _decision_payload(
        session0,
        candidate_verdicts=[
            (first_ref, VERIFY, "valuable memory"),
            (second_ref, DENY_FINAL, "duplicate"),
        ],
    )
    decision_path = _write_decision(tmp_path / "decision-session-0.json", decision_payload)

    harness.sequencer.resume_with_decision(decision_path)
    paused_session_1 = _await_review(harness)

    assert paused_session_1["sequence_index"] == 1
    catalog_records = harness.catalog.load()
    assert [record.submission_hash for record in catalog_records] == [first_ref]


def test_step_until_review_is_resume_safe_at_await(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    first_pause = _await_review(harness)
    extract_calls_before = harness.runner.extract_calls
    run_calls_before = harness.runner.run_calls

    # Idempotent: extract_current again at AWAIT_COORDINATOR_REVIEW re-extracts nothing.
    second_pause = harness.sequencer.extract_current()
    assert second_pause == first_pause
    assert harness.runner.extract_calls == extract_calls_before

    # Resume-safe: step_until_review at AWAIT_COORDINATOR_REVIEW does not re-run run_session.
    third_pause = harness.sequencer.step_until_review()
    assert third_pause == first_pause
    assert harness.runner.run_calls == run_calls_before


@pytest.mark.parametrize("case", ["absent", "uncorrelatable"])
def test_manifest_contract_gate_requires_correlatable_integrity_attestation(
    tmp_path: Path,
    case: str,
) -> None:
    harness = _make_harness(tmp_path, index_ready_plan=[True])
    _await_review(harness)

    session = harness.sequencer.current_session()
    assert session is not None
    first_ref = _session_candidate_refs(session)[0]

    decision_payload = _decision_payload(
        session,
        candidate_verdicts=[
            (first_ref, VERIFY, "valuable memory"),
        ],
    )
    if case == "absent":
        decision_payload.pop("integrity")
    else:
        decision_payload["integrity"] = {
            "job_id": "wrong-job-id",
            "session_fp": "deadbeef",
            "resolved_problem_count": 1,
            "emitted_memory_count": 1,
            "invariant_violation": False,
            "integrity_record_seen": True,
            "log_path": "/tmp/fake.integrity.log",
        }

    decision_path = _write_decision(tmp_path / f"decision-gate-{case}.json", decision_payload)

    with pytest.raises(ValueError, match="integrity"):
        harness.sequencer.resume_with_decision(decision_path)

    assert harness.m2proof.verify_calls == []
    assert harness.hub_client.deny_calls == []




def test_resume_with_decision_unverified_delivery_is_fail_closed_and_advances(
    tmp_path: Path,
) -> None:
    """A delivery that never becomes ready within the bounded poll must not
    hang, must append a fail-closed `delivery`/`unverified` status record, and
    must still advance the run to the next session."""
    harness = _make_harness(
        tmp_path,
        index_ready_plan=[False] * 5,
        index_ready_max_polls=3,
    )
    _await_review(harness)

    session = harness.sequencer.current_session()
    assert session is not None
    first_ref = _session_candidate_refs(session)[0]
    decision_path = _write_decision(
        tmp_path / f"decision-unverified-{session.sequence_index}.json",
        _decision_payload(
            session,
            candidate_verdicts=[(first_ref, VERIFY, "valuable memory")],
        ),
    )

    resumed = harness.sequencer.resume_with_decision(decision_path)

    # (c) still advances to NEXT_SESSION — session committed result returned.
    assert resumed["status"] == "session_committed"
    assert resumed["next_index"] == 1

    # (a) bounded loop: exactly max_polls index_ready calls, no hang.
    assert harness.runner.index_ready_calls == 3

    # (b) fail-closed delivery record with a not_scored_reason appended.
    stream = StatusStream(default_status_stream_path(harness.manifest_path))
    delivery_records = [r for r in stream.records() if r.get("type") == "delivery"]
    assert len(delivery_records) == 1
    record = delivery_records[0]
    assert record["type"] == "delivery"
    assert record["schema_version"] == 1
    assert record["sequence_index"] == 0
    assert record["memory_mode"] == "off"
    assert record["org_id"] == "org-sequencer-test"
    assert record["delivery_state"] == "unverified"
    assert record["not_scored_reason"] == "delivery_unverified_after_3_polls"


def test_resume_with_decision_verified_delivery_records_no_disposition(
    tmp_path: Path,
) -> None:
    """A delivery that IS verified within the bound records no `delivery`
    status record (absence = scored normally)."""
    harness = _make_harness(tmp_path, index_ready_plan=[True], index_ready_max_polls=3)
    _await_review(harness)

    session = harness.sequencer.current_session()
    assert session is not None
    first_ref = _session_candidate_refs(session)[0]
    decision_path = _write_decision(
        tmp_path / f"decision-verified-{session.sequence_index}.json",
        _decision_payload(
            session,
            candidate_verdicts=[(first_ref, VERIFY, "valuable memory")],
        ),
    )

    resumed = harness.sequencer.resume_with_decision(decision_path)
    assert resumed["status"] == "session_committed"

    stream = StatusStream(default_status_stream_path(harness.manifest_path))
    assert [r for r in stream.records() if r.get("type") == "delivery"] == []


def test_resume_with_decision_unverified_but_gate_disabled_has_no_disposition(
    tmp_path: Path,
) -> None:
    """When the operator disables delivery verification, an unverified
    delivery still exits the bounded poll without hanging and proceeds to the
    next session, but records no disposition (cell scored normally)."""
    harness = _make_harness(
        tmp_path,
        index_ready_plan=[False] * 5,
        index_ready_max_polls=3,
        require_delivery_verification=False,
    )
    _await_review(harness)

    session = harness.sequencer.current_session()
    assert session is not None
    first_ref = _session_candidate_refs(session)[0]
    decision_path = _write_decision(
        tmp_path / f"decision-unverified-gate-off-{session.sequence_index}.json",
        _decision_payload(
            session,
            candidate_verdicts=[(first_ref, VERIFY, "valuable memory")],
        ),
    )

    resumed = harness.sequencer.resume_with_decision(decision_path)
    assert resumed["status"] == "session_committed"
    assert resumed["next_index"] == 1
    assert harness.runner.index_ready_calls == 3

    stream = StatusStream(default_status_stream_path(harness.manifest_path))
    assert [r for r in stream.records() if r.get("type") == "delivery"] == []


# --- WO-NIGHT2-1a chunk D: EXERCISED-PATH integration proof. The sequencer ---
# --- (real) WRITES the fail-closed delivery record to a real status stream, ---
# --- and the SAME real stream feeds build_scorecard — proving no hang, the ---
# --- cell is excluded from the scored set, not-scored-with-reason is        ---
# --- reported, and the run advances to the next session.                    ---


def test_delivery_failure_marks_cell_not_scored_end_to_end(tmp_path: Path) -> None:
    """EXERCISED-PATH integration proof (not a unit test).

    Drives the REAL ``CumulativeSequencer`` through the delivery-failure path
    with ``require_delivery_verification=True``: the sequencer WRITES a
    fail-closed ``delivery``/``unverified`` record to the real status stream,
    advances to the next session without hanging (wall-clock-measured), and
    ``build_scorecard`` — reading that SAME real stream + the write-once
    RunManifest — excludes the ON cell from the scored set and reports
    not-scored-with-reason.
    """
    harness = _make_harness(
        tmp_path,
        index_ready_plan=[False] * 5,
        index_ready_max_polls=3,
        require_delivery_verification=True,
        on_budget=1,
    )
    _await_review(harness)

    session = harness.sequencer.current_session()
    assert session is not None

    # Write the write-once RunManifest the scorecard reads (a B1 artifact the
    # FAKE runner never creates; mirrors _write_scorecard_manifest in the
    # wiring test but matched to THIS harness's identity).
    write_run_manifest(
        default_run_manifest_path(harness.manifest_path),
        RunManifest(
            run_id="run-chunk-d",
            created_at="2026-08-06T00:00:00Z",
            served_model=None,
            requested_model=str(session.model),
            memory_mode=str(session.memory_mode),
            org_id=str(session.org_id),
        ),
    )

    # Drive resume_with_decision through the delivery-failure path, measuring
    # wall-clock around the whole call to prove the bounded poll never hangs.
    t_start = time.monotonic()
    resumed = _verify_first_candidate_and_advance(harness, tmp_path)
    t_stop = time.monotonic()
    elapsed = t_stop - t_start

    assert resumed["status"] == "session_committed"
    assert resumed["next_index"] == 1
    assert elapsed < 10.0

    # (a) bounded poll: at most index_ready_max_polls index_ready calls.
    assert harness.runner.index_ready_calls <= 3

    # The sequencer wrote the fail-closed delivery record to the real stream.
    stream = StatusStream(default_status_stream_path(harness.manifest_path))
    records = stream.records()
    delivery_records = [r for r in records if r.get("type") == "delivery"]
    assert len(delivery_records) == 1
    delivery = delivery_records[0]
    assert delivery["delivery_state"] == "unverified"
    assert delivery["sequence_index"] == 0
    assert delivery["memory_mode"] == "off"
    assert delivery["not_scored_reason"]

    # Append the attempt record (present in a real run from run_session) so the
    # scorecard sees a would-be-scored cell and must exclude it via the
    # fail-closed delivery disposition.
    stream.append(
        {
            "type": "attempt",
            "schema_version": 1,
            "sequence_index": 0,
            "memory_mode": "off",
            "org_id": "org-sequencer-test",
            "progress": {
                "problems_before": 3,
                "problems_after": 1,
                "resolved_count": 2,
                "remaining_count": 1,
                "full_green": True,
                "attempts_to_green": 1,
                "turns": 2,
                "total_tokens": 1000,
                "wall_seconds": 1.0,
                "wall_cost_usd": 0.0,
            },
            "session_fp": str(session.session_fp),
            "session_id": session.session_id,
        }
    )

    scorecard = build_scorecard(harness.manifest_path)

    # (b) the ON cell is EXCLUDED: no scored cells, neither pass nor fail.
    assert scorecard["scored_sessions"] == 0
    assert scorecard["convergence"]["points"] == []
    assert scorecard["scored_pass"] == 0
    assert scorecard["scored_fail"] == 0

    # (c) not-scored-with-reason reported for the excluded cell.
    assert scorecard["not_scored"] == [
        {
            "sequence_index": 0,
            "memory_mode": "off",
            "not_scored_reason": delivery["not_scored_reason"],
        }
    ]










