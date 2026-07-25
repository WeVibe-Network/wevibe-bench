"""Private committed-memory catalog for the cumulative benchmark coordinator.

This module intentionally splits storage into:

* ``PrivateCatalog``: private, restricted catalog that stores synthetic accepted
  comparison text for duplicate checks across reruns.
* ``PrivateReviewCard``: private, restricted pending-candidate review material
  for cross-process coordinator resume.
* ``SafeDecisionLedger``: append-only operational ledger that is git-safe and
  MUST NOT contain plaintext content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import stat
from typing import Any, Mapping

from ._validation import _coerce_mapping_like, _optional_str, _require_mapping, _require_non_empty_string
from .decision import CandidateDecision, DENY_FINAL, VERIFY
from .types import CUMULATIVE_SCHEMA_VERSION, SessionRecord

_PRIVATE_CATALOG_SUFFIX = ".catalog.jsonl"
_PRIVATE_REVIEW_SUFFIX = ".review.jsonl"
_FORBIDDEN_PLAINTEXT_KEYS = {"comparison_text", "text", "plaintext"}
CATALOG_SCHEMA_VERSION = CUMULATIVE_SCHEMA_VERSION


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_non_empty_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None




def _normalize_keywords(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        keyword = str(item).strip()
        if not keyword:
            continue
        normalized = keyword.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(keyword)
    return out


def _keyword_set(keywords: list[str]) -> set[str]:
    return {keyword.casefold() for keyword in keywords if keyword.strip()}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_jsonl_line(path: str, payload: Mapping[str, Any], *, create_mode: int) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, create_mode)
    try:
        total = 0
        while total < len(encoded):
            total += os.write(fd, encoded[total:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _looks_like_forbidden_plaintext_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().casefold() in _FORBIDDEN_PLAINTEXT_KEYS:
                return True
            if _looks_like_forbidden_plaintext_key(nested):
                return True
        return False

    if isinstance(value, list):
        for item in value:
            if _looks_like_forbidden_plaintext_key(item):
                return True
    return False


@dataclass
class CatalogRecord:
    submission_hash: str
    committed_id: str | None
    keywords: list[str] = field(default_factory=list)
    comparison_text: str = ""
    org_id: str = ""
    sequence_index: int = 0
    committing_identity: str = ""
    content_hash: str = ""
    committed_at: str = ""
    producer_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "submission_hash": self.submission_hash,
            "committed_id": self.committed_id,
            "keywords": list(self.keywords),
            "comparison_text": self.comparison_text,
            "org_id": self.org_id,
            "sequence_index": self.sequence_index,
            "committing_identity": self.committing_identity,
            "content_hash": self.content_hash,
            "committed_at": self.committed_at,
        }
        if self.producer_model is not None:
            payload["producer_model"] = self.producer_model
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CatalogRecord:
        data = _require_mapping(value, field_name="catalog record")
        return cls(
            submission_hash=_require_non_empty_string(
                data.get("submission_hash"),
                field_name="submission_hash",
            ),
            committed_id=_optional_str(data.get("committed_id")),
            keywords=_normalize_keywords(data.get("keywords")),
            comparison_text=_require_non_empty_text(
                data.get("comparison_text"),
                field_name="comparison_text",
            ),
            org_id=_require_non_empty_string(data.get("org_id"), field_name="org_id"),
            sequence_index=int(data.get("sequence_index", 0)),
            committing_identity=_require_non_empty_string(
                data.get("committing_identity"),
                field_name="committing_identity",
            ),
            content_hash=_require_non_empty_string(
                data.get("content_hash"),
                field_name="content_hash",
            ),
            committed_at=_require_non_empty_string(
                data.get("committed_at"),
                field_name="committed_at",
            ),
            producer_model=_optional_str(data.get("producer_model")),
        )


def _candidate_submission_hash(candidate: Mapping[str, Any]) -> str | None:
    for key in ("submission_hash", "candidate_ref", "id", "candidate_id"):
        submission_hash = _optional_str(candidate.get(key))
        if submission_hash is not None:
            return submission_hash
    return None


def _candidate_text(candidate: Mapping[str, Any]) -> str | None:
    for key in ("comparison_text", "text", "content"):
        text = _optional_text(candidate.get(key))
        if text is not None:
            return text
    return None


def _candidate_keywords(candidate: Mapping[str, Any]) -> list[str]:
    return _normalize_keywords(candidate.get("keywords"))


def redacted_candidate_ref(candidate: Mapping[str, Any]) -> dict[str, Any]:
    candidate_map = _require_mapping(candidate, field_name="candidate")

    submission_hash = _optional_str(candidate_map.get("submission_hash"))
    if submission_hash is None:
        raise ValueError("candidate submission_hash is required")

    candidate_text = _optional_text(candidate_map.get("text"))
    if candidate_text is None:
        candidate_text = _optional_text(candidate_map.get("comparison_text"))
    if candidate_text is None:
        candidate_text = _optional_text(candidate_map.get("content"))
    if candidate_text is None:
        candidate_text = _optional_text(candidate_map.get("plaintext"))
    if candidate_text is None:
        raise ValueError("candidate text is required")

    return {
        "submission_hash": submission_hash,
        "keywords": _candidate_keywords(candidate_map),
        "memory_type": _optional_str(candidate_map.get("memory_type")),
        "producer_model": _optional_str(candidate_map.get("producer_model")),
        "content_hash": _sha256_text(candidate_text),
    }


def _session_candidate_by_ref(session: SessionRecord, *, candidate_ref: str) -> dict[str, Any]:
    for raw_candidate in session.candidate_refs:
        if not isinstance(raw_candidate, Mapping):
            continue

        candidate = dict(raw_candidate)
        for key in ("submission_hash", "candidate_ref", "id", "candidate_id"):
            if _optional_str(candidate.get(key)) == candidate_ref:
                return candidate

    raise ValueError(
        f"candidate_ref {candidate_ref!r} not found in session.candidate_refs"
    )


def _coerce_catalog_record(value: Any) -> CatalogRecord:
    if isinstance(value, CatalogRecord):
        return value
    if isinstance(value, Mapping):
        return CatalogRecord.from_dict(value)
    raise ValueError("catalog entry must be a CatalogRecord or mapping")


class PrivateCatalog:
    """Private append-only catalog that stores synthetic accepted comparison text."""

    def __init__(self, path: str, *, keyword_overlap_fraction: float = 0.6) -> None:
        path_value = os.fspath(path)
        if not isinstance(path_value, str):
            raise ValueError("path must be a string path")

        normalized_path = os.path.abspath(path_value)
        parent_dir = os.path.dirname(normalized_path)
        if not parent_dir or not os.path.isdir(parent_dir):
            raise ValueError("catalog parent directory must already exist")
        if not normalized_path.endswith(_PRIVATE_CATALOG_SUFFIX):
            raise ValueError(
                f"catalog path must end with {_PRIVATE_CATALOG_SUFFIX!r} so it remains gitignored"
            )

        overlap_fraction = float(keyword_overlap_fraction)
        if overlap_fraction < 0.0 or overlap_fraction > 1.0:
            raise ValueError("keyword_overlap_fraction must be in [0.0, 1.0]")

        self.path = normalized_path
        self.keyword_overlap_fraction = overlap_fraction

    def append(self, record: CatalogRecord) -> None:
        catalog_record = _coerce_catalog_record(record)

        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.chmod(self.path, 0o600)
            current_mode = stat.S_IMODE(os.stat(self.path).st_mode)
            if current_mode != 0o600:
                os.chmod(self.path, 0o600)

            payload = (
                json.dumps(catalog_record.to_dict(), ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            total = 0
            while total < len(payload):
                total += os.write(fd, payload[total:])
            os.fsync(fd)
        finally:
            os.close(fd)

    def load(self) -> list[CatalogRecord]:
        if not os.path.exists(self.path):
            return []

        out: list[CatalogRecord] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid JSON in catalog line {line_number}"
                    ) from exc

                out.append(CatalogRecord.from_dict(_require_mapping(decoded, field_name="catalog line")))
        return out

    def find_duplicates(self, candidate: Any) -> list[CatalogRecord]:
        candidate_map = _coerce_mapping_like(candidate, field_name="candidate")

        candidate_submission_hash = _candidate_submission_hash(candidate_map)
        candidate_text = _candidate_text(candidate_map)
        candidate_content_hash = _sha256_text(candidate_text) if candidate_text is not None else None
        candidate_keywords = _candidate_keywords(candidate_map)
        candidate_keyword_set = _keyword_set(candidate_keywords)

        matches: list[CatalogRecord] = []
        seen: set[tuple[str, str | None, str, int]] = set()

        for record in self.load():
            exact_by_hash = (
                candidate_content_hash is not None and record.content_hash == candidate_content_hash
            )
            exact_by_submission = (
                candidate_submission_hash is not None
                and record.submission_hash == candidate_submission_hash
            )

            keyword_advisory = False
            if candidate_keyword_set:
                overlap_count = len(candidate_keyword_set & _keyword_set(record.keywords))
                overlap_fraction = overlap_count / float(len(candidate_keyword_set))
                keyword_advisory = overlap_fraction >= self.keyword_overlap_fraction

            if not (exact_by_hash or exact_by_submission or keyword_advisory):
                continue

            marker = (
                record.submission_hash,
                record.committed_id,
                record.content_hash,
                record.sequence_index,
            )
            if marker in seen:
                continue
            seen.add(marker)
            matches.append(record)

        return matches

    def catalog_at_commit(
        self,
        session: SessionRecord,
        candidate_decision: CandidateDecision | Mapping[str, Any],
        committed_id: str,
        comparison_text: str,
        committing_identity: str,
        producer_model: str | None = None,
    ) -> CatalogRecord:
        if not isinstance(session, SessionRecord):
            raise ValueError("session must be a SessionRecord")

        if isinstance(candidate_decision, CandidateDecision):
            decision = candidate_decision
        elif isinstance(candidate_decision, Mapping):
            decision = CandidateDecision.from_dict(candidate_decision)
        else:
            raise ValueError("candidate_decision must be CandidateDecision or mapping")

        verdict = _require_non_empty_string(decision.verdict, field_name="candidate_decision.verdict")
        if verdict != VERIFY:
            raise ValueError(
                "catalog_at_commit only accepts VERIFY decisions "
                f"(got {verdict!r}; non-committing verdict is {DENY_FINAL!r})"
            )

        candidate_ref = _require_non_empty_string(
            decision.candidate_ref,
            field_name="candidate_decision.candidate_ref",
        )
        session_candidate = _session_candidate_by_ref(session, candidate_ref=candidate_ref)

        submission_hash = _optional_str(session_candidate.get("submission_hash"))
        if submission_hash is None:
            raise ValueError(
                f"session candidate for ref {candidate_ref!r} missing submission_hash"
            )

        comparison_text_value = _require_non_empty_text(
            comparison_text,
            field_name="comparison_text",
        )

        record = CatalogRecord(
            submission_hash=submission_hash,
            committed_id=_require_non_empty_string(committed_id, field_name="committed_id"),
            keywords=_candidate_keywords(session_candidate),
            comparison_text=comparison_text_value,
            org_id=_require_non_empty_string(session.org_id, field_name="session.org_id"),
            sequence_index=int(session.sequence_index),
            committing_identity=_require_non_empty_string(
                committing_identity,
                field_name="committing_identity",
            ),
            content_hash=_sha256_text(comparison_text_value),
            committed_at=_utc_now_iso(),
            producer_model=_optional_str(producer_model),
        )
        self.append(record)
        return record


class PrivateReviewCard:
    """Private append-only pending-candidate review material with synthetic text."""

    def __init__(self, path: str) -> None:
        path_value = os.fspath(path)
        if not isinstance(path_value, str):
            raise ValueError("path must be a string path")

        normalized_path = os.path.abspath(path_value)
        parent_dir = os.path.dirname(normalized_path)
        if not parent_dir or not os.path.isdir(parent_dir):
            raise ValueError("review-card parent directory must already exist")
        if not normalized_path.endswith(_PRIVATE_REVIEW_SUFFIX):
            raise ValueError(
                f"review-card path must end with {_PRIVATE_REVIEW_SUFFIX!r} so it remains gitignored"
            )

        self.path = normalized_path

    @staticmethod
    def _coerce_record(value: Mapping[str, Any], *, field_name: str) -> dict[str, Any]:
        data = _require_mapping(value, field_name=field_name)

        submission_hash = _optional_str(data.get("submission_hash"))
        if submission_hash is None:
            raise ValueError(f"{field_name} submission_hash is required")

        comparison_text = _optional_text(data.get("comparison_text"))
        if comparison_text is None:
            raise ValueError(f"{field_name} comparison_text is required")

        content_hash = _require_non_empty_string(
            data.get("content_hash"),
            field_name=f"{field_name} content_hash",
        )
        expected_content_hash = _sha256_text(comparison_text)
        if content_hash != expected_content_hash:
            raise ValueError(f"{field_name} content_hash mismatch")

        return {
            "sequence_index": int(data.get("sequence_index", 0)),
            "submission_hash": submission_hash,
            "comparison_text": comparison_text,
            "keywords": _candidate_keywords(data),
            "memory_type": _optional_str(data.get("memory_type")),
            "org_id": _require_non_empty_string(data.get("org_id"), field_name=f"{field_name} org_id"),
            "content_hash": content_hash,
            "written_at": _require_non_empty_string(
                data.get("written_at"),
                field_name=f"{field_name} written_at",
            ),
        }

    def write_session(self, session: SessionRecord) -> int:
        if not isinstance(session, SessionRecord):
            raise ValueError("session must be a SessionRecord")

        sequence_index = int(session.sequence_index)
        org_id = _require_non_empty_string(session.org_id, field_name="session.org_id")

        existing_pairs: set[tuple[int, str]] = {
            (int(record["sequence_index"]), record["submission_hash"]) for record in self.load()
        }

        written = 0
        for index, raw_candidate in enumerate(session.candidate_refs):
            candidate = _require_mapping(
                raw_candidate,
                field_name=f"session.candidate_refs[{index}]",
            )

            submission_hash = _optional_str(candidate.get("submission_hash"))
            if submission_hash is None:
                raise ValueError(
                    f"session.candidate_refs[{index}].submission_hash is required"
                )

            comparison_text = _optional_text(candidate.get("text"))
            if comparison_text is None:
                raise ValueError(f"session.candidate_refs[{index}].text is required")

            marker = (sequence_index, submission_hash)
            if marker in existing_pairs:
                continue

            payload = {
                "sequence_index": sequence_index,
                "submission_hash": submission_hash,
                "comparison_text": comparison_text,
                "keywords": _candidate_keywords(candidate),
                "memory_type": _optional_str(candidate.get("memory_type")),
                "org_id": org_id,
                "content_hash": _sha256_text(comparison_text),
                "written_at": _utc_now_iso(),
            }

            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.chmod(self.path, 0o600)
                current_mode = stat.S_IMODE(os.stat(self.path).st_mode)
                if current_mode != 0o600:
                    os.chmod(self.path, 0o600)

                encoded = (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
                ).encode("utf-8")
                total = 0
                while total < len(encoded):
                    total += os.write(fd, encoded[total:])
                os.fsync(fd)
            finally:
                os.close(fd)

            existing_pairs.add(marker)
            written += 1

        return written

    def load(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []

        out: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid JSON in review card line {line_number}"
                    ) from exc

                out.append(
                    self._coerce_record(
                        _require_mapping(decoded, field_name="review card line"),
                        field_name=f"review card line {line_number}",
                    )
                )
        return out

    def lookup_text(self, submission_hash: str) -> str | None:
        required_submission_hash = _require_non_empty_string(
            submission_hash,
            field_name="submission_hash",
        )
        for record in reversed(self.load()):
            if record["submission_hash"] == required_submission_hash:
                return record["comparison_text"]
        return None

    def session_material(self, sequence_index: int) -> list[dict[str, Any]]:
        target_sequence_index = int(sequence_index)
        material: list[dict[str, Any]] = []
        for record in self.load():
            if int(record["sequence_index"]) != target_sequence_index:
                continue
            material.append(
                {
                    "submission_hash": record["submission_hash"],
                    "comparison_text": record["comparison_text"],
                    "keywords": list(record["keywords"]),
                    "memory_type": record.get("memory_type"),
                }
            )
        return material


def _authoritative_committed_id(item: Mapping[str, Any]) -> str | None:
    for key in ("committed_id", "cid", "id"):
        committed_id = _optional_str(item.get(key))
        if committed_id is not None:
            return committed_id
    return None


def _safe_catalog_report_view(record: CatalogRecord) -> dict[str, Any]:
    return {
        "submission_hash": record.submission_hash,
        "committed_id": record.committed_id,
        "content_hash": record.content_hash,
        "org_id": record.org_id,
        "sequence_index": record.sequence_index,
        "committing_identity": record.committing_identity,
        "keyword_count": len(record.keywords),
        "committed_at": record.committed_at,
    }


def reconcile(
    catalog_records: list[CatalogRecord] | list[Mapping[str, Any]],
    authoritative_inventory: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare local catalog state against chain/hub authoritative inventory.

    The catalog is never authoritative; this report identifies divergence in both
    directions without exposing plaintext content.
    """

    normalized_catalog: list[CatalogRecord] = [_coerce_catalog_record(record) for record in catalog_records]

    normalized_authoritative: list[dict[str, Any]] = [
        _require_mapping(item, field_name="authoritative_inventory item")
        for item in authoritative_inventory
    ]

    authoritative_by_id: dict[str, set[int]] = {}
    authoritative_by_hash: dict[str, set[int]] = {}
    for index, item in enumerate(normalized_authoritative):
        committed_id = _authoritative_committed_id(item)
        if committed_id is not None:
            authoritative_by_id.setdefault(committed_id, set()).add(index)

        content_hash = _optional_str(item.get("content_hash"))
        if content_hash is not None:
            authoritative_by_hash.setdefault(content_hash, set()).add(index)

    in_catalog_not_chain: list[dict[str, Any]] = []
    identity_mismatch: list[dict[str, Any]] = []
    identity_mismatch_seen: set[tuple[str, str, str]] = set()
    matched_authoritative_indexes: set[int] = set()

    for record in normalized_catalog:
        matching_indexes: set[int] = set()

        if record.committed_id is not None:
            matching_indexes |= authoritative_by_id.get(record.committed_id, set())
        matching_indexes |= authoritative_by_hash.get(record.content_hash, set())

        if not matching_indexes:
            orphan = _safe_catalog_report_view(record)
            orphan["status"] = "orphan"
            in_catalog_not_chain.append(orphan)
            continue

        matched_authoritative_indexes |= matching_indexes

        if record.committed_id is None:
            continue

        for index in authoritative_by_id.get(record.committed_id, set()):
            authoritative_identity = _optional_str(
                normalized_authoritative[index].get("committing_identity")
            )
            if authoritative_identity is None:
                continue
            if authoritative_identity == record.committing_identity:
                continue

            marker = (record.committed_id, record.committing_identity, authoritative_identity)
            if marker in identity_mismatch_seen:
                continue
            identity_mismatch_seen.add(marker)

            identity_mismatch.append(
                {
                    "committed_id": record.committed_id,
                    "content_hash": record.content_hash,
                    "catalog_committing_identity": record.committing_identity,
                    "authoritative_committing_identity": authoritative_identity,
                }
            )

    in_chain_not_catalog: list[dict[str, Any]] = []
    for index, authoritative in enumerate(normalized_authoritative):
        if index in matched_authoritative_indexes:
            continue

        in_chain_not_catalog.append(
            {
                "committed_id": _authoritative_committed_id(authoritative),
                "content_hash": _optional_str(authoritative.get("content_hash")),
                "committing_identity": _optional_str(authoritative.get("committing_identity")),
                "status": "content_unavailable",
            }
        )

    return {
        "in_catalog_not_chain": in_catalog_not_chain,
        "in_chain_not_catalog": in_chain_not_catalog,
        "identity_mismatch": identity_mismatch,
        "catalog_complete": len(in_chain_not_catalog) == 0,
        "counts": {
            "in_catalog_not_chain": len(in_catalog_not_chain),
            "in_chain_not_catalog": len(in_chain_not_catalog),
            "identity_mismatch": len(identity_mismatch),
        },
    }


