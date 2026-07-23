import json
from pathlib import Path
import stat
import time

import pytest

from wevibe_bench.cumulative.bridge_state import (
    BRIDGE_STATE_SCHEMA_VERSION,
    BridgeDaemonState,
    ConsumedManifestRecord,
    ConsumerConflictError,
    DeliveredDecision,
    WorkerLease,
    already_delivered_cids,
    atomic_write_state,
    compute_manifest_digest,
    load_state,
    mark_acked,
    record_delivery,
    resume_or_create_state,
)


def _sample_delivered(*, cid: str, fate: str, delivered_at_ms: int) -> DeliveredDecision:
    return DeliveredDecision(
        candidate_cid=cid,
        fate=fate,
        delivered_at_ms=delivered_at_ms,
        ack_status="pending",
        outcome_ref=None,
    )


def _sample_state(*, now_ms: int) -> BridgeDaemonState:
    scope = "run-alpha::session-alpha"
    record = ConsumedManifestRecord(
        scope_key=scope,
        manifest_digest="digest-alpha",
        coordinator_trace="trace-alpha",
        applied_at_ms=now_ms,
        delivered=(
            _sample_delivered(cid="cid-accept", fate="accept", delivered_at_ms=now_ms),
            _sample_delivered(cid="cid-deny", fate="deny", delivered_at_ms=now_ms + 1),
        ),
    )
    return BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id="run-alpha",
        session_id="session-alpha",
        session_fp="session-fingerprint-alpha",
        container_name="bridge-worker-alpha",
        lease=WorkerLease(
            pid=4242,
            started_at_ms=now_ms,
            ttl_ms=60_000,
            expires_at_ms=now_ms + 60_000,
        ),
        consumed_manifests={scope: record},
        heartbeat_last_ts_ms=now_ms,
        plugin_outcome_refs={
            "served_confirmed": ["cid-accept"],
            "denied": ["cid-deny"],
            "reported": [],
            "blocked": [],
            "missing_accepted": [],
            "nonaccept_leaked": [],
            "side_effect_timeouts": [],
        },
        resume_marker="active",
    )


def _walk_forbidden_keys(payload: object, forbidden: set[str]) -> set[str]:
    found: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(key, str) and key.lower() in forbidden:
                    found.add(key.lower())
                walk(nested)
            return

        if isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    return found


def test_bridge_state_dataclass_roundtrip_and_atomic_load_roundtrip(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    decision = _sample_delivered(cid="cid-a", fate="accept", delivered_at_ms=now_ms)
    lease = WorkerLease(
        pid=101,
        started_at_ms=now_ms,
        ttl_ms=30_000,
        expires_at_ms=now_ms + 30_000,
    )
    record = ConsumedManifestRecord(
        scope_key="run-a::session-a",
        manifest_digest="digest-a",
        coordinator_trace="trace-a",
        applied_at_ms=now_ms,
        delivered=(decision,),
    )

    state = BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id="run-a",
        session_id="session-a",
        session_fp="fp-a",
        container_name="container-a",
        lease=lease,
        consumed_manifests={record.scope_key: record},
        heartbeat_last_ts_ms=now_ms,
        plugin_outcome_refs={
            "served_confirmed": ["cid-a"],
            "denied": [],
            "reported": [],
            "blocked": [],
            "missing_accepted": [],
            "nonaccept_leaked": [],
            "side_effect_timeouts": [],
        },
        resume_marker="idle",
    )

    assert DeliveredDecision.from_dict(decision.to_dict()) == decision
    assert ConsumedManifestRecord.from_dict(record.to_dict()) == record
    assert WorkerLease.from_dict(lease.to_dict()) == lease
    assert BridgeDaemonState.from_dict(state.to_dict()) == state

    state_path = tmp_path / "bridge-state.json"
    atomic_write_state(state_path, state)

    loaded = load_state(state_path)
    assert loaded == state


def test_load_state_schema_drift_requires_start_fresh(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    state = _sample_state(now_ms=now_ms)
    state_path = tmp_path / "bridge-state.json"

    payload = state.to_dict()
    payload["schema_version"] = BRIDGE_STATE_SCHEMA_VERSION + 1
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="start fresh"):
        load_state(state_path)


