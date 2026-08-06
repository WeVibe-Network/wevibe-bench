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
    http_429_count: int | None = None,
    http_402_count: int | None = None,
    retry_count: int | None = None,
    upstream_error_count: int | None = None,
    max_request_ms: int | None = None,
    median_request_ms: int | None = None,
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
        http_429_count=http_429_count,
        http_402_count=http_402_count,
        retry_count=retry_count,
        upstream_error_count=upstream_error_count,
        max_request_ms=max_request_ms,
        median_request_ms=median_request_ms,
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


def test_model_diffs_surface_per_arm_contention_asymmetry() -> None:
    cfg = _cfg()
    scorecard = Scorecard(cfg, _now=datetime(2026, 7, 8, tzinfo=timezone.utc))

    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="off-1",
            condition="OFF",
            resolved=False,
            input_tokens=100,
            output_tokens=100,
            wall_cost_usd=1.0,
            http_429_count=4,
            http_402_count=1,
            retry_count=6,
            upstream_error_count=2,
            max_request_ms=1200,
            median_request_ms=700,
        )
    )
    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="off-2",
            condition="OFF",
            resolved=True,
            input_tokens=100,
            output_tokens=100,
            wall_cost_usd=1.0,
            http_429_count=2,
            http_402_count=0,
            retry_count=4,
            upstream_error_count=0,
            max_request_ms=900,
            median_request_ms=500,
        )
    )
    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="on-1",
            condition="ON",
            resolved=True,
            input_tokens=100,
            output_tokens=100,
            wall_cost_usd=1.0,
            http_429_count=0,
            http_402_count=0,
            retry_count=0,
            upstream_error_count=0,
            max_request_ms=300,
            median_request_ms=200,
        )
    )

    payload = scorecard.model_diffs()[0].to_dict()

    assert payload["off_contention"] == {
        "cell_count": 2,
        "measured_cell_count": 2,
        "http_429_count_mean": 3.0,
        "http_429_count_max": 4,
        "http_402_count_mean": 0.5,
        "http_402_count_max": 1,
        "retry_count_mean": 5.0,
        "retry_count_max": 6,
        "upstream_error_count_mean": 1.0,
        "upstream_error_count_max": 2,
        "median_request_ms_mean": 600.0,
        "max_request_ms_max": 1200,
    }
    assert payload["on_contention"] == {
        "cell_count": 1,
        "measured_cell_count": 1,
        "http_429_count_mean": 0.0,
        "http_429_count_max": 0,
        "http_402_count_mean": 0.0,
        "http_402_count_max": 0,
        "retry_count_mean": 0.0,
        "retry_count_max": 0,
        "upstream_error_count_mean": 0.0,
        "upstream_error_count_max": 0,
        "median_request_ms_mean": 200.0,
        "max_request_ms_max": 300,
    }


def test_model_diffs_leave_unmeasured_contention_as_none_not_zero() -> None:
    cfg = _cfg()
    scorecard = Scorecard(cfg, _now=datetime(2026, 7, 8, tzinfo=timezone.utc))
    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="off-1",
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
            task_id="on-1",
            condition="ON",
            resolved=True,
            input_tokens=100,
            output_tokens=100,
            wall_cost_usd=1.0,
        )
    )

    payload = scorecard.model_diffs()[0].to_dict()

    assert payload["off_contention"]["cell_count"] == 1
    assert payload["off_contention"]["measured_cell_count"] == 0
    assert payload["off_contention"]["http_429_count_mean"] is None
    assert payload["off_contention"]["retry_count_max"] is None
    assert payload["on_contention"]["cell_count"] == 1
    assert payload["on_contention"]["measured_cell_count"] == 0
    assert payload["on_contention"]["upstream_error_count_mean"] is None
    assert payload["on_contention"]["max_request_ms_max"] is None


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


def test_model_diffs_metrics_are_invariant_to_arm_label() -> None:
    """Guard for Ruling #1: computed per-arm metrics depend only on the numeric
    inputs, never on which arm label (OFF vs ON) a cell sits under."""
    cfg = _cfg()
    fixed_now = datetime(2026, 7, 8, tzinfo=timezone.utc)

    # Scenario 1: one OFF and one ON cell with IDENTICAL numeric inputs.
    # Reading the arm label to group/label is not branching, and the pure
    # arithmetic must yield the same numbers under either label.
    scorecard = Scorecard(cfg, _now=fixed_now)
    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="same",
            condition="OFF",
            resolved=True,
            input_tokens=100,
            output_tokens=100,
            wall_cost_usd=1.0,
        )
    )
    scorecard.add_cell(
        _cell(
            model="model-a",
            task_id="same",
            condition="ON",
            resolved=True,
            input_tokens=100,
            output_tokens=100,
            wall_cost_usd=1.0,
        )
    )
    diff = scorecard.model_diffs()[0]
    assert diff.off_pass_rate == diff.on_pass_rate == 1.0
    assert diff.off_total_tokens == diff.on_total_tokens == 200
    assert diff.off_cost_usd == diff.on_cost_usd == 1.0
    assert diff.total_token_delta_pct == 0.0
    assert diff.cost_delta_pct == 0.0
    assert diff.capability_lift_pp == 0.0

    # Scenario 2: two distinct numeric cells, then the arm labels swapped via
    # dataclasses.replace. Each arm's computed numbers must travel with the
    # underlying data, not with the label.
    from dataclasses import replace

    off_data = _cell(
        model="model-a",
        task_id="data-x",
        condition="OFF",
        resolved=True,
        input_tokens=50,
        output_tokens=50,
        wall_cost_usd=0.5,
    )
    on_data = _cell(
        model="model-a",
        task_id="data-y",
        condition="ON",
        resolved=False,
        input_tokens=80,
        output_tokens=120,
        wall_cost_usd=2.0,
    )

    set_a = Scorecard(cfg, _now=fixed_now)
    set_a.add_cell(off_data)
    set_a.add_cell(on_data)
    diff_a = set_a.model_diffs()[0]

    set_b = Scorecard(cfg, _now=fixed_now)
    set_b.add_cell(replace(off_data, condition="ON"))
    set_b.add_cell(replace(on_data, condition="OFF"))
    diff_b = set_b.model_diffs()[0]

    assert diff_a.off_total_tokens == diff_b.on_total_tokens == 100
    assert diff_a.on_total_tokens == diff_b.off_total_tokens == 200
    assert diff_a.off_cost_usd == diff_b.on_cost_usd == 0.5
    assert diff_a.on_cost_usd == diff_b.off_cost_usd == 2.0
    assert diff_a.off_pass_rate == diff_b.on_pass_rate == 1.0
    assert diff_a.on_pass_rate == diff_b.off_pass_rate == 0.0
