import io
import json
import logging
from pathlib import Path

import pytest

from wevibe_bench.cumulative.bridge_state import (
    DeliveredDecision,
    WorkerLease,
    load_state,
    record_delivery,
)
from wevibe_bench.cumulative.consumer_bridge import (
    DEFAULT_LEASE_TTL_MS,
    ConsumerBridge,
    manifest_inbox_name,
)
from wevibe_bench.cumulative.consumer_decision import (
    CONSUMER_DECISION_SCHEMA_VERSION,
    ConsumerCandidateDecision,
    ConsumerDecisionManifest,
)
from wevibe_bench.cumulative.consumer_gate import (
    DECISIONS_FILENAME,
    HEARTBEAT_FILENAME,
    QUEUE_FILENAME,
    ConsumerGateCoordinator,
)
from wevibe_bench.cumulative.types import SessionRecord


class FakeClock:
    def __init__(self, start: float = 2_000_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _queue_entry(cid: str, text: str = "memory") -> dict[str, str]:
    return {"id": cid, "cid": cid, "text": text, "source": "recall"}


def _manifest(
    *,
    run_id: str,
    default_fate: str,
    coordinator_trace: str,
    decisions: tuple[ConsumerCandidateDecision, ...],
) -> ConsumerDecisionManifest:
    return ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=run_id,
        policy_id="coordinator-policy-v1",
        default_fate=default_fate,
        decisions=decisions,
        coordinator_trace=coordinator_trace,
    )


def _write_manifest(inbox: Path, *, run_id: str, session_fp: str, manifest: ConsumerDecisionManifest) -> None:
    _write_json(inbox / manifest_inbox_name(run_id, session_fp), manifest.to_dict())


def _build_bridge(
    tmp_path: Path,
    *,
    run_id: str,
    session_id: str,
    queue_payload: list[dict[str, str]],
    clock: FakeClock,
    logger: logging.Logger | None = None,
    lease_ttl_ms: int = 120_000,
    state_path: Path | None = None,
) -> tuple[ConsumerBridge, Path, Path, Path, str]:
    state_dir = tmp_path / "state"
    inbox = tmp_path / "inbox"
    served_store_path = tmp_path / "served-store.json"
    state_file = state_path if state_path is not None else (tmp_path / "bridge-state.json")

    _write_json(state_dir / QUEUE_FILENAME, queue_payload)

    coordinator = ConsumerGateCoordinator(state_dir=state_dir, clock=clock)
    session_fp = SessionRecord.session_fp_of(session_id)
    bridge = ConsumerBridge(
        coordinator=coordinator,
        state_path=state_file,
        manifest_inbox=inbox,
        served_store_path=served_store_path,
        run_id=run_id,
        session_id=session_id,
        session_fp=session_fp,
        clock=clock,
        logger=logger,
        lease_ttl_ms=lease_ttl_ms,
        heartbeat_cadence_ms=5_000,
        poll_interval_ms=10,
    )
    return bridge, state_dir, inbox, served_store_path, session_fp


def test_manifest_arrival_delivered_once_then_idempotent(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-manifest-once"
    session_id = "session-manifest-once"
    bridge, state_dir, inbox, _, session_fp = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[_queue_entry("cid-a"), _queue_entry("cid-b")],
        clock=clock,
    )

    manifest = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-once",
        decisions=(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-b",
                fate="deny",
                coordinator_trace="trace-once",
                reason="coordinator denied",
            ),
        ),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest)

    first = bridge.poll_once()
    assert first["decision_emitted"] is True
    assert first["reason"] == "delivered"

    second = bridge.poll_once()
    assert second["decision_emitted"] is False
    assert second["reason"] == "already_delivered"

    state = load_state(bridge.state_path)
    assert state is not None
    record = state.consumed_manifests[f"{run_id}::{session_id}"]
    assert len(record.delivered) == 2

    decisions = _read_json(state_dir / DECISIONS_FILENAME)
    assert isinstance(decisions, list)
    assert len(decisions) == 2


