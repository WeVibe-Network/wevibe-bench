from __future__ import annotations

import ast
import contextlib
import inspect
import json
import os
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from wevibe_bench.adapters.backgammon import BackgammonRunner
from wevibe_bench.adapters.docker_worker import (
    DockerCell,
    DockerCellConfig,
    WORKER_IMAGE,
    _build_run_argv,
    docker_available,
    image_exists,
    worker_image_fingerprint,
)
from wevibe_bench.config import RunConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_GOLDEN_PATH = (REPO_ROOT / "tasks" / "backgammon" / "golden").resolve()
HOST_RUNNER_PATH = (REPO_ROOT / "scripts" / "run_backgammon.py").resolve()
RUN_BACKGAMMON_PATH = REPO_ROOT / "scripts" / "run_backgammon.py"

_DOCKER_OK, _DOCKER_DETAIL = docker_available()
REQUIRES_DOCKER = pytest.mark.skipif(
    not _DOCKER_OK,
    reason=f"docker unavailable: {_DOCKER_DETAIL}",
)

TEST_PROXY_BASE_URL = "http://host.docker.internal:8789/api/v1"
TEST_PROXY_TOKEN = "test-ephemeral-token"


def _run(
    argv: list[str],
    *,
    timeout_s: int = 30,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            "command failed "
            f"rc={completed.returncode} argv={argv!r} "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    return completed


def _require_worker_image() -> None:
    assert image_exists(WORKER_IMAGE), (
        "docker worker image missing. Build with: "
        "docker build -t wevibe-bench-worker:v1 docker/worker"
    )


def test_worker_image_fingerprint_returns_id_and_created_from_mocked_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="sha256:unit-test-image\n2026-07-31T01:25:11Z\n",
            stderr="",
        )

    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    fingerprint = worker_image_fingerprint("wevibe-bench-worker:test")

    assert fingerprint is not None
    assert fingerprint.image_id == "sha256:unit-test-image"
    assert fingerprint.created == "2026-07-31T01:25:11Z"
    assert calls == [
        [
            "docker",
            "image",
            "inspect",
            "wevibe-bench-worker:test",
            "--format",
            "{{.Id}}\n{{.Created}}",
        ]
    ]


def test_worker_image_fingerprint_absent_returns_none_and_logs_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="Error: No such image: wevibe-bench-worker:missing",
        )

    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    with caplog.at_level("WARNING", logger="wevibe_bench.adapters.docker_worker"):
        fingerprint = worker_image_fingerprint("wevibe-bench-worker:missing")

    assert fingerprint is None
    assert "docker_worker.image_fingerprint_absent" in caplog.text
    assert "No such image" in caplog.text


def _unique_container_name(prefix: str = "wevibe-bench-cell") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def _started_cell(worktree: Path, *, memory_mode: str) -> DockerCell:
    cell = DockerCell(
        DockerCellConfig(
            worktree=worktree,
            memory_mode=memory_mode,
            container_name=_unique_container_name(),
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=TEST_PROXY_TOKEN,
        )
    )
    try:
        cell.__enter__()
        yield cell
    finally:
        cell.teardown()


def _inspect_mounts(container_name: str) -> list[dict[str, object]]:
    raw = _run(
        ["docker", "inspect", container_name, "--format", "{{json .Mounts}}"],
        timeout_s=30,
    ).stdout.strip()
    mounts = json.loads(raw or "[]")
    assert isinstance(mounts, list), f"docker inspect mount payload must be a list, got: {type(mounts)!r}"
    return mounts


def _assert_mounts_are_only_worktree(mounts: list[dict[str, object]], worktree: Path) -> None:
    assert mounts, "container must expose at least one mount"
    destinations = {str(mount.get("Destination", "")) for mount in mounts}
    assert destinations == {"/work"}

    expected_source = os.path.realpath(str(worktree.resolve()))
    source_paths = {
        os.path.realpath(str(Path(str(mount.get("Source", ""))).resolve()))
        for mount in mounts
        if mount.get("Source")
    }
    assert source_paths == {expected_source}
    assert os.path.realpath(str(HOST_GOLDEN_PATH)) not in source_paths


