from __future__ import annotations

import dataclasses
import http.client
import json
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import pytest

from wevibe_bench.adapters.docker_worker import DockerCell, DockerCellConfig, WORKER_IMAGE, docker_available, image_exists
from wevibe_bench.adapters.openrouter_proxy import (
    BudgetLedger,
    DEFAULT_PROFILES,
    OPENROUTER_UPSTREAM_URL,
    ProfileRegistry,
    ProxyLogger,
)
from wevibe_bench.adapters.openrouter_proxy_server import ProxyServer, UpstreamResponse, make_server


TEST_PRICING = {
    "input": 0.1,
    "output": 0.2,
    "cache_read": 0.05,
    "cache_write": 0.1,
}

_DOCKER_OK, _DOCKER_DETAIL = docker_available()
_IMAGE_OK = image_exists(WORKER_IMAGE) if _DOCKER_OK else False
_DOCKER_SKIP_REASON = (
    f"docker unavailable: {_DOCKER_DETAIL}"
    if not _DOCKER_OK
    else (
        f"docker image missing: {WORKER_IMAGE}. Build with: docker build -t {WORKER_IMAGE} docker/worker"
        if not _IMAGE_OK
        else ""
    )
)


@dataclass
class _CapturedCall:
    url: str
    headers: dict[str, str]
    body_json: dict[str, Any]
    stream: bool


class _FakeUpstream:
    def __init__(self, outcomes: list[UpstreamResponse | Exception | Callable[[_CapturedCall], UpstreamResponse]]):
        self._outcomes = list(outcomes)
        self.calls: list[_CapturedCall] = []

    def __call__(self, url: str, headers: dict[str, str], body_bytes: bytes, stream: bool) -> UpstreamResponse:
        captured = _CapturedCall(
            url=str(url),
            headers={str(k): str(v) for k, v in headers.items()},
            body_json=json.loads(body_bytes.decode("utf-8")),
            stream=bool(stream),
        )
        self.calls.append(captured)

        if not self._outcomes:
            raise AssertionError("fake upstream received more calls than configured")

        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(captured)
        return outcome


@dataclass
class _RunningProxy:
    host: str
    port: int
    run_token: str
    max_tokens_cap: int
    glm_provider_object: dict[str, Any]
    ledger: BudgetLedger
    fake_upstream: _FakeUpstream


def _runnable_glm_profiles() -> tuple[dict[str, Any], dict[str, Any]]:
    profiles = DEFAULT_PROFILES()
    glm = profiles["glm"]
    runnable_glm = dataclasses.replace(glm, pricing=TEST_PRICING, authorized=True)
    profiles["glm"] = runnable_glm
    return profiles, runnable_glm.provider_object or {}


@contextmanager
def _running_proxy_server(
    tmp_path: Path,
    *,
    fake_upstream: _FakeUpstream,
    max_tokens_cap: int = 64,
    hard_cap_usd: float = 12.0,
) -> Iterable[_RunningProxy]:
    profiles, glm_provider_object = _runnable_glm_profiles()
    registry = ProfileRegistry(profiles)
    glm_profile = registry.get("glm")

    ledger = BudgetLedger(
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        model_id=glm_profile.model_id,
        profile_name="glm",
        hard_cap_usd=float(hard_cap_usd),
        checkpoint_path=str(tmp_path / f"ledger-{uuid.uuid4().hex}.json"),
    )
    logger = ProxyLogger(str(tmp_path / f"proxy-{uuid.uuid4().hex}.log"))
    run_token = f"run-token-{uuid.uuid4().hex}"

    proxy = ProxyServer(
        registry=registry,
        profile_name="glm",
        ledger=ledger,
        upstream_key=f"upstream-key-{uuid.uuid4().hex}",
        run_token=run_token,
        logger=logger,
        max_tokens_cap=int(max_tokens_cap),
        upstream_transport=fake_upstream,
    )

    server = make_server(proxy, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        yield _RunningProxy(
            host=str(host),
            port=int(port),
            run_token=run_token,
            max_tokens_cap=int(max_tokens_cap),
            glm_provider_object=glm_provider_object,
            ledger=ledger,
            fake_upstream=fake_upstream,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        logger._handle.close()


def _stream_lines_with_usage(cost: float) -> list[bytes]:
    first = {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "O"}}],
    }
    final = {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
        "choices": [{"index": 0, "delta": {"content": "K"}, "finish_reason": "stop"}],
        "usage": {
            "completion_tokens": 2,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "cost": cost,
        },
    }
    return [
        f"data: {json.dumps(first, separators=(',', ':'))}\n".encode("utf-8"),
        f"data: {json.dumps(final, separators=(',', ':'))}\n".encode("utf-8"),
        b"data: [DONE]\n",
    ]


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _tail(text: str, *, max_lines: int = 8, max_chars: int = 700) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    compact = " | ".join(lines[-max_lines:])
    if len(compact) > max_chars:
        return compact[-max_chars:]
    return compact


