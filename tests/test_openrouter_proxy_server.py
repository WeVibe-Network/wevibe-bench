from __future__ import annotations

import dataclasses
import http.client
import json
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
    OPENROUTER_UPSTREAM_URL,
    ProfileRegistry,
    ProxyLogger,
    key_fingerprint,
)
from wevibe_bench.adapters.openrouter_proxy_server import ProxyServer, UpstreamResponse, make_server


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


def _write_auth_json(tmp_path: Path, *, key: str = "sk-or-test-main") -> Path:
    auth_path = tmp_path / f"auth-{uuid.uuid4().hex}.json"
    auth_path.write_text(
        json.dumps({"openrouter": {"type": "api", "key": key}}),
        encoding="utf-8",
    )
    return auth_path


def _main_argv(tmp_path: Path, auth_path: Path) -> list[str]:
    return [
        "--run-id",
        "run-main-test",
        "--model",
        "openrouter/z-ai/glm-5.2",
        "--profile",
        "glm",
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


def test_main_sources_upstream_key_from_auth_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    auth_path = _write_auth_json(tmp_path, key="sk-or-test-main-valid")
    proxy = _run_main_with_fake_server(monkeypatch, _main_argv(tmp_path, auth_path))

    assert proxy.upstream_key == "sk-or-test-main-valid"


def test_main_errors_when_auth_json_path_missing(tmp_path: Path) -> None:
    missing_auth = tmp_path / "missing-auth.json"

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(_main_argv(tmp_path, missing_auth))

    assert excinfo.value.code == 2


def test_main_authorize_with_live_pricing_unblocks_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    auth_path = _write_auth_json(tmp_path)
    argv = _main_argv(tmp_path, auth_path) + [
        "--authorize",
        "--pricing-input",
        "1.0",
        "--pricing-output",
        "3.0",
        "--pricing-cache-read",
        "0.2",
        "--pricing-cache-write",
        "1.3",
    ]
    proxy = _run_main_with_fake_server(monkeypatch, argv)

    selected_profile = proxy.registry.get("glm")
    assert selected_profile.runnable_reason() is None
    assert selected_profile.authorized is True
    assert selected_profile.pricing == {
        "input": pytest.approx(1.0),
        "output": pytest.approx(3.0),
        "cache_read": pytest.approx(0.2),
        "cache_write": pytest.approx(1.3),
    }


def test_main_without_authorize_keeps_profile_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    auth_path = _write_auth_json(tmp_path)
    proxy = _run_main_with_fake_server(monkeypatch, _main_argv(tmp_path, auth_path))

    selected_profile = proxy.registry.get("glm")
    assert selected_profile.runnable_reason() == "pricing_missing"
    assert selected_profile.authorized is False


def test_main_authorize_requires_pricing_flags(tmp_path: Path) -> None:
    auth_path = _write_auth_json(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(_main_argv(tmp_path, auth_path) + ["--authorize"])

    assert excinfo.value.code == 2


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
    max_tokens_cap: int
    glm_profile_provider_object: dict[str, Any]
    ledger: BudgetLedger
    log_path: Path
    fake_upstream: _FakeUpstream


def _runnable_glm_profiles() -> tuple[dict[str, Any], dict[str, Any]]:
    profiles = DEFAULT_PROFILES()
    glm = profiles["glm"]
    runnable_glm = dataclasses.replace(
        glm,
        pricing=TEST_PRICING,
        authorized=True,
    )
    profiles["glm"] = runnable_glm
    return profiles, runnable_glm.provider_object or {}


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
    if profiles_override is not None:
        profiles = profiles_override
        glm_provider_object = (profiles.get("glm").provider_object if "glm" in profiles else {}) or {}
    else:
        profiles, glm_provider_object = _runnable_glm_profiles()

    registry = ProfileRegistry(profiles)
    profile = registry.get(profile_name)

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
            max_tokens_cap=int(max_tokens_cap),
            glm_profile_provider_object=glm_provider_object,
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
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    conn = http.client.HTTPConnection(running.host, running.port, timeout=5)
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


def _non_stream_success_response(cost: float) -> UpstreamResponse:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "z-ai/glm-5.2",
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


def _stream_lines_with_usage(cost: float) -> list[bytes]:
    first = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "o"}}],
    }
    second = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
        "choices": [{"index": 0, "delta": {"content": "k"}}],
    }
    final = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "z-ai/glm-5.2",
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


def test_injected_provider_and_max_tokens_clamp_and_protected_provider_rejected(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.01)])

    with _running_proxy_server(tmp_path, fake_upstream=fake, max_tokens_cap=64) as running:
        bad_body = _glm_request_body()
        bad_body["provider"] = {"order": ["attacker"]}
        bad_status, _, bad_payload = _post_json(running, bad_body, token=running.run_token)

        ok_status, _, _ = _post_json(running, _glm_request_body(max_tokens=999_999), token=running.run_token)

    assert bad_status == 400
    assert _decode_json(bad_payload)["error"]["code"] == "provider"

    assert ok_status == 200
    assert len(fake.calls) == 1
    forwarded = fake.calls[0].body_json
    assert forwarded["provider"] == running.glm_profile_provider_object
    assert forwarded["max_tokens"] == 64


def test_opus_profile_without_pricing_is_blocked_before_upstream_call(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.01)])
    profiles = DEFAULT_PROFILES()

    with _running_proxy_server(
        tmp_path,
        fake_upstream=fake,
        profile_name="opus",
        profiles_override=profiles,
    ) as running:
        body = {
            "model": "openrouter/anthropic/claude-opus-4.8",
            "messages": [{"role": "user", "content": "blocked"}],
        }
        status, _, payload = _post_json(running, body, token=running.run_token)

    assert status == 403
    error = _decode_json(payload)["error"]
    assert error["code"] == "pricing_missing"
    assert len(fake.calls) == 0


