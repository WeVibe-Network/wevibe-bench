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


def test_main_sources_upstream_key_from_auth_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    auth_path = _write_auth_json(
        tmp_path,
        key="sk-or-test-main-valid",
        orcarouter_key="sk-orca-test-main-valid",
    )
    proxy = _run_main_with_fake_server(monkeypatch, _main_argv(tmp_path, auth_path))

    assert proxy.upstream_key == "sk-orca-test-main-valid"
    assert proxy.upstream_url == ORCAROUTER_UPSTREAM_URL


def test_main_bigpickle_sources_opencode_key_and_zen_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # This test isolates key-source + URL routing; pinned-fingerprint behavior is
    # covered by dedicated _assert_expected_upstream_key_fp tests below.
    monkeypatch.setattr(proxy_server, "key_fingerprint", lambda _raw: "b5ce6e5e")

    auth_path = _write_auth_json(
        tmp_path,
        key="sk-or-test-main-openrouter",
        opencode_key="sk-zen-test-main-valid",
    )
    proxy = _run_main_with_fake_server(
        monkeypatch,
        _main_argv(
            tmp_path,
            auth_path,
            model=_model_selector_for_profile("bigpickle"),
            profile="bigpickle",
        ),
    )

    assert proxy.upstream_key == "sk-zen-test-main-valid"
    assert proxy.upstream_url == OPENCODE_ZEN_UPSTREAM_URL


def test_main_errors_when_auth_json_path_missing(tmp_path: Path) -> None:
    missing_auth = tmp_path / "missing-auth.json"

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(_main_argv(tmp_path, missing_auth))

    assert excinfo.value.code == 2


def test_main_authorize_with_live_pricing_unblocks_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(proxy_server, "fetch_orcarouter_pricing", lambda: _valid_orcarouter_payload())

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


def test_main_authorize_orcarouter_pricing_gate_logs_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(proxy_server, "fetch_orcarouter_pricing", lambda: _valid_orcarouter_payload())

    auth_path = _write_auth_json(tmp_path)
    argv = _main_argv(tmp_path, auth_path) + [
        "--authorize",
        "--pricing-input",
        "1.0",
        "--pricing-output",
        "3.0",
        "--pricing-cache-read",
        "0.2",
    ]
    proxy = _run_main_with_fake_server(monkeypatch, argv)

    log_text = Path(proxy.logger.log_path).read_text(encoding="utf-8")
    assert 'event="pricing_gate"' in log_text
    assert 'status="ok"' in log_text


def test_main_authorize_orcarouter_pricing_gate_failure_refuses_paid_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    ]

    make_server_calls = 0

    def _fake_make_server(_proxy: ProxyServer, host: str = "127.0.0.1", port: int = 0) -> _MainTestServer:
        nonlocal make_server_calls
        make_server_calls += 1
        return _MainTestServer()

    monkeypatch.setattr(proxy_server, "make_server", _fake_make_server)

    def _fake_fetch() -> dict[str, Any]:
        raise PricingGateError("orcarouter pricing gate failed: pricing_version pin mismatch")

    monkeypatch.setattr(proxy_server, "fetch_orcarouter_pricing", _fake_fetch)

    assert proxy_server.main(argv) == 2
    assert make_server_calls == 0
    assert "refusing paid calls" in capsys.readouterr().err

    log_text = (tmp_path / "main-proxy.log").read_text(encoding="utf-8")
    assert 'event="pricing_gate"' in log_text
    assert 'status="failed"' in log_text


def test_main_without_authorize_does_not_consult_orcarouter_pricing_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _unexpected_fetch() -> dict[str, Any]:
        raise AssertionError("fetch_orcarouter_pricing should not be called without --authorize")

    monkeypatch.setattr(proxy_server, "fetch_orcarouter_pricing", _unexpected_fetch)

    auth_path = _write_auth_json(tmp_path)
    proxy = _run_main_with_fake_server(monkeypatch, _main_argv(tmp_path, auth_path))

    log_text = Path(proxy.logger.log_path).read_text(encoding="utf-8")
    assert 'event="pricing_gate"' not in log_text


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