def test_manifest_reapplied_when_recalled_queue_grows(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-manifest-growth"
    session_id = "session-manifest-growth"
    bridge, state_dir, inbox, _, session_fp = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[_queue_entry("cid-a"), _queue_entry("cid-b")],
        clock=clock,
    )

    manifest = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-growth",
        decisions=(),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest)

    first = bridge.poll_once()
    assert first["decision_emitted"] is True
    assert first["reason"] == "delivered"

    first_decisions = _read_json(state_dir / DECISIONS_FILENAME)
    assert isinstance(first_decisions, list)
    assert {(entry["memoryID"], entry["action"]) for entry in first_decisions} == {
        ("cid-a", "accept"),
        ("cid-b", "accept"),
    }

    _write_json(
        state_dir / QUEUE_FILENAME,
        [_queue_entry("cid-a"), _queue_entry("cid-b"), _queue_entry("cid-c")],
    )

    second = bridge.poll_once()
    assert second["decision_emitted"] is True
    assert second["reason"] == "delivered"

    second_decisions = _read_json(state_dir / DECISIONS_FILENAME)
    assert isinstance(second_decisions, list)
    assert {(entry["memoryID"], entry["action"]) for entry in second_decisions} == {
        ("cid-a", "accept"),
        ("cid-b", "accept"),
        ("cid-c", "accept"),
    }

    third = bridge.poll_once()
    assert third["decision_emitted"] is False
    assert third["reason"] == "already_delivered"


def test_record_delivery_same_digest_merges_new_cids(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-record-merge"
    session_id = "session-record-merge"
    bridge, _, _, _, _ = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[_queue_entry("cid-a")],
        clock=clock,
    )
    scope_key = f"{run_id}::{session_id}"
    digest = "digest-same"

    record_delivery(
        bridge.state,
        scope_key,
        digest,
        "trace-1",
        [
            DeliveredDecision(
                candidate_cid="cid-a",
                fate="accept",
                delivered_at_ms=1,
                ack_status="pending",
                outcome_ref=None,
            )
        ],
        1,
    )

    record_delivery(
        bridge.state,
        scope_key,
        digest,
        "trace-2",
        [
            DeliveredDecision(
                candidate_cid="cid-a",
                fate="accept",
                delivered_at_ms=2,
                ack_status="pending",
                outcome_ref=None,
            ),
            DeliveredDecision(
                candidate_cid="cid-b",
                fate="deny",
                delivered_at_ms=2,
                ack_status="pending",
                outcome_ref=None,
            ),
        ],
        2,
    )

    record = bridge.state.consumed_manifests[scope_key]
    assert record.applied_at_ms == 2
    assert {decision.candidate_cid: decision.fate for decision in record.delivered} == {
        "cid-a": "accept",
        "cid-b": "deny",
    }
    assert record.coordinator_trace == "trace-2"

    record_delivery(
        bridge.state,
        scope_key,
        digest,
        "trace-3",
        [
            DeliveredDecision(
                candidate_cid="cid-a",
                fate="accept",
                delivered_at_ms=3,
                ack_status="pending",
                outcome_ref=None,
            ),
            DeliveredDecision(
                candidate_cid="cid-b",
                fate="deny",
                delivered_at_ms=3,
                ack_status="pending",
                outcome_ref=None,
            ),
        ],
        3,
    )

    unchanged = bridge.state.consumed_manifests[scope_key]
    assert unchanged.applied_at_ms == 3
    assert {decision.candidate_cid: decision.fate for decision in unchanged.delivered} == {
        "cid-a": "accept",
        "cid-b": "deny",
    }
    assert unchanged.coordinator_trace == "trace-2"


