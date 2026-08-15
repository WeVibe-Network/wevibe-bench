from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from wevibe_bench.config import RunConfig
from wevibe_bench.cumulative.types import PhaseGroup, SessionRecord
from wevibe_bench.lifecycle.lconfig import LifecycleConfig


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_session_runner_forwards_proxy_creds_to_backgammon_runner(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    runner = module.RealSessionRunner.__new__(module.RealSessionRunner)

    captured: dict[str, Any] = {}

    class _FakeRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run_cell(self, run_label: str, run_dir: Path, task_id: str = "backgammon") -> Any:
            return type("_R", (), {"session_id": "sid-1", "verdict": "PASS"})()

    runner._session_states = {}
    runner._runs_dir = tmp_path / "runs"
    runner._task_dir = tmp_path / "task"
    runner._task = "backgammon"
    runner._max_attempts = 3
    runner._proxy_base_url = "http://127.0.0.1:11434/v1"
    runner._proxy_token = "proxy-token-value"
    runner._runner_cls = _FakeRunner
    runner._progress = lambda message: None

    session = SessionRecord(
        sequence_index=0,
        model="openrouter/tencent/hy3",
        provider_pin="tencent",
        memory_mode="off",
        phase_group=PhaseGroup.OFF_BASELINE.value,
        phase="RUN_SESSION",
    )

    result = runner.run_session(session)

    assert captured["proxy_base_url"] == "http://127.0.0.1:11434/v1"
    assert captured["proxy_token"] == "proxy-token-value"
    assert captured["model"] == "openrouter/tencent/hy3"
    assert result.session_id == "sid-1"


def test_lifecycle_config_env_hooks_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEVIBE_BENCH_HUB_URL", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_LEADER_MCP_URL", raising=False)

    default_cfg = LifecycleConfig()
    assert default_cfg.hub_url == "http://127.0.0.1:4440"
    # :4550 is the seed-derived bench leader clone. The default was :4450 (the
    # real host wevibe-mcp, keychain identity, no seed support), so a run
    # without the env override minted its org under the wrong leader.
    assert default_cfg.leader_mcp_url == "http://127.0.0.1:4550"

    monkeypatch.setenv("WEVIBE_BENCH_HUB_URL", "http://127.0.0.1:4449")
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_MCP_URL", "http://127.0.0.1:4550")

    overridden_cfg = LifecycleConfig()
    assert overridden_cfg.hub_url == "http://127.0.0.1:4449"
    assert overridden_cfg.leader_mcp_url == "http://127.0.0.1:4550"


def test_run_config_env_hooks_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEVIBE_BENCH_HUB_URL", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_MCP_RECALL_URL", raising=False)

    default_cfg = RunConfig()
    assert default_cfg.hub_url == "http://127.0.0.1:4440"
    assert default_cfg.mcp_recall_url == "http://127.0.0.1:4450"

    monkeypatch.setenv("WEVIBE_BENCH_HUB_URL", "http://127.0.0.1:4444")
    monkeypatch.setenv("WEVIBE_BENCH_MCP_RECALL_URL", "http://127.0.0.1:4550")

    overridden_cfg = RunConfig()
    assert overridden_cfg.hub_url == "http://127.0.0.1:4444"
    assert overridden_cfg.mcp_recall_url == "http://127.0.0.1:4550"






