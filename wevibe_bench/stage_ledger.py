"""Stage-level qualification budget ledger and admission CLI.

This module aggregates per-run OpenRouter proxy budget checkpoint JSON files
into stage-level totals and enforces stage/global caps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # R-37 trace compatibility with lifecycle logging utilities.
    from wevibe_bench.lifecycle.logging_util import new_trace_id as _new_trace_id
except Exception:  # noqa: BLE001 - fallback required by task directive.
    _new_trace_id = None


SCHEMA_VERSION = 1
CAP_REFUSAL_EXIT = 3
EPSILON = 1e-9

STAGE_KEYS: tuple[str, ...] = ("stage2", "stage3", "stage4", "stage5")
DEFAULT_CAPS: dict[str, float] = {
    "stage2": 10.0,
    "stage3": 25.0,
    "stage4": 40.0,
    "stage5": 40.0,
    "global": 115.0,
}

DEFAULT_LEDGER_PATH = Path("runs/qualification/stage-ledger.json")
DEFAULT_LOG_PATH = Path("runs/qualification/stage-ledger.log")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _trace_id() -> str:
    if callable(_new_trace_id):
        return _new_trace_id()
    return uuid.uuid4().hex


def _json_stdout(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _append_log(
    *,
    log_path: Path,
    trace_id: str,
    op: str,
    stage: str,
    outcome: str,
    **fields: Any,
) -> None:
    """Append one timestamped operation line to the stage-ledger log."""

    directory = log_path.parent
    if str(directory):
        directory.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "op": op,
        "stage": stage,
        "outcome": outcome,
        **fields,
    }

    chunks = [f"{key}={json.dumps(payload[key], ensure_ascii=False, separators=(',', ':'))}" for key in sorted(payload)]
    line = f"{_utc_now_iso()} | {' '.join(chunks)}"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def _fresh_ledger() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "caps": dict(DEFAULT_CAPS),
        "stages": {stage: [] for stage in STAGE_KEYS},
    }


def _stage_key(stage_number: int) -> str:
    key = f"stage{int(stage_number)}"
    if key not in STAGE_KEYS:
        raise ValueError(f"unsupported stage: {stage_number}")
    return key


def _coerce_nonnegative_float(value: Any, *, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if number < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return number


def _load_ledger(ledger_path: Path) -> dict[str, Any]:
    if not ledger_path.exists():
        return _fresh_ledger()

    try:
        with ledger_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return _fresh_ledger()
    except json.JSONDecodeError as exc:
        raise ValueError(f"ledger JSON decode failed: {ledger_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("ledger root must be an object")

    schema_version = int(raw.get("schema_version", 0))
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"ledger schema_version mismatch: expected {SCHEMA_VERSION}, got {schema_version}"
        )

    caps_raw = raw.get("caps")
    if not isinstance(caps_raw, dict):
        raise ValueError("ledger.caps must be an object")

    caps: dict[str, float] = {}
    for cap_key in (*STAGE_KEYS, "global"):
        if cap_key not in caps_raw:
            raise ValueError(f"ledger.caps missing {cap_key}")
        caps[cap_key] = _coerce_nonnegative_float(caps_raw[cap_key], field_name=f"caps.{cap_key}")

    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, dict):
        raise ValueError("ledger.stages must be an object")

    stages: dict[str, list[dict[str, Any]]] = {}
    for stage in STAGE_KEYS:
        stage_entries = stages_raw.get(stage, [])
        if not isinstance(stage_entries, list):
            raise ValueError(f"ledger.stages.{stage} must be a list")

        normalized_entries: list[dict[str, Any]] = []
        for idx, entry_raw in enumerate(stage_entries):
            if not isinstance(entry_raw, dict):
                raise ValueError(f"ledger.stages.{stage}[{idx}] must be an object")

            run_id = str(entry_raw.get("run_id", "")).strip()
            if not run_id:
                raise ValueError(f"ledger.stages.{stage}[{idx}].run_id is required")

            budget_json = str(entry_raw.get("budget_json", "")).strip()
            if not budget_json:
                raise ValueError(f"ledger.stages.{stage}[{idx}].budget_json is required")

            normalized_entries.append(
                {
                    "run_id": run_id,
                    "budget_json": budget_json,
                    "accrued_usd": _coerce_nonnegative_float(
                        entry_raw.get("accrued_usd", 0.0),
                        field_name=f"ledger.stages.{stage}[{idx}].accrued_usd",
                    ),
                    "committed_unproven_usd": _coerce_nonnegative_float(
                        entry_raw.get("committed_unproven_usd", 0.0),
                        field_name=f"ledger.stages.{stage}[{idx}].committed_unproven_usd",
                    ),
                    "recorded_at": str(entry_raw.get("recorded_at", "")).strip() or _utc_now_iso(),
                }
            )

        stages[stage] = normalized_entries

    return {
        "schema_version": SCHEMA_VERSION,
        "caps": caps,
        "stages": stages,
    }


def _write_ledger_atomic(ledger_path: Path, ledger: dict[str, Any]) -> None:
    directory = ledger_path.parent
    if str(directory):
        directory.mkdir(parents=True, exist_ok=True)

    tmp_path = ledger_path.with_name(f"{ledger_path.name}.{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(ledger, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(tmp_path, ledger_path)


def _load_budget_json(budget_path: Path) -> dict[str, Any]:
    try:
        with budget_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"budget JSON not found: {budget_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"budget JSON decode failed: {budget_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("budget JSON root must be an object")

    run_id = str(raw.get("run_id", "")).strip()
    if not run_id:
        raise ValueError("budget JSON missing run_id")

    schema_version = int(raw.get("schema_version", 0))
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"budget schema_version mismatch: expected {SCHEMA_VERSION}, got {schema_version}"
        )

    return {
        "run_id": run_id,
        "budget_json": str(budget_path.resolve()),
        "accrued_usd": _coerce_nonnegative_float(raw.get("accrued_actual_usd", 0.0), field_name="accrued_actual_usd"),
        "committed_unproven_usd": _coerce_nonnegative_float(
            raw.get("committed_unproven_usd", 0.0),
            field_name="committed_unproven_usd",
        ),
    }


def _summaries(ledger: dict[str, Any]) -> dict[str, Any]:
    stages_report: dict[str, dict[str, float]] = {}
    global_accrued = 0.0
    global_committed = 0.0

    for stage in STAGE_KEYS:
        entries = ledger["stages"][stage]
        accrued = sum(float(entry["accrued_usd"]) for entry in entries)
        committed = sum(float(entry["committed_unproven_usd"]) for entry in entries)
        total = accrued + committed
        cap = float(ledger["caps"][stage])

        stages_report[stage] = {
            "accrued_usd": accrued,
            "committed_unproven_usd": committed,
            "sum_usd": total,
            "cap_usd": cap,
            "remaining_usd": cap - total,
        }

        global_accrued += accrued
        global_committed += committed

    global_sum = global_accrued + global_committed
    global_cap = float(ledger["caps"]["global"])

    return {
        "schema_version": SCHEMA_VERSION,
        "caps": dict(ledger["caps"]),
        "stages": stages_report,
        "global": {
            "accrued_usd": global_accrued,
            "committed_unproven_usd": global_committed,
            "sum_usd": global_sum,
            "cap_usd": global_cap,
            "remaining_usd": global_cap - global_sum,
        },
    }


def _record(args: argparse.Namespace, *, trace_id: str) -> int:
    stage = _stage_key(args.stage)
    ledger_path = Path(args.ledger)
    log_path = Path(args.log)

    try:
        budget = _load_budget_json(Path(args.budget_json))
        ledger = _load_ledger(ledger_path)
        before = _summaries(ledger)

        entries = list(ledger["stages"][stage])
        old_total = 0.0
        remaining_entries: list[dict[str, Any]] = []
        for entry in entries:
            if entry["run_id"] == budget["run_id"]:
                old_total += float(entry["accrued_usd"]) + float(entry["committed_unproven_usd"])
                continue
            remaining_entries.append(entry)

        new_total = float(budget["accrued_usd"]) + float(budget["committed_unproven_usd"])
        projected_stage_sum = float(before["stages"][stage]["sum_usd"]) - old_total + new_total
        projected_global_sum = float(before["global"]["sum_usd"]) - old_total + new_total

        stage_cap = float(ledger["caps"][stage])
        global_cap = float(ledger["caps"]["global"])
        stage_remaining = stage_cap - projected_stage_sum
        global_remaining = global_cap - projected_global_sum

        if stage_remaining < -EPSILON or global_remaining < -EPSILON:
            payload = {
                "recorded": False,
                "reason": "cap_exceeded",
                "stage": stage,
                "run_id": budget["run_id"],
                "stage_remaining": stage_remaining,
                "global_remaining": global_remaining,
            }
            _json_stdout(payload)
            _append_log(
                log_path=log_path,
                trace_id=trace_id,
                op="record",
                stage=stage,
                outcome="refused",
                run_id=budget["run_id"],
                accrued_usd=budget["accrued_usd"],
                committed_unproven_usd=budget["committed_unproven_usd"],
                stage_cap_usd=stage_cap,
                global_cap_usd=global_cap,
                stage_projected_usd=projected_stage_sum,
                global_projected_usd=projected_global_sum,
            )
            return CAP_REFUSAL_EXIT

        remaining_entries.append(
            {
                "run_id": budget["run_id"],
                "budget_json": budget["budget_json"],
                "accrued_usd": budget["accrued_usd"],
                "committed_unproven_usd": budget["committed_unproven_usd"],
                "recorded_at": _utc_now_iso(),
            }
        )
        ledger["stages"][stage] = remaining_entries

        _write_ledger_atomic(ledger_path, ledger)

        payload = {
            "recorded": True,
            "stage": stage,
            "run_id": budget["run_id"],
            "stage_remaining": stage_remaining,
            "global_remaining": global_remaining,
        }
        _json_stdout(payload)
        _append_log(
            log_path=log_path,
            trace_id=trace_id,
            op="record",
            stage=stage,
            outcome="recorded",
            run_id=budget["run_id"],
            accrued_usd=budget["accrued_usd"],
            committed_unproven_usd=budget["committed_unproven_usd"],
            stage_projected_usd=projected_stage_sum,
            global_projected_usd=projected_global_sum,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI must surface/log full errors.
        _append_log(
            log_path=log_path,
            trace_id=trace_id,
            op="record",
            stage=stage,
            outcome="error",
            budget_json=str(args.budget_json),
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        print(f"stage-ledger record error: {exc}", file=sys.stderr, flush=True)
        return 1


def _check(args: argparse.Namespace, *, trace_id: str) -> int:
    stage = _stage_key(args.stage)
    ledger_path = Path(args.ledger)
    log_path = Path(args.log)

    try:
        estimated_usd = _coerce_nonnegative_float(args.estimated_usd, field_name="--estimated-usd")
        ledger = _load_ledger(ledger_path)
        summary = _summaries(ledger)

        stage_cap = float(summary["stages"][stage]["cap_usd"])
        global_cap = float(summary["global"]["cap_usd"])
        projected_stage_sum = float(summary["stages"][stage]["sum_usd"]) + estimated_usd
        projected_global_sum = float(summary["global"]["sum_usd"]) + estimated_usd
        stage_remaining = stage_cap - projected_stage_sum
        global_remaining = global_cap - projected_global_sum
        admitted = stage_remaining >= -EPSILON and global_remaining >= -EPSILON

        payload = {
            "admitted": admitted,
            "stage": stage,
            "estimated_usd": estimated_usd,
            "stage_remaining": stage_remaining,
            "global_remaining": global_remaining,
        }
        _json_stdout(payload)
        _append_log(
            log_path=log_path,
            trace_id=trace_id,
            op="check",
            stage=stage,
            outcome="admitted" if admitted else "refused",
            estimated_usd=estimated_usd,
            stage_projected_usd=projected_stage_sum,
            global_projected_usd=projected_global_sum,
            stage_cap_usd=stage_cap,
            global_cap_usd=global_cap,
        )
        return 0 if admitted else CAP_REFUSAL_EXIT
    except Exception as exc:  # noqa: BLE001 - CLI must surface/log full errors.
        _append_log(
            log_path=log_path,
            trace_id=trace_id,
            op="check",
            stage=stage,
            outcome="error",
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        print(f"stage-ledger check error: {exc}", file=sys.stderr, flush=True)
        return 1


def _report(args: argparse.Namespace, *, trace_id: str) -> int:
    ledger_path = Path(args.ledger)
    log_path = Path(args.log)

    try:
        ledger = _load_ledger(ledger_path)
        summary = _summaries(ledger)
        _json_stdout(summary)
        _append_log(
            log_path=log_path,
            trace_id=trace_id,
            op="report",
            stage="all",
            outcome="ok",
            global_sum_usd=summary["global"]["sum_usd"],
            global_remaining_usd=summary["global"]["remaining_usd"],
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI must surface/log full errors.
        _append_log(
            log_path=log_path,
            trace_id=trace_id,
            op="report",
            stage="all",
            outcome="error",
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        print(f"stage-ledger report error: {exc}", file=sys.stderr, flush=True)
        return 1


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage qualification budget ledger")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record", help="Record one run budget JSON into a stage ledger")
    _add_common_paths(record_parser)
    record_parser.add_argument("--stage", type=int, choices=(2, 3, 4, 5), required=True)
    record_parser.add_argument("--budget-json", required=True)

    check_parser = subparsers.add_parser("check", help="Admission check against stage/global caps")
    _add_common_paths(check_parser)
    check_parser.add_argument("--stage", type=int, choices=(2, 3, 4, 5), required=True)
    check_parser.add_argument("--estimated-usd", type=float, default=0.0)

    report_parser = subparsers.add_parser("report", help="Emit stage/global totals report as JSON")
    _add_common_paths(report_parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trace_id = _trace_id()

    if args.command == "record":
        return _record(args, trace_id=trace_id)
    if args.command == "check":
        return _check(args, trace_id=trace_id)
    if args.command == "report":
        return _report(args, trace_id=trace_id)

    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
