"""Lifecycle milestone-1 orchestration for clone MCP bring-up + org bootstrap."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
import urllib.parse
from typing import Any, Callable

from .admin_cli import AdminCli
from .hub_client import HubClient
from .identity import Identity
from .lconfig import LifecycleConfig
from .logging_util import fp, new_trace_id
from .mcp_process import McpInstance, McpProcessManager, _require_bench_identity_store
from .mcp_rest import McpRest


AdminCliFactory = Callable[[dict[str, str]], Any]
McpRestFactory = Callable[[str], Any]
Runner = Callable[..., subprocess.CompletedProcess[str]]


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
        admin_cli_factory: AdminCliFactory | None = None,
        hub_client: HubClient | None = None,
        mcp_rest_factory: McpRestFactory | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        run_cmd: Runner = subprocess.run,
    ) -> None:
        self._cfg = cfg
        self._wevibe_root = wevibe_root
        self._leader = leader
        self._contributor = contributor
        self._leader_wallet = leader_wallet
        self._logger = logger
        self._procman = procman
        self._sleep = sleep_fn
        self._run_cmd = run_cmd

        self._hub_client = hub_client or HubClient(cfg, logger)
        self._admin_cli_factory = admin_cli_factory or (
            lambda env: AdminCli(self._wevibe_root, env, self._logger)
        )
        self._mcp_rest_factory = mcp_rest_factory or (
            lambda base_url: McpRest(base_url, self._cfg, self._logger)
        )

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

    @staticmethod
    def _parse_last_json_line(stdout: str, command_name: str) -> dict[str, Any]:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(f"{command_name} returned empty stdout")

        tail = lines[-1]
        try:
            payload = json.loads(tail)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{command_name} last stdout line is not valid JSON: {tail}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError(f"{command_name} last stdout line must decode to object: {tail}")
        return payload

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

    def _leader_admin_env(self) -> dict[str, str]:
        identity_home, keystore_path = _require_bench_identity_store()
        env = dict(os.environ)
        env.update(
            {
                "WEVIBE_SEED_BACKEND": "file",
                "WEVIBE_HOME": identity_home,
                "WEVIBE_KEYSTORE_PATH": keystore_path,
                "WEVIBE_HUB_URL": self._cfg.hub_url,
                "WEVIBE_LEADER_WALLET": self._leader_wallet,
                "WEVIBE_BENCH_ENDPOINTS": "1",
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
            }
        )
        if self._leader_instance is not None:
            env["WEVIBE_MCP_HTTP_PORT"] = str(self._leader_instance.port)
            env["WEVIBE_HTTP_HOST"] = "127.0.0.1"
        return env

    def _resolve_owned_org(self) -> str | None:
        trace = new_trace_id()
        cfg_org_id = getattr(self._cfg, "org_id", None)
        if not isinstance(cfg_org_id, str) or not cfg_org_id:
            cfg_org_id = None

        try:
            payload = self._hub_client.member_orgs(self._leader)
            org_ids = self._extract_org_ids(payload)
        except Exception as exc:
            self._log(
                "info",
                "lifecycle.orchestrator.create_org",
                trace,
                "ok",
                0,
                phase="resolve_owned_org_failed",
                error=str(exc),
            )
            self._log(
                "info",
                "lifecycle.orchestrator.create_org",
                trace,
                "ok",
                0,
                phase="owned_org_resolved",
                org_id="none",
            )
            return None

        resolved: str | None
        if not org_ids:
            resolved = None
        elif cfg_org_id:
            # Explicit pin: reuse when owned; create fresh when not (never fall
            # through to sorted-first, which would target the wrong org).
            resolved = cfg_org_id if cfg_org_id in org_ids else None
        elif len(org_ids) == 1:
            resolved = next(iter(org_ids))
        else:
            sorted_org_ids = sorted(org_ids)
            resolved = sorted_org_ids[0]
            self._log(
                "info",
                "lifecycle.orchestrator.create_org",
                trace,
                "ok",
                0,
                phase="owned_org_multiple",
                selected_org_id=resolved,
                org_ids=sorted_org_ids,
            )

        self._log(
            "info",
            "lifecycle.orchestrator.create_org",
            trace,
            "ok",
            0,
            phase="owned_org_resolved",
            org_id=resolved or "none",
        )
        return resolved

    def _step(
        self,
        steps: list[dict[str, Any]],
        step_name: str,
        fn: Callable[[], Any],
    ) -> Any:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        self._log("info", "lifecycle.orchestrator.m1", trace, "ok", 0, step=step_name, phase="start")
        try:
            result = fn()
        except Exception as exc:
            dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
            self._log(
                "error",
                "lifecycle.orchestrator.m1",
                trace,
                "err",
                int(dur_ms),
                step=step_name,
                err=str(exc),
            )
            steps.append({"step": step_name, "status": "err", "dur_ms": int(dur_ms), "error": str(exc)})
            raise

        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        self._log("info", "lifecycle.orchestrator.m1", trace, "ok", int(dur_ms), step=step_name)
        steps.append({"step": step_name, "status": "ok", "dur_ms": int(dur_ms)})
        return result

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

    def create_org(self) -> str:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        self._log("info", "lifecycle.orchestrator.create_org", trace, "ok", 0, phase="start")
        owned = self._resolve_owned_org()

        signer_dir = os.path.expanduser(self._cfg.leader_signer_dir)
        signer_cli = os.path.join(signer_dir, "dist", "cli.js")

        if len(self._cfg.org_description) > 500:
            raise ValueError("org_description exceeds limit=500")
        if len(self._cfg.org_tech_stack) > 200:
            raise ValueError("org_tech_stack exceeds limit=200")
        if len(self._cfg.org_focus_areas) > 200:
            raise ValueError("org_focus_areas exceeds limit=200")

        cmd = [
            "node",
            signer_cli,
            "register-org",
            "--org-name",
            self._cfg.org_name,
            "--domain",
            self._cfg.domain,
        ]
        if self._cfg.org_description.strip():
            cmd.extend(["--description", self._cfg.org_description])
        if self._cfg.org_tech_stack.strip():
            cmd.extend(["--tech-stack", self._cfg.org_tech_stack])
        if self._cfg.org_focus_areas.strip():
            cmd.extend(["--focus-areas", self._cfg.org_focus_areas])
        if owned:
            self._log(
                "info",
                "lifecycle.orchestrator.create_org",
                trace,
                "ok",
                0,
                phase="reuse",
                org_id=owned,
            )
            cmd.extend(["--org-id", owned])
        env = dict(os.environ)
        env.update(
            {
                "WEVIBE_IDENTITY_SEED_HEX": self._leader.seed_hex,
                "HUB_URL": self._cfg.hub_url,
                "WEVIBE_MCP_URL": self._cfg.leader_mcp_url,
                "WEVIBE_MCP_TOKEN_FILE": self._cfg.expanded_session_token_path(),
                "WEVIBE_CHAIN_RPC": "http://localhost:26657",
                "WEVIBE_CHAIN_REST": "http://localhost:1317",
            }
        )

        result = self._run_cmd(
            cmd,
            cwd=signer_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            self._log(
                "error",
                "lifecycle.orchestrator.create_org",
                trace,
                "err",
                int(dur_ms),
                rc=result.returncode,
                err=stderr,
            )
            raise RuntimeError(
                f"leader-signer register-org failed rc={result.returncode}: {stderr or 'unknown error'}"
            )

        response = self._parse_last_json_line(result.stdout or "", "leader-signer register-org")
        org_id = response.get("org_id")
        if not isinstance(org_id, str) or not org_id:
            raise RuntimeError(f"leader-signer register-org missing org_id: {response}")

        tx_hash = response.get("tx_hash")
        if not isinstance(tx_hash, str) or not tx_hash:
            raise RuntimeError(f"leader-signer register-org missing tx_hash: {response}")

        self.org_id = org_id
        self._log(
            "info",
            "lifecycle.orchestrator.create_org",
            trace,
            "ok",
            int(dur_ms),
            org_id=org_id,
            tx_hash=tx_hash,
            leader_wallet=response.get("leader_wallet"),
        )
        return self.org_id

    def contributor_pubkeys(self) -> dict[str, Any]:
        client = self._mcp_rest_factory(self._cfg.contributor_mcp_url)
        payload = client.identity_pubkeys()
        required = {"ed25519", "x25519", "pre_pubkey"}
        if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in required):
            raise RuntimeError(f"contributor pubkeys missing fields: {payload}")
        return payload

    def invite_contributor(self, org_id: str, contributor_pk: dict[str, Any]) -> Any:
        cli = self._admin_cli_factory(self._leader_admin_env())
        return cli.invite(
            org_id=org_id,
            invitee_pubkey=str(contributor_pk["ed25519"]),
            invitee_x25519=str(contributor_pk["x25519"]),
            invitee_pre_pubkey=str(contributor_pk["pre_pubkey"]),
            can_contribute=True,
        )

    def add_member_onchain(self, org_id: str, contributor_pk: dict[str, Any]) -> str:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        self._log(
            "info",
            "lifecycle.orchestrator.add_member_onchain",
            trace,
            "ok",
            0,
            phase="start",
            org_id=org_id,
        )
        cfg_org_id = getattr(self._cfg, "org_id", None)
        if isinstance(cfg_org_id, str) and cfg_org_id:
            try:
                already = org_id in self._extract_org_ids(self._hub_client.member_orgs(self._contributor))
            except Exception:
                already = False
        else:
            already = False

        if already:
            self._log(
                "info",
                "lifecycle.orchestrator.add_member_onchain",
                trace,
                "ok",
                0,
                phase="already_member",
                org_id=org_id,
            )
            return "already-member"

        signer_dir = os.path.expanduser(self._cfg.leader_signer_dir)
        signer_cli = os.path.join(signer_dir, "dist", "cli.js")
        cmd = [
            "node",
            signer_cli,
            "add-member",
            "--org-id",
            org_id,
            "--member-pubkey",
            str(contributor_pk["ed25519"]),
            "--x25519",
            str(contributor_pk["x25519"]),
            "--role",
            "member",
            "--can-contribute",
            "true",
            "--can-moderate",
            "false",
        ]
        env = dict(os.environ)
        env.update(
            {
                "WEVIBE_IDENTITY_SEED_HEX": self._leader.seed_hex,
                "WEVIBE_CHAIN_RPC": "http://localhost:26657",
            }
        )

        result = self._run_cmd(
            cmd,
            cwd=signer_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            self._log(
                "error",
                "lifecycle.orchestrator.add_member_onchain",
                trace,
                "err",
                int(dur_ms),
                rc=result.returncode,
                err=stderr,
            )
            raise RuntimeError(
                f"leader-signer add-member failed rc={result.returncode}: {stderr or 'unknown error'}"
            )

        response = self._parse_last_json_line(result.stdout or "", "leader-signer add-member")
        code = response.get("code")
        if not isinstance(code, int):
            raise RuntimeError(f"leader-signer add-member missing numeric code: {response}")
        if code != 0:
            raise RuntimeError(f"leader-signer add-member returned code={code}: {response}")

        tx_hash = response.get("tx_hash")
        if not isinstance(tx_hash, str) or not tx_hash:
            raise RuntimeError(f"leader-signer add-member missing tx_hash: {response}")

        self._log(
            "info",
            "lifecycle.orchestrator.add_member_onchain",
            trace,
            "ok",
            int(dur_ms),
            org_id=org_id,
            tx_hash=tx_hash,
            code=code,
        )
        return tx_hash

    def enable_recall(self, org_id: str, member_pubkey: str) -> Any:
        return self._hub_client.enable_recall(self._leader, org_id, member_pubkey, free=True)

    def seed_keywords(self, org_id: str) -> int:
        count = 0
        for keyword in self._cfg.org_keywords:
            self._hub_client.add_keyword(self._leader, org_id, keyword)
            count += 1
        self._log(
            "info",
            "lifecycle.orchestrator.seed_keywords",
            new_trace_id(),
            "ok",
            0,
            org_id=org_id,
            org_id_fp=fp(org_id),
            keyword_count=count,
        )
        return count

    def provision_recall(self, org_id: str) -> Any:
        cli = self._admin_cli_factory(self._leader_admin_env())
        return cli.provision_recall(org_id)

    @staticmethod
    def _extract_org_ids(payload: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(payload, str):
            found.add(payload)
            return found

        if isinstance(payload, list):
            for item in payload:
                found.update(LifecycleOrchestrator._extract_org_ids(item))
            return found

        if isinstance(payload, dict):
            for key in ("org_id", "id"):
                value = payload.get(key)
                if isinstance(value, str):
                    found.add(value)
            for key in ("orgs", "items", "memberships", "data", "results"):
                nested = payload.get(key)
                if isinstance(nested, list):
                    for item in nested:
                        found.update(LifecycleOrchestrator._extract_org_ids(item))
        return found

    def poll_membership(self, identity: Identity, org_id: str, timeout: float = 30) -> bool:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = self._hub_client.member_orgs(identity)
            if org_id in self._extract_org_ids(payload):
                dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
                self._log(
                    "info",
                    "lifecycle.orchestrator.poll_membership",
                    trace,
                    "ok",
                    int(dur_ms),
                    org_id=org_id,
                )
                return True
            self._sleep(0.5)

        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        self._log(
            "error",
            "lifecycle.orchestrator.poll_membership",
            trace,
            "err",
            int(dur_ms),
            org_id=org_id,
        )
        return False

    def run_m1(self) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        org_id = self._step(steps, "create_org", self.create_org)
        self._step(steps, "seed_keywords", lambda: self.seed_keywords(org_id))
        contributor_pk = self._step(steps, "contributor_pubkeys", self.contributor_pubkeys)
        try:
            already_member = org_id in self._extract_org_ids(
                self._hub_client.member_orgs(self._contributor)
            )
        except Exception:
            already_member = False
        if already_member:
            self._log(
                "info",
                "lifecycle.orchestrator.m1",
                new_trace_id(),
                "ok",
                0,
                phase="skip_membership_create",
                org_id=org_id,
            )
        else:
            self._step(steps, "invite", lambda: self.invite_contributor(org_id, contributor_pk))
            self._step(steps, "add_member_onchain", lambda: self.add_member_onchain(org_id, contributor_pk))
        self._step(
            steps,
            "enable_recall",
            lambda: self.enable_recall(org_id, str(contributor_pk["ed25519"])),
        )
        self._step(steps, "provision_recall", lambda: self.provision_recall(org_id))
        membership_ok = self._step(
            steps,
            "poll_membership",
            lambda: self.poll_membership(self._contributor, org_id),
        )
        if not membership_ok:
            raise RuntimeError(f"membership did not include org_id={org_id}")

        self.org_id = org_id
        return {
            "org_id": org_id,
            "contributor_pk": contributor_pk,
            "steps": steps,
        }
