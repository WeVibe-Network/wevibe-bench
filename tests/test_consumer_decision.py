import pytest

from wevibe_bench.cumulative.consumer_decision import (
    CONSUMER_DECISION_SCHEMA_VERSION,
    DEFAULT_PRIMARY_POLICY,
    ConflictError,
    ConsumerCandidateDecision,
    ConsumerDecisionManifest,
    default_primary_manifest,
    resolve_fate,
    validate_correlation,
    validate_one_per_candidate,
    validate_replay,
    validate_schema,
)


def _decision(
    *,
    run_id: str = "run-1",
    session_id: str = "session-1",
    candidate_cid: str = "cid-a",
    fate: str = "accept",
    coordinator_trace: str = "trace-1",
    reason: str = "",
    note: str = "",
) -> ConsumerCandidateDecision:
    return ConsumerCandidateDecision(
        run_id=run_id,
        session_id=session_id,
        candidate_cid=candidate_cid,
        fate=fate,
        coordinator_trace=coordinator_trace,
        reason=reason,
        note=note,
    )


def _manifest(
    *,
    run_id: str = "run-1",
    policy_id: str = "policy-v1",
    default_fate: str = "accept",
    decisions: tuple[ConsumerCandidateDecision, ...] = (),
    coordinator_trace: str = "trace-1",
) -> ConsumerDecisionManifest:
    return ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=run_id,
        policy_id=policy_id,
        default_fate=default_fate,
        decisions=decisions,
        coordinator_trace=coordinator_trace,
    )


def test_validate_schema_rejects_missing_default_bad_fate_and_non_accept_without_reason() -> None:
    validate_schema(_manifest())

    missing_default = _manifest(default_fate="   ")
    with pytest.raises(ValueError, match="primary default policy must be declared explicitly"):
        validate_schema(missing_default)

    bad_fate = _manifest(decisions=(_decision(fate="approve", reason="not a valid fate"),))
    with pytest.raises(ValueError, match="must be one of"):
        validate_schema(bad_fate)

    missing_reason = _manifest(decisions=(_decision(fate="deny", reason="   "),))
    with pytest.raises(ValueError, match="reason"):
        validate_schema(missing_reason)


def test_validate_one_per_candidate_rejects_duplicates() -> None:
    manifest = _manifest(
        decisions=(
            _decision(candidate_cid="cid-a"),
            _decision(candidate_cid="cid-a", fate="block", reason="duplicate should fail"),
        )
    )

    with pytest.raises(ValueError, match="duplicate decision for session/candidate"):
        validate_one_per_candidate(manifest)


def test_validate_correlation_rejects_wrong_candidate_and_session_and_reports_uncovered() -> None:
    manifest = _manifest(decisions=(_decision(candidate_cid="cid-a"),))
    uncovered = validate_correlation(
        manifest,
        run_id="run-1",
        session_id="session-1",
        recalled_cids={"cid-a", "cid-b"},
    )
    assert uncovered == {"cid-b"}

    wrong_candidate = _manifest(decisions=(_decision(candidate_cid="cid-missing"),))
    with pytest.raises(ValueError, match="not found in recalled_cids"):
        validate_correlation(
            wrong_candidate,
            run_id="run-1",
            session_id="session-1",
            recalled_cids={"cid-a", "cid-b"},
        )

    wrong_session = _manifest(
        decisions=(_decision(session_id="session-other", candidate_cid="cid-a"),)
    )
    with pytest.raises(ValueError, match="session_id mismatch"):
        validate_correlation(
            wrong_session,
            run_id="run-1",
            session_id="session-1",
            recalled_cids={"cid-a"},
        )


def test_validate_replay_allows_idempotent_and_rejects_fate_flip() -> None:
    previous = _manifest(decisions=(_decision(candidate_cid="cid-a", fate="accept"),))
    identical_replay = _manifest(decisions=(_decision(candidate_cid="cid-a", fate="accept"),))
    validate_replay(previous, identical_replay)

    flipped = _manifest(
        decisions=(_decision(candidate_cid="cid-a", fate="block", reason="new denial"),)
    )
    with pytest.raises(ConflictError, match="already applied with different fate"):
        validate_replay(previous, flipped)


def test_resolve_fate_returns_explicit_else_default_accept() -> None:
    explicit = _decision(candidate_cid="cid-a", fate="report", reason="safety concern")
    manifest = _manifest(decisions=(explicit,))

    assert resolve_fate(manifest, session_id="session-1", candidate_cid="cid-a") == explicit

    fallback = resolve_fate(manifest, session_id="session-1", candidate_cid="cid-b")
    assert fallback == ConsumerCandidateDecision(
        run_id="run-1",
        session_id="session-1",
        candidate_cid="cid-b",
        fate="accept",
        coordinator_trace="trace-1",
        reason="",
        note="",
    )


def test_to_stored_decision_emits_plugin_shape() -> None:
    decision = _decision(
        candidate_cid="cid-42",
        fate="deny",
        reason="not useful",
        note="session-local skip",
    )

    assert decision.to_stored_decision(1_735_100_000_123) == {
        "memoryID": "cid-42",
        "action": "deny",
        "reason": "not useful",
        "note": "session-local skip",
        "timestamp": 1_735_100_000_123,
    }


def test_default_primary_manifest_declares_explicit_accept_default_policy() -> None:
    manifest = default_primary_manifest(
        "run-77",
        "session-77",
        {"cid-a", "cid-b"},
        coordinator_trace="trace-77",
    )

    assert manifest == ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id="run-77",
        policy_id=DEFAULT_PRIMARY_POLICY,
        default_fate="accept",
        decisions=(),
        coordinator_trace="trace-77",
    )
    validate_schema(manifest)
