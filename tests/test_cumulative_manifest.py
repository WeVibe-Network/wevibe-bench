from dataclasses import replace

import pytest

from wevibe_bench.cumulative.manifest import (
    CumulativeManifest,
    atomic_write,
    load,
    resume_or_create,
    roster_hash,
    validate_or_fail,
)
from wevibe_bench.cumulative.ordering import build_schedule
from wevibe_bench.cumulative.types import RosterEntry


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


def _sample_manifest() -> CumulativeManifest:
    roster = _sample_roster()
    seed = 12345
    computed_roster_hash = roster_hash(roster)
    schedule = build_schedule(
        roster,
        seed=seed,
        roster_hash=computed_roster_hash,
        on_budget=5,
    )

    return CumulativeManifest(
        created_at="2026-07-23T12:00:00Z",
        task="backgammon",
        org_id="org-test",
        roster=roster,
        roster_hash=computed_roster_hash,
        seed=seed,
        config_fingerprint="cfg-12345",
        schedule=schedule,
        session_records=[],
        current_index=2,
        updated_at="2026-07-23T12:30:00Z",
    )


def test_roster_hash_is_deterministic_and_order_sensitive() -> None:
    roster = _sample_roster()

    first = roster_hash(roster)
    second = roster_hash(roster)
    reordered = roster_hash(list(reversed(roster)))

    assert first == second
    assert first != reordered


def test_cumulative_manifest_dict_round_trip() -> None:
    manifest = _sample_manifest()

    reconstructed = CumulativeManifest.from_dict(manifest.to_dict())

    assert reconstructed == manifest


def test_atomic_write_then_load_round_trip(tmp_path) -> None:
    manifest = _sample_manifest()
    manifest_path = tmp_path / "manifest.json"

    atomic_write(manifest_path, manifest)
    loaded_manifest = load(manifest_path)

    assert loaded_manifest == manifest


def test_validate_or_fail_accepts_match_and_rejects_each_drift() -> None:
    manifest = _sample_manifest()

    validate_or_fail(
        manifest,
        expected_roster=manifest.roster,
        expected_seed=manifest.seed,
        expected_task=manifest.task,
    )

    with pytest.raises(ValueError, match="schema mismatch"):
        validate_or_fail(
            replace(manifest, schema_version=manifest.schema_version + 1),
            expected_roster=manifest.roster,
            expected_seed=manifest.seed,
            expected_task=manifest.task,
        )

    with pytest.raises(ValueError, match="roster hash drift"):
        validate_or_fail(
            manifest,
            expected_roster=list(reversed(manifest.roster)),
            expected_seed=manifest.seed,
            expected_task=manifest.task,
        )

    with pytest.raises(ValueError, match="seed drift"):
        validate_or_fail(
            manifest,
            expected_roster=manifest.roster,
            expected_seed=manifest.seed + 1,
            expected_task=manifest.task,
        )

    with pytest.raises(ValueError, match="task drift"):
        validate_or_fail(
            manifest,
            expected_roster=manifest.roster,
            expected_seed=manifest.seed,
            expected_task=f"{manifest.task}-other",
        )

    with pytest.raises(ValueError, match="chunk-plan hash drift"):
        validate_or_fail(
            replace(manifest, chunk_plan_hash="stale-hash"),
            expected_roster=manifest.roster,
            expected_seed=manifest.seed,
            expected_task=manifest.task,
            expected_chunk_plan_hash="fresh-hash",
        )

    # A manifest written before the chunk-plan field existed loads with an
    # empty hash and then fails drift validation loudly (never silently
    # resumes against a chunked prompt plan).
    legacy = CumulativeManifest.from_dict(
        {k: v for k, v in manifest.to_dict().items() if k != "chunk_plan_hash"}
    )
    assert legacy.chunk_plan_hash == ""
    with pytest.raises(ValueError, match="chunk-plan hash drift"):
        validate_or_fail(
            legacy,
            expected_roster=legacy.roster,
            expected_seed=legacy.seed,
            expected_task=legacy.task,
            expected_chunk_plan_hash="fresh-hash",
        )


def test_resume_or_create_create_then_resume_without_clobber(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    roster = _sample_roster()
    seed = 555
    task = "backgammon"
    org_id = "org-resume"
    config_fingerprint = "cfg-resume"
    computed_roster_hash = roster_hash(roster)
    schedule = build_schedule(
        roster,
        seed=seed,
        roster_hash=computed_roster_hash,
        on_budget=7,
    )

    created = resume_or_create(
        manifest_path,
        roster=roster,
        seed=seed,
        task=task,
        org_id=org_id,
        config_fingerprint=config_fingerprint,
        schedule=schedule,
    )

    assert manifest_path.exists()
    assert created.current_index == 0
    assert created.session_records == []

    bumped = replace(created, current_index=3, updated_at="2026-07-23T13:00:00Z")
    atomic_write(manifest_path, bumped)

    resumed = resume_or_create(
        manifest_path,
        roster=roster,
        seed=seed,
        task=task,
        org_id=org_id,
        config_fingerprint=config_fingerprint,
        schedule=schedule,
    )

    assert resumed == bumped
    assert resumed.current_index == 3
    assert load(manifest_path) == bumped

    with pytest.raises(ValueError, match="seed drift"):
        resume_or_create(
            manifest_path,
            roster=roster,
            seed=seed + 1,
            task=task,
            org_id=org_id,
            config_fingerprint=config_fingerprint,
            schedule=schedule,
        )
