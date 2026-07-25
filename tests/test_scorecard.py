from __future__ import annotations

from datetime import datetime, timezone
import json

from wevibe_bench.config import BenchmarkSchedule, BenchmarkWave, RunConfig
from wevibe_bench.scorecard import Cell, Scorecard


def _cfg() -> RunConfig:
    return RunConfig(
        schedule=BenchmarkSchedule(
            waves=(BenchmarkWave(wave_id="single", models=("model-a",)),),
        ),
        rng_seed=9001,
    )


def _cell(
    *,
    model: str,
    task_id: str,
    condition: str,
    resolved: bool,
    input_tokens: int,
    output_tokens: int,
    wall_cost_usd: float,
    scored: bool = True,
) -> Cell:
    return Cell(
        model=model,
        task_id=task_id,
        condition=condition,
        resolved=resolved,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        turns=1,
        wall_cost_usd=wall_cost_usd,
        wall_seconds=1.0,
        delivery="YES" if condition == "ON" else "N/A",
        scored=scored,
        not_scored_reason=None if scored else "delivery=NO",
    )


def test_cell_total_tokens_is_input_plus_output() -> None:
    cell = _cell(
        model="m",
        task_id="t",
        condition="OFF",
        resolved=False,
        input_tokens=123,
        output_tokens=77,
        wall_cost_usd=1.0,
    )
    assert cell.total_tokens == 200


def test_cell_to_dict_includes_injected_block_fields_without_affecting_total_tokens() -> None:
    cell = Cell(
        model="m",
        task_id="t",
        condition="ON",
        resolved=True,
        input_tokens=123,
        output_tokens=77,
        turns=1,
        wall_cost_usd=1.0,
        wall_seconds=1.0,
        delivery="YES",
        scored=True,
        injected_block_chars=2400,
        injected_block_est_tokens=600,
    )

    payload = cell.to_dict()

    assert payload["injected_block_chars"] == 2400
    assert payload["injected_block_est_tokens"] == 600
    assert payload["total_tokens"] == 200
    assert cell.total_tokens == 200


def test_model_diff_math_for_capability_tokens_and_cost() -> None:
    cfg = _cfg()
    scorecard = Scorecard(cfg, _now=datetime(2026, 7, 8, tzinfo=timezone.utc))

    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="t1",
            condition="OFF",
            resolved=False,
            input_tokens=100,
            output_tokens=100,
            wall_cost_usd=1.0,
        )
    )
    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="t2",
            condition="OFF",
            resolved=True,
            input_tokens=50,
            output_tokens=50,
            wall_cost_usd=1.0,
        )
    )
    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="t1",
            condition="ON",
            resolved=True,
            input_tokens=110,
            output_tokens=115,
            wall_cost_usd=1.5,
        )
    )
    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="t2",
            condition="ON",
            resolved=True,
            input_tokens=100,
            output_tokens=125,
            wall_cost_usd=1.5,
        )
    )

    diff = scorecard.model_diffs()[0]

    assert diff.off_pass_rate == 0.5
    assert diff.on_pass_rate == 1.0
    assert diff.capability_lift_pp == 50.0
    assert diff.off_total_tokens == 300
    assert diff.on_total_tokens == 450
    assert diff.total_token_delta_pct == 50.0
    assert diff.off_cost_usd == 2.0
    assert diff.on_cost_usd == 3.0
    assert diff.cost_delta_pct == 50.0


def test_model_diffs_ignore_injected_block_fields_in_token_totals() -> None:
    cfg = _cfg()
    scorecard = Scorecard(cfg, _now=datetime(2026, 7, 8, tzinfo=timezone.utc))

    scorecard.add_cell(
        Cell(
            model="model-a",
            task_id="off-1",
            condition="OFF",
            resolved=False,
            input_tokens=100,
            output_tokens=100,
            turns=1,
            wall_cost_usd=1.0,
            wall_seconds=1.0,
            delivery="N/A",
            scored=True,
            injected_block_chars=9999,
            injected_block_est_tokens=2500,
        )
    )
    scorecard.add_cell(
        Cell(
            model="model-a",
            task_id="on-1",
            condition="ON",
            resolved=True,
            input_tokens=110,
            output_tokens=90,
            turns=1,
            wall_cost_usd=1.0,
            wall_seconds=1.0,
            delivery="YES",
            scored=True,
            injected_block_chars=4000,
            injected_block_est_tokens=1000,
        )
    )

    diff = scorecard.model_diffs()[0]

    assert diff.off_total_tokens == 200
    assert diff.on_total_tokens == 200
    assert diff.total_token_delta_pct == 0.0


def test_manifest_contains_verbatim_config_seed_version_created_at_and_json_round_trip() -> None:
    cfg = _cfg()
    fixed_now = datetime(2026, 7, 8, 17, 5, 0, tzinfo=timezone.utc)
    scorecard = Scorecard(cfg, split_disclosure={"seed_count": 1}, _now=fixed_now)

    assert scorecard.manifest.config == cfg.to_dict()
    assert scorecard.manifest.rng_seed == cfg.rng_seed
    assert scorecard.manifest.harness_version == cfg.harness_version
    assert scorecard.manifest.created_at == fixed_now.isoformat()

    payload = json.loads(scorecard.to_json())
    assert payload["manifest"]["config"] == cfg.to_dict()
    assert payload["manifest"]["rng_seed"] == cfg.rng_seed
    assert payload["manifest"]["harness_version"] == cfg.harness_version
    assert payload["manifest"]["created_at"] == fixed_now.isoformat()
    assert payload["manifest"]["split_disclosure"] == {"seed_count": 1}
