from __future__ import annotations

import json
from pathlib import Path

import pytest

from wevibe_bench.adapters.docker_worker import DockerCellConfig, _build_run_argv


TEST_PROXY_BASE_URL = "http://host.docker.internal:8789/api/v1"
TEST_PROXY_TOKEN = "test-ephemeral-token"


def _contains_pair(argv: list[str], left: str, right: str) -> bool:
    for idx, item in enumerate(argv[:-1]):
        if item == left and argv[idx + 1] == right:
            return True
    return False


def test_run_argv_memory_mode_on_mounts_plugin_state_rw_and_preserves_existing_mounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "fake-home"
    host_wevibe = fake_home / ".wevibe"
    host_wevibe.mkdir(parents=True, exist_ok=True)

    token_host_path = host_wevibe / "mcp-session-token"
    token_host_path.write_text("bridge-test-token\n", encoding="utf-8")
    token_host_path.chmod(0o600)

    plugin_config_host_path = host_wevibe / "plugin-config.json"
    plugin_config_host_path.write_text(json.dumps({"preserve": "still-here"}), encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))

    worktree = tmp_path / "worktree-on"
    worktree.mkdir(parents=True, exist_ok=True)

    served_memories_host_path = tmp_path / "served-memories.json"
    plugin_state_host_path = tmp_path / "plugin-state"

    cfg = DockerCellConfig(
        worktree=worktree,
        memory_mode="on",
        container_name="wevibe-bench-cell-bridge-mount-on",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
        served_memories_host_path=str(served_memories_host_path),
        plugin_config_host_path=str(plugin_config_host_path),
        plugin_state_host_path=str(plugin_state_host_path),
    )

    argv = _build_run_argv(config=cfg, worktree=worktree, uid=501, gid=20, memory_mode="on")

    resolved_plugin_state = plugin_state_host_path.resolve()
    assert _contains_pair(argv, "-v", f"{resolved_plugin_state}:/work/.wevibe/state:rw")
    assert resolved_plugin_state.is_dir()
    assert (resolved_plugin_state.stat().st_mode & 0o777) == 0o700

    assert _contains_pair(
        argv,
        "-v",
        f"{token_host_path.resolve()}:/home/worker/.wevibe/mcp-session-token:ro",
    )
    assert _contains_pair(
        argv,
        "-v",
        f"{plugin_config_host_path.resolve()}:/home/worker/.wevibe/plugin-config.json:ro",
    )
    assert _contains_pair(
        argv,
        "-v",
        f"{served_memories_host_path.resolve()}:{cfg.served_memories_container_path}:rw",
    )


def test_run_argv_memory_mode_off_has_no_plugin_state_mount(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-off"
    cfg = DockerCellConfig(
        worktree=worktree,
        memory_mode="off",
        container_name="wevibe-bench-cell-bridge-mount-off",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
        plugin_state_host_path=str(tmp_path / "plugin-state-off"),
    )

    argv = _build_run_argv(config=cfg, worktree=worktree, uid=501, gid=20, memory_mode="off")

    assert not any("/work/.wevibe/state" in entry for entry in argv)
