import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import wevibe_bench.cumulative.sequencer as sequencer_module
from wevibe_bench.cumulative.catalog import PrivateCatalog, PrivateReviewCard
from wevibe_bench.cumulative.decision import DENY_FINAL, VERIFY
from wevibe_bench.cumulative.leader_client import LeaderClient
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
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
    paused = harness.sequencer.step_until_review()
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

    paused = _await_review(harness)

    assert paused["sequence_index"] == 0
    assert harness.runner.phase_trace == [
        "PREPARE_FIXTURE",
        "RUN_SESSION",
        "EXTRACT_NORMAL_PIPELINE",
    ]
    assert harness.m2proof.verify_calls == []
    assert harness.hub_client.deny_calls == []

    manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    assert manifest["session_records"][0]["phase"] == "AWAIT_COORDINATOR_REVIEW"


def test_off_baseline_session_still_runs_extract(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    _await_review(harness)

    assert harness.runner.extract_calls == 1
    assert harness.runner.extract_sequence_indexes == [0]
    assert harness.runner.extract_memory_modes == ["off"]


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


def test_resume_waits_for_index_readiness_before_advancing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sequencer_module.time, "sleep", lambda _seconds: None)

    harness = _make_harness(tmp_path, index_ready_plan=[False, True])
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
    decision_path = _write_decision(tmp_path / "decision-readiness.json", decision_payload)

    resumed = harness.sequencer.resume_with_decision(decision_path)

    assert harness.runner.index_ready_history[:2] == [False, True]
    assert harness.runner.not_ready_snapshots == [
        {
            "current_index": 0,
            "phase": "COMMIT_INDEX_READY",
        }
    ]
    assert resumed["next_index"] == 1
    assert harness.sequencer.state()["current_index"] == 1


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

    second_pause = harness.sequencer.step_until_review()

    assert second_pause == first_pause
    assert harness.runner.extract_calls == extract_calls_before


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


def test_mid_walk_gate_failure_returns_halted_descriptor_and_stops_later_ordinals(
    tmp_path: Path,
) -> None:
    harness = _make_harness(
        tmp_path,
        on_budget=4,
        walk_gates_by_sequence={2: [_failing_walk_gate(), _not_evaluated_walk_gate()]},
    )

    assert _await_review(harness)["sequence_index"] == 0
    _verify_first_candidate_and_advance(harness, tmp_path)
    assert _await_review(harness)["sequence_index"] == 1
    _verify_first_candidate_and_advance(harness, tmp_path)

    halted = harness.sequencer.step_until_review()

    assert halted["status"] == "halted_on_gate"
    assert halted["phase"] == SessionPhase.HALTED_ON_GATE.value
    assert halted["sequence_index"] == 2
    manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    assert manifest["current_index"] == 2
    assert manifest["session_records"][2]["phase"] == SessionPhase.HALTED_ON_GATE.value
    assert manifest["session_records"][3]["phase"] == SessionPhase.PREPARE_FIXTURE.value
    assert manifest["session_records"][4]["phase"] == SessionPhase.PREPARE_FIXTURE.value
    assert harness.runner.extract_sequence_indexes == [0, 1]


def test_resume_after_gate_halt_restarts_halted_ordinal_not_zero(tmp_path: Path) -> None:
    gates_by_sequence = {2: [_failing_walk_gate()]}
    harness = _make_harness(
        tmp_path,
        on_budget=4,
        walk_gates_by_sequence=gates_by_sequence,
    )
    assert _await_review(harness)["sequence_index"] == 0
    _verify_first_candidate_and_advance(harness, tmp_path)
    assert _await_review(harness)["sequence_index"] == 1
    _verify_first_candidate_and_advance(harness, tmp_path)
    assert harness.sequencer.step_until_review()["status"] == "halted_on_gate"

    gates_by_sequence[2] = []
    resumed = harness.sequencer.resume_after_gate_halt()

    assert resumed["status"] == "awaiting_coordinator_review"
    assert resumed["sequence_index"] == 2
    assert harness.runner.extract_sequence_indexes == [0, 1, 2]
    assert harness.runner.run_calls == 4
    manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    assert manifest["current_index"] == 2
    assert manifest["session_records"][0]["phase"] == SessionPhase.NEXT_SESSION.value
    assert manifest["session_records"][1]["phase"] == SessionPhase.NEXT_SESSION.value


