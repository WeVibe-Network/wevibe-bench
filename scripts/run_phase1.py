"""Phase-1 OFF/ON benchmark measurement driver for one model on held-out Python tasks.

This script runs the benchmark harness end-to-end for the Phase-1 memory ablation
measurement (OFF vs ON) and emits scorecard/summary artifacts plus a pre-ablation
recall diagnostic for each held-out task.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
from pathlib import Path
import sys

from wevibe_bench.adapters.aider_polyglot import AiderPolyglotRunner
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import RunConfig
from wevibe_bench.preflight import preflight
from wevibe_bench.runner import run_ablation
from wevibe_bench.scorecard import Cell, ModelDiff, Scorecard


DEFAULT_MODEL = "lm_studio/qwen/qwen3.6-35b-a3b"
DEFAULT_TASKS = "python/pig-latin,python/two-bucket,python/bottle-song"
DEFAULT_POLYGLOT_DIR = "/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench/polyglot-benchmark"
DEFAULT_WORK_ROOT = "~/Desktop/benchmark/runs/aider-work"
RUNS_DIR = "~/Desktop/benchmark/runs"
DIAGNOSTIC_FLOOR = 0.05
DIAGNOSTIC_BUDGET = 25

SPLIT_DISCLOSURE = {
    "train": ["python/bowling", "python/grade-school", "python/wordy"],
    "held_out": ["python/pig-latin", "python/two-bucket", "python/bottle-song"],
    "note": "disjoint; pool seeded on TRAIN only",
}


def _parse_tasks(raw_tasks: str) -> list[str]:
    tasks = [part.strip() for part in raw_tasks.split(",") if part.strip()]
    if not tasks:
        raise ValueError("PHASE1_TASKS resolved to an empty task list")
    return tasks


def _configure_model_env(model_slug: str) -> None:
    os.environ.setdefault("LM_STUDIO_API_BASE", "http://localhost:1234/v1")
    os.environ.setdefault("LM_STUDIO_API_KEY", "lm-studio")

    if model_slug.startswith("openrouter/"):
        auth_path = Path("~/.local/share/opencode/auth.json").expanduser()
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        openrouter = payload.get("openrouter")
        if not isinstance(openrouter, dict):
            raise RuntimeError("auth.json missing object at key path openrouter")
        key = openrouter.get("key")
        if not isinstance(key, str) or not key:
            raise RuntimeError("auth.json missing non-empty key at key path openrouter.key")
        os.environ["OPENROUTER_API_KEY"] = key


def _setup_logging(log_path: Path) -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    logging.getLogger("wevibe_bench.runner").setLevel(logging.INFO)
    logging.getLogger("wevibe_bench.adapters.aider_polyglot").setLevel(logging.INFO)
    logging.getLogger("wevibe_bench.backend").setLevel(logging.INFO)

    return logging.getLogger("phase1_driver")


def _preview(text: str, width: int = 80) -> str:
    flat = " ".join(text.split())
    return flat[:width]


def _collect_recall_diagnostic(
    *,
    cfg: RunConfig,
    tasks: list[str],
    agent: AiderPolyglotRunner,
    logger: logging.Logger,
    ts: str,
) -> dict[str, dict[str, object]]:
    production_tau = cfg.tau
    diag_cfg = dataclasses.replace(
        cfg,
        tau=DIAGNOSTIC_FLOOR,
        surface_budget=DIAGNOSTIC_BUDGET,
    )
    backend = WeVibeBackend(diag_cfg)
    diagnostic: dict[str, dict[str, object]] = {}

    logger.info("phase_boundary start recall_diagnostic")
    for task_id in tasks:
        need = agent.build_need_card(task_id)
        backend.prime_session(f"phase1-diagnostic-{task_id.replace('/', '-')}-{ts}")
        result = backend.recall(need, diag_cfg)
        verdict = backend.verify_delivery(result).value

        memory_entries: list[dict[str, object]] = []
        n_above_production_tau = 0
        for idx, memory in enumerate(result.memories):
            combined = memory.combined_score
            above_tau = bool(combined is not None and combined >= production_tau)
            if above_tau:
                n_above_production_tau += 1
            entry = {
                "cid": (memory.cid or "")[:12],
                "vector": memory.vector_score,
                "combined": combined,
                "keyword": memory.keyword_score,
                "above_tau": above_tau,
                "text_len": len(memory.text),
                "preview": _preview(memory.text, width=80),
            }
            memory_entries.append(entry)

            logger.info(
                "diagnostic_memory task_id=%s idx=%d cid=%s vector_score=%s combined_score=%s keyword_score=%s above_tau=%s text_len=%d preview=%r",
                task_id,
                idx,
                entry["cid"],
                entry["vector"],
                entry["combined"],
                entry["keyword"],
                entry["above_tau"],
                entry["text_len"],
                entry["preview"],
            )

        logger.info(
            "diagnostic_result task_id=%s verdict=%s n_memories=%d n_above_production_tau=%d",
            task_id,
            verdict,
            len(result.memories),
            n_above_production_tau,
        )

        diagnostic[task_id] = {
            "n_memories": len(result.memories),
            "n_above_production_tau": n_above_production_tau,
            "verdict": verdict,
            "memories": memory_entries,
            "diagnostic_floor": DIAGNOSTIC_FLOOR,
            "diagnostic_budget": DIAGNOSTIC_BUDGET,
            "production_tau": production_tau,
        }

    logger.info("phase_boundary end recall_diagnostic")
    return diagnostic


def _find_cell(*, scorecard: Scorecard, model: str, task_id: str, condition: str) -> Cell:
    matches = [
        cell
        for cell in scorecard.cells
        if cell.model == model and cell.task_id == task_id and cell.condition == condition
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one {condition} cell for model={model} task_id={task_id}; found {len(matches)}"
        )
    return matches[0]


def _find_model_diff(scorecard: Scorecard, model: str) -> ModelDiff:
    matches = [diff for diff in scorecard.model_diffs() if diff.model == model]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one model diff for model={model}; found {len(matches)}")
    return matches[0]


def _build_phase1_result_json(*, scorecard: Scorecard, model: str, tasks: list[str]) -> dict[str, object]:
    per_task: dict[str, dict[str, object]] = {}
    for task_id in tasks:
        off_cell = _find_cell(scorecard=scorecard, model=model, task_id=task_id, condition="OFF")
        on_cell = _find_cell(scorecard=scorecard, model=model, task_id=task_id, condition="ON")
        per_task[task_id] = {
            "off_resolved": off_cell.resolved,
            "on_resolved": on_cell.resolved,
            "on_delivery": on_cell.delivery,
            "on_scored": on_cell.scored,
            "off_total_tokens": off_cell.total_tokens,
            "on_total_tokens": on_cell.total_tokens,
            "off_turns": off_cell.turns,
            "on_turns": on_cell.turns,
        }

    diff = _find_model_diff(scorecard, model)
    return {
        "model": model,
        "tasks": tasks,
        "per_task": per_task,
        "model_diff": {
            "off_pass_rate": diff.off_pass_rate,
            "on_pass_rate": diff.on_pass_rate,
            "capability_lift_pp": diff.capability_lift_pp,
            "total_token_delta_pct": diff.total_token_delta_pct,
            "on_not_scored_n": diff.on_not_scored_n,
        },
    }


def main() -> int:
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")

    runs_dir = Path(RUNS_DIR).expanduser()
    runs_dir.mkdir(parents=True, exist_ok=True)

    work_root = Path(DEFAULT_WORK_ROOT).expanduser()
    work_root.mkdir(parents=True, exist_ok=True)

    log_path = runs_dir / f"{ts}-phase1-measure.log"
    scorecard_path = runs_dir / f"{ts}-phase1-scorecard.json"
    summary_path = runs_dir / f"{ts}-phase1-summary.json"

    logger = _setup_logging(log_path)

    model_slug = os.environ.get("PHASE1_MODEL", DEFAULT_MODEL)
    tasks = _parse_tasks(os.environ.get("PHASE1_TASKS", DEFAULT_TASKS))
    polyglot_dir = Path(os.environ.get("AIDER_POLYGLOT_DIR", DEFAULT_POLYGLOT_DIR)).expanduser()

    _configure_model_env(model_slug)

    cfg = RunConfig(
        model_ladder=(model_slug,),
        tau=0.68,
        surface_budget=3,
        mcp_recall_url="http://localhost:4450",
    )

    preflight(
        hub_url=cfg.hub_url,
        mcp_recall_url=cfg.mcp_recall_url,
        session_token_path=cfg.session_token_path,
    )

    logger.info("phase_boundary start setup")
    logger.info("phase1 log_path=%s", str(log_path))
    logger.info("phase1 scorecard_path=%s", str(scorecard_path))
    logger.info("phase1 summary_path=%s", str(summary_path))
    logger.info("phase1 work_root=%s", str(work_root))
    logger.info("phase1 polyglot_dir=%s", str(polyglot_dir))
    logger.info("phase1 model=%s", model_slug)
    logger.info("phase1 tasks=%s", json.dumps(tasks))
    logger.info("phase1 config_manifest=%s", json.dumps(cfg.to_dict(), sort_keys=True))
    logger.info("phase_boundary end setup")

    agent = AiderPolyglotRunner(polyglot_dir=polyglot_dir, cfg=cfg, work_root=work_root)
    available = agent.load_exercises()
    missing = [task_id for task_id in tasks if task_id not in available]
    if missing:
        raise ValueError(f"tasks not found in polyglot benchmark: {missing}")

    diagnostic = _collect_recall_diagnostic(cfg=cfg, tasks=tasks, agent=agent, logger=logger, ts=ts)

    logger.info("phase_boundary start ablation")
    scorecard = run_ablation(
        cfg,
        tasks,
        agent,
        split_disclosure=SPLIT_DISCLOSURE,
    )
    logger.info("phase_boundary end ablation")

    logger.info("phase_boundary start artifacts")
    scorecard_json = scorecard.to_json()
    scorecard_path.write_text(scorecard_json, encoding="utf-8")

    summary_payload = {
        "diagnostic": diagnostic,
        "model_diffs": [diff.to_dict() for diff in scorecard.model_diffs()],
        "cells": [cell.to_dict() for cell in scorecard.cells],
        "log_path": str(log_path),
        "scorecard_path": str(scorecard_path),
    }
    summary_path.write_text(json.dumps(summary_payload, indent=2, sort_keys=True), encoding="utf-8")

    result_payload = _build_phase1_result_json(scorecard=scorecard, model=model_slug, tasks=tasks)
    result_payload["log_path"] = str(log_path)
    result_payload["scorecard_path"] = str(scorecard_path)
    result_payload["summary_path"] = str(summary_path)

    logger.info("phase_boundary end artifacts")
    logger.info("phase1 complete")

    print("PHASE1_RESULT_JSON " + json.dumps(result_payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
