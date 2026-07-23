import json

import pytest

from wevibe_bench.cumulative.catalog import PrivateCatalog
from wevibe_bench.cumulative.decision import (
    CandidateDecision,
    ConflictError,
    DENY_FINAL,
    DecisionManifest,
    IntegrityAttestation,
    VERIFY,
)
from wevibe_bench.cumulative.leader_client import LeaderClient
from wevibe_bench.cumulative.types import SessionRecord


SYNTHETIC_TEXT_CANARY = "SYNTHETIC_TEXT_CANARY_DO_NOT_LEAK_TO_SAFE_LEDGER"


class FakeLeader:
    ed_pubkey_hex = "f00df00d"

    def ed_pub_fp(self) -> str:
        return "leaderfp8"


class FakeM2Proof:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def leader_verify_and_commit(
        self,
        org_id: str,
        submission_hash: str,
        keywords: list[str],
    ) -> dict[str, object]:
        self.calls.append((org_id, submission_hash, tuple(keywords)))
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
        self.calls: list[tuple[str, str, str, str]] = []

    def deny_submission(
        self,
        leader: FakeLeader,
        org_id: str,
        submission_hash: str,
        reason: str,
    ) -> dict[str, str]:
        self.calls.append((leader.ed_pubkey_hex, org_id, submission_hash, reason))
        return {"status": "denied"}


def _session() -> SessionRecord:
    return SessionRecord(
        sequence_index=11,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="on",
        phase_group="on",
        phase="AWAIT_COORDINATOR_REVIEW",
        session_id="sess-11",
        org_id="org-11",
        extraction_job_id="job-11",
        session_fp="sessfp11",
        candidate_refs=[
            {
                "submission_hash": "sub-11-verify",
                "text": SYNTHETIC_TEXT_CANARY,
                "keywords": ["alpha", "beta"],
            },
            {
                "submission_hash": "sub-11-deny",
                "text": "this one should be denied",
                "comparison_text": "private-comparison-deny",
                "plaintext": "should-never-leak",
                "keywords": ["gamma"],
            },
        ],
        extraction_candidate_count=2,
    )


def _manifest(
    session: SessionRecord,
    *,
    candidates: list[CandidateDecision],
    manifest_id: str,
) -> DecisionManifest:
    return DecisionManifest(
        schema_version=1,
        manifest_id=manifest_id,
        created_at="2026-07-23T12:05:00+00:00",
        sequence_index=int(session.sequence_index),
        org_id=str(session.org_id),
        coordinator_identity="coordinator-fp-11",
        integrity=IntegrityAttestation(
            job_id=session.extraction_job_id,
            session_fp=session.session_fp,
            resolved_problem_count=1,
            emitted_memory_count=len(candidates),
            invariant_violation=False,
            integrity_record_seen=True,
            log_path="/tmp/extraction.integrity.log",
        ),
        candidates=candidates,
    )


def _build_client(tmp_path):
    m2proof = FakeM2Proof()
    hub_client = FakeHubClient()
    leader = FakeLeader()
    catalog = PrivateCatalog(str(tmp_path / "private.catalog.jsonl"))
    safe_ledger_path = tmp_path / "safe-ledger.jsonl"
    idempotency_ledger_path = tmp_path / "idempotency.json"

    client = LeaderClient(
        m2proof,
        hub_client,
        leader,
        catalog,
        safe_ledger_path=str(safe_ledger_path),
        idempotency_ledger_path=str(idempotency_ledger_path),
    )
    return (
        client,
        m2proof,
        hub_client,
        leader,
        catalog,
        safe_ledger_path,
        idempotency_ledger_path,
    )


def test_apply_routes_verify_and_deny_to_correct_adapters_and_catalogs_commit(tmp_path) -> None:
    (
        client,
        m2proof,
        hub_client,
        leader,
        catalog,
        safe_ledger_path,
        _,
    ) = _build_client(tmp_path)
    session = _session()

    manifest = _manifest(
        session,
        manifest_id="decision-verify-deny",
        candidates=[
            CandidateDecision(
                candidate_ref="sub-11-verify",
                verdict=VERIFY,
                reason="accept useful candidate",
            ),
            CandidateDecision(
                candidate_ref="sub-11-deny",
                verdict=DENY_FINAL,
                reason="deny duplicate candidate",
            ),
        ],
    )

    result = client.apply(manifest, session)

    assert len(m2proof.calls) == 1
    assert m2proof.calls[0] == ("org-11", "sub-11-verify", ("alpha", "beta"))
    assert len(hub_client.calls) == 1
    assert hub_client.calls[0] == (
        leader.ed_pubkey_hex,
        "org-11",
        "sub-11-deny",
        "deny duplicate candidate",
    )

    assert result.committed_ids == ["cid-sub-11-verify"]
    assert result.denied_refs == ["sub-11-deny"]
    assert result.applied == {
        "sub-11-verify": VERIFY,
        "sub-11-deny": DENY_FINAL,
    }

    records = catalog.load()
    assert len(records) == 1
    record = records[0]
    assert record.submission_hash == "sub-11-verify"
    assert record.committed_id == "cid-sub-11-verify"
    assert record.committing_identity == leader.ed_pubkey_hex
    assert record.comparison_text == SYNTHETIC_TEXT_CANARY

    safe_ledger_bytes = safe_ledger_path.read_bytes()
    assert SYNTHETIC_TEXT_CANARY.encode("utf-8") not in safe_ledger_bytes