def test_budget_refusal_returns_402_without_forwarding_or_budget_mutation(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.01)])

    with _running_proxy_server(tmp_path, fake_upstream=fake, hard_cap_usd=0.0001, max_tokens_cap=64) as running:
        before = running.ledger.snapshot()
        status, _, payload = _post_json(running, _glm_request_body(), token=running.run_token)
        after = running.ledger.snapshot()

    assert status == 402
    error = _decode_json(payload)["error"]
    assert error["code"] == "budget_exceeded"
    assert len(fake.calls) == 0
    assert after == before


def test_non_stream_usage_cost_settles_actual_and_relays_body(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.02)])

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        status, _, payload = _post_json(running, _glm_request_body(), token=running.run_token)
        snapshot = running.ledger.snapshot()

    assert status == 200
    response = _decode_json(payload)
    assert response["usage"]["cost"] == pytest.approx(0.02)
    assert snapshot["accrued"] == pytest.approx(0.02)
    assert snapshot["committed_unproven"] == pytest.approx(0.0)
    assert snapshot["outstanding_total"] == pytest.approx(0.0)


def test_stream_usage_cost_settles_actual_and_relays_sse_lines(tmp_path: Path) -> None:
    lines = _stream_lines_with_usage(cost=0.03)
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
        )
        try:
            assert response.status == 200
            relayed = [response.readline() for _ in range(len(lines))]
        finally:
            response.close()
            conn.close()

        assert _wait_until(lambda: running.ledger.snapshot()["outstanding_total"] == 0.0)
        snapshot = running.ledger.snapshot()

    assert relayed == lines
    assert snapshot["accrued"] == pytest.approx(0.03)
    assert snapshot["committed_unproven"] == pytest.approx(0.0)


def test_stream_downstream_disconnect_still_drains_and_accounts_spend(tmp_path: Path) -> None:
    proven_cost = 0.07
    lines = _stream_lines_with_usage(cost=proven_cost)
    delayed_stream = _DelayedStream(lines, delay_s=0.05)
    fake = _FakeUpstream(
        [
            UpstreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body=None,
                stream_lines=delayed_stream,
            )
        ]
    )

    with _running_proxy_server(tmp_path, fake_upstream=fake, hard_cap_usd=2.0) as running:
        conn, response = _open_stream_request(
            running,
            _glm_request_body(stream=True),
            token=running.run_token,
        )
        try:
            assert response.status == 200
            first_line = response.readline()
            assert first_line.startswith(b"data: ")
        finally:
            response.close()
            conn.close()

        assert delayed_stream.drained.wait(timeout=5.0), "proxy did not drain fake upstream after disconnect"
        assert delayed_stream.yielded == len(lines)
        assert _wait_until(lambda: running.ledger.snapshot()["outstanding_total"] == 0.0)
        snapshot = running.ledger.snapshot()

    settled_to_actual = (
        snapshot["accrued"] == pytest.approx(proven_cost)
        and snapshot["committed_unproven"] == pytest.approx(0.0)
    )
    retained_unproven = snapshot["committed_unproven"] > 0.0
    assert settled_to_actual or retained_unproven
    assert snapshot["remaining"] <= (running.ledger.hard_cap - proven_cost) + 1e-9


def test_stream_without_usage_retains_unproven_reservation(tmp_path: Path) -> None:
    partial_lines = [
        b'data: {"id":"chatcmpl-fake","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"x"}}]}\n',
    ]
    fake = _FakeUpstream(
        [
            UpstreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body=None,
                stream_lines=partial_lines,
            )
        ]
    )

    with _running_proxy_server(tmp_path, fake_upstream=fake, hard_cap_usd=1.0) as running:
        conn, response = _open_stream_request(
            running,
            _glm_request_body(stream=True),
            token=running.run_token,
        )
        try:
            assert response.status == 200
            _ = response.readline()
        finally:
            response.close()
            conn.close()

        assert _wait_until(lambda: running.ledger.snapshot()["outstanding_total"] == 0.0)
        snapshot = running.ledger.snapshot()

    assert snapshot["accrued"] == pytest.approx(0.0)
    assert snapshot["committed_unproven"] > 0.0


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (
            UpstreamResponse(
                status=500,
                headers={"Content-Type": "application/json"},
                body=b'{"error":{"message":"upstream failed"}}',
                stream_lines=None,
            ),
            500,
        ),
        (RuntimeError("boom"), 502),
    ],
)
def test_upstream_error_paths_retain_unproven_and_surface_error(
    tmp_path: Path,
    outcome: UpstreamResponse | Exception,
    expected_status: int,
) -> None:
    fake = _FakeUpstream([outcome])

    with _running_proxy_server(tmp_path, fake_upstream=fake, hard_cap_usd=1.0) as running:
        status, _, payload = _post_json(running, _glm_request_body(), token=running.run_token)
        snapshot = running.ledger.snapshot()

    assert status == expected_status
    assert len(fake.calls) == 1
    if expected_status == 502:
        assert _decode_json(payload)["error"]["code"] == "upstream_failure"
    assert snapshot["accrued"] == pytest.approx(0.0)
    assert snapshot["outstanding_total"] == pytest.approx(0.0)
    assert snapshot["committed_unproven"] > 0.0


def test_upstream_target_is_openrouter_constant_only(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.01)])

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        status, _, _ = _post_json(running, _glm_request_body(), token=running.run_token)

    assert status == 200
    assert len(fake.calls) == 1
    assert all(call.url == OPENROUTER_UPSTREAM_URL for call in fake.calls)


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
