from __future__ import annotations

from pathlib import Path

import pytest

from wevibe_bench.adapters.docker_worker import DockerCellConfig, _build_run_argv


TEST_PROXY_BASE_URL = "http://host.docker.internal:8789/api/v1"
TEST_PROXY_TOKEN = "test-ephemeral-token"


def _contains_env_pair(argv: list[str], key: str, value: str) -> bool:
    expected = f"{key}={value}"
    for idx, item in enumerate(argv[:-1]):
        if item == "-e" and argv[idx + 1] == expected:
            return True
    return False


def _build_memory_on_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DockerCellConfig:
    fake_home = tmp_path / "fake-home"
    host_wevibe = fake_home / ".wevibe"
    host_wevibe.mkdir(parents=True, exist_ok=True)
    (host_wevibe / "mcp-session-token").write_text("bridge-test-token\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    return DockerCellConfig(
        worktree=tmp_path / "worktree-on",
        memory_mode="on",
        container_name="wevibe-bench-cell-answerer-on",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
        served_memories_host_path=str(tmp_path / "served-memories.json"),
        plugin_config_host_path=str(tmp_path / "plugin-config.json"),
        plugin_state_host_path=str(tmp_path / "plugin-state"),
    )


def test_on_cell_carries_auto_accept_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _build_memory_on_cfg(monkeypatch, tmp_path)
    argv = _build_run_argv(config=cfg, worktree=cfg.worktree, uid=501, gid=20, memory_mode="on")

    assert _contains_env_pair(argv, "WEVIBE_ANSWERER_POLICY", "auto-accept")


def test_off_cell_carries_off_policy(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-off"
    cfg = DockerCellConfig(
        worktree=worktree,
        memory_mode="off",
        container_name="wevibe-bench-cell-answerer-off",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
    )

    argv = _build_run_argv(config=cfg, worktree=worktree, uid=501, gid=20, memory_mode="off")

    assert _contains_env_pair(argv, "WEVIBE_ANSWERER_POLICY", "off")


def test_explicit_override_wins(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-override"
    cfg = DockerCellConfig(
        worktree=worktree,
        memory_mode="off",
        container_name="wevibe-bench-cell-answerer-override",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
        answerer_policy="auto-accept",
    )

    argv = _build_run_argv(config=cfg, worktree=worktree, uid=501, gid=20, memory_mode="off")

    assert _contains_env_pair(argv, "WEVIBE_ANSWERER_POLICY", "auto-accept")