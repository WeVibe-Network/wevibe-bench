#!/usr/bin/env python3
"""Canonical primary scored cumulative benchmark CLI.

This script is **THE** canonical primary scored cumulative path for WeVibe.
`scripts/run_aider_solve.py` (Path C) and `scripts/backgammon_scored_ladder.py`
are diagnostic/historical paths and are **not** the active primary path.
"""

from __future__ import annotations

import argparse
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, Mapping, NamedTuple

from wevibe_bench import config
from wevibe_bench.benv import load_bench_env
from wevibe_bench.cumulative.bridge_state import atomic_write_state, load_state
from wevibe_bench.cumulative.consumer_bridge import (
    DEFAULT_HEARTBEAT_CADENCE_MS,
    DEFAULT_LEASE_TTL_MS,
    DEFAULT_POLL_INTERVAL_MS,
    ConsumerBridge,
    scope_key,
)
from wevibe_bench.cumulative.catalog import PrivateCatalog, PrivateReviewCard
from wevibe_bench.cumulative.consumer_decision import (
    CONSUMER_DECISION_SCHEMA_VERSION,
    DEFAULT_PRIMARY_POLICY,
    ConsumerCandidateDecision,
    ConsumerDecisionManifest,
    default_primary_manifest,
    validate_correlation,
    validate_one_per_candidate,
    validate_schema,
)
from wevibe_bench.cumulative.consumer_gate import (
    ConsumerGateCoordinator,
    default_plugin_state_dir,
)
from wevibe_bench.cumulative.decision import (
    CandidateDecision,
    DecisionManifest,
    IntegrityAttestation,
)
from wevibe_bench.cumulative.leader_client import LeaderClient
from wevibe_bench.cumulative.manifest import roster_hash as cumulative_roster_hash
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
from wevibe_bench.cumulative.types import (
    ConsumerGateRecord,
    PhaseGroup,
    RosterEntry,
    SessionRecord,
)
from wevibe_bench.lifecycle.hub_client import HubClient
from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import LifecycleConfig
from wevibe_bench.lifecycle.m2_proof import M2Proof
from wevibe_bench.lifecycle.mcp_rest import McpRest

IS_PRIMARY_SCORED_PATH = True
_LOG = logging.getLogger("run_cumulative")

DEFAULT_MANIFEST_PATH = Path("runs") / "cumulative" / "manifest.json"
DEFAULT_TASK_LABEL = "backgammon-cumulative-primary"
DEFAULT_ORG_ID = "wevibe-org-0"
DEFAULT_EXTRACT_TIMEOUT_S = 900
DEFAULT_SEED = config.RunConfig().rng_seed
DEFAULT_ON_BUDGET = 0

CONSUMER_STATE_DIR_ENV = "WEVIBE_BENCH_CONSUMER_STATE_DIR"
CONSUMER_SCOPED_WEVIBE_DIR_ENV = "WEVIBE_BENCH_CONSUMER_SCOPED_WEVIBE_DIR"


class PathLayout(NamedTuple):
    manifest_path: Path
    runs_dir: Path
    catalog_path: Path
    review_card_path: Path
    safe_ledger_path: Path
    idempotency_ledger_path: Path


class BridgePaths(NamedTuple):
    base_dir: Path
    state_path: Path
    pidfile: Path
    inbox: Path
    logdir: Path
    served_store_path: Path
    consumer_state_dir: Path


class BridgeRuntimeConfig(NamedTuple):
    layout: PathLayout
    bridge_paths: BridgePaths
    manifest_inbox: Path
    consumer_state_dir: Path
    served_store_path: Path


class _SessionRunState:
    def __init__(self, *, run_label: str, run_dir: Path, last_session_id: str | None = None) -> None:
        self.run_label = run_label
        self.run_dir = run_dir
        self.last_session_id = last_session_id


class CliContext(NamedTuple):
    sequencer: CumulativeSequencer
    leader_client: LeaderClient
    review_card: PrivateReviewCard
    layout: PathLayout


BuildSubstrateEvents = Callable[
    ...,
    tuple[list[dict[str, Any]], dict[str, Any], list[Path]],
]
SessionIdCounts = Callable[[Path], dict[str, int]]
SessionIdFromEvents = Callable[..., str | None]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required env {name}")
    return value


