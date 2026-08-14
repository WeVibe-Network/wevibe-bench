"""Lifecycle orchestration for clone MCP bring-up."""

from __future__ import annotations

import contextlib
import os
import time
import urllib.parse
from typing import Any

from .hub_client import HubClient
from .identity import Identity
from .lconfig import LifecycleConfig
from .logging_util import fp, new_trace_id
from .mcp_process import (
    McpInstance,
    McpProcessManager,
)


class LifecycleOrchestrator:
    def __init__(
        self,
        cfg: LifecycleConfig,
        wevibe_root: str,
        leader: Identity,
        contributor: Identity,
        leader_keystore: str,
        contributor_keystore: str,
        leader_wallet: str,
        logger: Any,
        procman: McpProcessManager,
        *,
        hub_client: HubClient | None = None,
    ) -> None:
        self._cfg = cfg
        self._wevibe_root = wevibe_root
        self._leader = leader
        self._contributor = contributor
        self._leader_wallet = leader_wallet
        self._logger = logger
        self._procman = procman

        self._hub_client = hub_client or HubClient(cfg, logger)

        self._leader_instance: McpInstance | None = None
        self._contributor_instance: McpInstance | None = None
        self.org_id: str | None = None

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

    @staticmethod
    def _port_from_url(url: str) -> int:
        parsed = urllib.parse.urlparse(url)
        if parsed.port is None:
            raise ValueError(f"missing port in MCP URL: {url}")
        return parsed.port

    @contextlib.contextmanager
    def _bench_endpoint_flag(self):
        key = "WEVIBE_BENCH_ENDPOINTS"
        previous = os.environ.get(key)
        os.environ[key] = "1"
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    @property
    def hub_client(self) -> HubClient:
        return self._hub_client

    @property
    def leader_instance(self) -> McpInstance | None:
        return self._leader_instance

    @property
    def contributor_instance(self) -> McpInstance | None:
        return self._contributor_instance

    def bring_up(self, build: bool = False) -> tuple[McpInstance, McpInstance]:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        self._log("info", "lifecycle.orchestrator.bring_up", trace, "ok", 0, build=build, phase="start")

        if build:
            self._procman.build_dist()

        leader_port = self._port_from_url(self._cfg.leader_mcp_url)
        contributor_port = self._port_from_url(self._cfg.contributor_mcp_url)

        with self._bench_endpoint_flag():
            leader_instance = self._procman.spawn(
                name="leader",
                port=leader_port,
                leader_wallet=self._leader_wallet,
            )
            contributor_instance = self._procman.spawn(
                name="contributor",
                port=contributor_port,
            )

        if not self._procman.wait_healthy(leader_instance):
            raise RuntimeError("leader MCP failed health check")
        if not self._procman.wait_healthy(contributor_instance):
            raise RuntimeError("contributor MCP failed health check")

        self._leader_instance = leader_instance
        self._contributor_instance = contributor_instance
        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        self._log(
            "info",
            "lifecycle.orchestrator.bring_up",
            trace,
            "ok",
            int(dur_ms),
            leader_port=leader_port,
            contributor_port=contributor_port,
            leader_seed_fp=fp(bytes.fromhex(self._leader.seed_hex)),
            contributor_seed_fp=fp(bytes.fromhex(self._contributor.seed_hex)),
        )
        return leader_instance, contributor_instance
