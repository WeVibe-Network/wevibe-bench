"""Data model for GSTV run ledger serialization (logs-only, honest absence)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

HONEST_ABSENCE_NOTE = (
    "Missing/invalid source measurements are represented as explicit absence "
    "(null/empty/zero as shaped) and disclosed in integrity.gaps_disclosed; "
    "values are never inferred."
)

SignalKeyMode = Literal["parsed", "raw", "mixed", "absent"]


@dataclass(frozen=True)
class GoalReceipts:
    predicate_fps: list[str] = field(default_factory=list)
    negative_fps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "predicate_fps": list(self.predicate_fps),
            "negative_fps": list(self.negative_fps),
        }


@dataclass(frozen=True)
class GoalEntry:
    goal_id: str
    seal_fp: str | None
    closed: bool
    attempts_to_green: int | None
    sessions: int
    links: int
    gaps: int
    red_boundaries: int | None
    receipts: GoalReceipts
    unlock_fp: str | None
    signal_key_mode: SignalKeyMode

    def to_dict(self) -> dict[str, object]:
        return {
            "goal_id": self.goal_id,
            "seal_fp": self.seal_fp,
            "closed": self.closed,
            "attempts_to_green": self.attempts_to_green,
            "sessions": self.sessions,
            "links": self.links,
            "gaps": self.gaps,
            "red_boundaries": self.red_boundaries,
            "receipts": self.receipts.to_dict(),
            "unlock_fp": self.unlock_fp,
            "signal_key_mode": self.signal_key_mode,
        }


@dataclass(frozen=True)
class ProblemEntry:
    signal_key: str
    episode_id: str | None
    attempt_diff_fp: str | None
    candidate_hash: str | None
    leader_decision: str | None
    committed_cid: str | None
    injected_memory_overlap: bool | None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "signal_key": self.signal_key,
            "episode_id": self.episode_id,
            "attempt_diff_fp": self.attempt_diff_fp,
            "candidate_hash": self.candidate_hash,
            "leader_decision": self.leader_decision,
            "committed_cid": self.committed_cid,
            "injected_memory_overlap": self.injected_memory_overlap,
        }


@dataclass(frozen=True)
class Cadence:
    recalls: int
    gate_events: int
    injections: int
    serves: int
    unattributed_vector_only: int
    basis: str

    def to_dict(self) -> dict[str, int | str]:
        return {
            "recalls": self.recalls,
            "gate_events": self.gate_events,
            "injections": self.injections,
            "serves": self.serves,
            "unattributed_vector_only": self.unattributed_vector_only,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class Extraction:
    resolved: int
    emitted: int
    empty_reason: str | None
    invariant_violation: bool

    def to_dict(self) -> dict[str, int | str | bool | None]:
        return {
            "resolved": self.resolved,
            "emitted": self.emitted,
            "empty_reason": self.empty_reason,
            "invariant_violation": self.invariant_violation,
        }


@dataclass(frozen=True)
class UtilizationPair:
    memory_fp: str
    attempt_diff_fp: str
    similarity: float

    def to_dict(self) -> dict[str, str | float]:
        return {
            "memory_fp": self.memory_fp,
            "attempt_diff_fp": self.attempt_diff_fp,
            "similarity": self.similarity,
        }


@dataclass(frozen=True)
class UtilizationProxy:
    label: Literal["proxy-not-scoreboard"] = "proxy-not-scoreboard"
    pairs: list[UtilizationPair] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "pairs": [pair.to_dict() for pair in self.pairs],
        }


@dataclass(frozen=True)
class OpsCoverage:
    present: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "present": list(self.present),
            "absent": list(self.absent),
        }


@dataclass(frozen=True)
class Integrity:
    ledger_source: Literal["logs-only"] = "logs-only"
    gaps_disclosed: list[str] = field(default_factory=list)
    ops_coverage: OpsCoverage = field(default_factory=OpsCoverage)

    def to_dict(self) -> dict[str, object]:
        return {
            "ledger_source": self.ledger_source,
            "gaps_disclosed": list(self.gaps_disclosed),
            "ops_coverage": self.ops_coverage.to_dict(),
        }


@dataclass(frozen=True)
class RunLedger:
    run_id: str
    generated_at: str
    signal_key_mode: SignalKeyMode
    goals: list[GoalEntry] = field(default_factory=list)
    problems: list[ProblemEntry] = field(default_factory=list)
    cadence: Cadence = field(default_factory=lambda: Cadence(0, 0, 0, 0, 0, ""))
    extraction: Extraction = field(default_factory=lambda: Extraction(0, 0, None, False))
    utilization_proxy: UtilizationProxy = field(default_factory=UtilizationProxy)
    integrity: Integrity = field(default_factory=Integrity)
    v: Literal["gstv-run-v1"] = "gstv-run-v1"
    ledger_source: Literal["logs-only"] = "logs-only"

    def to_dict(self) -> dict[str, object]:
        return {
            "v": self.v,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "ledger_source": self.ledger_source,
            "signal_key_mode": self.signal_key_mode,
            "goals": [goal.to_dict() for goal in self.goals],
            "problems": [problem.to_dict() for problem in self.problems],
            "cadence": self.cadence.to_dict(),
            "extraction": self.extraction.to_dict(),
            "utilization_proxy": self.utilization_proxy.to_dict(),
            "integrity": self.integrity.to_dict(),
        }
