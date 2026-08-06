"""AgentRunner seam and TaskOutcome for substrate adapters.

This module defines the two pieces of the live runner contract that adapters
depend on: the ``TaskOutcome`` telemetry dataclass and the abstract
``AgentRunner`` seam real substrates (Backgammon, SWE-ContextBench) implement.

The retired OFF/ON ablation driver (``run_ablation``) and its helpers
(``MockAgentRunner``, ``_cell_from_outcome``, ``_log_cell``, ``_session_id``)
were removed with WO-DEADPATH-1: they had no live entrypoint.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from wevibe_bench.backends.base import NeedCard, RecalledMemory


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