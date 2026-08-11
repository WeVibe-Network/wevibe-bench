#!/usr/bin/env python3
"""Canonical primary scored cumulative benchmark CLI.

This script is **THE** canonical primary scored cumulative path for WeVibe.
`scripts/run_aider_solve.py` (Path C) and `scripts/backgammon_scored_ladder.py`
are diagnostic/historical paths and are **not** the active primary path.
"""

from __future__ import annotations

import argparse
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
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
from wevibe_bench.cumulative.progress import progress_from_cell_result
from wevibe_bench.cumulative.run_artifacts import (
    RunManifest,
    StatusStream,
    default_run_manifest_path,
    default_status_stream_path,
    write_run_manifest,
)
from wevibe_bench.cumulative.run_context import collect_run_context, compare_run_context
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
from wevibe_bench.cumulative.types import (
    PhaseGroup,
    RosterEntry,
    SessionPhase,
    SessionRecord,
)
from wevibe_bench.lifecycle.hub_client import HubClient
from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import LifecycleConfig
from wevibe_bench.lifecycle.m2_proof import M2Proof
from wevibe_bench.lifecycle.mcp_rest import McpRest
from wevibe_bench.preflight import verify_org_checklist, verify_worker_model_acceptance
from wevibe_bench.process_reaper import (
    ProcessReaper,
    run_reaper_unconditional,
)
from wevibe_bench.proxy_meter import SpendMeter
from wevibe_bench.spend_key import (
    key_fingerprint,
    resolve_local_llm_proxy_api_key,
    resolve_spend_db_dsn,
    resolve_spend_proxy_base_url,
    resolve_worker_spend_proxy_base_url,
)

IS_PRIMARY_SCORED_PATH = True
_LOG = logging.getLogger("run_cumulative")

DEFAULT_MANIFEST_PATH = Path("runs") / "cumulative" / "manifest.json"
DEFAULT_ORG_ID = "wevibe-org-0"
DEFAULT_PROXY_RUNS_DIR = Path("/Users/jerrysmith/Desktop/Local LLM Proxy/runs")
DEFAULT_TASK_LABEL = "backgammon-cumulative-primary"
DEFAULT_EXTRACT_TIMEOUT_S = 900
DEFAULT_SEED = config.RunConfig().rng_seed
DEFAULT_ON_BUDGET = 0

# FROZEN_TASK_TEMPLATE_HASH — WO-FREEZE-1 template freeze.
#
# SHA-256 over the live `tasks/backgammon/scaffold/` directory using the EXACT
# algorithm `compute_task_template_hash` applies at runtime (sorted relative
# path + raw bytes per file). Frozen at WO-FREEZE-1 (2026-08-06). Any change to
# the scaffold invalidates the hash and therefore every previously scored cell
# that ran against the old bytes — the run path fails closed until the freeze is
# re-baselined deliberately.
# Re-baselined 2026-08-10 (Walter, WO-FEEDBACK-CONTRACT): CONTRACT.md moved into
# the scaffold so the published requirements seed every worker worktree.
FROZEN_TASK_TEMPLATE_HASH = "08afc8011cde5b81e6e158def2bc040f42372bbc1e32e7ca125382c27031cdb1"


def compute_task_template_hash(scaffold: Path) -> str | None:
    """Stable SHA-256 over task scaffold files (sorted relative paths + bytes).

    Pure function: no instance state, no model endpoints. Returns the hexdigest
    over the concatenation of each file's utf-8-encoded relative path (sorted by
    ``str(path)``) followed by its raw bytes. Returns ``None`` when the scaffold
    directory is unavailable (mirrors the instance method's best-effort
    contract) and never raises for missing/unreadable files.
    """
    if scaffold is None or not scaffold.is_dir():
        return None
    digest = hashlib.sha256()
    files = sorted((p for p in scaffold.rglob("*") if p.is_file()), key=lambda p: str(p))
    for path in files:
        try:
            rel = str(path.relative_to(scaffold))
            digest.update(rel.encode("utf-8"))
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def verify_task_template_frozen() -> None:
    """Fail-closed template-freeze guard for the benchmark run path.

    Computes the live scaffold hash (via ``compute_task_template_hash`` over
    ``tasks/backgammon/scaffold``) and raises a RuntimeError naming BOTH the
    expected (frozen) and actual (live) hashes plus the scaffold path whenever
    they differ OR the live hash cannot be computed. Purposely touches no model
    endpoint/proxy. Must be called before any scaffold copy or cell scoring.
    """
    repo_root = Path(__file__).resolve().parent.parent
    scaffold = repo_root / "tasks" / "backgammon" / "scaffold"
    live_hash = compute_task_template_hash(scaffold)
    if live_hash is None:
        raise RuntimeError(
            "task template freeze FAILED: scaffold unavailable at "
            f"{scaffold}; expected frozen hash {FROZEN_TASK_TEMPLATE_HASH}, "
            "could not compute live hash"
        )
    if live_hash != FROZEN_TASK_TEMPLATE_HASH:
        raise RuntimeError(
            "task template freeze FAILED: scaffold mismatch at "
            f"{scaffold}; expected (frozen) {FROZEN_TASK_TEMPLATE_HASH}, "
            f"actual (live) {live_hash}"
        )


