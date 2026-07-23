import json
from pathlib import Path

import pytest

from wevibe_bench.cumulative.consumer_decision import (
    CONSUMER_DECISION_SCHEMA_VERSION,
    ConflictError,
    ConsumerCandidateDecision,
    ConsumerDecisionManifest,
    default_primary_manifest,
)
from wevibe_bench.cumulative.consumer_gate import ConsumerGateCoordinator


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest(
    *,
    run_id: str,
    default_fate: str,
    decisions: tuple[ConsumerCandidateDecision, ...],
    coordinator_trace: str,
) -> ConsumerDecisionManifest:
    return ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=run_id,
        policy_id="consumer-policy-v1",
        default_fate=default_fate,
        decisions=decisions,
        coordinator_trace=coordinator_trace,
    )


def test_apply_manifest_writes_decisions_and_heartbeat(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "wevibe-plugin-queue.json",
        [
            {"id": "cid-accept", "cid": "cid-accept", "text": "alpha", "source": "recall"},
            {"id": "cid-deny", "cid": "cid-deny", "text": "beta", "source": "recall"},
            {"id": "cid-block", "cid": "cid-block", "text": "gamma", "source": "recall"},
            {"id": "cid-report", "cid": "cid-report", "text": "delta", "source": "recall"},
        ],
    )

    coordinator = ConsumerGateCoordinator(state_dir=state_dir, clock=lambda: 1_735_100_000.123)
    manifest = _manifest(
        run_id="run-a",
        default_fate="accept",
        coordinator_trace="trace-a",
        decisions=(
            ConsumerCandidateDecision(
                run_id="run-a",
                session_id="session-a",
                candidate_cid="cid-deny",
                fate="deny",
                coordinator_trace="trace-a",
                reason="not relevant",
            ),
            ConsumerCandidateDecision(
                run_id="run-a",
                session_id="session-a",
                candidate_cid="cid-block",
                fate="block",
                coordinator_trace="trace-a",
                reason="unsafe",
            ),
            ConsumerCandidateDecision(
                run_id="run-a",
                session_id="session-a",
                candidate_cid="cid-report",
                fate="report",
                coordinator_trace="trace-a",
                reason="policy issue",
            ),
        ),
    )

    outcome = coordinator.apply_manifest(manifest, run_id="run-a", session_id="session-a")

    decisions = _read_json(state_dir / "wevibe-plugin-decisions.json")
    assert isinstance(decisions, list)
    assert len(decisions) == 4
    assert [entry["memoryID"] for entry in decisions] == [
        "cid-accept",
        "cid-deny",
        "cid-block",
        "cid-report",
    ]
    assert {entry["memoryID"]: entry["action"] for entry in decisions} == {
        "cid-accept": "accept",
        "cid-deny": "deny",
        "cid-block": "block",
        "cid-report": "report",
    }
    assert all(entry["timestamp"] == 1_735_100_000_123 for entry in decisions)

    heartbeat = _read_json(state_dir / "wevibe-tui-active.json")
    assert heartbeat == {"ts": 1_735_100_000_123}

    assert outcome.accept_count == 1
    assert outcome.deny_count == 1
    assert outcome.block_count == 1
    assert outcome.report_count == 1
    assert outcome.decisions == [
        ("cid-accept", "accept"),
        ("cid-deny", "deny"),
        ("cid-block", "block"),
        ("cid-report", "report"),
    ]


def test_apply_manifest_default_accept_applies_to_all_recalled_candidates(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "wevibe-plugin-queue.json",
        [
            {"id": "cid-1", "cid": "cid-1", "text": "a", "source": "recall"},
            {"id": "cid-2", "cid": "cid-2", "text": "b", "source": "recall"},
            {"id": "cid-3", "cid": "cid-3", "text": "c", "source": "recall"},
        ],
    )

    coordinator = ConsumerGateCoordinator(state_dir=state_dir, clock=lambda: 1_735_100_010.0)
    manifest = default_primary_manifest(
        "run-default",
        "session-default",
        {"cid-1", "cid-2", "cid-3"},
        coordinator_trace="trace-default",
    )

    coordinator.apply_manifest(manifest, run_id="run-default", session_id="session-default")

    decisions = _read_json(state_dir / "wevibe-plugin-decisions.json")
    assert isinstance(decisions, list)
    assert {entry["memoryID"]: entry["action"] for entry in decisions} == {
        "cid-1": "accept",
        "cid-2": "accept",
        "cid-3": "accept",
    }


