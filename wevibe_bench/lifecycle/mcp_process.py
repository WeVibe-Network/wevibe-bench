"""Lifecycle MCP process management for env-seed identity instances."""

from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .lconfig import LifecycleConfig
from .logging_util import fp, new_trace_id
from .mcp_root import resolve_mcp_root


Transport = Callable[[str, dict[str, str], dict[str, Any] | None], tuple[int, Any, bool]]
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SEED_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _json_value(payload: bytes) -> Any:
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


@dataclass(frozen=True)
class McpInstance:
    name: str
    port: int
    seed_hex: str
    keystore_path: str
    log_path: str
    pid: int
    url: str


class McpProcessManager:
    def __init__(
        self,
        wevibe_root: str,
        cfg: LifecycleConfig,
        logger: Any,
        transport: Transport | None = None,
        mcp_root: str | None = None,
    ) -> None:
        self._wevibe_root = (
            wevibe_root
            or os.environ.get("WEVIBE_BENCH_WEVIBE_ROOT")
            or str(pathlib.Path(__file__).resolve().parents[3])
        )
        self._mcp_root = resolve_mcp_root(self._wevibe_root, mcp_root)
        self._cfg = cfg
        self._logger = logger
        self._transport: Transport = transport or self._urllib_transport

    @property
    def mcp_root(self) -> str:
        return self._mcp_root

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
            with urllib.request.urlopen(request, timeout=5.0) as response:
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
    def _now_ts() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    @staticmethod
    def _sanitize(value: Any) -> str:
        rendered = str(value)
        return " ".join(rendered.split())

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
        token_path = self._cfg.expanded_session_token_path()
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

    def _auth_headers(self, trace: str) -> dict[str, str]:
        token = self._read_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-WeVibe-Trace-Id": trace,
        }

    def _build_env(
        self,
        name: str,
        port: int,
        seed_hex: str,
        keystore_path: str,
        leader_wallet: str | None = None,
    ) -> dict[str, str]:
        if not _SEED_RE.fullmatch(seed_hex):
            raise ValueError("seed_hex must match ^[0-9a-fA-F]{64}$")

        env = dict(os.environ)
        env.update(
            {
                "WEVIBE_MCP_HTTP_ONLY": "1",
                "WEVIBE_MCP_HTTP_PORT": str(port),
                "WEVIBE_HTTP_HOST": "127.0.0.1",
                "WEVIBE_SEED_BACKEND": "env",
                "WEVIBE_IDENTITY_SEED_HEX": seed_hex,
                "WEVIBE_KEYSTORE_PATH": keystore_path,
                "WEVIBE_UMBRAL_SIDECAR_BIN": os.path.join(
                    self._wevibe_root,
                    "wevibe-umbral",
                    "target",
                    "release",
                    "wevibe-umbral",
                ),
                "WEVIBE_GUARD_BIN": os.path.join(
                    self._wevibe_root,
                    "wevibe-guard",
                    "target",
                    "release",
                    "wevibe-guard",
                ),
                "WEVIBE_HUB_URL": self._cfg.hub_url,
            }
        )
        if leader_wallet:
            env["WEVIBE_LEADER_WALLET"] = leader_wallet

        trace = new_trace_id()
        self._log(
            "info",
            "lifecycle.mcp.env",
            trace,
            "ok",
            0,
            name=name,
            port=port,
            seed_fp=fp(bytes.fromhex(seed_hex)),
            keystore_path=keystore_path,
        )
        return env

    def build_dist(self) -> None:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        self._log("info", "lifecycle.mcp.build_dist", trace, "ok", 0, phase="entry")

        if os.environ.get("WEVIBE_BENCH_SKIP_BUILD", "").strip() == "1":
            dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
            self._log(
                "info",
                "lifecycle.mcp.build_dist",
                trace,
                "ok",
                int(dur_ms),
                skipped=True,
                reason="WEVIBE_BENCH_SKIP_BUILD=1",
                mcp_root=self._mcp_root,
            )
            return

        mcp_dir = self._mcp_root
        result = subprocess.run(
            ["npm", "run", "build"],
            cwd=mcp_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            self._log(
                "error",
                "lifecycle.mcp.build_dist",
                trace,
                "err",
                int(dur_ms),
                rc=result.returncode,
                err=stderr,
            )
            raise RuntimeError(f"npm run build failed: {stderr or 'unknown error'}")

        self._log(
            "info",
            "lifecycle.mcp.build_dist",
            trace,
            "ok",
            int(dur_ms),
            rc=result.returncode,
        )

    def spawn(
        self,
        name: str,
        port: int,
        seed_hex: str,
        keystore_path: str,
        leader_wallet: str | None = None,
    ) -> McpInstance:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        run_root = os.path.expanduser(self._cfg.runs_dir)
        os.makedirs(run_root, exist_ok=True)
        log_path = os.path.join(run_root, f"{self._now_ts()}-{name}-mcp.log")
        env = self._build_env(name, port, seed_hex, keystore_path, leader_wallet=leader_wallet)
        cmd = ["node", os.path.join(self._mcp_root, "dist", "server.js")]

        with open(log_path, "a", encoding="utf-8") as log_file:
            process = subprocess.Popen(  # noqa: S603
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                env=env,
                start_new_session=True,
            )

        inst = McpInstance(
            name=name,
            port=port,
            seed_hex=seed_hex,
            keystore_path=keystore_path,
            log_path=log_path,
            pid=process.pid,
            url=f"http://127.0.0.1:{port}",
        )
        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        self._log(
            "info",
            "lifecycle.mcp.spawn",
            trace,
            "ok",
            int(dur_ms),
            name=name,
            port=port,
            pid=process.pid,
            seed_fp=fp(bytes.fromhex(seed_hex)),
            log_path=log_path,
        )
        return inst

    def wait_healthy(self, inst: McpInstance, timeout_s: float = 15) -> bool:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        deadline = time.time() + timeout_s
        headers = self._auth_headers(trace)
        url = f"{inst.url}/v1/health"

        while time.time() < deadline:
            status, _, reachable = self._transport(url, headers, None)
            if reachable and status == 200:
                dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
                self._log(
                    "info",
                    "lifecycle.mcp.wait_healthy",
                    trace,
                    "ok",
                    int(dur_ms),
                    name=inst.name,
                    port=inst.port,
                    http_status=status,
                )
                return True
            time.sleep(0.2)

        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        self._log(
            "error",
            "lifecycle.mcp.wait_healthy",
            trace,
            "err",
            int(dur_ms),
            name=inst.name,
            port=inst.port,
        )
        return False

    def stop(self, inst: McpInstance) -> None:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        url = f"{inst.url}/v1/shutdown"
        err: str | None = None

        try:
            headers = self._auth_headers(trace)
            self._transport(url, headers, {})
        except Exception as exc:  # pragma: no cover - best-effort shutdown path
            err = str(exc)

        try:
            os.killpg(inst.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:  # pragma: no cover - platform/path dependent
            err = str(exc)
            try:
                os.kill(inst.pid, signal.SIGTERM)
            except Exception:
                pass

        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        if err:
            self._log(
                "error",
                "lifecycle.mcp.stop",
                trace,
                "err",
                int(dur_ms),
                name=inst.name,
                pid=inst.pid,
                err=err,
            )
            return

        self._log(
            "info",
            "lifecycle.mcp.stop",
            trace,
            "ok",
            int(dur_ms),
            name=inst.name,
            pid=inst.pid,
        )

    def export_pairing(self, inst: McpInstance) -> dict[str, Any]:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        url = f"{inst.url}/v1/identity/export-pairing"
        headers = self._auth_headers(trace)
        status, payload, reachable = self._transport(url, headers, {})
        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000

        if not reachable:
            self._log(
                "error",
                "lifecycle.mcp.export_pairing",
                trace,
                "err",
                int(dur_ms),
                name=inst.name,
                reason="unreachable",
            )
            raise RuntimeError(f"export pairing unreachable for {inst.name} ({inst.url})")

        if status != 200 or not isinstance(payload, dict):
            self._log(
                "error",
                "lifecycle.mcp.export_pairing",
                trace,
                "err",
                int(dur_ms),
                name=inst.name,
                http_status=status,
            )
            raise RuntimeError(f"export pairing failed ({status}): {payload}")

        self._log(
            "info",
            "lifecycle.mcp.export_pairing",
            trace,
            "ok",
            int(dur_ms),
            name=inst.name,
            http_status=status,
            response_keys=",".join(sorted(payload.keys())),
        )
        return payload
