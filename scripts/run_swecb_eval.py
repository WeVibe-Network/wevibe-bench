#!/usr/bin/env python3
"""Per-instance SWEContextBench evaluator driver with resume + scorecard assembly.

This script has two modes:

1) Evaluation mode (default):
   Runs SWEContextBench one prediction file at a time so each instance is
   checkpointed independently and resume-safe.

2) Assembly mode (--assemble):
   Joins OFF/ON evaluation checkpoints with per-instance telemetry into a
   `wevibe_bench.scorecard.Scorecard` JSON artifact.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any

from wevibe_bench.config import RunConfig
from wevibe_bench.scorecard import Cell, Scorecard


DEFAULT_SWECB_DIR = Path("~/Desktop/benchmark/SWEContextBench").expanduser()
DEFAULT_SWECB_RUNS_ROOT = Path("~/Desktop/benchmark/runs/swecb").expanduser()
PREDICTION_SUFFIX = "_preds.json"
DRIVER_ERROR_PREFIX = "DRIVER_ERROR:"


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


def _to_bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


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
        for part in message.splitlines() or [message]:
            self.log(part)

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


def _load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"checkpoint must be a JSON object: {path}")
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            normalized[str(key)] = value
    return normalized


def _has_resolved_verdict(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get("resolved"), bool):
        return False
    err = entry.get("error")
    if isinstance(err, str) and err.startswith(DRIVER_ERROR_PREFIX):
        return False
    return True


def _list_prediction_files(predictions_dir: Path) -> dict[str, Path]:
    if not predictions_dir.is_dir():
        raise FileNotFoundError(f"predictions dir not found: {predictions_dir}")
    mapping: dict[str, Path] = {}
    for file in sorted(predictions_dir.glob(f"*{PREDICTION_SUFFIX}")):
        iid = file.name[: -len(PREDICTION_SUFFIX)]
        if iid in mapping:
            raise ValueError(f"duplicate prediction file for instance_id={iid}")
        mapping[iid] = file
    if not mapping:
        raise ValueError(f"no *{PREDICTION_SUFFIX} files found in {predictions_dir}")
    return mapping


def _parse_instances_arg(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items or None


def _resolve_instance_selection(
    *,
    prediction_files: dict[str, Path],
    instances_filter: list[str] | None,
) -> list[str]:
    if instances_filter is None:
        return sorted(prediction_files.keys())

    missing = [iid for iid in instances_filter if iid not in prediction_files]
    if missing:
        raise ValueError(
            "requested instance(s) missing prediction files: " + ", ".join(sorted(missing))
        )
    return list(instances_filter)


def _safe_run_id_component(value: str) -> str:
    safe_chars: list[str] = []
    for ch in value.strip():
        if ch.isalnum() or ch in {"-", "_", "."}:
            safe_chars.append(ch)
        else:
            safe_chars.append("_")
    safe = "".join(safe_chars).strip("_")
    return safe or "unknown"


def _stream_command(
    *,
    argv: list[str],
    cwd: Path,
    logger: LiveLogger,
    prefix: str,
) -> int:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    logger.log(f"{prefix} argv={shlex.join(argv)}")
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        logger.log(f"{prefix} {line.rstrip()}")
    rc = proc.wait()
    logger.log(f"{prefix} exit_code={rc}")
    return rc


def _instance_report_entry(report: dict[str, Any], instance_id: str) -> dict[str, Any]:
    direct = report.get(instance_id)
    if isinstance(direct, dict):
        return direct

    lower_target = instance_id.lower()
    for key, value in report.items():
        if isinstance(key, str) and key.lower() == lower_target and isinstance(value, dict):
            return value
    raise KeyError(f"instance_id not found in report: {instance_id}")


def _f2p_counts(entry: dict[str, Any]) -> tuple[int, int]:
    tests_status = entry.get("tests_status")
    if not isinstance(tests_status, dict):
        return 0, 0
    fail_to_pass = tests_status.get("FAIL_TO_PASS")
    if not isinstance(fail_to_pass, dict):
        return 0, 0

    success = fail_to_pass.get("success")
    failure = fail_to_pass.get("failure")
    pass_count = len(success) if isinstance(success, list) else _to_int(success)
    fail_count = len(failure) if isinstance(failure, list) else _to_int(failure)
    return max(pass_count, 0), max(fail_count, 0)


@dataclass
class EvalVerdict:
    resolved: bool
    f2p_pass: int
    f2p_fail: int
    patch_applied: bool | None
    error: str | None


def _parse_per_instance_report(*, report_path: Path, instance_id: str) -> EvalVerdict:
    if not report_path.exists():
        raise FileNotFoundError(f"evaluation report not found: {report_path}")
    payload = _load_json(report_path)
    if not isinstance(payload, dict):
        raise ValueError(f"evaluation report is not a JSON object: {report_path}")

    entry = _instance_report_entry(payload, instance_id)
    resolved = bool(entry.get("resolved", False))
    f2p_pass, f2p_fail = _f2p_counts(entry)
    patch_applied = _to_bool_or_none(entry.get("patch_applied"))
    error_raw = entry.get("error")
    error = str(error_raw) if error_raw is not None else None

    return EvalVerdict(
        resolved=resolved,
        f2p_pass=f2p_pass,
        f2p_fail=f2p_fail,
        patch_applied=patch_applied,
        error=error,
    )


def _evaluate_single_instance(
    *,
    swecb_dir: Path,
    prediction_file: Path,
    instance_id: str,
    run_id: str,
    keep_image: bool,
    logger: LiveLogger,
) -> tuple[EvalVerdict, float]:
    start = time.monotonic()
    python_cmd = ["python3"]

    with tempfile.TemporaryDirectory(prefix=f"swecb-{instance_id}-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        single_preds_dir = tmp_path / "predictions"
        single_preds_dir.mkdir(parents=True, exist_ok=True)
        dst_file = single_preds_dir / prediction_file.name
        shutil.copy2(prediction_file, dst_file)

        dataset_file = tmp_path / "batch_dataset.json"
        predictions_file = tmp_path / "batch_predictions.json"

        combine_cmd = [
            *python_cmd,
            "-m",
            "swebench_memory.harness.combine_instances",
            "--instances",
            "cases/SWEContextBench Lite",
            "--predictions",
            str(single_preds_dir),
            "--dataset-output",
            str(dataset_file),
            "--predictions-output",
            str(predictions_file),
        ]
        combine_rc = _stream_command(
            argv=combine_cmd,
            cwd=swecb_dir,
            logger=logger,
            prefix=f"[combine {instance_id}]",
        )
        if combine_rc != 0:
            raise RuntimeError(f"combine_instances failed with exit code {combine_rc}")

        eval_cmd = [
            *python_cmd,
            "-m",
            "swebench_memory.harness.run_evaluation",
            "--dataset_name",
            str(dataset_file),
            "--predictions_path",
            str(predictions_file),
            "--run_id",
            run_id,
        ]
        if keep_image:
            eval_cmd.append("--no-remove-instance-image")

        eval_rc = _stream_command(
            argv=eval_cmd,
            cwd=swecb_dir,
            logger=logger,
            prefix=f"[run_evaluation {instance_id}]",
        )

    report_path = swecb_dir / f"{run_id}.json"
    verdict = _parse_per_instance_report(report_path=report_path, instance_id=instance_id)
    if eval_rc != 0:
        err = f"run_evaluation exited {eval_rc}"
        verdict = EvalVerdict(
            resolved=verdict.resolved,
            f2p_pass=verdict.f2p_pass,
            f2p_fail=verdict.f2p_fail,
            patch_applied=verdict.patch_applied,
            error=verdict.error or err,
        )

    return verdict, time.monotonic() - start


def run_eval_mode(args: argparse.Namespace) -> int:
    swecb_dir = _safe_path(args.swecb_dir)
    predictions_dir = _safe_path(args.predictions_dir)
    condition = args.condition.lower()
    run_label = args.run_label

    run_root = DEFAULT_SWECB_RUNS_ROOT / run_label
    checkpoint_path = _safe_path(args.checkpoint) if args.checkpoint else run_root / f"eval_checkpoint_{condition}.json"
    log_path = _safe_path(args.log) if args.log else run_root / f"{_timestamp_for_filename()}-eval-{condition}.log"

    logger = LiveLogger(log_path)
    logger.log(
        "[eval driver] start "
        f"condition={condition} run_label={run_label} predictions_dir={predictions_dir} "
        f"swecb_dir={swecb_dir} checkpoint={checkpoint_path} keep_image={bool(args.keep_image)}"
    )

    try:
        prediction_files = _list_prediction_files(predictions_dir)
        selected_iids = _resolve_instance_selection(
            prediction_files=prediction_files,
            instances_filter=_parse_instances_arg(args.instances),
        )

        checkpoint = _load_checkpoint(checkpoint_path)
        total = len(selected_iids)
        logger.log(f"[eval driver] selected_instances={total}")

        for index, instance_id in enumerate(selected_iids, start=1):
            existing = checkpoint.get(instance_id)
            if _has_resolved_verdict(existing):
                f2p_pass = _to_int(existing.get("f2p_pass"))
                f2p_fail = _to_int(existing.get("f2p_fail"))
                f2p_total = f2p_pass + f2p_fail
                logger.log(
                    f"[eval {condition}] {instance_id} already scored "
                    f"resolved={bool(existing.get('resolved'))} "
                    f"f2p={f2p_pass}/{f2p_total} ({index}/{total})"
                )
                continue

            per_iid_run_id = (
                f"{_safe_run_id_component(run_label)}-"
                f"{_safe_run_id_component(condition)}-"
                f"{_safe_run_id_component(instance_id)}"
            )

            try:
                verdict, elapsed = _evaluate_single_instance(
                    swecb_dir=swecb_dir,
                    prediction_file=prediction_files[instance_id],
                    instance_id=instance_id,
                    run_id=per_iid_run_id,
                    keep_image=bool(args.keep_image),
                    logger=logger,
                )
                f2p_total = verdict.f2p_pass + verdict.f2p_fail

                checkpoint[instance_id] = {
                    "resolved": verdict.resolved,
                    "f2p_pass": verdict.f2p_pass,
                    "f2p_fail": verdict.f2p_fail,
                    "patch_applied": verdict.patch_applied,
                    "error": verdict.error,
                    "run_id": per_iid_run_id,
                    "scored_at": _utc_now_iso(),
                }
                _write_json_atomic(checkpoint_path, checkpoint)

                logger.log(
                    f"[eval {condition}] {instance_id} resolved={verdict.resolved} "
                    f"f2p={verdict.f2p_pass}/{f2p_total} ({index}/{total}) "
                    f"elapsed={elapsed:.1f}s"
                )
                if verdict.error:
                    logger.log(f"[eval {condition}] {instance_id} evaluator_error={verdict.error}")
            except Exception as exc:
                full_trace = traceback.format_exc()
                error_text = f"{DRIVER_ERROR_PREFIX} {type(exc).__name__}: {exc}"

                checkpoint[instance_id] = {
                    "resolved": False,
                    "f2p_pass": 0,
                    "f2p_fail": 0,
                    "patch_applied": None,
                    "error": error_text,
                    "run_id": per_iid_run_id,
                    "scored_at": _utc_now_iso(),
                }
                _write_json_atomic(checkpoint_path, checkpoint)

                logger.log(f"[eval {condition}] {instance_id} resolved=False f2p=0/0 ({index}/{total})")
                logger.log(f"[eval {condition}] {instance_id} FULL ERROR BEGIN")
                logger.log_multiline(full_trace.rstrip())
                logger.log(f"[eval {condition}] {instance_id} FULL ERROR END")

        logger.log("[eval driver] complete")
        logger.log(f"[eval driver] checkpoint={checkpoint_path}")
        logger.log(f"[eval driver] log={log_path}")
        return 0
    finally:
        logger.close()


def _load_telemetry(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = _load_json(path)
    if not isinstance(payload, dict):
        return None
    return payload


def _cell_from_records(
    *,
    model: str,
    instance_id: str,
    condition: str,
    verdict_entry: dict[str, Any] | None,
    telemetry: dict[str, Any] | None,
) -> Cell | None:
    if not isinstance(verdict_entry, dict) or not isinstance(verdict_entry.get("resolved"), bool):
        return None
    if telemetry is None:
        return None

    resolved = bool(verdict_entry["resolved"])
    input_tokens = _to_int(telemetry.get("input_tokens"), default=0)
    output_tokens = _to_int(telemetry.get("output_tokens"), default=0)
    turns = _to_int(telemetry.get("turns"), default=0)
    wall_cost_usd = _to_float(telemetry.get("wall_cost_usd"), default=0.0)
    wall_seconds = _to_float(telemetry.get("wall_seconds"), default=0.0)

    if condition == "OFF":
        delivery = "N/A"
        scored = True
        not_scored_reason = None
    else:
        delivery_raw = telemetry.get("delivery")
        delivery = str(delivery_raw) if delivery_raw is not None else "UNKNOWN"
        scored = delivery == "YES"
        not_scored_reason = None if scored else f"delivery={delivery}"

    return Cell(
        model=model,
        task_id=instance_id,
        condition=condition,
        resolved=resolved,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        turns=turns,
        wall_cost_usd=wall_cost_usd,
        wall_seconds=wall_seconds,
        delivery=delivery,
        scored=scored,
        not_scored_reason=not_scored_reason,
    )


def assemble_scorecard(
    *,
    off_checkpoint: Path,
    on_checkpoint: Path,
    telemetry_dir: Path,
    model: str,
    output_path: Path,
) -> Scorecard:
    off_data = _load_checkpoint(off_checkpoint)
    on_data = _load_checkpoint(on_checkpoint)

    cfg = RunConfig()
    scorecard = Scorecard(cfg)

    all_iids = sorted(set(off_data.keys()) | set(on_data.keys()))

    for iid in all_iids:
        off_telemetry_path = telemetry_dir / f"off_{iid}.json"
        on_telemetry_path = telemetry_dir / f"on_{iid}.json"

        off_telemetry = _load_telemetry(off_telemetry_path)
        on_telemetry = _load_telemetry(on_telemetry_path)

        if off_telemetry is None:
            print(f"[assemble] missing telemetry: {off_telemetry_path}", flush=True)
        if on_telemetry is None:
            print(f"[assemble] missing telemetry: {on_telemetry_path}", flush=True)

        off_cell = _cell_from_records(
            model=model,
            instance_id=iid,
            condition="OFF",
            verdict_entry=off_data.get(iid),
            telemetry=off_telemetry,
        )
        if off_cell is None:
            print(f"[assemble] skipped OFF cell for {iid}", flush=True)
        else:
            scorecard.add_cell(off_cell)

        on_cell = _cell_from_records(
            model=model,
            instance_id=iid,
            condition="ON",
            verdict_entry=on_data.get(iid),
            telemetry=on_telemetry,
        )
        if on_cell is None:
            print(f"[assemble] skipped ON cell for {iid}", flush=True)
        else:
            scorecard.add_cell(on_cell)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(scorecard.to_json(), encoding="utf-8")

    return scorecard


def run_assemble_mode(args: argparse.Namespace) -> int:
    run_root = DEFAULT_SWECB_RUNS_ROOT / args.run_label

    off_checkpoint = _safe_path(args.off_checkpoint) if args.off_checkpoint else run_root / "eval_checkpoint_off.json"
    on_checkpoint = _safe_path(args.on_checkpoint) if args.on_checkpoint else run_root / "eval_checkpoint_on.json"
    telemetry_dir = _safe_path(args.telemetry_dir) if args.telemetry_dir else run_root / "telemetry"
    output_path = _safe_path(args.scorecard_out) if args.scorecard_out else run_root / "scorecard.json"

    scorecard = assemble_scorecard(
        off_checkpoint=off_checkpoint,
        on_checkpoint=on_checkpoint,
        telemetry_dir=telemetry_dir,
        model=args.model,
        output_path=output_path,
    )

    model_diffs = scorecard.model_diffs()
    diff = next((d for d in model_diffs if d.model == args.model), None)
    if diff is None and model_diffs:
        diff = model_diffs[0]

    off_cells = [c for c in scorecard.cells if c.condition == "OFF" and c.model == args.model]
    on_cells = [c for c in scorecard.cells if c.condition == "ON" and c.model == args.model]
    on_scored = [c for c in on_cells if c.scored]

    off_resolved = sum(1 for c in off_cells if c.resolved)
    on_resolved_scored = sum(1 for c in on_scored if c.resolved)
    on_delivery_yes = sum(1 for c in on_cells if c.delivery == "YES")

    print("[assemble] scorecard written: " + str(output_path), flush=True)
    print(f"[assemble] OFF resolved {off_resolved}/{len(off_cells)}", flush=True)
    print(f"[assemble] ON resolved {on_resolved_scored}/{len(on_scored)} (scored)", flush=True)

    if diff is not None:
        print(f"[assemble] capability_lift_pp {diff.capability_lift_pp:.2f}", flush=True)
        print(
            f"[assemble] tokens OFF vs ON {diff.off_total_tokens} vs {diff.on_total_tokens}",
            flush=True,
        )
    else:
        print("[assemble] no model_diff available (no complete cell set)", flush=True)

    print(f"[assemble] ON delivery=YES count {on_delivery_yes}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable SWEContextBench evaluator + scorecard assembler")

    parser.add_argument("--assemble", action="store_true", help="Assemble scorecard instead of running evaluation")

    # Evaluation mode args.
    parser.add_argument("--predictions-dir", type=str, help="Directory containing <iid>_preds.json files")
    parser.add_argument("--condition", choices=("off", "on"), help="Ablation condition")
    parser.add_argument("--run-label", type=str, required=True, help="Run label used for output roots")
    parser.add_argument(
        "--swecb-dir",
        type=str,
        default=str(DEFAULT_SWECB_DIR),
        help=f"SWEContextBench root (default: {DEFAULT_SWECB_DIR})",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        help="Checkpoint path (default: ~/Desktop/benchmark/runs/swecb/<run-label>/eval_checkpoint_<condition>.json)",
    )
    parser.add_argument("--instances", type=str, help="Optional comma-separated instance filter")
    parser.add_argument(
        "--keep-image",
        action="store_true",
        help="Pass --no-remove-instance-image to run_evaluation",
    )
    parser.add_argument(
        "--log",
        type=str,
        help="Log file path (default: timestamped log under ~/Desktop/benchmark/runs/swecb/<run-label>/)",
    )

    # Assembly mode args.
    parser.add_argument("--off-checkpoint", type=str, help="OFF checkpoint JSON path")
    parser.add_argument("--on-checkpoint", type=str, help="ON checkpoint JSON path")
    parser.add_argument("--telemetry-dir", type=str, help="Telemetry directory path")
    parser.add_argument("--model", type=str, help="Model label for scorecard cells")
    parser.add_argument("--scorecard-out", type=str, help="Scorecard output JSON path")

    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser = build_parser()
    args = parser.parse_args()

    if args.assemble:
        if not args.model:
            parser.error("--model is required in --assemble mode")
        return run_assemble_mode(args)

    if not args.predictions_dir:
        parser.error("--predictions-dir is required in evaluation mode")
    if not args.condition:
        parser.error("--condition is required in evaluation mode")

    return run_eval_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
