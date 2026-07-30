from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wevibe_bench.contention import ContentionCovariates
from wevibe_bench.cumulative.progress import progress_from_cell_result
from wevibe_bench.cumulative.types import PhaseGroup, SessionRecord


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script_contention", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_real_session_runner(module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    repo_root = tmp_path / "repo"
    (repo_root / "tasks" / "backgammon").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(module, "_load_required_text", lambda _path: "strategy-s")
    monkeypatch.setattr(
        module,
        "_load_sxe_helpers",
        lambda _repo_root: (
            lambda **_kwargs: ([], {}, []),
            lambda _run_dir: {},
            lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr(module, "resolve_spend_db_dsn", lambda: "postgresql://unit-test")

    return module.RealSessionRunner(
        task="backgammon",
        org_id="wevibe-org-0",
        runs_dir=tmp_path / "runs",
        repo_root=repo_root,
        proof=SimpleNamespace(),
        hub_client=SimpleNamespace(),
        leader=SimpleNamespace(),
        contributor_rest=SimpleNamespace(last_job_id=None),
        extract_api_key="extract-key",
        extract_api_key_source="env",
        extract_base_url=None,
        extract_num_ctx=None,
        extract_timeout_s=900,
        consumer_decision_manifest=None,
        served_store_host_path=tmp_path / "served-store.json",
    )


def _sample_session() -> SessionRecord:
    return SessionRecord(
        sequence_index=0,
        model="openrouter/tencent/hy3",
        provider_pin="tencent",
        memory_mode="off",
        phase_group=PhaseGroup.OFF_BASELINE.value,
        phase="RUN_SESSION",
    )


class _FakeCellResult(SimpleNamespace):
    verdict = "PASS"
    attempts_to_green = 1
    termination_reason = "ok"
    conformed = True
    input_tokens = 1
    output_tokens = 2
    turns = 3
    delivery = "passed"
    failed_gates: list[str] = []
    problems_final: list[dict[str, Any]] = []
    wall_cost_usd = 0.0


def test_cumulative_contention_covariates_land_in_persisted_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_run_cumulative_module()
    monkeypatch.setenv("WEVIBE_BENCH_RUN_TIMEOUT_S", "100")
    runner = _build_real_session_runner(module, monkeypatch, tmp_path)

    class _FakeSpendMeter:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://unit-test"

        def contention_covariates(self, session_id: str | None, **kwargs: Any) -> ContentionCovariates:
            assert session_id == "session-1"
            assert kwargs == {"retry_count": 2, "wall_seconds": 98.0, "wall_near_timeout": True}
            return ContentionCovariates(
                http_429_count=4,
                http_402_count=1,
                retry_count=2,
                upstream_error_count=3,
                max_request_ms=900,
                median_request_ms=500,
                wall_seconds=98.0,
                wall_near_timeout=True,
            )

    runner._spend_meter = _FakeSpendMeter("postgresql://unit-test")

    class _FakeRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run_cell(self, _run_label: str, _run_dir: Path, task_id: str = "backgammon") -> Any:
            return _FakeCellResult(session_id="session-1", wall_seconds=98.0, zero_tool_resumes=2)

    runner._runner_cls = _FakeRunner
    result = runner.run_session(_sample_session())
    record = _sample_session()
    record.progress = progress_from_cell_result(result).to_dict()

    assert record.to_dict()["progress"] | {} == record.progress
    assert record.progress["http_429_count"] == 4
    assert record.progress["http_402_count"] == 1
    assert record.progress["retry_count"] == 2
    assert record.progress["upstream_error_count"] == 3
    assert record.progress["max_request_ms"] == 900
    assert record.progress["median_request_ms"] == 500
    assert record.progress["wall_seconds"] == 98.0
    assert record.progress["wall_near_timeout"] is True


def test_cumulative_contention_db_failure_falls_back_to_empty_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _load_run_cumulative_module()
    monkeypatch.setenv("WEVIBE_BENCH_RUN_TIMEOUT_S", "100")
    runner = _build_real_session_runner(module, monkeypatch, tmp_path)

    class _FailingSpendMeter:
        def contention_covariates(self, _session_id: str | None, **_kwargs: Any) -> ContentionCovariates:
            raise RuntimeError("db is down")

    class _FakeRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run_cell(self, _run_label: str, _run_dir: Path, task_id: str = "backgammon") -> Any:
            return _FakeCellResult(session_id="session-2", wall_seconds=99.0, zero_tool_resumes=7)

    runner._spend_meter = _FailingSpendMeter()
    runner._runner_cls = _FakeRunner

    with caplog.at_level("ERROR", logger="run_cumulative"):
        result = runner.run_session(_sample_session())

    progress = progress_from_cell_result(result).to_dict()
    assert progress["http_429_count"] == 0
    assert progress["http_402_count"] == 0
    assert progress["retry_count"] == 7
    assert progress["upstream_error_count"] == 0
    assert progress["max_request_ms"] is None
    assert progress["median_request_ms"] is None
    assert progress["wall_seconds"] == 99.0
    assert progress["wall_near_timeout"] is True
    assert "run_cumulative.contention_covariates_failed" in caplog.text
    assert "db is down" in caplog.text