def _worktree_listing(cell: DockerCell) -> set[str]:
    listing = _run(cell.exec_argv(["find", "/work", "-mindepth", "1", "-print"]))
    out: set[str] = set()
    for line in listing.stdout.splitlines():
        entry = line.strip()
        if not entry:
            continue
        out.add(entry.removeprefix("/work/"))
    return out


def _container_env(cell: DockerCell) -> dict[str, str]:
    env_map: dict[str, str] = {}
    for line in _run(cell.exec_argv(["printenv"]), timeout_s=30).stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env_map[key] = value
    return env_map


def _contains_pair(argv: list[str], left: str, right: str) -> bool:
    for idx, item in enumerate(argv[:-1]):
        if item == left and argv[idx + 1] == right:
            return True
    return False


def test_docker_cell_requires_proxy_token_no_fallback(tmp_path: Path) -> None:
    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "missing-proxy-token-worktree",
            memory_mode="off",
            container_name="wevibe-bench-cell-missing-token",
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=None,
        )
    )

    with pytest.raises(ValueError, match="proxy token required"):
        cell.__enter__()


def test_exec_argv_includes_stdin_forwarding_flag(tmp_path: Path) -> None:
    container_name = "wevibe-bench-cell-exec-argv"
    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "exec-argv-worktree",
            memory_mode="off",
            container_name=container_name,
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=TEST_PROXY_TOKEN,
        )
    )

    cmd = cell.exec_argv(["opencode", "run", "--help"])

    assert cmd[:6] == ["docker", "exec", "-i", "-w", "/work", container_name]
    assert cmd[6:] == ["opencode", "run", "--help"]


def test_docker_cell_forwards_ephemeral_proxy_token_not_host_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_orcarouter_key = "sk-or-host-orcarouter-key"
    captured: dict[str, object] = {}

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[0] == "docker" and argv[1] == "run":
            captured["argv"] = list(argv)
            env_payload = kwargs.get("env", {})
            assert isinstance(env_payload, dict)
            captured["env"] = dict(env_payload)
            return subprocess.CompletedProcess(argv, 0, stdout="fake-container-id\n", stderr="")
        if len(argv) >= 2 and argv[0] == "docker" and argv[1] == "rm":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker invocation: {argv!r}")

    monkeypatch.setenv("ORCAROUTER_API_KEY", host_orcarouter_key)
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.ensure_network", lambda *_: None)
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker._host_uid", lambda: 501)
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker._host_gid", lambda: 20)
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "argv-ephemeral-token-worktree",
            memory_mode="off",
            container_name="wevibe-bench-cell-argv-ephemeral-token",
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=TEST_PROXY_TOKEN,
        )
    )

    try:
        cell.__enter__()
    finally:
        cell.teardown()

    assert "argv" in captured
    assert "env" in captured
    run_argv = captured["argv"]
    run_env = captured["env"]
    assert isinstance(run_argv, list)
    assert isinstance(run_env, dict)

    assert _contains_pair(run_argv, "-e", "ORCAROUTER_API_KEY")
    assert all(not part.startswith("ORCAROUTER_API_KEY=") for part in run_argv)
    assert all(host_orcarouter_key not in part for part in run_argv)

    assert run_env.get("ORCAROUTER_API_KEY") == TEST_PROXY_TOKEN
    assert run_env.get("ORCAROUTER_API_KEY") != host_orcarouter_key


def test_kill_worker_processes_uses_exec_pkill_without_container_rm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    progress_lines: list[str] = []

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    container_name = "wevibe-bench-cell-process-kill"
    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "process-kill-worktree",
            memory_mode="off",
            container_name=container_name,
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=TEST_PROXY_TOKEN,
        ),
        progress=progress_lines.append,
    )

    cell.kill_worker_processes()

    assert calls == [
        [
            "docker",
            "exec",
            "-i",
            "-w",
            "/work",
            container_name,
            "sh",
            "-lc",
            "pkill -9 -f '[o]pencode' || true",
        ]
    ]
    assert not any(len(argv) >= 2 and argv[0] == "docker" and argv[1] == "rm" for argv in calls)
    assert any("worker-process-kill start" in line for line in progress_lines)
    assert any("worker-process-kill done" in line for line in progress_lines)


