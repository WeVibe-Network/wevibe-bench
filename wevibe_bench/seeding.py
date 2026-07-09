"""Seed→held-out split planning for the benchmark harness.

This module enforces:
1) a chronological/temporal split (never random),
2) grouped disjointness (no repo/stack-group straddles across the cutoff), and
3) publication-grade split disclosure metadata.

Seeding execution is a seam: D-5.7 contribution remains sanctioned manual extraction
(no back-door bulk loader), and INV-9 wipe-before-freeze still applies. `build_split`
produces the split plan only; it does not load or ingest memories.

Note: fit τ / vocabulary / thresholds on the SEED half only (split-then-preprocess).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable


class SplitViolation(Exception):
    """Raised when grouped disjointness is violated by a straddling group."""


@dataclass(frozen=True)
class Task:
    task_id: str
    group: str
    timestamp: datetime
    stack: tuple[str, ...] = ()
    provenance: str = ""


@dataclass
class SplitPlan:
    seed_tasks: list[Task]
    heldout_tasks: list[Task]
    cutoff: datetime

    def disclosure(self) -> dict:
        """Return publication-grade split disclosure metadata."""

        return {
            "cutoff": self.cutoff.isoformat(),
            "seed_count": len(self.seed_tasks),
            "heldout_count": len(self.heldout_tasks),
            "seed_groups": sorted({task.group for task in self.seed_tasks}),
            "heldout_groups": sorted({task.group for task in self.heldout_tasks}),
            "seed_stacks": sorted(
                {stack_tag for task in self.seed_tasks for stack_tag in task.stack}
            ),
            "heldout_stacks": sorted(
                {stack_tag for task in self.heldout_tasks for stack_tag in task.stack}
            ),
            "seed_provenance": sorted(
                {task.provenance for task in self.seed_tasks if task.provenance}
            ),
            "heldout_provenance": sorted(
                {task.provenance for task in self.heldout_tasks if task.provenance}
            ),
            "grouped_disjoint": True,
            "temporal_split": True,
        }


def assert_no_straddle(
    tasks: Iterable[Task],
    cutoff: datetime,
    group_key: Callable[[Task], str] | None = None,
) -> set[str]:
    """Return group keys that straddle seed and held-out halves; does not raise."""

    task_list = list(tasks)
    key_fn = group_key or (lambda task: task.group)
    seed_groups = {key_fn(task) for task in task_list if task.timestamp < cutoff}
    heldout_groups = {key_fn(task) for task in task_list if task.timestamp >= cutoff}
    return seed_groups & heldout_groups


def build_split(
    tasks: Iterable[Task],
    cutoff: datetime,
    *,
    group_key: Callable[[Task], str] | None = None,
) -> SplitPlan:
    """Build a deterministic temporal split plan and enforce grouped disjointness."""

    task_list = list(tasks)
    seed_tasks = sorted(
        (task for task in task_list if task.timestamp < cutoff),
        key=lambda task: (task.timestamp, task.task_id),
    )
    heldout_tasks = sorted(
        (task for task in task_list if task.timestamp >= cutoff),
        key=lambda task: (task.timestamp, task.task_id),
    )

    key_fn = group_key or (lambda task: task.group)
    straddling_groups = assert_no_straddle(task_list, cutoff, key_fn)
    if straddling_groups:
        details: list[str] = []
        for group in sorted(straddling_groups):
            seed_ids = sorted(task.task_id for task in seed_tasks if key_fn(task) == group)
            heldout_ids = sorted(
                task.task_id for task in heldout_tasks if key_fn(task) == group
            )
            details.append(f"{group} (seed task_ids={seed_ids}, heldout task_ids={heldout_ids})")
        detail_text = "; ".join(details)
        raise SplitViolation(
            "Grouped disjointness violation: group(s) straddle the temporal cutoff: "
            f"{detail_text}"
        )

    return SplitPlan(seed_tasks=seed_tasks, heldout_tasks=heldout_tasks, cutoff=cutoff)
