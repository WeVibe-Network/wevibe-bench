import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from wevibe_bench.cumulative.catalog import (
    CatalogRecord,
    PrivateCatalog,
    PrivateReviewCard,
    reconcile,
    redacted_candidate_ref,
)
from wevibe_bench.cumulative.decision import DENY_FINAL, VERIFY
from wevibe_bench.cumulative.leader_client import LeaderClient
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
from wevibe_bench.cumulative.types import RosterEntry, SessionRecord


def _synthetic_text(sequence_index: int, label: str) -> str:
    return f"SYNTHETIC_{label}_SEQ_{sequence_index}_DO_NOT_LEAK"


def _synthetic_pair(sequence_index: int) -> tuple[str, str]:
    return (
        _synthetic_text(sequence_index, "ALPHA"),
        _synthetic_text(sequence_index, "BETA"),
    )


def _session_with_plaintext_candidates(sequence_index: int) -> SessionRecord:
    alpha_text, beta_text = _synthetic_pair(sequence_index)
    session_id = f"session-{sequence_index}"
    return SessionRecord(
        sequence_index=sequence_index,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="on",
        phase_group="on",
        phase="AWAIT_COORDINATOR_REVIEW",
        session_id=session_id,
        org_id="org-review-test",
        extraction_job_id=f"job-{sequence_index}",
        session_fp=SessionRecord.session_fp_of(session_id),
        candidate_refs=[
            {
                "submission_hash": f"sub-{sequence_index}-a",
                "text": alpha_text,
                "keywords": ["alpha", "backgammon"],
                "memory_type": "memory",
            },
            {
                "submission_hash": f"sub-{sequence_index}-b",
                "text": beta_text,
                "keywords": ["beta", "backgammon"],
                "memory_type": "memory",
            },
        ],
        extraction_candidate_count=2,
    )


