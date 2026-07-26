"""Bootstrap M1 org lifecycle against already-running MCP clones.

This script intentionally does NOT call bring_up() and does NOT commit memories.
It only performs preflight checks and runs LifecycleOrchestrator.run_m1().
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from wevibe_bench.benv import load_bench_env
from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import (
    DEFAULT_CONTRIB_KEYSTORE_PATH,
    DEFAULT_LEADER_KEYSTORE_PATH,
    LifecycleConfig,
)
from wevibe_bench.lifecycle.logging_util import new_trace_id
from wevibe_bench.lifecycle.mcp_process import McpProcessManager
from wevibe_bench.lifecycle.orchestrator import LifecycleOrchestrator
from wevibe_bench.preflight import PreflightError, verify_org_checklist

_M1_STEPS = [
    "create_org",
    "seed_keywords",
    "contributor_pubkeys",
    "invite",
    "add_member_onchain",
    "enable_recall",
    "provision_recall",
    "poll_membership",
]


@dataclass(frozen=True)
class ResolvedArgs:
    leader_mcp_url: str
    contributor_mcp_url: str
    hub_url: str
    org_name: str
    domain: str
    dry_run: bool
    log_file: str


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required env {name}")
    return value


def _sha256_first8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _utc_iso_file_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_log_file() -> str:
    return str(Path("runs") / "bootstrap-org-m1" / f"{_utc_iso_file_stamp()}.log")


def _read_token(path: str) -> str:
    token_path = Path(path).expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PreflightError(f"preflight token read failed path={token_path}: {exc}") from exc
    if not token:
        raise PreflightError(f"preflight token file is empty path={token_path}")
    return token


def _http_get(url: str, headers: dict[str, str], timeout_s: float = 5.0) -> tuple[int, bool]:
    request = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return int(response.getcode()), True
    except urllib.error.HTTPError as exc:
        return int(exc.code), True
    except (urllib.error.URLError, OSError, socket.timeout):
        return 0, False


def _build_parser() -> argparse.ArgumentParser:
    defaults = LifecycleConfig()

    parser = argparse.ArgumentParser(description="Bootstrap M1 org lifecycle against running MCP clones")
    parser.add_argument(
        "--leader-mcp-url",
        default=(os.environ.get("WEVIBE_BENCH_LEADER_MCP_URL") or "http://127.0.0.1:4550"),
    )
    parser.add_argument(
        "--contributor-mcp-url",
        default=(os.environ.get("WEVIBE_BENCH_CONTRIB_MCP_URL") or "http://127.0.0.1:4451"),
    )
    parser.add_argument(
        "--hub-url",
        default=(os.environ.get("WEVIBE_BENCH_HUB_URL") or "http://127.0.0.1:4440"),
    )
    parser.add_argument(
        "--org-name",
        default=(os.environ.get("WEVIBE_BENCH_ORG_NAME") or defaults.org_name),
    )
    parser.add_argument(
        "--domain",
        default=(os.environ.get("WEVIBE_BENCH_ORG_DOMAIN") or defaults.domain),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--log-file",
        default=(os.environ.get("WEVIBE_BENCH_BOOTSTRAP_ORG_M1_LOG_FILE") or _default_log_file()),
    )
    return parser


def _resolve_args(argv: list[str] | None) -> ResolvedArgs:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    return ResolvedArgs(
        leader_mcp_url=str(ns.leader_mcp_url).strip(),
        contributor_mcp_url=str(ns.contributor_mcp_url).strip(),
        hub_url=str(ns.hub_url).strip(),
        org_name=str(ns.org_name).strip(),
        domain=str(ns.domain).strip(),
        dry_run=bool(ns.dry_run),
        log_file=str(ns.log_file).strip(),
    )


def _setup_logger(log_file: str) -> logging.Logger:
    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"wevibe_bench.bootstrap_org_m1.{os.getpid()}.{int(time.time() * 1000)}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.logfile_path = str(path)  # type: ignore[attr-defined]
    return logger


def _preflight(
    cfg: LifecycleConfig,
    *,
    fetch_health: Callable[[str, dict[str, str]], tuple[int, bool]] = _http_get,
) -> None:
    token = _read_token(cfg.expanded_session_token_path())
    bearer_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    leader_health = f"{cfg.leader_mcp_url.rstrip('/')}/v1/health"
    leader_status, leader_reachable = fetch_health(leader_health, bearer_headers)
    if not leader_reachable or leader_status != 200:
        raise PreflightError(
            "preflight leader health failed "
            f"url={leader_health} reachable={leader_reachable} status={leader_status} expected_status=200"
        )

    contributor_health = f"{cfg.contributor_mcp_url.rstrip('/')}/v1/health"
    contributor_status, contributor_reachable = fetch_health(contributor_health, bearer_headers)
    if not contributor_reachable or contributor_status != 200:
        raise PreflightError(
            "preflight contributor health failed "
            f"url={contributor_health} reachable={contributor_reachable} status={contributor_status} expected_status=200"
        )

    hub_health = f"{cfg.hub_url.rstrip('/')}/health"
    hub_status, hub_reachable = fetch_health(hub_health, {"Accept": "application/json"})
    if not hub_reachable:
        raise PreflightError(f"preflight hub health failed url={hub_health} reachable={hub_reachable}")
    if hub_status != 200:
        raise PreflightError(f"preflight hub health failed url={hub_health} status={hub_status} expected_status=200")

    signer_cli = Path(cfg.leader_signer_dir).expanduser() / "dist" / "cli.js"
    if not signer_cli.is_file():
        raise PreflightError(f"preflight leader-signer missing cli path={signer_cli}")


def _progress(line: str, logger: logging.Logger) -> None:
    print(line)
    logger.info(line)


def _bootstrap(
    resolved: ResolvedArgs,
    *,
    fetch_health: Callable[[str, dict[str, str]], tuple[int, bool]] = _http_get,
    orchestrator_factory: type[LifecycleOrchestrator] = LifecycleOrchestrator,
) -> int:
    logger = _setup_logger(resolved.log_file)
    bootstrap_trace = new_trace_id()

    leader = Identity.from_hex(_required_env("WEVIBE_BENCH_LEADER_SEED_HEX"))
    contributor = Identity.from_hex(_required_env("WEVIBE_BENCH_CONTRIB_SEED_HEX"))
    leader_wallet = _required_env("WEVIBE_BENCH_LEADER_WALLET")

    cfg = LifecycleConfig(
        leader_mcp_url=resolved.leader_mcp_url,
        contributor_mcp_url=resolved.contributor_mcp_url,
        hub_url=resolved.hub_url,
        org_name=resolved.org_name,
        domain=resolved.domain,
    )

    _progress(
        "BOOTSTRAP_M1_START "
        f"trace={bootstrap_trace} "
        f"leader_seed_fp={leader.seed_fp()} contributor_seed_fp={contributor.seed_fp()} "
        f"leader_wallet_fp={_sha256_first8(leader_wallet)} org_name_fp={_sha256_first8(cfg.org_name)}",
        logger,
    )
    _progress(
        "BOOTSTRAP_M1_PREFLIGHT_START "
        f"trace={bootstrap_trace} leader={cfg.leader_mcp_url} contributor={cfg.contributor_mcp_url} hub={cfg.hub_url}",
        logger,
    )

    _preflight(cfg, fetch_health=fetch_health)
    _progress("BOOTSTRAP_M1_PREFLIGHT_OK " f"trace={bootstrap_trace}", logger)

    if resolved.dry_run:
        dry_payload = {
            "trace": bootstrap_trace,
            "dry_run": True,
            "log_file": getattr(logger, "logfile_path", resolved.log_file),
            "config": {
                "leader_mcp_url": cfg.leader_mcp_url,
                "contributor_mcp_url": cfg.contributor_mcp_url,
                "hub_url": cfg.hub_url,
                "org_name": cfg.org_name,
                "domain": cfg.domain,
                "leader_signer_dir": cfg.leader_signer_dir,
                "session_token_path": cfg.session_token_path,
            },
            "plan_steps": list(_M1_STEPS),
            "fingerprints": {
                "leader_seed_fp": leader.seed_fp(),
                "contributor_seed_fp": contributor.seed_fp(),
                "leader_wallet_fp": _sha256_first8(leader_wallet),
                "org_name_fp": _sha256_first8(cfg.org_name),
                "domain_fp": _sha256_first8(cfg.domain),
            },
        }
        line = f"BOOTSTRAP_M1_DRY_RUN {json.dumps(dry_payload, sort_keys=True)}"
        _progress(line, logger)
        return 0

    wevibe_root = os.environ.get("WEVIBE_BENCH_WEVIBE_ROOT", str(Path(__file__).resolve().parents[2]))
    leader_keystore = os.environ.get("WEVIBE_BENCH_LEADER_KEYSTORE", DEFAULT_LEADER_KEYSTORE_PATH)
    contributor_keystore = os.environ.get("WEVIBE_BENCH_CONTRIB_KEYSTORE", DEFAULT_CONTRIB_KEYSTORE_PATH)

    procman = McpProcessManager(wevibe_root=wevibe_root, cfg=cfg, logger=logger)
    orchestrator = orchestrator_factory(
        cfg=cfg,
        wevibe_root=wevibe_root,
        leader=leader,
        contributor=contributor,
        leader_keystore=leader_keystore,
        contributor_keystore=contributor_keystore,
        leader_wallet=leader_wallet,
        logger=logger,
        procman=procman,
    )

    run_t0 = time.perf_counter_ns()
    try:
        result = orchestrator.run_m1()
    except Exception as exc:  # noqa: BLE001
        dur_ms = (time.perf_counter_ns() - run_t0) // 1_000_000
        _progress(
            "BOOTSTRAP_M1_FAIL "
            f"step=run_m1 trace={bootstrap_trace} dur_ms={int(dur_ms)} reason={str(exc).strip()}",
            logger,
        )
        return 1

    steps = result.get("steps") if isinstance(result, dict) else None
    if isinstance(steps, list):
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            _progress(
                "BOOTSTRAP_M1_STEP "
                f"trace={bootstrap_trace} idx={idx} step={step.get('step')} "
                f"status={step.get('status')} dur_ms={step.get('dur_ms')}",
                logger,
            )

    org_id = str(result.get("org_id", "")) if isinstance(result, dict) else ""
    org_fp = _sha256_first8(org_id) if org_id else "none"
    _progress(
        f"BOOTSTRAP_M1_OK org_id={org_id} members=2 trace={bootstrap_trace} org_id_fp={org_fp}",
        logger,
    )
    verify_org_checklist(
        hub_url=cfg.hub_url,
        org_id=org_id,
        identity=leader,
        logger=logger,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_bench_env()
    resolved = _resolve_args(argv)
    try:
        return _bootstrap(resolved)
    except PreflightError as exc:
        logger = _setup_logger(resolved.log_file)
        _progress(f"BOOTSTRAP_M1_FAIL step=preflight reason={str(exc).strip()}", logger)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger = _setup_logger(resolved.log_file)
        _progress(f"BOOTSTRAP_M1_FAIL step=setup reason={str(exc).strip()}", logger)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