def test_no_manifest_emits_no_decision(tmp_path: Path) -> None:
    clock = FakeClock()
    bridge, state_dir, _, _, _ = _build_bridge(
        tmp_path,
        run_id="run-no-manifest",
        session_id="session-no-manifest",
        queue_payload=[_queue_entry("cid-1")],
        clock=clock,
    )

    status = bridge.poll_once()
    assert status["decision_emitted"] is False
    assert status["reason"] == "no_manifest"
    assert status["recalled"] == 1
    assert not (state_dir / DECISIONS_FILENAME).exists()


def test_invalid_correlation_rejected_without_decision_write(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-correlation"
    session_id = "session-correlation"
    bridge, state_dir, inbox, _, session_fp = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[_queue_entry("cid-real")],
        clock=clock,
    )

    manifest = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-correlation",
        decisions=(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-missing",
                fate="deny",
                coordinator_trace="trace-correlation",
                reason="not recalled",
            ),
        ),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest)

    status = bridge.poll_once()
    assert status["decision_emitted"] is False
    assert status["reason"] == "correlation_rejected"
    assert not (state_dir / DECISIONS_FILENAME).exists()


def test_replay_idempotent_then_fate_flip_rejected(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-replay"
    session_id = "session-replay"
    bridge, state_dir, inbox, _, session_fp = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[_queue_entry("cid-1")],
        clock=clock,
    )

    manifest_accept = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-replay",
        decisions=(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-1",
                fate="accept",
                coordinator_trace="trace-replay",
                reason="",
            ),
        ),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest_accept)

    assert bridge.poll_once()["reason"] == "delivered"
    assert bridge.poll_once()["reason"] == "already_delivered"

    manifest_flip = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-replay",
        decisions=(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-1",
                fate="block",
                coordinator_trace="trace-replay",
                reason="flipped",
            ),
        ),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest_flip)

    conflict = bridge.poll_once()
    assert conflict["decision_emitted"] is False
    assert conflict["reason"] == "replay_conflict"

    decisions = _read_json(state_dir / DECISIONS_FILENAME)
    assert isinstance(decisions, list)
    assert decisions[0]["action"] == "accept"


def test_refresh_heartbeat_renews_lease_and_preserves_state_invariant(tmp_path: Path) -> None:
    clock = FakeClock()
    bridge, state_dir, _, _, _ = _build_bridge(
        tmp_path,
        run_id="run-heartbeat",
        session_id="session-heartbeat",
        queue_payload=[_queue_entry("cid-hb")],
        clock=clock,
        lease_ttl_ms=2_000,
    )

    initial_lease = bridge.state.lease
    assert initial_lease is not None

    clock.advance(1.0)
    bridge.refresh_heartbeat()
    heartbeat_ts = _read_json(state_dir / HEARTBEAT_FILENAME)["ts"]
    assert heartbeat_ts == int(clock() * 1000)

    renewed_lease = bridge.state.lease
    assert renewed_lease is not None
    assert renewed_lease.expires_at_ms > initial_lease.expires_at_ms
    assert renewed_lease.started_at_ms == int(clock() * 1000)
    assert renewed_lease.expires_at_ms == renewed_lease.started_at_ms + renewed_lease.ttl_ms
    assert WorkerLease.from_dict(renewed_lease.to_dict()) == renewed_lease

    state = load_state(bridge.state_path)
    assert state is not None
    assert state.lease == renewed_lease


def test_poll_once_heartbeat_keeps_lease_alive_past_2x_default_ttl(tmp_path: Path) -> None:
    clock = FakeClock()
    bridge, _, _, _, _ = _build_bridge(
        tmp_path,
        run_id="run-watchdog",
        session_id="session-watchdog",
        queue_payload=[_queue_entry("cid-watchdog")],
        clock=clock,
        lease_ttl_ms=DEFAULT_LEASE_TTL_MS,
    )

    elapsed_ms = 0
    while elapsed_ms <= 240_000:
        clock.advance(30.0)
        elapsed_ms += 30_000
        status = bridge.poll_once()
        assert status["reason"] != "lease_expired"
        assert bridge.state.resume_marker == "active"

    persisted = load_state(bridge.state_path)
    assert persisted is not None
    assert persisted.resume_marker == "active"


