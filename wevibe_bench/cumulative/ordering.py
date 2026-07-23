"""Deterministic schedule ordering for cumulative benchmark sessions."""

from __future__ import annotations

import hashlib
import random

from .types import PhaseGroup, RosterEntry, ScheduledSession


def _require_non_empty_roster(roster: list[RosterEntry]) -> None:
    if not roster:
        raise ValueError("roster must not be empty")


def _require_non_negative_budget(*, budget: int, field_name: str) -> None:
    if budget < 0:
        raise ValueError(f"{field_name} must be non-negative")


def _seeded_rng(*, seed: int, roster_hash: str) -> random.Random:
    seed_int = int.from_bytes(
        hashlib.sha256(f"{seed}:{roster_hash}".encode()).digest()[:8],
        "big",
    )
    return random.Random(seed_int)


def build_off_order(roster: list[RosterEntry]) -> list[ScheduledSession]:
    """Build the declared-roster OFF baseline schedule in roster order."""
    _require_non_empty_roster(roster)
    return [
        ScheduledSession(
            sequence_index=index,
            model=entry.model,
            provider_pin=entry.provider_pin,
            memory_mode="off",
            phase_group=PhaseGroup.OFF_BASELINE.value,
            roster_index=index,
        )
        for index, entry in enumerate(roster)
    ]


def build_on_order(
    roster: list[RosterEntry],
    *,
    seed: int,
    roster_hash: str,
    budget: int,
    start_index: int,
) -> list[ScheduledSession]:
    """Build a deterministic seeded ON schedule with possible model repetition."""
    _require_non_empty_roster(roster)
    _require_non_negative_budget(budget=budget, field_name="budget")

    rng = _seeded_rng(seed=seed, roster_hash=roster_hash)
    sessions: list[ScheduledSession] = []
    roster_size = len(roster)
    for slot in range(budget):
        roster_index = rng.randrange(roster_size)
        entry = roster[roster_index]
        sessions.append(
            ScheduledSession(
                sequence_index=start_index + slot,
                model=entry.model,
                provider_pin=entry.provider_pin,
                memory_mode="on",
                phase_group=PhaseGroup.ON.value,
                roster_index=roster_index,
            )
        )
    return sessions


def build_schedule(
    roster: list[RosterEntry],
    *,
    seed: int,
    roster_hash: str,
    on_budget: int,
) -> list[ScheduledSession]:
    """Build cumulative schedule: full OFF baseline then seeded ON phase."""
    _require_non_empty_roster(roster)
    _require_non_negative_budget(budget=on_budget, field_name="on_budget")

    off_order = build_off_order(roster)
    on_order = build_on_order(
        roster,
        seed=seed,
        roster_hash=roster_hash,
        budget=on_budget,
        start_index=len(off_order),
    )
    return off_order + on_order


__all__ = ["build_off_order", "build_on_order", "build_schedule"]
