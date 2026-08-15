"""Backgammon ladder driver: session with retry/resume/escalation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wevibe_bench.lifecycle.logging_util import new_trace_id, run_logger


DEFAULT_MAX_RETRIES = 5
_PHASE_ORDER = ("session",)
_SESSION_RESULT_PREFIX = "BACKGAMMON_RESULT_JSON "
_LADDER_RESULT_PREFIX = "BACKGAMMON_LADDER_RESULT_JSON "


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _bench_repo_dir() -> Path:
    return (_workspace_root() / "wevibe-bench").resolve()


def _default_runs_dir() -> Path:
    return _bench_repo_dir() / "runs" / "backgammon"


def _slugify_model(model: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", model.replace("/", "-")).strip("-").lower()
    return slug or "model"


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
        raise RuntimeError(f"JSON file exists but is empty: {path}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON file must decode to an object: {path}")
    return payload


def _split_extra_flags(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    return shlex.split(text)


def _result_prefix(phase: str) -> str:
    if phase == "session":
        return _SESSION_RESULT_PREFIX
    raise ValueError(f"unknown phase {phase!r}")


def _parse_result_payload(phase: str, stdout: str, stderr: str) -> tuple[dict[str, Any] | None, str]:
    prefix = _result_prefix(phase)
    lines = list(stdout.splitlines()) + list(stderr.splitlines())
    for raw in reversed(lines):
        line = raw.strip()
        if not line.startswith(prefix):
            continue
        payload_raw = line[len(prefix) :].strip()
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            return None, f"invalid result JSON ({prefix.strip()}): {exc}"
        if not isinstance(payload, dict):
            return None, f"invalid result JSON ({prefix.strip()}): payload must be object"
        return payload, ""
    return None, f"missing result JSON line ({prefix.strip()})"


def build_session_cmd(
    python_exe: str,
    scripts_dir: Path,
    args: argparse.Namespace,
    run_label: str,
) -> list[str]:
    cmd = [
        python_exe,
        str(scripts_dir / "run_backgammon.py"),
        "--model",
        str(args.model),
        "--memory-modes",
        str(args.memory_modes),
        "--run-label",
        run_label,
        "--mock",
        str(args.mock),
        "--runs-dir",
        str(args.runs_dir),
        "--session-id",
        str(args.session_id),
    ]
    if bool(args.resume):
        cmd.append("--resume")
    cmd.extend(_split_extra_flags(str(args.extra_session_flags)))
    return cmd


def run_unit(phase: str, cmd: list[str], logfile_path: str | Path, dry_run: bool) -> tuple[bool, dict[str, Any]]:
    phase_norm = phase.strip().lower()
    if phase_norm not in _PHASE_ORDER:
        raise ValueError(f"unsupported phase {phase!r}")

    path = Path(logfile_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    if dry_run:
        payload: dict[str, Any] = {"status": "ok"}
        prefix = _result_prefix(phase_norm)
        lines = [
            f"DRY_RUN cmd={json.dumps(cmd)}",
            prefix + json.dumps(payload, separators=(",", ":"), sort_keys=True),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        dur_seconds = round(time.perf_counter() - started, 6)
        detail = {
            "phase": phase_norm,
            "exit_code": 0,
            "status": "ok",
            "result_payload": payload,
            "error_text": "",
            "stdout": "",
            "stderr": "",
            "dur_seconds": dur_seconds,
            "logfile": str(path),
            "dry_run": True,
        }
        return True, detail

    # Stream the unit's output LIVE (R-31): merged stdout+stderr is relayed
    # line-by-line to this process's stdout AND the step logfile as it is
    # produced, so a hang-watchdog and a human tail see per-step progress
    # during a long session instead of one buffered burst at unit end.
    with path.open("w", encoding="utf-8") as fh:
        fh.write(f"phase={phase_norm}\n")
        fh.write(f"cmd={json.dumps(cmd)}\n")
        fh.write("=== OUTPUT (stdout+stderr merged, streamed live) ===\n")
        fh.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        lines: list[str] = []
        for line in proc.stdout:
            lines.append(line)
            fh.write(line)
            fh.flush()
            print(line, end="" if line.endswith("\n") else "\n", flush=True)
        proc.stdout.close()
        returncode = proc.wait()

    stdout = "".join(lines)
    stderr = ""
    output_tail = "".join(lines[-20:]).strip()

    payload, parse_error = _parse_result_payload(phase_norm, stdout, stderr)
    status = ""
    if payload is not None:
        status = str(payload.get("status") or "").strip()

    ok = returncode == 0 and status == "ok"

    error_parts: list[str] = []
    if returncode != 0:
        error_parts.append(f"exit={returncode}")
    if parse_error:
        error_parts.append(parse_error)
    if status and status != "ok":
        error_parts.append(f"status={status}")
    if payload is not None and payload.get("error"):
        error_parts.append(f"error={payload.get('error')}")
    if not ok and output_tail:
        error_parts.append(f"output_tail={output_tail}")
    if not ok and not error_parts:
        error_parts.append("unknown failure")

    dur_seconds = round(time.perf_counter() - started, 6)
    detail = {
        "phase": phase_norm,
        "exit_code": returncode,
        "status": status,
        "result_payload": payload,
        "error_text": "; ".join(error_parts),
        "stdout": stdout,
        "stderr": stderr,
        "dur_seconds": dur_seconds,
        "logfile": str(path),
        "dry_run": False,
    }
    return ok, detail


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backgammon ladder driver (session-only).")
    parser.add_argument("--model", required=True, help="Session model slug.")
    parser.add_argument("--run-number", required=True, type=int)
    parser.add_argument("--run-label", help="Default: run{run_number}-{slug(model)}")
    parser.add_argument("--phase", choices=("session",), default="session")
    parser.add_argument("--memory-modes", default="off")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--mock", choices=("none", "golden", "scaffold"), default="none")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--runs-dir", default=str(_default_runs_dir()))
    parser.add_argument("--extra-session-flags", default="")
    return parser


def _checkpoint_units(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    units_raw = checkpoint.get("units")
    if units_raw is None:
        checkpoint["units"] = []
        return checkpoint["units"]
    if not isinstance(units_raw, list):
        raise RuntimeError("ladder checkpoint 'units' must be an array")
    clean: list[dict[str, Any]] = []
    for item in units_raw:
        if isinstance(item, dict):
            clean.append(item)
    checkpoint["units"] = clean
    return clean


def _new_checkpoint() -> dict[str, Any]:
    return {"units": []}


def _load_checkpoint(path: Path) -> dict[str, Any]:
    loaded = _load_json(path)
    if loaded is None:
        return _new_checkpoint()
    _checkpoint_units(loaded)
    return loaded


def _same_unit(entry: dict[str, Any], run_number: int, model: str, phase: str) -> bool:
    return (
        int(entry.get("run_number", -1)) == int(run_number)
        and str(entry.get("model") or "") == model
        and str(entry.get("phase") or "") == phase
    )


def _find_unit_entry(checkpoint: dict[str, Any], run_number: int, model: str, phase: str) -> dict[str, Any] | None:
    for entry in _checkpoint_units(checkpoint):
        if _same_unit(entry, run_number, model, phase):
            return entry
    return None


def _upsert_unit_entry(checkpoint: dict[str, Any], unit_entry: dict[str, Any]) -> None:
    run_number = int(unit_entry.get("run_number", -1))
    model = str(unit_entry.get("model") or "")
    phase = str(unit_entry.get("phase") or "")
    if run_number < 0 or not model or not phase:
        raise RuntimeError(f"invalid checkpoint unit entry: {unit_entry}")

    units = _checkpoint_units(checkpoint)
    for idx, existing in enumerate(units):
        if _same_unit(existing, run_number, model, phase):
            units[idx] = unit_entry
            return
    units.append(unit_entry)


def _requested_units(phase_arg: str) -> list[str]:
    return ["session"]


def _cleanup_success_logs(
    *,
    runs_dir: Path,
    run_number: int,
    model_slug: str,
    run_label: str,
    step_logfiles: list[str],
    driver_logfile: str,
    logger: Any,
    trace: str,
) -> None:
    removed: list[Path] = []
    preserved: list[Path] = []

    seen: set[str] = set()
    for raw in step_logfiles:
        path = Path(raw).expanduser().resolve()
        if str(path) in seen:
            continue
        seen.add(str(path))
        if not path.is_file():
            continue
        if f"-r{run_number}-{model_slug}-" not in path.name:
            continue
        if "-attempt" not in path.name:
            continue
        path.unlink()
        removed.append(path)
        logger.info("trace=%s cleanup removed=%s", trace, path)

    checkpoint_path = runs_dir / "ladder-checkpoint.json"
    if checkpoint_path.is_file():
        preserved.append(checkpoint_path)

    for path in sorted(runs_dir.glob("*.json")):
        name = path.name
        if run_label in name and (name.endswith("-scorecard.json") or name.endswith("-detail.json")):
            preserved.append(path)
    for path in sorted(runs_dir.glob("*.md")):
        preserved.append(path)

    preserve_seen: set[str] = set()
    for path in preserved:
        key = str(path)
        if key in preserve_seen:
            continue
        preserve_seen.add(key)
        logger.info("trace=%s cleanup preserved=%s", trace, path)

    driver_path = Path(driver_logfile).expanduser().resolve() if driver_logfile else None
    if driver_path and driver_path.is_file() and driver_path.parent == runs_dir:
        logger.info("trace=%s cleanup removing driver_logfile=%s", trace, driver_path)
        driver_path.unlink()
        removed.append(driver_path)

    print(
        f"[{_utc_iso()}] PROGRESS trace={trace} cleanup removed={len(removed)} preserved={len(preserve_seen)}",
        flush=True,
    )


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.run_number < 0:
        parser.error("--run-number must be >= 0")
    if args.max_retries < 1:
        parser.error("--max-retries must be >= 1")
    try:
        _split_extra_flags(str(args.extra_session_flags))
    except ValueError as exc:
        parser.error(f"invalid --extra-session-flags: {exc}")


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    _validate_args(args, parser)

    model_slug = _slugify_model(str(args.model))
    run_label = str(args.run_label).strip() if args.run_label else f"run{args.run_number}-{model_slug}"

    runs_dir = Path(str(args.runs_dir)).expanduser().resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    args.runs_dir = str(runs_dir)

    logger = run_logger("backgammon-ladder", str(runs_dir))
    driver_logfile = str(getattr(logger, "logfile_path", "")).strip()
    trace = new_trace_id()

    def progress(message: str) -> None:
        line = f"[{_utc_iso()}] PROGRESS trace={trace} {message}"
        print(line, flush=True)
        logger.info(line)

    scripts_dir = Path(__file__).resolve().parent
    python_exe = sys.executable
    checkpoint_path = runs_dir / "ladder-checkpoint.json"
    checkpoint = _load_checkpoint(checkpoint_path)

    units = _requested_units(str(args.phase))
    progress(
        "start "
        f"run_number={args.run_number} model={args.model} run_label={run_label} "
        f"phase={args.phase} units={','.join(units)} dry_run={args.dry_run} resume={args.resume}"
    )

    created_step_logs: list[str] = []
    completed_units: list[dict[str, Any]] = []

    for phase in units:
        prior = _find_unit_entry(checkpoint, args.run_number, str(args.model), phase)
        if args.resume and prior is not None and str(prior.get("status") or "") == "ok":
            progress(f"resume-skip run_number={args.run_number} model={args.model} phase={phase}")
            completed_units.append(
                {
                    "phase": phase,
                    "status": "ok",
                    "attempts": int(prior.get("attempts") or 0),
                    "skipped": True,
                }
            )
            continue

        cmd = build_session_cmd(python_exe, scripts_dir, args, run_label)

        attempt_logfiles: list[str] = []
        last_error = ""

        for attempt in range(1, int(args.max_retries) + 1):
            step_log = runs_dir / f"{_utc_compact()}-r{args.run_number}-{model_slug}-{phase}-attempt{attempt}.log"
            created_step_logs.append(str(step_log))
            attempt_logfiles.append(str(step_log))

            logger.info(
                (
                    "trace=%s op=ladder.attempt.start run_number=%s model=%s phase=%s attempt=%s "
                    "run_label=%s memory_modes=%s cmd=%s logfile=%s"
                ),
                trace,
                args.run_number,
                args.model,
                phase,
                attempt,
                run_label,
                args.memory_modes,
                cmd,
                step_log,
            )
            progress(
                f"attempt-start run_number={args.run_number} model={args.model} phase={phase} "
                f"attempt={attempt}/{args.max_retries} logfile={step_log}"
            )

            if args.dry_run:
                progress(f"dry-run command phase={phase} cmd={json.dumps(cmd)}")

            ok, detail = run_unit(phase=phase, cmd=cmd, logfile_path=step_log, dry_run=bool(args.dry_run))

            logger.info(
                (
                    "trace=%s op=ladder.attempt.end run_number=%s model=%s phase=%s attempt=%s "
                    "exit=%s status=%s dur_seconds=%.3f ok=%s"
                ),
                trace,
                args.run_number,
                args.model,
                phase,
                attempt,
                detail.get("exit_code"),
                detail.get("status"),
                float(detail.get("dur_seconds") or 0.0),
                ok,
            )

            if ok:
                unit_entry = {
                    "run_number": int(args.run_number),
                    "model": str(args.model),
                    "phase": phase,
                    "status": "ok",
                    "attempts": attempt,
                    "completed_at": _utc_iso(),
                    "logfiles": attempt_logfiles,
                }
                _upsert_unit_entry(checkpoint, unit_entry)
                _save_json_atomic(checkpoint_path, checkpoint)
                progress(
                    f"attempt-ok run_number={args.run_number} model={args.model} phase={phase} "
                    f"attempt={attempt}"
                )
                completed_units.append(
                    {
                        "phase": phase,
                        "status": "ok",
                        "attempts": attempt,
                        "skipped": False,
                    }
                )
                break

            last_error = str(detail.get("error_text") or "unit failed")
            logger.error(
                (
                    "trace=%s op=ladder.attempt.fail run_number=%s model=%s phase=%s attempt=%s "
                    "exit=%s status=%s last_error=%s full_stderr=%s"
                ),
                trace,
                args.run_number,
                args.model,
                phase,
                attempt,
                detail.get("exit_code"),
                detail.get("status"),
                last_error,
                detail.get("stderr") or detail.get("stdout") or "",
            )
            progress(
                f"attempt-fail run_number={args.run_number} model={args.model} phase={phase} "
                f"attempt={attempt}/{args.max_retries} exit={detail.get('exit_code')} "
                f"status={detail.get('status')} last_error={last_error}"
            )

            if attempt >= int(args.max_retries):
                escalation_payload = {
                    "status": "aborted",
                    "run_number": int(args.run_number),
                    "model": str(args.model),
                    "phase": phase,
                    "attempts": attempt,
                    "last_error": last_error,
                    "logfiles": attempt_logfiles,
                    "ts": _utc_iso(),
                    "run_label": run_label,
                    "trace": trace,
                }
                escalation_path = runs_dir / f"{run_label}-ESCALATE.json"
                _save_json_atomic(escalation_path, escalation_payload)
                logger.error("trace=%s op=ladder.abort escalation_path=%s payload=%s", trace, escalation_path, escalation_payload)
                print(_LADDER_RESULT_PREFIX + json.dumps(escalation_payload, separators=(",", ":"), sort_keys=True), flush=True)
                return 3

    if not args.dry_run:
        _cleanup_success_logs(
            runs_dir=runs_dir,
            run_number=int(args.run_number),
            model_slug=model_slug,
            run_label=run_label,
            step_logfiles=created_step_logs,
            driver_logfile=driver_logfile,
            logger=logger,
            trace=trace,
        )

    result_payload = {
        "status": "ok",
        "run_number": int(args.run_number),
        "model": str(args.model),
        "run_label": run_label,
        "phase": str(args.phase),
        "units": completed_units,
        "trace": trace,
        "checkpoint_path": str(checkpoint_path),
        "dry_run": bool(args.dry_run),
    }
    print(_LADDER_RESULT_PREFIX + json.dumps(result_payload, separators=(",", ":"), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