def test_poll_once_still_expires_if_no_heartbeat_refresh_past_ttl(tmp_path: Path) -> None:
    clock = FakeClock()
    bridge, _, _, _, _ = _build_bridge(
        tmp_path,
        run_id="run-backstop",
        session_id="session-backstop",
        queue_payload=[_queue_entry("cid-backstop")],
        clock=clock,
        lease_ttl_ms=2_000,
    )

    clock.advance(2.001)
    status = bridge.poll_once()
    assert status["reason"] == "lease_expired"


def test_all_four_fates_written_with_exact_stored_shape(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-all-fates"
    session_id = "session-all-fates"
    bridge, state_dir, inbox, _, session_fp = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[
            _queue_entry("cid-accept"),
            _queue_entry("cid-deny"),
            _queue_entry("cid-block"),
            _queue_entry("cid-report"),
        ],
        clock=clock,
    )

    manifest = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-fates",
        decisions=(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-deny",
                fate="deny",
                coordinator_trace="trace-fates",
                reason="deny",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-block",
                fate="block",
                coordinator_trace="trace-fates",
                reason="block",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-report",
                fate="report",
                coordinator_trace="trace-fates",
                reason="report",
            ),
        ),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest)

    status = bridge.poll_once()
    assert status["decision_emitted"] is True
    assert status["fate_counts"] == {"accept": 1, "deny": 1, "block": 1, "report": 1}

    decisions = _read_json(state_dir / DECISIONS_FILENAME)
    assert isinstance(decisions, list)
    assert len(decisions) == 4
    expected_keys = {"memoryID", "action", "reason", "note", "timestamp"}
    assert all(set(entry.keys()) == expected_keys for entry in decisions)
    assert {entry["memoryID"]: entry["action"] for entry in decisions} == {
        "cid-accept": "accept",
        "cid-deny": "deny",
        "cid-block": "block",
        "cid-report": "report",
    }


def test_daemon_adds_no_extra_fates_vs_direct_coordinator_apply(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-default-check"
    session_id = "session-default-check"
    queue_payload = [_queue_entry("cid-1"), _queue_entry("cid-2"), _queue_entry("cid-3")]
    manifest = _manifest(
        run_id=run_id,
        default_fate="deny",
        coordinator_trace="trace-default",
        decisions=(),
    )

    direct_state_dir = tmp_path / "direct-state"
    _write_json(direct_state_dir / QUEUE_FILENAME, queue_payload)
    direct_coordinator = ConsumerGateCoordinator(state_dir=direct_state_dir, clock=clock)
    direct_outcome = direct_coordinator.apply_manifest(manifest, run_id=run_id, session_id=session_id)

    bridge_root = tmp_path / "bridge"
    bridge, state_dir, inbox, _, session_fp = _build_bridge(
        bridge_root,
        run_id=run_id,
        session_id=session_id,
        queue_payload=queue_payload,
        clock=clock,
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest)

    status = bridge.poll_once()
    bridge_decisions = _read_json(state_dir / DECISIONS_FILENAME)
    bridge_pairs = {(entry["memoryID"], entry["action"]) for entry in bridge_decisions}
    direct_pairs = set(direct_outcome.decisions)

    assert status["decision_emitted"] is True
    assert bridge_pairs == direct_pairs
    assert set(status["delivered_cids"]) == {cid for cid, _ in direct_outcome.decisions}


def test_safe_logs_and_status_never_include_memory_text(tmp_path: Path) -> None:
    clock = FakeClock()
    memory_text = "TOP SECRET MEMORY TEXT"

    stream = io.StringIO()
    logger = logging.getLogger("test-consumer-bridge-safe")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)

    run_id = "run-safe"
    session_id = "session-safe"
    bridge, _, inbox, _, session_fp = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[_queue_entry("cid-safe", text=memory_text)],
        clock=clock,
        logger=logger,
    )

    manifest = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-safe",
        decisions=(),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest)

    status = bridge.poll_once()
    logs = stream.getvalue()

    assert "PROGRESS " in logs
    assert memory_text not in logs
    assert memory_text not in json.dumps(status, sort_keys=True)


