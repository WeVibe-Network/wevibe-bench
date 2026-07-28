from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wevibe_bench.config import RunConfig
from wevibe_bench.cumulative.types import PhaseGroup, SessionRecord


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
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


def test_pacing_knobs_env_unset_uses_defaults_without_optional_runner_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_run_cumulative_module()
    monkeypatch.delenv("WEVIBE_BENCH_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_MAX_STEPS_PER_ATTEMPT", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_RUN_TIMEOUT_S", raising=False)

    runner = _build_real_session_runner(module, monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    class _FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run_cell(self, run_label: str, run_dir: Path, task_id: str = "backgammon") -> Any:
            return type("_R", (), {"session_id": "sid-default", "verdict": "PASS"})()

    runner._runner_cls = _FakeRunner
    runner.run_session(_sample_session())

    assert captured["max_attempts"] == RunConfig().max_attempts
    assert "max_steps_per_attempt" not in captured
    assert "run_timeout_s" not in captured


def test_pacing_knobs_env_set_forwards_runner_constructor_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_run_cumulative_module()
    monkeypatch.setenv("WEVIBE_BENCH_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("WEVIBE_BENCH_MAX_STEPS_PER_ATTEMPT", "83")
    monkeypatch.setenv("WEVIBE_BENCH_RUN_TIMEOUT_S", "1200")

    runner = _build_real_session_runner(module, monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    class _FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run_cell(self, run_label: str, run_dir: Path, task_id: str = "backgammon") -> Any:
            return type("_R", (), {"session_id": "sid-env", "verdict": "PASS"})()

    runner._runner_cls = _FakeRunner
    runner.run_session(_sample_session())

    assert captured["max_attempts"] == 5
    assert captured["max_steps_per_attempt"] == 83
    assert captured["run_timeout_s"] == 1200


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("WEVIBE_BENCH_MAX_ATTEMPTS", "0"),
        ("WEVIBE_BENCH_MAX_ATTEMPTS", "abc"),
        ("WEVIBE_BENCH_MAX_STEPS_PER_ATTEMPT", "0"),
        ("WEVIBE_BENCH_MAX_STEPS_PER_ATTEMPT", "abc"),
        ("WEVIBE_BENCH_RUN_TIMEOUT_S", "0"),
        ("WEVIBE_BENCH_RUN_TIMEOUT_S", "abc"),
    ],
)
def test_pacing_knobs_invalid_env_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_name: str,
    env_value: str,
) -> None:
    module = _load_run_cumulative_module()
    monkeypatch.delenv("WEVIBE_BENCH_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_MAX_STEPS_PER_ATTEMPT", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_RUN_TIMEOUT_S", raising=False)
    monkeypatch.setenv(env_name, env_value)

    with pytest.raises(RuntimeError, match=env_name):
        _build_real_session_runner(module, monkeypatch, tmp_path)