def test_atomic_write_is_all_or_nothing_mode_600_and_no_tmp_leftovers(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    state = _sample_state(now_ms=now_ms)
    state_path = tmp_path / "state" / "bridge-state.json"

    atomic_write_state(state_path, state)

    assert state_path.exists()
    assert list(state_path.parent.glob(f".{state_path.name}.tmp-*")) == []
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_resume_or_create_state_enforces_one_active_scope_and_resumes(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    state_path = tmp_path / "bridge-state.json"

    state = _sample_state(now_ms=now_ms)
    atomic_write_state(state_path, state)

    with pytest.raises(ValueError, match="one active worker scope"):
        resume_or_create_state(
            state_path,
            run_id="run-other",
            session_id="session-other",
            session_fp="fp-other",
            container_name="container-other",
        )

    resumed_same_scope = resume_or_create_state(
        state_path,
        run_id="run-alpha",
        session_id="session-alpha",
        session_fp="session-fingerprint-alpha",
        container_name="bridge-worker-alpha",
    )
    assert resumed_same_scope.consumed_manifests == state.consumed_manifests

    expired_state = _sample_state(now_ms=now_ms - 120_000)
    expired_state.lease = WorkerLease(
        pid=4242,
        started_at_ms=now_ms - 120_000,
        ttl_ms=1_000,
        expires_at_ms=now_ms - 119_000,
    )
    atomic_write_state(state_path, expired_state)

    resumed_after_expiry = resume_or_create_state(
        state_path,
        run_id="run-new",
        session_id="session-new",
        session_fp="fp-new",
        container_name="container-new",
    )
    assert resumed_after_expiry.run_id == "run-new"
    assert resumed_after_expiry.session_id == "session-new"
    assert resumed_after_expiry.consumed_manifests == expired_state.consumed_manifests


def test_record_delivery_same_digest_is_idempotent_and_cids_stable() -> None:
    now_ms = int(time.time() * 1000)
    state = BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id="run-1",
        session_id="session-1",
        session_fp="fp-1",
        container_name="container-1",
        lease=None,
        consumed_manifests={},
        heartbeat_last_ts_ms=None,
        plugin_outcome_refs={
            "served_confirmed": [],
            "denied": [],
            "reported": [],
            "blocked": [],
            "missing_accepted": [],
            "nonaccept_leaked": [],
            "side_effect_timeouts": [],
        },
        resume_marker="idle",
    )

    scope_key = "run-1::session-1"
    digest = compute_manifest_digest({"run_id": "run-1", "session_id": "session-1"})
    delivered = [
        _sample_delivered(cid="cid-1", fate="accept", delivered_at_ms=now_ms),
        _sample_delivered(cid="cid-2", fate="deny", delivered_at_ms=now_ms + 1),
    ]

    record_delivery(
        state,
        scope_key,
        digest,
        "trace-1",
        delivered,
        now_ms,
    )
    record_delivery(
        state,
        scope_key,
        digest,
        "trace-1",
        delivered,
        now_ms + 5,
    )

    assert len(state.consumed_manifests[scope_key].delivered) == 2
    assert already_delivered_cids(state, scope_key) == {"cid-1", "cid-2"}

    mark_acked(state, scope_key, "cid-1", "drained", outcome_ref="outcome-ref-1")
    record = state.consumed_manifests[scope_key]
    acked = {decision.candidate_cid: decision for decision in record.delivered}["cid-1"]
    assert acked.ack_status == "drained"
    assert acked.outcome_ref == "outcome-ref-1"


def test_conflicting_replay_rejected_and_additive_replay_accepted() -> None:
    now_ms = int(time.time() * 1000)
    state = BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id="run-2",
        session_id="session-2",
        session_fp="fp-2",
        container_name="container-2",
        lease=None,
        consumed_manifests={},
        heartbeat_last_ts_ms=None,
        plugin_outcome_refs={
            "served_confirmed": [],
            "denied": [],
            "reported": [],
            "blocked": [],
            "missing_accepted": [],
            "nonaccept_leaked": [],
            "side_effect_timeouts": [],
        },
        resume_marker="idle",
    )

    scope_key = "run-2::session-2"
    digest_1 = compute_manifest_digest({"rev": 1})
    digest_2 = compute_manifest_digest({"rev": 2})

    record_delivery(
        state,
        scope_key,
        digest_1,
        "trace-2",
        [_sample_delivered(cid="cid-1", fate="accept", delivered_at_ms=now_ms)],
        now_ms,
    )

    with pytest.raises(ConsumerConflictError, match="conflicting replay rejected"):
        record_delivery(
            state,
            scope_key,
            digest_2,
            "trace-2b",
            [_sample_delivered(cid="cid-1", fate="deny", delivered_at_ms=now_ms + 1)],
            now_ms + 1,
        )

    record_delivery(
        state,
        scope_key,
        digest_2,
        "trace-2c",
        [
            _sample_delivered(cid="cid-1", fate="accept", delivered_at_ms=now_ms + 2),
            _sample_delivered(cid="cid-2", fate="block", delivered_at_ms=now_ms + 3),
        ],
        now_ms + 2,
    )

    assert already_delivered_cids(state, scope_key) == {"cid-1", "cid-2"}
    assert state.consumed_manifests[scope_key].manifest_digest == digest_2


