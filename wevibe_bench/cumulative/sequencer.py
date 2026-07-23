"""Cumulative phase-machine sequencer for benchmark session orchestration.

This module owns *only* deterministic sequencing + checkpointing.
All side effects are injected through ``SessionRunner`` and ``LeaderClient``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from .catalog import PrivateReviewCard, redacted_candidate_ref
from .decision import DENY_FINAL, DecisionManifest
from .leader_client import ApplyResult, LeaderClient
from .manifest import atomic_write, resume_or_create, roster_hash
from .ordering import build_schedule
from .progress import progress_from_cell_result
from .types import RosterEntry, SessionPhase, SessionRecord

_LOG = logging.getLogger(__name__)


class AwaitingCoordinatorReview(TypedDict):
    status: Literal["awaiting_coordinator_review"]
    sequence_index: int
    org_id: str
    extraction_job_id: str
    session_fp: str
    candidate_count: int


class SessionCommitted(TypedDict):
    status: Literal["session_committed"]
    sequence_index: int
    committed_ids: list[str]
    denied_refs: list[str]
    all_denied: bool
    next_index: int


class DoneState(TypedDict):
    status: Literal["done"]


StepUntilReviewResult = AwaitingCoordinatorReview | DoneState
ResumeWithDecisionResult = SessionCommitted | DoneState


@runtime_checkable
class SessionRunner(Protocol):
    """Injected runtime seam for all side-effecting per-session operations."""

    def prepare_fixture(self, session: SessionRecord) -> None:
        """Reset the per-session coding fixture (never the cumulative corpus)."""

    def run_session(self, session: SessionRecord) -> object:
        """Execute one coding session and return telemetry (BackgammonCellResult-shaped)."""

    def extract(self, session: SessionRecord) -> dict[str, Any]:
        """Run the normal extraction pipeline and return candidate + job/session metadata."""

    def index_ready(self, session: SessionRecord) -> bool:
        """Return True once committed memories are visible/indexed for this session."""


class CumulativeSequencer:
    def __init__(
        self,
        manifest_path: str | os.PathLike[str],
        *,
        runner: SessionRunner,
        leader_client: LeaderClient,
        review_card: PrivateReviewCard,
        roster: list[RosterEntry],
        seed: int,
        task: str,
        org_id: str,
        config_fingerprint: str,
        on_budget: int,
    ) -> None:
        if not isinstance(runner, SessionRunner):
            raise ValueError("runner must implement SessionRunner")
        if not isinstance(leader_client, LeaderClient):
            raise ValueError("leader_client must be a LeaderClient")
        if review_card is None or not isinstance(review_card, PrivateReviewCard):
            raise ValueError("review_card must be a PrivateReviewCard")

        normalized_manifest_path = os.path.abspath(os.fspath(manifest_path))
        if not normalized_manifest_path:
            raise ValueError("manifest_path must be a non-empty path")

        planned_roster_hash = roster_hash(roster)
        planned_schedule = build_schedule(
            roster,
            seed=seed,
            roster_hash=planned_roster_hash,
            on_budget=on_budget,
        )

        manifest = resume_or_create(
            normalized_manifest_path,
            roster=roster,
            seed=seed,
            task=task,
            org_id=org_id,
            config_fingerprint=config_fingerprint,
            schedule=planned_schedule,
        )

        if manifest.org_id != org_id:
            raise ValueError(
                "cannot resume: org_id drift detected "
                f"(manifest={manifest.org_id!r} expected={org_id!r})"
            )
        if manifest.config_fingerprint != config_fingerprint:
            raise ValueError(
                "cannot resume: config_fingerprint drift detected "
                f"(manifest={manifest.config_fingerprint!r} expected={config_fingerprint!r})"
            )

        manifest_schedule = [item.to_dict() for item in manifest.schedule]
        expected_schedule = [item.to_dict() for item in planned_schedule]
        if manifest_schedule != expected_schedule:
            raise ValueError(
                "cannot resume: schedule drift detected; start a fresh run"
            )

        self._manifest_path = normalized_manifest_path
        self._runner = runner
        self._leader_client = leader_client
        self._review_card = review_card
        self._manifest = manifest

        if not self._manifest.session_records:
            self._manifest.session_records = [
                SessionRecord(
                    sequence_index=session.sequence_index,
                    model=session.model,
                    provider_pin=session.provider_pin,
                    memory_mode=session.memory_mode,
                    phase_group=session.phase_group,
                    phase=SessionPhase.PREPARE_FIXTURE.value,
                    org_id=org_id,
                )
                for session in self._manifest.schedule
            ]
            self._manifest.current_index = 0
            self._checkpoint()

        if self._manifest.current_index < 0:
            raise ValueError("manifest.current_index must be non-negative")
        if self._manifest.current_index > len(self._manifest.session_records):
            raise ValueError(
                "manifest.current_index exceeds session_records length "
                f"({self._manifest.current_index} > {len(self._manifest.session_records)})"
            )

    def current_session(self) -> SessionRecord | None:
        index = self._manifest.current_index
        if index < 0:
            raise ValueError("manifest.current_index must be non-negative")

        if index >= len(self._manifest.session_records):
            return None
        return self._manifest.session_records[index]

    def state(self) -> dict[str, Any]:
        session = self.current_session()
        total_sessions = len(self._manifest.session_records)
        completed = min(max(self._manifest.current_index, 0), total_sessions)
        remaining = max(total_sessions - completed, 0)
        phase = SessionPhase.DONE.value if session is None else self._phase_of(session).value
        return {
            "current_index": self._manifest.current_index,
            "phase": phase,
            "totals": {
                "sessions": total_sessions,
                "completed": completed,
                "remaining": remaining,
                "decision_applied": sum(
                    1 for record in self._manifest.session_records if record.decision_applied
                ),
            },
        }

    def step_until_review(self) -> StepUntilReviewResult:
        session = self.current_session()
        if session is None:
            return {"status": "done"}

        while True:
            phase = self._phase_of(session)

            if phase == SessionPhase.AWAIT_COORDINATOR_REVIEW:
                return self._paused_descriptor(session)

            if phase == SessionPhase.PREPARE_FIXTURE:
                _LOG.info(
                    "cumulative.sequencer.prepare_fixture sequence_index=%d memory_mode=%s",
                    session.sequence_index,
                    session.memory_mode,
                )
                session.org_id = self._manifest.org_id
                self._runner.prepare_fixture(session)
                session.set_phase(SessionPhase.RUN_SESSION)
                self._checkpoint()
                continue

            if phase == SessionPhase.RUN_SESSION:
                if session.started_at is None:
                    session.started_at = self._utc_now_iso()

                _LOG.info(
                    "cumulative.sequencer.run_session sequence_index=%d memory_mode=%s",
                    session.sequence_index,
                    session.memory_mode,
                )
                telemetry = self._runner.run_session(session)
                session.progress = progress_from_cell_result(telemetry).to_dict()
                session.set_phase(SessionPhase.EXTRACT_NORMAL_PIPELINE)
                self._checkpoint()
                continue

            if phase == SessionPhase.EXTRACT_NORMAL_PIPELINE:
                _LOG.info(
                    "cumulative.sequencer.extract_start sequence_index=%d memory_mode=%s",
                    session.sequence_index,
                    session.memory_mode,
                )
                extraction_payload = self._runner.extract(session)
                if not isinstance(extraction_payload, Mapping):
                    raise ValueError("runner.extract must return a mapping payload")

                self._populate_extraction_result(session, extraction_payload)
                session.set_phase(SessionPhase.AWAIT_COORDINATOR_REVIEW)
                self._checkpoint()

                paused = self._paused_descriptor(session)
                _LOG.info(
                    "cumulative.sequencer.awaiting_review sequence_index=%d org_id=%s job_id=%s session_fp=%s candidate_count=%d",
                    session.sequence_index,
                    paused["org_id"],
                    paused["extraction_job_id"],
                    paused["session_fp"],
                    paused["candidate_count"],
                )
                return paused

            if phase in {
                SessionPhase.LEADER_DECISION_APPLY,
                SessionPhase.COMMIT_INDEX_READY,
                SessionPhase.NEXT_SESSION,
            }:
                raise ValueError(
                    "current session already passed coordinator review; "
                    "use resume_with_decision to continue"
                )

            if phase == SessionPhase.DONE:
                return {"status": "done"}

            raise ValueError(f"unsupported session phase for step_until_review: {phase.value!r}")

    def resume_with_decision(
        self,
        decision_manifest_path: str | os.PathLike[str],
    ) -> ResumeWithDecisionResult:
        session = self.current_session()
        if session is None:
            return {"status": "done"}

        decision_manifest = self._load_decision_manifest(decision_manifest_path)
        phase = self._phase_of(session)
        if phase not in {
            SessionPhase.AWAIT_COORDINATOR_REVIEW,
            SessionPhase.LEADER_DECISION_APPLY,
            SessionPhase.COMMIT_INDEX_READY,
            SessionPhase.NEXT_SESSION,
        }:
            raise ValueError(
                "resume_with_decision requires current session phase "
                f"{SessionPhase.AWAIT_COORDINATOR_REVIEW.value!r} (or a crash-resume apply phase); "
                f"got {phase.value!r}"
            )

        applied_result: ApplyResult | None = None

        if phase == SessionPhase.AWAIT_COORDINATOR_REVIEW:
            session.set_phase(SessionPhase.LEADER_DECISION_APPLY)
            self._checkpoint()
            phase = SessionPhase.LEADER_DECISION_APPLY

        if phase == SessionPhase.LEADER_DECISION_APPLY:
            self._leader_client.validate(decision_manifest, session)
            applied_result = self._leader_client.apply(decision_manifest, session)

            session.committed_ids = list(applied_result.committed_ids)
            session.decision_applied = True
            session.corpus_delta = len(applied_result.committed_ids)

            if isinstance(session.progress, Mapping):
                progress = dict(session.progress)
                progress["accepted_count"] = len(applied_result.committed_ids)
                progress["rejected_count"] = len(applied_result.denied_refs)
                progress["rejected_reasons"] = [
                    str(outcome.get("reason", ""))
                    for outcome in applied_result.candidate_outcomes
                    if str(outcome.get("verdict", "")) == DENY_FINAL
                ]
                session.progress = progress

            leader_fp = str(getattr(self._leader_client, "_leader_fp", "unknown") or "unknown")
            session_fp = session.session_fp or "none"
            for denied_ref in applied_result.denied_refs:
                _LOG.info(
                    "cumulative.sequencer.deny_nonfatal sequence_index=%d session_fp=%s submission_hash=%s leader_fp=%s",
                    session.sequence_index,
                    session_fp,
                    denied_ref,
                    leader_fp,
                )

            session.set_phase(SessionPhase.COMMIT_INDEX_READY)
            self._checkpoint()
            phase = SessionPhase.COMMIT_INDEX_READY

        if applied_result is None:
            applied: dict[str, str] = {}
            denied_refs: list[str] = []
            seen_denied: set[str] = set()
            for candidate in decision_manifest.candidates:
                candidate_ref = str(candidate.candidate_ref)
                verdict = str(candidate.verdict)
                prior = applied.get(candidate_ref)
                if prior is None:
                    applied[candidate_ref] = verdict
                if verdict == DENY_FINAL and candidate_ref not in seen_denied:
                    seen_denied.add(candidate_ref)
                    denied_refs.append(candidate_ref)
            applied_result = ApplyResult(
                committed_ids=list(session.committed_ids),
                denied_refs=denied_refs,
                applied=applied,
                all_denied=bool(applied) and all(value == DENY_FINAL for value in applied.values()),
                candidate_outcomes=[],
            )

        if phase == SessionPhase.COMMIT_INDEX_READY:
            poll_count = 0
            while True:
                poll_count += 1
                if self._runner.index_ready(session):
                    _LOG.info(
                        "cumulative.sequencer.index_ready sequence_index=%d session_fp=%s job_id=%s polls=%d committed_count=%d",
                        session.sequence_index,
                        session.session_fp or "none",
                        session.extraction_job_id or "none",
                        poll_count,
                        len(session.committed_ids),
                    )
                    break

                if poll_count == 1 or poll_count % 10 == 0:
                    _LOG.info(
                        "cumulative.sequencer.index_wait sequence_index=%d session_fp=%s job_id=%s polls=%d committed_count=%d",
                        session.sequence_index,
                        session.session_fp or "none",
                        session.extraction_job_id or "none",
                        poll_count,
                        len(session.committed_ids),
                    )
                time.sleep(0.25)

            session.set_phase(SessionPhase.NEXT_SESSION)
            self._checkpoint()
            phase = SessionPhase.NEXT_SESSION

        if phase == SessionPhase.NEXT_SESSION:
            if self._manifest.current_index == session.sequence_index:
                self._manifest.current_index += 1
                if self._manifest.current_index < len(self._manifest.session_records):
                    next_session = self._manifest.session_records[self._manifest.current_index]
                    next_session.org_id = self._manifest.org_id
                    if self._phase_of(next_session) != SessionPhase.PREPARE_FIXTURE:
                        next_session.set_phase(SessionPhase.PREPARE_FIXTURE)
                self._checkpoint()
            elif self._manifest.current_index != session.sequence_index + 1:
                raise ValueError(
                    "manifest.current_index drift detected during NEXT_SESSION "
                    f"(current_index={self._manifest.current_index}, session.sequence_index={session.sequence_index})"
                )

            if self._manifest.current_index >= len(self._manifest.session_records):
                return {"status": "done"}

            return self._session_committed_result(
                session=session,
                applied_result=applied_result,
            )

        raise ValueError(f"unsupported session phase for resume_with_decision: {phase.value!r}")

    def _checkpoint(self) -> None:
        self._manifest.updated_at = self._utc_now_iso()
        atomic_write(self._manifest_path, self._manifest)

    def _phase_of(self, session: SessionRecord) -> SessionPhase:
        try:
            return SessionPhase(session.phase)
        except ValueError as exc:
            raise ValueError(
                f"session[{session.sequence_index}] has unknown phase {session.phase!r}"
            ) from exc

    def _paused_descriptor(self, session: SessionRecord) -> AwaitingCoordinatorReview:
        extraction_job_id = str(session.extraction_job_id or "").strip()
        if not extraction_job_id:
            raise ValueError(
                "cannot pause for review: session extraction_job_id is missing"
            )

        session_fp = str(session.session_fp or "").strip()
        if not session_fp:
            raise ValueError("cannot pause for review: session_fp is missing")

        org_id = str(session.org_id or self._manifest.org_id).strip()
        if not org_id:
            raise ValueError("cannot pause for review: org_id is missing")

        if session.extraction_candidate_count is None:
            candidate_count = len(session.candidate_refs)
        else:
            candidate_count = int(session.extraction_candidate_count)
        if candidate_count < 0:
            raise ValueError("extraction candidate_count must be non-negative")

        return {
            "status": "awaiting_coordinator_review",
            "sequence_index": session.sequence_index,
            "org_id": org_id,
            "extraction_job_id": extraction_job_id,
            "session_fp": session_fp,
            "candidate_count": candidate_count,
        }

    def _load_decision_manifest(
        self,
        decision_manifest_path: str | os.PathLike[str],
    ) -> DecisionManifest:
        normalized_path = os.path.abspath(os.fspath(decision_manifest_path))
        with open(normalized_path, "r", encoding="utf-8") as handle:
            decoded = json.load(handle)

        if not isinstance(decoded, Mapping):
            raise ValueError("decision manifest file must decode to a JSON object")

        manifest = DecisionManifest.from_dict(decoded)
        _LOG.info(
            "cumulative.sequencer.decision_loaded sequence_index=%d org_id=%s manifest_id=%s candidate_count=%d",
            manifest.sequence_index,
            manifest.org_id,
            manifest.manifest_id,
            len(manifest.candidates),
        )
        return manifest

    def _populate_extraction_result(
        self,
        session: SessionRecord,
        extraction_payload: Mapping[str, Any],
    ) -> None:
        raw_candidates = extraction_payload.get("candidate_refs")
        if not isinstance(raw_candidates, list):
            raise ValueError("extract payload field 'candidate_refs' must be a list")

        candidate_refs: list[dict[str, Any]] = []
        for index, raw_candidate in enumerate(raw_candidates):
            if not isinstance(raw_candidate, Mapping):
                raise ValueError(
                    f"extract payload candidate_refs[{index}] must be an object"
                )
            candidate = dict(raw_candidate)

            submission_hash = candidate.get("submission_hash")
            if not isinstance(submission_hash, str) or not submission_hash.strip():
                raise ValueError(
                    f"extract payload candidate_refs[{index}].submission_hash is required"
                )

            candidate_text = candidate.get("text")
            if not isinstance(candidate_text, str) or not candidate_text.strip():
                raise ValueError(
                    f"extract payload candidate_refs[{index}].text is required"
                )

            keywords = candidate.get("keywords")
            if not isinstance(keywords, list):
                raise ValueError(
                    f"extract payload candidate_refs[{index}].keywords must be a list"
                )

            memory_type = candidate.get("memory_type")
            if not isinstance(memory_type, str) or not memory_type.strip():
                raise ValueError(
                    f"extract payload candidate_refs[{index}].memory_type is required"
                )

            candidate_refs.append(candidate)

        extraction_job_id_raw = extraction_payload.get("extraction_job_id")
        if not isinstance(extraction_job_id_raw, str) or not extraction_job_id_raw.strip():
            raise ValueError("extract payload field 'extraction_job_id' is required")
        extraction_job_id = extraction_job_id_raw.strip()

        session_id_raw = extraction_payload.get("session_id")
        if not isinstance(session_id_raw, str) or not session_id_raw.strip():
            raise ValueError("extract payload field 'session_id' is required")
        session_id = session_id_raw.strip()

        extraction_candidate_count_raw = extraction_payload.get("extraction_candidate_count")
        if isinstance(extraction_candidate_count_raw, bool) or not isinstance(
            extraction_candidate_count_raw, int
        ):
            raise ValueError(
                "extract payload field 'extraction_candidate_count' must be an integer"
            )
        if extraction_candidate_count_raw < 0:
            raise ValueError(
                "extract payload field 'extraction_candidate_count' must be non-negative"
            )

        extraction_candidate_count = int(extraction_candidate_count_raw)
        if extraction_candidate_count != len(candidate_refs):
            raise ValueError(
                "extract payload candidate count mismatch: "
                f"extraction_candidate_count={extraction_candidate_count} "
                f"candidate_refs={len(candidate_refs)}"
            )

        session.org_id = session.org_id or self._manifest.org_id
        session.extraction_job_id = extraction_job_id
        session.session_id = session_id
        session.session_fp = SessionRecord.session_fp_of(session_id)
        session.candidate_refs = candidate_refs
        session.extraction_candidate_count = extraction_candidate_count

        self._review_card.write_session(session)
        session.candidate_refs = [
            redacted_candidate_ref(ref) for ref in session.candidate_refs
        ]

        _LOG.info(
            "cumulative.sequencer.review_card_written sequence_index=%d session_fp=%s candidate_count=%d",
            session.sequence_index,
            session.session_fp,
            extraction_candidate_count,
        )

        if isinstance(session.progress, Mapping):
            progress = dict(session.progress)
            progress["extraction_candidate_count"] = extraction_candidate_count
            session.progress = progress

        _LOG.info(
            "cumulative.sequencer.extract_end sequence_index=%d org_id=%s job_id=%s session_fp=%s candidate_count=%d",
            session.sequence_index,
            session.org_id,
            extraction_job_id,
            session.session_fp,
            extraction_candidate_count,
        )

    def _session_committed_result(
        self,
        *,
        session: SessionRecord,
        applied_result: ApplyResult,
    ) -> SessionCommitted:
        return {
            "status": "session_committed",
            "sequence_index": session.sequence_index,
            "committed_ids": list(applied_result.committed_ids),
            "denied_refs": list(applied_result.denied_refs),
            "all_denied": bool(applied_result.all_denied),
            "next_index": self._manifest.current_index,
        }

    @staticmethod
    def _utc_now_iso() -> str:
        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )


__all__ = [
    "SessionRunner",
    "CumulativeSequencer",
]
