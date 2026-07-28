from __future__ import annotations

import os
from pathlib import Path
import sys
import textwrap

import pytest

from wevibe_bench.adapters.backgammon import BackgammonRunner
from wevibe_bench.adapters.docker_worker import DockerCellConfig, _build_run_argv


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()
TEST_PROXY_BASE_URL = "http://host.docker.internal:8789/api/v1"
TEST_PROXY_TOKEN = "test-ephemeral-token"


def _contains_pair(argv: list[str], left: str, right: str) -> bool:
    for idx, item in enumerate(argv[:-1]):
        if item == left and argv[idx + 1] == right:
            return True
    return False


def _build_memory_on_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, worktree: Path) -> DockerCellConfig:
    fake_home = tmp_path / "fake-home"
    host_wevibe = fake_home / ".wevibe"
    host_wevibe.mkdir(parents=True, exist_ok=True)
    (host_wevibe / "mcp-session-token").write_text("bridge-test-token\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    return DockerCellConfig(
        worktree=worktree,
        memory_mode="on",
        container_name="wevibe-bench-cell-session-db-on",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
        served_memories_host_path=str(tmp_path / "served-memories.json"),
        plugin_config_host_path=str(tmp_path / "plugin-config.json"),
        plugin_state_host_path=str(tmp_path / "plugin-state"),
    )


def test_run_argv_session_db_mount_present_when_configured_memory_mode_off(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-off"
    session_db = tmp_path / "session-db-off"
    cfg = DockerCellConfig(
        worktree=worktree,
        memory_mode="off",
        container_name="wevibe-bench-cell-session-db-off",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
        session_db_host_path=session_db,
    )

    argv = _build_run_argv(config=cfg, worktree=worktree, uid=501, gid=20, memory_mode="off")

    assert _contains_pair(
        argv,
        "-v",
        f"{session_db.resolve()}:/home/worker/.local/share/opencode:rw",
    )
    # The 1777 tmpfs on .local keeps opencode's XDG_STATE_HOME sibling writable:
    # docker pre-creates the bind destination's parents as root:0755 otherwise.
    assert _contains_pair(argv, "--tmpfs", "/home/worker/.local:mode=1777")


def test_run_argv_session_db_mount_present_when_configured_memory_mode_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree-on"
    session_db = tmp_path / "session-db-on"
    cfg = _build_memory_on_cfg(monkeypatch, tmp_path, worktree=worktree)
    cfg.session_db_host_path = session_db

    argv = _build_run_argv(config=cfg, worktree=worktree, uid=501, gid=20, memory_mode="on")

    assert _contains_pair(
        argv,
        "-v",
        f"{session_db.resolve()}:/home/worker/.local/share/opencode:rw",
    )
    assert _contains_pair(argv, "--tmpfs", "/home/worker/.local:mode=1777")


def test_run_argv_session_db_mount_absent_when_none(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-none"
    cfg = DockerCellConfig(
        worktree=worktree,
        memory_mode="off",
        container_name="wevibe-bench-cell-session-db-none",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
        session_db_host_path=None,
    )

    argv = _build_run_argv(config=cfg, worktree=worktree, uid=501, gid=20, memory_mode="off")

    assert not any("/home/worker/.local/share/opencode" in entry for entry in argv)
    assert not any(entry == "/home/worker/.local:mode=1777" for entry in argv)


def test_build_cell_config_wires_session_db_host_path_to_run_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    worktree = run_dir / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    runner = BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="openrouter/anthropic/claude-opus-4.8",
        mock="scaffold",
    )

    cell_config = runner._build_cell_config(worktree=worktree, container_name="cell-session-db")

    expected = run_dir / "session-db"
    assert cell_config.session_db_host_path == expected
    assert expected.is_dir()


def test_run_opencode_emits_session_db_progress_line(tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model="openrouter/anthropic/claude-opus-4.8",
        mock="scaffold",
        progress=progress_lines.append,
    )
    script_path = tmp_path / "fake_opencode.py"
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    script_path.write_text(textwrap.dedent("""
    print('{"type":"step_finish","sessionID":"sess-1","part":{"reason":"stop","tokens":{"input":1,"output":1,"reasoning":0}}}')
    """), encoding="utf-8")

    runner._run_opencode(
        cmd=[sys.executable, str(script_path)],
        worktree=worktree,
        events_path=tmp_path / "events.jsonl",
        env=os.environ.copy(),
        run_label="session-db-progress",
        phase="initial",
        fallback_session_id=None,
    )

    assert any(
        "step=session-db" in line
        and f"path={tmp_path / 'session-db'}" in line
        and "live worker session observable here" in line
        for line in progress_lines
    )
