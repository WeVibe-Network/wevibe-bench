"""Canonical cumulative benchmark sequencer — organizational-learning loop."""

from __future__ import annotations

from .types import (
    CUMULATIVE_SCHEMA_VERSION,
    MISSING_TELEMETRY_SEAMS,
    PHASE_ORDER,
    PhaseGroup,
    ProgressVector,
    RosterEntry,
    ScheduledSession,
    SessionPhase,
    SessionRecord,
    next_phase,
)
from .manifest import CumulativeManifest, roster_hash
from .ordering import build_schedule
from .progress import progress_from_cell_result
from .decision import (
    CandidateDecision,
    ConflictError,
    DENY_FINAL,
    DecisionManifest,
    VERIFY,
)
from .catalog import (
    PrivateCatalog,
    PrivateReviewCard,
    SafeDecisionLedger,
    redacted_candidate_ref,
)
from .leader_client import ApplyResult, LeaderClient
from .sequencer import CumulativeSequencer, SessionRunner

__all__ = [
    "SessionPhase",
    "PhaseGroup",
    "PHASE_ORDER",
    "next_phase",
    "RosterEntry",
    "ScheduledSession",
    "SessionRecord",
    "ProgressVector",
    "CUMULATIVE_SCHEMA_VERSION",
    "MISSING_TELEMETRY_SEAMS",
    "CumulativeManifest",
    "roster_hash",
    "build_schedule",
    "progress_from_cell_result",
    "DecisionManifest",
    "CandidateDecision",
    "VERIFY",
    "DENY_FINAL",
    "ConflictError",
    "PrivateCatalog",
    "PrivateReviewCard",
    "SafeDecisionLedger",
    "redacted_candidate_ref",
    "LeaderClient",
    "ApplyResult",
    "CumulativeSequencer",
    "SessionRunner",
]
