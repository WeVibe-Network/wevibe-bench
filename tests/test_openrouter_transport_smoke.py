from __future__ import annotations

import importlib.util
import json
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "openrouter_transport_smoke.py"
_SPEC = importlib.util.spec_from_file_location("openrouter_transport_smoke", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"failed to load script module at {_SCRIPT_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
SMOKE_MAIN = _MODULE.main


@dataclass(slots=True)
class _ServerState:
    expected_token: str
    expected_model: str
    expected_max_tokens: int
    response_status: int
    response_payload: dict[str, Any]
    request_count: int = 0
    errors: list[str] = field(default_factory=list)


def _handler_for(state: _ServerState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_POST(self) -> None:  # noqa: N802
            state.request_count += 1
            try:
                assert self.path == "/api/v1/chat/completions"
                assert self.headers.get("Authorization") == f"Bearer {state.expected_token}"

                content_length = int(self.headers.get("Content-Length", "0"))
                request_payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                assert request_payload["model"] == state.expected_model
                assert request_payload["max_tokens"] == state.expected_max_tokens
                assert request_payload["stream"] is False
                assert request_payload["usage"] == {"include": True}
                assert request_payload["messages"][0]["role"] == "user"
            except Exception as exc:  # noqa: BLE001
                state.errors.append(str(exc))

            body = json.dumps(state.response_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.send_response(state.response_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


@contextmanager
def _serve(state: _ServerState) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@dataclass(slots=True)
class _ProbeResponse:
    status: int
    payload: dict[str, Any] | None = None
    sse_data: list[str] | None = None


@dataclass(slots=True)
class _ProbeServerState:
    expected_token: str
    expected_model: str
    expected_max_tokens: int
    responses: dict[str, _ProbeResponse]
    request_count: int = 0
    request_kinds: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _classify_request(payload: dict[str, Any]) -> str:
    if payload.get("stream") is True:
        return "streaming"
    if "tools" in payload:
        return "tools"
    if "response_format" in payload:
        return "structured"
    return "shape"


def _probe_handler_for(state: _ProbeServerState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_POST(self) -> None:  # noqa: N802
            state.request_count += 1
            response = _ProbeResponse(status=500, payload={"error": {"message": "missing test response"}})
            try:
                assert self.path == "/api/v1/chat/completions"
                assert self.headers.get("Authorization") == f"Bearer {state.expected_token}"

                content_length = int(self.headers.get("Content-Length", "0"))
                request_payload = json.loads(self.rfile.read(content_length).decode("utf-8"))

                assert request_payload["model"] == state.expected_model
                assert request_payload["max_tokens"] == state.expected_max_tokens
                assert request_payload["usage"] == {"include": True}
                assert request_payload["messages"][0]["role"] == "user"

                kind = _classify_request(request_payload)
                state.request_kinds.append(kind)

                if kind == "streaming":
                    assert request_payload["stream"] is True
                else:
                    assert request_payload["stream"] is False

                if kind == "tools":
                    assert "tool_choice" in request_payload
                    assert "tools" in request_payload
                if kind == "structured":
                    assert "response_format" in request_payload

                response = state.responses.get(kind) or state.responses.get("shape")
                assert response is not None
            except Exception as exc:  # noqa: BLE001
                state.errors.append(str(exc))

            if response.sse_data is not None:
                self.send_response(response.status)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for segment in response.sse_data:
                    frame = f"data: {segment}\n\n".encode("utf-8")
                    self.wfile.write(frame)
                    self.wfile.flush()
                return

            payload = response.payload or {}
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


@contextmanager
def _serve_probe(state: _ProbeServerState) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _probe_handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_openrouter_transport_smoke_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")

    log_path = tmp_path / "smoke-success.log"
    state = _ServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        response_status=200,
        response_payload={
            "id": "chatcmpl-smoke-1",
            "provider": "openrouter-test-provider",
            "model": "z-ai/glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "cost": 0.0000123,
            },
        },
    )

    with _serve(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--log",
                str(log_path),
                "--evidence-out",
                str(tmp_path / "shape-success-evidence.json"),
            ]
        )

    assert state.request_count == 1
    assert state.errors == []
    assert exit_code == 0

    stdout_line = capsys.readouterr().out.strip()
    summary = json.loads(stdout_line)
    assert summary["completion_ok"] is True
    assert summary["cost_usd"] == pytest.approx(0.0000123)

    log_line = log_path.read_text(encoding="utf-8").strip()
    logged = json.loads(log_line)
    assert logged["completion_ok"] is True
    assert logged["cost_usd"] == pytest.approx(0.0000123)
    assert logged["content_present"] is True
    assert token not in log_line
    assert token not in stdout_line
    assert '"OK"' not in log_line
    assert '"OK"' not in stdout_line


def test_openrouter_transport_smoke_http_404_is_transport_ok(tmp_path: Path) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")

    log_path = tmp_path / "smoke-error.log"
    state = _ServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        response_status=404,
        response_payload={
            "error": {
                "message": "No endpoints found",
                "code": 404,
            }
        },
    )

    with _serve(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--log",
                str(log_path),
                "--evidence-out",
                str(tmp_path / "shape-404-evidence.json"),
            ]
        )

    assert state.request_count == 1
    assert state.errors == []
    assert exit_code == 2

    logged = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert logged["transport_ok"] is True
    assert logged["completion_ok"] is False
    assert logged["http_status"] == 404
    assert logged["error_code"] == 404


