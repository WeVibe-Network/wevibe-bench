from __future__ import annotations

import subprocess

from wevibe_bench.cumulative.manifest import CumulativeManifest, resume_or_create, roster_hash
from wevibe_bench.cumulative.ordering import build_schedule
from wevibe_bench.cumulative.run_context import (
    collect_run_context,
    compare_run_context,
    parse_policy_anchor_log_line,
)
from wevibe_bench.cumulative.types import RosterEntry


POLICY_LINE = (
    '{"ts":"2026-07-30T01:00:00Z","op":"hub.policy_anchor",'
    '"status":"anchor_verified","policy_version":"edge-policy-v1",'
    '"policy_hash":"91b9a7e23e58c14e18c152851e0987093870b575a641d604c14de32f4c8df993"}'
)


def _roster() -> list[RosterEntry]:
    return [
        RosterEntry(
            model="openrouter/model-a",
            role="assistant",
            provider_pin="openrouter",
            config_identity={"slot": 1},
        )
    ]


def _schedule(roster: list[RosterEntry]):
    computed = roster_hash(roster)
    return build_schedule(roster, seed=17, roster_hash=computed, on_budget=1)


def test_parse_policy_anchor_log_line() -> None:
    parsed = parse_policy_anchor_log_line(f"prefix {POLICY_LINE}")

    assert parsed == {
        "version": "edge-policy-v1",
        "hash": "91b9a7e23e58c14e18c152851e0987093870b575a641d604c14de32f4c8df993",
        "anchor_status": "anchor_verified",
        "observed_at": "2026-07-30T01:00:00Z",
    }












def test_manifest_round_trip_with_and_without_run_context() -> None:
    roster = _roster()
    schedule = _schedule(roster)
    context = {"status": "available", "levers": {"L1_relevance_floor": {"value": "0.55", "source": "documented-default"}}}
    manifest = CumulativeManifest(
        created_at="2026-07-30T01:00:00Z",
        task="backgammon",
        org_id="org-test",
        roster=roster,
        roster_hash=roster_hash(roster),
        seed=17,
        config_fingerprint="cfg",
        schedule=schedule,
        session_records=[],
        current_index=0,
        updated_at="2026-07-30T01:00:00Z",
        run_context=context,
    )

    assert CumulativeManifest.from_dict(manifest.to_dict()).run_context == context

    payload_without_context = manifest.to_dict()
    payload_without_context.pop("run_context")
    assert CumulativeManifest.from_dict(payload_without_context).run_context is None


def test_resume_keeps_original_run_context(tmp_path) -> None:
    roster = _roster()
    schedule = _schedule(roster)
    path = tmp_path / "manifest.json"
    first_context = {"status": "available", "levers": {"L8_RETRIEVAL_TEMPERATURE": {"value": "0.7", "source": "hub-env"}}}
    second_context = {"status": "available", "levers": {"L8_RETRIEVAL_TEMPERATURE": {"value": "0.9", "source": "hub-env"}}}

    created = resume_or_create(
        path,
        roster=roster,
        seed=17,
        task="backgammon",
        org_id="org-test",
        config_fingerprint="cfg",
        schedule=schedule,
        run_context=first_context,
    )
    resumed = resume_or_create(
        path,
        roster=roster,
        seed=17,
        task="backgammon",
        org_id="org-test",
        config_fingerprint="cfg",
        schedule=schedule,
        run_context=second_context,
    )

    assert created.run_context == first_context
    assert resumed.run_context == first_context


def test_compare_run_context_flags_changed_lever() -> None:
    recorded = {"status": "available", "levers": {"L8_RETRIEVAL_TEMPERATURE": {"value": "0.7", "source": "hub-env"}}}
    current = {"status": "available", "levers": {"L8_RETRIEVAL_TEMPERATURE": {"value": "0.9", "source": "hub-env"}}}

    assert compare_run_context(recorded, current) == ["levers.L8_RETRIEVAL_TEMPERATURE"]
