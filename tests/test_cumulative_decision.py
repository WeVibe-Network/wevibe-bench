from dataclasses import replace

import pytest

from wevibe_bench.cumulative.decision import (
    CandidateDecision,
    ConflictError,
    DENY_FINAL,
    DecisionManifest,
    IntegrityAttestation,
    VERIFY,
    applied_map,
    validate_correlation,
    validate_replay,
    validate_schema,
)
from wevibe_bench.cumulative.types import SessionRecord


def _session() -> SessionRecord:
    return SessionRecord(
        sequence_index=7,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="on",
        phase_group="on",
        phase="AWAIT_COORDINATOR_REVIEW",
        session_id="sess-7",
        org_id="org-7",
        extraction_job_id="job-7",
        session_fp="sessfp07",
        candidate_refs=[
            {
                "submission_hash": "sub-7-a",
                "text": "alpha synthetic memory",
                "keywords": ["alpha", "backgammon"],
            },
            {
                "submission_hash": "sub-7-b",
                "text": "beta synthetic memory",
                "keywords": ["beta", "backgammon"],
            },
        ],
        extraction_candidate_count=2,
    )


def _manifest(
    session: SessionRecord,
    *,
    candidates: list[CandidateDecision] | None = None,
    integrity: IntegrityAttestation | None = None,
    sequence_index: int | None = None,
    org_id: str | None = None,
) -> DecisionManifest:
    if candidates is None:
        candidates = [
            CandidateDecision(
                candidate_ref="sub-7-a",
                verdict=VERIFY,
                reason="useful and grounded",
                evidence={"score": 0.93},
                duplicate_refs=[],
            )
        ]

    if integrity is None:
        integrity = IntegrityAttestation(
            job_id=session.extraction_job_id,
            session_fp=session.session_fp,
            resolved_problem_count=1,
            emitted_memory_count=len(candidates),
            invariant_violation=False,
            integrity_record_seen=True,
            log_path="/tmp/extraction.integrity.log",
        )

    return DecisionManifest(
        schema_version=1,
        manifest_id="decision-7",
        created_at="2026-07-23T12:00:00+00:00",
        sequence_index=session.sequence_index if sequence_index is None else sequence_index,
        org_id=session.org_id if org_id is None else org_id,
        coordinator_identity="coordinator-fp-7",
        integrity=integrity,
        candidates=candidates,
    )


def test_validate_schema_rejects_invalid_entries_and_accepts_well_formed_manifest() -> None:
    session = _session()
    valid_manifest = _manifest(session)
    validate_schema(valid_manifest)

    invalid_verdict_manifest = _manifest(
        session,
        candidates=[
            CandidateDecision(
                candidate_ref="sub-7-a",
                verdict="approve",
                reason="invalid verdict token",
            )
        ],
    )
    with pytest.raises(ValueError, match="must be one of"):
        validate_schema(invalid_verdict_manifest)

    empty_reason_manifest = _manifest(
        session,
        candidates=[
            CandidateDecision(
                candidate_ref="sub-7-a",
                verdict=VERIFY,
                reason="   ",
            )
        ],
    )
    with pytest.raises(ValueError, match="reason"):
        validate_schema(empty_reason_manifest)

    conflicting_duplicate_manifest = _manifest(
        session,
        candidates=[
            CandidateDecision(
                candidate_ref="sub-7-a",
                verdict=VERIFY,
                reason="initial verdict",
            ),
            CandidateDecision(
                candidate_ref="sub-7-a",
                verdict=DENY_FINAL,
                reason="conflicting verdict",
            ),
        ],
    )
    with pytest.raises(ValueError, match="conflicting duplicate candidate_ref"):
        validate_schema(conflicting_duplicate_manifest)


def test_validate_correlation_contract_gate() -> None:
    session = _session()

    correlated_manifest = _manifest(
        session,
        integrity=IntegrityAttestation(
            job_id=session.extraction_job_id,
            session_fp="does-not-need-to-match-when-job-id-does",
            integrity_record_seen=True,
        ),
    )
    validate_correlation(correlated_manifest, session)

    missing_attestation_manifest = _manifest(
        session,
        integrity=replace(
            correlated_manifest.integrity,
            integrity_record_seen=False,
        ),
    )
    with pytest.raises(ValueError, match="integrity_record_seen is False"):
        validate_correlation(missing_attestation_manifest, session)

    uncorrelatable_manifest = _manifest(
        session,
        integrity=IntegrityAttestation(
            job_id="job-mismatch",
            session_fp="fp-mismatch",
            integrity_record_seen=True,
        ),
    )
    with pytest.raises(ValueError, match="cannot be correlated"):
        validate_correlation(uncorrelatable_manifest, session)

    sequence_mismatch_manifest = _manifest(session, sequence_index=session.sequence_index + 1)
    with pytest.raises(ValueError, match="sequence_index mismatch"):
        validate_correlation(sequence_mismatch_manifest, session)

    org_mismatch_manifest = _manifest(session, org_id="org-other")
    with pytest.raises(ValueError, match="org_id mismatch"):
        validate_correlation(org_mismatch_manifest, session)

    missing_candidate_manifest = _manifest(
        session,
        candidates=[
            CandidateDecision(
                candidate_ref="sub-7-missing",
                verdict=VERIFY,
                reason="candidate was not part of extraction",
            )
        ],
    )
    with pytest.raises(ValueError, match="not found in session candidate_refs"):
        validate_correlation(missing_candidate_manifest, session)


def test_validate_replay_and_applied_map() -> None:
    session = _session()
    manifest = _manifest(
        session,
        candidates=[
            CandidateDecision(
                candidate_ref="sub-7-a",
                verdict=VERIFY,
                reason="accept",
            ),
            CandidateDecision(
                candidate_ref="sub-7-b",
                verdict=DENY_FINAL,
                reason="reject duplicate",
            ),
        ],
    )

    expected_applied = {
        "sub-7-a": VERIFY,
        "sub-7-b": DENY_FINAL,
    }
    assert applied_map(manifest) == expected_applied

    validate_replay(expected_applied, manifest)

    flipped_manifest = _manifest(
        session,
        candidates=[
            CandidateDecision(
                candidate_ref="sub-7-a",
                verdict=DENY_FINAL,
                reason="flip should conflict",
            )
        ],
    )
    with pytest.raises(ConflictError, match="already applied"):
        validate_replay(expected_applied, flipped_manifest)


def test_decision_manifest_to_dict_from_dict_round_trip() -> None:
    session = _session()
    manifest = _manifest(
        session,
        candidates=[
            CandidateDecision(
                candidate_ref="sub-7-a",
                verdict=VERIFY,
                reason="accepted",
                evidence={"rule": "grounded"},
                duplicate_refs=["dup-1", "dup-2"],
            ),
            CandidateDecision(
                candidate_ref="sub-7-b",
                verdict=DENY_FINAL,
                reason="rejected as duplicate",
                evidence={"rule": "duplicate"},
                duplicate_refs=["sub-7-a"],
            ),
        ],
        integrity=IntegrityAttestation(
            job_id=session.extraction_job_id,
            session_fp=session.session_fp,
            resolved_problem_count=2,
            emitted_memory_count=2,
            invariant_violation=False,
            integrity_record_seen=True,
            log_path="/tmp/extraction.integrity.log",
        ),
    )

    assert DecisionManifest.from_dict(manifest.to_dict()) == manifest