class SafeDecisionLedger:
    """Public append-only, git-safe operation ledger (never stores plaintext)."""

    _ALLOWED_KEYS = {
        "sequence_index",
        "submission_hash",
        "committed_id",
        "cid",
        "verdict",
        "reason",
        "keyword_count",
        "content_hash",
        "duplicate_refs",
        "committing_identity",
        "counts",
        "timestamp",
    }

    @staticmethod
    def append_entry(path: str, entry: Mapping[str, Any] | Any) -> dict[str, Any]:
        path_value = os.fspath(path)
        if not isinstance(path_value, str):
            raise ValueError("ledger path must be a string path")
        normalized_path = os.path.abspath(path_value)

        parent_dir = os.path.dirname(normalized_path)
        if not parent_dir or not os.path.isdir(parent_dir):
            raise ValueError("ledger parent directory must already exist")

        entry_map = _coerce_mapping_like(entry, field_name="entry")

        if _looks_like_forbidden_plaintext_key(entry_map):
            raise ValueError(
                "ledger entry contains forbidden plaintext key "
                "(comparison_text/text/plaintext)"
            )

        unknown_keys = sorted(set(entry_map.keys()) - SafeDecisionLedger._ALLOWED_KEYS)
        if unknown_keys:
            raise ValueError(f"unsupported ledger keys: {unknown_keys}")

        for required_key in (
            "sequence_index",
            "submission_hash",
            "verdict",
            "reason",
            "keyword_count",
            "content_hash",
            "committing_identity",
        ):
            if required_key not in entry_map:
                raise ValueError(f"missing required ledger key: {required_key}")

        committed_id = _optional_str(entry_map.get("committed_id"))
        cid = _optional_str(entry_map.get("cid"))
        if committed_id is None:
            committed_id = cid
        elif cid is not None and committed_id != cid:
            raise ValueError("committed_id and cid disagree")

        verdict = _require_non_empty_string(entry_map.get("verdict"), field_name="verdict")
        if verdict not in {VERIFY, DENY_FINAL}:
            raise ValueError(f"verdict must be one of {[VERIFY, DENY_FINAL]}")

        counts_raw = entry_map.get("counts", {})
        if counts_raw is None:
            counts_map: dict[str, Any] = {}
        elif isinstance(counts_raw, Mapping):
            counts_map = {str(key): value for key, value in counts_raw.items()}
        else:
            raise ValueError("counts must be a mapping")

        duplicate_refs = _normalize_keywords(entry_map.get("duplicate_refs"))

        payload = {
            "sequence_index": int(entry_map["sequence_index"]),
            "submission_hash": _require_non_empty_string(
                entry_map["submission_hash"],
                field_name="submission_hash",
            ),
            "committed_id": committed_id,
            "verdict": verdict,
            "reason": _require_non_empty_string(entry_map["reason"], field_name="reason"),
            "keyword_count": int(entry_map["keyword_count"]),
            "content_hash": _require_non_empty_string(
                entry_map["content_hash"],
                field_name="content_hash",
            ),
            "duplicate_refs": duplicate_refs,
            "committing_identity": _require_non_empty_string(
                entry_map["committing_identity"],
                field_name="committing_identity",
            ),
            "counts": counts_map,
            "timestamp": _optional_str(entry_map.get("timestamp")) or _utc_now_iso(),
        }

        _write_jsonl_line(normalized_path, payload, create_mode=0o644)
        return payload


__all__ = [
    "CatalogRecord",
    "PrivateCatalog",
    "PrivateReviewCard",
    "SafeDecisionLedger",
    "redacted_candidate_ref",
    "reconcile",
]
