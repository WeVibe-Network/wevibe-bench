"""CLI driver for backgammon bench cells with checkpoint/resume and scorecard output."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wevibe_bench.adapters.backgammon import BackgammonCellResult, BackgammonRunner
from wevibe_bench.benv import load_bench_env
from wevibe_bench.config import RunConfig
from wevibe_bench.lifecycle.logging_util import run_logger
from wevibe_bench.preflight import preflight
from wevibe_bench.scorecard import Cell, Scorecard


DEFAULT_MODEL = "openrouter/google/gemini-3.1-pro-preview"
_ALLOWED_MEMORY_MODES = {"off", "on"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _repo_dir() -> Path:
    workspace_root = Path(__file__).resolve().parents[2]
    return (workspace_root / "wevibe-bench").resolve()


def _default_runs_dir() -> Path:
    return _repo_dir() / "runs" / "backgammon"


def _parse_memory_modes(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        mode = part.strip().lower()
        if not mode:
            continue
        if mode not in _ALLOWED_MEMORY_MODES:
            raise ValueError(
                f"invalid memory mode {mode!r}; expected comma list from {sorted(_ALLOWED_MEMORY_MODES)}"
            )
        if mode in seen:
            continue
        seen.add(mode)
        out.append(mode)
    if not out:
        raise ValueError("--memory-modes resolved to an empty list")
    return out


def _save_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}"
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise RuntimeError(f"checkpoint exists but is empty: {path}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError(f"checkpoint must decode to an object: {path}")
    return payload


def _as_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _new_checkpoint(*, run_label: str, model: str) -> dict[str, Any]:
    return {
        "run_label": run_label,
        "model": model,
        "created_at": _utc_iso(),
        "cells": [],
    }


def _checkpoint_cells_by_mode(checkpoint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cells_raw = checkpoint.get("cells")
    if not isinstance(cells_raw, list):
        raise RuntimeError("checkpoint.cells must be an array")

    by_mode: dict[str, dict[str, Any]] = {}
    for raw in cells_raw:
        if not isinstance(raw, dict):
            continue
        mode = str(raw.get("memory_mode", "")).strip().lower()
        if not mode:
            continue
        by_mode[mode] = raw
    return by_mode


def _upsert_checkpoint_cell(checkpoint: dict[str, Any], cell: dict[str, Any]) -> None:
    cells_raw = checkpoint.get("cells")
    if not isinstance(cells_raw, list):
        cells_raw = []
        checkpoint["cells"] = cells_raw

    mode = str(cell.get("memory_mode", "")).strip().lower()
    if not mode:
        raise RuntimeError("checkpoint cell is missing memory_mode")

    for idx, existing in enumerate(cells_raw):
        if isinstance(existing, dict) and str(existing.get("memory_mode", "")).strip().lower() == mode:
            cells_raw[idx] = cell
            return
    cells_raw.append(cell)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run backgammon bench cells.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--memory-modes", default="off")
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--mock", choices=("none", "golden", "scaffold"), default="none")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--token-cap", type=int, default=200000)
    parser.add_argument("--cost-limit", type=float, default=None)
    parser.add_argument("--cost-target", type=float, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--max-steps-per-attempt", type=int, default=None)
    parser.add_argument("--output-price-per-1m", type=float, default=None)
    parser.add_argument("--run-timeout", type=int, default=1800)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--proxy-base-url", default=None)
    parser.add_argument("--proxy-token-file", default=None)
    parser.add_argument("--agent", default="build")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--runs-dir", default=str(_default_runs_dir()))
    return parser


def main() -> int:
    load_bench_env()
    args = _build_arg_parser().parse_args()

    memory_modes = _parse_memory_modes(args.memory_modes)
    mock_mode = None if args.mock == "none" else args.mock
    run_ts = _utc_compact()
    proxy_token: str | None = None
    if args.proxy_token_file:
        proxy_token = Path(args.proxy_token_file).expanduser().read_text(encoding="utf-8").strip()

    if args.cost_limit is not None and args.cost_limit <= 0:
        raise ValueError("--cost-limit must be > 0")
    if args.cost_target is not None and args.cost_target <= 0:
        raise ValueError("--cost-target must be > 0")
    if args.max_output_tokens is not None and args.max_output_tokens <= 0:
        raise ValueError("--max-output-tokens must be > 0")
    if args.max_steps_per_attempt is not None and args.max_steps_per_attempt <= 0:
        raise ValueError("--max-steps-per-attempt must be > 0")
    if args.output_price_per_1m is not None and args.output_price_per_1m <= 0:
        raise ValueError("--output-price-per-1m must be > 0")
    if args.cost_limit is not None and args.cost_target is not None and args.cost_target >= args.cost_limit:
        raise ValueError("--cost-target must be < --cost-limit")

    runs_dir = Path(args.runs_dir).expanduser().resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)

    logger = run_logger(args.run_label, str(runs_dir))
    raw_logfile_raw = str(getattr(logger, "logfile_path", "")).strip()
    logfile_path = runs_dir / f"{run_ts}-{args.run_label}.log"
    if raw_logfile_raw:
        raw_logfile_path = Path(raw_logfile_raw).expanduser()
        if raw_logfile_path != logfile_path and raw_logfile_path.is_file():
            os.replace(raw_logfile_path, logfile_path)
            logger.logfile_path = str(logfile_path)  # type: ignore[attr-defined]
        elif raw_logfile_path.is_file():
            logfile_path = raw_logfile_path.resolve()

    def progress(message: str) -> None:
        line = f"[{_utc_iso()}] PROGRESS {message}"
        print(line, flush=True)
        logger.info(line)

    start_line = (
        f"start run_label={args.run_label} model={args.model} "
        f"memory_modes={','.join(memory_modes)} mock={args.mock}"
    )
    if args.cost_limit is not None:
        start_line += f" cost_limit_usd={args.cost_limit:.4f}"
    if args.cost_target is not None:
        start_line += f" cost_target_usd={args.cost_target:.4f}"
    if args.max_output_tokens is not None:
        start_line += f" max_output_tokens={args.max_output_tokens}"
    if args.max_steps_per_attempt is not None:
        start_line += f" max_steps_per_attempt={args.max_steps_per_attempt}"
    if args.output_price_per_1m is not None:
        start_line += f" output_price_per_1m={args.output_price_per_1m:.4f}"
    if args.reasoning_effort is not None:
        start_line += f" reasoning_effort={args.reasoning_effort}"
    if args.proxy_base_url is not None:
        start_line += f" proxy_base_url={args.proxy_base_url}"
    progress(start_line)

    cfg = RunConfig(
        model_ladder=(args.model,),
        run_label=args.run_label,
        tau=0.68,
        surface_budget=3,
        max_attempts=args.max_attempts,
        mcp_recall_url="http://127.0.0.1:4550",
        cost_limit_usd=args.cost_limit,
        cost_target_usd=args.cost_target,
        max_output_tokens=args.max_output_tokens,
        max_steps_per_attempt=args.max_steps_per_attempt,
        output_price_per_1m=args.output_price_per_1m,
        reasoning_effort=args.reasoning_effort,
    )

    try:
        preflight_result = preflight(
            hub_url=cfg.hub_url,
            mcp_recall_url="http://127.0.0.1:4550",
            session_token_path=cfg.session_token_path,
        )
        if preflight_result not in (None, True):
            raise RuntimeError(f"unexpected preflight result: {preflight_result!r}")
    except Exception as exc:  # noqa: BLE001 - fail closed with explicit output contract.
        progress(f"PREFLIGHT FAILURE {exc}")
        print('BACKGAMMON_RESULT_JSON {"status":"preflight_failed"}', flush=True)
        return 2

    progress("preflight ok hub and clone recall client reachable")

    checkpoint_path = runs_dir / f"{args.run_label}-checkpoint.json"
    if args.resume:
        checkpoint = _load_json(checkpoint_path) or _new_checkpoint(run_label=args.run_label, model=args.model)
    else:
        checkpoint = _new_checkpoint(run_label=args.run_label, model=args.model)

    if str(checkpoint.get("run_label", "")) != args.run_label:
        raise RuntimeError(
            f"checkpoint run_label mismatch at {checkpoint_path}: "
            f"{checkpoint.get('run_label')!r} != {args.run_label!r}"
        )
    if str(checkpoint.get("model", "")) != args.model:
        raise RuntimeError(
            f"checkpoint model mismatch at {checkpoint_path}: "
            f"{checkpoint.get('model')!r} != {args.model!r}"
        )
    if not isinstance(checkpoint.get("cells"), list):
        checkpoint["cells"] = []

    task_dir = _repo_dir() / "tasks" / "backgammon"
    executed: dict[str, BackgammonCellResult] = {}

    for mode in memory_modes:
        prior_cells = _checkpoint_cells_by_mode(checkpoint)
        if args.resume and mode in prior_cells:
            progress(f"resume-skip memory_mode={mode} reason=checkpoint")
            continue

        run_dir = runs_dir / args.run_label / mode
        runner = BackgammonRunner(
            task_dir=task_dir,
            work_root=run_dir,
            model=args.model,
            memory_mode=mode,
            mock=mock_mode,
            max_attempts=cfg.max_attempts,
            token_cap=args.token_cap,
            run_timeout_s=args.run_timeout,
            cost_limit_usd=cfg.cost_limit_usd,
            cost_target_usd=cfg.cost_target_usd,
            max_output_tokens=cfg.max_output_tokens,
            max_steps_per_attempt=cfg.max_steps_per_attempt,
            output_price_per_1m=cfg.output_price_per_1m,
            reasoning_effort=cfg.reasoning_effort,
            proxy_base_url=args.proxy_base_url,
            proxy_token=proxy_token,
            agent=args.agent,
            logger=logger,
            progress=progress,
        )

        result = runner.run_cell(f"{args.run_label}-{mode}", run_dir)
        executed[mode] = result

        progress(
            f"cell-complete memory_mode={mode} verdict={result.verdict} "
            f"attempts_to_green={result.attempts_to_green} "
            f"input_tokens={result.input_tokens} output_tokens={result.output_tokens}"
        )

        checkpoint_cell = {
            "memory_mode": mode,
            "verdict": str(result.verdict),
            "attempts_to_green": result.attempts_to_green,
            "input_tokens": int(result.input_tokens),
            "output_tokens": int(result.output_tokens),
            "turns": int(result.turns),
            "wall_cost_usd": float(result.wall_cost_usd),
            "wall_seconds": float(result.wall_seconds),
            "conformed": bool(result.conformed),
            "failed_gates": _as_str_list(result.failed_gates),
            "scorecard_path": "",
        }
        _upsert_checkpoint_cell(checkpoint, checkpoint_cell)
        _save_json_atomic(checkpoint_path, checkpoint)

    cells_by_mode = _checkpoint_cells_by_mode(checkpoint)
    missing_modes = [mode for mode in memory_modes if mode not in cells_by_mode]
    if missing_modes:
        raise RuntimeError(f"missing checkpoint cells for requested modes: {missing_modes}")

    scorecard = Scorecard(cfg)
    result_cells: list[dict[str, Any]] = []
    detail_cells: list[dict[str, Any]] = []

    for mode in memory_modes:
        cell_data = cells_by_mode[mode]
        verdict = str(cell_data.get("verdict", "FAIL"))
        input_tokens = _as_int(cell_data.get("input_tokens"))
        output_tokens = _as_int(cell_data.get("output_tokens"))
        turns = _as_int(cell_data.get("turns"))
        wall_seconds = _as_float(cell_data.get("wall_seconds"))
        failed_gates = _as_str_list(cell_data.get("failed_gates"))
        wall_cost_usd = _as_float(cell_data.get("wall_cost_usd"))

        run_result = executed.get(mode)
        delivery = run_result.delivery if run_result is not None else "N/A"
        if run_result is not None:
            wall_cost_usd = run_result.wall_cost_usd

        scorecard.add_cell(
            Cell(
                model=args.model,
                task_id="backgammon",
                condition=mode.upper(),
                resolved=(verdict == "PASS"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                turns=turns,
                wall_cost_usd=wall_cost_usd,
                wall_seconds=wall_seconds,
                delivery=delivery,
                scored=True,
                not_scored_reason=None,
            )
        )

        result_cells.append(
            {
                "memory_mode": mode,
                "verdict": verdict,
                "attempts_to_green": cell_data.get("attempts_to_green"),
                "total_tokens": input_tokens + output_tokens,
                "turns": turns,
                "wall_seconds": wall_seconds,
                "delivery": delivery,
                "n_failed_gates": len(failed_gates),
            }
        )

        detail_cells.append(
            {
                "memory_mode": mode,
                "attempts_to_green": cell_data.get("attempts_to_green"),
                "conformed": bool(cell_data.get("conformed", False)),
                "failed_gates": failed_gates,
                "n_problems": len(run_result.problems_final) if run_result is not None else 0,
                "attempt_reports": run_result.attempt_reports if run_result is not None else [],
                "worktree": run_result.worktree if run_result is not None else "",
                "session_id": run_result.session_id if run_result is not None else None,
            }
        )

    scorecard_path = runs_dir / f"{run_ts}-{args.run_label}-scorecard.json"
    scorecard_path.write_text(scorecard.to_json(), encoding="utf-8")

    detail_path = runs_dir / f"{run_ts}-{args.run_label}-backgammon-detail.json"
    detail_path.write_text(json.dumps({"cells": detail_cells}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checkpoint_cells = checkpoint.get("cells")
    if isinstance(checkpoint_cells, list):
        for raw in checkpoint_cells:
            if isinstance(raw, dict):
                raw["scorecard_path"] = str(scorecard_path)
    _save_json_atomic(checkpoint_path, checkpoint)

    result_payload = {
        "status": "ok",
        "run_label": args.run_label,
        "cells": result_cells,
        "scorecard_path": str(scorecard_path),
        "detail_path": str(detail_path),
        "logfile": str(logfile_path),
    }
    print("BACKGAMMON_RESULT_JSON " + json.dumps(result_payload, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
