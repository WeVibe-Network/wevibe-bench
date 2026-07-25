import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wevibe_bench.cumulative.catalog import PrivateCatalog, PrivateReviewCard
from wevibe_bench.cumulative.consumer_gate import ConsumerGateOutcome, ServedStoreReconcile
from wevibe_bench.cumulative.decision import DENY_FINAL
from wevibe_bench.cumulative.leader_client import LeaderClient
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
from wevibe_bench.cumulative.types import ConsumerGateRecord, RosterEntry, SessionRecord

_MEMORY_TEXT = "TOP SECRET MEMORY TEXT"


class _RunnerWithoutConsumerHook:
    def __init__(self, *, manifest_path: Path) -> None:
        self.manifest_path = manifest_path

    def prepare_fixture(self, session: SessionRecord) -> None:
        del session

    def run_session(self, session: SessionRecord) -> dict[str, Any]:
        del session
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
        index = session.sequence_index
        return {
            "candidate_refs": [
                {
                    "submission_hash": f"sub-{index}-a",
                    "text": f"alpha {_MEMORY_TEXT} {index}",
                    "keywords": ["alpha", "backgammon"],
                    "memory_type": "memory",
                    "producer_model": "model-a",
                },
                {
                    "submission_hash": f"sub-{index}-b",
                    "text": f"beta {_MEMORY_TEXT} {index}",
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
        del session
        return True


class _RunnerWithConsumerHook(_RunnerWithoutConsumerHook):
    def __init__(self, *, manifest_path: Path, gate_record: ConsumerGateRecord) -> None:
        super().__init__(manifest_path=manifest_path)
        self._gate_record = gate_record
        self.consumer_hook_calls: list[int] = []

    def consumer_gate_outcome(self, session: SessionRecord) -> ConsumerGateRecord:
        self.consumer_hook_calls.append(session.sequence_index)
        return self._gate_record


class _FakeM2Proof:
    @staticmethod
    def leader_verify_and_commit(
        org_id: str,
        submission_hash: str,
        keywords: list[str],
        producer_model_id: str | None = None,
    ) -> dict[str, Any]:
        raise AssertionError(
            "leader_verify_and_commit should not be called in all-deny tests; "
            "org_id={org_id} submission_hash={submission_hash} keywords={keywords} "
            f"producer_model_id={producer_model_id}"
        )


class _FakeHubClient:
    def __init__(self) -> None:
        self.deny_calls: list[tuple[str, str, str]] = []

    def deny_submission(
        self,
        leader: Any,
        org_id: str,
        submission_hash: str,
        reason: str,
    ) -> dict[str, str]:
        del leader
        self.deny_calls.append((org_id, submission_hash, reason))
        return {"status": "denied"}


class _FakeLeader:
    ed_pubkey_hex = "f00df00d"

    @staticmethod
    def ed_pub_fp() -> str:
        return "leaderfp8"


@dataclass
class _Harness:
    sequencer: CumulativeSequencer
    manifest_path: Path
    runner: _RunnerWithoutConsumerHook | _RunnerWithConsumerHook


def _make_harness(
    tmp_path: Path,
    *,
    on_budget: int,
    runner: _RunnerWithoutConsumerHook | _RunnerWithConsumerHook,
) -> _Harness:
    manifest_path = tmp_path / "manifest.json"
    catalog = PrivateCatalog(str(tmp_path / "private.catalog.jsonl"))
    review_card = PrivateReviewCard(str(tmp_path / "private.review.jsonl"))

    leader_client = LeaderClient(
        _FakeM2Proof(),
        _FakeHubClient(),
        _FakeLeader(),
        catalog,
        safe_ledger_path=str(tmp_path / "safe-ledger.jsonl"),
        idempotency_ledger_path=str(tmp_path / "idempotency.json"),
        review_card=review_card,
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
    return _Harness(sequencer=sequencer, manifest_path=manifest_path, runner=runner)


def _decision_payload(
    session: SessionRecord,
    *,
    candidate_verdicts: list[tuple[str, str, str]],
) -> dict[str, Any]:
    return {
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


def _write_decision(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _await_review(harness: _Harness) -> dict[str, Any]:
    paused = harness.sequencer.step_until_review()
    assert paused["status"] == "awaiting_coordinator_review"
    return paused


def _session_candidate_refs(session: SessionRecord) -> list[str]:
    return [str(candidate["submission_hash"]) for candidate in session.candidate_refs]


def _resume_current_session_with_all_deny(harness: _Harness, tmp_path: Path) -> None:
    session = harness.sequencer.current_session()
    assert session is not None
    candidate_verdicts = [
        (candidate_ref, DENY_FINAL, "not useful")
        for candidate_ref in _session_candidate_refs(session)
    ]
    decision_path = _write_decision(
        tmp_path / f"decision-{session.sequence_index}.json",
        _decision_payload(session, candidate_verdicts=candidate_verdicts),
    )
    resumed = harness.sequencer.resume_with_decision(decision_path)
    assert resumed["status"] == "session_committed"


def test_consumer_gate_record_from_outcome_maps_counts_and_reconcile() -> None:
    outcome = ConsumerGateOutcome(
        run_id="run-1",
        session_id="session-1",
        coordinator_trace="trace://artifact/1",
        accept_count=2,
        deny_count=1,
        block_count=1,
        report_count=1,
        decisions=[
            ("cid-1", "accept"),
            ("cid-2", "accept"),
            ("cid-3", "deny"),
            ("cid-4", "block"),
            ("cid-5", "report"),
        ],
        decisions_path="/tmp/decisions.json",
    )

    reconcile = ServedStoreReconcile(
        accepted_confirmed=["cid-1"],
        missing_accepted=["cid-2"],
        nonaccept_leaked=["cid-3"],
        served_store_present=True,
    )

    record = ConsumerGateRecord.from_outcome(
        outcome,
        reconcile,
        serve_receipt_status="persisted",
        serve_receipt_ids=("cid-1",),
        denial_signal_status="emitted",
        report_signal_status="emitted",
    )

    assert record.consumer_injected_count == 2
    assert record.accepted_count == 2
    assert record.denied_count == 1
    assert record.blocked_count == 1
    assert record.reported_count == 1
    assert record.serve_receipt_status == "persisted"
    assert record.serve_receipt_ids == ("cid-1",)
    assert record.denial_signal_status == "emitted"
    assert record.report_signal_status == "emitted"
    assert record.served_store_write_confirmed is False
    assert record.served_store_missing_accepted == ("cid-2",)
    assert record.served_store_nonaccept_leaked == ("cid-3",)

    unknown_legs = ConsumerGateRecord.from_outcome(outcome)
    assert unknown_legs.serve_receipt_status is None
    assert unknown_legs.serve_receipt_ids is None
    assert unknown_legs.denial_signal_status is None
    assert unknown_legs.report_signal_status is None
    assert unknown_legs.served_store_write_confirmed is None
    assert unknown_legs.consumer_injected_count != 0


def test_session_record_roundtrips_consumer_gate_and_none() -> None:
    consumer_gate = ConsumerGateRecord(
        policy_id="primary-auto-accept-eligible-v1",
        coordinator_trace="trace://artifact/2",
        consumer_injected_count=3,
        accepted_count=3,
        denied_count=0,
        blocked_count=0,
        reported_count=0,
        serve_receipt_status="persisted",
        serve_receipt_ids=("cid-a", "cid-b"),
        denial_signal_status="none",
        report_signal_status="none",
        served_store_write_confirmed=True,
        served_store_missing_accepted=(),
        served_store_nonaccept_leaked=(),
    )

    session_with_gate = SessionRecord(
        sequence_index=1,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="on",
        phase_group="on",
        phase="AWAIT_COORDINATOR_REVIEW",
        progress={"turns": 1},
        consumer_gate=consumer_gate,
    )
    roundtripped = SessionRecord.from_dict(session_with_gate.to_dict())
    assert roundtripped.consumer_gate == consumer_gate

    session_without_gate = SessionRecord(
        sequence_index=0,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="off",
        phase_group="off_baseline",
        phase="AWAIT_COORDINATOR_REVIEW",
        progress={"turns": 1},
        consumer_gate=None,
    )
    roundtripped_none = SessionRecord.from_dict(session_without_gate.to_dict())
    assert roundtripped_none.consumer_gate is None


def test_sequencer_stores_consumer_gate_record_when_runner_hook_exists(tmp_path: Path) -> None:
    expected_record = ConsumerGateRecord(
        policy_id="primary-auto-accept-eligible-v1",
        coordinator_trace="trace://artifact/on-session",
        consumer_injected_count=2,
        accepted_count=2,
        denied_count=0,
        blocked_count=0,
        reported_count=0,
        serve_receipt_status=None,
        serve_receipt_ids=None,
        denial_signal_status=None,
        report_signal_status=None,
        served_store_write_confirmed=None,
        served_store_missing_accepted=(),
        served_store_nonaccept_leaked=(),
    )
    manifest_path = tmp_path / "manifest.json"
    runner = _RunnerWithConsumerHook(
        manifest_path=manifest_path,
        gate_record=expected_record,
    )
    harness = _make_harness(
        tmp_path,
        on_budget=1,
        runner=runner,
    )

    # Session 0 is OFF baseline and should never carry consumer-gate telemetry.
    _await_review(harness)
    first_manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    assert first_manifest["session_records"][0]["consumer_gate"] is None
    _resume_current_session_with_all_deny(harness, tmp_path)

    # Session 1 is ON and should persist the runner-provided consumer-gate record.
    _await_review(harness)
    current = harness.sequencer.current_session()
    assert current is not None
    assert current.sequence_index == 1
    assert current.consumer_gate == expected_record

    on_manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    stored_gate = on_manifest["session_records"][1]["consumer_gate"]
    assert stored_gate is not None
    assert stored_gate["accepted_count"] == 2
    assert stored_gate["consumer_injected_count"] == 2
    assert on_manifest["session_records"][1]["progress"]["consumer_injected_count"] == 2
    assert runner.consumer_hook_calls == [1]


def test_sequencer_runner_without_hook_keeps_consumer_gate_none(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    runner = _RunnerWithoutConsumerHook(manifest_path=manifest_path)
    harness = _make_harness(
        tmp_path,
        on_budget=1,
        runner=runner,
    )

    _await_review(harness)
    first_manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    assert first_manifest["session_records"][0]["consumer_gate"] is None

    _resume_current_session_with_all_deny(harness, tmp_path)
    _await_review(harness)

    current = harness.sequencer.current_session()
    assert current is not None
    assert current.sequence_index == 1
    assert current.consumer_gate is None

    second_manifest = json.loads(harness.manifest_path.read_text(encoding="utf-8"))
    assert second_manifest["session_records"][1]["consumer_gate"] is None
    assert second_manifest["session_records"][1]["progress"]["consumer_injected_count"] is None


def test_serialized_session_record_contains_no_memory_text(tmp_path: Path) -> None:
    expected_record = ConsumerGateRecord(
        policy_id="primary-auto-accept-eligible-v1",
        coordinator_trace="trace://artifact/on-session",
        consumer_injected_count=1,
        accepted_count=1,
        denied_count=1,
        blocked_count=0,
        reported_count=0,
        serve_receipt_status=None,
        serve_receipt_ids=None,
        denial_signal_status=None,
        report_signal_status=None,
        served_store_write_confirmed=None,
        served_store_missing_accepted=(),
        served_store_nonaccept_leaked=(),
    )
    manifest_path = tmp_path / "manifest.json"
    runner = _RunnerWithConsumerHook(manifest_path=manifest_path, gate_record=expected_record)
    harness = _make_harness(tmp_path, on_budget=1, runner=runner)

    _await_review(harness)
    _resume_current_session_with_all_deny(harness, tmp_path)
    _await_review(harness)

    serialized = harness.manifest_path.read_text(encoding="utf-8")
    assert _MEMORY_TEXT not in serialized
