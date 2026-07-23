#!/usr/bin/env python3
"""Canonical primary scored cumulative benchmark CLI.

This script is **THE** canonical primary scored cumulative path for WeVibe.
`scripts/run_aider_solve.py` (Path C) and `scripts/backgammon_scored_ladder.py`
are diagnostic/historical paths and are **not** the active primary path.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import shutil
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, Mapping, NamedTuple

from wevibe_bench import config
from wevibe_bench.benv import load_bench_env
from wevibe_bench.cumulative.catalog import PrivateCatalog, PrivateReviewCard
from wevibe_bench.cumulative.decision import (
    CandidateDecision,
    DecisionManifest,
    IntegrityAttestation,
)
from wevibe_bench.cumulative.leader_client import LeaderClient
from wevibe_bench.cumulative.manifest import roster_hash as cumulative_roster_hash
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
from wevibe_bench.cumulative.types import RosterEntry, SessionRecord
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


class PathLayout(NamedTuple):
    manifest_path: Path
    runs_dir: Path
    catalog_path: Path
    review_card_path: Path
    safe_ledger_path: Path
    idempotency_ledger_path: Path


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


def assert_primary_path() -> None:
    """Raise if this module is not marked as the canonical primary path."""

    if IS_PRIMARY_SCORED_PATH is not True:
        raise AssertionError("run_cumulative.py lost primary-path marker")


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

    validate_parser = subparsers.add_parser(
        "validate-decision",
        help="Validate a coordinator DecisionManifest against current session.",
    )
    validate_parser.add_argument("--decision", required=True, help="Path to decision JSON.")

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
        "validate-decision": _handle_validate_decision,
    }
    handler = handlers.get(str(args.command))
    if handler is None:
        raise RuntimeError(f"unsupported command: {args.command!r}")
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
