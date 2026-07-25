"""Decision manifest contract for cumulative benchmark coordinator handoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from ._validation import _optional_str, _require_non_empty_string
from .types import CUMULATIVE_SCHEMA_VERSION, SessionRecord

VERIFY = "verify"
DENY_FINAL = "deny_final"
VALID_VERDICTS = {VERIFY, DENY_FINAL}


class ConflictError(Exception):
    """Raised when replay attempts to change an already-applied verdict."""


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dict_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def _require_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _require_utc_iso(value: Any, *, field_name: str) -> str:
    text = _require_non_empty_string(value, field_name=field_name)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be a UTC ISO-8601 timestamp")
    return text


def _required_key(mapping: Mapping[str, Any], *, field_name: str) -> Any:
    if field_name not in mapping:
        raise ValueError(f"missing required field: {field_name}")
    return mapping[field_name]


@dataclass
class CandidateDecision:
    candidate_ref: str
    verdict: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    duplicate_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_ref": self.candidate_ref,
            "verdict": self.verdict,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "duplicate_refs": list(self.duplicate_refs),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CandidateDecision:
        if not isinstance(value, Mapping):
            raise ValueError("candidate decision must be a mapping")

        candidate_ref = _required_key(value, field_name="candidate_ref")
        verdict = _required_key(value, field_name="verdict")
        reason = _required_key(value, field_name="reason")

        return cls(
            candidate_ref=str(candidate_ref),
            verdict=str(verdict),
            reason=str(reason),
            evidence=_dict_value(value.get("evidence")),
            duplicate_refs=_string_list(value.get("duplicate_refs")),
        )


@dataclass
class IntegrityAttestation:
    job_id: str | None = None
    session_fp: str | None = None
    resolved_problem_count: int | None = None
    emitted_memory_count: int | None = None
    invariant_violation: bool = False
    integrity_record_seen: bool = False
    log_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "session_fp": self.session_fp,
            "resolved_problem_count": self.resolved_problem_count,
            "emitted_memory_count": self.emitted_memory_count,
            "invariant_violation": self.invariant_violation,
            "integrity_record_seen": self.integrity_record_seen,
            "log_path": self.log_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> IntegrityAttestation:
        if not isinstance(value, Mapping):
            raise ValueError("integrity attestation must be a mapping")
        return cls(
            job_id=_optional_str(value.get("job_id")),
            session_fp=_optional_str(value.get("session_fp")),
            resolved_problem_count=_optional_int(value.get("resolved_problem_count")),
            emitted_memory_count=_optional_int(value.get("emitted_memory_count")),
            invariant_violation=bool(value.get("invariant_violation", False)),
            integrity_record_seen=bool(value.get("integrity_record_seen", False)),
            log_path=_optional_str(value.get("log_path")),
        )


@dataclass
class DecisionManifest:
    manifest_id: str
    created_at: str
    sequence_index: int
    org_id: str
    coordinator_identity: str
    integrity: IntegrityAttestation
    candidates: list[CandidateDecision]
    schema_version: int = CUMULATIVE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "created_at": self.created_at,
            "sequence_index": self.sequence_index,
            "org_id": self.org_id,
            "coordinator_identity": self.coordinator_identity,
            "integrity": self.integrity.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DecisionManifest:
        if not isinstance(value, Mapping):
            raise ValueError("decision manifest must be a mapping")

        manifest_id = _required_key(value, field_name="manifest_id")
        created_at = _required_key(value, field_name="created_at")
        sequence_index = _required_key(value, field_name="sequence_index")
        org_id = _required_key(value, field_name="org_id")
        coordinator_identity = _required_key(value, field_name="coordinator_identity")
        raw_integrity = _required_key(value, field_name="integrity")
        raw_candidates = _required_key(value, field_name="candidates")

        if isinstance(raw_integrity, IntegrityAttestation):
            integrity = raw_integrity
        elif isinstance(raw_integrity, Mapping):
            integrity = IntegrityAttestation.from_dict(raw_integrity)
        else:
            raise ValueError("integrity must be a mapping")

        if not isinstance(raw_candidates, list):
            raise ValueError("candidates must be a list")

        candidates: list[CandidateDecision] = []
        for raw_candidate in raw_candidates:
            if isinstance(raw_candidate, CandidateDecision):
                candidates.append(raw_candidate)
            elif isinstance(raw_candidate, Mapping):
                candidates.append(CandidateDecision.from_dict(raw_candidate))
            else:
                raise ValueError("candidate entry must be a mapping")

        return cls(
            schema_version=int(value.get("schema_version", CUMULATIVE_SCHEMA_VERSION)),
            manifest_id=str(manifest_id),
            created_at=str(created_at),
            sequence_index=int(sequence_index),
            org_id=str(org_id),
            coordinator_identity=str(coordinator_identity),
            integrity=integrity,
            candidates=candidates,
        )


def validate_schema(manifest: DecisionManifest) -> None:
    if not isinstance(manifest, DecisionManifest):
        raise ValueError("manifest must be a DecisionManifest")
    if manifest.schema_version != CUMULATIVE_SCHEMA_VERSION:
        raise ValueError(
            "schema_version mismatch: "
            f"expected {CUMULATIVE_SCHEMA_VERSION}, got {manifest.schema_version}"
        )

    _require_non_empty_string(manifest.manifest_id, field_name="manifest_id")
    _require_utc_iso(manifest.created_at, field_name="created_at")
    _require_int(manifest.sequence_index, field_name="sequence_index")
    _require_non_empty_string(manifest.org_id, field_name="org_id")
    _require_non_empty_string(
        manifest.coordinator_identity,
        field_name="coordinator_identity",
    )

    if not isinstance(manifest.integrity, IntegrityAttestation):
        raise ValueError("integrity is required")
    if not isinstance(manifest.candidates, list):
        raise ValueError("candidates must be a list")

    seen_verdict_by_ref: dict[str, str] = {}
    for index, candidate in enumerate(manifest.candidates):
        if not isinstance(candidate, CandidateDecision):
            raise ValueError(f"candidates[{index}] must be a CandidateDecision")
        candidate_ref = _require_non_empty_string(
            candidate.candidate_ref,
            field_name=f"candidates[{index}].candidate_ref",
        )
        verdict = _require_non_empty_string(
            candidate.verdict,
            field_name=f"candidates[{index}].verdict",
        )
        if verdict not in VALID_VERDICTS:
            raise ValueError(
                f"candidates[{index}].verdict must be one of "
                f"{sorted(VALID_VERDICTS)}"
            )
        _require_non_empty_string(
            candidate.reason,
            field_name=f"candidates[{index}].reason",
        )

        seen_verdict = seen_verdict_by_ref.get(candidate_ref)
        if seen_verdict is None:
            seen_verdict_by_ref[candidate_ref] = verdict
        elif seen_verdict != verdict:
            raise ValueError(
                "conflicting duplicate candidate_ref in manifest: "
                f"{candidate_ref!r} has both {seen_verdict!r} and {verdict!r}"
            )


def _session_candidate_ref_set(session: SessionRecord) -> set[str]:
    refs: set[str] = set()
    for candidate in session.candidate_refs:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("submission_hash", "id", "candidate_id", "candidate_ref"):
            candidate_ref = _optional_str(candidate.get(key))
            if candidate_ref is not None:
                refs.add(candidate_ref)
    return refs


def validate_correlation(manifest: DecisionManifest, session: SessionRecord) -> None:
    if not isinstance(manifest, DecisionManifest):
        raise ValueError("manifest must be a DecisionManifest")
    if not isinstance(session, SessionRecord):
        raise ValueError("session must be a SessionRecord")

    if manifest.sequence_index != session.sequence_index:
        raise ValueError(
            "sequence_index mismatch: "
            f"manifest {manifest.sequence_index}, session {session.sequence_index}"
        )

    session_org_id = _optional_str(session.org_id)
    if session_org_id is None:
        raise ValueError("session.org_id is required for correlation")
    if manifest.org_id != session_org_id:
        raise ValueError(
            f"org_id mismatch: manifest {manifest.org_id!r}, session {session_org_id!r}"
        )

    integrity = manifest.integrity
    if not isinstance(integrity, IntegrityAttestation):
        raise ValueError("integrity attestation is required")
    if not integrity.integrity_record_seen:
        raise ValueError("integrity attestation missing: integrity_record_seen is False")

    session_job_id = _optional_str(session.extraction_job_id)
    session_fp = _optional_str(session.session_fp)
    if session_fp is None:
        session_id = _optional_str(session.session_id)
        if session_id is not None:
            session_fp = SessionRecord.session_fp_of(session_id)

    attested_job_id = _optional_str(integrity.job_id)
    attested_session_fp = _optional_str(integrity.session_fp)

    job_matches = (
        attested_job_id is not None
        and session_job_id is not None
        and attested_job_id == session_job_id
    )
    session_fp_matches = (
        attested_session_fp is not None
        and session_fp is not None
        and attested_session_fp == session_fp
    )
    if not (job_matches or session_fp_matches):
        raise ValueError(
            "integrity attestation cannot be correlated to session "
            f"(attestation.job_id={attested_job_id!r}, "
            f"session.extraction_job_id={session_job_id!r}, "
            f"attestation.session_fp={attested_session_fp!r}, session.session_fp={session_fp!r})"
        )

    known_refs = _session_candidate_ref_set(session)
    for index, candidate in enumerate(manifest.candidates):
        if not isinstance(candidate, CandidateDecision):
            raise ValueError(f"candidates[{index}] must be a CandidateDecision")
        candidate_ref = _require_non_empty_string(
            candidate.candidate_ref,
            field_name=f"candidates[{index}].candidate_ref",
        )
        if candidate_ref not in known_refs:
            raise ValueError(
                f"candidates[{index}].candidate_ref {candidate_ref!r} "
                "not found in session candidate_refs"
            )


def applied_map(manifest: DecisionManifest) -> dict[str, str]:
    if not isinstance(manifest, DecisionManifest):
        raise ValueError("manifest must be a DecisionManifest")

    applied: dict[str, str] = {}
    for index, candidate in enumerate(manifest.candidates):
        if not isinstance(candidate, CandidateDecision):
            raise ValueError(f"candidates[{index}] must be a CandidateDecision")
        candidate_ref = _require_non_empty_string(
            candidate.candidate_ref,
            field_name=f"candidates[{index}].candidate_ref",
        )
        verdict = _require_non_empty_string(
            candidate.verdict,
            field_name=f"candidates[{index}].verdict",
        )
        prior_verdict = applied.get(candidate_ref)
        if prior_verdict is not None and prior_verdict != verdict:
            raise ValueError(
                "conflicting duplicate candidate_ref in manifest: "
                f"{candidate_ref!r} has both {prior_verdict!r} and {verdict!r}"
            )
        applied[candidate_ref] = verdict
    return applied


def validate_replay(previous_applied: dict[str, str], manifest: DecisionManifest) -> None:
    if not isinstance(previous_applied, Mapping):
        raise ValueError("previous_applied must be a mapping")

    for candidate_ref, verdict in applied_map(manifest).items():
        previous_verdict = previous_applied.get(candidate_ref)
        if previous_verdict is None:
            continue
        previous_verdict_text = str(previous_verdict)
        if previous_verdict_text != verdict:
            raise ConflictError(
                f"candidate_ref {candidate_ref!r} already applied with verdict "
                f"{previous_verdict_text!r}; cannot apply {verdict!r}"
            )


__all__ = [
    "VERIFY",
    "DENY_FINAL",
    "VALID_VERDICTS",
    "ConflictError",
    "CandidateDecision",
    "IntegrityAttestation",
    "DecisionManifest",
    "validate_schema",
    "validate_correlation",
    "validate_replay",
    "applied_map",
]
