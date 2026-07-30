"""Shared contract leaf types for the cumulative benchmark sequencer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import enum
import hashlib
import json
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .consumer_gate import ConsumerGateOutcome, ServedStoreReconcile

CUMULATIVE_SCHEMA_VERSION = 1
# Telemetry seams that currently have no source in the adapter.
# Keep this as the mechanism for future genuinely-missing seams.
# Any ProgressVector field left None is still reported honestly via value-driven seam detection.
MISSING_TELEMETRY_SEAMS: tuple[str, ...] = ()


def _enum_value(value: Any) -> str:
    if isinstance(value, enum.Enum):
        return str(value.value)
    return "" if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return bool(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value)


def _optional_string_tuple(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple):
        return None
    return tuple(str(item) for item in value)


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _normalize_missing_telemetry_seams(
    provided: list[str] | None,
    *,
    seam_values: Mapping[str, Any],
) -> list[str]:
    normalized: list[str] = []
    for seam in provided or []:
        seam_name = str(seam).strip()
        if seam_name and seam_name not in normalized:
            normalized.append(seam_name)

    for seam in MISSING_TELEMETRY_SEAMS:
        if seam not in normalized:
            normalized.append(seam)

    for seam_name, seam_value in seam_values.items():
        if seam_value is None and seam_name not in normalized:
            normalized.append(seam_name)

    return normalized


class SessionPhase(str, enum.Enum):
    PREPARE_FIXTURE = "PREPARE_FIXTURE"
    RUN_SESSION = "RUN_SESSION"
    EXTRACT_NORMAL_PIPELINE = "EXTRACT_NORMAL_PIPELINE"
    AWAIT_COORDINATOR_REVIEW = "AWAIT_COORDINATOR_REVIEW"
    LEADER_DECISION_APPLY = "LEADER_DECISION_APPLY"
    COMMIT_INDEX_READY = "COMMIT_INDEX_READY"
    NEXT_SESSION = "NEXT_SESSION"
    DONE = "DONE"


class PhaseGroup(str, enum.Enum):
    OFF_BASELINE = "off_baseline"
    ON = "on"


PHASE_ORDER: tuple[SessionPhase, ...] = (
    SessionPhase.PREPARE_FIXTURE,
    SessionPhase.RUN_SESSION,
    SessionPhase.EXTRACT_NORMAL_PIPELINE,
    SessionPhase.AWAIT_COORDINATOR_REVIEW,
    SessionPhase.LEADER_DECISION_APPLY,
    SessionPhase.COMMIT_INDEX_READY,
    SessionPhase.NEXT_SESSION,
)


_PHASE_INDEX: dict[SessionPhase, int] = {
    phase: index for index, phase in enumerate(PHASE_ORDER)
}


def next_phase(phase: SessionPhase) -> SessionPhase | None:
    index = _PHASE_INDEX.get(phase)
    if index is None:
        return None
    next_index = index + 1
    if next_index >= len(PHASE_ORDER):
        return None
    return PHASE_ORDER[next_index]


@dataclass(frozen=True)
class RosterEntry:
    model: str
    role: str
    provider_pin: str
    # Dict fields are mutable; do not rely on dataclass hashing for this type.
    config_identity: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> list[Any]:
        return [
            self.model,
            self.role,
            self.provider_pin,
            json.dumps(self.config_identity, sort_keys=True, separators=(",", ":")),
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "role": self.role,
            "provider_pin": self.provider_pin,
            "config_identity": dict(self.config_identity),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RosterEntry:
        raw_identity = d.get("config_identity")
        config_identity = dict(raw_identity) if isinstance(raw_identity, Mapping) else {}
        return cls(
            model=str(d["model"]),
            role=str(d["role"]),
            provider_pin=str(d.get("provider_pin", "")),
            config_identity=config_identity,
        )


@dataclass(frozen=True)
class ScheduledSession:
    sequence_index: int
    model: str
    provider_pin: str
    memory_mode: str
    phase_group: str
    roster_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_index": self.sequence_index,
            "model": self.model,
            "provider_pin": self.provider_pin,
            "memory_mode": self.memory_mode,
            "phase_group": self.phase_group,
            "roster_index": self.roster_index,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ScheduledSession:
        return cls(
            sequence_index=int(d["sequence_index"]),
            model=str(d["model"]),
            provider_pin=str(d.get("provider_pin", "")),
            memory_mode=str(d["memory_mode"]),
            phase_group=_enum_value(d["phase_group"]),
            roster_index=int(d["roster_index"]),
        )


@dataclass
class ProgressVector:
    """None means telemetry is unavailable for this run (see missing_telemetry_seams), not zero."""

    problems_before: int | None = None
    problems_after: int | None = None
    resolved_count: int | None = None
    remaining_count: int | None = None
    full_green: bool = False
    attempts_to_green: int | None = None
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    wall_seconds: float = 0.0
    wall_cost_usd: float = 0.0
    injected_count: int | None = None
    injected_block_chars: int | None = None
    injected_block_est_tokens: int | None = None
    recall_fired_total: int | None = None
    recall_fired_user_message: int | None = None
    recall_fired_tool_failure: int | None = None
    recall_returned_total: int | None = None
    recall_returned_count_sum: int | None = None
    no_keywords_count: int | None = None
    served_attempted: int | None = None
    served_failed: int | None = None
    served_confirmed: int | None = None
    # Derived ratios: None means not derivable (for example, zero denominator), not missing telemetry.
    recall_return_rate: float | None = None
    inject_yield: float | None = None
    serve_success_rate: float | None = None
    consumer_injected_count: int | None = None
    extraction_candidate_count: int | None = None
    accepted_count: int | None = None
    rejected_count: int | None = None
    rejected_reasons: list[str] = field(default_factory=list)
    termination_reason: str = ""
    failed_gates: list[str] = field(default_factory=list)
    tool_calls: int | None = None
    test_invocations: int | None = None
    agentic_cycles: int | None = None
    http_429_count: int | None = None
    http_402_count: int | None = None
    retry_count: int | None = None
    upstream_error_count: int | None = None
    max_request_ms: int | None = None
    median_request_ms: int | None = None
    wall_near_timeout: bool | None = None
    missing_telemetry_seams: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.missing_telemetry_seams = _normalize_missing_telemetry_seams(
            self.missing_telemetry_seams,
            seam_values={
                "problems_before": self.problems_before,
                "problems_after": self.problems_after,
                "resolved_count": self.resolved_count,
                "remaining_count": self.remaining_count,
                "attempts_to_green": self.attempts_to_green,
                "injected_count": self.injected_count,
                "injected_block_chars": self.injected_block_chars,
                "injected_block_est_tokens": self.injected_block_est_tokens,
                "recall_fired_total": self.recall_fired_total,
                "recall_fired_user_message": self.recall_fired_user_message,
                "recall_fired_tool_failure": self.recall_fired_tool_failure,
                "recall_returned_total": self.recall_returned_total,
                "recall_returned_count_sum": self.recall_returned_count_sum,
                "no_keywords_count": self.no_keywords_count,
                "served_attempted": self.served_attempted,
                "served_failed": self.served_failed,
                "served_confirmed": self.served_confirmed,
                "consumer_injected_count": self.consumer_injected_count,
                "extraction_candidate_count": self.extraction_candidate_count,
                "accepted_count": self.accepted_count,
                "rejected_count": self.rejected_count,
                "tool_calls": self.tool_calls,
                "test_invocations": self.test_invocations,
                "agentic_cycles": self.agentic_cycles,
                "http_429_count": self.http_429_count,
                "http_402_count": self.http_402_count,
                "retry_count": self.retry_count,
                "upstream_error_count": self.upstream_error_count,
                "wall_near_timeout": self.wall_near_timeout,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        missing_telemetry_seams = _normalize_missing_telemetry_seams(
            self.missing_telemetry_seams,
            seam_values={
                "problems_before": self.problems_before,
                "problems_after": self.problems_after,
                "resolved_count": self.resolved_count,
                "remaining_count": self.remaining_count,
                "attempts_to_green": self.attempts_to_green,
                "injected_count": self.injected_count,
                "injected_block_chars": self.injected_block_chars,
                "injected_block_est_tokens": self.injected_block_est_tokens,
                "recall_fired_total": self.recall_fired_total,
                "recall_fired_user_message": self.recall_fired_user_message,
                "recall_fired_tool_failure": self.recall_fired_tool_failure,
                "recall_returned_total": self.recall_returned_total,
                "recall_returned_count_sum": self.recall_returned_count_sum,
                "no_keywords_count": self.no_keywords_count,
                "served_attempted": self.served_attempted,
                "served_failed": self.served_failed,
                "served_confirmed": self.served_confirmed,
                "consumer_injected_count": self.consumer_injected_count,
                "extraction_candidate_count": self.extraction_candidate_count,
                "accepted_count": self.accepted_count,
                "rejected_count": self.rejected_count,
                "tool_calls": self.tool_calls,
                "test_invocations": self.test_invocations,
                "agentic_cycles": self.agentic_cycles,
                "http_429_count": self.http_429_count,
                "http_402_count": self.http_402_count,
                "retry_count": self.retry_count,
                "upstream_error_count": self.upstream_error_count,
                "wall_near_timeout": self.wall_near_timeout,
            },
        )
        return {
            "problems_before": self.problems_before,
            "problems_after": self.problems_after,
            "resolved_count": self.resolved_count,
            "remaining_count": self.remaining_count,
            "full_green": self.full_green,
            "attempts_to_green": self.attempts_to_green,
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "wall_seconds": self.wall_seconds,
            "wall_cost_usd": self.wall_cost_usd,
            "injected_count": self.injected_count,
            "injected_block_chars": self.injected_block_chars,
            "injected_block_est_tokens": self.injected_block_est_tokens,
            "recall_fired_total": self.recall_fired_total,
            "recall_fired_user_message": self.recall_fired_user_message,
            "recall_fired_tool_failure": self.recall_fired_tool_failure,
            "recall_returned_total": self.recall_returned_total,
            "recall_returned_count_sum": self.recall_returned_count_sum,
            "no_keywords_count": self.no_keywords_count,
            "served_attempted": self.served_attempted,
            "served_failed": self.served_failed,
            "served_confirmed": self.served_confirmed,
            # Derived ratios: not telemetry seams.
            "recall_return_rate": self.recall_return_rate,
            "inject_yield": self.inject_yield,
            "serve_success_rate": self.serve_success_rate,
            "consumer_injected_count": self.consumer_injected_count,
            "extraction_candidate_count": self.extraction_candidate_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "rejected_reasons": list(self.rejected_reasons),
            "termination_reason": self.termination_reason,
            "failed_gates": list(self.failed_gates),
            "tool_calls": self.tool_calls,
            "test_invocations": self.test_invocations,
            "agentic_cycles": self.agentic_cycles,
            "http_429_count": self.http_429_count,
            "http_402_count": self.http_402_count,
            "retry_count": self.retry_count,
            "upstream_error_count": self.upstream_error_count,
            "max_request_ms": self.max_request_ms,
            "median_request_ms": self.median_request_ms,
            "wall_near_timeout": self.wall_near_timeout,
            "missing_telemetry_seams": missing_telemetry_seams,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ProgressVector:
        return cls(
            problems_before=_optional_int(d.get("problems_before")),
            problems_after=_optional_int(d.get("problems_after")),
            resolved_count=_optional_int(d.get("resolved_count")),
            remaining_count=_optional_int(d.get("remaining_count")),
            full_green=bool(d.get("full_green", False)),
            attempts_to_green=_optional_int(d.get("attempts_to_green")),
            turns=int(d.get("turns", 0)),
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            total_tokens=int(d.get("total_tokens", 0)),
            wall_seconds=float(d.get("wall_seconds", 0.0)),
            wall_cost_usd=float(d.get("wall_cost_usd", 0.0)),
            injected_count=_optional_int(d.get("injected_count")),
            injected_block_chars=_optional_int(d.get("injected_block_chars")),
            injected_block_est_tokens=_optional_int(d.get("injected_block_est_tokens")),
            recall_fired_total=_optional_int(d.get("recall_fired_total")),
            recall_fired_user_message=_optional_int(d.get("recall_fired_user_message")),
            recall_fired_tool_failure=_optional_int(d.get("recall_fired_tool_failure")),
            recall_returned_total=_optional_int(d.get("recall_returned_total")),
            recall_returned_count_sum=_optional_int(d.get("recall_returned_count_sum")),
            no_keywords_count=_optional_int(d.get("no_keywords_count")),
            served_attempted=_optional_int(d.get("served_attempted")),
            served_failed=_optional_int(d.get("served_failed")),
            served_confirmed=_optional_int(d.get("served_confirmed")),
            recall_return_rate=_optional_float(d.get("recall_return_rate")),
            inject_yield=_optional_float(d.get("inject_yield")),
            serve_success_rate=_optional_float(d.get("serve_success_rate")),
            consumer_injected_count=_optional_int(d.get("consumer_injected_count")),
            extraction_candidate_count=_optional_int(d.get("extraction_candidate_count")),
            accepted_count=_optional_int(d.get("accepted_count")),
            rejected_count=_optional_int(d.get("rejected_count")),
            rejected_reasons=_string_list(d.get("rejected_reasons")),
            termination_reason=str(d.get("termination_reason", "")),
            failed_gates=_string_list(d.get("failed_gates")),
            tool_calls=_optional_int(d.get("tool_calls")),
            test_invocations=_optional_int(d.get("test_invocations")),
            agentic_cycles=_optional_int(d.get("agentic_cycles")),
            http_429_count=_optional_int(d.get("http_429_count")),
            http_402_count=_optional_int(d.get("http_402_count")),
            retry_count=_optional_int(d.get("retry_count")),
            upstream_error_count=_optional_int(d.get("upstream_error_count")),
            max_request_ms=_optional_int(d.get("max_request_ms")),
            median_request_ms=_optional_int(d.get("median_request_ms")),
            wall_near_timeout=_optional_bool(d.get("wall_near_timeout")),
            missing_telemetry_seams=_string_list(d.get("missing_telemetry_seams")),
        )


@dataclass(frozen=True)
class ConsumerGateRecord:
    """Consumer-gate checkpoint telemetry for one ON session.

    None means telemetry is genuinely unavailable (for example, OFF sessions,
    runner hook absent, or an unavailable downstream receipt leg) and is never
    a stand-in for numeric zero/empty data.
    """

    policy_id: str
    coordinator_trace: str
    consumer_injected_count: int | None = None
    accepted_count: int | None = None
    denied_count: int | None = None
    blocked_count: int | None = None
    reported_count: int | None = None
    serve_receipt_status: str | None = None
    serve_receipt_ids: tuple[str, ...] | None = None
    denial_signal_status: str | None = None
    report_signal_status: str | None = None
    served_store_write_confirmed: bool | None = None
    served_store_missing_accepted: tuple[str, ...] = ()
    served_store_nonaccept_leaked: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "coordinator_trace": self.coordinator_trace,
            "consumer_injected_count": self.consumer_injected_count,
            "accepted_count": self.accepted_count,
            "denied_count": self.denied_count,
            "blocked_count": self.blocked_count,
            "reported_count": self.reported_count,
            "serve_receipt_status": self.serve_receipt_status,
            "serve_receipt_ids": (
                list(self.serve_receipt_ids)
                if self.serve_receipt_ids is not None
                else None
            ),
            "denial_signal_status": self.denial_signal_status,
            "report_signal_status": self.report_signal_status,
            "served_store_write_confirmed": self.served_store_write_confirmed,
            "served_store_missing_accepted": list(self.served_store_missing_accepted),
            "served_store_nonaccept_leaked": list(self.served_store_nonaccept_leaked),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ConsumerGateRecord:
        return cls(
            policy_id=str(d.get("policy_id", "")),
            coordinator_trace=str(d.get("coordinator_trace", "")),
            consumer_injected_count=_optional_int(d.get("consumer_injected_count")),
            accepted_count=_optional_int(d.get("accepted_count")),
            denied_count=_optional_int(d.get("denied_count")),
            blocked_count=_optional_int(d.get("blocked_count")),
            reported_count=_optional_int(d.get("reported_count")),
            serve_receipt_status=_optional_string(d.get("serve_receipt_status")),
            serve_receipt_ids=_optional_string_tuple(d.get("serve_receipt_ids")),
            denial_signal_status=_optional_string(d.get("denial_signal_status")),
            report_signal_status=_optional_string(d.get("report_signal_status")),
            served_store_write_confirmed=_optional_bool(d.get("served_store_write_confirmed")),
            served_store_missing_accepted=_string_tuple(d.get("served_store_missing_accepted")),
            served_store_nonaccept_leaked=_string_tuple(d.get("served_store_nonaccept_leaked")),
        )

    @classmethod
    def from_outcome(
        cls,
        outcome: ConsumerGateOutcome,
        reconcile: ServedStoreReconcile | None = None,
        *,
        serve_receipt_status: str | None = None,
        serve_receipt_ids: tuple[str, ...] | None = None,
        denial_signal_status: str | None = None,
        report_signal_status: str | None = None,
        durable_accepted_count: int | None = None,
        durable_injected_count: int | None = None,
    ) -> ConsumerGateRecord:
        """Build a checkpoint record from gate outcome + optional durable bridge counts.

        Durable counts are sourced from the bridge daemon delivered-decision record,
        which survives queue rewrites and reflects correlated accepted/drained truth.
        """

        accepted_count = _optional_int(getattr(outcome, "accept_count", None))
        if durable_accepted_count is not None:
            accepted_count = _optional_int(durable_accepted_count)
        denied_count = _optional_int(getattr(outcome, "deny_count", None))
        blocked_count = _optional_int(getattr(outcome, "block_count", None))
        reported_count = _optional_int(getattr(outcome, "report_count", None))
        consumer_injected_count = accepted_count
        if durable_injected_count is not None:
            consumer_injected_count = _optional_int(durable_injected_count)

        served_store_write_confirmed: bool | None = None
        served_store_missing_accepted: tuple[str, ...] = ()
        served_store_nonaccept_leaked: tuple[str, ...] = ()

        if reconcile is not None:
            served_store_missing_accepted = _string_tuple(
                getattr(reconcile, "missing_accepted", ())
            )
            served_store_nonaccept_leaked = _string_tuple(
                getattr(reconcile, "nonaccept_leaked", ())
            )
            served_store_present = _optional_bool(
                getattr(reconcile, "served_store_present", None)
            )
            served_store_write_confirmed = (
                served_store_present is True
                and not served_store_missing_accepted
                and not served_store_nonaccept_leaked
            )

        return cls(
            policy_id=_optional_string(getattr(outcome, "policy_id", "")) or "",
            coordinator_trace=_optional_string(getattr(outcome, "coordinator_trace", ""))
            or "",
            consumer_injected_count=consumer_injected_count,
            accepted_count=accepted_count,
            denied_count=denied_count,
            blocked_count=blocked_count,
            reported_count=reported_count,
            serve_receipt_status=_optional_string(serve_receipt_status),
            serve_receipt_ids=_optional_string_tuple(serve_receipt_ids),
            denial_signal_status=_optional_string(denial_signal_status),
            report_signal_status=_optional_string(report_signal_status),
            served_store_write_confirmed=served_store_write_confirmed,
            served_store_missing_accepted=served_store_missing_accepted,
            served_store_nonaccept_leaked=served_store_nonaccept_leaked,
        )


@dataclass
class SessionRecord:
    sequence_index: int
    model: str
    provider_pin: str
    memory_mode: str
    phase_group: str
    phase: str
    run_id: str | None = None
    run_label: str | None = None
    session_id: str | None = None
    org_id: str | None = None
    extraction_job_id: str | None = None
    session_fp: str | None = None
    candidate_refs: list[dict[str, Any]] = field(default_factory=list)
    extraction_candidate_count: int | None = None
    progress: dict[str, Any] | None = None
    consumer_gate: ConsumerGateRecord | None = None
    decision_applied: bool = False
    committed_ids: list[str] = field(default_factory=list)
    corpus_delta: int | None = None
    retry_count: int = 0
    resume_marker: str | None = None
    error: str | None = None
    started_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_index": self.sequence_index,
            "model": self.model,
            "provider_pin": self.provider_pin,
            "memory_mode": self.memory_mode,
            "phase_group": self.phase_group,
            "phase": self.phase,
            "run_id": self.run_id,
            "run_label": self.run_label,
            "session_id": self.session_id,
            "org_id": self.org_id,
            "extraction_job_id": self.extraction_job_id,
            "session_fp": self.session_fp,
            "candidate_refs": [dict(candidate) for candidate in self.candidate_refs],
            "extraction_candidate_count": self.extraction_candidate_count,
            "progress": dict(self.progress) if isinstance(self.progress, Mapping) else self.progress,
            "consumer_gate": self.consumer_gate.to_dict() if self.consumer_gate else None,
            "decision_applied": self.decision_applied,
            "committed_ids": list(self.committed_ids),
            "corpus_delta": self.corpus_delta,
            "retry_count": self.retry_count,
            "resume_marker": self.resume_marker,
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SessionRecord:
        progress_value = d.get("progress")
        progress_dict = dict(progress_value) if isinstance(progress_value, Mapping) else None
        consumer_gate_value = d.get("consumer_gate")
        consumer_gate = (
            ConsumerGateRecord.from_dict(consumer_gate_value)
            if isinstance(consumer_gate_value, Mapping)
            else None
        )
        return cls(
            sequence_index=int(d["sequence_index"]),
            model=str(d["model"]),
            provider_pin=str(d.get("provider_pin", "")),
            memory_mode=str(d.get("memory_mode", "off")),
            phase_group=_enum_value(d.get("phase_group", PhaseGroup.OFF_BASELINE.value)),
            phase=_enum_value(d.get("phase", SessionPhase.PREPARE_FIXTURE.value)),
            run_id=d.get("run_id"),
            run_label=d.get("run_label"),
            session_id=d.get("session_id"),
            org_id=d.get("org_id"),
            extraction_job_id=d.get("extraction_job_id"),
            session_fp=d.get("session_fp"),
            candidate_refs=_dict_list(d.get("candidate_refs")),
            extraction_candidate_count=_optional_int(d.get("extraction_candidate_count")),
            progress=progress_dict,
            consumer_gate=consumer_gate,
            decision_applied=bool(d.get("decision_applied", False)),
            committed_ids=_string_list(d.get("committed_ids")),
            corpus_delta=_optional_int(d.get("corpus_delta")),
            retry_count=int(d.get("retry_count", 0)),
            resume_marker=d.get("resume_marker"),
            error=d.get("error"),
            started_at=d.get("started_at"),
            updated_at=d.get("updated_at"),
        )

    def set_phase(self, phase: SessionPhase) -> None:
        self.phase = phase.value
        self.updated_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def session_fp_of(session_id: str) -> str:
        return hashlib.sha256(session_id.encode()).hexdigest()[:8]


__all__ = [
    "CUMULATIVE_SCHEMA_VERSION",
    "MISSING_TELEMETRY_SEAMS",
    "SessionPhase",
    "PhaseGroup",
    "PHASE_ORDER",
    "next_phase",
    "RosterEntry",
    "ScheduledSession",
    "ProgressVector",
    "ConsumerGateRecord",
    "SessionRecord",
]
