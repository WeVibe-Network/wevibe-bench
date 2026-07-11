"""Thin subprocess wrappers for the wevibe-admin CLI."""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Callable

from .logging_util import new_trace_id
from .mcp_root import resolve_mcp_root


Runner = Callable[..., subprocess.CompletedProcess[str]]
_ORG_CREATED_RE = re.compile(r"Org created:\s*(\S+)")


class AdminCli:
    def __init__(
        self,
        wevibe_root: str,
        env: dict[str, str],
        logger: Any,
        runner: Runner = subprocess.run,
        mcp_root: str | None = None,
    ) -> None:
        self._wevibe_root = wevibe_root
        self._mcp_root = resolve_mcp_root(self._wevibe_root, mcp_root)
        self._env = dict(env)
        self._logger = logger
        self._runner = runner
        self._admin_js = os.path.join(self._mcp_root, "dist", "admin.js")
        self._cwd = self._mcp_root

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

    def _run(self, op: str, args: list[str], extra_env: dict[str, str] | None = None) -> str:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        cmd = ["node", self._admin_js, *args]
        self._log("info", op, trace, "ok", 0, phase="entry", command=args[0])

        env = dict(os.environ)
        env.update(self._env)
        if extra_env:
            env.update(extra_env)

        result = self._runner(
            cmd,
            cwd=self._cwd,
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
                op,
                trace,
                "err",
                int(dur_ms),
                rc=result.returncode,
                err=stderr,
            )
            raise RuntimeError(
                f"wevibe-admin {' '.join(args)} failed rc={result.returncode}: {stderr or 'unknown error'}"
            )

        self._log(
            "info",
            op,
            trace,
            "ok",
            int(dur_ms),
            rc=result.returncode,
        )
        return result.stdout

    def create_org(self, org_name: str, domain: str, leader_wallet: str) -> dict[str, str]:
        stdout = self._run(
            "lifecycle.admin.create_org",
            ["create-org", "--name", org_name, "--domain", domain],
            extra_env={"WEVIBE_LEADER_WALLET": leader_wallet},
        )
        match = _ORG_CREATED_RE.search(stdout)
        if not match:
            raise RuntimeError("unable to parse org id from create-org output")
        return {
            "org_id": match.group(1),
            "stdout": stdout,
        }

    def invite(
        self,
        org_id: str,
        invitee_pubkey: str,
        invitee_x25519: str,
        invitee_pre_pubkey: str,
        can_contribute: bool = True,
        can_moderate: bool = False,
    ) -> str:
        return self._run(
            "lifecycle.admin.invite",
            [
                "invite",
                "--org",
                org_id,
                "--pubkey",
                invitee_pubkey,
                "--x25519",
                invitee_x25519,
                "--pre-pubkey",
                invitee_pre_pubkey,
                "--can-contribute",
                "true" if can_contribute else "false",
                "--can-moderate",
                "true" if can_moderate else "false",
            ],
        )

    def provision_recall(self, org_id: str) -> str:
        return self._run("lifecycle.admin.provision_recall", ["provision-recall", "--org", org_id])

    def moderate_queue(self, org_id: str | None = None) -> str:
        args = ["moderate-queue"]
        if org_id:
            args.extend(["--org", org_id])
        return self._run("lifecycle.admin.moderate_queue", args)

    def moderate_approve(
        self,
        org_id: str,
        submission_hash: str,
        **extra_flags: str | bool,
    ) -> str:
        args = ["moderate-approve", "--hash", submission_hash, "--org", org_id]
        for key, value in extra_flags.items():
            args.append(f"--{key.replace('_', '-')}")
            args.append("true" if value is True else "false" if value is False else str(value))
        return self._run("lifecycle.admin.moderate_approve", args)