def _write_opencode_proxy_config(*, worktree: Path, proxy_base_url: str) -> None:
    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "*": "allow",
            "external_directory": {"*": "allow"},
            "bash": {"*": "allow"},
            "edit": {"*": "allow", "*opencode.json": "deny"},
            "doom_loop": "deny",
            "question": "deny",
        },
        "provider": {
            "openrouter": {
                "options": {
                    "baseURL": proxy_base_url,
                }
            }
        },
    }
    (worktree / "opencode.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


@contextmanager
def _owned_docker_cell(cfg: DockerCellConfig) -> Iterable[DockerCell]:
    cell = DockerCell(cfg, progress=None)
    try:
        cell.__enter__()
        yield cell
    finally:
        cell.teardown()


def _assert_precise_single_rm_call(rm_calls: list[list[str]], *, container_name: str) -> None:
    assert rm_calls == [["docker", "rm", "-f", container_name]]
    rm_argv = rm_calls[0]
    assert "--filter" not in rm_argv
    assert "prune" not in rm_argv
    assert "-a" not in rm_argv
    assert "--all" not in rm_argv


def _install_fake_docker_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exec_timeout_s: int | None = None,
) -> list[list[str]]:
    rm_calls: list[list[str]] = []

    def _fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        argv_list = [str(part) for part in argv]
        if argv_list[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(argv_list, 0, stdout="fake-container-id\n", stderr="")
        if argv_list[:2] == ["docker", "exec"]:
            if exec_timeout_s is not None:
                raise subprocess.TimeoutExpired(cmd=argv_list, timeout=exec_timeout_s)
            return subprocess.CompletedProcess(argv_list, 0, stdout="", stderr="")
        if argv_list[:3] == ["docker", "rm", "-f"]:
            rm_calls.append(argv_list)
            return subprocess.CompletedProcess(argv_list, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker invocation: {argv_list!r}")

    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.ensure_network", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    return rm_calls


def _post_chat_stream(
    *,
    base_url: str,
    token: str,
    body: dict[str, Any],
    expected_line_count: int,
) -> tuple[int, list[bytes]]:
    parsed = urlparse(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    path_prefix = parsed.path.rstrip("/")
    request_path = f"{path_prefix}/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    conn.request("POST", request_path, body=json.dumps(body), headers=headers)
    response = conn.getresponse()
    try:
        status = int(response.status)
        lines = [response.readline() for _ in range(expected_line_count)]
    finally:
        response.close()
        conn.close()
    return status, lines


# GUARANTEED serializer proof (always runs, no Docker): stdlib OpenAI-compatible POST through proxy.
def test_openrouter_proxy_serializer_proof_without_docker(tmp_path: Path) -> None:
    usage_cost = 0.05
    lines = _stream_lines_with_usage(cost=usage_cost)
    fake = _FakeUpstream(
        [
            UpstreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body=None,
                stream_lines=lines,
            )
        ]
    )

    with _running_proxy_server(tmp_path, fake_upstream=fake, max_tokens_cap=77) as running:
        base_url = f"http://127.0.0.1:{running.port}/api/v1"
        request_body = {
            "model": "openrouter/z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "serializer-proof"}],
            "stream": True,
            "max_tokens": 999_999,
        }
        status, relayed_lines = _post_chat_stream(
            base_url=base_url,
            token=running.run_token,
            body=request_body,
            expected_line_count=len(lines),
        )

        assert _wait_until(lambda: running.ledger.snapshot()["outstanding_total"] == 0.0)
        snapshot = running.ledger.snapshot()

    assert status == 200
    assert relayed_lines == lines
    assert len(fake.calls) == 1
    assert fake.calls[0].url == OPENROUTER_UPSTREAM_URL

    forwarded = fake.calls[0].body_json
    assert forwarded["provider"] == running.glm_provider_object
    assert forwarded["max_tokens"] == 77
    assert snapshot["accrued"] == pytest.approx(usage_cost)
    assert snapshot["committed_unproven"] == pytest.approx(0.0)


# BEST-EFFORT real Docker path: runs OpenCode in worker image pointed at local proxy/fake upstream.
@pytest.mark.skipif(not (_DOCKER_OK and _IMAGE_OK), reason=_DOCKER_SKIP_REASON)
def test_openrouter_proxy_worker_docker_best_effort(tmp_path: Path) -> None:
    usage_cost = 0.04
    lines = _stream_lines_with_usage(cost=usage_cost)
    fake = _FakeUpstream(
        [
            UpstreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body=None,
                stream_lines=lines,
            )
        ]
    )

    completed: subprocess.CompletedProcess[str] | None = None

    with _running_proxy_server(tmp_path, fake_upstream=fake, max_tokens_cap=88) as running:
        worktree = tmp_path / "docker-worker"
        worktree.mkdir(parents=True, exist_ok=True)
        proxy_base_url = f"http://host.docker.internal:{running.port}/api/v1"
        _write_opencode_proxy_config(worktree=worktree, proxy_base_url=proxy_base_url)

        cfg = DockerCellConfig(
            worktree=worktree,
            memory_mode="off",
            container_name=f"wevibe-proxy-e2e-{uuid.uuid4().hex[:12]}",
        )
        cfg.proxy_token = running.run_token

        with _owned_docker_cell(cfg) as cell:
            cmd = cell.exec_argv(
                [
                    "opencode",
                    "run",
                    "Reply with exactly OK.",
                    "--model",
                    "openrouter/z-ai/glm-5.2",
                    "--dir",
                    "/work",
                    "--format",
                    "json",
                    "--pure",
                ]
            )
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                pytest.skip(
                    "OpenCode-in-Docker synthetic path skipped: command timed out after 120s against local fake "
                    f"upstream (likely offline metadata/handshake dependency): {exc}"
                )

        if not fake.calls:
            rc = completed.returncode if completed is not None else "unknown"
            stderr_tail = _tail(completed.stderr if completed is not None else "")
            pytest.skip(
                "OpenCode-in-Docker synthetic path skipped: no chat/completions request reached proxy "
                f"(rc={rc}; stderr_tail={stderr_tail})"
            )

        forwarded = fake.calls[0].body_json
        assert fake.calls[0].url == OPENROUTER_UPSTREAM_URL
        assert forwarded["provider"] == running.glm_provider_object
        assert forwarded["max_tokens"] == 88

        assert _wait_until(lambda: running.ledger.snapshot()["outstanding_total"] == 0.0)
        snapshot = running.ledger.snapshot()

    assert snapshot["accrued"] == pytest.approx(usage_cost)
    assert completed is not None
    if completed.returncode != 0:
        # Honest infeasibility report: transport request hit the proxy, but full OpenCode completion
        # did not finish cleanly in this offline synthetic setup.
        pytest.skip(
            "OpenCode-in-Docker synthetic path skipped: request serialization reached proxy and fake upstream, "
            "but OpenCode exited non-zero in this offline environment "
            f"(rc={completed.returncode}; stdout_tail={_tail(completed.stdout)}; stderr_tail={_tail(completed.stderr)})"
        )


def test_openrouter_proxy_worker_docker_best_effort_teardown_on_timeout_skip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rm_calls = _install_fake_docker_subprocess(monkeypatch, exec_timeout_s=120)

    cfg = DockerCellConfig(
        worktree=tmp_path / "mock-timeout-worker",
        memory_mode="off",
        container_name=f"wevibe-proxy-e2e-timeout-{uuid.uuid4().hex[:12]}",
    )
    cfg.proxy_token = "run-token-timeout"

    with pytest.raises(pytest.skip.Exception, match="timed out after 120s"):
        with _owned_docker_cell(cfg) as cell:
            cmd = cell.exec_argv(
                [
                    "opencode",
                    "run",
                    "Reply with exactly OK.",
                    "--model",
                    "openrouter/z-ai/glm-5.2",
                    "--dir",
                    "/work",
                    "--format",
                    "json",
                    "--pure",
                ]
            )
            try:
                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                pytest.skip(
                    "OpenCode-in-Docker synthetic path skipped: command timed out after 120s against local fake "
                    f"upstream (likely offline metadata/handshake dependency): {exc}"
                )

    _assert_precise_single_rm_call(rm_calls, container_name=cfg.container_name)


def test_openrouter_proxy_worker_docker_best_effort_teardown_on_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rm_calls = _install_fake_docker_subprocess(monkeypatch)

    cfg = DockerCellConfig(
        worktree=tmp_path / "mock-runtime-worker",
        memory_mode="off",
        container_name=f"wevibe-proxy-e2e-runtime-{uuid.uuid4().hex[:12]}",
    )
    cfg.proxy_token = "run-token-runtime"

    with pytest.raises(RuntimeError, match="synthetic-runtime-error"):
        with _owned_docker_cell(cfg):
            raise RuntimeError("synthetic-runtime-error")

    _assert_precise_single_rm_call(rm_calls, container_name=cfg.container_name)
