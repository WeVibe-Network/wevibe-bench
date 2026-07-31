from wevibe_bench.cumulative.types import (
    SessionRecord,
    WalkGateName,
    WalkGateVerdict,
    WalkGateVerdictRecord,
)


def test_walk_gate_record_round_trips_with_not_evaluated_state() -> None:
    gate = WalkGateVerdictRecord(
        ordinal=1,
        gate=WalkGateName.INJECTION.value,
        verdict=WalkGateVerdict.NOT_EVALUATED.value,
        evidence={"expected_count": 2, "observed_count": 0},
        expected_producer_model_ids=("z-ai/glm-5.2", "kimi/kimi-k3"),
        observed_producer_model_ids=("minimax/minimax-m3",),
    )

    reconstructed = WalkGateVerdictRecord.from_dict(gate.to_dict())

    assert reconstructed == gate
    assert reconstructed.verdict == WalkGateVerdict.NOT_EVALUATED.value
    assert reconstructed.stops_walk is False


def test_walk_gate_records_attach_to_session_record_round_trip() -> None:
    session = SessionRecord(
        sequence_index=4,
        model="kimi/kimi-k3",
        provider_pin="openrouter",
        memory_mode="on",
        phase_group="on",
        phase="DONE",
        walk_gates=[
            WalkGateVerdictRecord(
                ordinal=1,
                gate=WalkGateName.INJECTION.value,
                verdict=WalkGateVerdict.PASS.value,
                evidence={"observed_by_producer": {"z-ai/glm-5.2": 3}},
                expected_producer_model_ids=("z-ai/glm-5.2", "kimi/kimi-k3"),
                observed_producer_model_ids=("z-ai/glm-5.2", "kimi/kimi-k3"),
            )
        ],
    )

    reconstructed = SessionRecord.from_dict(session.to_dict())

    assert reconstructed == session
    assert reconstructed.to_dict()["walk_gates"] == [
        session.walk_gates[0].to_dict()
    ]


def test_walk_gate_ordering_failed_injection_leaves_later_gates_not_evaluated() -> None:
    injection = WalkGateVerdictRecord(
        ordinal=99,
        gate="ignored",
        verdict=WalkGateVerdict.FAIL.value,
        evidence={"missing_producer_model_ids": ["kimi/kimi-k3"]},
        expected_producer_model_ids=("z-ai/glm-5.2", "kimi/kimi-k3"),
        observed_producer_model_ids=("z-ai/glm-5.2",),
    )
    work = WalkGateVerdictRecord(
        ordinal=2,
        gate=WalkGateName.WORK.value,
        verdict=WalkGateVerdict.FAIL.value,
        evidence={"full_green": False},
    )
    lift = WalkGateVerdictRecord(
        ordinal=3,
        gate=WalkGateName.LIFT.value,
        verdict=WalkGateVerdict.FAIL.value,
        evidence={"delta": -1},
    )

    gates = WalkGateVerdictRecord.records_for_ordered_gates(
        injection=injection,
        work=work,
        lift=lift,
    )

    assert [gate.gate for gate in gates] == ["injection", "work", "lift"]
    assert [gate.ordinal for gate in gates] == [1, 2, 3]
    assert gates[0].verdict == WalkGateVerdict.FAIL.value
    assert gates[1].verdict == WalkGateVerdict.NOT_EVALUATED.value
    assert gates[2].verdict == WalkGateVerdict.NOT_EVALUATED.value


def test_walk_gate_failing_verdict_sets_stop_flag() -> None:
    gate = WalkGateVerdictRecord(
        ordinal=2,
        gate=WalkGateName.WORK.value,
        verdict=WalkGateVerdict.FAIL.value,
        evidence={"full_green": False},
    )

    assert gate.stops_walk is True
    assert gate.to_dict()["stops_walk"] is True