def _read_proxy_served_identity(proxy_runs_dir: Path | None = None) -> str | None:
    """Read the API-reported served model identity from the relay proxy run logs.

    The local relay proxy writes per-day JSONL run logs carrying ``type:
    "request"`` rows. A genuine served identity is such a row whose
    ``upstreamModel`` is a non-empty string and differs from ``requestedModel``
    (alias-echo rows are rejected). Returns the latest genuine ``upstreamModel``
    across today's file and, for runs crossing midnight, the previous day's file
    (when the current UTC hour is < 6). Never raises; missing/unparseable input
    degrades to ``None``.
    """
    if proxy_runs_dir is None:
        proxy_runs_dir = Path(
            os.environ.get("WEVIBE_PROXY_RUNS_DIR", str(DEFAULT_PROXY_RUNS_DIR))
        )
    now_utc = datetime.now(timezone.utc)
    candidate_dates = [now_utc]
    if now_utc.hour < 6:
        candidate_dates.append(now_utc - timedelta(days=1))

    latest_served: str | None = None
    latest_ts: str = ""
    for candidate in candidate_dates:
        run_log = proxy_runs_dir / f"{candidate.strftime('%Y-%m-%d')}.jsonl"
        try:
            with open(run_log, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        row = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict) or row.get("type") != "request":
                        continue
                    requested = row.get("requestedModel")
                    upstream = row.get("upstreamModel")
                    if not isinstance(upstream, str) or not upstream.strip():
                        continue
                    if upstream.strip() == requested:
                        continue
                    ts = row.get("ts")
                    if not isinstance(ts, str):
                        continue
                    if latest_served is None or ts > latest_ts:
                        latest_served = upstream.strip()
                        latest_ts = ts
        except OSError:
            continue
    return latest_served


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
    runner: object


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


def _runs_root_from_args(args: argparse.Namespace) -> Path:
    manifest = Path(str(getattr(args, "manifest", None) or DEFAULT_MANIFEST_PATH))
    return manifest.expanduser().resolve().parent.parent


def _prune_runs_retention(runs_root: Path, *, keep: int = 2) -> dict[str, Any]:
    """Prune accumulated run artifacts under ``runs/`` (Walter 2026-08-10).

    Retention: the newest ``keep`` launch logs (``off-cell-*.log``) and the
    newest ``keep`` archived run states (``cumulative.*`` dirs) survive;
    anything older is deleted. The live ``cumulative/`` state dir is never
    touched, and no entry outside those two classes is either — so a failed
    run's evidence (the latest, plus one prior) is always available for
    post-mortem while long-term accumulation stays bounded. Hooked into
    every mutating command's exit path (the early-cancellation flow:
    normal return, exception, and KeyboardInterrupt all land in main()'s
    finally). Supersedes the blanket "archive, never delete" recovery note
    for archives older than latest+1. Never raises: a prune failure must
    not alter the command's exit path or code.
    """
    summary: dict[str, Any] = {"kept": [], "deleted": [], "skipped_root": None}
    try:
        if not runs_root.is_dir():
            summary["skipped_root"] = str(runs_root)
            return summary
        classes = (
            sorted(runs_root.glob("off-cell-*.log"), key=lambda p: p.stat().st_mtime, reverse=True),
            [p for p in sorted(runs_root.glob("cumulative.*"), key=lambda p: p.stat().st_mtime, reverse=True) if p.is_dir()],
        )
        for entries in classes:
            for idx, path in enumerate(entries):
                if idx < keep:
                    summary["kept"].append(path.name)
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                summary["deleted"].append(path.name)
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary



