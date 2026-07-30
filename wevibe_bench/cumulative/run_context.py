"""Run-start context capture for cumulative benchmark comparability."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import subprocess
from typing import Any, Mapping, Sequence

_LOG = logging.getLogger(__name__)

ALLOW_MISSING_ENV = "WEVIBE_BENCH_ALLOW_MISSING_RUN_CONTEXT"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_command(args: Sequence[str], *, timeout_s: int = 10) -> str:
    _LOG.info("op=run_context.command_start argv=%s timeout_s=%d", " ".join(args), timeout_s)
    try:
        completed = subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.CalledProcessError as exc:
        _LOG.error(
            "op=run_context.command_failed argv=%s returncode=%s stdout=%r stderr=%r",
            " ".join(args),
            exc.returncode,
            exc.stdout,
            exc.stderr,
        )
        raise RuntimeError(f"run context command failed: {' '.join(args)}") from exc
    except subprocess.TimeoutExpired as exc:
        _LOG.error(
            "op=run_context.command_timeout argv=%s timeout_s=%d stdout=%r stderr=%r",
            " ".join(args),
            timeout_s,
            exc.stdout,
            exc.stderr,
        )
        raise RuntimeError(f"run context command timed out: {' '.join(args)}") from exc

    _LOG.info(
        "op=run_context.command_ok argv=%s stdout_bytes=%d stderr_bytes=%d",
        " ".join(args),
        len(completed.stdout or ""),
        len(completed.stderr or ""),
    )
    return completed.stdout


def _parse_env_output(output: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _latest_policy_anchor_payload(logs: str) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    for line in logs.splitlines():
        if "hub.policy_anchor" not in line:
            continue
        start = line.find("{")
        if start < 0:
            continue
        try:
            decoded = json.loads(line[start:])
        except json.JSONDecodeError:
            _LOG.warning("op=run_context.policy_anchor_parse_skip line=%r", line)
            continue
        if isinstance(decoded, dict) and decoded.get("op") == "hub.policy_anchor":
            latest = decoded

    if latest is None:
        raise RuntimeError("no parseable hub.policy_anchor log line found in wevibe-hub logs")
    return latest


def parse_policy_anchor_log_line(line: str) -> dict[str, Any]:
    """Parse one structured hub.policy_anchor log line for unit tests and callers."""

    payload = _latest_policy_anchor_payload(line)
    return {
        "version": str(payload.get("policy_version", "")),
        "hash": str(payload.get("policy_hash", "")),
        "anchor_status": str(payload.get("status", "")),
        "observed_at": str(payload.get("ts") or payload.get("time") or _utc_now_iso()),
    }


def _lever(value: Any, source: str) -> dict[str, Any]:
    return {"value": value, "source": source}


def _required_env_lever(env: Mapping[str, str], key: str) -> str:
    value = str(env.get(key, "")).strip()
    if not value:
        raise RuntimeError(f"hub env missing required recall lever {key}")
    return value


def _collect_available() -> dict[str, Any]:
    env_output = _run_command(["docker", "exec", "wevibe-hub", "env"])
    hub_env = _parse_env_output(env_output)

    logs_output = _run_command(["docker", "logs", "wevibe-hub"], timeout_s=20)
    edge_policy = parse_policy_anchor_log_line(logs_output)

    levers = {
        "L1_relevance_floor": _lever("0.55", "documented-default"),
        "L2_surface_budget": _lever("3", "documented-default"),
        "L3_recall_limit": _lever("3", "documented-default"),
        "L4_WEVIBE_RECALL_MODE": _lever(
            _required_env_lever(hub_env, "WEVIBE_RECALL_MODE"), "hub-env"
        ),
        "L6_gamma": _lever("0.1", "compiled-const"),
        "L7_delta": _lever("0.15", "compiled-const"),
        "L8_RETRIEVAL_TEMPERATURE": _lever(
            _required_env_lever(hub_env, "RETRIEVAL_TEMPERATURE"), "hub-env"
        ),
        "L9_RETRIEVAL_NEW_MEM_BOOST_MULT": _lever(
            _required_env_lever(hub_env, "RETRIEVAL_NEW_MEM_BOOST_MULT"), "hub-env"
        ),
        "L10_RETRIEVAL_NEW_MEM_BOOST_WINDOW": _lever(
            _required_env_lever(hub_env, "RETRIEVAL_NEW_MEM_BOOST_WINDOW"), "hub-env"
        ),
        "L11_contestedThreshold": _lever("0.20", "compiled-const"),
    }

    return {
        "status": "available",
        "collected_at": _utc_now_iso(),
        "levers": levers,
        "edge_policy": edge_policy,
    }


def collect_run_context() -> dict[str, Any]:
    """Collect the frozen-by-record recall/policy context for a bench run."""

    _LOG.info("op=run_context.collect_start")
    try:
        context = _collect_available()
    except Exception as exc:
        _LOG.error("op=run_context.collect_failed error=%r", exc)
        if os.environ.get(ALLOW_MISSING_ENV) == "1":
            _LOG.warning(
                "op=run_context.missing_allowed env=%s status=unavailable error=%r",
                ALLOW_MISSING_ENV,
                exc,
            )
            return {
                "status": "unavailable",
                "collected_at": _utc_now_iso(),
                "error": str(exc),
                "levers": {},
                "edge_policy": None,
            }
        raise

    policy = context.get("edge_policy") or {}
    _LOG.info(
        "op=run_context.collect_ok policy_version=%s policy_hash=%s anchor_status=%s lever_count=%d",
        policy.get("version"),
        policy.get("hash"),
        policy.get("anchor_status"),
        len(context.get("levers", {})),
    )
    return context


def compare_run_context(recorded: Mapping[str, Any] | None, current: Mapping[str, Any]) -> list[str]:
    """Return dotted keys whose recorded run context differs from current context."""

    if not recorded:
        return ["run_context"]

    drift: list[str] = []
    for key in ("status", "levers", "edge_policy"):
        if recorded.get(key) != current.get(key):
            if key == "levers" and isinstance(recorded.get(key), Mapping) and isinstance(current.get(key), Mapping):
                names = sorted(set(recorded[key]) | set(current[key]))
                for name in names:
                    if recorded[key].get(name) != current[key].get(name):
                        drift.append(f"levers.{name}")
            elif key == "edge_policy" and isinstance(recorded.get(key), Mapping) and isinstance(current.get(key), Mapping):
                names = sorted(set(recorded[key]) | set(current[key]))
                for name in names:
                    if name == "observed_at":
                        continue
                    if recorded[key].get(name) != current[key].get(name):
                        drift.append(f"edge_policy.{name}")
            else:
                drift.append(key)
    return drift


__all__ = [
    "collect_run_context",
    "compare_run_context",
    "parse_policy_anchor_log_line",
]
