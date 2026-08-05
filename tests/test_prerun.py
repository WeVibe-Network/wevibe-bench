from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from wevibe_bench.adapters.backgammon import BackgammonCellResult
from wevibe_bench.cumulative.prerun import (
    CachedSessionRunner,
    cell_result_from_dict,
    cell_result_to_dict,
    is_local_llm,
    load_prerun_checkpoint,
    prerun_off_cells,
    resolve_off_concurrency,
)
from wevibe_bench.cumulative.progress import progress_from_cell_result


@dataclass(frozen=True)
class FakeSession:
    sequence_index: int
    model: str = "openrouter/model-a"
    provider_pin: str = "openrouter"
    memory_mode: str = "off"
    phase_group: str = "off_baseline"
    run_label: str = ""
    run_id: str = ""


def _result(session: FakeSession, *, cost: float = 0.25) -> BackgammonCellResult:
    return BackgammonCellResult(
        verdict="PASS",
        attempts_to_green=1,
        termination_reason="green",
        conformed=True,
        input_tokens=10 + session.sequence_index,
        output_tokens=20,
        turns=3,
        wall_seconds=0.5,
        delivery="ok",
        failed_gates=[],
        problems_final=[],
        attempt_reports=[],
        worktree=f"/tmp/worktree-{session.sequence_index}",
        session_id=f"sess-{session.sequence_index}",
        memory_mode=session.memory_mode,
        model=session.model,
        wall_cost_usd=cost,
        problems_before=2,
        injected_count=1,
        tool_calls=4,
        test_invocations=1,
        agentic_cycles=2,
    )


class FakeRunner:
    def __init__(
        self,
        calls: list[tuple[str, int]],
        *,
        fail: set[int] | None = None,
        gate: object | None = None,
    ) -> None:
        self.calls = calls
        self.fail = fail or set()
        self.gate = gate

    def prepare_fixture(self, session: FakeSession) -> None:
        self.calls.append(("prepare", session.sequence_index))

    def run_session(self, session: FakeSession) -> BackgammonCellResult:
        self.calls.append(("run", session.sequence_index))
        if session.sequence_index in self.fail:
            raise RuntimeError(f"boom-{session.sequence_index}")
        return _result(session)

    def extract(self, session: FakeSession) -> dict[str, Any]:
        return {"sequence_index": session.sequence_index}

    def index_ready(self, session: FakeSession) -> bool:
        return True

    def consumer_gate_outcome(self, session: FakeSession) -> object | None:
        return self.gate
















def test_cell_result_to_dict_json_serializable_with_nested_contention() -> None:
    """Regression (2026-07-31): a nested ContentionCovariates dataclass crashed
    the prerun checkpoint write (TypeError: not JSON serializable), losing the
    failed cell's checkpoint and triggering a silent re-run."""
    from wevibe_bench.contention import ContentionCovariates

    session = FakeSession(7)
    result = _result(session)
    object.__setattr__(
        result,
        "contention",
        ContentionCovariates.empty(retry_count=2, wall_seconds=12.5, wall_near_timeout=False),
    )

    rendered = cell_result_to_dict(result)
    blob = json.dumps(rendered, sort_keys=True)
    assert json.loads(blob)["contention"]["retry_count"] == 2
