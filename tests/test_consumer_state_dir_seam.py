from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _restore_environ() -> Any:
    snapshot = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_consumer_state_dir_seam_default_and_override(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = _load_run_cumulative_module()

    run_label = "cumulative-0001-on-openrouter-model-a"
    manifest_path = tmp_path / "runs" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("{}\n", encoding="utf-8")

    run_dir = manifest_path.parent / "sessions" / run_label
    worktree = run_dir / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    monkeypatch.delenv("WEVIBE_BENCH_CONSUMER_STATE_DIR", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_CONSUMER_SCOPED_WEVIBE_DIR", raising=False)

    from wevibe_bench.adapters.backgammon import BackgammonRunner
    from wevibe_bench.adapters.docker_worker import _resolve_host_path

    runner = BackgammonRunner(
        task_dir=tmp_path,
        work_root=run_dir,
        model="openrouter/model-a",
        memory_mode="on",
        mock="scaffold",
    )
    cell_config = runner._build_cell_config(worktree=worktree, container_name="x")

    canonical = module._canonical_consumer_state_dir(run_dir)
    assert _resolve_host_path(cell_config.plugin_state_host_path) == canonical.resolve()
    assert cell_config.plugin_state_container_path == "/work/.wevibe/state"

    run_cfg = module.config.RunConfig()
    bridge_paths = module._bridge_paths(run_cfg, manifest_path, run_dir=run_dir)
    assert bridge_paths.consumer_state_dir == canonical.resolve()

    runtime = module._resolve_bridge_runtime_config(
        argparse.Namespace(
            manifest=str(manifest_path),
            state_dir="",
            manifest_inbox="",
            served_store="",
            run_id=run_label,
        )
    )
    assert runtime.consumer_state_dir == canonical.resolve()

    checkpoint_dir = module._resolve_consumer_gate_state_dir(
        run_cfg.served_memories_host_path,
        run_dir=run_dir,
    )
    assert checkpoint_dir == canonical

    override_dir = tmp_path / "override-state"
    monkeypatch.setenv("WEVIBE_BENCH_CONSUMER_STATE_DIR", str(override_dir))

    bridge_paths_override = module._bridge_paths(run_cfg, manifest_path, run_dir=run_dir)
    assert bridge_paths_override.consumer_state_dir == override_dir.resolve()

    runtime_override = module._resolve_bridge_runtime_config(
        argparse.Namespace(
            manifest=str(manifest_path),
            state_dir="",
            manifest_inbox="",
            served_store="",
            run_id=run_label,
        )
    )
    assert runtime_override.consumer_state_dir == override_dir.resolve()

    checkpoint_override = module._resolve_consumer_gate_state_dir(
        run_cfg.served_memories_host_path,
        run_dir=run_dir,
    )
    assert checkpoint_override == override_dir.resolve()
