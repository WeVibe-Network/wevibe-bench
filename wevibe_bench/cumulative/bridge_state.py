"""Durable bridge-daemon checkpoint state (no plugin/network/docker I/O)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping

from ._validation import _require_non_empty_string
from .consumer_decision import ConflictError as ConsumerConflictError

BRIDGE_STATE_SCHEMA_VERSION = 1

_VALID_FATES = frozenset({"accept", "deny", "block", "report"})
_FORBIDDEN_PLAINTEXT_KEYS = frozenset({"text", "transcript", "plaintext", "memory_text"})
_PLUGIN_OUTCOME_REF_BUCKETS = (
    "served_confirmed",
    "denied",
    "reported",
    "blocked",
    "missing_accepted",
    "nonaccept_leaked",
    "side_effect_timeouts",
)


def _optional_non_empty_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_non_empty_string(value, field_name=field_name)


def _require_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _scope_key(run_id: str, session_id: str) -> str:
    normalized_run_id = _require_non_empty_string(run_id, field_name="run_id")
    normalized_session_id = _require_non_empty_string(session_id, field_name="session_id")
    return f"{normalized_run_id}::{normalized_session_id}"


def _require_scope_key(value: Any, *, field_name: str) -> str:
    scope_key = _require_non_empty_string(value, field_name=field_name)
    run_session = scope_key.split("::", 1)
    if len(run_session) != 2 or not run_session[0] or not run_session[1]:
        raise ValueError(f"{field_name} must be formatted as '<run_id>::<session_id>'")
    return scope_key


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_plugin_outcome_refs() -> dict[str, list[str]]:
    return {bucket: [] for bucket in _PLUGIN_OUTCOME_REF_BUCKETS}


@dataclass(frozen=True)
class DeliveredDecision:
    candidate_cid: str
    fate: str
    delivered_at_ms: int
    ack_status: str
    outcome_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_cid": self.candidate_cid,
            "fate": self.fate,
            "delivered_at_ms": self.delivered_at_ms,
            "ack_status": self.ack_status,
            "outcome_ref": self.outcome_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DeliveredDecision:
        if not isinstance(value, Mapping):
            raise ValueError("delivered decision must be a mapping")

        candidate_cid = _require_non_empty_string(
            value.get("candidate_cid"),
            field_name="candidate_cid",
        )
        fate = _require_non_empty_string(value.get("fate"), field_name="fate")
        if fate not in _VALID_FATES:
            raise ValueError(f"fate must be one of {sorted(_VALID_FATES)}")

        delivered_at_ms = _require_int(
            value.get("delivered_at_ms"),
            field_name="delivered_at_ms",
        )
        ack_status = _require_non_empty_string(value.get("ack_status"), field_name="ack_status")
        outcome_ref = _optional_non_empty_string(value.get("outcome_ref"), field_name="outcome_ref")

        return cls(
            candidate_cid=candidate_cid,
            fate=fate,
            delivered_at_ms=delivered_at_ms,
            ack_status=ack_status,
            outcome_ref=outcome_ref,
        )


@dataclass(frozen=True)
class ConsumedManifestRecord:
    scope_key: str
    manifest_digest: str
    coordinator_trace: str
    applied_at_ms: int
    delivered: tuple[DeliveredDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "manifest_digest": self.manifest_digest,
            "coordinator_trace": self.coordinator_trace,
            "applied_at_ms": self.applied_at_ms,
            "delivered": [decision.to_dict() for decision in self.delivered],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConsumedManifestRecord:
        if not isinstance(value, Mapping):
            raise ValueError("consumed manifest record must be a mapping")

        scope_key = _require_scope_key(value.get("scope_key"), field_name="scope_key")
        manifest_digest = _require_non_empty_string(
            value.get("manifest_digest"),
            field_name="manifest_digest",
        )
        coordinator_trace = _require_non_empty_string(
            value.get("coordinator_trace"),
            field_name="coordinator_trace",
        )
        applied_at_ms = _require_int(value.get("applied_at_ms"), field_name="applied_at_ms")

        raw_delivered = value.get("delivered", [])
        if not isinstance(raw_delivered, list):
            raise ValueError("delivered must be a list")

        parsed_delivered: list[DeliveredDecision] = []
        seen_cids: dict[str, str] = {}
        for index, raw_decision in enumerate(raw_delivered):
            if not isinstance(raw_decision, Mapping):
                raise ValueError(f"delivered[{index}] must be a mapping")
            decision = DeliveredDecision.from_dict(raw_decision)

            previous_fate = seen_cids.get(decision.candidate_cid)
            if previous_fate is not None and previous_fate != decision.fate:
                raise ValueError(
                    "delivered contains conflicting duplicate candidate fate: "
                    f"candidate_cid={decision.candidate_cid!r}, "
                    f"previous={previous_fate!r}, new={decision.fate!r}"
                )

            seen_cids[decision.candidate_cid] = decision.fate
            parsed_delivered.append(decision)

        return cls(
            scope_key=scope_key,
            manifest_digest=manifest_digest,
            coordinator_trace=coordinator_trace,
            applied_at_ms=applied_at_ms,
            delivered=tuple(parsed_delivered),
        )


@dataclass(frozen=True)
class WorkerLease:
    pid: int
    started_at_ms: int
    ttl_ms: int
    expires_at_ms: int

    def is_expired(self, now_ms: int) -> bool:
        current = _require_int(now_ms, field_name="now_ms")
        return current >= self.expires_at_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at_ms": self.started_at_ms,
            "ttl_ms": self.ttl_ms,
            "expires_at_ms": self.expires_at_ms,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkerLease:
        if not isinstance(value, Mapping):
            raise ValueError("worker lease must be a mapping")

        pid = _require_int(value.get("pid"), field_name="pid")
        if pid <= 0:
            raise ValueError("pid must be positive")

        started_at_ms = _require_int(value.get("started_at_ms"), field_name="started_at_ms")
        ttl_ms = _require_int(value.get("ttl_ms"), field_name="ttl_ms")
        if ttl_ms <= 0:
            raise ValueError("ttl_ms must be positive")

        expires_at_ms = _require_int(value.get("expires_at_ms"), field_name="expires_at_ms")
        expected_expires_at_ms = started_at_ms + ttl_ms
        if expires_at_ms != expected_expires_at_ms:
            raise ValueError(
                "expires_at_ms must equal started_at_ms + ttl_ms "
                f"({expires_at_ms} != {expected_expires_at_ms})"
            )

        return cls(
            pid=pid,
            started_at_ms=started_at_ms,
            ttl_ms=ttl_ms,
            expires_at_ms=expires_at_ms,
        )


@dataclass
class BridgeDaemonState:
    schema_version: int
    run_id: str | None
    session_id: str | None
    session_fp: str | None
    container_name: str | None
    lease: WorkerLease | None
    consumed_manifests: dict[str, ConsumedManifestRecord]
    heartbeat_last_ts_ms: int | None
    plugin_outcome_refs: dict[str, list[str]]
    resume_marker: str

    def to_dict(self) -> dict[str, Any]:
        consumed_payload: dict[str, Any] = {}
        for scope_key, record in self.consumed_manifests.items():
            normalized_scope_key = _require_scope_key(scope_key, field_name="consumed_manifests key")
            if isinstance(record, ConsumedManifestRecord):
                consumed_payload[normalized_scope_key] = record.to_dict()
            elif isinstance(record, Mapping):
                consumed_payload[normalized_scope_key] = dict(record)
            else:
                raise ValueError(
                    f"consumed_manifests[{normalized_scope_key!r}] "
                    "must be a ConsumedManifestRecord"
                )

        plugin_refs_payload: dict[str, list[Any]] = {}
        for bucket_name, refs in self.plugin_outcome_refs.items():
            normalized_bucket_name = _require_non_empty_string(
                bucket_name,
                field_name="plugin_outcome_refs bucket",
            )
            if not isinstance(refs, (list, tuple)):
                raise ValueError(
                    f"plugin_outcome_refs[{normalized_bucket_name!r}] must be a list"
                )
            plugin_refs_payload[normalized_bucket_name] = list(refs)

        return {
            "schema_version": int(self.schema_version),
            "run_id": self.run_id,
            "session_id": self.session_id,
            "session_fp": self.session_fp,
            "container_name": self.container_name,
            "lease": None if self.lease is None else self.lease.to_dict(),
            "consumed_manifests": consumed_payload,
            "heartbeat_last_ts_ms": self.heartbeat_last_ts_ms,
            "plugin_outcome_refs": plugin_refs_payload,
            "resume_marker": self.resume_marker,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BridgeDaemonState:
        if not isinstance(value, Mapping):
            raise ValueError("bridge daemon state must be a mapping")

        schema_version = _require_int(value.get("schema_version"), field_name="schema_version")
        if schema_version != BRIDGE_STATE_SCHEMA_VERSION:
            raise ValueError(
                "schema_version mismatch: "
                f"expected {BRIDGE_STATE_SCHEMA_VERSION}, got {schema_version}"
            )

        run_id = _optional_non_empty_string(value.get("run_id"), field_name="run_id")
        session_id = _optional_non_empty_string(value.get("session_id"), field_name="session_id")
        session_fp = _optional_non_empty_string(value.get("session_fp"), field_name="session_fp")
        container_name = _optional_non_empty_string(
            value.get("container_name"),
            field_name="container_name",
        )

        raw_lease = value.get("lease")
        lease: WorkerLease | None
        if raw_lease is None:
            lease = None
        else:
            if not isinstance(raw_lease, Mapping):
                raise ValueError("lease must be a mapping or null")
            lease = WorkerLease.from_dict(raw_lease)

        raw_consumed = value.get("consumed_manifests", {})
        if not isinstance(raw_consumed, Mapping):
            raise ValueError("consumed_manifests must be a mapping")

        consumed_manifests: dict[str, ConsumedManifestRecord] = {}
        for raw_key, raw_record in raw_consumed.items():
            if not isinstance(raw_key, str):
                raise ValueError("consumed_manifests keys must be strings")
            if not isinstance(raw_record, Mapping):
                raise ValueError(f"consumed_manifests[{raw_key!r}] must be a mapping")

            parsed_record = ConsumedManifestRecord.from_dict(raw_record)
            if parsed_record.scope_key != raw_key:
                raise ValueError(
                    "consumed_manifests key mismatch: "
                    f"key={raw_key!r} record.scope_key={parsed_record.scope_key!r}"
                )
            consumed_manifests[raw_key] = parsed_record

        raw_heartbeat = value.get("heartbeat_last_ts_ms")
        heartbeat_last_ts_ms: int | None
        if raw_heartbeat is None:
            heartbeat_last_ts_ms = None
        else:
            heartbeat_last_ts_ms = _require_int(raw_heartbeat, field_name="heartbeat_last_ts_ms")

        raw_plugin_refs = value.get("plugin_outcome_refs", {})
        if not isinstance(raw_plugin_refs, Mapping):
            raise ValueError("plugin_outcome_refs must be a mapping")

        for raw_bucket in raw_plugin_refs:
            if not isinstance(raw_bucket, str):
                raise ValueError("plugin_outcome_refs keys must be strings")
            if raw_bucket not in _PLUGIN_OUTCOME_REF_BUCKETS:
                raise ValueError(f"unsupported plugin_outcome_refs bucket: {raw_bucket!r}")

        plugin_outcome_refs = _default_plugin_outcome_refs()
        for bucket in _PLUGIN_OUTCOME_REF_BUCKETS:
            raw_refs = raw_plugin_refs.get(bucket, [])
            if not isinstance(raw_refs, list):
                raise ValueError(f"plugin_outcome_refs[{bucket!r}] must be a list")
            plugin_outcome_refs[bucket] = [
                _require_non_empty_string(
                    raw_ref,
                    field_name=f"plugin_outcome_refs[{bucket}][{index}]",
                )
                for index, raw_ref in enumerate(raw_refs)
            ]

        resume_marker = _require_non_empty_string(
            value.get("resume_marker", "idle"),
            field_name="resume_marker",
        )

        return cls(
            schema_version=schema_version,
            run_id=run_id,
            session_id=session_id,
            session_fp=session_fp,
            container_name=container_name,
            lease=lease,
            consumed_manifests=consumed_manifests,
            heartbeat_last_ts_ms=heartbeat_last_ts_ms,
            plugin_outcome_refs=plugin_outcome_refs,
            resume_marker=resume_marker,
        )


def compute_manifest_digest(manifest_payload: Mapping[str, Any] | dict[str, Any]) -> str:
    if not isinstance(manifest_payload, Mapping):
        raise ValueError("manifest_payload must be a mapping")
    canonical_payload = json.dumps(manifest_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _assert_no_plaintext_keys(payload: Any) -> None:
    def walk(value: Any, *, location: str) -> None:
        if isinstance(value, Mapping):
            for key, nested_value in value.items():
                if isinstance(key, str) and key.strip().lower() in _FORBIDDEN_PLAINTEXT_KEYS:
                    raise ValueError(
                        "forbidden plaintext-like key found in bridge state payload: "
                        f"{location}.{key}"
                    )
                walk(nested_value, location=f"{location}.{key}")
            return

        if isinstance(value, (list, tuple)):
            for index, nested_value in enumerate(value):
                walk(nested_value, location=f"{location}[{index}]")

    walk(payload, location="state")


def atomic_write_state(path: Path, state: BridgeDaemonState) -> None:
    if not isinstance(path, Path):
        raise ValueError("path must be a pathlib.Path")
    if not isinstance(state, BridgeDaemonState):
        raise ValueError("state must be a BridgeDaemonState")

    state_path = path
    state_path.parent.mkdir(parents=True, exist_ok=True)

    payload = state.to_dict()
    _assert_no_plaintext_keys(payload)

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{state_path.name}.tmp-",
        dir=state_path.parent,
        text=True,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, state_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def load_state(path: Path) -> BridgeDaemonState | None:
    if not isinstance(path, Path):
        raise ValueError("path must be a pathlib.Path")

    if not path.exists():
        return None

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, Mapping):
        raise ValueError(f"state at {path} must decode to a JSON object")

    raw_schema_version = _require_int(payload.get("schema_version"), field_name="schema_version")
    if raw_schema_version != BRIDGE_STATE_SCHEMA_VERSION:
        raise ValueError(
            "cannot resume bridge daemon state: schema_version mismatch "
            f"({raw_schema_version} vs {BRIDGE_STATE_SCHEMA_VERSION}); "
            "start fresh by deleting this state file"
        )

    return BridgeDaemonState.from_dict(payload)


def resume_or_create_state(
    path: Path,
    *,
    run_id: str,
    session_id: str,
    session_fp: str,
    container_name: str,
) -> BridgeDaemonState:
    if not isinstance(path, Path):
        raise ValueError("path must be a pathlib.Path")

    normalized_run_id = _require_non_empty_string(run_id, field_name="run_id")
    normalized_session_id = _require_non_empty_string(session_id, field_name="session_id")
    normalized_session_fp = _require_non_empty_string(session_fp, field_name="session_fp")
    normalized_container_name = _require_non_empty_string(
        container_name,
        field_name="container_name",
    )

    requested_scope_key = _scope_key(normalized_run_id, normalized_session_id)
    existing = load_state(path)

    if existing is None:
        created = BridgeDaemonState(
            schema_version=BRIDGE_STATE_SCHEMA_VERSION,
            run_id=normalized_run_id,
            session_id=normalized_session_id,
            session_fp=normalized_session_fp,
            container_name=normalized_container_name,
            lease=None,
            consumed_manifests={},
            heartbeat_last_ts_ms=None,
            plugin_outcome_refs=_default_plugin_outcome_refs(),
            resume_marker="idle",
        )
        atomic_write_state(path, created)
        return created

    now_ms = _now_ms()
    lease_is_expired = existing.lease is not None and existing.lease.is_expired(now_ms)
    lease_is_active = existing.lease is not None and not lease_is_expired

    existing_scope_key: str | None = None
    if existing.run_id is not None and existing.session_id is not None:
        existing_scope_key = _scope_key(existing.run_id, existing.session_id)

    if lease_is_active and existing_scope_key is None:
        raise ValueError(
            "cannot resume bridge daemon state: one active worker scope exists "
            "but scope identifiers are missing; start fresh"
        )

    if lease_is_active and existing_scope_key != requested_scope_key:
        raise ValueError(
            "cannot resume bridge daemon state: one active worker scope is already running "
            f"(active={existing_scope_key!r}, requested={requested_scope_key!r})"
        )

    if existing_scope_key == requested_scope_key or lease_is_expired:
        existing.run_id = normalized_run_id
        existing.session_id = normalized_session_id
        existing.session_fp = normalized_session_fp
        existing.container_name = normalized_container_name
        if not isinstance(existing.resume_marker, str) or not existing.resume_marker.strip():
            existing.resume_marker = "idle"
        atomic_write_state(path, existing)
        return existing

    created = BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id=normalized_run_id,
        session_id=normalized_session_id,
        session_fp=normalized_session_fp,
        container_name=normalized_container_name,
        lease=None,
        consumed_manifests={},
        heartbeat_last_ts_ms=None,
        plugin_outcome_refs=_default_plugin_outcome_refs(),
        resume_marker="idle",
    )
    atomic_write_state(path, created)
    return created


def already_delivered_cids(state: BridgeDaemonState, scope_key: str) -> set[str]:
    if not isinstance(state, BridgeDaemonState):
        raise ValueError("state must be a BridgeDaemonState")
    normalized_scope_key = _require_scope_key(scope_key, field_name="scope_key")

    record = state.consumed_manifests.get(normalized_scope_key)
    if record is None:
        return set()
    return {decision.candidate_cid for decision in record.delivered}


def record_delivery(
    state: BridgeDaemonState,
    scope_key: str,
    digest: str,
    coordinator_trace: str,
    delivered: list[DeliveredDecision],
    applied_at_ms: int,
) -> None:
    if not isinstance(state, BridgeDaemonState):
        raise ValueError("state must be a BridgeDaemonState")

    normalized_scope_key = _require_scope_key(scope_key, field_name="scope_key")
    normalized_digest = _require_non_empty_string(digest, field_name="digest")
    normalized_trace = _require_non_empty_string(coordinator_trace, field_name="coordinator_trace")
    normalized_applied_at_ms = _require_int(applied_at_ms, field_name="applied_at_ms")

    if not isinstance(delivered, list):
        raise ValueError("delivered must be a list")

    normalized_delivered: list[DeliveredDecision] = []
    incoming_fates: dict[str, str] = {}
    for index, decision in enumerate(delivered):
        if not isinstance(decision, DeliveredDecision):
            raise ValueError(f"delivered[{index}] must be a DeliveredDecision")

        previous = incoming_fates.get(decision.candidate_cid)
        if previous is not None and previous != decision.fate:
            raise ValueError(
                "delivered contains conflicting duplicate candidate fate: "
                f"candidate_cid={decision.candidate_cid!r}, previous={previous!r}, "
                f"new={decision.fate!r}"
            )
        incoming_fates[decision.candidate_cid] = decision.fate
        normalized_delivered.append(decision)

    existing = state.consumed_manifests.get(normalized_scope_key)
    if existing is None:
        state.consumed_manifests[normalized_scope_key] = ConsumedManifestRecord(
            scope_key=normalized_scope_key,
            manifest_digest=normalized_digest,
            coordinator_trace=normalized_trace,
            applied_at_ms=normalized_applied_at_ms,
            delivered=tuple(normalized_delivered),
        )
        return

    if existing.manifest_digest == normalized_digest:
        return

    existing_by_cid = {decision.candidate_cid: decision for decision in existing.delivered}
    for candidate_cid, next_fate in incoming_fates.items():
        previous_decision = existing_by_cid.get(candidate_cid)
        if previous_decision is None:
            continue
        if previous_decision.fate != next_fate:
            raise ConsumerConflictError(
                "conflicting replay rejected: "
                f"candidate_cid={candidate_cid!r}, "
                f"previous={previous_decision.fate!r}, new={next_fate!r}"
            )

    merged_delivered = list(existing.delivered)
    seen_cids = {decision.candidate_cid for decision in merged_delivered}
    for decision in normalized_delivered:
        if decision.candidate_cid in seen_cids:
            continue
        merged_delivered.append(decision)
        seen_cids.add(decision.candidate_cid)

    state.consumed_manifests[normalized_scope_key] = ConsumedManifestRecord(
        scope_key=normalized_scope_key,
        manifest_digest=normalized_digest,
        coordinator_trace=normalized_trace,
        applied_at_ms=normalized_applied_at_ms,
        delivered=tuple(merged_delivered),
    )


def mark_acked(
    state: BridgeDaemonState,
    scope_key: str,
    cid: str,
    ack_status: str,
    outcome_ref: str | None = None,
) -> None:
    if not isinstance(state, BridgeDaemonState):
        raise ValueError("state must be a BridgeDaemonState")

    normalized_scope_key = _require_scope_key(scope_key, field_name="scope_key")
    normalized_cid = _require_non_empty_string(cid, field_name="cid")
    normalized_ack_status = _require_non_empty_string(ack_status, field_name="ack_status")
    normalized_outcome_ref = _optional_non_empty_string(outcome_ref, field_name="outcome_ref")

    existing = state.consumed_manifests.get(normalized_scope_key)
    if existing is None:
        raise ValueError(f"scope_key not found: {normalized_scope_key!r}")

    updated_delivered: list[DeliveredDecision] = []
    matched = False
    for decision in existing.delivered:
        if decision.candidate_cid != normalized_cid:
            updated_delivered.append(decision)
            continue

        matched = True
        updated_delivered.append(
            DeliveredDecision(
                candidate_cid=decision.candidate_cid,
                fate=decision.fate,
                delivered_at_ms=decision.delivered_at_ms,
                ack_status=normalized_ack_status,
                outcome_ref=(
                    normalized_outcome_ref
                    if normalized_outcome_ref is not None
                    else decision.outcome_ref
                ),
            )
        )

    if not matched:
        raise ValueError(
            f"cid {normalized_cid!r} not found in delivered decisions for scope {normalized_scope_key!r}"
        )

    state.consumed_manifests[normalized_scope_key] = ConsumedManifestRecord(
        scope_key=existing.scope_key,
        manifest_digest=existing.manifest_digest,
        coordinator_trace=existing.coordinator_trace,
        applied_at_ms=existing.applied_at_ms,
        delivered=tuple(updated_delivered),
    )


__all__ = [
    "BRIDGE_STATE_SCHEMA_VERSION",
    "ConsumerConflictError",
    "DeliveredDecision",
    "ConsumedManifestRecord",
    "WorkerLease",
    "BridgeDaemonState",
    "compute_manifest_digest",
    "atomic_write_state",
    "load_state",
    "resume_or_create_state",
    "record_delivery",
    "mark_acked",
    "already_delivered_cids",
]
