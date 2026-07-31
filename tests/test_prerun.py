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


def test_resolve_off_concurrency_default_clamp_and_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEVIBE_BENCH_OFF_CONCURRENCY", raising=False)
    assert resolve_off_concurrency() == 3
    assert resolve_off_concurrency("0") == 1
    assert resolve_off_concurrency(-7) == 1
    monkeypatch.setenv("WEVIBE_BENCH_OFF_CONCURRENCY", "5")
    assert resolve_off_concurrency() == 5
    monkeypatch.setenv("WEVIBE_BENCH_OFF_CONCURRENCY", "not-int")
    with pytest.raises(ValueError, match="WEVIBE_BENCH_OFF_CONCURRENCY"):
        resolve_off_concurrency()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("openrouter/anthropic/claude", False),
        ("OLLAMA/llama3", True),
        ("http://localhost:1234/v1", True),
        ("http://localhost:11434", True),
        ("lm-studio/qwen", True),
        ("provider/lmstudio-model", True),
    ],
)
def test_is_local_llm_classification(value: str, expected: bool) -> None:
    assert is_local_llm(value) is expected


def test_prerun_pool_executes_with_fresh_runners_and_writes_checkpoints(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    runner_ids: list[int] = []
    runner_refs: list[FakeRunner] = []

    def factory(_session: FakeSession) -> FakeRunner:
        runner = FakeRunner(calls)
        runner_ids.append(id(runner))
        runner_refs.append(runner)
        return runner

    sessions = [FakeSession(1), FakeSession(2), FakeSession(3)]
    results = prerun_off_cells(sessions, factory, tmp_path, concurrency=2)

    assert [item["status"] for item in results] == ["done", "done", "done"]
    assert len(set(runner_ids)) == 3
    for session in sessions:
        checkpoint = load_prerun_checkpoint(tmp_path, session.sequence_index)
        assert checkpoint is not None
        assert checkpoint["status"] == "done"
        assert checkpoint["sequence_index"] == session.sequence_index
        assert checkpoint["progress"] == progress_from_cell_result(_result(session)).to_dict()
        assert cell_result_from_dict(checkpoint["telemetry"]).session_id == f"sess-{session.sequence_index}"


def test_prerun_resume_skips_done_checkpoint(tmp_path: Path) -> None:
    session = FakeSession(7)
    checkpoint = {
        "sequence_index": 7,
        "model": session.model,
        "run_label": "",
        "run_id": "",
        "telemetry": cell_result_to_dict(_result(session)),
        "progress": progress_from_cell_result(_result(session)).to_dict(),
        "status": "done",
        "error": "",
        "started_at": "2026-07-30T00:00:00Z",
        "finished_at": "2026-07-30T00:00:01Z",
    }
    (tmp_path / "prerun-7.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    calls: list[tuple[str, int]] = []

    results = prerun_off_cells([session], lambda _session: FakeRunner(calls), tmp_path, concurrency=1)

    assert results == [checkpoint]
    assert calls == []


def test_prerun_failed_cell_does_not_kill_pool_and_failed_checkpoint_reruns(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    fail = {2}
    sessions = [FakeSession(1), FakeSession(2), FakeSession(3)]

    first = prerun_off_cells(sessions, lambda _session: FakeRunner(calls, fail=fail), tmp_path, concurrency=3)

    assert [item["status"] for item in first] == ["done", "failed", "done"]
    assert "boom-2" in first[1]["error"]
    calls.clear()
    fail.clear()

    second = prerun_off_cells(sessions, lambda _session: FakeRunner(calls, fail=fail), tmp_path, concurrency=3)

    assert [item["status"] for item in second] == ["done", "done", "done"]
    assert calls == [("prepare", 2), ("run", 2)]


def test_local_llm_cells_run_in_serial_lane(tmp_path: Path) -> None:
    active = 0
    max_active = 0
    lock = threading.Lock()

    class LocalRunner(FakeRunner):
        def run_session(self, session: FakeSession) -> BackgammonCellResult:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return _result(session)

    sessions = [
        FakeSession(1, provider_pin="ollama"),
        FakeSession(2, model="lm-studio/qwen"),
        FakeSession(3, provider_pin="http://localhost:1234/v1"),
    ]
    results = prerun_off_cells(sessions, lambda _session: LocalRunner([]), tmp_path, concurrency=3)

    assert [item["status"] for item in results] == ["done", "done", "done"]
    assert max_active == 1


def test_cached_session_runner_returns_cached_telemetry_and_delegates_rest(tmp_path: Path) -> None:
    session = FakeSession(4)
    result = _result(session, cost=0.77)
    checkpoint = {
        "sequence_index": 4,
        "model": session.model,
        "run_label": "",
        "run_id": "",
        "telemetry": cell_result_to_dict(result),
        "progress": progress_from_cell_result(result).to_dict(),
        "status": "done",
        "error": "",
        "started_at": "2026-07-30T00:00:00Z",
        "finished_at": "2026-07-30T00:00:01Z",
    }
    (tmp_path / "prerun-4.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    calls: list[tuple[str, int]] = []
    gate = object()
    wrapped = FakeRunner(calls, gate=gate)
    cached = CachedSessionRunner(wrapped, tmp_path)

    assert cached.prepare_fixture(session) is None
    telemetry = cached.run_session(session)

    assert isinstance(telemetry, BackgammonCellResult)
    assert progress_from_cell_result(telemetry).to_dict() == checkpoint["progress"]
    assert calls == []
    assert cached.extract(session) == {"sequence_index": 4}
    assert cached.index_ready(session) is True
    assert cached.consumer_gate_outcome(session) is gate


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
