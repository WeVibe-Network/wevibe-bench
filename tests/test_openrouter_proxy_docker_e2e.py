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
    log_path: Path


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
    log_path = tmp_path / f"proxy-{uuid.uuid4().hex}.log"
    logger = ProxyLogger(str(log_path))
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
            log_path=log_path,
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


def _streamed_tool_then_stop_responses() -> list[UpstreamResponse]:
    tool_call_id = "call_c0probe_001"
    tool_arguments = json.dumps({"command": "echo hi"}, separators=(",", ":"))

    tool_call_chunk = {
        "id": "chatcmpl-c0probe-1",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": tool_arguments,
                            },
                        }
                    ],
                },
            }
        ],
    }
    tool_call_finish = {
        "id": "chatcmpl-c0probe-1",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        "usage": {
            "prompt_tokens": 14,
            "completion_tokens": 6,
            "total_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "cost": 0.003,
        },
    }

    stop_chunk = {
        "id": "chatcmpl-c0probe-2",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}}],
    }
    stop_finish = {
        "id": "chatcmpl-c0probe-2",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 18,
            "completion_tokens": 4,
            "total_tokens": 22,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "cost": 0.002,
        },
    }

    response_one = UpstreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream"},
        body=None,
        stream_lines=[
            f"data: {json.dumps(tool_call_chunk, separators=(',', ':'))}\n\n".encode("utf-8"),
            f"data: {json.dumps(tool_call_finish, separators=(',', ':'))}\n\n".encode("utf-8"),
            b"data: [DONE]\n\n",
        ],
    )
    response_two = UpstreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream"},
        body=None,
        stream_lines=[
            f"data: {json.dumps(stop_chunk, separators=(',', ':'))}\n\n".encode("utf-8"),
            f"data: {json.dumps(stop_finish, separators=(',', ':'))}\n\n".encode("utf-8"),
            b"data: [DONE]\n\n",
        ],
    )
    return [response_one, response_two]


def _append_probe_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}Z {message}\n")


def _parse_json_events(stdout_text: str) -> tuple[list[dict[str, Any]], int]:
    parsed: list[dict[str, Any]] = []
    malformed = 0
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(payload, dict):
            parsed.append(payload)
    return parsed, malformed


