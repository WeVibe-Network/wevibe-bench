from __future__ import annotations

import dataclasses
import http.client
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

import wevibe_bench.adapters.openrouter_proxy_server as proxy_server
from wevibe_bench.adapters.openrouter_proxy import (
    BudgetLedger,
    DEFAULT_PROFILES,
    OPENCODE_ZEN_UPSTREAM_URL,
    ORCAROUTER_UPSTREAM_URL,
    PricingGateError,
    ProfileRegistry,
    ProxyLogger,
    UPSTREAM_CHAT_COMPLETIONS_URLS,
    key_fingerprint,
)
from wevibe_bench.adapters.openrouter_proxy_server import (
    ProxyServer,
    UpstreamResponse,
    _assert_expected_upstream_key_fp,
    make_server,
)


CHAT_COMPLETIONS_PATH = "/api/v1/chat/completions"
TEST_PRICING = {
    "input": 0.1,
    "output": 0.2,
    "cache_read": 0.05,
    "cache_write": 0.1,
}


class _MainTestServer:
    def __init__(self) -> None:
        self.closed = False
        self.serve_calls = 0

    def serve_forever(self) -> None:
        self.serve_calls += 1
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


def _write_auth_json(
    tmp_path: Path,
    *,
    key: str = "sk-or-test-main",
    orcarouter_key: str | None = None,
    opencode_key: str | None = None,
) -> Path:
    auth_path = tmp_path / f"auth-{uuid.uuid4().hex}.json"
    payload: dict[str, Any] = {
        "openrouter": {"type": "api", "key": key},
        "orcarouter": {"type": "api", "key": orcarouter_key or key},
    }
    if opencode_key is not None:
        payload["opencode"] = {"type": "api", "key": opencode_key}
    auth_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return auth_path


def _main_argv(
    tmp_path: Path,
    auth_path: Path,
    *,
    model: str = "openrouter/z-ai/glm-5.2",
    profile: str = "glm",
) -> list[str]:
    return [
        "--run-id",
        "run-main-test",
        "--model",
        model,
        "--profile",
        profile,
        "--cap-usd",
        "12",
        "--port",
        "8789",
        "--checkpoint",
        str(tmp_path / "main-ledger.json"),
        "--log",
        str(tmp_path / "main-proxy.log"),
        "--max-output-tokens",
        "256",
        "--token-file",
        str(tmp_path / "main-proxy.token"),
        "--auth-path",
        str(auth_path),
    ]


def _model_selector_for_profile(profile_name: str) -> str:
    profile = DEFAULT_PROFILES()[profile_name]
    model_id = profile.model_id
    if profile.upstream != "openrouter":
        return model_id
    return f"openrouter/{model_id}"


def _valid_orcarouter_payload() -> dict[str, Any]:
    return {
        "pricing_version": "c58e194db3f6a20e7d41b8c9e2f05a17",
        "model_discounts": {},
        "workspace_discount": 1,
        "effective_group_ratio": 1,
        "group_ratio": {"default": 1, "vip": 1},
        "data": [
            {
                "model_name": "openai/gpt-4o-mini",
                "model_ratio": 0.075,
                "completion_ratio": 4.0,
            },
            {
                "model_name": "z-ai/glm-5.2",
                "model_ratio": 0.5,
                "completion_ratio": 3.0,
                "cache_ratio": 0.2,
            },
        ],
    }