def test_gate_halt_checkpoint_round_trips_and_preserves_completed_sessions(
    tmp_path: Path,
) -> None:
    harness = _make_harness(
        tmp_path,
        on_budget=3,
        walk_gates_by_sequence={2: [_failing_walk_gate()]},
    )
    assert _await_review(harness)["sequence_index"] == 0
    _verify_first_candidate_and_advance(harness, tmp_path)
    assert _await_review(harness)["sequence_index"] == 1
    _verify_first_candidate_and_advance(harness, tmp_path)

    halted = harness.sequencer.step_until_review()
    assert halted["status"] == "halted_on_gate"

    resumed_harness = _make_harness(
        tmp_path,
        on_budget=3,
        walk_gates_by_sequence={2: [_failing_walk_gate()]},
    )
    resumed_state = resumed_harness.sequencer.state()
    assert resumed_state["current_index"] == 2
    assert resumed_state["phase"] == SessionPhase.HALTED_ON_GATE.value
    manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    assert manifest["session_records"][0]["committed_ids"]
    assert manifest["session_records"][1]["committed_ids"]


def test_not_evaluated_walk_gates_do_not_trigger_halt(tmp_path: Path) -> None:
    harness = _make_harness(
        tmp_path,
        on_budget=2,
        walk_gates_by_sequence={
            1: [
                WalkGateVerdictRecord(
                    ordinal=1,
                    gate=WalkGateName.INJECTION.value,
                    verdict=WalkGateVerdict.PASS.value,
                ),
                _not_evaluated_walk_gate(ordinal=2),
            ]
        },
    )

    assert _await_review(harness)["sequence_index"] == 0
    _verify_first_candidate_and_advance(harness, tmp_path)
    paused = harness.sequencer.step_until_review()

    assert paused["status"] == "awaiting_coordinator_review"
    assert paused["sequence_index"] == 1


def test_halted_terminal_state_is_distinct_from_done_and_capability_fail(
    tmp_path: Path,
) -> None:
    harness = _make_harness(
        tmp_path,
        on_budget=2,
        walk_gates_by_sequence={1: [_failing_walk_gate()]},
    )
    assert _await_review(harness)["sequence_index"] == 0
    _verify_first_candidate_and_advance(harness, tmp_path)

    halted = harness.sequencer.step_until_review()
    state = harness.sequencer.state()
    manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    progress = manifest["session_records"][1]["progress"]

    assert halted["status"] == "halted_on_gate"
    assert state["phase"] == SessionPhase.HALTED_ON_GATE.value
    assert state["phase"] != SessionPhase.DONE.value
    assert manifest["session_records"][1]["error"] is None
    assert progress["termination_reason"] == "attempt_ceiling_reached"


def test_halt_descriptor_and_record_include_gate_model_and_producer_ids(
    tmp_path: Path,
) -> None:
    gate = _failing_walk_gate(ordinal=1)
    harness = _make_harness(
        tmp_path,
        on_budget=1,
        walk_gates_by_sequence={1: [gate]},
    )
    assert _await_review(harness)["sequence_index"] == 0
    _verify_first_candidate_and_advance(harness, tmp_path)

    halted = harness.sequencer.step_until_review()
    manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    record_gate = manifest["session_records"][1]["walk_gates"][0]

    assert halted["gate"] == WalkGateName.WORK.value
    assert halted["ordinal"] == 1
    assert halted["model"] == manifest["session_records"][1]["model"]
    assert halted["expected_producer_model_ids"] == [
        "openrouter/model-a",
        "openrouter/model-b",
    ]
    assert halted["observed_producer_model_ids"] == ["openrouter/model-a"]
    assert record_gate["stops_walk"] is True
    assert record_gate["expected_producer_model_ids"] == halted["expected_producer_model_ids"]