def test_openrouter_transport_smoke_streaming_happy_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")
    log_path = tmp_path / "streaming-happy.log"

    chunk_1 = json.dumps(
        {
            "provider": {"slug": "openrouter-test-provider", "quantization": "q4"},
            "choices": [{"delta": {"content": "O"}, "finish_reason": None}],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    chunk_2 = json.dumps(
        {
            "choices": [{"delta": {"content": "K"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 9, "cost": 0.00001},
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )

    state = _ProbeServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        responses={"streaming": _ProbeResponse(status=200, sse_data=[chunk_1, chunk_2, "[DONE]"])},
    )

    with _serve_probe(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--checks",
                "streaming",
                "--log",
                str(log_path),
                "--evidence-out",
                str(tmp_path / "streaming-happy-evidence.json"),
            ]
        )

    assert exit_code == 0
    assert state.request_count == 1
    assert state.request_kinds == ["streaming"]
    assert state.errors == []

    stdout_line = capsys.readouterr().out.strip()
    summary = json.loads(stdout_line)
    check = summary["checks"]["streaming"]
    assert check["pass"] is True
    assert check["chunks"] == 2
    assert check["done"] is True
    assert check["ms"] is not None

    log_line = log_path.read_text(encoding="utf-8").strip()
    logged = json.loads(log_line)
    assert logged["checks"]["streaming"]["pass"] is True
    assert token not in log_line
    assert token not in stdout_line
    assert '"OK"' not in log_line
    assert '"OK"' not in stdout_line


def test_openrouter_transport_smoke_streaming_truncated_fails_exit_4(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")
    log_path = tmp_path / "streaming-truncated.log"

    chunk_1 = json.dumps(
        {
            "provider": "openrouter-test-provider",
            "choices": [{"delta": {"content": "O"}, "finish_reason": None}],
            "usage": {"total_tokens": 7, "cost": 0.00001},
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )

    state = _ProbeServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        responses={"streaming": _ProbeResponse(status=200, sse_data=[chunk_1])},
    )

    with _serve_probe(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--checks",
                "streaming",
                "--log",
                str(log_path),
                "--evidence-out",
                str(tmp_path / "streaming-truncated-evidence.json"),
            ]
        )

    assert exit_code == 4
    assert state.request_count == 1
    assert state.request_kinds == ["streaming"]
    assert state.errors == []

    summary = json.loads(capsys.readouterr().out.strip())
    check = summary["checks"]["streaming"]
    assert check["pass"] is False
    assert check["done"] is False
    assert "[DONE]" in summary["errors"][0]


def test_openrouter_transport_smoke_tools_honored(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")
    log_path = tmp_path / "tools-pass.log"

    state = _ProbeServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        responses={
            "tools": _ProbeResponse(
                status=200,
                payload={
                    "provider": "openrouter-test-provider",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": "{\"location\":\"Paris\"}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"total_tokens": 11, "cost": 0.00003},
                },
            )
        },
    )

    with _serve_probe(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--checks",
                "tools",
                "--log",
                str(log_path),
                "--evidence-out",
                str(tmp_path / "tools-pass-evidence.json"),
            ]
        )

    assert exit_code == 0
    assert state.request_count == 1
    assert state.request_kinds == ["tools"]
    assert state.errors == []

    summary = json.loads(capsys.readouterr().out.strip())
    check = summary["checks"]["tools"]
    assert check["pass"] is True
    assert check["tool_name"] == "get_weather"
    assert check["arguments_json_ok"] is True


def test_openrouter_transport_smoke_tools_silent_drop_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")
    log_path = tmp_path / "tools-fail.log"

    state = _ProbeServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        responses={
            "tools": _ProbeResponse(
                status=200,
                payload={
                    "provider": "openrouter-test-provider",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "The weather in Paris is sunny.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 10, "cost": 0.00002},
                },
            )
        },
    )

    with _serve_probe(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--checks",
                "tools",
                "--log",
                str(log_path),
                "--evidence-out",
                str(tmp_path / "tools-fail-evidence.json"),
            ]
        )

    assert exit_code == 4
    assert state.request_count == 1
    assert state.request_kinds == ["tools"]
    assert state.errors == []

    summary = json.loads(capsys.readouterr().out.strip())
    check = summary["checks"]["tools"]
    assert check["pass"] is False
    assert check["tool_calls_present"] is False
    assert "silent parameter drop" in summary["errors"][0]


def test_openrouter_transport_smoke_structured_valid_json_passes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")
    log_path = tmp_path / "structured-pass.log"

    state = _ProbeServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        responses={
            "structured": _ProbeResponse(
                status=200,
                payload={
                    "provider": "openrouter-test-provider",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "{\"answer\":\"OK\"}",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 8, "cost": 0.00002},
                },
            )
        },
    )

    with _serve_probe(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--checks",
                "structured",
                "--log",
                str(log_path),
                "--evidence-out",
                str(tmp_path / "structured-pass-evidence.json"),
            ]
        )

    assert exit_code == 0
    assert state.request_count == 1
    assert state.request_kinds == ["structured"]
    assert state.errors == []

    summary = json.loads(capsys.readouterr().out.strip())
    check = summary["checks"]["structured"]
    assert check["pass"] is True
    assert check["schema_ok"] is True