def test_force_kill_still_tears_down_with_docker_rm_f(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    progress_lines: list[str] = []

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    container_name = "wevibe-bench-cell-force-kill"
    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "force-kill-worktree",
            memory_mode="off",
            container_name=container_name,
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=TEST_PROXY_TOKEN,
        ),
        progress=progress_lines.append,
    )

    cell.force_kill()

    assert calls == [["docker", "rm", "-f", container_name]]
    assert any("docker-rm start" in line for line in progress_lines)
    assert any("docker-rm done" in line for line in progress_lines)


def test_teardown_captures_worker_logs_before_container_rm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    progress_lines: list[str] = []

    worker_logs_dir = tmp_path / "worker-logs"
    container_name = "wevibe-bench-cell-capture-order"

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout='[{"State":{"ExitCode":143}}]\n', stderr="")
        if argv[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="2026-07-22T12:15:00Z worker stdout\n2026-07-22T12:15:01Z worker stderr\n",
                stderr="",
            )
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["docker", "cp"]:
            copied_dir = Path(argv[-1])
            copied_dir.mkdir(parents=True, exist_ok=True)
            (copied_dir / "fake-session.log").write_text("fake-d6-evidence\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["docker", "rm"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker invocation: {argv!r}")

    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "capture-order-worktree",
            memory_mode="off",
            container_name=container_name,
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=TEST_PROXY_TOKEN,
            worker_logs_dir=worker_logs_dir,
        ),
        progress=progress_lines.append,
    )

    cell.teardown()

    assert [tuple(call[:2]) for call in calls] == [
        ("docker", "inspect"),
        ("docker", "logs"),
        ("docker", "exec"),
        ("docker", "cp"),
        ("docker", "rm"),
    ]
    stage_cmd = calls[2]
    assert stage_cmd[-1].startswith("mkdir -p /work/.wevibe-worker-log-export/opencode")
    assert "/home/worker/.local/share/opencode/." in stage_cmd[-1]
    assert (worker_logs_dir / "container-inspect.json").read_text(encoding="utf-8").strip().startswith("[")
    assert "worker stdout" in (worker_logs_dir / "container-docker.log").read_text(encoding="utf-8")
    assert (worker_logs_dir / "opencode" / "fake-session.log").read_text(encoding="utf-8").strip() == "fake-d6-evidence"
    assert any("step=inspect" in line and "status=ok" in line for line in progress_lines)
    assert any("step=docker_logs" in line and "status=ok" in line for line in progress_lines)
    assert any("step=cp" in line and "status=ok" in line for line in progress_lines)


def test_teardown_cp_failure_is_logged_and_rm_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    progress_lines: list[str] = []

    worker_logs_dir = tmp_path / "worker-logs-failing-cp"
    container_name = "wevibe-bench-cell-capture-fail"

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]\n", stderr="")
        if argv[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["docker", "cp"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="source path missing")
        if argv[:2] == ["docker", "rm"]:
            return subprocess.CompletedProcess(argv, 0, stdout="removed", stderr="")
        raise AssertionError(f"unexpected docker invocation: {argv!r}")

    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "capture-failure-worktree",
            memory_mode="off",
            container_name=container_name,
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=TEST_PROXY_TOKEN,
            worker_logs_dir=worker_logs_dir,
        ),
        progress=progress_lines.append,
    )

    cell.teardown()

    assert [tuple(call[:2]) for call in calls] == [
        ("docker", "inspect"),
        ("docker", "logs"),
        ("docker", "exec"),
        ("docker", "cp"),
        ("docker", "rm"),
    ]
    assert any("step=cp" in line and "status=failed" in line and "rc=1" in line for line in progress_lines)
    assert any("docker-rm done" in line for line in progress_lines)