def _run_main_with_fake_server(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> ProxyServer:
    captured: dict[str, Any] = {}

    def _fake_make_server(proxy: ProxyServer, host: str = "127.0.0.1", port: int = 0) -> _MainTestServer:
        server = _MainTestServer()
        captured["proxy"] = proxy
        captured["server"] = server
        captured["host"] = host
        captured["port"] = port
        return server

    monkeypatch.setattr(proxy_server, "make_server", _fake_make_server)

    assert proxy_server.main(argv) == 0

    server = captured["server"]
    assert isinstance(server, _MainTestServer)
    assert server.serve_calls == 1
    assert server.closed is True

    proxy = captured["proxy"]
    proxy.logger._handle.close()
    return proxy




































@dataclass
class _CapturedCall:
    url: str
    headers: dict[str, str]
    body_bytes: bytes
    body_json: dict[str, Any]
    stream: bool


class _FakeUpstream:
    def __init__(self, outcomes: list[UpstreamResponse | Exception | Callable[[_CapturedCall], UpstreamResponse]]):
        self._outcomes = list(outcomes)
        self.calls: list[_CapturedCall] = []

    def __call__(self, url: str, headers: dict[str, str], body_bytes: bytes, stream: bool) -> UpstreamResponse:
        body_json = json.loads(body_bytes.decode("utf-8"))
        captured = _CapturedCall(
            url=str(url),
            headers={str(k): str(v) for k, v in headers.items()},
            body_bytes=body_bytes,
            body_json=body_json,
            stream=bool(stream),
        )
        self.calls.append(captured)

        if not self._outcomes:
            raise AssertionError("fake upstream received more calls than configured outcomes")

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
    upstream_key: str
    upstream_url: str
    max_tokens_cap: int
    ledger: BudgetLedger
    log_path: Path
    fake_upstream: _FakeUpstream


def _runnable_glm_profiles() -> dict[str, Any]:
    profiles = DEFAULT_PROFILES()
    glm = profiles["glm"]
    runnable_glm = dataclasses.replace(
        glm,
        pricing=TEST_PRICING,
        authorized=True,
    )
    profiles["glm"] = runnable_glm
    return profiles


@contextmanager
def _running_proxy_server(
    tmp_path: Path,
    *,
    fake_upstream: _FakeUpstream,
    profile_name: str = "glm",
    hard_cap_usd: float = 12.0,
    max_tokens_cap: int = 64,
    profiles_override: dict[str, Any] | None = None,
) -> Iterable[_RunningProxy]:
    profiles = profiles_override if profiles_override is not None else _runnable_glm_profiles()

    registry = ProfileRegistry(profiles)
    profile = registry.get(profile_name)
    upstream_url = UPSTREAM_CHAT_COMPLETIONS_URLS[profile.upstream]

    checkpoint_path = tmp_path / f"ledger-{uuid.uuid4().hex}.json"
    log_path = tmp_path / f"proxy-{uuid.uuid4().hex}.log"
    run_token = f"run-token-{uuid.uuid4().hex}"
    upstream_key = f"upstream-key-{uuid.uuid4().hex}"

    ledger = BudgetLedger(
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        model_id=profile.model_id,
        profile_name=profile_name,
        hard_cap_usd=float(hard_cap_usd),
        checkpoint_path=str(checkpoint_path),
    )
    logger = ProxyLogger(str(log_path))

    proxy = ProxyServer(
        registry=registry,
        profile_name=profile_name,
        ledger=ledger,
        upstream_key=upstream_key,
        run_token=run_token,
        logger=logger,
        max_tokens_cap=int(max_tokens_cap),
        upstream_url=upstream_url,
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
            upstream_key=upstream_key,
            upstream_url=upstream_url,
            max_tokens_cap=int(max_tokens_cap),
            ledger=ledger,
            log_path=log_path,
            fake_upstream=fake_upstream,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        logger._handle.close()


def _glm_request_body(*, stream: bool = False, max_tokens: int = 999_999, prompt: str = "hello") -> dict[str, Any]:
    return {
        "model": "openrouter/z-ai/glm-5.2",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": stream,
    }


def _post_json(
    running: _RunningProxy,
    body: dict[str, Any],
    *,
    token: str | None,
    trace_id: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(running.host, running.port, timeout=5)
    headers = {
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if trace_id is not None:
        headers["X-WeVibe-Trace-Id"] = trace_id

    conn.request(
        "POST",
        CHAT_COMPLETIONS_PATH,
        body=json.dumps(body),
        headers=headers,
    )
    response = conn.getresponse()
    status = int(response.status)
    response_headers = {str(k): str(v) for k, v in response.getheaders()}
    payload = response.read()
    response.close()
    conn.close()
    return status, response_headers, payload


def _open_stream_request(
    running: _RunningProxy,
    body: dict[str, Any],
    *,
    token: str,
    timeout_s: float = 5.0,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    conn = http.client.HTTPConnection(running.host, running.port, timeout=float(timeout_s))
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    conn.request("POST", CHAT_COMPLETIONS_PATH, body=json.dumps(body), headers=headers)
    response = conn.getresponse()
    return conn, response


def _decode_json(payload: bytes) -> dict[str, Any]:
    return json.loads(payload.decode("utf-8"))


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _non_stream_success_response(cost: float, *, model: str = "glm-5.2") -> UpstreamResponse:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "provider": {"slug": "fireworks"},
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {
            "completion_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 2},
            "cost": cost,
        },
    }
    return UpstreamResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        stream_lines=None,
    )


def _non_stream_derived_response(*, model: str = "glm-5.2") -> UpstreamResponse:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "provider": {"slug": "fireworks"},
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {
            "prompt_tokens": 16,
            "completion_tokens": 8,
            "total_tokens": 24,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
    return UpstreamResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        stream_lines=None,
    )


def _non_stream_response_without_usage() -> UpstreamResponse:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "glm-5.2",
        "provider": {"slug": "fireworks"},
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    }
    return UpstreamResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        stream_lines=None,
    )


def _summary_log_line_for_trace(log_path: Path, trace_id: str) -> str:
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if f'trace_id="{trace_id}"' in line and "cached_in_tokens_ub=" in line:
            return line
    raise AssertionError(f"no summary log line found for trace_id={trace_id}")


def _int_field(line: str, field: str) -> int:
    match = re.search(rf"(?:^|\s){re.escape(field)}=(\d+)", line)
    if match is None:
        raise AssertionError(f"field {field} not found in log line: {line}")
    return int(match.group(1))


def _stream_lines_with_usage(cost: float) -> list[bytes]:
    first = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "o"}}],
    }
    second = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {"content": "k"}}],
    }
    final = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "completion_tokens": 6,
            "completion_tokens_details": {"reasoning_tokens": 1},
            "cost": cost,
        },
    }
    return [
        f"data: {json.dumps(first, separators=(',', ':'))}\n".encode("utf-8"),
        f"data: {json.dumps(second, separators=(',', ':'))}\n".encode("utf-8"),
        f"data: {json.dumps(final, separators=(',', ':'))}\n".encode("utf-8"),
        b"data: [DONE]\n",
    ]


