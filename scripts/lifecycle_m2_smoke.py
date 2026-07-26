"""Lifecycle M1+M2 smoke runner for clone-MCP endpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import (
    DEFAULT_CONTRIB_KEYSTORE_PATH,
    DEFAULT_LEADER_KEYSTORE_PATH,
    LifecycleConfig,
)
from wevibe_bench.lifecycle.logging_util import run_logger
from wevibe_bench.lifecycle.m2_proof import M2Proof
from wevibe_bench.lifecycle.mcp_process import McpProcessManager
from wevibe_bench.lifecycle.orchestrator import LifecycleOrchestrator


def _load_identity(env_name: str) -> Identity:
    seed_hex = os.environ.get(env_name, "").strip()
    if seed_hex:
        identity = Identity.from_hex(seed_hex)
        print(f"{env_name} provided seed_fp={identity.seed_fp()} ed_pub_fp={identity.ed_pub_fp()}")
        return identity

    identity = Identity.generate()
    print(
        f"{env_name} missing -> generated ephemeral identity "
        f"seed_fp={identity.seed_fp()} ed_pub_fp={identity.ed_pub_fp()}"
    )
    return identity


def _project_context() -> dict[str, Any]:
    raw = os.environ.get("WEVIBE_BENCH_PROJECT_CONTEXT_JSON", "{}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WEVIBE_BENCH_PROJECT_CONTEXT_JSON must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("WEVIBE_BENCH_PROJECT_CONTEXT_JSON must decode to an object")
    return parsed


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required env {name}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def main() -> int:
    leader = _load_identity("WEVIBE_BENCH_LEADER_SEED_HEX")
    contributor = _load_identity("WEVIBE_BENCH_CONTRIB_SEED_HEX")

    direct_text = os.environ.get("WEVIBE_BENCH_DIRECT_MEMORY_TEXT", "").strip()
    direct_keywords_raw = os.environ.get("WEVIBE_BENCH_DIRECT_MEMORY_KEYWORDS", "")
    direct_stack_raw = os.environ.get("WEVIBE_BENCH_DIRECT_MEMORY_STACK")

    events: list[dict[str, Any]] = []
    events_path: Path | None = None
    direct_memory: dict[str, Any] | None = None
    if direct_text:
        direct_keywords = [part.strip() for part in direct_keywords_raw.split(",") if part.strip()]
        stack_hint = direct_stack_raw.strip() if isinstance(direct_stack_raw, str) and direct_stack_raw.strip() else None
        direct_memory = {
            "text": direct_text,
            "keywords": direct_keywords,
            "stack_hint": stack_hint,
        }
    else:
        events_path = Path(_required_env("WEVIBE_BENCH_EVENTS_PATH")).expanduser()
        if not events_path.is_file():
            raise RuntimeError(f"events file not found: {events_path}")
        raw_events = json.loads(events_path.read_text(encoding="utf-8"))
        if not isinstance(raw_events, list) or not raw_events:
            raise RuntimeError(f"events file must hold a non-empty JSON array: {events_path}")
        events = raw_events

    model = os.environ.get("WEVIBE_BENCH_MODEL", "openai/gpt-5.3-codex")
    api_key = os.environ.get("WEVIBE_BENCH_API_KEY", "")

    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    logger = run_logger("lifecycle-m2", cfg.runs_dir)
    logfile = getattr(logger, "logfile_path", "")

    wevibe_root = os.environ.get(
        "WEVIBE_BENCH_WEVIBE_ROOT",
        str(Path(__file__).resolve().parents[2]),
    )
    leader_keystore = os.environ.get(
        "WEVIBE_BENCH_LEADER_KEYSTORE",
        DEFAULT_LEADER_KEYSTORE_PATH,
    )
    contributor_keystore = os.environ.get(
        "WEVIBE_BENCH_CONTRIB_KEYSTORE",
        DEFAULT_CONTRIB_KEYSTORE_PATH,
    )
    leader_wallet = _required_env("WEVIBE_BENCH_LEADER_WALLET")

    logger.info(
        "op=lifecycle.m2.smoke.start wevibe_root=%s events=%s model=%s logfile=%s",
        wevibe_root,
        events_path if events_path is not None else "<direct_memory>",
        model,
        logfile,
    )

    procman = McpProcessManager(wevibe_root=wevibe_root, cfg=cfg, logger=logger)
    orchestrator = LifecycleOrchestrator(
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
    proof = M2Proof(
        cfg=cfg,
        orchestrator=orchestrator,
        leader=leader,
        contributor=contributor,
        logger=logger,
        direct_memory=direct_memory,
    )

    leader_instance = None
    contributor_instance = None
    try:
        leader_instance, contributor_instance = orchestrator.bring_up(build=_bool_env("WEVIBE_BENCH_BUILD_DIST"))
        m1_result = orchestrator.run_m1()
        m2_result = proof.run(
            events=events,
            model=model,
            api_key=api_key,
            project_context=_project_context(),
        )
        payload = {
            "m1": m1_result,
            "m2": m2_result,
            "logfile": logfile,
        }
        tail = json.dumps(payload, sort_keys=True)
        logger.info("M2_RESULT_JSON %s", tail)
        print(f"M2_RESULT_JSON {tail}")
        return 0
    finally:
        if contributor_instance is not None:
            procman.stop(contributor_instance)
        if leader_instance is not None:
            procman.stop(leader_instance)


if __name__ == "__main__":
    raise SystemExit(main())