def test_openrouter_transport_smoke_structured_invalid_json_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")
    log_path = tmp_path / "structured-fail.log"

    state = _ProbeServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        responses={
            "structured": _ProbeResponse(
                status=200,
                payload={
                    "provider": "openrouter-test-provider",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "{\"answer\":42}",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 8, "cost": 0.00002},
                },
            )
        },
    )

    with _serve_probe(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--checks",
                "structured",
                "--log",
                str(log_path),
                "--evidence-out",
                str(tmp_path / "structured-fail-evidence.json"),
            ]
        )

    assert exit_code == 4
    assert state.request_count == 1
    assert state.request_kinds == ["structured"]
    assert state.errors == []

    summary = json.loads(capsys.readouterr().out.strip())
    check = summary["checks"]["structured"]
    assert check["pass"] is False
    assert check["schema_ok"] is False




def test_openrouter_transport_smoke_evidence_json_written_with_stage3_schema(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")
    log_path = tmp_path / "evidence-schema.log"
    evidence_path = tmp_path / "stage3-evidence.json"

    tools_payload = {
        "provider": {"slug": "openrouter-test-provider", "quantization": "q4"},
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": "{\"location\":\"Paris\"}",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"total_tokens": 7, "cost": 0.00003},
    }

    state = _ProbeServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        responses={
            "tools": _ProbeResponse(status=200, payload=tools_payload),
            "structured": _ProbeResponse(
                status=200,
                payload={
                    "provider": {"slug": "openrouter-test-provider", "quantization": "q4"},
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "{\"answer\":\"OK\"}",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 5, "cost": 0.00002},
                },
            ),
        },
    )

    with _serve_probe(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--checks",
                "tools,structured,require-params",
                "--evidence-out",
                str(evidence_path),
                "--log",
                str(log_path),
            ]
        )

    assert exit_code == 0
    assert state.request_count == 3
    assert state.request_kinds == ["tools", "structured", "tools"]
    assert state.errors == []
    _ = capsys.readouterr()

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == 1
    assert evidence["stage"] == 3
    assert evidence["slug"] == "z-ai/glm-5.2"
    assert isinstance(evidence["captured_at"], str)
    assert evidence["checks"]["tools"]["pass"] is True
    assert evidence["checks"]["tools"]["tool_name"] == "get_weather"
    assert evidence["checks"]["structured"]["pass"] is True
    assert evidence["checks"]["structured"]["schema_ok"] is True
    assert evidence["checks"]["require-params"]["pass"] is True
    assert evidence["tokens_used_total"] == 19
    assert evidence["token_budget"] == 8000
    assert evidence["budget_ok"] is True
    assert evidence["cost_usd_total"] == pytest.approx(0.00008)
    assert evidence["provider_slugs"] == ["openrouter-test-provider"]
    assert evidence["quantizations"] == ["q4"]
    assert evidence["errors"] == []
    assert evidence["trace"].startswith("smoke-")


def test_openrouter_transport_smoke_non_shape_defaults_evidence_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "ephemeral-transport-token"
    token_file = tmp_path / "token.txt"
    token_file.write_text(token, encoding="utf-8")
    log_path = tmp_path / "auto-evidence.log"

    state = _ProbeServerState(
        expected_token=token,
        expected_model="z-ai/glm-5.2",
        expected_max_tokens=16,
        responses={
            "tools": _ProbeResponse(
                status=200,
                payload={
                    "provider": "openrouter-test-provider",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": "{\"location\":\"Paris\"}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"total_tokens": 7, "cost": 0.00003},
                },
            )
        },
    )

    monkeypatch.chdir(tmp_path)
    with _serve_probe(state) as server:
        port = int(server.server_address[1])
        exit_code = SMOKE_MAIN(
            [
                "--proxy-base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--token-file",
                str(token_file),
                "--model",
                "z-ai/glm-5.2",
                "--checks",
                "tools",
                "--log",
                str(log_path),
            ]
        )

    assert exit_code == 0
    assert state.request_count == 1
    assert state.request_kinds == ["tools"]
    assert state.errors == []
    _ = capsys.readouterr()

    qualification_dir = tmp_path / "runs" / "qualification"
    evidence_files = list(qualification_dir.glob("stage3-z-ai-glm-5-2-*.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["stage"] == 3
    assert evidence["checks"]["tools"]["pass"] is True