def test_outcome_observation_records_missing_and_marks_drained_ack(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-observe"
    session_id = "session-observe"
    bridge, state_dir, inbox, served_store_path, session_fp = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[_queue_entry("cid-accept")],
        clock=clock,
    )
    _write_json(served_store_path, {"version": 1, "memories": {}})

    manifest = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-observe",
        decisions=(),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest)

    first = bridge.poll_once()
    assert first["decision_emitted"] is True
    assert first["reconcile"]["missing_accepted"] == ["cid-accept"]
    assert first["side_effect_timeouts"] == ["cid-accept"]

    _write_json(state_dir / DECISIONS_FILENAME, [])
    second = bridge.poll_once()
    assert second["reason"] == "already_delivered"
    assert "cid-accept" in second["ack_summary"]["drained_cids"]

    state = load_state(bridge.state_path)
    assert state is not None
    record = state.consumed_manifests[f"{run_id}::{session_id}"]
    assert record.delivered[0].ack_status == "drained"
    assert state.plugin_outcome_refs["side_effect_timeouts"] == ["cid-accept"]


def test_restart_resume_without_duplicate_delivery(tmp_path: Path) -> None:
    clock = FakeClock()
    run_id = "run-restart"
    session_id = "session-restart"
    bridge, _, inbox, served_store_path, session_fp = _build_bridge(
        tmp_path,
        run_id=run_id,
        session_id=session_id,
        queue_payload=[_queue_entry("cid-r")],
        clock=clock,
    )

    manifest = _manifest(
        run_id=run_id,
        default_fate="accept",
        coordinator_trace="trace-restart",
        decisions=(),
    )
    _write_manifest(inbox, run_id=run_id, session_fp=session_fp, manifest=manifest)

    first = bridge.poll_once()
    assert first["reason"] == "delivered"

    state = load_state(bridge.state_path)
    assert state is not None
    delivered_before = len(state.consumed_manifests[f"{run_id}::{session_id}"].delivered)

    resumed = ConsumerBridge(
        coordinator=bridge.coordinator,
        state_path=bridge.state_path,
        manifest_inbox=inbox,
        served_store_path=served_store_path,
        run_id=run_id,
        session_id=session_id,
        session_fp=session_fp,
        clock=clock,
        lease_ttl_ms=120_000,
        heartbeat_cadence_ms=5_000,
        poll_interval_ms=10,
    )
    second = resumed.poll_once()
    assert second["decision_emitted"] is False
    assert second["reason"] == "already_delivered"

    state_after = load_state(bridge.state_path)
    assert state_after is not None
    delivered_after = len(state_after.consumed_manifests[f"{run_id}::{session_id}"].delivered)
    assert delivered_after == delivered_before


def test_one_worker_isolation_rejects_different_active_scope(tmp_path: Path) -> None:
    clock = FakeClock()
    bridge, _, _, served_store_path, _ = _build_bridge(
        tmp_path,
        run_id="run-primary",
        session_id="session-primary",
        queue_payload=[_queue_entry("cid-x")],
        clock=clock,
        lease_ttl_ms=120_000,
    )

    other_coordinator = ConsumerGateCoordinator(
        state_dir=tmp_path / "state",
        clock=clock,
    )
    with pytest.raises(ValueError, match="one active worker scope"):
        ConsumerBridge(
            coordinator=other_coordinator,
            state_path=bridge.state_path,
            manifest_inbox=tmp_path / "inbox",
            served_store_path=served_store_path,
            run_id="run-secondary",
            session_id="session-secondary",
            session_fp=SessionRecord.session_fp_of("session-secondary"),
            clock=clock,
            lease_ttl_ms=120_000,
            heartbeat_cadence_ms=5_000,
            poll_interval_ms=10,
        )