def test_teardown_skips_capture_when_worker_logs_dir_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    progress_lines: list[str] = []

    container_name = "wevibe-bench-cell-capture-skip"

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[:2] == ["docker", "rm"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker invocation: {argv!r}")

    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "capture-skip-worktree",
            memory_mode="off",
            container_name=container_name,
            proxy_base_url=TEST_PROXY_BASE_URL,
            proxy_token=TEST_PROXY_TOKEN,
            worker_logs_dir=None,
        ),
        progress=progress_lines.append,
    )

    cell.teardown()

    assert calls == [["docker", "rm", "-f", container_name]]
    assert any("INFO op=worker_logs.capture step=skip" in line for line in progress_lines)


@REQUIRES_DOCKER
def test_forbidden_mounts_and_oracle_paths_absent(tmp_path: Path) -> None:
    _require_worker_image()

    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "seed.txt").write_text("seed\n", encoding="utf-8")
    (worktree / "nested").mkdir(parents=True, exist_ok=True)
    (worktree / "nested" / "note.txt").write_text("hello\n", encoding="utf-8")

    expected = {"seed.txt", "nested", "nested/note.txt"}

    with _started_cell(worktree, memory_mode="off") as cell:
        _run(cell.exec_argv(["test", "-d", "/work"]))
        assert _worktree_listing(cell) == expected

        _run(cell.exec_argv(["test", "!", "-e", "/work/gates"]))
        _run(cell.exec_argv(["test", "!", "-e", "/work/golden"]))
        _run(cell.exec_argv(["test", "!", "-e", str(HOST_GOLDEN_PATH)]))
        _run(cell.exec_argv(["test", "!", "-e", str(HOST_RUNNER_PATH)]))

        _run(
            cell.exec_argv(
                [
                    "sh",
                    "-lc",
                    "if find / \\( -name report.mjs -o -name run.mjs \\) 2>/dev/null | grep -q .; "
                    "then exit 1; fi",
                ]
            ),
            timeout_s=45,
        )
        _run(
            cell.exec_argv(
                [
                    "sh",
                    "-lc",
                    "if grep -q ' /Users ' /proc/self/mountinfo; then exit 1; fi",
                ]
            )
        )

        mounts = _inspect_mounts(cell.container_name)
        _assert_mounts_are_only_worktree(mounts, worktree)


@REQUIRES_DOCKER
def test_permitted_worktree_bind_and_export_after_teardown(tmp_path: Path) -> None:
    _require_worker_image()

    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "host-seed.txt").write_text("HOST-SEED\n", encoding="utf-8")

    with _started_cell(worktree, memory_mode="off") as cell:
        seen = _run(
            cell.exec_argv(["sh", "-lc", "cat /work/host-seed.txt"]),
            timeout_s=30,
        ).stdout
        assert seen.strip() == "HOST-SEED"

        _run(
            cell.exec_argv(["sh", "-lc", "echo CONTAINER-WRITE > /work/from-container.txt"]),
            timeout_s=30,
        )

    exported = worktree / "from-container.txt"
    assert exported.is_file()
    assert exported.read_text(encoding="utf-8").strip() == "CONTAINER-WRITE"


@REQUIRES_DOCKER
def test_fresh_cell_isolation_between_distinct_worktrees(tmp_path: Path) -> None:
    _require_worker_image()

    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    worktree_a.mkdir(parents=True, exist_ok=True)
    worktree_b.mkdir(parents=True, exist_ok=True)

    with contextlib.ExitStack() as stack:
        cell_a = stack.enter_context(_started_cell(worktree_a, memory_mode="off"))
        _run(
            cell_a.exec_argv(["sh", "-lc", "echo A-MARKER > /work/marker-from-a.txt"]),
            timeout_s=30,
        )

        cell_b = stack.enter_context(_started_cell(worktree_b, memory_mode="off"))
        assert cell_a.container_name != cell_b.container_name

        _run(cell_b.exec_argv(["test", "!", "-e", "/work/marker-from-a.txt"]))