def _load_required_text(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"required file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"required file empty: {path}")
    return text


def _resolve_manifest_layout(manifest_arg: str) -> PathLayout:
    manifest_path = Path(manifest_arg).expanduser().resolve()
    runs_dir = manifest_path.parent
    stem = manifest_path.stem or "manifest"
    return PathLayout(
        manifest_path=manifest_path,
        runs_dir=runs_dir,
        catalog_path=runs_dir / f"{stem}.catalog.jsonl",
        review_card_path=runs_dir / f"{stem}.review.jsonl",
        safe_ledger_path=runs_dir / f"{stem}.safe-ledger.jsonl",
        idempotency_ledger_path=runs_dir / f"{stem}.idempotency-ledger.json",
    )


def _normalize_model_slug(model: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", model).strip("-")
    return slug or "model"


def _provider_pin_from_model(model: str) -> str:
    parts = [part for part in model.split("/") if part]
    if not parts:
        raise ValueError("model slug must be non-empty")
    if len(parts) >= 2 and parts[0] == "openrouter":
        return parts[1]
    return parts[0]


def _build_roster() -> tuple[list[RosterEntry], str]:
    roster: list[RosterEntry] = []
    for rung in config.backgammon_scored_ladder_roster():
        model = str(rung.model)
        roster.append(
            RosterEntry(
                model=model,
                role=str(rung.role),
                provider_pin=_provider_pin_from_model(model),
                config_identity={
                    "memory_modes": [str(mode) for mode in rung.memory_modes],
                    "recorded_class": rung.recorded_class,
                },
            )
        )
    if not roster:
        raise RuntimeError("backgammon_scored_ladder_roster resolved empty")
    return roster, cumulative_roster_hash(roster)


def _resolve_extract_base_url() -> str | None:
    value = os.environ.get("WEVIBE_BENCH_EXTRACT_BASE_URL", "").strip()
    return value or None


def _resolve_extract_num_ctx() -> int | None:
    raw = os.environ.get("WEVIBE_BENCH_EXTRACT_NUM_CTX", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("WEVIBE_BENCH_EXTRACT_NUM_CTX must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError("WEVIBE_BENCH_EXTRACT_NUM_CTX must be a positive integer")
    return value


def _resolve_extract_timeout_s() -> int:
    raw = os.environ.get("WEVIBE_BENCH_EXTRACT_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_EXTRACT_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("WEVIBE_BENCH_EXTRACT_TIMEOUT_S must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError("WEVIBE_BENCH_EXTRACT_TIMEOUT_S must be a positive integer")
    return value


def _resolve_extract_api_key() -> tuple[str, str]:
    token_file = os.environ.get("WEVIBE_BENCH_EXTRACT_API_KEY_FILE", "").strip()
    if token_file:
        token_path = Path(token_file).expanduser().resolve()
        if not token_path.is_file():
            raise RuntimeError(f"proxy token file not found: {token_path}")
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"proxy token file empty: {token_path}")
        return token, "WEVIBE_BENCH_EXTRACT_API_KEY_FILE"

    for env_name in (
        "WEVIBE_BENCH_EXTRACT_API_KEY",
        "WEVIBE_BENCH_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value, env_name

    raise RuntimeError(
        "missing extraction API key; set WEVIBE_BENCH_EXTRACT_API_KEY_FILE, "
        "WEVIBE_BENCH_EXTRACT_API_KEY, WEVIBE_BENCH_API_KEY, or OPENROUTER_API_KEY"
    )


def _load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_optional_path_env(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _resolve_consumer_gate_state_dir(run_cfg: config.RunConfig) -> Path:
    explicit_state_dir = _resolve_optional_path_env(CONSUMER_STATE_DIR_ENV)
    if explicit_state_dir is not None:
        return explicit_state_dir

    scoped_wevibe_dir = _resolve_optional_path_env(CONSUMER_SCOPED_WEVIBE_DIR_ENV)
    if scoped_wevibe_dir is None:
        served_store_parent = Path(run_cfg.served_memories_host_path).expanduser().resolve().parent
        scoped_wevibe_dir = served_store_parent

    return default_plugin_state_dir(scoped_wevibe_dir)


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _bridge_paths(run_cfg: config.RunConfig, manifest_path: Path) -> BridgePaths:
    manifest_parent = Path(manifest_path).expanduser().resolve().parent
    base_dir = _ensure_private_dir(manifest_parent / "bridge")
    inbox = _ensure_private_dir(base_dir / "inbox")

    return BridgePaths(
        base_dir=base_dir,
        state_path=base_dir / "bridge-state.json",
        pidfile=base_dir / "bridge.pid",
        inbox=inbox,
        logdir=base_dir,
        served_store_path=Path(run_cfg.served_memories_host_path).expanduser().resolve(),
        consumer_state_dir=_resolve_consumer_gate_state_dir(run_cfg).expanduser().resolve(),
    )


def _load_consumer_decision_manifest(decision_path: str) -> ConsumerDecisionManifest:
    path = Path(decision_path).expanduser().resolve()
    payload = _load_json_file(path)
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"consumer decision manifest must decode to object: {path}")
    manifest = ConsumerDecisionManifest.from_dict(payload)
    validate_schema(manifest)
    validate_one_per_candidate(manifest)
    return manifest


def _consumer_decision_template_manifest() -> ConsumerDecisionManifest:
    run_id = "YOUR_RUN_ID"
    session_id = "YOUR_SESSION_ID"
    coordinator_trace = "trace://your-coordinator-trace"
    template = ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=run_id,
        policy_id=DEFAULT_PRIMARY_POLICY,
        default_fate="accept",
        decisions=(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-example-accept",
                fate="accept",
                coordinator_trace=coordinator_trace,
                reason="",
                note="example explicit accept decision (optional)",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-example-deny",
                fate="deny",
                coordinator_trace=coordinator_trace,
                reason="not useful for this session",
                note="",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-example-block",
                fate="block",
                coordinator_trace=coordinator_trace,
                reason="unsafe memory",
                note="",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-example-report",
                fate="report",
                coordinator_trace=coordinator_trace,
                reason="policy issue",
                note="",
            ),
        ),
        coordinator_trace=coordinator_trace,
    )
    validate_schema(template)
    validate_one_per_candidate(template)
    return template


def _parse_recalled_cids_arg(raw: str) -> list[str]:
    chunks = [part.strip() for part in raw.split(",")]
    recalled: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        if not chunk:
            continue
        if chunk in seen:
            continue
        seen.add(chunk)
        recalled.append(chunk)
    return recalled


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_private_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.chmod(path, 0o600)
        total = 0
        while total < len(encoded):
            total += os.write(fd, encoded[total:])
        os.fsync(fd)
    finally:
        os.close(fd)

    os.chmod(path, 0o600)


def _resolve_review_material_output_path(
    args: argparse.Namespace,
    *,
    layout: PathLayout,
    sequence_index: int,
) -> Path:
    out_arg = str(getattr(args, "out", "") or "").strip()
    if out_arg:
        out_path = Path(out_arg).expanduser().resolve()
    else:
        out_path = (layout.runs_dir / f"review-material.{sequence_index}.private.json").resolve()

    if not str(out_path).endswith(".private.json"):
        raise RuntimeError(
            "review-material output path must end with '.private.json' so it remains gitignored"
        )

    return out_path


def _is_no_memory_candidate_error(exc: RuntimeError) -> bool:
    return str(exc).startswith("extract produced no usable memory candidate")


class _NoopSessionRunner:
    """Coordinator-only session runner used for no-service subcommands."""

    def prepare_fixture(self, session: SessionRecord) -> None:
        raise RuntimeError(
            f"prepare_fixture not available for coordinator-only command (sequence_index={session.sequence_index})"
        )

    def run_session(self, session: SessionRecord) -> object:
        raise RuntimeError(
            f"run_session not available for coordinator-only command (sequence_index={session.sequence_index})"
        )

    def extract(self, session: SessionRecord) -> dict[str, Any]:
        raise RuntimeError(
            f"extract not available for coordinator-only command (sequence_index={session.sequence_index})"
        )

    def index_ready(self, session: SessionRecord) -> bool:
        raise RuntimeError(
            f"index_ready not available for coordinator-only command (sequence_index={session.sequence_index})"
        )


class _NoopM2Proof:
    pass


class _NoopHubClient:
    pass


class _PromptInjectingCaptureRest:
    """Delegate MCP REST calls while injecting extractor prompt and recording job id."""

    def __init__(self, inner: McpRest, prompt: str) -> None:
        self._inner = inner
        self._prompt = prompt
        self.last_job_id: str | None = None

    def extract(
        self,
        events: list[dict[str, Any]],
        model: str,
        project_context: dict[str, Any] | None = None,
        org_id: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        num_ctx: int | None = None,
        prompt: str | None = None,
        session_id: str | None = None,
    ) -> str:
        job_id = self._inner.extract(
            events=events,
            model=model,
            project_context=project_context,
            org_id=org_id,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            num_ctx=num_ctx,
            prompt=prompt if prompt is not None else self._prompt,
            session_id=session_id,
        )
        self.last_job_id = job_id
        return job_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _load_sxe_helpers(
    repo_root: Path,
) -> tuple[BuildSubstrateEvents, SessionIdCounts, SessionIdFromEvents]:
    module_path = repo_root / "scripts" / "backgammon_sxe.py"
    spec = importlib.util.spec_from_file_location("_wevibe_backgammon_sxe", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load backgammon_sxe helpers from {module_path}")
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)

    build_substrate_events = getattr(module, "_build_substrate_events", None)
    session_id_counts = getattr(module, "_session_id_counts_from_events", None)
    session_id_from_events = getattr(module, "_session_id_from_events", None)

    if not callable(build_substrate_events):
        raise RuntimeError("backgammon_sxe._build_substrate_events missing or non-callable")
    if not callable(session_id_counts):
        raise RuntimeError("backgammon_sxe._session_id_counts_from_events missing or non-callable")
    if not callable(session_id_from_events):
        raise RuntimeError("backgammon_sxe._session_id_from_events missing or non-callable")
    return build_substrate_events, session_id_counts, session_id_from_events


class RealSessionRunner:
    """Real per-session runtime seam composed from BackgammonRunner + M2Proof."""

    def __init__(
        self,
        *,
        task: str,
        org_id: str,
        runs_dir: Path,
        repo_root: Path,
        proof: M2Proof,
        hub_client: HubClient,
        leader: Identity,
        contributor_rest: _PromptInjectingCaptureRest,
        extract_api_key: str,
        extract_api_key_source: str,
        extract_base_url: str | None,
        extract_num_ctx: int | None,
        extract_timeout_s: int,
        consumer_gate_coordinator: ConsumerGateCoordinator,
        consumer_decision_manifest: ConsumerDecisionManifest | None,
        served_store_host_path: Path,
        bridge_state_path: Path | None = None,
    ) -> None:
        self._task = task
        self._org_id = org_id
        self._runs_dir = runs_dir
        self._repo_root = repo_root
        self._proof = proof
        self._hub_client = hub_client
        self._leader = leader
        self._contributor_rest = contributor_rest
        self._extract_api_key = extract_api_key
        self._extract_api_key_source = extract_api_key_source
        self._extract_base_url = extract_base_url
        self._extract_num_ctx = extract_num_ctx
        self._extract_timeout_s = extract_timeout_s

        if not isinstance(consumer_gate_coordinator, ConsumerGateCoordinator):
            raise TypeError("consumer_gate_coordinator must be a ConsumerGateCoordinator")
        if not isinstance(served_store_host_path, Path):
            raise TypeError("served_store_host_path must be a pathlib.Path")
        if consumer_decision_manifest is not None:
            validate_schema(consumer_decision_manifest)
            validate_one_per_candidate(consumer_decision_manifest)
        self._consumer_gate_coordinator = consumer_gate_coordinator
        self._consumer_decision_manifest = consumer_decision_manifest
        self._served_store_host_path = served_store_host_path.expanduser().resolve()
        if bridge_state_path is not None and not isinstance(bridge_state_path, Path):
            raise TypeError("bridge_state_path must be a pathlib.Path when provided")
        self._bridge_state_path = (
            bridge_state_path.expanduser().resolve()
            if isinstance(bridge_state_path, Path)
            else None
        )

        self._task_dir = self._repo_root / "tasks" / "backgammon"
        if not self._task_dir.is_dir():
            raise RuntimeError(f"backgammon task directory missing: {self._task_dir}")

        self._strategy_e_prompt_path = self._repo_root / "scaffold" / "sxe-candidate" / "E-assembled.txt"
        self._strategy_s_prompt_path = self._repo_root / "scaffold" / "sxe-candidate" / "S-fork-reasoning.md"
        self._strategy_s_prompt = _load_required_text(self._strategy_s_prompt_path)

        (
            self._build_substrate_events,
            self._session_id_counts_from_events,
            self._session_id_from_events,
        ) = _load_sxe_helpers(self._repo_root)

        self._max_attempts = config.RunConfig().max_attempts
        self._session_states: dict[int, _SessionRunState] = {}

        from wevibe_bench.adapters.backgammon import BackgammonRunner

        self._runner_cls = BackgammonRunner

    @staticmethod
    def _normalize_keywords(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            keyword = str(item).strip()
            if not keyword:
                continue
            marker = keyword.casefold()
            if marker in seen:
                continue
            seen.add(marker)
            out.append(keyword)
        return out

    def _progress(self, message: str) -> None:
        _LOG.info("run_cumulative.progress %s", message)

    def _state_for_session(self, session: SessionRecord) -> _SessionRunState:
        state = self._session_states.get(session.sequence_index)
        if state is not None:
            return state

        model_slug = _normalize_model_slug(session.model)
        run_label = f"cumulative-{session.sequence_index:04d}-{session.memory_mode}-{model_slug}"
        run_dir = self._runs_dir / run_label
        state = _SessionRunState(run_label=run_label, run_dir=run_dir)
        self._session_states[session.sequence_index] = state
        return state

    @staticmethod
    def _extract_model_for_session(session: SessionRecord) -> tuple[str, str]:
        provider = "openrouter"
        extract_model = str(session.model)
        provider_prefix = f"{provider}/"
        if extract_model.startswith(provider_prefix):
            extract_model = extract_model[len(provider_prefix):]
        if not extract_model.strip():
            raise RuntimeError(f"extract model resolved empty from session.model={session.model!r}")
        return extract_model, provider

    def _project_context_for_session(
        self,
        session: SessionRecord,
        state: _SessionRunState,
    ) -> dict[str, Any]:
        return {
            "title": f"cumulative-{session.sequence_index:04d}-{session.memory_mode}",
            "directory": str((state.run_dir / "worktree").resolve()),
            "stack": ["typescript", "node", "backgammon"],
            "task": self._task,
            "strategy_s_prompt": self._strategy_s_prompt,
            "strategy_e_prompt_path": str(self._strategy_e_prompt_path.resolve()),
            "api_key_source": self._extract_api_key_source,
        }

    @staticmethod
    def _recalled_candidate_cids(recalled_candidates: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for index, entry in enumerate(recalled_candidates):
            cid_raw = entry.get("cid")
            if cid_raw is None:
                cid_raw = entry.get("id")
            if not isinstance(cid_raw, str) or not cid_raw.strip():
                raise RuntimeError(
                    "consumer gate queue entry missing cid/id: "
                    f"index={index} keys={sorted(entry.keys())}"
                )
            cid = cid_raw.strip()
            if cid in seen:
                continue
            seen.add(cid)
            ordered.append(cid)
        return ordered

    @staticmethod
    def _partition_decisions_by_fate(
        decisions: list[tuple[str, str]],
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        accepted: list[str] = []
        denied: list[str] = []
        blocked: list[str] = []
        reported: list[str] = []

        for raw_cid, raw_fate in decisions:
            cid = str(raw_cid).strip()
            fate = str(raw_fate).strip()
            if not cid:
                raise RuntimeError("consumer gate outcome produced empty candidate cid")
            if fate == "accept":
                accepted.append(cid)
            elif fate == "deny":
                denied.append(cid)
            elif fate == "block":
                blocked.append(cid)
            elif fate == "report":
                reported.append(cid)
            else:
                raise RuntimeError(f"consumer gate outcome produced unsupported fate {fate!r}")

        return accepted, denied, blocked, reported

    def _resolve_run_and_session_ids(
        self,
        session: SessionRecord,
        *,
        state: _SessionRunState,
    ) -> tuple[str, str]:
        run_id = str(session.run_id or state.run_label).strip()
        if not run_id:
            raise RuntimeError(
                f"unable to resolve run_id for consumer gate outcome sequence_index={session.sequence_index}"
            )

        if isinstance(state.last_session_id, str) and state.last_session_id.strip():
            session_id = state.last_session_id.strip()
        elif isinstance(session.session_id, str) and session.session_id.strip():
            session_id = session.session_id.strip()
        else:
            raise RuntimeError(
                "unable to resolve raw session_id for consumer gate outcome; "
                "run_session/extract session id plumbing is incomplete"
            )

        return run_id, session_id

    def _manifest_for_consumer_gate(
        self,
        *,
        run_id: str,
        session_id: str,
        recalled_cids: list[str],
    ) -> ConsumerDecisionManifest:
        if self._consumer_decision_manifest is not None:
            return self._consumer_decision_manifest

        coordinator_trace = f"consumer-gate://{run_id}/{session_id}"
        return default_primary_manifest(
            run_id,
            session_id,
            recalled_cids,
            coordinator_trace=coordinator_trace,
        )

    def consumer_gate_outcome(self, session: SessionRecord) -> ConsumerGateRecord | None:
        if str(session.phase_group).strip().lower() != PhaseGroup.ON.value:
            return None

        state = self._state_for_session(session)
        run_id, raw_session_id = self._resolve_run_and_session_ids(session, state=state)

        session.run_label = state.run_label
        session.run_id = run_id
        session.session_id = raw_session_id

        recalled_candidates = self._consumer_gate_coordinator.read_recalled_candidates()
        recalled_cids = self._recalled_candidate_cids(recalled_candidates)
        manifest = self._manifest_for_consumer_gate(
            run_id=run_id,
            session_id=raw_session_id,
            recalled_cids=recalled_cids,
        )

        # LIVE timing note: in the eventual live benchmark daemon, apply_manifest+
        # heartbeat must run *during* the worker session so the plugin transform
        # drains decisions before injection. That runtime timing bridge is deferred
        # (out of this zero-provider scope). This hook currently performs durable
        # correlation + benchmark recording for the session checkpoint.
        outcome = self._consumer_gate_coordinator.apply_manifest(
            manifest,
            run_id=run_id,
            session_id=raw_session_id,
        )

        accepted_cids, denied_cids, blocked_cids, reported_cids = self._partition_decisions_by_fate(
            outcome.decisions
        )
        reconcile = self._consumer_gate_coordinator.served_store_reconcile(
            self._served_store_host_path,
            session_id=raw_session_id,
            accepted_cids=accepted_cids,
            denied_cids=denied_cids,
            blocked_cids=blocked_cids,
            reported_cids=reported_cids,
        )

        record = ConsumerGateRecord.from_outcome(
            outcome,
            reconcile,
            serve_receipt_status=None,
            serve_receipt_ids=None,
            denial_signal_status=None,
            report_signal_status=None,
        )
        if record.policy_id != manifest.policy_id:
            record = dataclass_replace(record, policy_id=manifest.policy_id)
        return record

    def prepare_fixture(self, session: SessionRecord) -> None:
        state = self._state_for_session(session)
        worktree = state.run_dir / "worktree"
        state.run_dir.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            shutil.rmtree(worktree)
        worktree.mkdir(parents=True, exist_ok=True)
        self._runner_cls._copy_tree_contents(self._task_dir / "scaffold", worktree)

        _LOG.info(
            "run_cumulative.prepare_fixture sequence_index=%d memory_mode=%s run_label=%s",
            session.sequence_index,
            session.memory_mode,
            state.run_label,
        )

    def run_session(self, session: SessionRecord) -> object:
        state = self._state_for_session(session)
        state.run_dir.mkdir(parents=True, exist_ok=True)
        session.run_label = state.run_label
        if not isinstance(session.run_id, str) or not session.run_id.strip():
            session.run_id = state.run_label

        on_session = (
            str(session.memory_mode).strip().lower() == "on"
            or str(session.phase_group).strip().lower() == PhaseGroup.ON.value
        )
        if on_session and self._bridge_state_path is not None:
            assert_bridge_ready(self._bridge_state_path)

        runner = self._runner_cls(
            task_dir=self._task_dir,
            work_root=state.run_dir,
            model=session.model,
            memory_mode=session.memory_mode,
            max_attempts=self._max_attempts,
            logger=_LOG,
            progress=self._progress,
        )
        result = runner.run_cell(state.run_label, state.run_dir, task_id="backgammon")
        state.last_session_id = result.session_id or state.last_session_id

        _LOG.info(
            "run_cumulative.run_session sequence_index=%d memory_mode=%s verdict=%s session_fp=%s",
            session.sequence_index,
            session.memory_mode,
            result.verdict,
            SessionRecord.session_fp_of(result.session_id)
            if isinstance(result.session_id, str) and result.session_id.strip()
            else "none",
        )
        return result

    def extract(self, session: SessionRecord) -> dict[str, Any]:
        state = self._state_for_session(session)
        if not state.run_dir.is_dir():
            raise RuntimeError(
                f"missing run directory for extraction sequence_index={session.sequence_index}: {state.run_dir}"
            )

        events, event_stats, _ = self._build_substrate_events(session_dir=state.run_dir)
        session_counts = self._session_id_counts_from_events(state.run_dir)
        session_id = self._session_id_from_events(
            state.run_dir,
            session_counts=session_counts,
        )
        if session_id is None:
            session_id = state.last_session_id
        if session_id is None:
            raise RuntimeError(
                "unable to resolve session_id from worker events; extraction cannot continue"
            )

        self._contributor_rest.last_job_id = None
        extract_model, extract_provider = self._extract_model_for_session(session)
        project_context = self._project_context_for_session(session, state)

        try:
            memories = self._proof.produce_memories(
                events=events,
                model=extract_model,
                api_key=self._extract_api_key,
                project_context=project_context,
                org_id=self._org_id,
                provider=extract_provider,
                base_url=self._extract_base_url,
                num_ctx=self._extract_num_ctx,
                extract_timeout_s=self._extract_timeout_s,
                session_id=session_id,
            )
        except RuntimeError as exc:
            if _is_no_memory_candidate_error(exc):
                memories = []
            else:
                raise

        extraction_job_id = self._contributor_rest.last_job_id
        if not isinstance(extraction_job_id, str) or not extraction_job_id.strip():
            raise RuntimeError("extract completed without a recorded extraction job_id")

        candidate_refs: list[dict[str, Any]] = []
        for memory in memories:
            if not isinstance(memory, Mapping):
                raise RuntimeError("produce_memories returned a non-mapping memory candidate")
            memory_map = dict(memory)

            text = memory_map.get("text")
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("produce_memories returned candidate with empty text")

            keywords = self._normalize_keywords(memory_map.get("keywords"))
            memory_type_raw = memory_map.get("memory_type")
            memory_type = (
                memory_type_raw.strip()
                if isinstance(memory_type_raw, str) and memory_type_raw.strip()
                else "memory"
            )

            submission_hash = self._proof.submit_memory(self._org_id, memory_map)
            candidate_refs.append(
                {
                    "submission_hash": submission_hash,
                    "text": text,
                    "keywords": keywords,
                    "memory_type": memory_type,
                }
            )

        _LOG.info(
            "run_cumulative.extract sequence_index=%d memory_mode=%s job_id=%s session_fp=%s candidate_count=%d events_sha256_first8=%s",
            session.sequence_index,
            session.memory_mode,
            extraction_job_id,
            SessionRecord.session_fp_of(session_id),
            len(candidate_refs),
            str(event_stats.get("events_sha256_first8") or "none"),
        )

        return {
            "candidate_refs": candidate_refs,
            "extraction_job_id": extraction_job_id,
            "session_id": session_id,
            "extraction_candidate_count": len(candidate_refs),
        }

    def index_ready(self, session: SessionRecord) -> bool:
        committed_ids = [
            str(committed_id).strip()
            for committed_id in session.committed_ids
            if isinstance(committed_id, str) and committed_id.strip()
        ]
        if not committed_ids:
            return True

        org_id = str(session.org_id or self._org_id).strip()
        if not org_id:
            raise RuntimeError("session org_id missing during index_ready")

        commit_status_payload = self._hub_client.commit_status(self._leader, org_id)
        for committed_id in committed_ids:
            if not self._proof._is_committed(commit_status_payload, committed_id):
                return False

        committed_set = set(committed_ids)
        delivery_targets: list[dict[str, str]] = []
        for raw_candidate in session.candidate_refs:
            if not isinstance(raw_candidate, Mapping):
                continue
            candidate = dict(raw_candidate)
            submission_hash = candidate.get("submission_hash")
            text = candidate.get("text")
            if (
                isinstance(submission_hash, str)
                and submission_hash in committed_set
                and isinstance(text, str)
                and text.strip()
            ):
                delivery_targets.append(
                    {
                        "fragment": text,
                        "cid": submission_hash,
                    }
                )

        if not delivery_targets:
            return True

        delivery_payload = self._proof.prove_delivery(org_id, delivery_targets)
        if not isinstance(delivery_payload, Mapping):
            return False
        return str(delivery_payload.get("delivery") or "NO") == "YES"


def _build_offline_leader_client(
    layout: PathLayout,
    *,
    review_card: PrivateReviewCard,
) -> LeaderClient:
    layout.runs_dir.mkdir(parents=True, exist_ok=True)
    offline_leader = Identity.from_hex("11" * 32)
    catalog = PrivateCatalog(str(layout.catalog_path))
    return LeaderClient(
        m2proof=_NoopM2Proof(),
        hub_client=_NoopHubClient(),
        leader=offline_leader,
        catalog=catalog,
        safe_ledger_path=str(layout.safe_ledger_path),
        idempotency_ledger_path=str(layout.idempotency_ledger_path),
        review_card=review_card,
    )


def _build_real_runner_and_leader_client(
    args: argparse.Namespace,
    layout: PathLayout,
    *,
    review_card: PrivateReviewCard,
) -> tuple[RealSessionRunner, LeaderClient]:
    load_bench_env()
    layout.runs_dir.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[1]
    cfg = LifecycleConfig()
    run_cfg = config.RunConfig()
    bridge_paths = _bridge_paths(run_cfg, layout.manifest_path)

    consumer_state_dir = bridge_paths.consumer_state_dir
    consumer_gate_coordinator = ConsumerGateCoordinator(state_dir=consumer_state_dir)
    consumer_decision_arg = str(getattr(args, "consumer_decision", "") or "").strip()
    consumer_decision_manifest = (
        _load_consumer_decision_manifest(consumer_decision_arg)
        if consumer_decision_arg
        else None
    )
    served_store_host_path = bridge_paths.served_store_path

    leader = Identity.from_hex(_required_env("WEVIBE_BENCH_LEADER_SEED_HEX"))
    contributor = Identity.from_hex(_required_env("WEVIBE_BENCH_CONTRIB_SEED_HEX"))

    hub_client = HubClient(cfg, _LOG)
    extract_prompt = _load_required_text(repo_root / "scaffold" / "sxe-candidate" / "E-assembled.txt")

    contributor_rest = _PromptInjectingCaptureRest(
        McpRest(cfg.contributor_mcp_url, cfg, _LOG),
        extract_prompt,
    )
    leader_rest = McpRest(cfg.leader_mcp_url, cfg, _LOG)

    def rest_factory(base_url: str) -> Any:
        if base_url == cfg.contributor_mcp_url:
            return contributor_rest
        if base_url == cfg.leader_mcp_url:
            return leader_rest
        return McpRest(base_url, cfg, _LOG)

    extract_api_key, extract_api_key_source = _resolve_extract_api_key()
    proof = M2Proof(
        cfg=cfg,
        orchestrator=SimpleNamespace(org_id=str(args.org), hub_client=hub_client),
        leader=leader,
        contributor=contributor,
        logger=_LOG,
        mcp_rest_factory=rest_factory,
        hub_client=hub_client,
    )

    real_runner = RealSessionRunner(
        task=str(args.task),
        org_id=str(args.org),
        runs_dir=(layout.runs_dir / "sessions").resolve(),
        repo_root=repo_root,
        proof=proof,
        hub_client=hub_client,
        leader=leader,
        contributor_rest=contributor_rest,
        extract_api_key=extract_api_key,
        extract_api_key_source=extract_api_key_source,
        extract_base_url=_resolve_extract_base_url(),
        extract_num_ctx=_resolve_extract_num_ctx(),
        extract_timeout_s=_resolve_extract_timeout_s(),
        consumer_gate_coordinator=consumer_gate_coordinator,
        consumer_decision_manifest=consumer_decision_manifest,
        served_store_host_path=served_store_host_path,
        bridge_state_path=bridge_paths.state_path,
    )

    catalog = PrivateCatalog(str(layout.catalog_path))
    leader_client = LeaderClient(
        m2proof=proof,
        hub_client=hub_client,
        leader=leader,
        catalog=catalog,
        safe_ledger_path=str(layout.safe_ledger_path),
        idempotency_ledger_path=str(layout.idempotency_ledger_path),
        review_card=review_card,
    )
    return real_runner, leader_client


def _build_context(args: argparse.Namespace, *, require_runtime: bool) -> CliContext:
    layout = _resolve_manifest_layout(str(args.manifest))
    layout.runs_dir.mkdir(parents=True, exist_ok=True)
    review_card = PrivateReviewCard(str(layout.review_card_path))

    roster, _ = _build_roster()
    config_fingerprint = config.backgammon_ladder_roster_fingerprint()

    if require_runtime:
        runner, leader_client = _build_real_runner_and_leader_client(
            args,
            layout,
            review_card=review_card,
        )
    else:
        runner = _NoopSessionRunner()
        leader_client = _build_offline_leader_client(layout, review_card=review_card)

    sequencer = CumulativeSequencer(
        manifest_path=str(layout.manifest_path),
        runner=runner,
        leader_client=leader_client,
        review_card=review_card,
        roster=roster,
        seed=int(args.seed),
        task=str(args.task),
        org_id=str(args.org),
        config_fingerprint=config_fingerprint,
        on_budget=int(args.on_budget),
    )
    return CliContext(
        sequencer=sequencer,
        leader_client=leader_client,
        review_card=review_card,
        layout=layout,
    )


def _load_decision_manifest(decision_path: str) -> DecisionManifest:
    path = Path(decision_path).expanduser().resolve()
    payload = _load_json_file(path)
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"decision manifest must decode to object: {path}")
    return DecisionManifest.from_dict(payload)


def _current_session_or_raise(sequencer: CumulativeSequencer) -> SessionRecord:
    session = sequencer.current_session()
    if session is None:
        raise RuntimeError("sequencer is done; no current session")
    return session


def _bridge_now_ms() -> int:
    return int(time.time() * 1000)


def _bridge_scope_from_state(state: Any) -> str | None:
    run_id = getattr(state, "run_id", None)
    session_id = getattr(state, "session_id", None)
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return scope_key(run_id, session_id)


def assert_bridge_ready(state_path: Path, *, now_ms: int | None = None) -> None:
    state = load_state(state_path)
    if state is None:
        raise RuntimeError(
            "consumer bridge is not ready: bridge state missing "
            f"({state_path}). Start bridge first with `bridge start --run-id <id> --session-id <id>`."
        )

    lease = state.lease
    if lease is None:
        raise RuntimeError(
            "consumer bridge is not ready: no active worker lease in bridge state. "
            "Start bridge first with `bridge start --run-id <id> --session-id <id>`."
        )

    current_ms = _bridge_now_ms() if now_ms is None else int(now_ms)
    if lease.is_expired(current_ms):
        raise RuntimeError(
            "consumer bridge is not ready: worker lease is expired. "
            "Start bridge first with `bridge start --run-id <id> --session-id <id>`."
        )

    resume_marker = str(state.resume_marker or "").strip().lower()
    if resume_marker not in {"active", "running"}:
        raise RuntimeError(
            "consumer bridge is not ready: bridge state is not active "
            f"(resume_marker={state.resume_marker!r}). "
            "Start bridge first with `bridge start --run-id <id> --session-id <id>`."
        )


def _render_inventory_record(record: Any) -> dict[str, Any]:
    keywords = getattr(record, "keywords", [])
    normalized_keywords = [str(keyword) for keyword in keywords] if isinstance(keywords, list) else []
    return {
        "submission_hash": str(getattr(record, "submission_hash", "")),
        "committed_id": getattr(record, "committed_id", None),
        "content_hash": str(getattr(record, "content_hash", "")),
        "org_id": str(getattr(record, "org_id", "")),
        "sequence_index": int(getattr(record, "sequence_index", 0)),
        "committing_identity": str(getattr(record, "committing_identity", "")),
        "keywords": normalized_keywords,
        "keyword_count": len(normalized_keywords),
        "committed_at": str(getattr(record, "committed_at", "")),
    }


def _decision_template_for_session(
    session: SessionRecord,
    *,
    org_id: str,
) -> DecisionManifest:
    candidates: list[CandidateDecision] = []
    for index, raw_candidate in enumerate(session.candidate_refs):
        if not isinstance(raw_candidate, Mapping):
            raise RuntimeError(f"session.candidate_refs[{index}] must be an object")
        submission_hash = raw_candidate.get("submission_hash")
        if not isinstance(submission_hash, str) or not submission_hash.strip():
            raise RuntimeError(
                f"session.candidate_refs[{index}] missing submission_hash; cannot emit decision template"
            )
        candidates.append(
            CandidateDecision(
                candidate_ref=submission_hash,
                verdict="",
                reason="",
                evidence={},
                duplicate_refs=[],
            )
        )

    session_fp = session.session_fp
    if session_fp is None and isinstance(session.session_id, str) and session.session_id.strip():
        session_fp = SessionRecord.session_fp_of(session.session_id)

    return DecisionManifest(
        manifest_id=f"decision-template-{session.sequence_index}-{_utc_now_iso()}",
        created_at=_utc_now_iso(),
        sequence_index=int(session.sequence_index),
        org_id=str(session.org_id or org_id),
        coordinator_identity="",
        integrity=IntegrityAttestation(
            job_id=session.extraction_job_id,
            session_fp=session_fp,
            resolved_problem_count=None,
            emitted_memory_count=None,
            invariant_violation=False,
            integrity_record_seen=False,
            log_path=None,
        ),
        candidates=candidates,
    )


def _handle_run(args: argparse.Namespace) -> int:
    if not bool(args.until_review):
        raise RuntimeError("run requires --until-review")

    context = _build_context(args, require_runtime=True)
    result = context.sequencer.step_until_review()
    if result["status"] == "awaiting_coordinator_review":
        _print_json(
            {
                "status": "awaiting_coordinator_review",
                "sequence_index": int(result["sequence_index"]),
                "job_id": str(result["extraction_job_id"]),
                "session_fp": str(result["session_fp"]),
                "candidate_count": int(result["candidate_count"]),
            }
        )
    else:
        _print_json(result)
    return 0


def _handle_resume(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=True)
    result = context.sequencer.resume_with_decision(args.decision)
    if result["status"] == "session_committed":
        _print_json(
            {
                "status": "session_committed",
                "committed_ids": list(result["committed_ids"]),
                "denied_refs": list(result["denied_refs"]),
                "all_denied": bool(result["all_denied"]),
                "next_index": int(result["next_index"]),
            }
        )
    else:
        _print_json(result)
    return 0


def _handle_state(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=False)
    _print_json(context.sequencer.state())
    return 0


def _handle_list_pending(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=False)
    session = context.sequencer.current_session()
    if session is None:
        _print_json({"status": "done"})
        return 0
    _print_json(context.leader_client.list_pending(session))
    return 0


def _handle_list_inventory(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=False)
    records = context.leader_client.list_inventory()
    rendered = [_render_inventory_record(record) for record in records]
    _print_json({"count": len(rendered), "records": rendered})
    return 0


def _handle_reconcile_inventory(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=False)
    authoritative_path = Path(args.authoritative).expanduser().resolve()
    authoritative_payload = _load_json_file(authoritative_path)
    if not isinstance(authoritative_payload, list):
        raise RuntimeError("authoritative inventory JSON must be a list")
    report = context.leader_client.reconcile_inventory(authoritative_payload)
    _print_json(report)

    if bool(args.require_complete) and not bool(report.get("catalog_complete")):
        raise SystemExit(
            "catalog completeness gate failed (--require-complete): "
            "authoritative inventory contains in_chain_not_catalog entries"
        )

    return 0


def _handle_review_material(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=False)
    session = _current_session_or_raise(context.sequencer)
    sequence_index = int(session.sequence_index)

    new_candidates = context.review_card.session_material(sequence_index)

    prior_accepted: list[dict[str, Any]] = []
    for record in context.leader_client.list_inventory():
        committed_id_raw = getattr(record, "committed_id", None)
        committed_id = committed_id_raw.strip() if isinstance(committed_id_raw, str) else ""
        if not committed_id:
            continue

        keywords_raw = getattr(record, "keywords", [])
        keywords = [str(keyword) for keyword in keywords_raw] if isinstance(keywords_raw, list) else []

        prior_accepted.append(
            {
                "committed_id": committed_id,
                "comparison_text": str(getattr(record, "comparison_text", "")),
                "keywords": keywords,
                "committing_identity": str(getattr(record, "committing_identity", "")),
            }
        )

    reconciliation: dict[str, Any] | None = None
    authoritative_arg = str(getattr(args, "authoritative", "") or "").strip()
    if authoritative_arg:
        authoritative_path = Path(authoritative_arg).expanduser().resolve()
        authoritative_payload = _load_json_file(authoritative_path)
        if not isinstance(authoritative_payload, list):
            raise RuntimeError("authoritative inventory JSON must be a list")
        reconciliation = context.leader_client.reconcile_inventory(authoritative_payload)

    out_path = _resolve_review_material_output_path(
        args,
        layout=context.layout,
        sequence_index=sequence_index,
    )
    _write_private_json_file(
        out_path,
        {
            "new_candidates": new_candidates,
            "prior_accepted": prior_accepted,
            "reconciliation": reconciliation,
        },
    )

    output_summary: dict[str, Any] = {
        "path": str(out_path),
        "new_candidate_count": len(new_candidates),
        "prior_accepted_count": len(prior_accepted),
    }
    if reconciliation is not None:
        output_summary["catalog_complete"] = bool(reconciliation.get("catalog_complete"))
    _print_json(output_summary)
    return 0


def _handle_emit_decision_template(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=False)
    session = _current_session_or_raise(context.sequencer)
    template = _decision_template_for_session(session, org_id=str(args.org))
    rendered = json.dumps(template.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    out_arg = str(getattr(args, "out", "") or "").strip()
    if out_arg:
        out_path = Path(out_arg).expanduser().resolve()
        _write_text_file(out_path, rendered)
    print(rendered, end="")
    return 0


def _handle_emit_consumer_decision_template(args: argparse.Namespace) -> int:
    template = _consumer_decision_template_manifest()
    rendered = json.dumps(template.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    out_arg = str(getattr(args, "out", "") or "").strip()
    if out_arg:
        out_path = Path(out_arg).expanduser().resolve()
        _write_text_file(out_path, rendered)
    print(rendered, end="")
    return 0


def _handle_validate_decision(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=False)
    session = _current_session_or_raise(context.sequencer)
    decision_manifest = _load_decision_manifest(args.decision)
    try:
        context.leader_client.validate(decision_manifest, session)
    except Exception as exc:
        print(str(exc))
        return 1
    print("PASS")
    return 0


def _handle_validate_consumer_decision(args: argparse.Namespace) -> int:
    try:
        manifest = _load_consumer_decision_manifest(str(args.file))

        run_id = str(getattr(args, "run_id", "") or "").strip()
        session_id = str(getattr(args, "session_id", "") or "").strip()
        recalled_cids_arg = str(getattr(args, "recalled_cids", "") or "").strip()

        correlation_requested = bool(run_id or session_id or recalled_cids_arg)
        pass_reason = "schema + one_per_candidate validated"

        if correlation_requested:
            missing: list[str] = []
            if not run_id:
                missing.append("--run-id")
            if not session_id:
                missing.append("--session-id")
            if not recalled_cids_arg:
                missing.append("--recalled-cids")
            if missing:
                raise ValueError(
                    "correlation validation requires all of "
                    "--run-id, --session-id, --recalled-cids; missing "
                    + ", ".join(missing)
                )

            recalled_cids = _parse_recalled_cids_arg(recalled_cids_arg)
            if not recalled_cids:
                raise ValueError("--recalled-cids must include at least one candidate cid")

            uncovered = validate_correlation(
                manifest,
                run_id=run_id,
                session_id=session_id,
                recalled_cids=recalled_cids,
            )
            if uncovered:
                pass_reason = (
                    "schema + one_per_candidate + correlation validated; "
                    f"uncovered_recalled_cids={','.join(sorted(uncovered))}"
                )
            else:
                pass_reason = "schema + one_per_candidate + correlation validated"

    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1

    print(f"PASS: {pass_reason}")
    return 0


def _read_pidfile(path: Path) -> int | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _write_pidfile(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _remove_pidfile_if_matches(path: Path, pid: int) -> None:
    existing_pid = _read_pidfile(path)
    if existing_pid != pid:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _reap_child_pid_if_exited(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return waited_pid == pid


def _pid_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if _reap_child_pid_if_exited(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid: int, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_alive(pid)


def _mark_bridge_state_stopped(state_path: Path, *, clear_lease: bool) -> None:
    state = load_state(state_path)
    if state is None:
        return
    state.resume_marker = "stopped"
    if clear_lease:
        state.lease = None
    atomic_write_state(state_path, state)


def _bridge_fate_counts(state: Any) -> dict[str, int]:
    counts = {"accept": 0, "deny": 0, "block": 0, "report": 0}
    for record in state.consumed_manifests.values():
        for delivered in record.delivered:
            fate = str(delivered.fate)
            if fate in counts:
                counts[fate] += 1
    return counts


def _resolve_bridge_runtime_config(args: argparse.Namespace) -> BridgeRuntimeConfig:
    load_bench_env()
    run_cfg = config.RunConfig()
    layout = _resolve_manifest_layout(str(args.manifest))
    bridge_paths = _bridge_paths(run_cfg, layout.manifest_path)

    manifest_inbox_arg = str(getattr(args, "manifest_inbox", "") or "").strip()
    if manifest_inbox_arg:
        manifest_inbox = _ensure_private_dir(Path(manifest_inbox_arg).expanduser().resolve())
    else:
        manifest_inbox = bridge_paths.inbox

    state_dir_arg = str(getattr(args, "state_dir", "") or "").strip()
    if state_dir_arg:
        consumer_state_dir = Path(state_dir_arg).expanduser().resolve()
    else:
        consumer_state_dir = bridge_paths.consumer_state_dir
    consumer_state_dir.mkdir(parents=True, exist_ok=True)

    served_store_arg = str(getattr(args, "served_store", "") or "").strip()
    if served_store_arg:
        served_store_path = Path(served_store_arg).expanduser().resolve()
    else:
        served_store_path = bridge_paths.served_store_path

    return BridgeRuntimeConfig(
        layout=layout,
        bridge_paths=bridge_paths,
        manifest_inbox=manifest_inbox,
        consumer_state_dir=consumer_state_dir,
        served_store_path=served_store_path,
    )


def _require_bridge_scope(args: argparse.Namespace) -> tuple[str, str, str]:
    run_id = str(getattr(args, "run_id", "") or "").strip()
    session_id = str(getattr(args, "session_id", "") or "").strip()
    if not run_id:
        raise RuntimeError("bridge action requires --run-id")
    if not session_id:
        raise RuntimeError("bridge action requires --session-id")
    return run_id, session_id, SessionRecord.session_fp_of(session_id)


def _bridge_status_payload(runtime: BridgeRuntimeConfig) -> dict[str, Any]:
    state_path = runtime.bridge_paths.state_path
    pidfile = runtime.bridge_paths.pidfile
    pid = _read_pidfile(pidfile)
    pid_alive = _pid_is_alive(pid)
    now_ms = _bridge_now_ms()

    state = load_state(state_path)
    lease_expired = True
    lease_remaining_ms = 0
    heartbeat_age_ms: int | None = None
    heartbeat_last_ts_ms: int | None = None
    resume_marker: str | None = None
    consumed_manifest_count = 0
    run_id: str | None = None
    session_id: str | None = None
    session_fp: str | None = None
    side_effect_timeouts: list[str] = []
    fate_counts = {"accept": 0, "deny": 0, "block": 0, "report": 0}

    if state is not None:
        consumed_manifest_count = len(state.consumed_manifests)
        run_id = state.run_id
        session_id = state.session_id
        session_fp = state.session_fp
        resume_marker = str(state.resume_marker)
        fate_counts = _bridge_fate_counts(state)

        if state.lease is not None:
            lease_expired = state.lease.is_expired(now_ms)
            lease_remaining_ms = max(0, int(state.lease.expires_at_ms) - now_ms)

        if state.heartbeat_last_ts_ms is not None:
            heartbeat_last_ts_ms = int(state.heartbeat_last_ts_ms)
            heartbeat_age_ms = max(0, now_ms - heartbeat_last_ts_ms)

        raw_side_effect_timeouts = state.plugin_outcome_refs.get("side_effect_timeouts", [])
        if isinstance(raw_side_effect_timeouts, list):
            side_effect_timeouts = [
                str(cid).strip()
                for cid in raw_side_effect_timeouts
                if isinstance(cid, str) and cid.strip()
            ]

    running = bool(
        pid_alive
        and state is not None
        and state.lease is not None
        and not lease_expired
        and str(state.resume_marker or "").strip().lower() in {"active", "running"}
    )

    return {
        "status": "ok",
        "running": running,
        "pid": pid,
        "pid_alive": pid_alive,
        "pidfile": str(pidfile),
        "state_path": str(state_path),
        "state_dir": str(runtime.consumer_state_dir),
        "manifest_inbox": str(runtime.manifest_inbox),
        "served_store": str(runtime.served_store_path),
        "run_id": run_id,
        "session_id": session_id,
        "session_fp": session_fp,
        "resume_marker": resume_marker,
        "lease_expired": lease_expired,
        "lease_remaining_ms": lease_remaining_ms,
        "consumed_manifest_count": consumed_manifest_count,
        "fate_counts": fate_counts,
        "heartbeat_last_ts_ms": heartbeat_last_ts_ms,
        "heartbeat_age_ms": heartbeat_age_ms,
        "side_effect_timeouts": side_effect_timeouts,
    }


def _spawn_bridge_background(
    args: argparse.Namespace,
    *,
    runtime: BridgeRuntimeConfig,
    run_id: str,
    session_id: str,
    session_fp: str,
) -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logfile = runtime.bridge_paths.logdir / f"bridge-{timestamp}.log"

    script_path = Path(__file__).resolve()
    repo_root = script_path.parents[1]

    cmd = [
        sys.executable,
        str(script_path),
        "--manifest",
        str(runtime.layout.manifest_path),
        "--seed",
        str(args.seed),
        "--on-budget",
        str(args.on_budget),
        "--task",
        str(args.task),
        "--org",
        str(args.org),
        "bridge",
        "run-foreground",
        "--run-id",
        run_id,
        "--session-id",
        session_id,
        "--manifest-inbox",
        str(runtime.manifest_inbox),
        "--state-dir",
        str(runtime.consumer_state_dir),
        "--served-store",
        str(runtime.served_store_path),
        "--lease-ttl-ms",
        str(int(args.lease_ttl_ms)),
        "--poll-interval-ms",
        str(int(args.poll_interval_ms)),
        "--heartbeat-cadence-ms",
        str(int(args.heartbeat_cadence_ms)),
    ]
    if args.max_cycles is not None:
        cmd.extend(["--max-cycles", str(int(args.max_cycles))])

    container_name = str(getattr(args, "container_name", "") or "").strip()
    if container_name:
        cmd.extend(["--container-name", container_name])

    logfile.parent.mkdir(parents=True, exist_ok=True)
    handle = logfile.open("ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    finally:
        handle.close()

    _write_pidfile(runtime.bridge_paths.pidfile, proc.pid)

    _print_json(
        {
            "status": "started",
            "pid": proc.pid,
            "logfile": str(logfile),
            "state_path": str(runtime.bridge_paths.state_path),
            "pidfile": str(runtime.bridge_paths.pidfile),
            "manifest_inbox": str(runtime.manifest_inbox),
            "state_dir": str(runtime.consumer_state_dir),
            "served_store": str(runtime.served_store_path),
            "run_id": run_id,
            "session_id": session_id,
            "session_fp": session_fp,
        }
    )
    return 0


def _handle_bridge(args: argparse.Namespace) -> int:
    runtime = _resolve_bridge_runtime_config(args)
    action = str(getattr(args, "action", "") or "").strip()
    state_path = runtime.bridge_paths.state_path
    pidfile = runtime.bridge_paths.pidfile

    if action in {"start", "resume"}:
        run_id, session_id, session_fp = _require_bridge_scope(args)
        requested_scope = scope_key(run_id, session_id)

        state = load_state(state_path)
        if action == "resume" and state is None:
            raise RuntimeError(
                f"bridge resume requires existing bridge state file: {state_path}; run `bridge start` first"
            )

        stale_pid = _read_pidfile(pidfile)
        stale_pid_alive = _pid_is_alive(stale_pid)
        if stale_pid is not None and not stale_pid_alive:
            try:
                pidfile.unlink()
            except FileNotFoundError:
                pass

        if stale_pid_alive:
            raise RuntimeError(
                f"bridge already running (pid={stale_pid}); stop it first with `bridge stop`"
            )

        if state is not None and state.lease is not None:
            now_ms = _bridge_now_ms()
            lease_active = not state.lease.is_expired(now_ms)
            active_scope = _bridge_scope_from_state(state)
            if lease_active and active_scope is not None and active_scope != requested_scope:
                raise RuntimeError(
                    "cannot start bridge: different active worker scope holds lease "
                    f"(active={active_scope!r}, requested={requested_scope!r})"
                )

            lease_pid_alive = _pid_is_alive(state.lease.pid)
            if lease_active and lease_pid_alive and str(state.resume_marker or "").strip().lower() in {
                "active",
                "running",
            }:
                raise RuntimeError(
                    "bridge already running with active lease; stop it first with `bridge stop`"
                )

        return _spawn_bridge_background(
            args,
            runtime=runtime,
            run_id=run_id,
            session_id=session_id,
            session_fp=session_fp,
        )

    if action == "run-foreground":
        run_id, session_id, session_fp = _require_bridge_scope(args)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        stop_event = threading.Event()

        def _handle_signal(signum: int, _frame: Any) -> None:
            _LOG.info("run_cumulative.bridge.signal signum=%s", signum)
            stop_event.set()

        previous_handlers: dict[int, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _handle_signal)

        container_name = str(getattr(args, "container_name", "") or "").strip() or None
        coordinator = ConsumerGateCoordinator(state_dir=runtime.consumer_state_dir)

        try:
            bridge = ConsumerBridge(
                coordinator=coordinator,
                state_path=state_path,
                manifest_inbox=runtime.manifest_inbox,
                served_store_path=runtime.served_store_path,
                run_id=run_id,
                session_id=session_id,
                session_fp=session_fp,
                container_name=container_name,
                logger=_LOG,
                lease_ttl_ms=int(args.lease_ttl_ms),
                heartbeat_cadence_ms=int(args.heartbeat_cadence_ms),
                poll_interval_ms=int(args.poll_interval_ms),
            )
            bridge.run_loop(stop_event=stop_event, max_cycles=args.max_cycles)
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
            _mark_bridge_state_stopped(state_path, clear_lease=True)
            _remove_pidfile_if_matches(pidfile, os.getpid())
        return 0

    if action == "status":
        _print_json(_bridge_status_payload(runtime))
        return 0

    if action == "stop":
        pid = _read_pidfile(pidfile)
        if _pid_is_alive(pid):
            assert pid is not None
            os.kill(pid, signal.SIGTERM)
            if not _wait_for_pid_exit(pid, timeout_s=15.0):
                os.kill(pid, signal.SIGKILL)
                if not _wait_for_pid_exit(pid, timeout_s=15.0):
                    raise RuntimeError(f"bridge process did not exit after SIGKILL (pid={pid})")

        try:
            pidfile.unlink()
        except FileNotFoundError:
            pass

        _mark_bridge_state_stopped(state_path, clear_lease=True)

        payload = _bridge_status_payload(runtime)
        payload["status"] = "stopped"
        _print_json(payload)
        return 0

    raise RuntimeError(f"unsupported bridge action: {action!r}")


def assert_primary_path() -> None:
    """Raise if this module is not marked as the canonical primary path."""

    if IS_PRIMARY_SCORED_PATH is not True:
        raise AssertionError("run_cumulative.py lost primary-path marker")

    primary_recall_mode = str(config.RunConfig().primary_recall_mode).strip().lower()
    if primary_recall_mode != "prod":
        raise AssertionError(
            "run_cumulative.py primary scored path requires RunConfig.primary_recall_mode='prod' "
            "(declared consumer policy; no hidden env auto-accept)"
        )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run/resume/state coordinator CLI for cumulative benchmark sequencing.",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST_PATH),
        help=(
            "Path to cumulative manifest JSON "
            f"(default: {DEFAULT_MANIFEST_PATH.as_posix()})."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Deterministic schedule seed (default: {DEFAULT_SEED}).",
    )
    parser.add_argument(
        "--on-budget",
        type=int,
        default=DEFAULT_ON_BUDGET,
        help=f"ON-phase session budget (default: {DEFAULT_ON_BUDGET}).",
    )
    parser.add_argument(
        "--task",
        default=DEFAULT_TASK_LABEL,
        help="Logical task label used in manifest validation.",
    )
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG_ID,
        help=f"Org id for extraction/commit flow (default: {DEFAULT_ORG_ID}).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Build/resume manifest and step one session until coordinator review.",
    )
    run_parser.add_argument(
        "--until-review",
        action="store_true",
        help="Required marker: execute phase machine until AWAIT_COORDINATOR_REVIEW.",
    )
    run_parser.add_argument(
        "--consumer-decision",
        default="",
        help=(
            "Optional ConsumerDecisionManifest JSON path for ON-session consumer gate "
            "(for conformance deny/block/report runs)."
        ),
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume current session with coordinator DecisionManifest.",
    )
    resume_parser.add_argument("--decision", required=True, help="Path to decision JSON.")

    subparsers.add_parser("state", help="Print cumulative sequencer state summary.")
    subparsers.add_parser(
        "list-pending",
        help="Print current-session pending submission metadata (no plaintext).",
    )
    subparsers.add_parser(
        "list-inventory",
        help="Print catalog inventory metadata (no private comparison text).",
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile-inventory",
        help="Compare local catalog metadata to authoritative inventory JSON.",
    )
    reconcile_parser.add_argument(
        "--authoritative",
        required=True,
        help="Authoritative inventory JSON path.",
    )
    reconcile_parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Fail closed if authoritative inventory has entries missing from local catalog.",
    )

    review_material_parser = subparsers.add_parser(
        "review-material",
        help=(
            "Write private review material JSON for coordinator semantic cross-check "
            "(plaintext in 0600 private file only)."
        ),
    )
    review_material_parser.add_argument(
        "--authoritative",
        default="",
        help="Optional authoritative inventory JSON path for reconciliation payload.",
    )
    review_material_parser.add_argument(
        "--out",
        default="",
        help="Optional output path (must end with .private.json).",
    )

    emit_parser = subparsers.add_parser(
        "emit-decision-template",
        help="Emit DecisionManifest scaffold for current session candidate refs.",
    )
    emit_parser.add_argument(
        "--out",
        default="",
        help="Optional file path to also write the emitted template JSON.",
    )

    emit_consumer_parser = subparsers.add_parser(
        "emit-consumer-decision-template",
        help=(
            "Emit ConsumerDecisionManifest scaffold with explicit default_fate=accept "
            "and one example decision per fate."
        ),
    )
    emit_consumer_parser.add_argument(
        "--out",
        default="",
        help="Optional file path to also write the emitted consumer decision template JSON.",
    )

    validate_parser = subparsers.add_parser(
        "validate-decision",
        help="Validate a coordinator DecisionManifest against current session.",
    )
    validate_parser.add_argument("--decision", required=True, help="Path to decision JSON.")

    validate_consumer_parser = subparsers.add_parser(
        "validate-consumer-decision",
        help=(
            "Validate ConsumerDecisionManifest JSON schema/uniqueness and optional "
            "run/session/recalled-cids correlation."
        ),
    )
    validate_consumer_parser.add_argument(
        "--file",
        required=True,
        help="Path to consumer decision manifest JSON.",
    )
    validate_consumer_parser.add_argument(
        "--run-id",
        default="",
        help="Optional expected run_id for correlation validation.",
    )
    validate_consumer_parser.add_argument(
        "--session-id",
        default="",
        help="Optional expected session_id for correlation validation.",
    )
    validate_consumer_parser.add_argument(
        "--recalled-cids",
        default="",
        help="Optional comma-separated recalled candidate CIDs for correlation validation.",
    )

    bridge_parser = subparsers.add_parser(
        "bridge",
        help="Consumer bridge lifecycle (start/status/stop/resume).",
    )
    bridge_parser.add_argument(
        "action",
        choices=["start", "status", "stop", "resume", "run-foreground"],
        help="Bridge action (run-foreground is an internal worker entrypoint).",
    )
    bridge_parser.add_argument(
        "--run-id",
        default="",
        help="Run identifier (required for start/resume/run-foreground).",
    )
    bridge_parser.add_argument(
        "--session-id",
        default="",
        help="Session identifier (required for start/resume/run-foreground).",
    )
    bridge_parser.add_argument(
        "--manifest-inbox",
        default="",
        help="Optional manifest inbox directory override.",
    )
    bridge_parser.add_argument(
        "--state-dir",
        default="",
        help="Optional consumer-gate plugin state directory override.",
    )
    bridge_parser.add_argument(
        "--served-store",
        default="",
        help="Optional served-memory store path override.",
    )
    bridge_parser.add_argument(
        "--lease-ttl-ms",
        type=int,
        default=DEFAULT_LEASE_TTL_MS,
        help=f"Bridge worker lease TTL in milliseconds (default: {DEFAULT_LEASE_TTL_MS}).",
    )
    bridge_parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=DEFAULT_POLL_INTERVAL_MS,
        help=f"Bridge poll interval in milliseconds (default: {DEFAULT_POLL_INTERVAL_MS}).",
    )
    bridge_parser.add_argument(
        "--heartbeat-cadence-ms",
        type=int,
        default=DEFAULT_HEARTBEAT_CADENCE_MS,
        help=(
            "Bridge heartbeat cadence in milliseconds "
            f"(default: {DEFAULT_HEARTBEAT_CADENCE_MS})."
        ),
    )
    bridge_parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Optional max loop cycles (primarily for bounded test/diagnostic runs).",
    )
    bridge_parser.add_argument(
        "--container-name",
        default="",
        help="Optional bridge container/worker name override.",
    )

    return parser


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def main() -> int:
    assert_primary_path()
    parser = _build_arg_parser()
    args = parser.parse_args()

    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "run": _handle_run,
        "resume": _handle_resume,
        "state": _handle_state,
        "list-pending": _handle_list_pending,
        "list-inventory": _handle_list_inventory,
        "reconcile-inventory": _handle_reconcile_inventory,
        "review-material": _handle_review_material,
        "emit-decision-template": _handle_emit_decision_template,
        "emit-consumer-decision-template": _handle_emit_consumer_decision_template,
        "validate-decision": _handle_validate_decision,
        "validate-consumer-decision": _handle_validate_consumer_decision,
        "bridge": _handle_bridge,
    }
    handler = handlers.get(str(args.command))
    if handler is None:
        raise RuntimeError(f"unsupported command: {args.command!r}")
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
