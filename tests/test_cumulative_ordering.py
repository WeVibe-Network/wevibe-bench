import json

import pytest

from wevibe_bench.cumulative.manifest import roster_hash
from wevibe_bench.cumulative.ordering import (
    build_off_order,
    build_on_order,
    build_schedule,
)
from wevibe_bench.cumulative.types import PhaseGroup, RosterEntry


def _sample_roster() -> list[RosterEntry]:
    return [
        RosterEntry(
            model="model-alpha",
            role="assistant",
            provider_pin="provider-a",
            config_identity={"tier": "baseline", "slot": 1},
        ),
        RosterEntry(
            model="model-beta",
            role="assistant",
            provider_pin="provider-b",
            config_identity={"tier": "candidate", "slot": 2},
        ),
        RosterEntry(
            model="model-gamma",
            role="assistant",
            provider_pin="provider-c",
            config_identity={"tier": "candidate", "slot": 3},
        ),
    ]


def _to_bytes(sessions: list) -> bytes:
    payload = [session.to_dict() for session in sessions]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_build_off_order_declared_order_contract() -> None:
    roster = _sample_roster()

    sessions = build_off_order(roster)

    assert len(sessions) == len(roster)
    assert [session.sequence_index for session in sessions] == list(range(len(roster)))
    assert [session.roster_index for session in sessions] == list(range(len(roster)))
    assert [session.model for session in sessions] == [entry.model for entry in roster]
    assert [session.provider_pin for session in sessions] == [
        entry.provider_pin for entry in roster
    ]
    assert all(session.memory_mode == "off" for session in sessions)
    assert all(
        session.phase_group == PhaseGroup.OFF_BASELINE.value for session in sessions
    )


def test_build_on_order_and_schedule_reproducibility_and_shape() -> None:
    roster = _sample_roster()
    seed = 424242
    changed_seed = 424243
    on_budget = 64
    computed_roster_hash = roster_hash(roster)

    on_a = build_on_order(
        roster,
        seed=seed,
        roster_hash=computed_roster_hash,
        budget=on_budget,
        start_index=0,
    )
    on_b = build_on_order(
        roster,
        seed=seed,
        roster_hash=computed_roster_hash,
        budget=on_budget,
        start_index=0,
    )
    on_changed_seed = build_on_order(
        roster,
        seed=changed_seed,
        roster_hash=computed_roster_hash,
        budget=on_budget,
        start_index=0,
    )

    assert len(on_a) == on_budget
    assert [session.to_dict() for session in on_a] == [session.to_dict() for session in on_b]
    assert _to_bytes(on_a) == _to_bytes(on_b)
    assert _to_bytes(on_a) != _to_bytes(on_changed_seed)

    on_roster_indices = [session.roster_index for session in on_a]
    assert len(set(on_roster_indices)) < len(on_roster_indices)

    schedule_a = build_schedule(
        roster,
        seed=seed,
        roster_hash=computed_roster_hash,
        on_budget=on_budget,
    )
    schedule_b = build_schedule(
        roster,
        seed=seed,
        roster_hash=computed_roster_hash,
        on_budget=on_budget,
    )
    schedule_changed_seed = build_schedule(
        roster,
        seed=changed_seed,
        roster_hash=computed_roster_hash,
        on_budget=on_budget,
    )

    assert [session.to_dict() for session in schedule_a] == [
        session.to_dict() for session in schedule_b
    ]
    assert _to_bytes(schedule_a) == _to_bytes(schedule_b)

    off_count = len(roster)
    off_sessions = schedule_a[:off_count]
    on_sessions = schedule_a[off_count:]

    assert len(on_sessions) == on_budget
    assert [session.sequence_index for session in schedule_a] == list(
        range(len(schedule_a))
    )
    assert all(session.memory_mode == "off" for session in off_sessions)
    assert all(session.phase_group == PhaseGroup.OFF_BASELINE.value for session in off_sessions)
    assert all(session.memory_mode == "on" for session in on_sessions)
    assert all(session.phase_group == PhaseGroup.ON.value for session in on_sessions)

    assert _to_bytes(on_sessions) != _to_bytes(schedule_changed_seed[off_count:])


def test_ordering_bad_input_raises_value_error() -> None:
    roster = _sample_roster()
    computed_roster_hash = roster_hash(roster)

    with pytest.raises(ValueError):
        build_off_order([])

    with pytest.raises(ValueError):
        build_on_order(
            [],
            seed=7,
            roster_hash="unused",
            budget=1,
            start_index=0,
        )

    with pytest.raises(ValueError):
        build_on_order(
            roster,
            seed=7,
            roster_hash=computed_roster_hash,
            budget=-1,
            start_index=0,
        )

    with pytest.raises(ValueError):
        build_schedule(
            [],
            seed=7,
            roster_hash="unused",
            on_budget=1,
        )

    with pytest.raises(ValueError):
        build_schedule(
            roster,
            seed=7,
            roster_hash=computed_roster_hash,
            on_budget=-1,
        )
