#!/usr/bin/env python3
"""Resumable Aider polyglot solve driver for OFF/ON* measured runs.

This mirrors the SWEContextBench solve-driver shape (conditions, arm-org routing,
seeding gate, per-cell telemetry, checkpoint + resume), but scores inline from
``TaskOutcome`` because aider returns resolved/tokens/turns/wall directly.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback
from typing import Any

from wevibe_bench.adapters.aider_polyglot import AiderPolyglotRunner, _safe_stem
from wevibe_bench.backends.base import DeliveryVerdict
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import RunConfig
from wevibe_bench.preflight import preflight
from wevibe_bench.runner import TaskOutcome
from wevibe_bench.scorecard import Cell, Scorecard

try:
    from wevibe_bench.runner import _cell_from_outcome as _runner_cell_from_outcome
except ImportError:
    _runner_cell_from_outcome = None


DEFAULT_MODEL = "openrouter/minimax/minimax-m3"
DEFAULT_RECALL_URL = "http://127.0.0.1:4550"
DEFAULT_ORG_ID = "wevibe-org-0"
DEFAULT_CONDITIONS = "off,on_reasoning,on_discovery"
DEFAULT_RUNS_ROOT = Path("~/Desktop/benchmark/runs/aider").expanduser()
DEFAULT_AUTH_PATH = Path("~/.local/share/opencode/auth.json").expanduser()

ALLOWED_CONDITIONS = {"off", "on", "on_reasoning", "on_discovery"}
TASK_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{3,}")


def _bench_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_polyglot_dir() -> Path:
    return _bench_root() / "polyglot-benchmark"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_path(path_like: str | Path) -> Path:
    return Path(path_like).expanduser().resolve()


def _to_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _memory_fingerprint8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


class LiveLogger:
    """Line-buffered tee logger (stdout + file) for tail-friendly progress."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)

    def log(self, message: str) -> None:
        stamp = _utc_now_iso()
        line = f"{stamp} {message}"
        print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.flush()
        finally:
            self._fh.close()


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def _load_checkpoint(path: Path) -> dict[str, dict[str, bool]]:
    if not path.exists():
        return {}

    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"checkpoint must decode to a JSON object: {path}")

    normalized: dict[str, dict[str, bool]] = {}
    for task_id, value in payload.items():
        if not isinstance(task_id, str) or not isinstance(value, dict):
            continue
        per_condition: dict[str, bool] = {}
        for condition, done in value.items():
            if not isinstance(condition, str):
                continue
            condition_lc = condition.strip().lower()
            if not condition_lc:
                continue
            per_condition[condition_lc] = bool(done)
        normalized[task_id] = per_condition
    return normalized


def _parse_csv_list(raw: str, *, field_name: str) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        items.append(token)
    if not items:
        raise ValueError(f"{field_name} must include at least one non-empty value")
    return items


