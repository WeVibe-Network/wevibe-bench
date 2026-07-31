"""Scorecard artifacts for memory ablation benchmarking.

This module enforces BENCHMARK INTEGRITY (not_scored never counts), TOTAL tokens
(input+output), and reproducibility (config verbatim + rng_seed + timestamp in
the manifest of every scorecard).

Per D-BENCH-CONTRACT-2026-07, result fields capture pattern-position effects,
run-block grouping, injected-memory counts, and execution memory mode (OFF|ON)
— enabling pattern/quantity-resilience measurement without flooding.

Phase A fields (nullable: when not populated by the current execution path,
they remain None — this is an honest UNSUPPORTED/UNAVAILABLE status, not mock
proof). Phase B will populate them from harness-trusted producer+extraction
evidence and equal-or-lower filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any


def _mean_int(values: list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _max_int(values: list[int]) -> int | None:
    return max(values) if values else None


@dataclass
class ContentionArmSummary:
    cell_count: int
    measured_cell_count: int
    http_429_count_mean: float | None
    http_429_count_max: int | None
    http_402_count_mean: float | None
    http_402_count_max: int | None
    retry_count_mean: float | None
    retry_count_max: int | None
    upstream_error_count_mean: float | None
    upstream_error_count_max: int | None
    median_request_ms_mean: float | None
    max_request_ms_max: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_count": self.cell_count,
            "measured_cell_count": self.measured_cell_count,
            "http_429_count_mean": self.http_429_count_mean,
            "http_429_count_max": self.http_429_count_max,
            "http_402_count_mean": self.http_402_count_mean,
            "http_402_count_max": self.http_402_count_max,
            "retry_count_mean": self.retry_count_mean,
            "retry_count_max": self.retry_count_max,
            "upstream_error_count_mean": self.upstream_error_count_mean,
            "upstream_error_count_max": self.upstream_error_count_max,
            "median_request_ms_mean": self.median_request_ms_mean,
            "max_request_ms_max": self.max_request_ms_max,
        }


@dataclass
class Cell:
    model: str
    task_id: str
    condition: str  # 'OFF' | 'ON' | 'ON_REASONING' | 'ON_DISCOVERY' | ...
    resolved: bool
    input_tokens: int
    output_tokens: int
    turns: int
    wall_cost_usd: float
    wall_seconds: float
    delivery: str  # DeliveryVerdict value 'YES'|'CALLED'|'NO', or 'N/A' for OFF cells
    scored: bool  # BENCHMARK INTEGRITY: False => not_scored (never counts as a hit)
    not_scored_reason: str | None = None

    # Phase A fields (D-BENCH-CONTRACT-2026-07) — nullable: when not populated
    # by the current execution path, they remain None (honest UNSUPPORTED/
    # UNAVAILABLE status, not mock proof). Phase B will populate them from
    # harness-trusted producer+extraction evidence.
    pattern_position: str | None = None  # wave_id + position within wave (e.g. "baseline:0")
    run_block: str | None = None  # session block identifier for grouping
    injection_count: int | None = None  # number of injected memories
    injected_block_chars: int | None = None  # injected memory block chars (honest nullable telemetry)
    injected_block_est_tokens: int | None = None  # injected memory block estimated tokens (honest nullable telemetry)
    memory_mode: str | None = None  # OFF|ON from execution (replaces ambiguous off_injection)
    keyword_match_rate: float | None = None
    keyword_matched_count: int | None = None
    keyword_served_count: int | None = None
    vector_only_serve_count: int | None = None
    http_429_count: int | None = None
    http_402_count: int | None = None
    retry_count: int | None = None
    upstream_error_count: int | None = None
    max_request_ms: int | None = None
    median_request_ms: int | None = None
    wall_near_timeout: bool = False
    # DEFERRED: replaced by memory_mode field

    @property
    def total_tokens(self) -> int:
        """Return TOTAL tokens as input+output.

        Summing input+output is mandatory — counting output-only would let the
        efficiency claim lie (injected memory ADDS input tokens; a real net win
        must survive the input cost).
        """

        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "task_id": self.task_id,
            "condition": self.condition,
            "resolved": self.resolved,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "turns": self.turns,
            "wall_cost_usd": self.wall_cost_usd,
            "wall_seconds": self.wall_seconds,
            "delivery": self.delivery,
            "scored": self.scored,
            "not_scored_reason": self.not_scored_reason,
            # Phase A fields (nullable — UNSUPPORTED/UNAVAILABLE when None)
            "pattern_position": self.pattern_position,
            "run_block": self.run_block,
            "injection_count": self.injection_count,
            "injected_block_chars": self.injected_block_chars,
            "injected_block_est_tokens": self.injected_block_est_tokens,
            "memory_mode": self.memory_mode,
            "keyword_match_rate": self.keyword_match_rate,
            "keyword_matched_count": self.keyword_matched_count,
            "keyword_served_count": self.keyword_served_count,
            "vector_only_serve_count": self.vector_only_serve_count,
            "http_429_count": self.http_429_count,
            "http_402_count": self.http_402_count,
            "retry_count": self.retry_count,
            "upstream_error_count": self.upstream_error_count,
            "max_request_ms": self.max_request_ms,
            "median_request_ms": self.median_request_ms,
            "wall_near_timeout": self.wall_near_timeout,
        }


@dataclass
class ModelDiff:
    model: str
    off_pass_rate: float  # fraction resolved among OFF cells (0..1)
    on_pass_rate: float  # fraction resolved among SCORED ON cells only
    capability_lift_pp: float  # (on_pass_rate - off_pass_rate) * 100  (percentage POINTS)
    off_total_tokens: int
    on_total_tokens: int
    total_token_delta_pct: float  # (on - off)/off * 100 ; 0.0 if off==0
    off_cost_usd: float
    on_cost_usd: float
    cost_delta_pct: float  # (on - off)/off * 100 ; 0.0 if off==0
    off_n: int  # OFF cell count
    on_scored_n: int  # SCORED ON cell count
    on_not_scored_n: int  # ON cells excluded by the integrity gate
    off_contention: ContentionArmSummary
    on_contention: ContentionArmSummary
    on_condition: str = "ON"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "on_condition": self.on_condition,
            "off_pass_rate": self.off_pass_rate,
            "on_pass_rate": self.on_pass_rate,
            "capability_lift_pp": self.capability_lift_pp,
            "off_total_tokens": self.off_total_tokens,
            "on_total_tokens": self.on_total_tokens,
            "total_token_delta_pct": self.total_token_delta_pct,
            "off_cost_usd": self.off_cost_usd,
            "on_cost_usd": self.on_cost_usd,
            "cost_delta_pct": self.cost_delta_pct,
            "off_n": self.off_n,
            "on_scored_n": self.on_scored_n,
            "on_not_scored_n": self.on_not_scored_n,
            "off_contention": self.off_contention.to_dict(),
            "on_contention": self.on_contention.to_dict(),
        }


@dataclass
class Manifest:
    config: dict  # RunConfig.to_dict() VERBATIM — reproducibility
    rng_seed: int
    harness_version: str
    created_at: str  # datetime.now(timezone.utc).isoformat()
    split_disclosure: dict | None = None  # from SplitPlan.disclosure(), if provided

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "rng_seed": self.rng_seed,
            "harness_version": self.harness_version,
            "created_at": self.created_at,
            "split_disclosure": self.split_disclosure,
        }


class Scorecard:
    def __init__(self, config: Any, split_disclosure: dict | None = None, *, _now: datetime | None = None):
        # build Manifest from config.to_dict(), config.rng_seed, config.harness_version,
        # created_at = (_now or datetime.now(timezone.utc)).isoformat()  [_now injectable for deterministic tests]
        self.cells: list[Cell] = []
        self.manifest = Manifest(
            config=config.to_dict(),
            rng_seed=config.rng_seed,
            harness_version=config.harness_version,
            created_at=(_now or datetime.now(timezone.utc)).isoformat(),
            split_disclosure=split_disclosure,
        )

    def add_cell(self, cell: Cell) -> None:
        self.cells.append(cell)

    @staticmethod
    def _contention_summary(cells: list[Cell]) -> ContentionArmSummary:
        measured = [
            cell
            for cell in cells
            if cell.http_429_count is not None
            or cell.http_402_count is not None
            or cell.retry_count is not None
            or cell.upstream_error_count is not None
            or cell.max_request_ms is not None
            or cell.median_request_ms is not None
        ]
        http_429_counts = [int(cell.http_429_count) for cell in measured if cell.http_429_count is not None]
        http_402_counts = [int(cell.http_402_count) for cell in measured if cell.http_402_count is not None]
        retry_counts = [int(cell.retry_count) for cell in measured if cell.retry_count is not None]
        upstream_error_counts = [
            int(cell.upstream_error_count) for cell in measured if cell.upstream_error_count is not None
        ]
        median_request_ms = [
            int(cell.median_request_ms) for cell in measured if cell.median_request_ms is not None
        ]
        max_request_ms = [int(cell.max_request_ms) for cell in measured if cell.max_request_ms is not None]

        return ContentionArmSummary(
            cell_count=len(cells),
            measured_cell_count=len(measured),
            http_429_count_mean=_mean_int(http_429_counts),
            http_429_count_max=_max_int(http_429_counts),
            http_402_count_mean=_mean_int(http_402_counts),
            http_402_count_max=_max_int(http_402_counts),
            retry_count_mean=_mean_int(retry_counts),
            retry_count_max=_max_int(retry_counts),
            upstream_error_count_mean=_mean_int(upstream_error_counts),
            upstream_error_count_max=_max_int(upstream_error_counts),
            median_request_ms_mean=_mean_int(median_request_ms),
            max_request_ms_max=_max_int(max_request_ms),
        )

    def model_diffs(self) -> list[ModelDiff]:
        # group cells by model; for each ON-style condition per model:
        #   OFF cells = condition=='OFF'
        #   ON arm cells = condition.startswith('ON') and condition == on_condition
        #   on_scored/on_not_scored split by scored flag
        #   off_pass_rate = mean(resolved) over OFF (0 if none)
        #   on_pass_rate = mean(resolved) over on_scored (0 if none)
        #   token/cost sums over OFF and over on_scored ONLY (never count not_scored)
        #   deltas guarded against divide-by-zero (return 0.0)
        # return sorted by (model, on_condition) for determinism
        diffs: list[ModelDiff] = []
        models = sorted({cell.model for cell in self.cells})

        for model in models:
            model_cells = [cell for cell in self.cells if cell.model == model]
            off_cells = [cell for cell in model_cells if cell.condition == "OFF"]
            on_conditions = sorted(
                {
                    cell.condition
                    for cell in model_cells
                    if cell.condition.startswith("ON")
                }
            )

            off_n = len(off_cells)
            off_pass_rate = (
                sum(1 for cell in off_cells if cell.resolved) / off_n if off_n else 0.0
            )
            off_total_tokens = sum(cell.total_tokens for cell in off_cells)
            off_cost_usd = sum(cell.wall_cost_usd for cell in off_cells)

            for on_condition in on_conditions:
                on_cells = [cell for cell in model_cells if cell.condition == on_condition]
                on_scored = [cell for cell in on_cells if cell.scored]
                on_not_scored = [cell for cell in on_cells if not cell.scored]

                on_scored_n = len(on_scored)

                on_pass_rate = (
                    sum(1 for cell in on_scored if cell.resolved) / on_scored_n if on_scored_n else 0.0
                )

                on_total_tokens = sum(cell.total_tokens for cell in on_scored)
                on_cost_usd = sum(cell.wall_cost_usd for cell in on_scored)

                total_token_delta_pct = (
                    ((on_total_tokens - off_total_tokens) / off_total_tokens) * 100.0
                    if off_total_tokens
                    else 0.0
                )
                cost_delta_pct = (
                    ((on_cost_usd - off_cost_usd) / off_cost_usd) * 100.0
                    if off_cost_usd
                    else 0.0
                )

                diffs.append(
                    ModelDiff(
                        model=model,
                        on_condition=on_condition,
                        off_pass_rate=off_pass_rate,
                        on_pass_rate=on_pass_rate,
                        capability_lift_pp=(on_pass_rate - off_pass_rate) * 100.0,
                        off_total_tokens=off_total_tokens,
                        on_total_tokens=on_total_tokens,
                        total_token_delta_pct=total_token_delta_pct,
                        off_cost_usd=off_cost_usd,
                        on_cost_usd=on_cost_usd,
                        cost_delta_pct=cost_delta_pct,
                        off_n=off_n,
                        on_scored_n=on_scored_n,
                        on_not_scored_n=len(on_not_scored),
                        off_contention=self._contention_summary(off_cells),
                        on_contention=self._contention_summary(on_scored),
                    )
                )

        return diffs

    def to_json(self, *, indent: int = 2) -> str:
        # {"manifest": manifest.to_dict(), "cells": [c.to_dict()...], "model_diffs": [d.to_dict()...]}
        # deterministic: sort_keys=True
        payload = {
            "manifest": self.manifest.to_dict(),
            "cells": [cell.to_dict() for cell in self.cells],
            "model_diffs": [diff.to_dict() for diff in self.model_diffs()],
        }
        return json.dumps(payload, indent=indent, sort_keys=True)