def _normalize_model_slug(model: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", model).strip("-")
    return slug or "model"


def _producer_model_id_from_model(model: str) -> str:
    model_value = str(model).strip()
    if not model_value:
        raise RuntimeError("producer model cannot be empty")

    parts = [part for part in model_value.split("/") if part]
    if parts and parts[0] == "local-llm-proxy":
        parts = parts[1:]

    producer_model_id = "/".join(parts)
    if not producer_model_id:
        raise RuntimeError(f"unable to resolve producer model from model={model!r}")

    if not _normalize_model_slug(producer_model_id):
        raise RuntimeError(f"unable to normalize producer model from model={model!r}")

    return producer_model_id


def _provider_pin_from_model(model: str) -> str:
    parts = [part for part in model.split("/") if part]
    if not parts:
        raise ValueError("model slug must be non-empty")
    if len(parts) >= 2 and parts[0] == "local-llm-proxy":
        return parts[1]
    return parts[0]


def _apply_model_override(
    slugs: list[str],
    *,
    model_override: str | None,
) -> list[str]:
    """Apply the operator's --model selection to the roster slug list.

    The override names a proxy bench alias present in WORKER_MODEL_REGISTRY
    (e.g. ``qwen3.6-35b-a3b-bench``); the resulting roster slug is
    ``local-llm-proxy/<alias>``. The proxy makes that exact model resident on
    the first request (exclusive load on call). Valid only against the
    single-subject roster — a multi-rung roster has no defined override
    semantics, so it errors rather than guessing. Identity is still observed
    from API responses and recorded (RC-7); this flag selects, it does not
    gate. Changing the model changes the roster hash, which invalidates an
    existing manifest by design (archive + rerun, RUNBOOK §0).
    """
    override = str(model_override or "").strip()
    if not override:
        return slugs
    alias = override
    if alias.startswith("local-llm-proxy/"):
        alias = alias[len("local-llm-proxy/"):]
    registry = getattr(config, "WORKER_MODEL_REGISTRY", {})
    if alias not in registry:
        available = ", ".join(sorted(str(key) for key in registry)) or "none"
        print(
            f"error: --model {model_override!r} is not a known worker model alias. "
            f"available aliases: {available}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if len(slugs) != 1:
        print(
            "error: --model override requires a single-subject roster "
            f"(resolved {len(slugs)} slugs: {', '.join(slugs)})",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return [f"local-llm-proxy/{alias}"]


def _build_roster(
    *,
    roster_model: str | None = None,
    model_override: str | None = None,
) -> tuple[list[RosterEntry], str]:
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
    roster_model_filter = str(roster_model or "").strip()
    if roster_model_filter:
        marker = roster_model_filter.casefold()
        filtered = [entry for entry in roster if marker in entry.model.casefold()]
        if not filtered:
            available = ", ".join(entry.model for entry in roster)
            print(
                "error: --roster-model filter matched zero roster entries "
                f"({roster_model_filter!r}). available models: {available}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        roster = filtered
        _LOG.info(
            "run_cumulative.roster_filter filter=%s matched=%d models=%s",
            roster_model_filter,
            len(roster),
            ",".join(entry.model for entry in roster),
        )
    if not roster:
        raise RuntimeError("backgammon_scored_ladder_roster resolved empty")
    override_slugs = _apply_model_override(
        [entry.model for entry in roster],
        model_override=model_override,
    )
    if override_slugs != [entry.model for entry in roster]:
        roster = [
            RosterEntry(
                model=slug,
                role=entry.role,
                provider_pin=_provider_pin_from_model(slug),
                config_identity=entry.config_identity,
            )
            for entry, slug in zip(roster, override_slugs)
        ]
        _LOG.info(
            "run_cumulative.model_override models=%s",
            ",".join(entry.model for entry in roster),
        )
    return roster, cumulative_roster_hash(roster)


def _resolve_extract_base_url() -> str | None:
    value = resolve_spend_proxy_base_url().strip()
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


def _resolve_positive_int_env(
    name: str,
    *,
    optional: bool,
) -> tuple[int | None, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return (None if optional else config.RunConfig().max_attempts), "default"
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value, "env"


def _resolve_extract_api_key() -> tuple[str, str]:
    return resolve_local_llm_proxy_api_key()


def _load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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
        model: str,
        session_db_path: str,
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
            session_db_path=session_db_path,
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


def _atomic_write_json_private(path: Path, payload: Mapping[str, Any]) -> None:
    if not isinstance(path, Path):
        raise ValueError("path must be a pathlib.Path")
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


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
        proxy_base_url: str | None = None,
        proxy_token: str | None = None,
        run_manifest_base_path: str | None = None,
        seed: int | None = None,
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
        self._proxy_base_url = proxy_base_url
        self._proxy_token = proxy_token

        self._task_dir = self._repo_root / "tasks" / "backgammon"
        if not self._task_dir.is_dir():
            raise RuntimeError(f"backgammon task directory missing: {self._task_dir}")

        self._strategy_e_prompt_path = self._repo_root / "scaffold" / "sxe-candidate" / "E-assembled.txt"
        self._strategy_s_prompt_path = self._repo_root / "scaffold" / "sxe-candidate" / "S-fork-reasoning.md"
        self._strategy_s_prompt = _load_required_text(self._strategy_s_prompt_path)

        self._max_attempts, self._max_attempts_source = _resolve_positive_int_env(
            "WEVIBE_BENCH_MAX_ATTEMPTS",
            optional=False,
        )
        self._max_steps_per_attempt, self._max_steps_per_attempt_source = _resolve_positive_int_env(
            "WEVIBE_BENCH_MAX_STEPS_PER_ATTEMPT",
            optional=True,
        )
        self._run_timeout_s, self._run_timeout_s_source = _resolve_positive_int_env(
            "WEVIBE_BENCH_RUN_TIMEOUT_S",
            optional=True,
        )
        self._spend_meter = SpendMeter(resolve_spend_db_dsn())
        self._session_states: dict[int, _SessionRunState] = {}

        from wevibe_bench.adapters.backgammon import BackgammonRunner

        self._runner_cls = BackgammonRunner

        # Write-once run-manifest + append-only status stream sit as siblings of
        # the MUTABLE cumulative manifest. ``run_manifest_base_path`` is the
        # mutable manifest path; when None it falls back to
        # ``<runs_dir>/manifest.json``. The manifest is written exactly once
        # per run, guarded so subsequent sessions never attempt a rewrite.
        self._run_manifest_base_path = (
            str(run_manifest_base_path)
            if run_manifest_base_path is not None
            else str(Path(self._runs_dir) / "manifest.json")
        )
        self._run_manifest_written = False
        self._seed = seed

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

    @staticmethod
    def _wall_near_timeout(wall_seconds: Any, run_timeout_s: int | None) -> bool:
        if run_timeout_s is None:
            return False
        try:
            return float(wall_seconds) >= 0.98 * float(run_timeout_s)
        except (TypeError, ValueError):
            return False

    def _populate_contention_covariates(self, result: Any) -> None:
        from wevibe_bench.contention import ContentionCovariates

        retry_count = int(getattr(result, "zero_tool_resumes", 0) or 0)
        wall_seconds_raw = getattr(result, "wall_seconds", None)
        wall_seconds = float(wall_seconds_raw) if wall_seconds_raw is not None else None
        wall_near_timeout = self._wall_near_timeout(
            wall_seconds,
            getattr(self, "_run_timeout_s", None),
        )
        spend_meter = getattr(self, "_spend_meter", None)
        if spend_meter is None:
            spend_meter = SpendMeter(resolve_spend_db_dsn())
            self._spend_meter = spend_meter

        try:
            contention = spend_meter.contention_covariates(
                getattr(result, "session_id", None),
                retry_count=retry_count,
                wall_seconds=wall_seconds,
                wall_near_timeout=wall_near_timeout,
            )
        except Exception as exc:  # observability failure must not discard an expensive cell
            _LOG.exception(
                "run_cumulative.contention_covariates_failed session_fp=%s error_type=%s",
                SessionRecord.session_fp_of(result.session_id)
                if isinstance(getattr(result, "session_id", None), str)
                and result.session_id.strip()
                else "none",
                type(exc).__name__,
            )
            contention = ContentionCovariates.empty(
                retry_count=retry_count,
                wall_seconds=wall_seconds,
                wall_near_timeout=wall_near_timeout,
            )

        result.contention = contention

    @staticmethod
    def _current_git_head(repo_root: Path | None) -> str | None:
        """Best-effort source git commit; None on any failure. Never raises."""
        if repo_root is None:
            return None
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            _LOG.warning(
                "run_cumulative.git_head_failed error_type=%s",
                type(exc).__name__,
            )
            return None
        if completed.returncode != 0:
            return None
        commit = str(completed.stdout).strip()
        return commit or None

    def _compute_task_template_hash(self) -> str | None:
        """Stable SHA-256 over task scaffold files (sorted relative paths + bytes).

        Best-effort; None when the scaffold directory is unavailable. Never
        raises. Delegates to the pure module function ``compute_task_template_hash``.
        """
        task_dir = getattr(self, "_task_dir", None)
        if task_dir is None:
            return None
        scaffold = Path(task_dir) / "scaffold"
        return compute_task_template_hash(scaffold)

    @staticmethod
    def _serialize_worker_fingerprint(result: Any) -> dict | str | None:
        """Serialize an ImageFingerprint (image_id/created) as dict, or str."""
        fingerprint = getattr(result, "worker_image_fingerprint", None)
        if fingerprint is None:
            return None
        if isinstance(fingerprint, Mapping):
            return dict(fingerprint)
        to_dict = getattr(fingerprint, "to_dict", None)
        if callable(to_dict):
            try:
                rendered = to_dict()
                if isinstance(rendered, Mapping):
                    return dict(rendered)
            except Exception:
                pass
        return str(fingerprint)

    def _observe_served_model(
        self,
        session_id: Any,
        requested_model: str,
    ) -> tuple[str | None, dict | None]:
        """Observe the API-reported served model; never aborts the run.

        Returns ``(upstream_str, served_dict)`` where ``served_dict`` has the
        ``{"model": requested, "upstream_model": served|None}`` shape. Both are
        None on failure or when the spend DB records nothing (local pivot).
        """
        session_id_str = (
            str(session_id).strip()
            if isinstance(session_id, str) and str(session_id).strip()
            else None
        )
        if session_id_str is None:
            return None, None
        proxy_served = _read_proxy_served_identity()
        if proxy_served:
            served_dict = {
                "model": str(requested_model),
                "upstream_model": proxy_served,
            }
            return proxy_served, served_dict
        spend_meter = getattr(self, "_spend_meter", None)
        if spend_meter is None:
            return None, None
        try:
            identities = spend_meter.model_identity(session_id_str)
        except Exception as exc:
            _LOG.exception(
                "run_cumulative.model_identity_failed session_fp=%s error_type=%s",
                SessionRecord.session_fp_of(session_id_str),
                type(exc).__name__,
            )
            return None, None
        if not identities:
            return None, None
        first = identities[0]
        upstream = getattr(first, "upstream_model", None)
        model = getattr(first, "model", None)
        served_upstream = str(upstream).strip() if upstream is not None else None
        if not served_upstream:
            model_str = str(model).strip() if model is not None else ""
            served_upstream = model_str or None
        served_dict = {
            "model": str(requested_model),
            "upstream_model": served_upstream,
        }
        return served_upstream, served_dict

    def _write_run_manifest_once(
        self,
        *,
        session: SessionRecord,
        served_model: str | None,
        result: Any,
    ) -> None:
        """Write the write-once run manifest after the first served-model observation.

        Instrumentation-only: must never alter run behaviour or abort the run.
        """
        if getattr(self, "_run_manifest_written", False):
            return
        run_manifest_base_path = (
            getattr(self, "_run_manifest_base_path", None)
            or str(Path(self._runs_dir) / "manifest.json")
        )
        try:
            manifest = RunManifest(
                run_id=Path(self._runs_dir).name,
                created_at=_utc_now_iso(),
                served_model=served_model,
                requested_model=str(session.model),
                memory_mode=str(session.memory_mode),
                org_id=str(getattr(self, "_org_id", None) or ""),
                source_commit=self._current_git_head(getattr(self, "_repo_root", None)),
                worker_image_fingerprint=self._serialize_worker_fingerprint(result),
                seed=getattr(self, "_seed", None),
                template_hash=self._compute_task_template_hash(),
                roster_fingerprint=None,
            )
            write_run_manifest(
                default_run_manifest_path(run_manifest_base_path),
                manifest,
            )
            self._run_manifest_written = True
        except FileExistsError:
            # Already written by an earlier session — write-once invariant.
            self._run_manifest_written = True
        except Exception as exc:
            _LOG.exception(
                "run_cumulative.run_manifest_write_failed run_id=%s error_type=%s",
                Path(self._runs_dir).name,
                type(exc).__name__,
            )

    def _append_status_records(
        self,
        *,
        session: SessionRecord,
        result: Any,
        served_model: dict | None,
    ) -> None:
        """Append per-attempt status records to the append-only stream.

        Instrumentation-only: must never alter run behaviour or abort the run.
        """
        try:
            progress_dict = progress_from_cell_result(result).to_dict()
        except Exception as exc:
            _LOG.exception(
                "run_cumulative.status_progress_failed sequence_index=%s error_type=%s",
                session.sequence_index,
                type(exc).__name__,
            )
            return

        input_tokens = int(getattr(result, "input_tokens", 0) or 0)
        output_tokens = int(getattr(result, "output_tokens", 0) or 0)
        progress_injected_est = progress_dict.get("injected_block_est_tokens")
        progress_consumer_injected = progress_dict.get("consumer_injected_count")

        session_id = getattr(result, "session_id", None)
        session_fp = (
            SessionRecord.session_fp_of(session_id)
            if isinstance(session_id, str) and session_id.strip()
            else None
        )

        verdict_str = str(getattr(result, "verdict", "") or "")
        base: dict[str, Any] = {
            "type": "attempt",
            "schema_version": 1,
            "sequence_index": session.sequence_index,
            "memory_mode": str(session.memory_mode),
            "org_id": str(getattr(self, "_org_id", None) or ""),
            "served_model": served_model,
            "progress": progress_dict,
            "work_input_tokens": input_tokens,
            "work_output_tokens": output_tokens,
            "work_total_tokens": input_tokens + output_tokens,
            "injected_block_est_tokens": progress_injected_est,
            "injected_count": getattr(result, "injected_count", None),
            "injected_block_chars": getattr(result, "injected_block_chars", None),
            "consumer_injected_count": progress_consumer_injected,
            "extraction_state": "unknown",
            "extraction_candidate_count": None,
            # WO-TRUNC-1: the terminal outcome is now recorded, not placeholder-
            # null. True = the cell resolved (verdict PASS); False = every other
            # ending. terminal_reason carries the machine reason so a scorecard
            # can tell "the model failed" (attempt_ceiling_reached) from "the
            # stream died" (transport_incomplete / harness_error).
            "terminal_outcome": verdict_str == "PASS",
            "terminal_reason": str(getattr(result, "termination_reason", "") or ""),
            # Truncated-turn accounting (WO-TRUNC-1): anomalous turn endings are
            # first-class. length_truncations is the metered finish_reason=
            # length class; the truncated_* fields are the no-signal classes
            # whose upstream token burn is unmetered client-side (never
            # synthesized) but whose wall-clock is measured and real.
            "length_truncations": int(getattr(result, "truncations", 0) or 0),
            "truncated_turns": int(getattr(result, "truncated_turns", 0) or 0),
            "truncated_turns_retried": int(getattr(result, "truncated_turns_retried", 0) or 0),
            # Guard-killed turns excluded from scoring turns (WO-TURNACCT-1) —
            # carried so the exclusion is visible in the ledger, never silent.
            "guard_aborted_turns": int(getattr(result, "guard_aborted_turns", 0) or 0),
            # Finalize-killed turns, excluded from scoring turns on the same
            # grounds (WO-NUDGE-INF-1). Scoring turns are
            # `turns - guard_aborted_turns - finalize_timeout_turns`, so a
            # scorecard that cannot read this subtrahend cannot reconstruct the
            # measurement. RC-5 makes the manifest plus this status stream the
            # ONLY sources a scorecard may use, so a value carried solely on a
            # PROGRESS log line is invisible to it — which is why this is here
            # and not left to the log.
            #
            # NOTE: `recovery_nudges` is deliberately NOT emitted. It exists on
            # the internal per-phase `_OpencodeRunStats` only and never reaches
            # `BackgammonCellResult`, so emitting it here would silently write a
            # constant 0 and fabricate the appearance of a measurement. The
            # nudge count stays observable on the PROGRESS line until it is
            # plumbed through the cell result properly.
            "finalize_timeout_turns": int(getattr(result, "finalize_timeout_turns", 0) or 0),
            "unmetered_turns": int(getattr(result, "unmetered_turns", 0) or 0),
            "unmetered_turn_wall_s": float(getattr(result, "unmetered_turn_wall_s", 0.0) or 0.0),
            "session_fp": session_fp,
            "session_id": session_id,
        }

        stream = StatusStream(
            default_status_stream_path(
                getattr(self, "_run_manifest_base_path", None)
                or str(Path(self._runs_dir) / "manifest.json")
            )
        )
        attempt_reports = getattr(result, "attempt_reports", None)
        if isinstance(attempt_reports, list) and attempt_reports:
            for idx, attempt in enumerate(attempt_reports, start=1):
                attempt_record = dict(base)
                if isinstance(attempt, Mapping):
                    attempt_record["attempt"] = attempt.get("attempt", idx)
                    attempt_record["verdict"] = attempt.get("verdict", result.verdict)
                    attempt_record["n_problems"] = attempt.get("n_problems")
                    attempt_record["failed_gates"] = list(attempt.get("failed_gates", []) or [])
                    attempt_record["conformed"] = attempt.get("conformed")
                    attempt_record["attempt_cost_usd"] = attempt.get("attempt_cost_usd")
                else:
                    attempt_record["attempt"] = idx
                    attempt_record["verdict"] = result.verdict
                attempt_record["termination_reason"] = getattr(result, "termination_reason", "")
                attempt_record["attempts_to_green"] = getattr(result, "attempts_to_green", None)
                stream.append(attempt_record)
        else:
            record = dict(base)
            record["attempt"] = 1
            record["verdict"] = getattr(result, "verdict", "")
            record["termination_reason"] = getattr(result, "termination_reason", "")
            record["attempts_to_green"] = getattr(result, "attempts_to_green", None)
            stream.append(record)

        # WO-TRUNC-1: one turn_terminal record per anomalously-ended turn. These
        # are records, never rewrites — the stream stays append-only, and a run
        # that died mid-cell simply has fewer of them. `terminal` +
        # `reason` distinguish truncated_no_signal / guard_abort /
        # transport_error / stream_died_open / unclassified_finish from every
        # other way a turn can end; `retried`/`retry_kind` make the
        # burned-then-retried pair attributable.
        turn_anomalies = getattr(result, "turn_anomalies", None)
        if isinstance(turn_anomalies, list):
            for anomaly in turn_anomalies:
                if not isinstance(anomaly, Mapping):
                    continue
                turn_record = {
                    "type": "turn_terminal",
                    "schema_version": 1,
                    "sequence_index": session.sequence_index,
                    "memory_mode": str(session.memory_mode),
                    "org_id": str(getattr(self, "_org_id", None) or ""),
                    "session_fp": session_fp,
                    "session_id": session_id,
                    "phase": str(anomaly.get("phase", "")),
                    "turn_index": anomaly.get("turn_index"),
                    "terminal": str(anomaly.get("terminal", "")),
                    "reason": str(anomaly.get("reason", "")),
                    "tool_uses": anomaly.get("tool_uses"),
                    "file_writes": anomaly.get("file_writes"),
                    "input_tokens": anomaly.get("input_tokens"),
                    "output_tokens": anomaly.get("output_tokens"),
                    "reasoning_tokens": anomaly.get("reasoning_tokens"),
                    "cost_usd": anomaly.get("cost_usd"),
                    "tokens_unmetered": bool(anomaly.get("tokens_unmetered")),
                    "wall_seconds": anomaly.get("wall_seconds"),
                    "retried": bool(anomaly.get("retried")),
                    "retry_kind": anomaly.get("retry_kind"),
                }
                stream.append(turn_record)

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
        provider = "local-llm-proxy"
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
        verify_task_template_frozen()
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

        max_steps_per_attempt = getattr(self, "_max_steps_per_attempt", None)
        run_timeout_s = getattr(self, "_run_timeout_s", None)
        max_attempts_source = getattr(self, "_max_attempts_source", "default")
        max_steps_source = getattr(self, "_max_steps_per_attempt_source", "default")
        run_timeout_source = getattr(self, "_run_timeout_s_source", "default")

        runner_kwargs: dict[str, Any] = {
            "task_dir": self._task_dir,
            "work_root": state.run_dir,
            "model": session.model,
            "memory_mode": session.memory_mode,
            "max_attempts": self._max_attempts,
            "proxy_base_url": self._proxy_base_url,
            "proxy_token": self._proxy_token,
            "logger": _LOG,
            "progress": self._progress,
        }
        if max_steps_per_attempt is not None:
            runner_kwargs["max_steps_per_attempt"] = max_steps_per_attempt
        if run_timeout_s is not None:
            runner_kwargs["run_timeout_s"] = run_timeout_s

        _LOG.info(
            "run_cumulative.pacing max_attempts=%s max_steps_per_attempt=%s run_timeout_s=%s "
            "max_attempts_source=%s max_steps_per_attempt_source=%s run_timeout_s_source=%s",
            self._max_attempts,
            max_steps_per_attempt,
            run_timeout_s,
            max_attempts_source,
            max_steps_source,
            run_timeout_source,
        )

        runner = self._runner_cls(
            **runner_kwargs,
        )
        result = runner.run_cell(state.run_label, state.run_dir, task_id="backgammon")
        state.last_session_id = result.session_id or state.last_session_id
        self._populate_contention_covariates(result)

        # Instrumentation-only run artifacts (write-once manifest + append-only
        # status stream). Must never alter scoring/extraction/gates/feedback or
        # abort the run; served_model is only known after the first run_cell.
        served_upstream, served_dict = self._observe_served_model(
            getattr(result, "session_id", None),
            str(session.model),
        )
        self._write_run_manifest_once(
            session=session,
            served_model=served_upstream,
            result=result,
        )
        self._append_status_records(
            session=session,
            result=result,
            served_model=served_dict,
        )

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

        # The session DB is the substrate. The bench hands WeVibe the path and
        # WeVibe projects it (D-SESSION-SUBSTRATE: one builder, shared by the
        # dashboard Extract path and the benchmark). The bench builds nothing.
        session_db_path = state.run_dir / "session-db" / "opencode.db"
        if not session_db_path.is_file():
            raise RuntimeError(
                "missing session database for extraction "
                f"sequence_index={session.sequence_index}: {session_db_path}"
            )

        session_id = state.last_session_id

        self._contributor_rest.last_job_id = None
        extract_model, extract_provider = self._extract_model_for_session(session)
        producer_model_id = _producer_model_id_from_model(session.model)
        if not producer_model_id.strip():
            raise RuntimeError(
                f"unable to resolve producer_model_id from session.model={session.model!r}"
            )
        project_context = self._project_context_for_session(session, state)

        try:
            memories = self._proof.produce_memories(
                session_db_path=str(session_db_path),
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
                    "producer_model": producer_model_id,
                }
            )

        _LOG.info(
            "run_cumulative.extract sequence_index=%d memory_mode=%s job_id=%s session_fp=%s candidate_count=%d session_db=%s",
            session.sequence_index,
            session.memory_mode,
            extraction_job_id,
            SessionRecord.session_fp_of(session_id),
            len(candidate_refs),
            session_db_path,
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


def ensure_org(
    cfg: LifecycleConfig,
    wevibe_root: Path,
    leader: Identity,
    contributor: Identity,
    requested_org: str,
    logger: logging.Logger,
    orchestrator_factory: Callable[..., LifecycleOrchestrator] | None = None,
) -> str:
    """Idempotently ensure the requested org exists and is gate-ready.

    Reuses LifecycleOrchestrator.run_m1() (seeds keywords + org profile, exactly
    what verify_org_checklist gates on). run_m1 reuses an already-owned org
    (no recreation) or mints a fresh one via leader-signer register-org. The
    requested org is pinned via dataclasses.replace on a fresh LifecycleConfig
    (LifecycleConfig is frozen). Returns the resolved org_id.
    """
    from wevibe_bench.lifecycle.orchestrator import LifecycleOrchestrator
    from wevibe_bench.lifecycle.mcp_process import McpProcessManager, _resolve_role_keystore

    ensure_cfg = dataclass_replace(cfg, org_id=requested_org)
    if orchestrator_factory is not None:
        orch = orchestrator_factory(ensure_cfg)
    else:
        leader_keystore, _ = _resolve_role_keystore(ensure_cfg, "leader")
        contributor_keystore, _ = _resolve_role_keystore(ensure_cfg, "contributor")
        leader_wallet = os.environ.get("WEVIBE_BENCH_LEADER_WALLET", "")
        procman = McpProcessManager(
            wevibe_root=str(wevibe_root),
            cfg=ensure_cfg,
            logger=logger,
        )
        orch = LifecycleOrchestrator(
            cfg=ensure_cfg,
            wevibe_root=str(wevibe_root),
            leader=leader,
            contributor=contributor,
            leader_keystore=leader_keystore,
            contributor_keystore=contributor_keystore,
            leader_wallet=leader_wallet,
            logger=logger,
            procman=procman,
        )
    result = orch.run_m1()
    org_id = result.get("org_id")
    if not isinstance(org_id, str) or not org_id:
        raise RuntimeError(f"org-ensure run_m1 returned no org_id: {result}")
    logger.info("run_cumulative.org_ensured requested=%s resolved=%s", requested_org, org_id)
    return org_id


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
    proxy_base_url_arg = str(getattr(args, "proxy_base_url", "") or "").strip()
    proxy_base_url = proxy_base_url_arg or resolve_worker_spend_proxy_base_url()
    proxy_token: str | None = None
    proxy_token_source = ""
    proxy_token_file = str(getattr(args, "proxy_token_file", "") or "").strip()
    if proxy_token_file:
        proxy_token = Path(proxy_token_file).expanduser().read_text(encoding="utf-8").strip()
        proxy_token_source = f"proxy_token_file:{Path(proxy_token_file).expanduser()}"
        _LOG.info(
            "run_cumulative.proxy_token_loaded file=%s token_sha256_first8=%s",
            str(Path(proxy_token_file).expanduser()),
            key_fingerprint(proxy_token),
        )
    else:
        proxy_token, resolved_source = resolve_local_llm_proxy_api_key()
        proxy_token_source = resolved_source

    _LOG.info(
        "run_cumulative.proxy_source_resolved source=%s base_url=%s token_fp=%s",
        proxy_token_source,
        proxy_base_url,
        key_fingerprint(proxy_token),
    )

    leader = Identity.from_hex(_required_env("WEVIBE_BENCH_LEADER_SEED_HEX"))
    contributor = Identity.from_hex(_required_env("WEVIBE_BENCH_CONTRIB_SEED_HEX"))

    requested_org = str(getattr(args, "org", "") or "").strip() or None
    org_id = requested_org or DEFAULT_ORG_ID
    if requested_org:
        org_id = ensure_org(
            cfg=cfg,
            wevibe_root=os.environ.get("WEVIBE_BENCH_WEVIBE_ROOT", str(repo_root.parent)),
            leader=leader,
            contributor=contributor,
            requested_org=requested_org,
            logger=_LOG,
        )
    args.org = org_id

    hub_client = HubClient(cfg, _LOG)
    verify_org_checklist(
        hub_url=cfg.hub_url,
        org_id=str(args.org),
        identity=leader,
        logger=_LOG,
    )
    roster_filter = str(getattr(args, "roster_model", "") or "").strip()
    roster_marker = roster_filter.casefold() if roster_filter else None
    accepted_models: list[str] = []
    seen_models: set[str] = set()
    for rung in config.BACKGAMMON_SCORED_LADDER_ROSTER:
        slug = str(rung.model)
        if roster_marker and roster_marker not in slug.casefold():
            continue
        if slug in seen_models:
            continue
        seen_models.add(slug)
        accepted_models.append(slug)
    accepted_models = _apply_model_override(
        accepted_models,
        model_override=str(getattr(args, "model", "") or "").strip() or None,
    )
    verify_worker_model_acceptance(models=accepted_models, logger=_LOG)
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
    extract_base_url = _resolve_extract_base_url()
    _LOG.info(
        "run_cumulative.extract_llm_route provider=%s base_url=%s key_source=%s key_fp=%s",
        "local-llm-proxy",
        extract_base_url,
        extract_api_key_source,
        key_fingerprint(extract_api_key),
    )
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
        extract_base_url=extract_base_url,
        extract_num_ctx=_resolve_extract_num_ctx(),
        extract_timeout_s=_resolve_extract_timeout_s(),
        proxy_base_url=proxy_base_url,
        proxy_token=proxy_token,
        run_manifest_base_path=str(layout.manifest_path),
        seed=int(args.seed),
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
    # Card §2: --org is first-class; when omitted, every command falls back to
    # the campaign default org. Centralised here so all subcommands (run,
    # state, extract, ...) resolve identically — previously str(None) reached
    # the sequencer and `state` without --org died on a false org-drift error
    # (2026-08-09). The ON-cell requirement (--mode on requires an explicit
    # --org) is enforced in _handle_run BEFORE this fallback is applied.
    if not str(getattr(args, "org", "") or "").strip():
        args.org = DEFAULT_ORG_ID
    layout = _resolve_manifest_layout(str(args.manifest))
    layout.runs_dir.mkdir(parents=True, exist_ok=True)
    review_card = PrivateReviewCard(str(layout.review_card_path))

    roster_model_filter = str(getattr(args, "roster_model", "") or "").strip() or None
    model_override = str(getattr(args, "model", "") or "").strip() or None
    roster, _ = _build_roster(roster_model=roster_model_filter, model_override=model_override)
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

    current_run_context = collect_run_context()

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
        run_context=current_run_context,
        chunk_plan_hash=compute_task_template_hash(
            Path(__file__).resolve().parents[1] / "tasks" / "backgammon" / "prompts"
        )
        or "",
        require_delivery_verification=config.RunConfig().require_delivery_verification,
    )
    recorded_run_context = getattr(getattr(sequencer, "_manifest"), "run_context", None)
    drift = compare_run_context(recorded_run_context, current_run_context)
    if drift:
        _LOG.warning("op=run_context.drift differing_keys=%s", ",".join(drift))
    return CliContext(
        sequencer=sequencer,
        leader_client=leader_client,
        review_card=review_card,
        layout=layout,
        runner=runner,
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
    # Fail-open telemetry retention (data/ is a retention layer, never a source
    # of truth; a cleanup failure must never stop a run). Skip with
    # WEVIBE_BENCH_SKIP_CLEANUP=1.
    try:
        if os.environ.get("WEVIBE_BENCH_SKIP_CLEANUP", "") != "1":
            from cleanup_data import run_cleanup  # noqa: PLC0415 -- fail-open

            _removed = run_cleanup()
            _LOG.info("run_cumulative telemetry cleanup done; removed=%s", _removed)
    except Exception as _cleanup_exc:  # noqa: BLE001 -- fail-open, never break a run
        _LOG.warning("run_cumulative telemetry cleanup failed (fail-open): %r", _cleanup_exc)
    if not bool(args.until_review):
        raise RuntimeError("run requires --until-review")

    validated_mode = str(getattr(args, "mode", "") or "").strip().lower() or None
    validated_org = str(getattr(args, "org", "") or "").strip()
    if validated_mode == "on" and not validated_org:
        raise RuntimeError(
            "--mode on requires --org <org>: an ON cell needs a target org. "
            "Pass --org <org> (created idempotently if absent) or use --mode off."
        )

    context = _build_context(args, require_runtime=True)
    session = _current_session_or_raise(context.sequencer)
    mode_arg = str(getattr(args, "mode", "") or "").strip().lower() or None
    if mode_arg is not None and mode_arg != str(session.memory_mode):
        raise RuntimeError(
            f"--mode {mode_arg} requested but current cell is memory_mode={session.memory_mode}"
        )

    result = context.sequencer.step_until_review()
    if result["status"] == "awaiting_extract":
        _print_json(
            {
                "status": "awaiting_extract",
                "sequence_index": int(result["sequence_index"]),
                "session_fp": str(result["session_fp"]),
                "memory_mode": str(result["memory_mode"]),
            }
        )
    elif result["status"] == "awaiting_coordinator_review":
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


def _handle_extract(args: argparse.Namespace) -> int:
    context = _build_context(args, require_runtime=True)
    result = context.sequencer.extract_current()
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
        default=None,
        help="Org id for extraction/commit flow; required for the campaign/commit flow, no default.",
    )
    parser.add_argument(
        "--roster-model",
        default=None,
        help=(
            "Optional case-insensitive model substring filter for roster selection "
            "(smoke/diagnostic aid; canonical benchmark runs unfiltered)."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Pin the run's subject to a named proxy bench alias present in "
            "WORKER_MODEL_REGISTRY (e.g. qwen3.6-35b-a3b-bench). The proxy makes "
            "that exact model resident on the first request (exclusive load on "
            "call); identity is still observed from API responses and recorded "
            "(RC-7). Omit to keep the neutral auto-resident slug. Changing the "
            "model changes the roster hash, which invalidates an existing "
            "manifest by design: archive runs/cumulative and rerun (RUNBOOK §0)."
        ),
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
        "--mode",
        choices=["on", "off"],
        default=None,
        help=(
            "Validate the current cell's memory_mode matches on/off; errors if "
            "it does not. Does NOT restructure the schedule."
        ),
    )
    run_parser.add_argument(
        "--proxy-base-url",
        default=None,
        help=(
            "Spend-proxy base URL baked into worker container opencode.json; default resolves via "
            "WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL env/.env else "
            "http://host.docker.internal:4545/v1 (container-facing; host loopback "
            "127.0.0.1 is dead inside cells)."
        ),
    )
    run_parser.add_argument("--proxy-token-file", default=None)

    extract_parser = subparsers.add_parser(
        "extract",
        help=(
            "Run the normal extraction pipeline for the current session "
            "(separate invocation after 'run')."
        ),
    )
    extract_parser.add_argument(
        "--proxy-base-url",
        default=None,
        help=(
            "Spend-proxy base URL baked into worker container opencode.json; default resolves via "
            "WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL env/.env else "
            "http://host.docker.internal:4545/v1 (container-facing; host loopback "
            "127.0.0.1 is dead inside cells)."
        ),
    )
    extract_parser.add_argument("--proxy-token-file", default=None)

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


def _discover_bench_ports() -> list[int]:
    """Ports the BENCH itself publishes and must clear at teardown.

    Only the live-view serve host port qualifies (RunConfig.serve_host_port,
    default 4096) — one persistent `opencode serve` per cell (WO-WATCH-1E),
    asserted clear so a leaked serve is caught. The hub (:4440) and MCP recall
    client (:4450) are STANDING infra owned outside the bench (card §7: the
    hub is the ONE hub, normally already running); asserting them clear was a
    guaranteed false alarm on every run (2026-08-09).
    """
    rc = config.RunConfig()
    if rc.serve_host_port > 0:
        return [rc.serve_host_port]
    return []


def main() -> int:
    assert_primary_path()
    parser = _build_arg_parser()
    args = parser.parse_args()

    # R-37 no-silent-ops: configure root logging so harness PROGRESS lines are
    # actually emitted on the run/resume paths. Without this, no basicConfig
    # exists, so every _LOG.info (including the adapter's worker-nonzero
    # stderr_tail diagnostics) was silently dropped. Logs go to stderr;
    # machine-readable results stay on stdout (_print_json).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "run": _handle_run,
        "extract": _handle_extract,
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

    # RC-6 / D-NO-REAPER: the reaper runs UNCONDITIONALLY on every exit path of
    # a MUTATING command — normal return, exception (failure), and
    # KeyboardInterrupt (operator interrupt). A silent reaper is not a reaper.
    # It is a safety net wrapper only: it must not alter the handler's return
    # value or exit code. Read-only commands (state, list-*, review-material,
    # emit/validate-decision, reconcile-inventory) spawn no workers and are
    # never reaped — a read-only `state` must not tear anything down
    # (2026-08-09: it ran a compose down against an unrelated project).
    mutating = {"run", "extract", "resume"}
    if str(args.command) not in mutating:
        return handler(args)
    try:
        run_label = getattr(args, "task", None) or "bench"
        bench_ports = _discover_bench_ports()
        reaper = ProcessReaper(run_label=run_label, bench_ports=bench_ports)
        return handler(args)
    finally:
        _LOG.info("process_reaper: unconditional reap entering finally")
        report = run_reaper_unconditional(reaper)
        _LOG.info(
            "process_reaper: run_label=%s killed_count=%d ports=%s "
            "cell_containers_removed=%s ok=%s",
            report.run_label, report.killed_count, report.ports,
            report.cell_containers_removed, report.ok,
        )
        # Runs/ retention prune (Walter 2026-08-10): rides the same
        # unconditional exit path as the reaper, so every early-cancelled
        # run's predecessor debris is bounded at latest+1 while the failed
        # run's own logs survive for post-mortem. Non-fatal by construction.
        _LOG.info("runs_retention: %s", _prune_runs_retention(_runs_root_from_args(args)))


if __name__ == "__main__":
    raise SystemExit(main())