def _parse_conditions(raw: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        cond = part.strip().lower()
        if not cond:
            continue
        if cond not in ALLOWED_CONDITIONS:
            raise ValueError(
                "invalid condition "
                f"{cond!r}; expected comma-list of off,on,on_reasoning,on_discovery"
            )
        if cond in seen:
            continue
        seen.add(cond)
        ordered.append(cond)
    if not ordered:
        raise ValueError("--conditions must include at least one of off,on,on_reasoning,on_discovery")
    return ordered


def _is_on_condition(condition: str) -> bool:
    return condition.startswith("on")


def _parse_arm_org_map(raw_entries: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw_entries:
        return out

    for raw_entry in raw_entries:
        token = raw_entry.strip()
        if not token:
            continue

        arm_raw, sep, org_raw = token.partition("=")
        if sep != "=":
            raise ValueError(f"invalid --arm-org entry {raw_entry!r}; expected arm=org")

        arm = arm_raw.strip().lower()
        org = org_raw.strip()
        if not arm or not org:
            raise ValueError(f"invalid --arm-org entry {raw_entry!r}; expected arm=org")
        if arm not in ALLOWED_CONDITIONS or not _is_on_condition(arm):
            raise ValueError(
                f"invalid --arm-org arm {arm!r}; expected one of on,on_reasoning,on_discovery"
            )

        out[arm] = org

    return out


def _resolve_task_selection(*, args: argparse.Namespace, available_task_ids: list[str]) -> list[str]:
    available_set = set(available_task_ids)

    if args.instances is not None:
        selected = _parse_csv_list(args.instances, field_name="--instances")
        missing = [task_id for task_id in selected if task_id not in available_set]
        if missing:
            raise ValueError("unknown task_id(s): " + ", ".join(missing))
        return selected

    if args.all:
        return list(available_task_ids)

    raise ValueError("must pass one of --instances or --all")


def _seed_openrouter_key_from_opencode_auth(auth_path: Path) -> None:
    if not auth_path.is_file():
        raise RuntimeError(f"OpenCode auth file not found: {auth_path}")

    payload = _load_json(auth_path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"OpenCode auth file must decode to object: {auth_path}")

    openrouter = payload.get("openrouter")
    if not isinstance(openrouter, dict):
        raise RuntimeError("OpenCode auth file missing object field 'openrouter'")

    key = openrouter.get("key")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("OpenCode auth file missing non-empty openrouter.key")

    os.environ["OPENROUTER_API_KEY"] = key.strip()


def _prepend_venv_bin_to_path(venv_bin: Path) -> None:
    if not venv_bin.is_dir():
        raise RuntimeError(f"venv bin directory not found: {venv_bin}")

    venv_bin_str = str(venv_bin)
    current = os.environ.get("PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    if venv_bin_str in entries:
        return

    if current:
        os.environ["PATH"] = f"{venv_bin_str}{os.pathsep}{current}"
    else:
        os.environ["PATH"] = venv_bin_str


def _telemetry_path(telemetry_dir: Path, condition: str, task_id: str) -> Path:
    return telemetry_dir / f"{condition}_{_safe_stem(task_id)}.json"


def _task_keyword_tokens(task_text: str) -> set[str]:
    return {token.lower() for token in TASK_TOKEN_RE.findall(task_text)}


def _memory_on_topic(*, task_tokens: set[str], memory_text: str) -> bool:
    if not task_tokens:
        return False
    memory_tokens = {token.lower() for token in TASK_TOKEN_RE.findall(memory_text)}
    return bool(task_tokens & memory_tokens)


def _build_precision_dilution(
    *,
    injected_memories: list[Any],
    task_tokens: set[str],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for memory in injected_memories:
        text = memory.text if isinstance(memory.text, str) else str(memory.text)
        diagnostics.append(
            {
                "cid": memory.cid,
                "text_fp": _memory_fingerprint8(text),
                "on_topic": _memory_on_topic(task_tokens=task_tokens, memory_text=text),
                "combined_score": memory.combined_score,
                "vector_score": memory.vector_score,
                "keyword_score": memory.keyword_score,
                "matched_keywords": memory.matched_keywords,
                "text_gist": re.sub(r"\s+", " ", text).strip()[:240],
            }
        )
    return diagnostics


def _cell_from_outcome(
    *,
    model: str,
    task_id: str,
    condition: str,
    delivery: str,
    scored: bool,
    outcome: TaskOutcome,
) -> Cell:
    not_scored_reason = None if scored else f"delivery={delivery}"

    if _runner_cell_from_outcome is not None:
        return _runner_cell_from_outcome(
            model=model,
            task_id=task_id,
            condition=condition,
            delivery=delivery,
            scored=scored,
            not_scored_reason=not_scored_reason,
            outcome=outcome,
        )

    return Cell(
        model=model,
        task_id=task_id,
        condition=condition,
        resolved=outcome.resolved,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        turns=outcome.turns,
        wall_cost_usd=outcome.wall_cost_usd,
        wall_seconds=outcome.wall_seconds,
        delivery=delivery,
        scored=scored,
        not_scored_reason=not_scored_reason,
    )


def _cell_from_telemetry(*, model: str, task_id: str, condition: str, telemetry: dict[str, Any]) -> Cell:
    condition_label = condition.upper()
    delivery_raw = telemetry.get("delivery")
    delivery = str(delivery_raw) if delivery_raw is not None else ("N/A" if condition == "off" else "UNKNOWN")

    if condition == "off":
        scored = True
        not_scored_reason = None
    else:
        scored_raw = telemetry.get("scored")
        scored = bool(scored_raw) if isinstance(scored_raw, bool) else (delivery == DeliveryVerdict.YES.value)
        not_scored_reason = None if scored else f"delivery={delivery}"

    return Cell(
        model=model,
        task_id=task_id,
        condition=condition_label,
        resolved=bool(telemetry.get("resolved", False)),
        input_tokens=_to_int(telemetry.get("input_tokens"), default=0),
        output_tokens=_to_int(telemetry.get("output_tokens"), default=0),
        turns=_to_int(telemetry.get("turns"), default=0),
        wall_cost_usd=_to_float(telemetry.get("wall_cost_usd"), default=0.0),
        wall_seconds=_to_float(telemetry.get("wall_seconds"), default=0.0),
        delivery=delivery,
        scored=scored,
        not_scored_reason=not_scored_reason,
    )


@dataclass
class _CellBundle:
    task_id: str
    condition: str
    cell: Cell
    telemetry_payload: dict[str, Any] | None
    telemetry_path: Path
    checkpoint_key: tuple[str, str]
    log_line: str
    skipped: bool


def _run_cell(
    *,
    args: argparse.Namespace,
    cfg: RunConfig,
    runner: AiderPolyglotRunner,
    task_id: str,
    condition: str,
    need: Any,
    task_tokens: set[str],
    telemetry_path: Path,
) -> _CellBundle:
    if args.resume and telemetry_path.exists():
        telemetry = _load_json(telemetry_path)
        if not isinstance(telemetry, dict):
            raise ValueError(f"telemetry file must decode to object: {telemetry_path}")
        cell = _cell_from_telemetry(
            model=args.model,
            task_id=task_id,
            condition=condition,
            telemetry=telemetry,
        )
        log_line = (
            f"[cell] {cell.condition} {task_id} resolved={cell.resolved} "
            f"delivery={cell.delivery} tokens={cell.total_tokens} wall={cell.wall_seconds:.3f} "
            "resume=skip"
        )
        return _CellBundle(
            task_id=task_id,
            condition=condition,
            cell=cell,
            telemetry_payload=None,
            telemetry_path=telemetry_path,
            checkpoint_key=(task_id, condition),
            log_line=log_line,
            skipped=True,
        )

    if condition == "off":
        outcome = runner.run_task(args.model, task_id, [])
        cell = _cell_from_outcome(
            model=args.model,
            task_id=task_id,
            condition="OFF",
            delivery="N/A",
            scored=True,
            outcome=outcome,
        )
        telemetry_payload: dict[str, Any] = {
            "condition": condition,
            "task_id": task_id,
            "arm_org": None,
            "session_id": None,
            "delivery": "N/A",
            "resolved": outcome.resolved,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "turns": outcome.turns,
            "wall_seconds": outcome.wall_seconds,
            "wall_cost_usd": outcome.wall_cost_usd,
            "scored": True,
        }
    else:
        session_id = f"aider-{cfg.run_label}-{condition}-{_safe_stem(task_id)}"
        arm_org = cfg.arm_org_map.get(condition) or cfg.org_id

        backend = WeVibeBackend(cfg)
        backend.prime_session(session_id)
        recall_result = backend.recall(need, cfg, org_id=arm_org)
        verdict = backend.verify_delivery(recall_result)
        injected_memories = recall_result.memories if verdict == DeliveryVerdict.YES else []

        outcome = runner.run_task(args.model, task_id, injected_memories)
        scored = verdict == DeliveryVerdict.YES
        cell = _cell_from_outcome(
            model=args.model,
            task_id=task_id,
            condition=condition.upper(),
            delivery=verdict.value,
            scored=scored,
            outcome=outcome,
        )

        telemetry_payload = {
            "condition": condition,
            "task_id": task_id,
            "arm_org": arm_org,
            "session_id": session_id,
            "delivery": verdict.value,
            "resolved": outcome.resolved,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "turns": outcome.turns,
            "wall_seconds": outcome.wall_seconds,
            "wall_cost_usd": outcome.wall_cost_usd,
            "memory_cids": [memory.cid for memory in recall_result.memories if memory.has_content()],
            "injected_count": len(injected_memories),
            "precision_dilution": _build_precision_dilution(
                injected_memories=injected_memories,
                task_tokens=task_tokens,
            ),
            "scored": scored,
            "not_scored_reason": None if scored else f"delivery={verdict.value}",
        }

    log_line = (
        f"[cell] {cell.condition} {task_id} resolved={cell.resolved} "
        f"delivery={cell.delivery} tokens={cell.total_tokens} wall={cell.wall_seconds:.3f}"
    )
    return _CellBundle(
        task_id=task_id,
        condition=condition,
        cell=cell,
        telemetry_payload=telemetry_payload,
        telemetry_path=telemetry_path,
        checkpoint_key=(task_id, condition),
        log_line=log_line,
        skipped=False,
    )


def _dry_run_plan(
    *,
    logger: LiveLogger,
    tasks: list[str],
    conditions: list[str],
    cfg: RunConfig,
    telemetry_dir: Path,
    args: argparse.Namespace,
) -> int:
    run_cells = 0
    skip_cells = 0

    logger.log("[dry-run] mode=on (NO recall, NO solve)")
    logger.log(
        "[dry-run] config "
        f"tasks={len(tasks)} conditions={','.join(conditions)} run_label={args.run_label} "
        f"model={args.model} polyglot_dir={args.polyglot_dir or _default_polyglot_dir()}"
    )

    for task_id in tasks:
        task_stem = _safe_stem(task_id)
        for condition in conditions:
            telemetry_path = _telemetry_path(telemetry_dir, condition, task_id)
            should_skip = args.resume and telemetry_path.exists()
            if should_skip:
                state = "skip(resume)"
                skip_cells += 1
            else:
                state = "run"
                run_cells += 1

            if _is_on_condition(condition):
                arm_org = cfg.arm_org_map.get(condition) or cfg.org_id
                session_id = f"aider-{cfg.run_label}-{condition}-{task_stem}"
            else:
                arm_org = "n/a"
                session_id = "n/a"

            logger.log(
                f"[dry-run] {condition.upper()} task_id={task_id} state={state} "
                f"arm_org={arm_org} session_id={session_id} telemetry={telemetry_path}"
            )

    logger.log(f"[dry-run summary] planned_run_cells={run_cells} planned_skip_cells={skip_cells}")
    return 0


def run(args: argparse.Namespace) -> int:
    conditions = _parse_conditions(args.conditions)

    if args.max_workers < 1:
        raise ValueError("--max-workers must be >= 1")

    if args.producer_prompt and any(_is_on_condition(condition) for condition in conditions):
        raise SystemExit(
            "producer-prompt is seeding-only; measured ON arms must be marker-free — "
            "run seeding with --conditions off"
        )

    arm_org_map = _parse_arm_org_map(args.arm_org)

    out_root = _safe_path(args.out_root) if args.out_root else _safe_path(DEFAULT_RUNS_ROOT / args.run_label)
    work_root = _safe_path(args.work_root) if args.work_root else out_root / "work"
    polyglot_dir = _safe_path(args.polyglot_dir) if args.polyglot_dir else _default_polyglot_dir()
    telemetry_dir = out_root / "telemetry"
    checkpoint_path = out_root / "solve_checkpoint.json"
    scorecard_path = out_root / "scorecard.json"
    log_path = out_root / "logs" / f"{_timestamp_for_filename()}-solve.log"

    logger = LiveLogger(log_path)
    logger.log(
        "[aider solve driver] start "
        f"run_label={args.run_label} model={args.model} conditions={','.join(conditions)} "
        f"resume={bool(args.resume)} dry_run={bool(args.dry_run)} out_root={out_root} "
        f"work_root={work_root} polyglot_dir={polyglot_dir} recall_url={args.recall_url} org_id={args.org_id}"
    )
    logger.log(f"[aider solve driver] logfile={log_path}")
    if arm_org_map:
        logger.log("[aider solve driver] arm_org_map " + ",".join(f"{arm}={org}" for arm, org in arm_org_map.items()))

    try:
        _seed_openrouter_key_from_opencode_auth(DEFAULT_AUTH_PATH)
        _prepend_venv_bin_to_path(_bench_root() / ".venv" / "bin")

        producer_system_template: str | None = None
        if args.producer_prompt:
            producer_prompt_path = _safe_path(args.producer_prompt)
            producer_system_template = producer_prompt_path.read_text(encoding="utf-8")

        capture_transcript_dir: Path | None = None
        if args.capture_transcript_dir:
            capture_transcript_dir = _safe_path(args.capture_transcript_dir)

        cfg = RunConfig(
            run_label=args.run_label,
            arm_org_map=arm_org_map,
            org_id=args.org_id,
            mcp_recall_url=args.recall_url,
            deterministic_topn=True,
            recall_mode="test",
            max_attempts=args.max_attempts,
        )

        if not args.dry_run:
            preflight(
                hub_url=cfg.hub_url,
                mcp_recall_url=cfg.mcp_recall_url,
                session_token_path=cfg.session_token_path,
            )

        runner = AiderPolyglotRunner(
            polyglot_dir=polyglot_dir,
            cfg=cfg,
            work_root=work_root,
            producer_system_template=producer_system_template,
            capture_transcript_dir=capture_transcript_dir,
            max_attempts=cfg.max_attempts,
        )

        available_tasks = runner.task_ids()
        tasks = _resolve_task_selection(args=args, available_task_ids=available_tasks)
        checkpoint = _load_checkpoint(checkpoint_path)

        if args.dry_run:
            return _dry_run_plan(
                logger=logger,
                tasks=tasks,
                conditions=conditions,
                cfg=cfg,
                telemetry_dir=telemetry_dir,
                args=args,
            )

        scorecard = Scorecard(cfg)
        needs = {task_id: runner.build_need_card(task_id) for task_id in tasks}
        task_tokens_by_task = {task_id: _task_keyword_tokens(needs[task_id].task) for task_id in tasks}
        total_cells = len(tasks) * len(conditions)
        processed_cells = 0
        errored_cells: list[dict] = []

        for condition in conditions:
            logger.log(f"[arm] condition={condition} tasks={len(tasks)} max_workers={args.max_workers}")

            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                future_meta: dict[Any, str] = {}
                for task_id in tasks:
                    future = executor.submit(
                        _run_cell,
                        args=args,
                        cfg=cfg,
                        runner=runner,
                        task_id=task_id,
                        condition=condition,
                        need=needs[task_id],
                        task_tokens=task_tokens_by_task[task_id],
                        telemetry_path=_telemetry_path(telemetry_dir, condition, task_id),
                    )
                    future_meta[future] = task_id

                for future in as_completed(future_meta):
                    task_id_f = future_meta[future]
                    try:
                        bundle = future.result()
                    except Exception as exc:  # noqa: BLE001 - per-cell failures must not abort full run.
                        errored_cells.append(
                            {
                                "task_id": task_id_f,
                                "condition": condition,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        processed_cells += 1
                        logger.log(
                            f"[cell-error] {condition.upper()} {task_id_f} error={type(exc).__name__}: {exc} "
                            f"— EXCLUDED (no fabricated tokens), continuing ({processed_cells}/{total_cells})"
                        )
                        continue

                    if not bundle.skipped:
                        if bundle.telemetry_payload is None:
                            raise ValueError(
                                f"non-skipped cell missing telemetry payload: {bundle.condition} {bundle.task_id}"
                            )
                        _write_json_atomic(bundle.telemetry_path, bundle.telemetry_payload)

                    task_key, condition_key = bundle.checkpoint_key
                    checkpoint.setdefault(task_key, {})[condition_key] = True
                    _write_json_atomic(checkpoint_path, checkpoint)

                    scorecard.add_cell(bundle.cell)
                    processed_cells += 1

                    logger.log(f"{bundle.log_line} ({processed_cells}/{total_cells})")

        scorecard_path.parent.mkdir(parents=True, exist_ok=True)
        scorecard_path.write_text(scorecard.to_json() + "\n", encoding="utf-8")
        logger.log(f"[summary] scorecard={scorecard_path} cells={len(scorecard.cells)}")
        logger.log(f"[summary-errors] errored_cells={len(errored_cells)}")
        if errored_cells:
            _write_json_atomic(out_root / "errored_cells.json", errored_cells)
        return 0
    finally:
        logger.log(f"[aider solve driver] end logfile={log_path}")
        logger.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable aider OFF/ON* solve driver")

    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--instances", type=str, help="Comma-separated task IDs (e.g. python/poker)")
    selector.add_argument("--all", action="store_true", help="Run all available task IDs")

    parser.add_argument("--run-label", type=str, required=True, help="Run label (used in output paths)")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--conditions", type=str, default=DEFAULT_CONDITIONS)
    parser.add_argument(
        "--arm-org",
        action="append",
        default=[],
        help="Per-arm org override in arm=org form (repeatable)",
    )
    parser.add_argument(
        "--producer-prompt",
        type=str,
        help=(
            "Path to producer system prompt template (SEEDING ONLY). "
            "Rejected for measured ON* arms."
        ),
    )
    parser.add_argument(
        "--capture-transcript-dir",
        type=str,
        help="Directory where aider transcripts are captured as <safe_task_id>.jsonl",
    )
    parser.add_argument("--recall-url", type=str, default=DEFAULT_RECALL_URL)
    parser.add_argument("--org-id", type=str, default=DEFAULT_ORG_ID)
    parser.add_argument(
        "--polyglot-dir",
        type=str,
        help=f"Polyglot benchmark repo root (default: {_default_polyglot_dir()})",
    )
    parser.add_argument(
        "--work-root",
        type=str,
        help="Working root (default: ~/Desktop/benchmark/runs/aider/<run-label>/work)",
    )
    parser.add_argument(
        "--out-root",
        type=str,
        help="Output root (default: ~/Desktop/benchmark/runs/aider/<run-label>)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Max concurrent task cells per condition (must be >=1)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        help="Max solve attempts per task (>=1). Raise above 2 so OFF can resolve.",
    )
    parser.add_argument("--resume", action="store_true", help="Skip cells whose telemetry JSON already exists")
    parser.add_argument("--dry-run", action="store_true", help="Plan-only mode: no recall/solve calls")

    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser = build_parser()
    args = parser.parse_args()

    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 - top-level hard failure should print full traceback.
        print(f"[aider solve driver] fatal error={type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