def test_main_authorize_accepts_zero_pricing_for_bigpickle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(proxy_server, "key_fingerprint", lambda _raw: "b5ce6e5e")

    auth_path = _write_auth_json(tmp_path, opencode_key="sk-zen-zero-pricing")
    argv = _main_argv(
        tmp_path,
        auth_path,
        model=_model_selector_for_profile("bigpickle"),
        profile="bigpickle",
    ) + [
        "--authorize",
        "--pricing-input",
        "0",
        "--pricing-output",
        "0",
        "--pricing-cache-read",
        "0",
    ]

    proxy = _run_main_with_fake_server(monkeypatch, argv)
    selected_profile = proxy.registry.get("bigpickle")

    assert selected_profile.authorized is True
    assert selected_profile.pricing == {
        "input": pytest.approx(0.0),
        "output": pytest.approx(0.0),
        "cache_read": pytest.approx(0.0),
    }
    assert selected_profile.runnable_reason() is None


@pytest.mark.parametrize(
    ("pricing_flag", "pricing_value"),
    [
        ("--pricing-input", "-0.1"),
        ("--pricing-output", "-0.1"),
    ],
)
def test_main_authorize_rejects_negative_pricing_for_bigpickle(
    tmp_path: Path,
    pricing_flag: str,
    pricing_value: str,
) -> None:
    auth_path = _write_auth_json(tmp_path, opencode_key="sk-zen-negative-pricing")
    argv = _main_argv(
        tmp_path,
        auth_path,
        model=_model_selector_for_profile("bigpickle"),
        profile="bigpickle",
    ) + [
        "--authorize",
        "--pricing-input",
        "0",
        "--pricing-output",
        "0",
        pricing_flag,
        pricing_value,
    ]

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(argv)

    assert excinfo.value.code == 2


@pytest.mark.parametrize("profile_name", ("mimo", "mimo25", "ring"))
def test_main_requires_provider_order_for_unpinned_profiles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    profile_name: str,
) -> None:
    auth_path = _write_auth_json(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(
            _main_argv(
                tmp_path,
                auth_path,
                model=_model_selector_for_profile(profile_name),
                profile=profile_name,
            )
        )

    assert excinfo.value.code == 2
    assert "--provider-order" in capsys.readouterr().err


def test_main_rejects_provider_order_for_hardcoded_profiles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth_path = _write_auth_json(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(
            _main_argv(
                tmp_path,
                auth_path,
                model=_model_selector_for_profile("opus"),
                profile="opus",
            )
            + ["--provider-order", "novita"]
        )

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "hardcoded provider pin" in err
    assert "--provider-order" in err


@pytest.mark.parametrize("profile_name", ("glm", "hy3", "kimicode", "kimik3"))
def test_main_rejects_provider_pin_flags_for_orcarouter_profiles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    profile_name: str,
) -> None:
    auth_path = _write_auth_json(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(
            _main_argv(
                tmp_path,
                auth_path,
                model=_model_selector_for_profile(profile_name),
                profile=profile_name,
            )
            + ["--provider-order", "novita"]
        )

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "uses upstream 'orcarouter'" in err
    assert "--provider-order is incompatible" in err


def test_main_provider_quant_requires_provider_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    auth_path = _write_auth_json(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(
            _main_argv(
                tmp_path,
                auth_path,
                model=_model_selector_for_profile("opus"),
                profile="opus",
            )
            + ["--provider-quant", "fp8"]
        )

    assert excinfo.value.code == 2
    assert "--provider-quant requires --provider-order" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("extra_args", "expected_flag"),
    [
        (["--provider-order", "novita"], "--provider-order"),
        (["--provider-quant", "fp8"], "--provider-quant"),
    ],
)
def test_main_rejects_provider_pin_flags_for_zen_profiles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str],
    expected_flag: str,
) -> None:
    auth_path = _write_auth_json(tmp_path, opencode_key="sk-zen-provider-pin")

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(
            _main_argv(
                tmp_path,
                auth_path,
                model=_model_selector_for_profile("bigpickle"),
                profile="bigpickle",
            )
            + extra_args
        )

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert expected_flag in err
    assert "incompatible" in err


