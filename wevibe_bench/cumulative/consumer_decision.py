"""Consumer recall decision manifest contract for cumulative benchmark handoff."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

CONSUMER_DECISION_SCHEMA_VERSION = 1

VALID_FATES = frozenset({"accept", "deny", "block", "report"})

DEFAULT_PRIMARY_POLICY = "primary-auto-accept-eligible-v1"


class ConflictError(Exception):
    """Raised when replay attempts to change an already-applied consumer fate."""


def _required_key(mapping: Mapping[str, Any], *, field_name: str) -> Any:
    if field_name not in mapping:
        raise ValueError(f"missing required field: {field_name}")
    return mapping[field_name]


def _require_non_empty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _require_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _require_cid_set(value: Iterable[Any], *, field_name: str) -> set[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} must be an iterable of candidate CIDs")

    try:
        items = list(value)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be an iterable of candidate CIDs") from exc

    recalled: set[str] = set()
    for index, raw_cid in enumerate(items):
        cid = _require_non_empty_string(raw_cid, field_name=f"{field_name}[{index}]")
        recalled.add(cid)
    return recalled


@dataclass(frozen=True)
class ConsumerCandidateDecision:
    run_id: str
    session_id: str
    candidate_cid: str
    fate: str
    coordinator_trace: str
    reason: str
    note: str = ""

    def to_stored_decision(self, timestamp_ms: int) -> dict[str, Any]:
        timestamp = _require_int(timestamp_ms, field_name="timestamp_ms")
        return {
            "memoryID": self.candidate_cid,
            "action": self.fate,
            "reason": self.reason,
            "note": self.note,
            "timestamp": timestamp,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "candidate_cid": self.candidate_cid,
            "fate": self.fate,
            "coordinator_trace": self.coordinator_trace,
            "reason": self.reason,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConsumerCandidateDecision:
        if not isinstance(value, Mapping):
            raise ValueError("consumer candidate decision must be a mapping")

        run_id = _required_key(value, field_name="run_id")
        session_id = _required_key(value, field_name="session_id")
        candidate_cid = _required_key(value, field_name="candidate_cid")
        fate = _required_key(value, field_name="fate")
        coordinator_trace = _required_key(value, field_name="coordinator_trace")
        reason = _required_key(value, field_name="reason")

        return cls(
            run_id=str(run_id),
            session_id=str(session_id),
            candidate_cid=str(candidate_cid),
            fate=str(fate),
            coordinator_trace=str(coordinator_trace),
            reason=str(reason),
            note=str(value.get("note", "")),
        )


@dataclass(frozen=True)
class ConsumerDecisionManifest:
    schema_version: int
    run_id: str
    policy_id: str
    default_fate: str
    decisions: tuple[ConsumerCandidateDecision, ...]
    coordinator_trace: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "policy_id": self.policy_id,
            "default_fate": self.default_fate,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "coordinator_trace": self.coordinator_trace,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConsumerDecisionManifest:
        if not isinstance(value, Mapping):
            raise ValueError("consumer decision manifest must be a mapping")

        run_id = _required_key(value, field_name="run_id")
        policy_id = _required_key(value, field_name="policy_id")
        decisions = _required_key(value, field_name="decisions")
        coordinator_trace = _required_key(value, field_name="coordinator_trace")

        if not isinstance(decisions, list):
            raise ValueError("decisions must be a list")

        parsed_decisions: list[ConsumerCandidateDecision] = []
        for raw_decision in decisions:
            if isinstance(raw_decision, ConsumerCandidateDecision):
                parsed_decisions.append(raw_decision)
            elif isinstance(raw_decision, Mapping):
                parsed_decisions.append(ConsumerCandidateDecision.from_dict(raw_decision))
            else:
                raise ValueError("decision entry must be a mapping")

        return cls(
            schema_version=int(value.get("schema_version", CONSUMER_DECISION_SCHEMA_VERSION)),
            run_id=str(run_id),
            policy_id=str(policy_id),
            default_fate=str(value.get("default_fate", "")),
            decisions=tuple(parsed_decisions),
            coordinator_trace=str(coordinator_trace),
        )


def validate_schema(manifest: ConsumerDecisionManifest) -> None:
    if not isinstance(manifest, ConsumerDecisionManifest):
        raise ValueError("manifest must be a ConsumerDecisionManifest")
    if manifest.schema_version != CONSUMER_DECISION_SCHEMA_VERSION:
        raise ValueError(
            "schema_version mismatch: "
            f"expected {CONSUMER_DECISION_SCHEMA_VERSION}, got {manifest.schema_version}"
        )

    _require_non_empty_string(manifest.run_id, field_name="run_id")
    _require_non_empty_string(manifest.policy_id, field_name="policy_id")
    _require_non_empty_string(manifest.coordinator_trace, field_name="coordinator_trace")

    if not isinstance(manifest.default_fate, str) or not manifest.default_fate.strip():
        raise ValueError("primary default policy must be declared explicitly")

    default_fate = manifest.default_fate.strip()
    if default_fate not in VALID_FATES:
        raise ValueError(f"default_fate must be one of {sorted(VALID_FATES)}")

    if not isinstance(manifest.decisions, tuple):
        raise ValueError("decisions must be a tuple")

    for index, decision in enumerate(manifest.decisions):
        if not isinstance(decision, ConsumerCandidateDecision):
            raise ValueError(f"decisions[{index}] must be a ConsumerCandidateDecision")

        _require_non_empty_string(
            decision.run_id,
            field_name=f"decisions[{index}].run_id",
        )
        _require_non_empty_string(
            decision.session_id,
            field_name=f"decisions[{index}].session_id",
        )
        _require_non_empty_string(
            decision.candidate_cid,
            field_name=f"decisions[{index}].candidate_cid",
        )
        _require_non_empty_string(
            decision.coordinator_trace,
            field_name=f"decisions[{index}].coordinator_trace",
        )

        fate = _require_non_empty_string(
            decision.fate,
            field_name=f"decisions[{index}].fate",
        )
        if fate not in VALID_FATES:
            raise ValueError(
                f"decisions[{index}].fate must be one of {sorted(VALID_FATES)}"
            )

        if fate != "accept":
            _require_non_empty_string(
                decision.reason,
                field_name=f"decisions[{index}].reason",
            )
        elif not isinstance(decision.reason, str):
            raise ValueError(f"decisions[{index}].reason must be a string")

        if not isinstance(decision.note, str):
            raise ValueError(f"decisions[{index}].note must be a string")


def validate_one_per_candidate(manifest: ConsumerDecisionManifest) -> None:
    if not isinstance(manifest, ConsumerDecisionManifest):
        raise ValueError("manifest must be a ConsumerDecisionManifest")

    seen: set[tuple[str, str]] = set()
    for index, decision in enumerate(manifest.decisions):
        if not isinstance(decision, ConsumerCandidateDecision):
            raise ValueError(f"decisions[{index}] must be a ConsumerCandidateDecision")
        session_id = _require_non_empty_string(
            decision.session_id,
            field_name=f"decisions[{index}].session_id",
        )
        candidate_cid = _require_non_empty_string(
            decision.candidate_cid,
            field_name=f"decisions[{index}].candidate_cid",
        )

        key = (session_id, candidate_cid)
        if key in seen:
            raise ValueError(
                "duplicate decision for session/candidate: "
                f"session_id={session_id!r}, candidate_cid={candidate_cid!r}"
            )
        seen.add(key)


def validate_correlation(
    manifest: ConsumerDecisionManifest,
    *,
    run_id: str,
    session_id: str,
    recalled_cids: Iterable[Any],
) -> set[str]:
    if not isinstance(manifest, ConsumerDecisionManifest):
        raise ValueError("manifest must be a ConsumerDecisionManifest")

    manifest_run_id = _require_non_empty_string(manifest.run_id, field_name="manifest.run_id")
    expected_run_id = _require_non_empty_string(run_id, field_name="run_id")
    expected_session_id = _require_non_empty_string(session_id, field_name="session_id")
    known_recalled_cids = _require_cid_set(recalled_cids, field_name="recalled_cids")

    if manifest_run_id != expected_run_id:
        raise ValueError(
            f"run_id mismatch: manifest {manifest_run_id!r}, expected {expected_run_id!r}"
        )

    validate_one_per_candidate(manifest)

    decided_cids: set[str] = set()
    for index, decision in enumerate(manifest.decisions):
        if not isinstance(decision, ConsumerCandidateDecision):
            raise ValueError(f"decisions[{index}] must be a ConsumerCandidateDecision")

        decision_run_id = _require_non_empty_string(
            decision.run_id,
            field_name=f"decisions[{index}].run_id",
        )
        if decision_run_id != manifest_run_id:
            raise ValueError(
                f"decisions[{index}].run_id mismatch: {decision_run_id!r} != {manifest_run_id!r}"
            )

        decision_session_id = _require_non_empty_string(
            decision.session_id,
            field_name=f"decisions[{index}].session_id",
        )
        if decision_session_id != expected_session_id:
            raise ValueError(
                "session_id mismatch: "
                f"decisions[{index}] has {decision_session_id!r}, expected {expected_session_id!r}"
            )

        decision_cid = _require_non_empty_string(
            decision.candidate_cid,
            field_name=f"decisions[{index}].candidate_cid",
        )
        if decision_cid not in known_recalled_cids:
            raise ValueError(
                f"decisions[{index}].candidate_cid {decision_cid!r} not found in recalled_cids"
            )
        decided_cids.add(decision_cid)

    return known_recalled_cids - decided_cids


def _decision_fate_map(manifest: ConsumerDecisionManifest) -> dict[tuple[str, str], str]:
    mapping: dict[tuple[str, str], str] = {}
    for index, decision in enumerate(manifest.decisions):
        if not isinstance(decision, ConsumerCandidateDecision):
            raise ValueError(f"decisions[{index}] must be a ConsumerCandidateDecision")

        key = (
            _require_non_empty_string(
                decision.session_id,
                field_name=f"decisions[{index}].session_id",
            ),
            _require_non_empty_string(
                decision.candidate_cid,
                field_name=f"decisions[{index}].candidate_cid",
            ),
        )
        mapping[key] = _require_non_empty_string(
            decision.fate,
            field_name=f"decisions[{index}].fate",
        )
    return mapping


def validate_replay(
    prev_manifest: ConsumerDecisionManifest,
    new_manifest: ConsumerDecisionManifest,
) -> None:
    if not isinstance(prev_manifest, ConsumerDecisionManifest):
        raise ValueError("prev_manifest must be a ConsumerDecisionManifest")
    if not isinstance(new_manifest, ConsumerDecisionManifest):
        raise ValueError("new_manifest must be a ConsumerDecisionManifest")

    validate_one_per_candidate(prev_manifest)
    validate_one_per_candidate(new_manifest)

    previous = _decision_fate_map(prev_manifest)
    new = _decision_fate_map(new_manifest)

    for key, fate in new.items():
        previous_fate = previous.get(key)
        if previous_fate is None:
            continue
        if previous_fate != fate:
            session_id, candidate_cid = key
            raise ConflictError(
                "decision already applied with different fate: "
                f"session_id={session_id!r}, candidate_cid={candidate_cid!r}, "
                f"previous={previous_fate!r}, new={fate!r}"
            )


def resolve_fate(
    manifest: ConsumerDecisionManifest,
    *,
    session_id: str,
    candidate_cid: str,
) -> ConsumerCandidateDecision:
    if not isinstance(manifest, ConsumerDecisionManifest):
        raise ValueError("manifest must be a ConsumerDecisionManifest")

    expected_session_id = _require_non_empty_string(session_id, field_name="session_id")
    expected_candidate_cid = _require_non_empty_string(candidate_cid, field_name="candidate_cid")

    for index, decision in enumerate(manifest.decisions):
        if not isinstance(decision, ConsumerCandidateDecision):
            raise ValueError(f"decisions[{index}] must be a ConsumerCandidateDecision")
        if (
            decision.session_id == expected_session_id
            and decision.candidate_cid == expected_candidate_cid
        ):
            return decision

    fallback_fate = _require_non_empty_string(manifest.default_fate, field_name="default_fate")
    if fallback_fate not in VALID_FATES:
        raise ValueError(f"default_fate must be one of {sorted(VALID_FATES)}")

    return ConsumerCandidateDecision(
        run_id=_require_non_empty_string(manifest.run_id, field_name="run_id"),
        session_id=expected_session_id,
        candidate_cid=expected_candidate_cid,
        fate=fallback_fate,
        coordinator_trace=_require_non_empty_string(
            manifest.coordinator_trace,
            field_name="coordinator_trace",
        ),
        reason="",
        note="",
    )


def default_primary_manifest(
    run_id: str,
    session_id: str,
    recalled_cids: Iterable[Any],
    *,
    coordinator_trace: str,
) -> ConsumerDecisionManifest:
    """Build the explicit primary policy manifest (accept eligible recalled candidates)."""

    _require_non_empty_string(run_id, field_name="run_id")
    _require_non_empty_string(session_id, field_name="session_id")
    _require_non_empty_string(coordinator_trace, field_name="coordinator_trace")
    _require_cid_set(recalled_cids, field_name="recalled_cids")

    return ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=run_id,
        policy_id=DEFAULT_PRIMARY_POLICY,
        default_fate="accept",
        decisions=(),
        coordinator_trace=coordinator_trace,
    )


__all__ = [
    "CONSUMER_DECISION_SCHEMA_VERSION",
    "VALID_FATES",
    "DEFAULT_PRIMARY_POLICY",
    "ConflictError",
    "ConsumerCandidateDecision",
    "ConsumerDecisionManifest",
    "validate_schema",
    "validate_one_per_candidate",
    "validate_correlation",
    "validate_replay",
    "resolve_fate",
    "default_primary_manifest",
]