def test_restart_resume_same_manifest_is_noop_without_duplicate(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    state_path = tmp_path / "bridge-state.json"

    state = BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id="run-3",
        session_id="session-3",
        session_fp="fp-3",
        container_name="container-3",
        lease=None,
        consumed_manifests={},
        heartbeat_last_ts_ms=None,
        plugin_outcome_refs={
            "served_confirmed": [],
            "denied": [],
            "reported": [],
            "blocked": [],
            "missing_accepted": [],
            "nonaccept_leaked": [],
            "side_effect_timeouts": [],
        },
        resume_marker="idle",
    )

    scope_key = "run-3::session-3"
    digest = compute_manifest_digest({"rev": 1, "scope": scope_key})
    first_delivered = [_sample_delivered(cid="cid-r", fate="report", delivered_at_ms=now_ms)]

    record_delivery(state, scope_key, digest, "trace-3", first_delivered, now_ms)
    atomic_write_state(state_path, state)

    resumed = load_state(state_path)
    assert resumed is not None

    record_delivery(resumed, scope_key, digest, "trace-3", first_delivered, now_ms + 1)
    assert len(resumed.consumed_manifests[scope_key].delivered) == 1


def test_atomic_write_rejects_plaintext_keys_and_normal_state_is_safe(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    state_path = tmp_path / "bridge-state.json"

    with_forbidden_bucket = _sample_state(now_ms=now_ms)
    with_forbidden_bucket.plugin_outcome_refs["text"] = ["raw-text"]
    with pytest.raises(ValueError, match="forbidden plaintext-like key"):
        atomic_write_state(state_path, with_forbidden_bucket)

    with_forbidden_nested = _sample_state(now_ms=now_ms)
    with_forbidden_nested.plugin_outcome_refs["served_confirmed"] = [
        {"memory_text": "never-store-this"},
    ]
    with pytest.raises(ValueError, match="forbidden plaintext-like key"):
        atomic_write_state(state_path, with_forbidden_nested)

    safe_state = _sample_state(now_ms=now_ms)
    atomic_write_state(state_path, safe_state)

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    forbidden_found = _walk_forbidden_keys(
        payload,
        forbidden={"text", "transcript", "plaintext", "memory_text"},
    )
    assert forbidden_found == set()


def test_compute_manifest_digest_is_deterministic_and_order_independent() -> None:
    payload_a = {
        "run_id": "run-digest",
        "session_id": "session-digest",
        "decisions": [
            {"candidate_cid": "cid-1", "fate": "accept"},
            {"candidate_cid": "cid-2", "fate": "deny"},
        ],
        "meta": {"z": 99, "a": "x"},
    }
    payload_b = {
        "meta": {"a": "x", "z": 99},
        "decisions": [
            {"fate": "accept", "candidate_cid": "cid-1"},
            {"fate": "deny", "candidate_cid": "cid-2"},
        ],
        "session_id": "session-digest",
        "run_id": "run-digest",
    }

    digest_a = compute_manifest_digest(payload_a)
    digest_b = compute_manifest_digest(payload_b)

    assert digest_a == digest_b
    assert digest_a == compute_manifest_digest(payload_a)
    assert digest_a != compute_manifest_digest({**payload_a, "run_id": "run-digest-2"})
