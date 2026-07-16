from __future__ import annotations

import importlib.util
import json
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
