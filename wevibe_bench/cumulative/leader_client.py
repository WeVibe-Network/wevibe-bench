"""Leader API integration client for cumulative coordinator decisions.

This module intentionally has *no* network/bootstrap logic of its own. All
runtime side effects flow through injected seams:

* ``m2proof.leader_verify_and_commit(...)`` for VERIFY decisions.
* ``hub_client.deny_submission(...)`` for DENY_FINAL decisions.

It enforces the manifest contract gate, applies decisions in-order, writes a
private catalog record for committed memories, and maintains two local ledgers:

* a public/git-safe SafeDecisionLedger JSONL file (hashes/CIDs only), and
* a private idempotency JSON file to make replay crash-safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from typing import Any, Mapping

from .catalog import (
    CatalogRecord,
    PrivateCatalog,
    PrivateReviewCard,
    SafeDecisionLedger,
    reconcile,
)
from .decision import (
    CandidateDecision,
    ConflictError,
    DENY_FINAL,
    DecisionManifest,
    VERIFY,
    applied_map,
    validate_correlation,
    validate_replay,
    validate_schema,
)
from .types import SessionRecord

_LOG = logging.getLogger(__name__)

_IDEMPOTENCY_VERDICT_KEY = "verdict"
_IDEMPOTENCY_COMMITTED_ID_KEY = "committed_id"

_COMMIT_ID_KEYS = ("committed_id", "cid", "id")
_COMMIT_STATUS_KEYS = ("status", "state", "phase")
_CANDIDATE_TEXT_KEYS = ("text", "comparison_text", "content", "plaintext")
_COMMITTING_IDENTITY_KEYS = (
    "committing_leader_pubkey",
    "committing_identity",
    "leader_pubkey",
    "committing_leader",
)


@dataclass(eq=True)
class ApplyResult:
    """Result summary for one manifest application pass."""

    committed_ids: list[str] = field(default_factory=list)
    denied_refs: list[str] = field(default_factory=list)
    applied: dict[str, str] = field(default_factory=dict)
    all_denied: bool = False
    candidate_outcomes: list[dict[str, Any]] = field(default_factory=list)


class LeaderClient:
    """Apply coordinator decisions through injected REAL leader/hub seams."""

    def __init__(
        self,
        m2proof: Any,
        hub_client: Any,
        leader: Any,
        catalog: PrivateCatalog,
        *,
        safe_ledger_path: str | os.PathLike[str],
        idempotency_ledger_path: str | os.PathLike[str],
        review_card: PrivateReviewCard | None = None,
    ) -> None:
        if m2proof is None:
            raise ValueError("m2proof is required")
        if hub_client is None:
            raise ValueError("hub_client is required")
        if leader is None:
            raise ValueError("leader is required")
        if not isinstance(catalog, PrivateCatalog):
            raise ValueError("catalog must be a PrivateCatalog")
        if review_card is not None and not isinstance(review_card, PrivateReviewCard):
            raise ValueError("review_card must be a PrivateReviewCard or None")

        self.m2proof = m2proof
        self.hub_client = hub_client
        self.leader = leader
        self.catalog = catalog
        self._review_card = review_card

        self.safe_ledger_path = self._normalize_file_path(
            safe_ledger_path,
            field_name="safe_ledger_path",
        )
        self.idempotency_ledger_path = self._normalize_file_path(
            idempotency_ledger_path,
            field_name="idempotency_ledger_path",
        )

        self._leader_pubkey_hex = self._require_non_empty_string(
            getattr(leader, "ed_pubkey_hex", None),
            field_name="leader.ed_pubkey_hex",
        )

        fp_method = getattr(leader, "ed_pub_fp", None)
        if not callable(fp_method):
            raise ValueError("leader.ed_pub_fp() is required")

        leader_fp = fp_method()
        self._leader_fp = self._require_non_empty_string(
            leader_fp,
            field_name="leader.ed_pub_fp()",
        )

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    @staticmethod
    def _require_non_empty_string(value: Any, *, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} is required")
        return value.strip()

    @classmethod
    def _normalize_file_path(
        cls,
        value: str | os.PathLike[str],
        *,
        field_name: str,
    ) -> str:
        path_value = os.fspath(value)
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError(f"{field_name} must be a non-empty path")

        normalized = os.path.abspath(path_value)
        parent_dir = os.path.dirname(normalized)
        if not parent_dir or not os.path.isdir(parent_dir):
            raise ValueError(f"{field_name} parent directory must already exist")
        return normalized

    @classmethod
    def _normalize_keywords(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            raw_items: list[Any] = [part.strip() for part in value.split(",")]
        elif isinstance(value, list):
            raw_items = list(value)
        else:
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            keyword = str(item).strip()
            if not keyword:
                continue
            marker = keyword.casefold()
            if marker in seen:
                continue
            seen.add(marker)
            normalized.append(keyword)
        return normalized

    @classmethod
    def _session_fp(cls, session: SessionRecord) -> str | None:
        explicit = cls._optional_str(session.session_fp)
        if explicit is not None:
            return explicit

        session_id = cls._optional_str(session.session_id)
        if session_id is None:
            return None
        return SessionRecord.session_fp_of(session_id)

    @classmethod
    def _candidate_submission_hash(
        cls,
        candidate: Mapping[str, Any],
        *,
        field_name: str,
    ) -> str:
        submission_hash = cls._optional_str(candidate.get("submission_hash"))
        if submission_hash is None:
            raise ValueError(f"{field_name} missing submission_hash")
        return submission_hash

    def _candidate_text(
        self,
        candidate: Mapping[str, Any],
        *,
        submission_hash: str,
    ) -> str:
        for key in _CANDIDATE_TEXT_KEYS:
            raw = candidate.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw

        if self._review_card is not None:
            review_card_text = self._review_card.lookup_text(submission_hash)
            if isinstance(review_card_text, str) and review_card_text.strip():
                return review_card_text

        raise ValueError(
            "missing synthetic comparison text (absent from session refs and review card)"
        )

    @classmethod
    def _session_candidate_by_submission_hash(
        cls,
        session: SessionRecord,
        *,
        submission_hash: str,
    ) -> dict[str, Any]:
        for index, raw_candidate in enumerate(session.candidate_refs):
            if not isinstance(raw_candidate, Mapping):
                raise ValueError(
                    "session.candidate_refs contains non-mapping entry "
                    f"at index {index}"
                )

            candidate = dict(raw_candidate)
            candidate_hash = cls._optional_str(candidate.get("submission_hash"))
            if candidate_hash == submission_hash:
                return candidate

        raise ValueError(
            f"session.candidate_refs missing submission_hash {submission_hash!r}"
        )

    @staticmethod
    def _sha256_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def _dict_value(cls, value: Any, *, field_name: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{field_name} must be a mapping")
        return dict(value)

    @classmethod
    def _status_label(cls, payload: Mapping[str, Any]) -> str | None:
        committed_flag = payload.get("committed")
        if committed_flag is True:
            return "committed"

        for key in _COMMIT_STATUS_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @classmethod
    def _extract_commit_outcome(
        cls,
        verify_payload: Any,
        *,
        submission_hash: str,
    ) -> tuple[str, str, str | None]:
        payload = cls._dict_value(verify_payload, field_name="leader_verify_and_commit payload")

        committed_id = None
        for key in _COMMIT_ID_KEYS:
            committed_id = cls._optional_str(payload.get(key))
            if committed_id is not None:
                break

        commit_status_payload = payload.get("commit_status")
        commit_status_label: str | None = None
        committing_identity: str | None = None

        if isinstance(commit_status_payload, Mapping):
            commit_status_map = dict(commit_status_payload)

            submissions_raw = commit_status_map.get("submissions")
            if isinstance(submissions_raw, list):
                matching_entries = [
                    dict(item)
                    for item in submissions_raw
                    if isinstance(item, Mapping)
                ]
                match: dict[str, Any] | None = None
                for entry in matching_entries:
                    if cls._optional_str(entry.get("submission_hash")) == submission_hash:
                        match = entry
                        break
                if match is None and len(matching_entries) == 1:
                    match = matching_entries[0]

                if match is not None:
                    if committed_id is None:
                        for key in (*_COMMIT_ID_KEYS, "submission_hash"):
                            committed_id = cls._optional_str(match.get(key))
                            if committed_id is not None:
                                break
                    commit_status_label = cls._status_label(match)

                    if committing_identity is None:
                        for key in _COMMITTING_IDENTITY_KEYS:
                            committing_identity = cls._optional_str(match.get(key))
                            if committing_identity is not None:
                                break

            if committed_id is None:
                for key in (*_COMMIT_ID_KEYS, "submission_hash"):
                    committed_id = cls._optional_str(commit_status_map.get(key))
                    if committed_id is not None:
                        break

            if commit_status_label is None:
                commit_status_label = cls._status_label(commit_status_map)

            if committing_identity is None:
                for key in _COMMITTING_IDENTITY_KEYS:
                    committing_identity = cls._optional_str(commit_status_map.get(key))
                    if committing_identity is not None:
                        break
        elif isinstance(commit_status_payload, str) and commit_status_payload.strip():
            commit_status_label = commit_status_payload.strip()
        elif commit_status_payload is not None:
            raise ValueError(
                "leader_verify_and_commit commit_status must be mapping/string"
            )

        if committing_identity is None:
            for key in _COMMITTING_IDENTITY_KEYS:
                committing_identity = cls._optional_str(payload.get(key))
                if committing_identity is not None:
                    break

        if commit_status_label is None:
            raise ValueError(
                "leader_verify_and_commit payload missing commit status label"
            )

        if committed_id is None:
            committed_id = submission_hash

        return committed_id, commit_status_label, committing_identity

    @classmethod
    def _candidate_outcome(
        cls,
        *,
        candidate: CandidateDecision,
        committed_id: str | None,
    ) -> dict[str, Any]:
        return {
            "submission_hash": candidate.candidate_ref,
            "verdict": candidate.verdict,
            "reason": candidate.reason,
            "committed_id": committed_id,
            "duplicate_refs": list(candidate.duplicate_refs),
        }

    def _load_idempotency_ledger(self) -> dict[str, dict[str, str | None]]:
        path = self.idempotency_ledger_path
        if not os.path.exists(path):
            return {}

        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()

        if not raw:
            return {}

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("idempotency ledger contains invalid JSON") from exc

        if not isinstance(decoded, Mapping):
            raise ValueError("idempotency ledger must be a JSON object")

        normalized: dict[str, dict[str, str | None]] = {}
        for raw_submission_hash, raw_entry in decoded.items():
            submission_hash = self._require_non_empty_string(
                raw_submission_hash,
                field_name="idempotency ledger submission_hash",
            )
            entry = self._dict_value(raw_entry, field_name=f"idempotency ledger entry {submission_hash!r}")

            verdict = self._require_non_empty_string(
                entry.get(_IDEMPOTENCY_VERDICT_KEY),
                field_name=f"idempotency ledger {submission_hash!r}.verdict",
            )
            if verdict not in {VERIFY, DENY_FINAL}:
                raise ValueError(
                    f"idempotency ledger {submission_hash!r}.verdict must be "
                    f"{VERIFY!r} or {DENY_FINAL!r}"
                )

            committed_id = self._optional_str(entry.get(_IDEMPOTENCY_COMMITTED_ID_KEY))
            if verdict == VERIFY and committed_id is None:
                raise ValueError(
                    f"idempotency ledger {submission_hash!r} missing committed_id for VERIFY"
                )

            normalized[submission_hash] = {
                _IDEMPOTENCY_VERDICT_KEY: verdict,
                _IDEMPOTENCY_COMMITTED_ID_KEY: committed_id,
            }

        return normalized

    def _persist_idempotency_ledger(
        self,
        ledger: Mapping[str, Mapping[str, Any]],
    ) -> None:
        normalized: dict[str, dict[str, str | None]] = {}
        for raw_submission_hash, raw_entry in ledger.items():
            submission_hash = self._require_non_empty_string(
                raw_submission_hash,
                field_name="idempotency ledger write submission_hash",
            )
            entry = self._dict_value(
                raw_entry,
                field_name=f"idempotency ledger write entry {submission_hash!r}",
            )

            verdict = self._require_non_empty_string(
                entry.get(_IDEMPOTENCY_VERDICT_KEY),
                field_name=f"idempotency ledger write {submission_hash!r}.verdict",
            )
            if verdict not in {VERIFY, DENY_FINAL}:
                raise ValueError(
                    f"idempotency ledger write {submission_hash!r}.verdict must be "
                    f"{VERIFY!r} or {DENY_FINAL!r}"
                )

            committed_id = self._optional_str(entry.get(_IDEMPOTENCY_COMMITTED_ID_KEY))
            if verdict == VERIFY and committed_id is None:
                raise ValueError(
                    f"idempotency ledger write {submission_hash!r} missing committed_id for VERIFY"
                )

            normalized[submission_hash] = {
                _IDEMPOTENCY_VERDICT_KEY: verdict,
                _IDEMPOTENCY_COMMITTED_ID_KEY: committed_id,
            }

        temp_path = (
            f"{self.idempotency_ledger_path}.tmp."
            f"{os.getpid()}."
            f"{os.urandom(4).hex()}"
        )

        payload = json.dumps(
            normalized,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temp_path, self.idempotency_ledger_path)
            os.chmod(self.idempotency_ledger_path, 0o600)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def list_pending(self, session: SessionRecord) -> dict[str, Any]:
        if not isinstance(session, SessionRecord):
            raise ValueError("session must be a SessionRecord")

        pending_candidates: list[dict[str, Any]] = []
        for index, raw_candidate in enumerate(session.candidate_refs):
            if not isinstance(raw_candidate, Mapping):
                raise ValueError(
                    "session.candidate_refs contains non-mapping entry "
                    f"at index {index}"
                )

            candidate = dict(raw_candidate)
            submission_hash = self._candidate_submission_hash(
                candidate,
                field_name=f"session.candidate_refs[{index}]",
            )
            keywords = self._normalize_keywords(candidate.get("keywords"))
            pending_candidates.append(
                {
                    "submission_hash": submission_hash,
                    "keywords": keywords,
                    "keyword_count": len(keywords),
                }
            )

        extraction_candidate_count = (
            int(session.extraction_candidate_count)
            if session.extraction_candidate_count is not None
            else len(pending_candidates)
        )

        return {
            "sequence_index": int(session.sequence_index),
            "org_id": self._optional_str(session.org_id),
            "extraction_job_id": self._optional_str(session.extraction_job_id),
            "session_fp": self._session_fp(session),
            "extraction_candidate_count": extraction_candidate_count,
            "candidate_count": len(pending_candidates),
            "candidates": pending_candidates,
        }

    def list_inventory(self) -> list[CatalogRecord]:
        return self.catalog.load()

    def reconcile_inventory(
        self,
        authoritative_inventory: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(authoritative_inventory, list):
            raise ValueError("authoritative_inventory must be a list")

        authoritative: list[dict[str, Any]] = []
        for index, item in enumerate(authoritative_inventory):
            if not isinstance(item, Mapping):
                raise ValueError(
                    "authoritative_inventory contains non-mapping item "
                    f"at index {index}"
                )
            authoritative.append(dict(item))

        return reconcile(self.catalog.load(), authoritative)

    def validate(
        self,
        manifest: DecisionManifest,
        session: SessionRecord,
    ) -> None:
        validate_schema(manifest)
        validate_correlation(manifest, session)

        idempotency_ledger = self._load_idempotency_ledger()
        previous_applied = {
            submission_hash: str(entry[_IDEMPOTENCY_VERDICT_KEY])
            for submission_hash, entry in idempotency_ledger.items()
        }
        validate_replay(previous_applied, manifest)

        _LOG.info(
            "leader_client.validate leader_fp=%s sequence_index=%s org_id=%s candidate_count=%d",
            self._leader_fp,
            manifest.sequence_index,
            manifest.org_id,
            len(manifest.candidates),
        )

    @classmethod
    def _coerce_candidate_decision(
        cls,
        value: CandidateDecision | Mapping[str, Any],
        *,
        index: int,
    ) -> CandidateDecision:
        if isinstance(value, CandidateDecision):
            return value
        if isinstance(value, Mapping):
            return CandidateDecision.from_dict(value)
        raise ValueError(f"manifest.candidates[{index}] must be a CandidateDecision")

    @staticmethod
    def _append_unique(target: list[str], seen: set[str], value: str) -> None:
        if value in seen:
            return
        seen.add(value)
        target.append(value)

    def _append_verify_ledger_entry(
        self,
        *,
        session: SessionRecord,
        candidate: CandidateDecision,
        submission_hash: str,
        committed_id: str,
        keyword_count: int,
        content_hash: str,
        committing_identity: str,
    ) -> None:
        SafeDecisionLedger.append_entry(
            self.safe_ledger_path,
            {
                "sequence_index": int(session.sequence_index),
                "submission_hash": submission_hash,
                "committed_id": committed_id,
                "cid": committed_id,
                "verdict": VERIFY,
                "reason": candidate.reason,
                "keyword_count": int(keyword_count),
                "content_hash": content_hash,
                "duplicate_refs": list(candidate.duplicate_refs),
                "committing_identity": committing_identity,
                "counts": {
                    "duplicate_refs": len(candidate.duplicate_refs),
                },
            },
        )

    def _append_denial_ledger_entry(
        self,
        *,
        session: SessionRecord,
        candidate: CandidateDecision,
        submission_hash: str,
        keyword_count: int,
        deny_status: str,
    ) -> None:
        SafeDecisionLedger.append_entry(
            self.safe_ledger_path,
            {
                "sequence_index": int(session.sequence_index),
                "submission_hash": submission_hash,
                "committed_id": None,
                "verdict": DENY_FINAL,
                "reason": candidate.reason,
                "keyword_count": int(keyword_count),
                "content_hash": self._sha256_text(
                    f"{DENY_FINAL}:{submission_hash}:{candidate.reason}"
                ),
                "duplicate_refs": list(candidate.duplicate_refs),
                "committing_identity": self._leader_pubkey_hex,
                "counts": {
                    "duplicate_refs": len(candidate.duplicate_refs),
                    "deny_status": deny_status,
                },
            },
        )

    def apply(
        self,
        manifest: DecisionManifest,
        session: SessionRecord,
    ) -> ApplyResult:
        self.validate(manifest, session)

        org_id = self._require_non_empty_string(
            session.org_id,
            field_name="session.org_id",
        )

        idempotency_ledger = self._load_idempotency_ledger()
        committed_ids: list[str] = []
        denied_refs: list[str] = []
        candidate_outcomes: list[dict[str, Any]] = []

        seen_committed_ids: set[str] = set()
        seen_denied_refs: set[str] = set()

        for index, raw_candidate in enumerate(manifest.candidates):
            candidate = self._coerce_candidate_decision(raw_candidate, index=index)
            submission_hash = self._require_non_empty_string(
                candidate.candidate_ref,
                field_name=f"manifest.candidates[{index}].candidate_ref",
            )
            verdict = self._require_non_empty_string(
                candidate.verdict,
                field_name=f"manifest.candidates[{index}].verdict",
            )

            prior = idempotency_ledger.get(submission_hash)
            if prior is not None:
                prior_verdict = self._require_non_empty_string(
                    prior.get(_IDEMPOTENCY_VERDICT_KEY),
                    field_name=f"idempotency ledger {submission_hash!r}.verdict",
                )
                if prior_verdict != verdict:
                    raise ConflictError(
                        f"candidate_ref {submission_hash!r} already applied with "
                        f"verdict {prior_verdict!r}; cannot apply {verdict!r}"
                    )

                prior_committed_id = self._optional_str(prior.get(_IDEMPOTENCY_COMMITTED_ID_KEY))
                if verdict == VERIFY:
                    committed_id = self._require_non_empty_string(
                        prior_committed_id,
                        field_name=f"idempotency ledger {submission_hash!r}.committed_id",
                    )
                    self._append_unique(
                        committed_ids,
                        seen_committed_ids,
                        committed_id,
                    )
                    candidate_outcomes.append(
                        self._candidate_outcome(candidate=candidate, committed_id=committed_id)
                    )
                elif verdict == DENY_FINAL:
                    self._append_unique(denied_refs, seen_denied_refs, submission_hash)
                    candidate_outcomes.append(
                        self._candidate_outcome(candidate=candidate, committed_id=None)
                    )
                else:
                    raise ValueError(
                        f"unsupported verdict {verdict!r} for candidate_ref {submission_hash!r}"
                    )

                self._persist_idempotency_ledger(idempotency_ledger)
                continue

            session_candidate = self._session_candidate_by_submission_hash(
                session,
                submission_hash=submission_hash,
            )
            keywords = self._normalize_keywords(session_candidate.get("keywords"))

            if verdict == VERIFY:
                comparison_text = self._candidate_text(
                    session_candidate,
                    submission_hash=submission_hash,
                )

                verify_payload = self.m2proof.leader_verify_and_commit(
                    org_id,
                    submission_hash,
                    keywords,
                )
                committed_id, commit_status, payload_committing_identity = self._extract_commit_outcome(
                    verify_payload,
                    submission_hash=submission_hash,
                )
                committing_identity = payload_committing_identity or self._leader_pubkey_hex

                catalog_record = self.catalog.catalog_at_commit(
                    session,
                    candidate,
                    committed_id,
                    comparison_text,
                    committing_identity=committing_identity,
                )

                self._append_verify_ledger_entry(
                    session=session,
                    candidate=candidate,
                    submission_hash=submission_hash,
                    committed_id=committed_id,
                    keyword_count=len(keywords),
                    content_hash=catalog_record.content_hash,
                    committing_identity=committing_identity,
                )

                idempotency_ledger[submission_hash] = {
                    _IDEMPOTENCY_VERDICT_KEY: VERIFY,
                    _IDEMPOTENCY_COMMITTED_ID_KEY: committed_id,
                }
                self._persist_idempotency_ledger(idempotency_ledger)

                self._append_unique(committed_ids, seen_committed_ids, committed_id)
                candidate_outcomes.append(
                    self._candidate_outcome(candidate=candidate, committed_id=committed_id)
                )

                _LOG.info(
                    "leader_client.verify leader_fp=%s submission_hash=%s committed_id=%s commit_status=%s keyword_count=%d",
                    self._leader_fp,
                    submission_hash,
                    committed_id,
                    commit_status,
                    len(keywords),
                )
                continue

            if verdict == DENY_FINAL:
                deny_status = "denied"
                try:
                    deny_payload = self.hub_client.deny_submission(
                        self.leader,
                        org_id,
                        submission_hash,
                        candidate.reason,
                    )
                    if isinstance(deny_payload, Mapping):
                        deny_status_value = self._optional_str(deny_payload.get("status"))
                        if deny_status_value is not None:
                            deny_status = deny_status_value
                    elif deny_payload is not None:
                        deny_status = type(deny_payload).__name__
                except Exception as exc:  # DENY_FINAL is non-fatal by contract.
                    deny_status = "error"
                    _LOG.warning(
                        "leader_client.deny_nonfatal leader_fp=%s submission_hash=%s error_type=%s",
                        self._leader_fp,
                        submission_hash,
                        type(exc).__name__,
                    )

                self._append_denial_ledger_entry(
                    session=session,
                    candidate=candidate,
                    submission_hash=submission_hash,
                    keyword_count=len(keywords),
                    deny_status=deny_status,
                )

                idempotency_ledger[submission_hash] = {
                    _IDEMPOTENCY_VERDICT_KEY: DENY_FINAL,
                    _IDEMPOTENCY_COMMITTED_ID_KEY: None,
                }
                self._persist_idempotency_ledger(idempotency_ledger)

                self._append_unique(denied_refs, seen_denied_refs, submission_hash)
                candidate_outcomes.append(
                    self._candidate_outcome(candidate=candidate, committed_id=None)
                )

                _LOG.info(
                    "leader_client.deny leader_fp=%s submission_hash=%s status=%s keyword_count=%d",
                    self._leader_fp,
                    submission_hash,
                    deny_status,
                    len(keywords),
                )
                continue

            raise ValueError(
                f"unsupported verdict {verdict!r} for candidate_ref {submission_hash!r}"
            )

        applied = applied_map(manifest)
        all_denied = bool(applied) and all(verdict == DENY_FINAL for verdict in applied.values())
        return ApplyResult(
            committed_ids=committed_ids,
            denied_refs=denied_refs,
            applied=applied,
            all_denied=all_denied,
            candidate_outcomes=candidate_outcomes,
        )


__all__ = [
    "ApplyResult",
    "LeaderClient",
]
