import importlib.util
from pathlib import Path
from typing import Any

from wevibe_bench.cumulative.bridge_state import (
    BRIDGE_STATE_SCHEMA_VERSION,
    BridgeDaemonState,
    ConsumedManifestRecord,
    DeliveredDecision,
    atomic_write_state,
)
from wevibe_bench.cumulative.consumer_gate import ConsumerGateOutcome
from wevibe_bench.cumulative.types import ConsumerGateRecord


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_outcome_refs() -> dict[str, list[str]]:
    return {
        "served_confirmed": [],
        "denied": [],
        "reported": [],
        "blocked": [],
        "missing_accepted": [],
        "nonaccept_leaked": [],
        "side_effect_timeouts": [],
    }


def test_durable_consumer_gate_counts_happy_path_two_drained_accepts(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()

    run_id = "run-happy"
    scope_key = f"{run_id}::sess"
    state = BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id=run_id,
        session_id="sess",
        session_fp="fp-happy",
        container_name="bridge-happy",
        lease=None,
        consumed_manifests={
            scope_key: ConsumedManifestRecord(
                scope_key=scope_key,
                manifest_digest="digest-happy",
                coordinator_trace="trace-happy",
                applied_at_ms=1,
                delivered=(
                    DeliveredDecision(
                        candidate_cid="cid-1",
                        fate="accept",
                        delivered_at_ms=2,
                        ack_status="drained",
                        outcome_ref="outcome-1",
                    ),
                    DeliveredDecision(
                        candidate_cid="cid-2",
                        fate="accept",
                        delivered_at_ms=3,
                        ack_status="drained",
                        outcome_ref="outcome-2",
                    ),
                ),
            )
        },
        heartbeat_last_ts_ms=None,
        plugin_outcome_refs=_plugin_outcome_refs(),
        resume_marker="active",
    )
    state_path = tmp_path / "bridge-state.json"
    atomic_write_state(state_path, state)

    assert module._durable_consumer_gate_counts(state_path, run_id) == (2, 2)


def test_durable_consumer_gate_counts_filters_fates_ack_and_dedups(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()

    run_id = "run-mix"
    scope_a = f"{run_id}::session-a"
    scope_b = f"{run_id}::session-b"
    state = BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id=run_id,
        session_id="session-a",
        session_fp="fp-mix",
        container_name="bridge-mix",
        lease=None,
        consumed_manifests={
            scope_a: ConsumedManifestRecord(
                scope_key=scope_a,
                manifest_digest="digest-a",
                coordinator_trace="trace-a",
                applied_at_ms=11,
                delivered=(
                    DeliveredDecision(
                        candidate_cid="cid-a",
                        fate="accept",
                        delivered_at_ms=12,
                        ack_status="pending",
                        outcome_ref=None,
                    ),
                    DeliveredDecision(
                        candidate_cid="cid-deny",
                        fate="deny",
                        delivered_at_ms=13,
                        ack_status="drained",
                        outcome_ref=None,
                    ),
                    DeliveredDecision(
                        candidate_cid="cid-dup",
                        fate="accept",
                        delivered_at_ms=14,
                        ack_status="drained",
                        outcome_ref=None,
                    ),
                ),
            ),
            scope_b: ConsumedManifestRecord(
                scope_key=scope_b,
                manifest_digest="digest-b",
                coordinator_trace="trace-b",
                applied_at_ms=21,
                delivered=(
                    DeliveredDecision(
                        candidate_cid="cid-dup",
                        fate="accept",
                        delivered_at_ms=22,
                        ack_status="drained",
                        outcome_ref=None,
                    ),
                    DeliveredDecision(
                        candidate_cid="cid-b",
                        fate="accept",
                        delivered_at_ms=23,
                        ack_status="pending",
                        outcome_ref=None,
                    ),
                    DeliveredDecision(
                        candidate_cid="cid-block",
                        fate="block",
                        delivered_at_ms=24,
                        ack_status="drained",
                        outcome_ref=None,
                    ),
                ),
            ),
            "run-other::session-x": ConsumedManifestRecord(
                scope_key="run-other::session-x",
                manifest_digest="digest-other",
                coordinator_trace="trace-other",
                applied_at_ms=31,
                delivered=(
                    DeliveredDecision(
                        candidate_cid="cid-other",
                        fate="accept",
                        delivered_at_ms=32,
                        ack_status="drained",
                        outcome_ref=None,
                    ),
                ),
            ),
        },
        heartbeat_last_ts_ms=None,
        plugin_outcome_refs=_plugin_outcome_refs(),
        resume_marker="active",
    )
    state_path = tmp_path / "bridge-state.json"
    atomic_write_state(state_path, state)

    # accept-only counts, drained-only injection count, duplicate cid across records counted once
    assert module._durable_consumer_gate_counts(state_path, run_id) == (3, 1)
    assert module._durable_consumer_gate_counts(state_path, "run-absent") is None
    assert module._durable_consumer_gate_counts(tmp_path / "missing-state.json", run_id) is None


def test_durable_counts_ignore_transient_queue_rewrite_regression_shape(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()

    run_id = "run-r0"
    scope_key = f"{run_id}::sess"
    state = BridgeDaemonState(
        schema_version=BRIDGE_STATE_SCHEMA_VERSION,
        run_id=run_id,
        session_id="sess",
        session_fp="fp-r0",
        container_name="bridge-r0",
        lease=None,
        consumed_manifests={
            scope_key: ConsumedManifestRecord(
                scope_key=scope_key,
                manifest_digest="digest-r0",
                coordinator_trace="trace-r0",
                applied_at_ms=41,
                delivered=(
                    DeliveredDecision(
                        candidate_cid="cid-1",
                        fate="accept",
                        delivered_at_ms=42,
                        ack_status="drained",
                        outcome_ref=None,
                    ),
                    DeliveredDecision(
                        candidate_cid="cid-2",
                        fate="accept",
                        delivered_at_ms=43,
                        ack_status="drained",
                        outcome_ref=None,
                    ),
                ),
            )
        },
        heartbeat_last_ts_ms=None,
        plugin_outcome_refs=_plugin_outcome_refs(),
        resume_marker="active",
    )
    state_path = tmp_path / "bridge-state.json"
    atomic_write_state(state_path, state)

    # R0 D-D regression shape: transient queue can be rewritten to one entry late,
    # but durable bridge delivery history still records the true 2 accepted + 2 drained.
    queue_path = tmp_path / "wevibe-plugin-queue.json"
    queue_path.write_text('[{"cid":"cid-2"}]\n', encoding="utf-8")
    assert queue_path.is_file()

    assert module._durable_consumer_gate_counts(state_path, run_id) == (2, 2)


def test_consumer_gate_record_from_outcome_durable_counts_override_and_legacy_mapping() -> None:
    outcome = ConsumerGateOutcome(
        run_id="run-outcome",
        session_id="session-outcome",
        coordinator_trace="trace://durable",
        accept_count=1,
        deny_count=0,
        block_count=0,
        report_count=0,
        decisions=[("cid-1", "accept")],
        decisions_path="/tmp/decisions.json",
    )

    durable = ConsumerGateRecord.from_outcome(
        outcome,
        durable_accepted_count=2,
        durable_injected_count=2,
    )
    assert durable.accepted_count == 2
    assert durable.consumer_injected_count == 2

    legacy = ConsumerGateRecord.from_outcome(outcome)
    assert legacy.accepted_count == 1
    assert legacy.consumer_injected_count == 1
