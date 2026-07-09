from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from wevibe_bench.seeding import SplitViolation, Task, build_split


def test_temporal_cutoff_routes_tasks_before_and_at_after_correctly() -> None:
    cutoff = datetime(2026, 7, 1, 12, 0, 0)
    tasks = [
        Task(task_id="t-before", group="g-before", timestamp=cutoff - timedelta(seconds=1)),
        Task(task_id="t-at", group="g-at", timestamp=cutoff),
        Task(task_id="t-after", group="g-after", timestamp=cutoff + timedelta(seconds=1)),
    ]

    plan = build_split(tasks, cutoff)

    assert [task.task_id for task in plan.seed_tasks] == ["t-before"]
    assert [task.task_id for task in plan.heldout_tasks] == ["t-at", "t-after"]


def test_group_straddle_raises_split_violation() -> None:
    cutoff = datetime(2026, 7, 1, 12, 0, 0)
    tasks = [
        Task(task_id="seed-side", group="repo-a", timestamp=cutoff - timedelta(days=1)),
        Task(task_id="heldout-side", group="repo-a", timestamp=cutoff + timedelta(days=1)),
    ]

    with pytest.raises(SplitViolation) as excinfo:
        build_split(tasks, cutoff)

    message = str(excinfo.value)
    assert "repo-a" in message
    assert "seed-side" in message
    assert "heldout-side" in message


def test_disclosure_returns_expected_counts_groups_stacks_provenance_and_flags() -> None:
    cutoff = datetime(2026, 7, 1, 12, 0, 0)
    tasks = [
        Task(
            task_id="s1",
            group="grp-a",
            timestamp=cutoff - timedelta(days=3),
            stack=("python", "pytest"),
            provenance="jira",
        ),
        Task(
            task_id="s2",
            group="grp-b",
            timestamp=cutoff - timedelta(days=2),
            stack=("python",),
            provenance="linear",
        ),
        Task(
            task_id="h1",
            group="grp-c",
            timestamp=cutoff,
            stack=("go",),
            provenance="jira",
        ),
        Task(
            task_id="h2",
            group="grp-d",
            timestamp=cutoff + timedelta(days=1),
            stack=("rust",),
            provenance="",
        ),
    ]

    plan = build_split(tasks, cutoff)
    disclosure = plan.disclosure()

    assert disclosure == {
        "cutoff": cutoff.isoformat(),
        "seed_count": 2,
        "heldout_count": 2,
        "seed_groups": ["grp-a", "grp-b"],
        "heldout_groups": ["grp-c", "grp-d"],
        "seed_stacks": ["pytest", "python"],
        "heldout_stacks": ["go", "rust"],
        "seed_provenance": ["jira", "linear"],
        "heldout_provenance": ["jira"],
        "grouped_disjoint": True,
        "temporal_split": True,
    }


def test_split_then_preprocess_seed_and_heldout_are_disjoint_by_task_id() -> None:
    cutoff = datetime(2026, 7, 1, 12, 0, 0)
    tasks = [
        Task(task_id="seed-1", group="g1", timestamp=cutoff - timedelta(days=3)),
        Task(task_id="seed-2", group="g2", timestamp=cutoff - timedelta(days=2)),
        Task(task_id="heldout-1", group="g3", timestamp=cutoff),
        Task(task_id="heldout-2", group="g4", timestamp=cutoff + timedelta(days=1)),
    ]

    plan = build_split(tasks, cutoff)
    seed_ids = {task.task_id for task in plan.seed_tasks}
    heldout_ids = {task.task_id for task in plan.heldout_tasks}

    assert seed_ids.isdisjoint(heldout_ids)
    assert seed_ids | heldout_ids == {task.task_id for task in tasks}
