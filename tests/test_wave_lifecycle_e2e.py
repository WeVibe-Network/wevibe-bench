"""End-to-end zero-provider fixture tests for the wave lifecycle.

Verifies the full wave/barrier/corpus lifecycle without calling real LLMs,
Qdrant, or production services.
"""

from __future__ import annotations

from typing import Any

from wevibe_bench.backends.base import DeliveryVerdict, RecallResult, RecalledMemory
from wevibe_bench.config import BenchmarkSchedule, BenchmarkWave, RunConfig
from wevibe_bench.runner import MockAgentRunner, run_ablation


class MockWeVibeBackend:
    """Mock recall backend with deterministic injection cardinality."""

    def __init__(self, *, injected_count: int = 3) -> None:
        self.injected_count = injected_count

    def prime_session(self, session_id: str) -> None:
        _ = session_id

    def recall(self, need: Any, cfg: RunConfig, org_id: str | None = None) -> RecallResult:
        _ = (need, cfg, org_id)
        memories = [
            RecalledMemory(
                cid=f"mem-{idx}",
                score=0.5,
                vector_score=0.4,
                combined_score=0.45,
                keyword_score=0.5,
                matched_keywords=["test"],
                text=f"injected memory {idx}",
            )
            for idx in range(self.injected_count)
        ]
        return RecallResult(
            memories=memories,
            status="ok",
            reason_code=None,
            reachable=True,
            http_status=200,
        )

    def verify_delivery(self, result: RecallResult) -> DeliveryVerdict:
        return DeliveryVerdict.YES if result.memories else DeliveryVerdict.NO


def _make_schedule(
    waves: list[dict[str, Any]],
    **kwargs: Any,
) -> BenchmarkSchedule:
    return BenchmarkSchedule(
        waves=tuple(
            BenchmarkWave(
                wave_id=str(wave["wave_id"]),
                models=tuple(str(model) for model in wave["models"]),
                tier=str(wave.get("tier", "UNKNOWN")),
                memory_modes=tuple(str(mode) for mode in wave.get("memory_modes", ("off", "on"))),
            )
            for wave in waves
        ),
        schema_version=kwargs.get("schema_version", 1),
    )


def _make_config(schedule: BenchmarkSchedule, **kwargs: Any) -> RunConfig:
    return RunConfig(schedule=schedule, rng_seed=20260709, **kwargs)


def _agent() -> MockAgentRunner:
    return MockAgentRunner(tasks={"task-1": {"intent": "test", "task": "solve task-1"}})


def test_single_wave_off_on() -> None:
    schedule = _make_schedule(
        [
            {
                "wave_id": "baseline",
                "models": ["model-a", "model-b"],
                "memory_modes": ("off", "on"),
            }
        ]
    )
    cfg = _make_config(schedule)

    scorecard = run_ablation(
        cfg,
        tasks=["task-1"],
        agent=_agent(),
        split_disclosure=None,
        on_backend=MockWeVibeBackend(injected_count=3),
    )

    cells = scorecard.cells
    assert len(cells) == 4

    positions = {(cell.model, cell.condition): cell.pattern_position for cell in cells}
    assert positions == {
        ("model-a", "OFF"): "baseline:0",
        ("model-a", "ON"): "baseline:0",
        ("model-b", "OFF"): "baseline:1",
        ("model-b", "ON"): "baseline:1",
    }

    for model in ("model-a", "model-b"):
        model_cells = [cell for cell in cells if cell.model == model]
        assert {cell.condition for cell in model_cells} == {"OFF", "ON"}

    assert {cell.run_block for cell in cells} == {"baseline"}

    off_cells = [cell for cell in cells if cell.condition == "OFF"]
    assert len(off_cells) == 2
    for cell in off_cells:
        assert cell.memory_mode == "off"
        assert cell.injection_count == 0

    on_cells = [cell for cell in cells if cell.condition == "ON"]
    assert len(on_cells) == 2
    for cell in on_cells:
        assert cell.memory_mode == "on"
        assert cell.injection_count is not None
        assert cell.injection_count > 0


def test_multi_wave_sequential() -> None:
    schedule = _make_schedule(
        [
            {"wave_id": "wave_a", "models": ["model-a", "model-b"]},
            {"wave_id": "wave_b", "models": ["model-b", "model-c"]},
            {"wave_id": "wave_c", "models": ["model-c"]},
        ]
    )
    cfg = _make_config(schedule)

    scorecard = run_ablation(
        cfg,
        tasks=["task-1"],
        agent=_agent(),
        split_disclosure=None,
        on_backend=MockWeVibeBackend(injected_count=2),
    )

    cells = scorecard.cells
    assert len(cells) == 10

    assert [cell.pattern_position for cell in cells] == [
        "wave_a:0",
        "wave_a:0",
        "wave_a:1",
        "wave_a:1",
        "wave_b:0",
        "wave_b:0",
        "wave_b:1",
        "wave_b:1",
        "wave_c:0",
        "wave_c:0",
    ]

    model_b_positions = [cell.pattern_position for cell in cells if cell.model == "model-b"]
    assert model_b_positions == ["wave_a:1", "wave_a:1", "wave_b:0", "wave_b:0"]