def _stream_lines_derived() -> list[bytes]:
    first = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "o"}}],
    }
    second = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {"content": "k"}}],
    }
    final = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 16,
            "completion_tokens": 8,
            "total_tokens": 24,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
    return [
        f"data: {json.dumps(first, separators=(',', ':'))}\n".encode("utf-8"),
        f"data: {json.dumps(second, separators=(',', ':'))}\n".encode("utf-8"),
        f"data: {json.dumps(final, separators=(',', ':'))}\n".encode("utf-8"),
        b"data: [DONE]\n",
    ]


def _tool_call_stream_lines(
    *,
    cost: float,
    tool_call_id: str | None,
    include_done: bool,
) -> list[bytes]:
    first = {
        "id": "chatcmpl-tool-fake",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
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
                            "function": {"name": "bash", "arguments": "{}"},
                        }
                    ],
                    "reasoning_details": [
                        {
                            "type": "reasoning.text",
                            "format": "anthropic-claude-v1",
                            "index": 0,
                            "signature": "c2lnbmF0dXJlLWxvb2tpbmctdGVzdA==",
                            "text": "reasoning text",
                        }
                    ],
                },
            }
        ],
    }
    final = {
        "id": "chatcmpl-tool-fake",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        "usage": {
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 3},
            "cost": cost,
        },
    }

    lines = [
        f"data: {json.dumps(first, separators=(',', ':'))}\n\n".encode("utf-8"),
        f"data: {json.dumps(final, separators=(',', ':'))}\n\n".encode("utf-8"),
    ]
    if include_done:
        lines.append(b"data: [DONE]\n\n")
    return lines


class _DelayedStream:
    def __init__(self, lines: list[bytes], *, delay_s: float = 0.05):
        self._lines = list(lines)
        self._delay_s = float(delay_s)
        self.yielded = 0
        self.drained = threading.Event()

    def __iter__(self) -> Iterable[bytes]:
        try:
            for line in self._lines:
                time.sleep(self._delay_s)
                self.yielded += 1
                yield line
        finally:
            self.drained.set()


def _read_stream_to_eof(response: http.client.HTTPResponse) -> tuple[bytes, float]:
    started = time.monotonic()
    payload = response.read()
    return payload, time.monotonic() - started