@pytest.mark.parametrize(
    ("profile_name", "provider_slug", "provider_quant", "expected_provider"),
    [
        (
            "mimo",
            "atlascloud",
            None,
            {
                "order": ["atlascloud"],
                "only": ["atlascloud"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        ),
        (
            "ring",
            "deepinfra",
            "bf16",
            {
                "order": ["deepinfra"],
                "only": ["deepinfra"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "quantizations": ["bf16"],
            },
        ),
    ],
)
def test_main_builds_runtime_provider_pin_and_logs_proxy_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile_name: str,
    provider_slug: str,
    provider_quant: str | None,
    expected_provider: dict[str, Any],
) -> None:
    auth_path = _write_auth_json(tmp_path)
    argv = _main_argv(
        tmp_path,
        auth_path,
        model=_model_selector_for_profile(profile_name),
        profile=profile_name,
    ) + ["--provider-order", provider_slug]
    if provider_quant is not None:
        argv.extend(["--provider-quant", provider_quant])

    proxy = _run_main_with_fake_server(monkeypatch, argv)

    selected_profile = proxy.registry.get(profile_name)
    assert selected_profile.provider_object == expected_provider

    log_text = Path(proxy.logger.log_path).read_text(encoding="utf-8")
    expected_provider_json = json.dumps(expected_provider, ensure_ascii=False, separators=(",", ":"))
    assert "event=\"proxy_start\"" in log_text
    assert f"provider={expected_provider_json}" in log_text


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


def test_no_provider_injection_for_orcarouter_profile_and_max_tokens_clamp_and_protected_provider_rejected(
    tmp_path: Path,
) -> None:
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
    assert "provider" not in forwarded
    assert forwarded["model"] == "openrouter/z-ai/glm-5.2"
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


def test_proven_billed_prefix_flips_reservation_refusal_to_admit(tmp_path: Path) -> None:
    """A settled request establishes a cached prefix priced at cache-read.

    The hard cap is chosen so the second identical request would be REFUSED
    under conservative (cache-write) pricing but is ADMITTED once the
    proven-billed prefix is priced at cache-read rate (the opus48-smoke-19b
    feedback-refusal shape, scaled down).
    """
    fake = _FakeUpstream(
        [
            _non_stream_success_response(cost=0.01),
            _non_stream_success_response(cost=0.01),
        ]
    )
    body = _glm_request_body(prompt="x" * 200_000)

    with _running_proxy_server(
        tmp_path,
        fake_upstream=fake,
        hard_cap_usd=0.03,
        max_tokens_cap=64,
    ) as running:
        status_1, _, _ = _post_json(
            running, body, token=running.run_token, trace_id="trace-first-billed"
        )
        status_2, _, _ = _post_json(
            running, body, token=running.run_token, trace_id="trace-cached-repeat"
        )
        snapshot = running.ledger.snapshot()
        line_1 = _summary_log_line_for_trace(running.log_path, "trace-first-billed")
        line_2 = _summary_log_line_for_trace(running.log_path, "trace-cached-repeat")

    assert status_1 == 200
    assert status_2 == 200
    assert len(fake.calls) == 2
    assert snapshot["accrued"] == pytest.approx(0.02)
    assert snapshot["outstanding_total"] == pytest.approx(0.0)

    in_ub_1 = _int_field(line_1, "in_tokens_ub")
    assert _int_field(line_1, "cached_in_tokens_ub") == 0
    assert _int_field(line_2, "cached_in_tokens_ub") == in_ub_1


def test_retained_unproven_request_does_not_establish_cached_prefix(tmp_path: Path) -> None:
    """Only PROVEN-billed (usage.cost-settled) requests establish the prefix."""
    fake = _FakeUpstream(
        [
            _non_stream_response_without_usage(),
            _non_stream_success_response(cost=0.01),
            _non_stream_success_response(cost=0.01),
        ]
    )
    body = _glm_request_body(prompt="y" * 50_000)

    with _running_proxy_server(tmp_path, fake_upstream=fake, max_tokens_cap=64) as running:
        status_1, _, _ = _post_json(
            running, body, token=running.run_token, trace_id="trace-unproven"
        )
        status_2, _, _ = _post_json(
            running, body, token=running.run_token, trace_id="trace-after-unproven"
        )
        status_3, _, _ = _post_json(
            running, body, token=running.run_token, trace_id="trace-after-settled"
        )
        line_2 = _summary_log_line_for_trace(running.log_path, "trace-after-unproven")
        line_3 = _summary_log_line_for_trace(running.log_path, "trace-after-settled")

    assert status_1 == 200
    assert status_2 == 200
    assert status_3 == 200
    # Request 1 retained-unproven (no usage) => request 2 sees NO cached prefix.
    assert _int_field(line_2, "cached_in_tokens_ub") == 0
    # Request 2 settled with usage.cost => request 3 sees the full prefix.
    assert _int_field(line_3, "cached_in_tokens_ub") == _int_field(line_2, "in_tokens_ub")


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


def test_non_stream_usage_without_cost_settles_derived_and_logs_settle_state(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_derived_response()])

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        before = running.ledger.snapshot()
        trace_id = "trace-non-stream-derived"
        status, _, _ = _post_json(
            running,
            _glm_request_body(),
            token=running.run_token,
            trace_id=trace_id,
        )
        snapshot = running.ledger.snapshot()
        log_text = running.log_path.read_text(encoding="utf-8")

    expected_derived = 3.0e-6
    assert status == 200
    assert snapshot["accrued_derived"] == pytest.approx(expected_derived)
    assert snapshot["accrued"] == pytest.approx(0.0)
    assert snapshot["committed_unproven"] == pytest.approx(0.0)
    assert snapshot["outstanding_total"] == pytest.approx(0.0)
    assert snapshot["remaining"] == pytest.approx(before["remaining"] - expected_derived)

    line = next(
        (
            entry
            for entry in log_text.splitlines()
            if f'trace_id="{trace_id}"' in entry and 'settle_state="derived"' in entry
        ),
        None,
    )
    assert line is not None
    assert "accrued_derived_usd=" in line


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


def test_stream_usage_without_cost_settles_derived_and_relays_sse_lines(tmp_path: Path) -> None:
    lines = _stream_lines_derived()
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
        before = running.ledger.snapshot()
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

    expected_derived = 3.0e-6
    assert relayed == lines
    assert snapshot["accrued_derived"] == pytest.approx(expected_derived)
    assert snapshot["accrued"] == pytest.approx(0.0)
    assert snapshot["committed_unproven"] == pytest.approx(0.0)
    assert snapshot["outstanding_total"] == pytest.approx(0.0)
    assert snapshot["remaining"] == pytest.approx(before["remaining"] - expected_derived)


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


def test_upstream_target_is_orcarouter_constant_for_glm(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.01)])

    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        status, _, _ = _post_json(
            running,
            {
                "model": "z-ai/glm-5.2",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 999_999,
            },
            token=running.run_token,
        )

    assert status == 200
    assert running.upstream_url == ORCAROUTER_UPSTREAM_URL
    assert len(fake.calls) == 1
    assert all(call.url == ORCAROUTER_UPSTREAM_URL for call in fake.calls)
    assert "provider" not in fake.calls[0].body_json
    assert fake.calls[0].body_json["model"] == "z-ai/glm-5.2"
    assert fake.calls[0].headers["User-Agent"] == "wevibe-bench-proxy/1.0"