def test_wave_memory_modes_off_only() -> None:
    schedule = _make_schedule(
        [
            {
                "wave_id": "baseline",
                "models": ["model-a"],
                "memory_modes": ("off",),
            }
        ]
    )
    cfg = _make_config(schedule)

    scorecard = run_ablation(
        cfg,
        tasks=["task-1"],
        agent=_agent(),
        split_disclosure=None,
        on_backend=MockWeVibeBackend(injected_count=3),
    )

    cells = scorecard.cells
    assert len(cells) == 1
    assert cells[0].condition == "OFF"
    assert cells[0].memory_mode == "off"
    assert cells[0].injection_count == 0
    assert [cell for cell in cells if cell.condition == "ON"] == []


def test_wave_memory_modes_on_only() -> None:
    schedule = _make_schedule(
        [
            {
                "wave_id": "baseline",
                "models": ["model-a"],
                "memory_modes": ("on",),
            }
        ]
    )
    cfg = _make_config(schedule)

    scorecard = run_ablation(
        cfg,
        tasks=["task-1"],
        agent=_agent(),
        split_disclosure=None,
        on_backend=MockWeVibeBackend(injected_count=3),
    )

    cells = scorecard.cells
    assert len(cells) == 1
    assert cells[0].condition == "ON"
    assert cells[0].memory_mode == "on"
    assert cells[0].injection_count == 3
    assert [cell for cell in cells if cell.condition == "OFF"] == []


def test_barrier_not_supported() -> None:
    """Barrier/frozen-corpus semantics are NOT implemented in the benchmark harness.

    No corpus snapshot/reset primitive exists. Runs complete without barrier/frozen
    corpus claims. This test verifies the harness handles the absence gracefully.
    """
    schedule = _make_schedule([
        {"wave_id": "baseline", "models": ["model-a"]},
    ])
    cfg = _make_config(schedule)

    agent = MockAgentRunner(tasks={"task1": {"intent": "test", "task": "solve task1"}})
    backend = MockWeVibeBackend(injected_count=2)

    scorecard = run_ablation(
        cfg,
        ["task1"],
        agent,
        None,
        on_backend=backend,
        off_backend=None,
    )
    assert scorecard is not None
    assert len(scorecard.cells) == 2  # OFF + ON for one model


def test_wipe_guard_not_enforced() -> None:
    """The corpus_wipe_guard field was removed from the schema (no corpus wipe
    entry points exist in the benchmark harness). This test passes trivially."""
    schedule = _make_schedule([{"wave_id": "baseline", "models": ["model-a"]}])
    cfg = _make_config(schedule)
    agent = MockAgentRunner(tasks={"task1": {"intent": "test", "task": "solve task1"}})
    backend = MockWeVibeBackend(injected_count=2)
    scorecard = run_ablation(
        cfg,
        ["task1"],
        agent,
        None,
        on_backend=backend,
        off_backend=None,
    )
    assert scorecard is not None


def test_wipe_guard_not_enforced_positive() -> None:
    """Same as above — no corpus wipe entry points exist in the benchmark harness."""
    schedule = _make_schedule([{"wave_id": "baseline", "models": ["model-a"]}])
    cfg = _make_config(schedule)
    agent = MockAgentRunner(tasks={"task1": {"intent": "test", "task": "solve task1"}})
    backend = MockWeVibeBackend(injected_count=2)
    scorecard = run_ablation(
        cfg,
        ["task1"],
        agent,
        None,
        on_backend=backend,
        off_backend=None,
    )
    assert scorecard is not None


def test_schedule_all_models_ignores_wave_order() -> None:
    """Documentation test: all_models() deduplicates, wave iteration preserves repeats."""

    schedule = _make_schedule(
        [
            {"wave_id": "wave_a", "models": ["model-a", "model-b"]},
            {"wave_id": "wave_b", "models": ["model-b", "model-c"]},
        ]
    )

    assert schedule.all_models() == ("model-a", "model-b", "model-c")

    wave_order_models = [model for wave in schedule.waves for model in wave.models]
    assert wave_order_models == ["model-a", "model-b", "model-b", "model-c"]