@REQUIRES_DOCKER
def test_memory_mode_on_off_env_wiring_and_no_seed_keystore_corpus_mounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _require_worker_image()

    fake_home = tmp_path / "fake-home"
    host_wevibe = fake_home / ".wevibe"
    host_wevibe.mkdir(parents=True, exist_ok=True)

    token_host_path = host_wevibe / "mcp-session-token"
    token_host_path.write_text("bench-test-token\n", encoding="utf-8")
    token_host_path.chmod(0o600)

    plugin_config_host_path = host_wevibe / "plugin-config.json"
    plugin_config_host_path.write_text(
        json.dumps({"preserve_me": "still-here", "recall_max_injected": 1}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(fake_home))
    primary_cfg = RunConfig()
    served_store_host_path = Path(primary_cfg.served_memories_host_path)
    assert not served_store_host_path.exists()

    worktree_on = tmp_path / "worktree-on"
    worktree_off = tmp_path / "worktree-off"
    worktree_on.mkdir(parents=True, exist_ok=True)
    worktree_off.mkdir(parents=True, exist_ok=True)

    forbidden_env_names = {
        "WEVIBE_IDENTITY_SEED_HEX",
        "WEVIBE_KEYSTORE_PATH",
        "WEVIBE_CORPUS_PATH",
        "WEVIBE_CORPUS_FILE",
        "WEVIBE_SEED",
    }

    with _started_cell(worktree_on, memory_mode="on") as on_cell:
        on_env = _container_env(on_cell)
        assert on_env.get("WEVIBE_MCP_HTTP_URL") == "http://host.docker.internal:4550"
        assert on_env.get("WEVIBE_RECALL_MODE") == primary_cfg.primary_recall_mode
        assert on_env.get("WEVIBE_HUB_URL") == "http://host.docker.internal:4440"
        assert on_env.get("WEVIBE_SERVED_MEMORIES_PATH") == primary_cfg.served_memories_container_path
        for forbidden in forbidden_env_names:
            assert forbidden not in on_env

        mounts_on = _inspect_mounts(on_cell.container_name)
        assert mounts_on, "container must expose at least one mount"
        token_destination = "/home/worker/.wevibe/mcp-session-token"
        plugin_config_destination = "/home/worker/.wevibe/plugin-config.json"
        served_store_destination = primary_cfg.served_memories_container_path
        state_store_destination = "/work/.wevibe/state"
        destinations_on = {str(mount.get("Destination", "")) for mount in mounts_on}
        assert destinations_on == {
            "/work",
            token_destination,
            plugin_config_destination,
            served_store_destination,
            state_store_destination,
        }

        expected_worktree_source = os.path.realpath(str(worktree_on.resolve()))
        expected_token_source = os.path.realpath(str(token_host_path.resolve()))
        expected_plugin_config_source = os.path.realpath(str(plugin_config_host_path.resolve()))
        expected_served_store_source = os.path.realpath(str(served_store_host_path.resolve()))
        expected_state_store_source = os.path.realpath(
            str((Path(str(fake_home)) / ".wevibe" / "state").resolve())
        )
        source_paths_on = {
            os.path.realpath(str(Path(str(mount.get("Source", ""))).resolve()))
            for mount in mounts_on
            if mount.get("Source")
        }
        assert source_paths_on == {
            expected_worktree_source,
            expected_token_source,
            expected_plugin_config_source,
            expected_served_store_source,
            expected_state_store_source,
        }
        assert os.path.realpath(str(HOST_GOLDEN_PATH)) not in source_paths_on

        token_mount = next(
            mount for mount in mounts_on if str(mount.get("Destination", "")) == token_destination
        )
        assert os.path.realpath(str(Path(str(token_mount.get("Source", ""))).resolve())) == expected_token_source
        if "RW" in token_mount:
            assert token_mount["RW"] is False
        if "Mode" in token_mount and token_mount["Mode"] is not None:
            assert "ro" in str(token_mount["Mode"])

        plugin_config_mount = next(
            mount
            for mount in mounts_on
            if str(mount.get("Destination", "")) == plugin_config_destination
        )
        assert (
            os.path.realpath(str(Path(str(plugin_config_mount.get("Source", ""))).resolve()))
            == expected_plugin_config_source
        )
        if "RW" in plugin_config_mount:
            assert plugin_config_mount["RW"] is False
        if "Mode" in plugin_config_mount and plugin_config_mount["Mode"] is not None:
            assert "ro" in str(plugin_config_mount["Mode"])

        served_store_mount = next(
            mount
            for mount in mounts_on
            if str(mount.get("Destination", "")) == served_store_destination
        )
        assert (
            os.path.realpath(str(Path(str(served_store_mount.get("Source", ""))).resolve()))
            == expected_served_store_source
        )
        if "RW" in served_store_mount:
            assert served_store_mount["RW"] is True
        if "Mode" in served_store_mount and served_store_mount["Mode"] is not None:
            assert "ro" not in str(served_store_mount["Mode"])

        state_store_mount = next(
            mount
            for mount in mounts_on
            if str(mount.get("Destination", "")) == state_store_destination
        )
        assert (
            os.path.realpath(str(Path(str(state_store_mount.get("Source", ""))).resolve()))
            == expected_state_store_source
        )
        if "RW" in state_store_mount:
            assert state_store_mount["RW"] is True
        if "Mode" in state_store_mount and state_store_mount["Mode"] is not None:
            assert "ro" not in str(state_store_mount["Mode"])

        assert served_store_host_path.is_file()
        assert json.loads(served_store_host_path.read_text(encoding="utf-8")) == {
            "version": 1,
            "memories": {},
        }
        assert (served_store_host_path.stat().st_mode & 0o777) == 0o600

        state_store_host_path = Path(str(fake_home)) / ".wevibe" / "state"
        assert state_store_host_path.is_dir()
        assert (state_store_host_path.stat().st_mode & 0o777) == 0o700

        plugin_payload = json.loads(plugin_config_host_path.read_text(encoding="utf-8"))
        assert plugin_payload["preserve_me"] == "still-here"
        assert plugin_payload["recall_relevance_floor"] == pytest.approx(
            primary_cfg.primary_recall_relevance_floor
        )
        assert plugin_payload["recall_max_injected"] == primary_cfg.primary_recall_max_injected
        assert (plugin_config_host_path.stat().st_mode & 0o777) == 0o600

        for mount in mounts_on:
            source_text = str(mount.get("Source", "")).lower()
            assert "keystore" not in source_text
            assert "corpus" not in source_text

    with _started_cell(worktree_off, memory_mode="off") as off_cell:
        off_env = _container_env(off_cell)
        assert "WEVIBE_MCP_HTTP_URL" not in off_env
        assert "WEVIBE_RECALL_MODE" not in off_env
        assert "WEVIBE_HUB_URL" not in off_env
        assert "WEVIBE_SERVED_MEMORIES_PATH" not in off_env
        for forbidden in forbidden_env_names:
            assert forbidden not in off_env

        mounts_off = _inspect_mounts(off_cell.container_name)
        _assert_mounts_are_only_worktree(mounts_off, worktree_off)
        for mount in mounts_off:
            source_text = str(mount.get("Source", "")).lower()
            assert "keystore" not in source_text
            assert "corpus" not in source_text


def test_attempts_single_source_of_truth_from_run_config() -> None:
    assert RunConfig().max_attempts == 3
    assert RunConfig(max_attempts=5).to_dict()["max_attempts"] == 5

    tree = ast.parse(RUN_BACKGAMMON_PATH.read_text(encoding="utf-8"))
    runconfig_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "RunConfig"
    ]
    assert runconfig_calls, "run_backgammon.py must construct a RunConfig"

    def _keyword_value(call: ast.Call, name: str) -> ast.AST | None:
        for kw in call.keywords:
            if kw.arg == name:
                return kw.value
        return None

    assert any(
        isinstance((value := _keyword_value(call, "max_attempts")), ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "args"
        and value.attr == "max_attempts"
        for call in runconfig_calls
    ), "RunConfig.max_attempts must source from CLI args"

    runner_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "BackgammonRunner")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "BackgammonRunner")
        )
    ]
    assert runner_calls, "run_backgammon.py must construct BackgammonRunner"
    assert any(
        isinstance((value := _keyword_value(call, "max_attempts")), ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "cfg"
        and value.attr == "max_attempts"
        for call in runner_calls
    ), "BackgammonRunner.max_attempts must be sourced from cfg.max_attempts"


@REQUIRES_DOCKER
def test_image_and_run_argv_do_not_embed_secrets(tmp_path: Path) -> None:
    _require_worker_image()

    inspect_env = _run(
        ["docker", "image", "inspect", WORKER_IMAGE, "--format", "{{json .Config.Env}}"],
        timeout_s=30,
    )
    env_entries = json.loads(inspect_env.stdout.strip() or "[]")
    assert isinstance(env_entries, list)

    for item in env_entries:
        assert isinstance(item, str)
        key, _, value = item.partition("=")
        key_upper = key.upper()
        if "ORCAROUTER" in key_upper or "SEED" in key_upper or "KEYSTORE" in key_upper:
            assert value == "", f"sensitive key must not carry a baked value: {key}"

    history_out = _run(["docker", "history", "--no-trunc", WORKER_IMAGE], timeout_s=30).stdout
    red_flags = (
        "ORCAROUTER_API_KEY=",
        "WEVIBE_IDENTITY_SEED_HEX=",
        "WEVIBE_KEYSTORE_PATH=",
        "WEVIBE_SEED=",
        "--build-arg ORCAROUTER_API_KEY",
    )
    for marker in red_flags:
        assert marker not in history_out

    for env_name in ("ORCAROUTER_API_KEY", "WEVIBE_IDENTITY_SEED_HEX", "WEVIBE_KEYSTORE_PATH"):
        value = os.environ.get(env_name, "")
        if value:
            assert value not in inspect_env.stdout
            assert value not in history_out

    worktree = tmp_path / "argv-worktree"
    cfg = DockerCellConfig(
        worktree=worktree,
        memory_mode="off",
        container_name="wevibe-bench-cell-argv-check",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
    )
    run_argv = _build_run_argv(config=cfg, worktree=worktree, uid=1000, gid=1000, memory_mode="off")
    assert _contains_pair(run_argv, "-e", "ORCAROUTER_API_KEY")
    assert all(not part.startswith("ORCAROUTER_API_KEY=") for part in run_argv)


def test_gate_oracle_scoring_is_host_only_structurally() -> None:
    gate_source = inspect.getsource(BackgammonRunner._run_gate_report)
    gate_tree = ast.parse(textwrap.dedent(gate_source))

    gate_cmd_assignments = [
        node
        for node in ast.walk(gate_tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "gate_cmd" for target in node.targets)
    ]
    assert gate_cmd_assignments, "_run_gate_report must define gate_cmd"
    gate_cmd_expr = gate_cmd_assignments[0].value
    assert isinstance(gate_cmd_expr, ast.List)
    assert len(gate_cmd_expr.elts) >= 2
    assert isinstance(gate_cmd_expr.elts[0], ast.Constant) and gate_cmd_expr.elts[0].value == "node"
    assert isinstance(gate_cmd_expr.elts[1], ast.Constant) and gate_cmd_expr.elts[1].value == "report.mjs"

    subprocess_calls = [
        node
        for node in ast.walk(gate_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]
    assert subprocess_calls, "_run_gate_report must execute host subprocess.run"

    cwd_values = [kw.value for kw in subprocess_calls[0].keywords if kw.arg == "cwd"]
    assert cwd_values, "_run_gate_report subprocess.run must set cwd"
    cwd_repr = ast.unparse(cwd_values[0])
    assert "self.task_dir / 'gates'" in cwd_repr or 'self.task_dir / "gates"' in cwd_repr

    run_cell_source = inspect.getsource(BackgammonRunner._run_cell_impl)
    run_cell_tree = ast.parse(textwrap.dedent(run_cell_source))
    build_cell_config_calls = [
        node
        for node in ast.walk(run_cell_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr == "_build_cell_config"
    ]
    assert build_cell_config_calls, "_run_cell_impl must call self._build_cell_config"

    build_cell_source = inspect.getsource(BackgammonRunner._build_cell_config)
    build_cell_tree = ast.parse(textwrap.dedent(build_cell_source))
    docker_cfg_calls = [
        node
        for node in ast.walk(build_cell_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DockerCellConfig"
    ]
    assert docker_cfg_calls, "_build_cell_config must construct DockerCellConfig"
    docker_cfg_kw = {kw.arg for kw in docker_cfg_calls[0].keywords}
    assert docker_cfg_kw == {"worktree", "memory_mode", "container_name"}
    assert "docker exec" not in gate_source.lower()


def test_run_argv_makes_home_and_tmp_writable_tmpfs_mode_1777() -> None:
    # Pure-unit (no docker): regression for the TASK-4 fix #1. The container runs as the
    # host non-root uid via --user, and opencode writes $HOME/.local/share; a default tmpfs
    # is root:root 0755 -> EACCES. Both HOME and /tmp tmpfs must carry mode=1777.
    cfg = DockerCellConfig(
        worktree=Path("/tmp/argv-mode-check"),
        memory_mode="off",
        container_name="wevibe-bench-cell-mode-check",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
    )
    argv = _build_run_argv(config=cfg, worktree=cfg.worktree, uid=501, gid=20, memory_mode="off")
    assert _contains_pair(argv, "--tmpfs", "/tmp:mode=1777")
    assert _contains_pair(argv, "--tmpfs", f"{cfg.home_dir}:mode=1777")
    # --read-only isolation must remain (writable tmpfs, not a writable root fs).
    assert "--read-only" in argv
    # A bare (non-writable) tmpfs for HOME must NOT be present.
    assert not _contains_pair(argv, "--tmpfs", cfg.home_dir)


def test_run_argv_memory_mode_on_requires_host_token_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))

    cfg = DockerCellConfig(
        worktree=tmp_path / "argv-memory-on-check",
        memory_mode="on",
        container_name="wevibe-bench-cell-memory-on-check",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
    )

    with pytest.raises(FileNotFoundError, match=r"~/.wevibe/mcp-session-token"):
        _build_run_argv(config=cfg, worktree=cfg.worktree, uid=501, gid=20, memory_mode="on")


def test_run_argv_redirects_xdg_state_into_writable_home_and_keeps_baked_config() -> None:
    # Pure-unit (no docker): regression for the TASK-4 fix #2. The image pins
    # XDG_CONFIG_HOME/OPENCODE_CONFIG_DIR under /etc/xdg on the --read-only root, so opencode
    # cannot write its config-dir state. The adapter must redirect XDG + opencode state dirs
    # into the writable HOME tmpfs while still loading the baked config via OPENCODE_CONFIG.
    cfg = DockerCellConfig(
        worktree=Path("/tmp/argv-xdg-check"),
        memory_mode="off",
        container_name="wevibe-bench-cell-xdg-check",
        proxy_base_url=TEST_PROXY_BASE_URL,
        proxy_token=TEST_PROXY_TOKEN,
    )
    argv = _build_run_argv(config=cfg, worktree=cfg.worktree, uid=501, gid=20, memory_mode="off")
    home = cfg.home_dir
    assert _contains_pair(argv, "-e", f"XDG_CONFIG_HOME={home}/.config")
    assert _contains_pair(argv, "-e", f"XDG_DATA_HOME={home}/.local/share")
    assert _contains_pair(argv, "-e", f"XDG_CACHE_HOME={home}/.cache")
    assert _contains_pair(argv, "-e", f"OPENCODE_CONFIG_DIR={home}/.config/opencode")
    # Baked config file stays readable/loaded from the read-only image path.
    assert _contains_pair(argv, "-e", "OPENCODE_CONFIG=/etc/xdg/opencode/opencode.json")
    # HOME still points at the writable tmpfs.
    assert _contains_pair(argv, "-e", f"HOME={home}")
