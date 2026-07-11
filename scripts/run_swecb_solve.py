#!/usr/bin/env python3
"""Resumable SWEContextBench solve driver for OFF/ON ablation.

This script generates solve-stage prediction + telemetry artifacts for selected
held-out SWEContextBench instances under OFF and/or ON conditions.

Important invariants:
- OFF always solves with empty injected memory and delivery="N/A".
- ON uses real WeVibe recall + delivery verification.
- ON cells with delivery != YES still solve (with injected=[]), are recorded
  honestly, and are left for eval-stage delivery-gating (not scored downstream).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import traceback
from typing import Any

from wevibe_bench.adapters.swecontextbench import SWEContextBenchRunner, write_prediction
from wevibe_bench.backends.base import DeliveryVerdict, RecalledMemory
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import RunConfig


DEFAULT_MODEL = "openrouter/qwen/qwen3-coder"
DEFAULT_RECALL_URL = "http://127.0.0.1:4550"
DEFAULT_ORG_ID = "wevibe-org-0"
DEFAULT_DATASET_DIR = Path("~/Desktop/benchmark/datasets/swecontextbench").expanduser()
DEFAULT_HELDOUT_PATH = DEFAULT_DATASET_DIR / "heldout_lite.json"
DEFAULT_EDGE_MAP_PATH = DEFAULT_DATASET_DIR / "edge_map.json"
DEFAULT_RUNS_ROOT = Path("~/Desktop/benchmark/runs/swecb").expanduser()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_path(path_like: str | Path) -> Path:
    return Path(path_like).expanduser().resolve()


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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

    def log_multiline(self, message: str) -> None:
        lines = message.splitlines() or [message]
        for line in lines:
            self.log(line)

    def close(self) -> None:
        try:
            self._fh.flush()
        finally:
            self._fh.close()


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


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
    for iid, value in payload.items():
        if not isinstance(iid, str):
            continue
        if not isinstance(value, dict):
            continue
        normalized[iid] = {
            "off": bool(value.get("off", False)),
            "on": bool(value.get("on", False)),
        }
    return normalized


def _load_heldout_iids(path: Path) -> list[str]:
    payload = _load_json(path)
    if not isinstance(payload, list):
        raise ValueError(f"expected list in heldout dataset: {path}")

    iids: list[str] = []
    seen: set[str] = set()
    for idx, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"heldout row {idx} is not an object")
        iid = row.get("instance_id")
        if not isinstance(iid, str) or not iid.strip():
            raise ValueError(f"heldout row {idx} missing non-empty instance_id")
        iid = iid.strip()
        if iid in seen:
            raise ValueError(f"duplicate instance_id in heldout dataset: {iid}")
        seen.add(iid)
        iids.append(iid)
    return iids


def _load_edge_keys(path: Path) -> set[str]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"edge_map must decode to an object: {path}")
    keys: set[str] = set()
    for iid, _ in payload.items():
        if isinstance(iid, str) and iid.strip():
            keys.add(iid.strip())
    return keys


def _parse_csv_list(raw: str, *, field_name: str) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        items.append(token)
    if not items:
        raise ValueError(f"{field_name} must include at least one non-empty value")
    return items


def _parse_conditions(raw: str) -> list[str]:
    allowed = {"off", "on"}
    ordered: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        cond = part.strip().lower()
        if not cond:
            continue
        if cond not in allowed:
            raise ValueError(f"invalid condition {cond!r}; expected comma-list of off,on")
        if cond in seen:
            continue
        seen.add(cond)
        ordered.append(cond)
    if not ordered:
        raise ValueError("--conditions must include at least one of off,on")
    return ordered


def _resolve_instance_selection(
    *,
    args: argparse.Namespace,
    heldout_iids: list[str],
    edge_keys: set[str],
) -> list[str]:
    heldout_set = set(heldout_iids)

    if args.instances is not None:
        requested = _parse_csv_list(args.instances, field_name="--instances")
        missing = [iid for iid in requested if iid not in heldout_set]
        if missing:
            raise ValueError("unknown instance(s): " + ", ".join(missing))
        return requested

    if args.all:
        return list(heldout_iids)

    if args.only_with_edges:
        selected = [iid for iid in heldout_iids if iid in edge_keys]
        if not selected:
            raise ValueError("--only-with-edges matched zero held-out instances")
        return selected

    raise ValueError("must pass one of --instances, --all, --only-with-edges")


def _model_slug(value: str) -> str:
    out: list[str] = []
    for ch in value.strip():
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "model"


def _patch_len_bytes(patch: str) -> int:
    return len(patch.encode("utf-8"))


def _memory_fingerprint8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _condition_done(checkpoint: dict[str, dict[str, bool]], iid: str, condition: str) -> bool:
    return bool(checkpoint.get(iid, {}).get(condition, False))


def _telemetry_path(telemetry_dir: Path, condition: str, iid: str) -> Path:
    return telemetry_dir / f"{condition}_{iid}.json"


def _prediction_path(preds_dir: Path, iid: str) -> Path:
    return preds_dir / f"{iid}_preds.json"


def _build_eval_commands(*, run_label: str, out_root: Path, model: str) -> tuple[str, str, str]:
    py = shlex.quote(sys.executable)
    script = shlex.quote("scripts/run_swecb_eval.py")

    off_checkpoint = out_root / "eval_checkpoint_off.json"
    on_checkpoint = out_root / "eval_checkpoint_on.json"
    telemetry_dir = out_root / "telemetry"
    scorecard_out = out_root / "scorecard.json"

    off_cmd = " ".join(
        [
            py,
            script,
            "--run-label",
            shlex.quote(run_label),
            "--predictions-dir",
            shlex.quote(str(out_root / "preds_off")),
            "--condition",
            "off",
            "--checkpoint",
            shlex.quote(str(off_checkpoint)),
        ]
    )

    on_cmd = " ".join(
        [
            py,
            script,
            "--run-label",
            shlex.quote(run_label),
            "--predictions-dir",
            shlex.quote(str(out_root / "preds_on")),
            "--condition",
            "on",
            "--checkpoint",
            shlex.quote(str(on_checkpoint)),
        ]
    )

    assemble_cmd = " ".join(
        [
            py,
            script,
            "--assemble",
            "--run-label",
            shlex.quote(run_label),
            "--model",
            shlex.quote(model),
            "--off-checkpoint",
            shlex.quote(str(off_checkpoint)),
            "--on-checkpoint",
            shlex.quote(str(on_checkpoint)),
            "--telemetry-dir",
            shlex.quote(str(telemetry_dir)),
            "--scorecard-out",
            shlex.quote(str(scorecard_out)),
        ]
    )

    return off_cmd, on_cmd, assemble_cmd


def _build_resume_hint(args: argparse.Namespace, out_root: Path) -> str:
    py = shlex.quote(sys.executable)
    script = shlex.quote("scripts/run_swecb_solve.py")
    parts = [
        py,
        script,
        "--run-label",
        shlex.quote(args.run_label),
        "--model",
        shlex.quote(args.model),
        "--step-limit",
        str(args.step_limit),
        "--cost-limit",
        str(args.cost_limit),
        "--docker-solve" if args.docker_solve else "--no-docker-solve",
        "--conditions",
        shlex.quote(args.conditions),
        "--recall-url",
        shlex.quote(args.recall_url),
        "--org-id",
        shlex.quote(args.org_id),
        "--out-root",
        shlex.quote(str(out_root)),
        "--resume",
    ]
    if args.instances is not None:
        parts.extend(["--instances", shlex.quote(args.instances)])
    elif args.all:
        parts.append("--all")
    else:
        parts.append("--only-with-edges")
    return " ".join(parts)


def _dry_run_plan(
    *,
    logger: LiveLogger,
    instances: list[str],
    conditions: list[str],
    checkpoint: dict[str, dict[str, bool]],
    edge_keys: set[str],
    preds_off_dir: Path,
    preds_on_dir: Path,
    telemetry_dir: Path,
    args: argparse.Namespace,
    out_root: Path,
) -> int:
    run_cells = 0
    skip_cells = 0

    logger.log("[dry-run] mode=on (NO recall, NO solve)")
    logger.log(
        "[dry-run] config "
        f"instances={len(instances)} conditions={','.join(conditions)} "
        f"run_label={args.run_label} model={args.model} step_limit={args.step_limit} "
        f"cost_limit={args.cost_limit} docker_solve={bool(args.docker_solve)}"
    )
    logger.log(
        "[dry-run] recall "
        f"url={args.recall_url} org_id={args.org_id} token_path={RunConfig().session_token_path}"
    )
    logger.log(
        "[dry-run] outputs "
        f"out_root={out_root} checkpoint={out_root / 'solve_checkpoint.json'} "
        f"preds_off={preds_off_dir} preds_on={preds_on_dir} telemetry={telemetry_dir}"
    )

    total = len(instances)
    for index, iid in enumerate(instances, start=1):
        edge_seeded = "yes" if iid in edge_keys else "no"
        for condition in conditions:
            done = _condition_done(checkpoint, iid, condition)
            should_skip = args.resume and done
            if should_skip:
                skip_cells += 1
                state = "skip(resume)"
            else:
                run_cells += 1
                state = "run"

            pred_path = _prediction_path(preds_off_dir if condition == "off" else preds_on_dir, iid)
            telem_path = _telemetry_path(telemetry_dir, condition, iid)
            logger.log(
                f"[dry-run {condition}] {iid} state={state} edge_seeded={edge_seeded} "
                f"pred={pred_path} telemetry={telem_path} ({index}/{total})"
            )

    off_eval, on_eval, assemble = _build_eval_commands(run_label=args.run_label, out_root=out_root, model=args.model)
    logger.log(f"[dry-run summary] planned_run_cells={run_cells} planned_skip_cells={skip_cells}")
    logger.log("[dry-run next] score OFF command:")
    logger.log(off_eval)
    logger.log("[dry-run next] score ON command:")
    logger.log(on_eval)
    logger.log("[dry-run next] assemble command:")
    logger.log(assemble)
    return 0


def _summarize_completed(
    *,
    instances: list[str],
    conditions: list[str],
    checkpoint: dict[str, dict[str, bool]],
    telemetry_dir: Path,
) -> tuple[dict[str, int], int, int, float]:
    done_per_condition = {condition: 0 for condition in conditions}
    on_yes = 0
    on_not_yes = 0
    total_cost = 0.0

    for iid in instances:
        for condition in conditions:
            if not _condition_done(checkpoint, iid, condition):
                continue

            done_per_condition[condition] += 1
            telemetry_path = _telemetry_path(telemetry_dir, condition, iid)
            if telemetry_path.exists():
                payload = _load_json(telemetry_path)
                if isinstance(payload, dict):
                    total_cost += _to_float(payload.get("wall_cost_usd"), default=0.0)
                    if condition == "on":
                        if str(payload.get("delivery")) == "YES":
                            on_yes += 1
                        else:
                            on_not_yes += 1
                    continue

            if condition == "on":
                on_not_yes += 1

    return done_per_condition, on_yes, on_not_yes, total_cost


def run(args: argparse.Namespace) -> int:
    if not args.model.startswith("openrouter/"):
        raise ValueError(
            "hosted model required for SWEContextBench solve; "
            f"expected openrouter/*, got {args.model!r}"
        )

    conditions = _parse_conditions(args.conditions)
    heldout_iids = _load_heldout_iids(DEFAULT_HELDOUT_PATH)
    edge_keys = _load_edge_keys(DEFAULT_EDGE_MAP_PATH)
    instances = _resolve_instance_selection(args=args, heldout_iids=heldout_iids, edge_keys=edge_keys)

    out_root = _safe_path(args.out_root) if args.out_root else _safe_path(DEFAULT_RUNS_ROOT / args.run_label)
    preds_off_dir = out_root / "preds_off"
    preds_on_dir = out_root / "preds_on"
    telemetry_dir = out_root / "telemetry"
    checkpoint_path = out_root / "solve_checkpoint.json"
    log_path = out_root / "logs" / f"{_timestamp_for_filename()}-solve.log"

    checkpoint = _load_checkpoint(checkpoint_path)

    logger = LiveLogger(log_path)
    logger.log(
        "[solve driver] start "
        f"run_label={args.run_label} instances={len(instances)} conditions={','.join(conditions)} "
        f"model={args.model} step_limit={args.step_limit} cost_limit={args.cost_limit} "
        f"docker_solve={bool(args.docker_solve)} "
        f"recall_url={args.recall_url} org_id={args.org_id} out_root={out_root} "
        f"checkpoint={checkpoint_path} dry_run={bool(args.dry_run)} resume={bool(args.resume)}"
    )
    logger.log(f"[solve driver] logfile={log_path}")

    try:
        if args.dry_run:
            return _dry_run_plan(
                logger=logger,
                instances=instances,
                conditions=conditions,
                checkpoint=checkpoint,
                edge_keys=edge_keys,
                preds_off_dir=preds_off_dir,
                preds_on_dir=preds_on_dir,
                telemetry_dir=telemetry_dir,
                args=args,
                out_root=out_root,
            )

        cfg = RunConfig(hub_url=args.recall_url, org_id=args.org_id)
        model_slug = _model_slug(args.model)
        off_model_label = f"wevibe-off-{model_slug}"
        on_model_label = f"wevibe-on-{model_slug}"

        runner = SWEContextBenchRunner(
            dataset_dir=DEFAULT_DATASET_DIR,
            work_root=out_root / "work",
            model=args.model,
            step_limit=args.step_limit,
            cost_limit_usd=args.cost_limit,
            repo_cache_dir=out_root / "repo-cache",
            docker_solve=args.docker_solve,
        )

        on_backend = WeVibeBackend(cfg) if "on" in conditions else None

        failures: list[str] = []
        total = len(instances)
        for index, iid in enumerate(instances, start=1):
            try:
                edge_seeded = "yes" if iid in edge_keys else "no"
                if args.resume and all(_condition_done(checkpoint, iid, cond) for cond in conditions):
                    logger.log(f"[solve] {iid} skip resume=all_requested_conditions_done ({index}/{total})")
                    continue

                logger.log(f"[solve] {iid} start edge_seeded={edge_seeded} ({index}/{total})")
                need = runner.build_need_card(iid)

                if "off" in conditions:
                    if args.resume and _condition_done(checkpoint, iid, "off"):
                        logger.log(f"[solve off] {iid} skip resume=already_done")
                    else:
                        result_off = runner.solve_instance(iid, [])
                        off_pred_path = _prediction_path(preds_off_dir, iid)
                        off_telemetry_path = _telemetry_path(telemetry_dir, "off", iid)

                        write_prediction(iid, off_model_label, result_off.patch, off_pred_path)
                        off_telemetry = {
                            "iid": iid,
                            "condition": "off",
                            "input_tokens": result_off.input_tokens,
                            "output_tokens": result_off.output_tokens,
                            "turns": result_off.turns,
                            "wall_cost_usd": result_off.wall_cost_usd,
                            "wall_seconds": result_off.wall_seconds,
                            "delivery": "N/A",
                            "model": result_off.model,
                            "memory_cids": [],
                            "agent_status": result_off.agent_status,
                        }
                        _write_json_atomic(off_telemetry_path, off_telemetry)

                        checkpoint.setdefault(iid, {})["off"] = True
                        _write_json_atomic(checkpoint_path, checkpoint)

                        logger.log(
                            f"[solve off] {iid} patch_len={_patch_len_bytes(result_off.patch)} "
                            f"in={result_off.input_tokens} out={result_off.output_tokens} "
                            f"turns={result_off.turns} cost={result_off.wall_cost_usd:.6f} "
                            f"agent_status={result_off.agent_status}"
                        )

                if "on" in conditions:
                    if args.resume and _condition_done(checkpoint, iid, "on"):
                        logger.log(f"[solve on] {iid} skip resume=already_done")
                    else:
                        if on_backend is None:
                            raise RuntimeError("ON backend not initialized")

                        on_backend.prime_session(f"swecb-{iid}")
                        recall_result = on_backend.recall(need, cfg)
                        verdict = on_backend.verify_delivery(recall_result)

                        top_memory = recall_result.memories[0] if recall_result.memories else None
                        top_cid = str(top_memory.cid) if (top_memory and top_memory.cid is not None) else "none"
                        top_fp = _memory_fingerprint8(top_memory.text) if top_memory is not None else "none"
                        reason = recall_result.reason_code or "none"

                        logger.log(
                            f"[recall on] {iid} edge_seeded={edge_seeded} delivery={verdict.value} "
                            f"n_mem={len(recall_result.memories)} reason={reason} "
                            f"top_cid={top_cid} top_fp={top_fp}"
                        )

                        injected: list[RecalledMemory]
                        if verdict == DeliveryVerdict.YES:
                            injected = recall_result.memories
                        else:
                            injected = []

                        result_on = runner.solve_instance(iid, injected)
                        on_pred_path = _prediction_path(preds_on_dir, iid)
                        on_telemetry_path = _telemetry_path(telemetry_dir, "on", iid)
                        memory_cids = [memory.cid for memory in recall_result.memories if memory.has_content()]

                        write_prediction(iid, on_model_label, result_on.patch, on_pred_path)
                        on_telemetry = {
                            "iid": iid,
                            "condition": "on",
                            "input_tokens": result_on.input_tokens,
                            "output_tokens": result_on.output_tokens,
                            "turns": result_on.turns,
                            "wall_cost_usd": result_on.wall_cost_usd,
                            "wall_seconds": result_on.wall_seconds,
                            "delivery": verdict.value,
                            "model": result_on.model,
                            "memory_cids": memory_cids,
                            "agent_status": result_on.agent_status,
                        }
                        _write_json_atomic(on_telemetry_path, on_telemetry)

                        checkpoint.setdefault(iid, {})["on"] = True
                        _write_json_atomic(checkpoint_path, checkpoint)

                        logger.log(
                            f"[solve on] {iid} delivery={verdict.value} patch_len={_patch_len_bytes(result_on.patch)} "
                            f"in={result_on.input_tokens} out={result_on.output_tokens} "
                            f"turns={result_on.turns} cost={result_on.wall_cost_usd:.6f} "
                            f"injected_mem={len(injected)} agent_status={result_on.agent_status}"
                        )

                logger.log(f"[solve] {iid} done ({index}/{total})")
            except Exception as exc:  # noqa: BLE001 - must continue after per-instance failures.
                failures.append(iid)
                _write_json_atomic(checkpoint_path, checkpoint)
                logger.log(f"[solve] {iid} FULL ERROR BEGIN")
                logger.log_multiline(traceback.format_exc().rstrip())
                logger.log(f"[solve] {iid} FULL ERROR END")
                logger.log(f"[solve] {iid} failed error={type(exc).__name__}: {exc} ({index}/{total})")
                continue

        checkpoint = _load_checkpoint(checkpoint_path)
        done_per_condition, on_yes, on_not_yes, total_cost = _summarize_completed(
            instances=instances,
            conditions=conditions,
            checkpoint=checkpoint,
            telemetry_dir=telemetry_dir,
        )

        off_eval, on_eval, assemble = _build_eval_commands(run_label=args.run_label, out_root=out_root, model=args.model)

        logger.log("[summary] complete")
        for condition in conditions:
            logger.log(
                f"[summary] {condition} cells_done={done_per_condition.get(condition, 0)}/{len(instances)}"
            )
        if "on" in conditions:
            logger.log(f"[summary] on_delivery YES={on_yes} not_yes={on_not_yes}")
        logger.log(f"[summary] total_cost_usd={total_cost:.6f}")

        logger.log("[next] score OFF command:")
        logger.log(off_eval)
        logger.log("[next] score ON command:")
        logger.log(on_eval)
        logger.log("[next] assemble command:")
        logger.log(assemble)

        if failures:
            logger.log(f"[summary] failures={len(failures)} iids={','.join(failures)}")
            logger.log("[summary] resume hint:")
            logger.log(_build_resume_hint(args, out_root))
            return 2

        return 0
    finally:
        logger.log(f"[solve driver] end logfile={log_path}")
        logger.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable SWEContextBench OFF/ON solve driver")

    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--instances", type=str, help="Comma-separated list of instance IDs")
    selector.add_argument("--all", action="store_true", help="Run all held-out instances")
    selector.add_argument(
        "--only-with-edges",
        action="store_true",
        help="Run held-out instances that have an entry in edge_map.json",
    )

    parser.add_argument("--run-label", type=str, required=True, help="Run label (used in output paths)")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--step-limit", type=int, default=40)
    parser.add_argument("--cost-limit", type=float, default=1.0)
    parser.add_argument(
        "--docker-solve",
        dest="docker_solve",
        action="store_true",
        default=True,
        help="Run solve agent inside per-instance Docker testbed image (default)",
    )
    parser.add_argument(
        "--no-docker-solve",
        dest="docker_solve",
        action="store_false",
        help="Run solve agent against host checkout instead of Docker testbed image",
    )
    parser.add_argument("--conditions", type=str, default="off,on")
    parser.add_argument("--recall-url", type=str, default=DEFAULT_RECALL_URL)
    parser.add_argument("--org-id", type=str, default=DEFAULT_ORG_ID)
    parser.add_argument(
        "--out-root",
        type=str,
        help="Output root (default: ~/Desktop/benchmark/runs/swecb/<run-label>/)",
    )
    parser.add_argument("--resume", action="store_true", help="Skip cells already marked done in checkpoint")
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
        print(f"[solve driver] fatal error={type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
