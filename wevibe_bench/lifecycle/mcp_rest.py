"""Bearer-auth REST client for MCP lifecycle endpoints."""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .lconfig import LifecycleConfig
from .logging_util import new_trace_id


Transport = Callable[[str, dict[str, str], dict[str, Any] | None], tuple[int, Any, bool]]
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _json_value(payload: bytes) -> Any:
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


class McpRest:
    def __init__(
        self,
        base_url: str,
        cfg: LifecycleConfig,
        logger: Any,
        transport: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._cfg = cfg
        self._logger = logger
        self._transport: Transport = transport or self._urllib_transport

    @staticmethod
    def _urllib_transport(
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, Any, bool]:
        try:
            method = "GET" if body is None else "POST"
            payload = None if body is None else json.dumps(body).encode("utf-8")
            request = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=10.0) as response:
                status = response.getcode()
                response_payload = response.read()
            return status, _json_value(response_payload), True
        except urllib.error.HTTPError as exc:
            try:
                error_payload = exc.read()
            except OSError:
                error_payload = b""
            return exc.code, _json_value(error_payload), True
        except (urllib.error.URLError, OSError, socket.timeout):
            return 0, {}, False
        except Exception:
            return 0, {}, False

    @staticmethod
    def _sanitize(value: Any) -> str:
        return " ".join(str(value).split())

    def _log(
        self,
        level: str,
        op: str,
        trace: str,
        status: str,
        dur_ms: int,
        **fields: Any,
    ) -> None:
        details = " ".join(
            f"{key}={self._sanitize(value)}"
            for key, value in fields.items()
            if value is not None
        )
        msg = f"op={op} trace={trace}"
        if details:
            msg = f"{msg} {details}"
        msg = f"{msg} status={status} dur_ms={dur_ms}"
        log_fn = getattr(self._logger, level, self._logger.info)
        log_fn(msg)

    def _read_token(self) -> str:
        token_path = os.path.expanduser(self._cfg.session_token_path)
        try:
            with open(token_path, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as exc:
            raise RuntimeError(f"session token unavailable at {token_path}: {exc}") from exc

        if not token:
            raise RuntimeError(f"session token blank at {token_path}")
        if not _TOKEN_RE.fullmatch(token):
            raise RuntimeError(f"session token invalid format at {token_path}")
        return token

    def _request(
        self,
        op: str,
        path: str,
        body: dict[str, Any] | None,
        expected_statuses: tuple[int, ...] = (200,),
    ) -> Any:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        headers = {
            "Authorization": f"Bearer {self._read_token()}",
            "Content-Type": "application/json",
            "X-WeVibe-Trace-Id": trace,
        }
        url = f"{self._base_url}{path}"
        http_status, payload, reachable = self._transport(url, headers, body)
        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000

        if not reachable:
            self._log("error", op, trace, "err", int(dur_ms), url=path, reason="unreachable")
            raise RuntimeError(f"mcp unreachable for {path}")
        if http_status not in expected_statuses:
            self._log("error", op, trace, "err", int(dur_ms), url=path, http_status=http_status)
            raise RuntimeError(f"mcp request failed status={http_status} path={path} payload={payload}")

        self._log("info", op, trace, "ok", int(dur_ms), url=path, http_status=http_status)
        return payload

    def recall(self, query: str, org_id: str, **kw: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query,
            "org_id": org_id,
        }
        body.update(kw)
        payload = self._request("lifecycle.mcp.recall", "/v1/recall", body, expected_statuses=(200,))
        if not isinstance(payload, dict):
            raise RuntimeError(f"recall expected object, got: {payload}")
        return payload

    def identity_pubkeys(self) -> dict[str, Any]:
        payload = self._request(
            "lifecycle.mcp.identity_pubkeys",
            "/v1/identity/pubkeys",
            None,
            expected_statuses=(200,),
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"identity pubkeys expected object, got: {payload}")
        return payload
