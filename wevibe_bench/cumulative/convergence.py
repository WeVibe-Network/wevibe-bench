"""Derived read-only convergence trend over checkpointed per-session progress.

This module computes a rollup from ``SessionRecord.progress`` values already
persisted in the cumulative manifest checkpoint. It does not add or mutate any
checkpoint schema fields.

Design invariants:
- Derived-only: trend is computed on read from existing session records.
- None-honest: ``None`` means unavailable; ``None`` values are excluded from
  aggregates and are never silently zero-filled.
- Repo hash/version convention: canonical JSON (sorted keys, compact
  separators) + SHA-256 fingerprint.
- Safe output/logging surface: counts, timings, and fingerprints only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .types import SessionRecord

CONVERGENCE_SCHEMA_VERSION = 1


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value)
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bytes | bytearray):
        return None
    return coerced


def _coerce_int(value: Any, *, default: int) -> int:
    parsed = _coerce_optional_int(value)
    if parsed is None:
        return default
    return parsed


def _coerce_float(value: Any, *, default: float) -> float:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ConvergencePoint:
    sequence_index: int
    session_fp: str
    problems_before: int | None
    problems_after: int | None
    resolved_count: int | None
    remaining_count: int | None
    full_green: bool
    attempts_to_green: int | None
    turns: int
    total_tokens: int
    wall_seconds: float
    wall_cost_usd: float
    tool_calls: int | None
    test_invocations: int | None
    agentic_cycles: int | None

    @classmethod
    def from_session_record(cls, record: SessionRecord) -> ConvergencePoint | None:
        """Build a point from one scored session.

        Returns ``None`` when ``record.progress`` is missing, which represents a
        not-yet-scored session and is excluded from the convergence trend.
        """

        progress = record.progress
        if not isinstance(progress, Mapping):
            return None

        session_fp = str(record.session_fp or "").strip()
        if not session_fp:
            session_id = str(record.session_id or "").strip()
            session_fp = SessionRecord.session_fp_of(session_id) if session_id else "none"

        return cls(
            sequence_index=int(record.sequence_index),
            session_fp=session_fp,
            problems_before=_coerce_optional_int(progress.get("problems_before")),
            problems_after=_coerce_optional_int(progress.get("problems_after")),
            resolved_count=_coerce_optional_int(progress.get("resolved_count")),
            remaining_count=_coerce_optional_int(progress.get("remaining_count")),
            full_green=bool(progress.get("full_green", False))
            if isinstance(progress.get("full_green", False), bool)
            else False,
            attempts_to_green=_coerce_optional_int(progress.get("attempts_to_green")),
            turns=_coerce_int(progress.get("turns"), default=0),
            total_tokens=_coerce_int(progress.get("total_tokens"), default=0),
            wall_seconds=_coerce_float(progress.get("wall_seconds"), default=0.0),
            wall_cost_usd=_coerce_float(progress.get("wall_cost_usd"), default=0.0),
            tool_calls=_coerce_optional_int(progress.get("tool_calls")),
            test_invocations=_coerce_optional_int(progress.get("test_invocations")),
            agentic_cycles=_coerce_optional_int(progress.get("agentic_cycles")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_index": self.sequence_index,
            "session_fp": self.session_fp,
            "problems_before": self.problems_before,
            "problems_after": self.problems_after,
            "resolved_count": self.resolved_count,
            "remaining_count": self.remaining_count,
            "full_green": self.full_green,
            "attempts_to_green": self.attempts_to_green,
            "turns": self.turns,
            "total_tokens": self.total_tokens,
            "wall_seconds": self.wall_seconds,
            "wall_cost_usd": self.wall_cost_usd,
            "tool_calls": self.tool_calls,
            "test_invocations": self.test_invocations,
            "agentic_cycles": self.agentic_cycles,
        }


@dataclass(frozen=True)
class ConvergenceTrend:
    schema_version: int
    points: tuple[ConvergencePoint, ...]
    sessions_completed: int
    sessions_green: int
    resolved_total: int | None
    tokens_total: int
    wall_seconds_total: float
    wall_cost_usd_total: float

    @property
    def trend_hash(self) -> str:
        """First-8 SHA-256 fingerprint of canonical JSON point dicts.

        This hash is intended for safe log correlation and derived-state
        fingerprinting.
        """

        canonical_points = [point.to_dict() for point in self.points]
        payload = json.dumps(canonical_points, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "points": [point.to_dict() for point in self.points],
            "sessions_completed": self.sessions_completed,
            "sessions_green": self.sessions_green,
            "resolved_total": self.resolved_total,
            "tokens_total": self.tokens_total,
            "wall_seconds_total": self.wall_seconds_total,
            "wall_cost_usd_total": self.wall_cost_usd_total,
            "trend_hash": self.trend_hash,
        }


def build_convergence_trend(session_records: Iterable[SessionRecord]) -> ConvergenceTrend:
    points = tuple(
        sorted(
            (
                point
                for point in (
                    ConvergencePoint.from_session_record(record)
                    for record in session_records
                )
                if point is not None
            ),
            key=lambda point: point.sequence_index,
        )
    )

    resolved_values = [point.resolved_count for point in points if point.resolved_count is not None]
    resolved_total = sum(resolved_values) if resolved_values else None

    return ConvergenceTrend(
        schema_version=CONVERGENCE_SCHEMA_VERSION,
        points=points,
        sessions_completed=len(points),
        sessions_green=sum(1 for point in points if point.full_green),
        resolved_total=resolved_total,
        tokens_total=sum(point.total_tokens for point in points),
        wall_seconds_total=sum(point.wall_seconds for point in points),
        wall_cost_usd_total=sum(point.wall_cost_usd for point in points),
    )


__all__ = [
    "CONVERGENCE_SCHEMA_VERSION",
    "ConvergencePoint",
    "ConvergenceTrend",
    "build_convergence_trend",
]