def test_upstream_target_is_opencode_zen_for_bigpickle(tmp_path: Path) -> None:
    fake = _FakeUpstream([_non_stream_success_response(cost=0.0)])
    profiles = DEFAULT_PROFILES()
    profiles["bigpickle"] = dataclasses.replace(
        profiles["bigpickle"],
        pricing={"input": 0.0, "output": 0.0},
        authorized=True,
    )

    with _running_proxy_server(
        tmp_path,
        fake_upstream=fake,
        profile_name="bigpickle",
        profiles_override=profiles,
    ) as running:
        status, _, _ = _post_json(
            running,
            {
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "hello zen"}],
                "max_tokens": 32,
            },
            token=running.run_token,
        )

    assert status == 200
    assert running.upstream_url == OPENCODE_ZEN_UPSTREAM_URL
    assert len(fake.calls) == 1
    assert fake.calls[0].url == OPENCODE_ZEN_UPSTREAM_URL
    assert "provider" not in fake.calls[0].body_json
    # Zen requires the bare model id upstream (401 on "opencode/<id>", verified 2026-07-21).
    assert fake.calls[0].body_json["model"] == "big-pickle"
    # Cloudflare bans urllib's default UA signature (error 1010 -> 403); explicit UA required.
    assert fake.calls[0].headers["User-Agent"] == "wevibe-bench-proxy/1.0"


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