def test_apply_manifest_fails_closed_for_decision_not_in_queue(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "wevibe-plugin-queue.json",
        [{"id": "cid-real", "cid": "cid-real", "text": "memory", "source": "recall"}],
    )

    coordinator = ConsumerGateCoordinator(state_dir=state_dir)
    manifest = _manifest(
        run_id="run-correlation",
        default_fate="accept",
        coordinator_trace="trace-correlation",
        decisions=(
            ConsumerCandidateDecision(
                run_id="run-correlation",
                session_id="session-correlation",
                candidate_cid="cid-missing",
                fate="deny",
                coordinator_trace="trace-correlation",
                reason="not recalled",
            ),
        ),
    )

    with pytest.raises(ValueError, match="not found in recalled_cids"):
        coordinator.apply_manifest(
            manifest,
            run_id="run-correlation",
            session_id="session-correlation",
        )


def test_apply_manifest_is_idempotent_and_rejects_fate_flip_replay(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(
        state_dir / "wevibe-plugin-queue.json",
        [{"id": "cid-1", "cid": "cid-1", "text": "memory", "source": "recall"}],
    )

    coordinator = ConsumerGateCoordinator(state_dir=state_dir, clock=lambda: 1_735_100_020.0)
    accept_manifest = _manifest(
        run_id="run-replay",
        default_fate="accept",
        coordinator_trace="trace-replay",
        decisions=(
            ConsumerCandidateDecision(
                run_id="run-replay",
                session_id="session-replay",
                candidate_cid="cid-1",
                fate="accept",
                coordinator_trace="trace-replay",
                reason="",
            ),
        ),
    )

    coordinator.apply_manifest(accept_manifest, run_id="run-replay", session_id="session-replay")
    first_decisions = _read_json(state_dir / "wevibe-plugin-decisions.json")

    coordinator.apply_manifest(accept_manifest, run_id="run-replay", session_id="session-replay")
    second_decisions = _read_json(state_dir / "wevibe-plugin-decisions.json")
    assert second_decisions == first_decisions

    flipped_manifest = _manifest(
        run_id="run-replay",
        default_fate="accept",
        coordinator_trace="trace-replay",
        decisions=(
            ConsumerCandidateDecision(
                run_id="run-replay",
                session_id="session-replay",
                candidate_cid="cid-1",
                fate="block",
                coordinator_trace="trace-replay",
                reason="flipped fate",
            ),
        ),
    )

    with pytest.raises(ConflictError, match="already applied with different fate"):
        coordinator.apply_manifest(flipped_manifest, run_id="run-replay", session_id="session-replay")


def test_served_store_reconcile_confirms_accept_and_detects_nonaccept_leak(tmp_path: Path) -> None:
    coordinator = ConsumerGateCoordinator(state_dir=tmp_path / "state")
    served_store_path = tmp_path / "served-memories.json"

    _write_json(
        served_store_path,
        {
            "version": 1,
            "memories": {
                "cid-accepted": {
                    "cid": "cid-accepted",
                    "text": "sensitive accepted memory",
                    "session_ids": ["session-store"],
                    "last_used_at": 100,
                }
            },
        },
    )

    reconcile = coordinator.served_store_reconcile(
        served_store_path,
        session_id="session-store",
        accepted_cids=["cid-accepted"],
        denied_cids=["cid-denied"],
        blocked_cids=[],
        reported_cids=[],
    )
    assert reconcile.served_store_present is True
    assert reconcile.accepted_confirmed == ["cid-accepted"]
    assert reconcile.missing_accepted == []
    assert reconcile.nonaccept_leaked == []

    _write_json(
        served_store_path,
        {
            "version": 1,
            "memories": {
                "cid-accepted": {
                    "cid": "cid-accepted",
                    "text": "sensitive accepted memory",
                    "session_ids": ["session-store"],
                    "last_used_at": 101,
                },
                "cid-denied": {
                    "cid": "cid-denied",
                    "text": "should not be served",
                    "session_ids": ["session-store"],
                    "last_used_at": 102,
                },
            },
        },
    )

    reconcile_with_leak = coordinator.served_store_reconcile(
        served_store_path,
        session_id="session-store",
        accepted_cids=["cid-accepted"],
        denied_cids=["cid-denied"],
        blocked_cids=[],
        reported_cids=[],
    )
    assert reconcile_with_leak.nonaccept_leaked == ["cid-denied"]


def test_outcome_to_dict_contains_no_memory_text(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    memory_text = "TOP SECRET MEMORY TEXT"
    _write_json(
        state_dir / "wevibe-plugin-queue.json",
        [{"id": "cid-1", "cid": "cid-1", "text": memory_text, "source": "recall"}],
    )

    coordinator = ConsumerGateCoordinator(state_dir=state_dir, clock=lambda: 1_735_100_030.0)
    manifest = default_primary_manifest(
        "run-safe",
        "session-safe",
        {"cid-1"},
        coordinator_trace="trace-safe",
    )

    outcome = coordinator.apply_manifest(manifest, run_id="run-safe", session_id="session-safe")
    payload = outcome.to_dict()
    assert memory_text not in json.dumps(payload, sort_keys=True)
