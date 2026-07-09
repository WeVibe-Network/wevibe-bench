"""OFF/ON ablation driver for the WeVibe benchmark capability ladder.

This module executes paired OFF-then-ON cells for each (model, task) pair and
emits a ``Scorecard`` that is diffed via ``Scorecard.model_diffs()``.

``AgentRunner`` is the seam for real substrates (Aider first; other adapters
built in later tasks). ``MockAgentRunner`` provides a deterministic offline
runner for tests without live model execution.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
import logging
import random
from typing import Any, Callable, Iterable

from wevibe_bench.backends.base import (
    DeliveryVerdict,
    MemoryBackend,
    NeedCard,
    RecalledMemory,
)
from wevibe_bench.backends.none_backend import NoneBackend
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import RunConfig
from wevibe_bench.scorecard import Cell, Scorecard


LOGGER = logging.getLogger("wevibe_bench.runner")


@dataclass
class TaskOutcome:
    resolved: bool
    input_tokens: int
    output_tokens: int
    turns: int
    wall_cost_usd: float
    wall_seconds: float


class AgentRunner(abc.ABC):
    """Seam for substrate adapters (Aider polyglot, SWE-ContextBench — built in a LATER task)."""

    @abc.abstractmethod
    def build_need_card(self, task_id: str) -> NeedCard:
        """Build benchmark need-card from task context.

        MUST mirror the live plugin harvest (MC-1 symmetry): intent/task prose in
        the dense channel, and stack/deps/errors/files in the keyword channel.
        Real adapters harvest these from the live session exactly as
        ``recall-harvest.ts`` does.
        """

    @abc.abstractmethod
    def run_task(self, model: str, task_id: str, injected_memory: list[RecalledMemory]) -> TaskOutcome:
        """Run one task, injecting memory via the live chat system-transform path.

        Adapters must use the same transform path as the live plugin (not a
        bespoke prompt slot) and return resolve + telemetry totals.
        """


NeedCardBuilder = Callable[[str], NeedCard]
OutcomeBuilder = Callable[[str, str, int], TaskOutcome]


class MockAgentRunner(AgentRunner):
    """Deterministic offline runner for tests.

    Optional ``tasks`` specs let tests override need-card fields per task_id.
    Optional callables can override need-card or outcome generation.

    Default outcome model is pure in ``(model, task_id, len(injected_memory))``:
    memory delivery (non-empty injected_memory) increases resolve likelihood and
    reduces turns/tokens/latency/cost relative to OFF.
    """

    def __init__(
        self,
        tasks: dict[str, dict[str, Any]] | None = None,
        *,
        need_card_builder: NeedCardBuilder | None = None,
        outcome_builder: OutcomeBuilder | None = None,
    ) -> None:
        self._tasks = dict(tasks or {})
        self._need_card_builder = need_card_builder
        self._outcome_builder = outcome_builder

    @staticmethod
    def _stable_int(value: str) -> int:
        """Deterministic small hash independent of Python hash randomization."""

        acc = 0
        for char in value:
            acc = (acc * 131 + ord(char)) % 2_147_483_647
        return acc

    def build_need_card(self, task_id: str) -> NeedCard:
        if self._need_card_builder is not None:
            return self._need_card_builder(task_id)

        raw_spec: Any = self._tasks.get(task_id, {})
        spec = raw_spec if isinstance(raw_spec, dict) else {}

        explicit_need = spec.get("need_card")
        if isinstance(explicit_need, NeedCard):
            return explicit_need

        if isinstance(explicit_need, dict):
            need_source = explicit_need
        else:
            need_source = spec

        def _list_of_str(key: str, default: list[str] | None = None) -> list[str]:
            raw = need_source.get(key, default or [])
            if isinstance(raw, (list, tuple)):
                return [str(item) for item in raw]
            return [str(raw)] if raw else []

        return NeedCard(
            intent=str(need_source.get("intent", "implement")),
            task=str(need_source.get("task", f"solve {task_id}")),
            language=(
                str(need_source["language"])
                if need_source.get("language") is not None
                else None
            ),
            stack=_list_of_str("stack", ["python"]),
            frameworks=_list_of_str("frameworks"),
            deps=_list_of_str("deps"),
            error_strings=_list_of_str("error_strings"),
            files=_list_of_str("files"),
            directory=(
                str(need_source["directory"])
                if need_source.get("directory") is not None
                else None
            ),
            project_name=(
                str(need_source["project_name"])
                if need_source.get("project_name") is not None
                else None
            ),
            query=str(need_source.get("query", f"solve {task_id}")),
        )

    def run_task(self, model: str, task_id: str, injected_memory: list[RecalledMemory]) -> TaskOutcome:
        memory_count = len(injected_memory)
        if self._outcome_builder is not None:
            return self._outcome_builder(model, task_id, memory_count)

        # Deterministic synthetic telemetry model:
        # - base signal from stable hash(model|task_id)
        # - non-empty injected memory adds a recall bonus
        # - ON runs are cheaper/faster and more likely to resolve
        baseline = self._stable_int(f"{model}|{task_id}")
        memory_hit = 1 if memory_count > 0 else 0

        resolve_score = ((baseline // 7) % 100) + (25 * memory_hit)
        resolved = resolve_score >= 30

        base_turns = 7 + (baseline % 4)  # 7..10
        turns = max(1, base_turns - (2 if memory_hit else 0))

        base_input = 900 + (baseline % 170)
        base_output = 500 + ((baseline // 5) % 140)
        if memory_hit:
            injected_overhead = 20 * min(memory_count, 5)
            input_tokens = max(150, base_input - 180 + injected_overhead)
            output_tokens = max(120, base_output - 170)
        else:
            input_tokens = base_input
            output_tokens = base_output

        total_tokens = input_tokens + output_tokens
        wall_cost_usd = round(total_tokens * 0.0000025, 6)

        base_seconds = 70.0 + float((baseline // 11) % 25)
        wall_seconds = round(max(1.0, base_seconds - (15.0 if memory_hit else 0.0)), 3)

        return TaskOutcome(
            resolved=resolved,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            turns=turns,
            wall_cost_usd=wall_cost_usd,
            wall_seconds=wall_seconds,
        )


def _cell_from_outcome(
    *,
    model: str,
    task_id: str,
    condition: str,
    delivery: str,
    scored: bool,
    not_scored_reason: str | None,
    outcome: TaskOutcome,
) -> Cell:
    return Cell(
        model=model,
        task_id=task_id,
        condition=condition,
        resolved=outcome.resolved,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        turns=outcome.turns,
        wall_cost_usd=outcome.wall_cost_usd,
        wall_seconds=outcome.wall_seconds,
        delivery=delivery,
        scored=scored,
        not_scored_reason=not_scored_reason,
    )


def _log_cell(cell: Cell) -> None:
    LOGGER.info(
        "ablation_cell model=%s task_id=%s condition=%s resolved=%s total_tokens=%d delivery=%s scored=%s",
        cell.model,
        cell.task_id,
        cell.condition,
        cell.resolved,
        cell.total_tokens,
        cell.delivery,
        cell.scored,
    )


def _session_id(model: str, task_id: str, condition: str, rng: random.Random) -> str:
    nonce = rng.randrange(1_000_000_000)
    return f"bench-{condition.lower()}-{model}-{task_id}-{nonce}"


def run_ablation(
    cfg: RunConfig,
    tasks: Iterable[str],
    agent: AgentRunner,
    split_disclosure: dict[str, Any] | None,
    *,
    on_backend: MemoryBackend | None = None,
    off_backend: MemoryBackend | None = None,
) -> Scorecard:
    """Run deterministic OFF/ON memory ablation across cfg.model_ladder.

    A local RNG seeded from ``cfg.rng_seed`` drives any non-semantic ordering/id
    choices (session ids here), so repeated runs with the same seed are stable.

    Integrity gate choice: when ON delivery verification is required and verdict
    is not YES, we still execute ``run_task(..., [])`` to capture telemetry but
    mark the ON cell ``scored=False`` so model diffs exclude it.
    """

    rng = random.Random(cfg.rng_seed)
    task_ids = list(tasks)

    off = off_backend or NoneBackend()
    on = on_backend or WeVibeBackend(cfg)

    scorecard = Scorecard(cfg, split_disclosure=split_disclosure)

    for model in cfg.model_ladder:
        for task_id in task_ids:
            need = agent.build_need_card(task_id)

            off.prime_session(_session_id(model, task_id, "OFF", rng))
            _ = off.recall(need, cfg)
            off_outcome = agent.run_task(model, task_id, [])
            off_cell = _cell_from_outcome(
                model=model,
                task_id=task_id,
                condition="OFF",
                delivery="N/A",
                scored=True,
                not_scored_reason=None,
                outcome=off_outcome,
            )
            scorecard.add_cell(off_cell)
            _log_cell(off_cell)

            on.prime_session(_session_id(model, task_id, "ON", rng))
            result = on.recall(need, cfg)
            verdict = on.verify_delivery(result)

            if cfg.require_delivery_verification and verdict != DeliveryVerdict.YES:
                on_outcome = agent.run_task(model, task_id, [])
                on_cell = _cell_from_outcome(
                    model=model,
                    task_id=task_id,
                    condition="ON",
                    delivery=verdict.value,
                    scored=False,
                    not_scored_reason=f"delivery={verdict.value}",
                    outcome=on_outcome,
                )
            else:
                on_outcome = agent.run_task(model, task_id, result.memories)
                on_cell = _cell_from_outcome(
                    model=model,
                    task_id=task_id,
                    condition="ON",
                    delivery=verdict.value,
                    scored=True,
                    not_scored_reason=None,
                    outcome=on_outcome,
                )

            scorecard.add_cell(on_cell)
            _log_cell(on_cell)

    return scorecard
