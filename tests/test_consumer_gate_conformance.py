import json
from pathlib import Path

import pytest

from wevibe_bench.cumulative.consumer_decision import (
    CONSUMER_DECISION_SCHEMA_VERSION,
    ConflictError,
    ConsumerCandidateDecision,
    ConsumerDecisionManifest,
)
from wevibe_bench.cumulative.consumer_gate import ConsumerGateCoordinator


RUN_ID = "run-conformance"
SESSION_ID = "session-conformance"
TRACE = "trace://consumer-gate/conformance"

MEMORY_TEXTS = {
    "c_accept": "QUEUE_TEXT_ACCEPT_MARKER",
    "c_deny": "QUEUE_TEXT_DENY_MARKER",
    "c_block": "QUEUE_TEXT_BLOCK_MARKER",
    "c_report": "QUEUE_TEXT_REPORT_MARKER",
}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def _queue_payload() -> list[dict[str, str]]:
    return [
        {"id": "c_accept", "cid": "c_accept", "text": MEMORY_TEXTS["c_accept"], "source": "recall"},
        {"id": "c_deny", "cid": "c_deny", "text": MEMORY_TEXTS["c_deny"], "source": "recall"},
        {"id": "c_block", "cid": "c_block", "text": MEMORY_TEXTS["c_block"], "source": "recall"},
        {"id": "c_report", "cid": "c_report", "text": MEMORY_TEXTS["c_report"], "source": "recall"},
    ]


def _manifest(
    *,
    run_id: str,
    session_id: str,
    deny_fate: str,
    deny_reason: str,
) -> ConsumerDecisionManifest:
    return ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=run_id,
        policy_id="consumer-gate-conformance-v1",
        default_fate="accept",
        decisions=(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="c_accept",
                fate="accept",
                coordinator_trace=TRACE,
                reason="",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="c_deny",
                fate=deny_fate,
                coordinator_trace=TRACE,
                reason=deny_reason,
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="c_block",
                fate="block",
                coordinator_trace=TRACE,
                reason="fixture-block-reason",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="c_report",
                fate="report",
                coordinator_trace=TRACE,
                reason="fixture-report-reason",
            ),
        ),
        coordinator_trace=TRACE,
    )


def test_four_fates_conform_to_plugin_decision_seam_and_counts(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(state_dir / "wevibe-plugin-queue.json", _queue_payload())

    coordinator = ConsumerGateCoordinator(state_dir=state_dir, clock=lambda: 1_735_100_100.125)
    outcome = coordinator.apply_manifest(
        _manifest(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            deny_fate="deny",
            deny_reason="fixture-deny-reason",
        ),
        run_id=RUN_ID,
        session_id=SESSION_ID,
    )

    decisions_path = state_dir / "wevibe-plugin-decisions.json"
    decisions_raw = decisions_path.read_text(encoding="utf-8")
    decisions_payload = json.loads(decisions_raw)
    assert isinstance(decisions_payload, list)
    assert len(decisions_payload) == 4

    assert [(entry["memoryID"], entry["action"]) for entry in decisions_payload] == [
        ("c_accept", "accept"),
        ("c_deny", "deny"),
        ("c_block", "block"),
        ("c_report", "report"),
    ]

    by_cid = {entry["memoryID"]: entry for entry in decisions_payload}
    assert by_cid["c_deny"]["reason"] == "fixture-deny-reason"
    assert by_cid["c_block"]["reason"] == "fixture-block-reason"
    assert by_cid["c_report"]["reason"] == "fixture-report-reason"

    assert outcome.accept_count == 1
    assert outcome.deny_count == 1
    assert outcome.block_count == 1
    assert outcome.report_count == 1
    assert outcome.decisions == [
        ("c_accept", "accept"),
        ("c_deny", "deny"),
        ("c_block", "block"),
        ("c_report", "report"),
    ]

    for memory_text in MEMORY_TEXTS.values():
        assert memory_text not in decisions_raw

    outcome_json = json.dumps(outcome.to_dict(), sort_keys=True)
    for memory_text in MEMORY_TEXTS.values():
        assert memory_text not in outcome_json


def test_apply_manifest_replay_idempotent_and_rejects_fate_flip(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(state_dir / "wevibe-plugin-queue.json", _queue_payload())

    coordinator = ConsumerGateCoordinator(state_dir=state_dir, clock=lambda: 1_735_100_101.0)
    stable_manifest = _manifest(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        deny_fate="deny",
        deny_reason="fixture-deny-reason",
    )

    coordinator.apply_manifest(stable_manifest, run_id=RUN_ID, session_id=SESSION_ID)
    first_write = (state_dir / "wevibe-plugin-decisions.json").read_text(encoding="utf-8")

    coordinator.apply_manifest(stable_manifest, run_id=RUN_ID, session_id=SESSION_ID)
    second_write = (state_dir / "wevibe-plugin-decisions.json").read_text(encoding="utf-8")
    assert second_write == first_write

    flipped_manifest = _manifest(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        deny_fate="block",
        deny_reason="fixture-flipped-fate-reason",
    )
    with pytest.raises(ConflictError, match="already applied with different fate"):
        coordinator.apply_manifest(flipped_manifest, run_id=RUN_ID, session_id=SESSION_ID)


def test_apply_manifest_fails_closed_for_correlation_violations(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_json(state_dir / "wevibe-plugin-queue.json", _queue_payload())

    coordinator = ConsumerGateCoordinator(state_dir=state_dir)

    unknown_cid_manifest = ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=RUN_ID,
        policy_id="consumer-gate-conformance-v1",
        default_fate="accept",
        decisions=(
            ConsumerCandidateDecision(
                run_id=RUN_ID,
                session_id=SESSION_ID,
                candidate_cid="cid-not-in-queue",
                fate="report",
                coordinator_trace=TRACE,
                reason="fixture-unknown-cid-reason",
            ),
        ),
        coordinator_trace=TRACE,
    )
    with pytest.raises(ValueError, match="not found in recalled_cids"):
        coordinator.apply_manifest(unknown_cid_manifest, run_id=RUN_ID, session_id=SESSION_ID)

    wrong_session_manifest = ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=RUN_ID,
        policy_id="consumer-gate-conformance-v1",
        default_fate="accept",
        decisions=(
            ConsumerCandidateDecision(
                run_id=RUN_ID,
                session_id="session-wrong",
                candidate_cid="c_accept",
                fate="deny",
                coordinator_trace=TRACE,
                reason="fixture-wrong-session-reason",
            ),
        ),
        coordinator_trace=TRACE,
    )
    with pytest.raises(ValueError, match="session_id mismatch"):
        coordinator.apply_manifest(wrong_session_manifest, run_id=RUN_ID, session_id=SESSION_ID)