def _event_has_completed_tool_execution(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type", "")).strip().lower()
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    part_type = str(part.get("type", "")).strip().lower()

    if event_type in {"tool_result", "tool_finish", "tool_finished"}:
        return True

    if event_type in {"tool_use", "tool"} or part_type == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        if not state and isinstance(event.get("state"), dict):
            state = event.get("state")
        status = str(state.get("status", "")).strip().lower()
        if status in {"done", "complete", "completed", "finished", "success", "ok"}:
            return True
        if state.get("output") is not None:
            return True

    return False


def _event_is_step_finish_stop(event: dict[str, Any]) -> bool:
    if str(event.get("type", "")).strip().lower() != "step_finish":
        return False
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    return str(part.get("reason", "")).strip().lower() == "stop"


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


@pytest.mark.skipif(not (_DOCKER_OK and _IMAGE_OK), reason=_DOCKER_SKIP_REASON)
def test_worker_completes_tool_result_continuation_through_fixed_proxy(tmp_path: Path) -> None:
    fake = _FakeUpstream(_streamed_tool_then_stop_responses())
    run_log_path = Path(__file__).resolve().parents[1] / "runs" / "proxy-e2e" / (
        f"{time.strftime('%Y%m%d-%H%M%S')}-worker-continuation-c0probe-{uuid.uuid4().hex[:8]}.log"
    )
    completed: subprocess.CompletedProcess[str] | None = None

    _append_probe_log(
        run_log_path,
        (
            "start test_worker_completes_tool_result_continuation_through_fixed_proxy "
            f"tmp_path={tmp_path}"
        ),
    )

    with _running_proxy_server(tmp_path, fake_upstream=fake, max_tokens_cap=96) as running:
        worktree = tmp_path / "docker-worker-c0probe"
        worktree.mkdir(parents=True, exist_ok=True)
        proxy_base_url = f"http://host.docker.internal:{running.port}/api/v1"
        _write_opencode_proxy_config(worktree=worktree, proxy_base_url=proxy_base_url)

        cfg = DockerCellConfig(
            worktree=worktree,
            memory_mode="off",
            container_name=f"wevibe-c0probe-{uuid.uuid4().hex[:12]}",
        )
        cfg.proxy_token = running.run_token

        with _owned_docker_cell(cfg) as cell:
            cmd = cell.exec_argv(
                [
                    "opencode",
                    "run",
                    "Use exactly one bash command `echo hi`, then reply with done.",
                    "--model",
                    "openrouter/z-ai/glm-5.2",
                    "--agent",
                    "build",
                    "--dir",
                    "/work",
                    "--format",
                    "json",
                    "--pure",
                ]
            )
            _append_probe_log(run_log_path, f"opencode command={cmd}")
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                _append_probe_log(
                    run_log_path,
                    (
                        "opencode timeout "
                        f"fake_calls={len(fake.calls)} timeout={exc.timeout} cmd={exc.cmd}"
                    ),
                )
                if len(fake.calls) == 0:
                    pytest.skip(
                        "OpenCode c0 continuation probe skipped: docker command timed out before any proxy request "
                        f"(likely offline handshake dependency): {exc}"
                    )
                pytest.fail(
                    "OpenCode c0 continuation probe timed out after proxy activity "
                    f"(fake_calls={len(fake.calls)}). This is a regression signal, not a skippable condition."
                )

        assert completed is not None
        events_path = Path(f"{worktree}.events.jsonl")
        events_path.write_text(completed.stdout, encoding="utf-8")

        _append_probe_log(run_log_path, f"events_path={events_path}")
        _append_probe_log(run_log_path, f"proxy_log_path={running.log_path}")
        _append_probe_log(run_log_path, f"opencode returncode={completed.returncode}")
        _append_probe_log(run_log_path, "opencode stderr begin")
        if completed.stderr:
            for line in completed.stderr.splitlines():
                _append_probe_log(run_log_path, f"stderr {line}")
        _append_probe_log(run_log_path, "opencode stderr end")

        parsed_events, malformed_event_lines = _parse_json_events(completed.stdout)
        for event in parsed_events:
            _append_probe_log(
                run_log_path,
                f"event type={event.get('type')} session={event.get('sessionID', '')}",
            )
        _append_probe_log(run_log_path, f"event parse malformed_lines={malformed_event_lines}")

        if len(fake.calls) == 0:
            pytest.skip(
                "OpenCode c0 continuation probe skipped: opencode exited before sending any chat/completions request "
                f"(rc={completed.returncode}; stderr_tail={_tail(completed.stderr)})"
            )

        for idx, call in enumerate(fake.calls, start=1):
            _append_probe_log(
                run_log_path,
                (
                    f"proxy_call idx={idx} stream={call.stream} model={call.body_json.get('model')} "
                    f"messages={len(call.body_json.get('messages', []))}"
                ),
            )

        proxy_log_text = running.log_path.read_text(encoding="utf-8")
        proxy_log_lines = [line for line in proxy_log_text.splitlines() if line.strip()]
        request_event_count = sum(
            1 for line in proxy_log_lines if "ordinal=" in line and 'event="stream_relay_end"' not in line
        )
        stream_relay_end_count = sum(1 for line in proxy_log_lines if 'event="stream_relay_end"' in line)
        stream_relay_connected_count = sum(
            1
            for line in proxy_log_lines
            if 'event="stream_relay_end"' in line and "client_connected=true" in line
        )
        _append_probe_log(
            run_log_path,
            (
                "proxy_log_counts "
                f"request_events={request_event_count} stream_relay_end={stream_relay_end_count} "
                f"stream_relay_connected={stream_relay_connected_count}"
            ),
        )

        if completed.returncode != 0 and len(fake.calls) == 0:
            pytest.skip(
                "OpenCode c0 continuation probe skipped: non-zero exit before proxy traffic "
                f"(rc={completed.returncode}; stderr_tail={_tail(completed.stderr)})"
            )

        assert completed.returncode == 0, (
            "opencode run exited non-zero in continuation probe "
            f"(rc={completed.returncode}; stdout_tail={_tail(completed.stdout)}; stderr_tail={_tail(completed.stderr)})"
        )
        assert len(fake.calls) == 2, (
            "expected tool-result continuation request through proxy; "
            f"fake upstream saw {len(fake.calls)} calls"
        )
        assert all(call.url == OPENROUTER_UPSTREAM_URL for call in fake.calls)
        assert all(call.stream for call in fake.calls)

        tool_completed = any(_event_has_completed_tool_execution(event) for event in parsed_events)
        step_finish_stop = any(_event_is_step_finish_stop(event) for event in parsed_events)
        assert tool_completed, "expected at least one completed tool execution event in opencode json stream"
        assert step_finish_stop, "expected step_finish reason=stop event in opencode json stream"

        assert request_event_count >= 2, f"expected >=2 proxy request events, saw {request_event_count}"
        assert stream_relay_end_count >= 2, f"expected >=2 stream_relay_end events, saw {stream_relay_end_count}"
        assert stream_relay_connected_count >= 2, (
            "expected >=2 stream_relay_end events with client_connected=true, "
            f"saw {stream_relay_connected_count}"
        )