def test_apply_all_deny_final_completes_normally_and_marks_all_denied(tmp_path) -> None:
    client, m2proof, hub_client, _, catalog, safe_ledger_path, _ = _build_client(tmp_path)
    session = _session()

    manifest = _manifest(
        session,
        manifest_id="decision-all-deny",
        candidates=[
            CandidateDecision(
                candidate_ref="sub-11-verify",
                verdict=DENY_FINAL,
                reason="deny first",
            ),
            CandidateDecision(
                candidate_ref="sub-11-deny",
                verdict=DENY_FINAL,
                reason="deny second",
            ),
        ],
    )

    result = client.apply(manifest, session)

    assert len(m2proof.calls) == 0
    assert len(hub_client.calls) == 2
    assert result.committed_ids == []
    assert result.denied_refs == ["sub-11-verify", "sub-11-deny"]
    assert result.all_denied is True
    assert catalog.load() == []

    ledger_lines = [
        json.loads(line)
        for line in safe_ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(ledger_lines) == 2
    assert all(entry["verdict"] == DENY_FINAL for entry in ledger_lines)


def test_apply_replay_is_idempotent_and_conflicting_verdict_replay_raises(tmp_path) -> None:
    client, m2proof, hub_client, _, _, _, _ = _build_client(tmp_path)
    session = _session()
    manifest = _manifest(
        session,
        manifest_id="decision-replay",
        candidates=[
            CandidateDecision(
                candidate_ref="sub-11-verify",
                verdict=VERIFY,
                reason="accept",
            ),
            CandidateDecision(
                candidate_ref="sub-11-deny",
                verdict=DENY_FINAL,
                reason="deny",
            ),
        ],
    )

    first_result = client.apply(manifest, session)
    calls_after_first = (len(m2proof.calls), len(hub_client.calls))

    second_result = client.apply(manifest, session)

    assert second_result == first_result
    assert (len(m2proof.calls), len(hub_client.calls)) == calls_after_first

    conflict_manifest = _manifest(
        session,
        manifest_id="decision-replay-conflict",
        candidates=[
            CandidateDecision(
                candidate_ref="sub-11-verify",
                verdict=DENY_FINAL,
                reason="flip verdict should conflict",
            )
        ],
    )
    with pytest.raises(ConflictError, match="already applied"):
        client.apply(conflict_manifest, session)

    assert (len(m2proof.calls), len(hub_client.calls)) == calls_after_first


def test_list_pending_redacts_plaintext_and_reconcile_inventory_reports_both_directions(
    tmp_path,
) -> None:
    client, _, _, _, _, _, _ = _build_client(tmp_path)
    session = _session()

    pending = client.list_pending(session)
    assert pending["sequence_index"] == 11
    assert pending["org_id"] == "org-11"
    assert pending["candidate_count"] == 2
    assert pending["extraction_candidate_count"] == 2
    for candidate in pending["candidates"]:
        assert set(candidate.keys()) == {"submission_hash", "keywords", "keyword_count"}
        assert "text" not in candidate
        assert "comparison_text" not in candidate
        assert "plaintext" not in candidate

    verify_manifest = _manifest(
        session,
        manifest_id="decision-reconcile-seed",
        candidates=[
            CandidateDecision(
                candidate_ref="sub-11-verify",
                verdict=VERIFY,
                reason="seed catalog",
            )
        ],
    )
    client.apply(verify_manifest, session)

    reconciliation = client.reconcile_inventory(
        [
            {
                "committed_id": "cid-remote-only",
                "content_hash": "hash-remote-only",
                "committing_identity": "leader-remote",
            }
        ]
    )

    assert reconciliation["counts"]["in_catalog_not_chain"] == 1
    assert reconciliation["counts"]["in_chain_not_catalog"] == 1

    orphan = reconciliation["in_catalog_not_chain"][0]
    assert orphan["status"] == "orphan"

    chain_only = reconciliation["in_chain_not_catalog"][0]
    assert chain_only["committed_id"] == "cid-remote-only"
    assert chain_only["content_hash"] == "hash-remote-only"
    assert chain_only["committing_identity"] == "leader-remote"
    assert chain_only["status"] == "content_unavailable"