def test_stream_upstream_iterator_failure_retains_unproven_and_logs_no_hang(tmp_path: Path) -> None:
    def _broken_stream() -> Iterable[bytes]:
        yield (
            b'data: {"id":"chatcmpl-fake","object":"chat.completion.chunk",'
            b'"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
        )
        raise RuntimeError("truncated upstream")

    fake = _FakeUpstream(
        [
            UpstreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body=None,
                stream_lines=_broken_stream(),
            )
        ]
    )

    with _running_proxy_server(tmp_path, fake_upstream=fake, hard_cap_usd=1.0) as running:
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

        assert _wait_until(lambda: running.ledger.snapshot()["outstanding_total"] == 0.0)
        snapshot = running.ledger.snapshot()
        log_text = running.log_path.read_text(encoding="utf-8")

    assert connection_header == "close"
    assert elapsed_s < 4.0
    assert body.startswith(b"data: ")
    assert "event=\"stream_relay_end\"" in log_text
    assert "stream_error=\"upstream stream failure: truncated upstream\"" in log_text
    assert snapshot["accrued"] == pytest.approx(0.0)
    assert snapshot["committed_unproven"] > 0.0
    assert snapshot["outstanding_total"] == pytest.approx(0.0)
    assert len(fake.calls) == 1


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


def test_identity_match_passes_and_never_trips(tmp_path: Path) -> None:
    fake = _FakeUpstream(
        [
            _bigpickle_zen_response(model="big-pickle"),
            _bigpickle_zen_response(model="big-pickle"),
        ]
    )
    with _running_proxy_server(
        tmp_path,
        fake_upstream=fake,
        profile_name="bigpickle",
        profiles_override=_runnable_bigpickle_profiles(),
    ) as running:
        status_1, _, _ = _post_json(running, _bigpickle_body(), token=running.run_token)
        status_2, _, _ = _post_json(running, _bigpickle_body(), token=running.run_token)
        log_text = running.log_path.read_text(encoding="utf-8")

    assert status_1 == 200 and status_2 == 200
    assert 'event="identity_mismatch"' not in log_text
    assert log_text.count('model="big-pickle"') == 2