class FakeRunner:
    def __init__(
        self,
        *,
        manifest_path: Path,
        index_ready_plan: list[bool] | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self._index_ready_plan = list(index_ready_plan or [True])
        self.prepare_calls = 0
        self.run_calls = 0
        self.extract_calls = 0

    def prepare_fixture(self, session: SessionRecord) -> None:
        self.prepare_calls += 1

    def run_session(self, session: SessionRecord) -> dict[str, Any]:
        self.run_calls += 1
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
        self.extract_calls += 1
        alpha_text, beta_text = _synthetic_pair(session.sequence_index)
        index = session.sequence_index
        return {
            "candidate_refs": [
                {
                    "submission_hash": f"sub-{index}-a",
                    "text": alpha_text,
                    "keywords": ["alpha", "backgammon"],
                    "memory_type": "memory",
                    "producer_model": "model-a",
                },
                {
                    "submission_hash": f"sub-{index}-b",
                    "text": beta_text,
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
        if self._index_ready_plan:
            return self._index_ready_plan.pop(0)
        return True


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
    review_card: PrivateReviewCard
    manifest_path: Path
    catalog_path: Path
    review_card_path: Path
    safe_ledger_path: Path
    idempotency_ledger_path: Path


def _roster() -> list[RosterEntry]:
    return [
        RosterEntry(
            model="openrouter/model-a",
            role="assistant",
            provider_pin="openrouter",
            config_identity={"slot": 1},
        )
    ]


def _make_harness(
    tmp_path: Path,
    *,
    manifest_path: Path | None = None,
    catalog_path: Path | None = None,
    review_card_path: Path | None = None,
    safe_ledger_path: Path | None = None,
    idempotency_ledger_path: Path | None = None,
    index_ready_plan: list[bool] | None = None,
) -> Harness:
    resolved_manifest_path = manifest_path or (tmp_path / "manifest.json")
    resolved_catalog_path = catalog_path or (tmp_path / "private.catalog.jsonl")
    resolved_review_card_path = review_card_path or (tmp_path / "private.review.jsonl")
    resolved_safe_ledger_path = safe_ledger_path or (tmp_path / "safe-ledger.jsonl")
    resolved_idempotency_ledger_path = idempotency_ledger_path or (tmp_path / "idempotency.json")

    catalog = PrivateCatalog(str(resolved_catalog_path))
    review_card = PrivateReviewCard(str(resolved_review_card_path))

    m2proof = FakeM2Proof()
    hub_client = FakeHubClient()
    leader_client = LeaderClient(
        m2proof,
        hub_client,
        FakeLeader(),
        catalog,
        safe_ledger_path=str(resolved_safe_ledger_path),
        idempotency_ledger_path=str(resolved_idempotency_ledger_path),
        review_card=review_card,
    )

    runner = FakeRunner(
        manifest_path=resolved_manifest_path,
        index_ready_plan=index_ready_plan,
    )

    sequencer = CumulativeSequencer(
        resolved_manifest_path,
        runner=runner,
        leader_client=leader_client,
        review_card=review_card,
        roster=_roster(),
        seed=17,
        task="backgammon",
        org_id="org-review-test",
        config_fingerprint="cfg-review-test",
        on_budget=1,
    )

    return Harness(
        sequencer=sequencer,
        runner=runner,
        m2proof=m2proof,
        hub_client=hub_client,
        catalog=catalog,
        review_card=review_card,
        manifest_path=resolved_manifest_path,
        catalog_path=resolved_catalog_path,
        review_card_path=resolved_review_card_path,
        safe_ledger_path=resolved_safe_ledger_path,
        idempotency_ledger_path=resolved_idempotency_ledger_path,
    )


def _await_review(harness: Harness) -> dict[str, Any]:
    pending = harness.sequencer.step_until_review()
    assert pending["status"] == "awaiting_extract"
    paused = harness.sequencer.extract_current()
    assert paused["status"] == "awaiting_coordinator_review"
    return paused


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
        "coordinator_identity": "coordinator-review-test",
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


def _session_candidate_refs(session: SessionRecord) -> list[str]:
    return [str(candidate["submission_hash"]) for candidate in session.candidate_refs]


def test_private_review_card_rejects_non_review_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.review\.jsonl"):
        PrivateReviewCard(str(tmp_path / "private.catalog.jsonl"))


def test_private_review_card_write_idempotent_lookup_and_session_material(tmp_path: Path) -> None:
    session = _session_with_plaintext_candidates(sequence_index=4)
    review_card_path = tmp_path / "private.review.jsonl"
    review_card = PrivateReviewCard(str(review_card_path))

    assert review_card.write_session(session) == 2
    assert review_card.write_session(session) == 0

    assert stat.S_IMODE(review_card_path.stat().st_mode) == 0o600

    alpha_text, beta_text = _synthetic_pair(sequence_index=4)
    assert review_card.lookup_text("sub-4-a") == alpha_text
    assert review_card.lookup_text("sub-4-b") == beta_text

    material = review_card.session_material(4)
    assert len(material) == 2
    assert {entry["submission_hash"] for entry in material} == {"sub-4-a", "sub-4-b"}
    assert {entry["comparison_text"] for entry in material} == {alpha_text, beta_text}
    assert all("comparison_text" in entry for entry in material)


def test_redacted_candidate_ref_removes_plaintext_and_hashes_text() -> None:
    plaintext = "SYNTHETIC_REDACTION_CANARY"
    redacted = redacted_candidate_ref(
        {
            "submission_hash": "sub-redaction",
            "text": plaintext,
            "comparison_text": "another-copy",
            "keywords": ["alpha"],
            "memory_type": "memory",
            "producer_model": "model-a",
        }
    )

    assert "text" not in redacted
    assert "comparison_text" not in redacted
    assert redacted["producer_model"] == "model-a"
    assert redacted["content_hash"] == hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def test_manifest_redaction_cross_process_resume_and_hash_only_artifacts(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, index_ready_plan=[True])
    paused = _await_review(harness)

    sequence_index = int(paused["sequence_index"])
    alpha_text, beta_text = _synthetic_pair(sequence_index)

    manifest_bytes = harness.manifest_path.read_bytes()
    assert alpha_text.encode("utf-8") not in manifest_bytes
    assert beta_text.encode("utf-8") not in manifest_bytes

    manifest = json.loads(manifest_bytes.decode("utf-8"))
    persisted_refs = manifest["session_records"][sequence_index]["candidate_refs"]
    assert len(persisted_refs) == 2
    for ref in persisted_refs:
        assert "content_hash" in ref
        assert "text" not in ref
        assert "comparison_text" not in ref

    assert stat.S_IMODE(harness.review_card_path.stat().st_mode) == 0o600
    assert harness.review_card.lookup_text(f"sub-{sequence_index}-a") == alpha_text
    assert harness.review_card.lookup_text(f"sub-{sequence_index}-b") == beta_text

    review_material = harness.review_card.session_material(sequence_index)
    assert {entry["comparison_text"] for entry in review_material} == {alpha_text, beta_text}

    resumed_harness = _make_harness(
        tmp_path,
        manifest_path=harness.manifest_path,
        catalog_path=harness.catalog_path,
        review_card_path=harness.review_card_path,
        safe_ledger_path=harness.safe_ledger_path,
        idempotency_ledger_path=harness.idempotency_ledger_path,
        index_ready_plan=[True],
    )

    resumed_session = resumed_harness.sequencer.current_session()
    assert resumed_session is not None
    for ref in resumed_session.candidate_refs:
        assert "text" not in ref
        assert "comparison_text" not in ref

    verify_ref, deny_ref = _session_candidate_refs(resumed_session)
    decision_payload = _decision_payload(
        resumed_session,
        candidate_verdicts=[
            (verify_ref, VERIFY, "accept useful candidate"),
            (deny_ref, DENY_FINAL, "deny duplicate candidate"),
        ],
    )
    decision_path = _write_decision(tmp_path / "decision-cross-process.json", decision_payload)

    resumed = resumed_harness.sequencer.resume_with_decision(decision_path)
    assert resumed["status"] == "session_committed"
    assert resumed["next_index"] == 1
    assert len(resumed["committed_ids"]) == 1
    assert resumed["denied_refs"] == [deny_ref]

    assert len(resumed_harness.m2proof.verify_calls) == 1

    catalog_records = resumed_harness.catalog.load()
    assert len(catalog_records) == 1
    record = catalog_records[0]
    assert record.submission_hash == verify_ref
    assert record.comparison_text == alpha_text
    assert record.producer_model == "model-a"

    safe_ledger_bytes = resumed_harness.safe_ledger_path.read_bytes()
    assert alpha_text.encode("utf-8") not in safe_ledger_bytes
    assert beta_text.encode("utf-8") not in safe_ledger_bytes

    manifest_bytes_after = harness.manifest_path.read_bytes()
    assert alpha_text.encode("utf-8") not in manifest_bytes_after
    assert beta_text.encode("utf-8") not in manifest_bytes_after


def test_reconcile_catalog_complete_flag_reports_missing_authoritative_ids() -> None:
    comparison_text = "SYNTHETIC_CATALOG_TEXT"
    record = CatalogRecord(
        submission_hash="sub-1",
        committed_id="cid-present",
        keywords=["alpha"],
        comparison_text=comparison_text,
        org_id="org-review-test",
        sequence_index=0,
        committing_identity="leader-a",
        content_hash=hashlib.sha256(comparison_text.encode("utf-8")).hexdigest(),
        committed_at="2026-07-23T12:10:00+00:00",
        producer_model="tencent/hy3",
    )

    partial = reconcile(
        [record],
        [
            {
                "committed_id": "cid-present",
                "content_hash": record.content_hash,
                "committing_identity": "leader-a",
            },
            {
                "committed_id": "cid-missing",
                "content_hash": "hash-missing",
                "committing_identity": "leader-b",
            },
        ],
    )
    assert partial["catalog_complete"] is False
    assert partial["counts"]["in_chain_not_catalog"] == 1
    assert partial["in_chain_not_catalog"][0]["committed_id"] == "cid-missing"

    complete = reconcile(
        [record],
        [
            {
                "committed_id": "cid-present",
                "content_hash": record.content_hash,
                "committing_identity": "leader-a",
            }
        ],
    )
    assert complete["catalog_complete"] is True
    assert complete["counts"]["in_chain_not_catalog"] == 0