def test_auth_requires_run_token_and_allows_correct_token(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.01)])

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        body = _glm_request_body()

        status_missing, _, missing_payload = _post_json(running, body, token=None)
        status_wrong, _, wrong_payload = _post_json(running, body, token="wrong-token")
        status_ok, _, _ = _post_json(running, body, token=running.run_token)

    assert status_missing == 401
    assert _decode_json(missing_payload)["error"]["code"] == "invalid_api_key"
    assert status_wrong == 401
    assert _decode_json(wrong_payload)["error"]["code"] == "invalid_api_key"
    assert status_ok == 200
    assert len(fake.calls) == 1






























def test_logfile_contains_fingerprints_and_status_without_secrets_or_prompt(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.01)])
    prompt_text = "TOP-SECRET-PROMPT-SHOULD-NOT-BE-LOGGED"

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        status, _, _ = _post_json(
            running,
            _glm_request_body(prompt=prompt_text),
            token=running.run_token,
            trace_id="trace-log-001",
        )
        log_text = running.log_path.read_text(encoding="utf-8")

    assert status == 200
    assert key_fingerprint(running.run_token) in log_text
    assert key_fingerprint(running.upstream_key) in log_text
    assert "token_fp=" in log_text
    assert "upstream_key_fp=" in log_text
    assert "status=200" in log_text
    assert prompt_text not in log_text
    assert running.run_token not in log_text
    assert running.upstream_key not in log_text


def test_stream_response_is_close_delimited_and_readable_to_eof(tmp_path: Path) -> None:
    lines = _tool_call_stream_lines(cost=0.04, tool_call_id="call_regression_001", include_done=True)
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

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        conn, response = _open_stream_request(
            running,
            _glm_request_body(stream=True),
            token=running.run_token,
            timeout_s=4.0,
        )
        try:
            assert response.status == 200
            connection_header = response.getheader("Connection")
            body, elapsed_s = _read_stream_to_eof(response)
        finally:
            response.close()
            conn.close()

    assert connection_header == "close"
    assert elapsed_s < 4.0
    assert b"tool_calls" in body
    assert b"data: [DONE]" in body


def test_stream_without_done_marker_still_terminates(tmp_path: Path) -> None:
    lines = _tool_call_stream_lines(cost=0.04, tool_call_id="call_without_done_001", include_done=False)
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

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        conn, response = _open_stream_request(
            running,
            _glm_request_body(stream=True),
            token=running.run_token,
            timeout_s=4.0,
        )
        try:
            assert response.status == 200
            connection_header = response.getheader("Connection")
            body, elapsed_s = _read_stream_to_eof(response)
        finally:
            response.close()
            conn.close()

    assert connection_header == "close"
    assert elapsed_s < 4.0
    assert b"tool_calls" in body
    assert b"data: [DONE]" not in body


def test_stream_with_malformed_tool_call_id_relays_without_hang(tmp_path: Path) -> None:
    lines = _tool_call_stream_lines(cost=0.06, tool_call_id=None, include_done=True)
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

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        conn, response = _open_stream_request(
            running,
            _glm_request_body(stream=True),
            token=running.run_token,
            timeout_s=4.0,
        )
        try:
            assert response.status == 200
            connection_header = response.getheader("Connection")
            body, elapsed_s = _read_stream_to_eof(response)
        finally:
            response.close()
            conn.close()

    assert connection_header == "close"
    assert elapsed_s < 4.0
    assert b'"tool_calls"' in body
    assert b'"id":null' in body
    assert b"data: [DONE]" in body




def _bigpickle_zen_response(*, model: str, cost: float = 0.0) -> UpstreamResponse:
    payload = {
        "id": "chatcmpl-zen",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"completion_tokens": 4, "cost": cost},
    }
    return UpstreamResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        stream_lines=None,
    )


def _runnable_bigpickle_profiles() -> dict[str, Any]:
    profiles = DEFAULT_PROFILES()
    profiles["bigpickle"] = dataclasses.replace(
        profiles["bigpickle"],
        pricing={"input": 0.0, "output": 0.0},
        authorized=True,
    )
    return profiles


def _bigpickle_body() -> dict[str, Any]:
    return {
        "model": "opencode/big-pickle",
        "messages": [{"role": "user", "content": "hello zen"}],
        "max_tokens": 32,
    }