def test_identity_mismatch_logs_and_refuses_subsequent_requests(tmp_path: Path) -> None:
    fake = _FakeUpstream(
        [
            _bigpickle_zen_response(model="somebody/else-entirely"),
            _bigpickle_zen_response(model="big-pickle"),
        ]
    )
    with _running_proxy_server(
        tmp_path,
        fake_upstream=fake,
        profile_name="bigpickle",
        profiles_override=_runnable_bigpickle_profiles(),
    ) as running:
        status_1, _, _ = _post_json(running, _bigpickle_body(), token=running.run_token)
        status_2, _, payload_2 = _post_json(running, _bigpickle_body(), token=running.run_token)
        log_text = running.log_path.read_text(encoding="utf-8")

    # First response relays (already consumed) but trips the one-way switch.
    assert status_1 == 200
    assert status_2 == 503
    error = _decode_json(payload_2)["error"]
    assert error["code"] == "identity_mismatch"
    assert 'event="identity_mismatch"' in log_text
    assert 'expected_upstream_model="big-pickle"' in log_text
    assert 'observed_upstream_model="somebody/else-entirely"' in log_text
    # The swapped model never reached upstream a second time.
    assert len(fake.calls) == 1


def test_identity_old_echo_now_trips_and_refuses(tmp_path: Path) -> None:
    fake = _FakeUpstream(
        [
            _bigpickle_zen_response(model="xiaomi/mimo-v2.5"),
            _bigpickle_zen_response(model="big-pickle"),
        ]
    )
    with _running_proxy_server(
        tmp_path,
        fake_upstream=fake,
        profile_name="bigpickle",
        profiles_override=_runnable_bigpickle_profiles(),
    ) as running:
        status_1, _, _ = _post_json(running, _bigpickle_body(), token=running.run_token)
        status_2, _, payload_2 = _post_json(running, _bigpickle_body(), token=running.run_token)
        log_text = running.log_path.read_text(encoding="utf-8")

    # Old upstream echo now mismatches the pinned identity and latches refusal.
    assert status_1 == 200
    assert status_2 == 503
    error = _decode_json(payload_2)["error"]
    assert error["code"] == "identity_mismatch"
    assert 'event="identity_mismatch"' in log_text
    assert 'expected_upstream_model="big-pickle"' in log_text
    assert 'observed_upstream_model="xiaomi/mimo-v2.5"' in log_text
    # Latched request was refused locally and never forwarded upstream.
    assert len(fake.calls) == 1


def test_identity_mismatch_latches_for_glm_orcarouter_profile(tmp_path: Path) -> None:
    fake = _FakeUpstream(
        [
            _non_stream_success_response(cost=0.01, model="somebody/else-entirely"),
            _non_stream_success_response(cost=0.01),
        ]
    )
    with _running_proxy_server(tmp_path, fake_upstream=fake) as running:
        status_1, _, _ = _post_json(running, _glm_request_body(), token=running.run_token)
        status_2, _, payload_2 = _post_json(running, _glm_request_body(), token=running.run_token)
        log_text = running.log_path.read_text(encoding="utf-8")

    assert status_1 == 200
    assert status_2 == 503
    error = _decode_json(payload_2)["error"]
    assert error["code"] == "identity_mismatch"
    assert 'event="identity_mismatch"' in log_text
    assert 'expected_upstream_model="glm-5.2"' in log_text
    assert 'observed_upstream_model="somebody/else-entirely"' in log_text
    assert len(fake.calls) == 1


def test_assert_expected_upstream_key_fp_noop_when_profile_unpinned() -> None:
    glm_profile = DEFAULT_PROFILES()["glm"]
    _assert_expected_upstream_key_fp(glm_profile, "deadbeef")


def test_assert_expected_upstream_key_fp_noop_when_matching() -> None:
    bigpickle_profile = DEFAULT_PROFILES()["bigpickle"]
    _assert_expected_upstream_key_fp(bigpickle_profile, "b5ce6e5e")


def test_assert_expected_upstream_key_fp_raises_when_mismatched() -> None:
    bigpickle_profile = DEFAULT_PROFILES()["bigpickle"]
    with pytest.raises(RuntimeError) as excinfo:
        _assert_expected_upstream_key_fp(bigpickle_profile, "deadbeef")

    message = str(excinfo.value)
    assert "b5ce6e5e" in message
    assert "deadbeef" in message
