"""Stage-7 scored backgammon ladder driver (roster A).

Runs the scored OFF/ON ladder by invoking scripts/backgammon_ladder.py once per
cell in strict roster order, deriving the roster from wevibe_bench/config.py
(``backgammon_scored_ladder_roster()``). The SOURCE rung (Opus) runs session +
self-extraction to populate the org pool; MEASURE rungs run scored OFF/ON
session-only cells consuming that pool (no extraction below the source, so the
pool is frozen for repeats).

Per cell this driver owns:
- stage-ledger admission (stage 7) + prebudget/post-run recording,
- per-cell OpenRouter proxy lifecycle (pinned profile, live pricing, hard cap),
- upstream-identity assertion (expected_upstream_model rungs, e.g. big-pickle),
- delivery log-assertion for memory-ON cells (clone /v1/recall 200s +
  recall_env_injection=container),
- variance policy N-logic (docs/VARIANCE-POLICY.md): N=1 baseline, borderline
  triggers T1-T4 repeat the cell to N=3, majority/median summary, N disclosed
  per cell in scored-ladder-summary.json,
- checkpoint + --resume per cell/rep (never restart from zero).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from wevibe_bench.config import (
    BACKGAMMON_LADDER_SCHEMA_VERSION,
    LadderRung,
    backgammon_ladder_roster_fingerprint,
    backgammon_scored_ladder_roster,
)


STAGE_NUMBER = 7
CELL_PHASE = "cell"
MAX_REPS = 3
TOKEN_DELTA_FRAGILE = 0.15  # T2 constant (manager-set, vetoable; VARIANCE-POLICY.md)
CHECKPOINT_NAME = "scored-ladder-checkpoint.json"
MANIFEST_NAME = "scored-ladder-manifest.json"
ESCALATE_NAME = "SCORED-LADDER-ESCALATE.json"
SUMMARY_NAME = "scored-ladder-summary.json"
DEFAULT_PROXY_PORT = 8789
PROXY_START_TIMEOUT_S = 45
PROXY_STOP_TIMEOUT_S = 15
RUN_TIMEOUT_S = 5400
MAX_ATTEMPTS = 3
MAX_STEPS_PER_ATTEMPT = 100
MAX_OUTPUT_TOKENS = 32000
REASONING_EFFORT = "high"
EXTRACT_TIMEOUT_S = 900
DEFAULT_ORG_ID = "wevibe-org-0"
DEFAULT_CLONE_LOG = "runs/clone4550.log"

REQUIRED_RUNG_PARAM_FIELDS = (
    "profile",
    "pricing_input",
    "pricing_output",
    "cap_usd",
    "cost_limit",
    "cost_target",
)
OPTIONAL_RUNG_PARAM_FIELDS = (
    "provider_order",
    "provider_quant",
    "output_price_per_1m",
    "expected_upstream_model",
)


class LadderAbort(RuntimeError):
    """Raised to abort the ladder with a structured escalation payload."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_runs_dir() -> Path:
    return _repo_root() / "runs" / "backgammon-scored-ladder"


def _default_ladder_runs_dir() -> Path:
    return _repo_root() / "runs" / "backgammon"


def _ladder_script_path() -> Path:
    return Path(__file__).resolve().parent / "backgammon_ladder.py"


def _proxy_script_path() -> Path:
    return Path(__file__).resolve().parent / "run_openrouter_proxy.py"


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


def _append_log_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _emit(path: Path, line: str) -> None:
    print(line, flush=True)
    _append_log_line(path, line)


def _new_trace_id() -> str:
    return f"stage7-ladder-{_utc_compact()}-{uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Plan / manifest


def _validate_roster(rungs: tuple[LadderRung, ...]) -> None:
    if not rungs:
        raise RuntimeError("scored ladder roster is empty")
    seen_measure = False
    for idx, rung in enumerate(rungs):
        if rung.role not in ("source", "measure"):
            raise RuntimeError(f"rung {idx} has unknown role {rung.role!r}")
        if not str(rung.model).strip():
            raise RuntimeError(f"rung {idx} has blank model")
        if not rung.memory_modes:
            raise RuntimeError(f"rung {idx} has no memory modes")
        for mode in rung.memory_modes:
            if mode not in ("off", "on"):
                raise RuntimeError(f"rung {idx} has unknown memory mode {mode!r}")
        if rung.role == "measure":
            seen_measure = True
            if tuple(rung.memory_modes) != ("off", "on"):
                raise RuntimeError(
                    f"measure rung {idx} must run exactly ('off', 'on'); got {rung.memory_modes!r}"
                )
        elif seen_measure:
            raise RuntimeError(
                f"source rung {idx} appears after a measure rung — the pool must be "
                "fully sourced before any measure cell runs"
            )
    if not seen_measure:
        raise RuntimeError("scored ladder roster defines no measure rung")


def _build_plan(rungs: tuple[LadderRung, ...] | None = None) -> list[dict[str, Any]]:
    roster = tuple(rungs) if rungs is not None else backgammon_scored_ladder_roster()
    _validate_roster(roster)

    cells: list[dict[str, Any]] = []
    run_number = 1
    for rung_index, rung in enumerate(roster):
        for mode in rung.memory_modes:
            cells.append(
                {
                    "rung_index": rung_index,
                    "model": str(rung.model),
                    "role": str(rung.role),
                    "run_number": run_number,
                    "memory_mode": str(mode),
                    "phase": "all" if rung.role == "source" else "session",
                    "recorded_class": rung.recorded_class,
                }
            )
            run_number += 1
    return cells


def _build_manifest(
    cells: list[dict[str, Any]],
    rung_params: dict[str, dict[str, Any]],
    trace: str,
) -> dict[str, Any]:
    roster = backgammon_scored_ladder_roster()
    return {
        "schema_version": int(BACKGAMMON_LADDER_SCHEMA_VERSION),
        "total_cells": len(cells),
        "roster": [
            [str(r.model), str(r.role), [str(m) for m in r.memory_modes], r.recorded_class]
            for r in roster
        ],
        "config_fingerprint": backgammon_ladder_roster_fingerprint(),
        "cell_allocation": [
            {
                "rung_index": int(cell["rung_index"]),
                "model": str(cell["model"]),
                "role": str(cell["role"]),
                "run_number": int(cell["run_number"]),
                "memory_mode": str(cell["memory_mode"]),
                "phase": str(cell["phase"]),
            }
            for cell in cells
        ],
        "rung_params": rung_params,
        "created_at": _utc_iso(),
        "trace": trace,
    }


def _manifest_comparable(manifest: dict[str, Any]) -> dict[str, Any]:
    comparable = dict(manifest)
    comparable.pop("created_at", None)
    comparable.pop("trace", None)
    return comparable


def _validate_manifest_or_fail(*, existing: dict[str, Any], current: dict[str, Any]) -> None:
    existing_schema = int(existing.get("schema_version", -1))
    current_schema = int(current["schema_version"])
    if existing_schema != current_schema:
        raise RuntimeError(
            "cannot resume: this run was frozen by a different version of the ladder "
            f"(manifest schema {existing_schema} vs current {current_schema}). Start a fresh run "
            "in a new --runs-dir instead of reinterpreting it."
        )

    existing_comparable = _manifest_comparable(existing)
    current_comparable = _manifest_comparable(current)
    if (
        existing_comparable.get("config_fingerprint") != current_comparable["config_fingerprint"]
        or existing_comparable.get("roster") != current_comparable["roster"]
    ):
        raise RuntimeError(
            "cannot resume: the model roster has changed since this run was frozen. "
            "Resuming would mix results from two different rosters. Start a fresh run in a new --runs-dir."
        )
    if existing_comparable.get("cell_allocation") != current_comparable["cell_allocation"]:
        raise RuntimeError(
            "cannot resume: the cell allocation has changed since this run was frozen. "
            "Start a fresh run in a new --runs-dir."
        )
    if existing_comparable.get("rung_params") != current_comparable["rung_params"]:
        raise RuntimeError(
            "cannot resume: the rung params (pins/pricing/caps) have changed since this run was "
            "frozen. Repeats must run the same pinned provider/config (VARIANCE-POLICY.md); "
            "start a fresh run in a new --runs-dir."
        )


def _load_rung_params(path: Path, rungs: tuple[LadderRung, ...]) -> dict[str, dict[str, Any]]:
    payload = _load_json(path)
    if payload is None:
        raise RuntimeError(f"rung params file not found: {path}")

    validated: dict[str, dict[str, Any]] = {}
    for rung in rungs:
        raw = payload.get(rung.model)
        if not isinstance(raw, dict):
            raise RuntimeError(f"rung params missing object for model {rung.model!r}: {path}")
        entry: dict[str, Any] = {}
        for field in REQUIRED_RUNG_PARAM_FIELDS:
            if field not in raw:
                raise RuntimeError(f"rung params[{rung.model!r}] missing {field!r}")
            entry[field] = raw[field]
        if not str(entry["profile"]).strip():
            raise RuntimeError(f"rung params[{rung.model!r}].profile must be non-empty")
        for numeric_field in ("pricing_input", "pricing_output", "cap_usd", "cost_limit", "cost_target"):
            value = float(entry[numeric_field])
            if value < 0:
                raise RuntimeError(f"rung params[{rung.model!r}].{numeric_field} must be >= 0")
            entry[numeric_field] = value
        if float(entry["cap_usd"]) <= 0:
            raise RuntimeError(f"rung params[{rung.model!r}].cap_usd must be > 0")
        if float(entry["cost_target"]) >= float(entry["cost_limit"]):
            raise RuntimeError(
                f"rung params[{rung.model!r}] requires cost_target < cost_limit"
            )
        for field in OPTIONAL_RUNG_PARAM_FIELDS:
            if field in raw and raw[field] is not None:
                entry[field] = raw[field]
        unknown = set(raw) - set(REQUIRED_RUNG_PARAM_FIELDS) - set(OPTIONAL_RUNG_PARAM_FIELDS)
        if unknown:
            raise RuntimeError(f"rung params[{rung.model!r}] has unknown fields: {sorted(unknown)}")
        validated[rung.model] = entry
    return validated

# ---------------------------------------------------------------------------
# Checkpoint


def _checkpoint_cells(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    cells_raw = checkpoint.get("cells")
    if cells_raw is None:
        checkpoint["cells"] = []
        return checkpoint["cells"]
    if not isinstance(cells_raw, list):
        raise RuntimeError("scored-ladder checkpoint 'cells' must be an array")
    cleaned: list[dict[str, Any]] = []
    for item in cells_raw:
        if isinstance(item, dict):
            cleaned.append(item)
    checkpoint["cells"] = cleaned
    return cleaned


def _load_checkpoint(path: Path) -> dict[str, Any]:
    loaded = _load_json(path)
    if loaded is None:
        return {"cells": []}
    _checkpoint_cells(loaded)
    return loaded


def _same_entry(entry: dict[str, Any], run_number: int, rep: int) -> bool:
    return (
        int(entry.get("run_number", -1)) == int(run_number)
        and int(entry.get("rep", -1)) == int(rep)
        and str(entry.get("phase") or "") == CELL_PHASE
    )


def _find_entry(checkpoint: dict[str, Any], run_number: int, rep: int) -> dict[str, Any] | None:
    for entry in _checkpoint_cells(checkpoint):
        if _same_entry(entry, run_number, rep):
            return entry
    return None


def _upsert_entry(checkpoint: dict[str, Any], entry: dict[str, Any]) -> None:
    run_number = int(entry.get("run_number", -1))
    rep = int(entry.get("rep", -1))
    if run_number < 1 or rep < 1 or str(entry.get("phase") or "") != CELL_PHASE:
        raise RuntimeError(f"invalid scored-ladder checkpoint entry: {entry}")
    cells = _checkpoint_cells(checkpoint)
    for idx, existing in enumerate(cells):
        if _same_entry(existing, run_number, rep):
            cells[idx] = entry
            return
    cells.append(entry)


# ---------------------------------------------------------------------------
# Artifacts / stats


def _newest_artifact(ladder_runs_dir: Path, run_label: str, suffix: str, not_before: float) -> Path:
    pattern = f"*-{run_label}-{suffix}"
    candidates = [
        path
        for path in ladder_runs_dir.glob(pattern)
        if path.is_file() and path.stat().st_mtime >= (not_before - 1.0)
    ]
    if not candidates:
        raise LadderAbort(
            "artifact_missing",
            {"pattern": str(ladder_runs_dir / pattern), "not_before": not_before},
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _extract_stats(scorecard: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    scorecard_cells = scorecard.get("cells")
    detail_cells = detail.get("cells")
    if not isinstance(scorecard_cells, list) or len(scorecard_cells) != 1:
        raise LadderAbort("scorecard_shape", {"cells": scorecard_cells})
    if not isinstance(detail_cells, list) or len(detail_cells) != 1:
        raise LadderAbort("detail_shape", {"cells": detail_cells})
    sc = scorecard_cells[0]
    dc = detail_cells[0]

    max_attempts = None
    manifest = scorecard.get("manifest")
    if isinstance(manifest, dict):
        config = manifest.get("config")
        if isinstance(config, dict) and config.get("max_attempts") is not None:
            max_attempts = int(config["max_attempts"])

    failed_gates = dc.get("failed_gates")
    if not isinstance(failed_gates, list):
        failed_gates = []

    return {
        "verdict": "PASS" if bool(sc.get("resolved")) else "FAIL",
        "scored": bool(sc.get("scored")),
        "conformed": bool(dc.get("conformed")),
        "attempts_to_green": (
            int(dc["attempts_to_green"]) if dc.get("attempts_to_green") is not None else None
        ),
        "failed_gates": [str(g) for g in failed_gates],
        "max_attempts": max_attempts if max_attempts is not None else MAX_ATTEMPTS,
        "total_tokens": float(sc.get("total_tokens") or 0.0),
        "turns": float(sc.get("turns") or 0.0),
        "wall_seconds": float(sc.get("wall_seconds") or 0.0),
        "cost_usd": float(sc.get("wall_cost_usd") or 0.0),
    }


# ---------------------------------------------------------------------------
# Post-cell assertions (identity + delivery), evaluated from retained logs


def _scan_identity(proxy_log_text: str, expected_model: str) -> dict[str, Any]:
    mismatch = 'event="identity_mismatch"' in proxy_log_text
    confirmed = proxy_log_text.count(f'model="{expected_model}"')
    return {
        "expected_upstream_model": expected_model,
        "mismatch": mismatch,
        "confirmed_response_count": confirmed,
        "ok": (not mismatch) and confirmed > 0,
    }


_CLONE_ENTRY_RE = re.compile(r"op=http\.request trace=(\S+) phase=entry method=POST url=/v1/recall")
_CLONE_OUTCOME_RE = re.compile(
    r"op=http\.request trace=(\S+) phase=outcome method=POST url=/v1/recall status=(\d+)"
)
_CLONE_RESULT_COUNT_RE = re.compile(r"\[recall\] /v1/recall result_count=(\d+)")


def _scan_delivery(clone_log_slice: str, cell_log_text: str) -> dict[str, Any]:
    entries = set(_CLONE_ENTRY_RE.findall(clone_log_slice))
    outcomes_200 = {
        trace for trace, status in _CLONE_OUTCOME_RE.findall(clone_log_slice) if status == "200"
    }
    continuous = sorted(entries & outcomes_200)
    result_counts = [int(n) for n in _CLONE_RESULT_COUNT_RE.findall(clone_log_slice)]
    injected_env = "recall_env_injection=container" in cell_log_text
    ok = bool(continuous) and any(n > 0 for n in result_counts) and injected_env
    return {
        "recall_200_traces": continuous,
        "result_counts": result_counts,
        "recall_env_injection_container": injected_env,
        "ok": ok,
    }


# ---------------------------------------------------------------------------
# Variance policy triggers (docs/VARIANCE-POLICY.md) — deterministic, artifact-based


def _classify(stats: dict[str, Any]) -> str:
    """Map one cell result onto CEILING/BRACKET/FLOOR (Stage-4 practice).

    Deterministic mapping (manager-approved interpretation, vetoable):
    PASS on attempt 1 = CEILING; any other PASS, or a conformed FAIL = BRACKET;
    a non-conformed FAIL = FLOOR.
    """

    if stats["verdict"] == "PASS":
        if stats.get("attempts_to_green") == 1:
            return "CEILING"
        return "BRACKET"
    if stats.get("conformed"):
        return "BRACKET"
    return "FLOOR"


def _evaluate_triggers(
    *,
    stats: dict[str, Any],
    off_stats: dict[str, Any] | None,
    anomalies: dict[str, bool],
    recorded_class: str | None,
) -> list[str]:
    fired: list[str] = []

    # T1 — gate margin <= 1.
    attempts = stats.get("attempts_to_green")
    if stats["verdict"] == "FAIL" and len(stats.get("failed_gates") or []) == 1:
        fired.append("T1")
    elif (
        stats["verdict"] == "PASS"
        and attempts is not None
        and int(attempts) == int(stats.get("max_attempts") or MAX_ATTEMPTS)
    ):
        fired.append("T1")

    # T2 — lift sign fragile (ON cells with an OFF baseline on the same rung).
    if off_stats is not None:
        off_tokens = float(off_stats.get("total_tokens") or 0.0)
        if off_tokens > 0:
            delta = abs(float(stats.get("total_tokens") or 0.0) - off_tokens) / off_tokens
            if delta < TOKEN_DELTA_FRAGILE:
                fired.append("T2")
        if "T2" not in fired and (
            stats.get("attempts_to_green") is not None
            and off_stats.get("attempts_to_green") is not None
            and int(stats["attempts_to_green"]) == int(off_stats["attempts_to_green"])
        ):
            fired.append("T2")

    # T3 — instrument anomaly while the cell still produced a scored verdict.
    if any(anomalies.values()):
        fired.append("T3")

    # T4 — classification flip vs the rung's recorded classification.
    if recorded_class is not None and _classify(stats) != str(recorded_class):
        fired.append("T4")

    return fired


def _detect_anomalies(proxy_log_text: str, cell_log_text: str, stats: dict[str, Any]) -> dict[str, bool]:
    return {
        "proxy_402_or_429": ("status=402" in proxy_log_text) or ("status=429" in proxy_log_text),
        "resume_mid_cell": "resume-skip" in cell_log_text,
        "wall_near_timeout": float(stats.get("wall_seconds") or 0.0) >= 0.98 * RUN_TIMEOUT_S,
    }


def _majority_verdict(verdicts: list[str]) -> str:
    passes = sum(1 for v in verdicts if v == "PASS")
    return "PASS" if passes * 2 > len(verdicts) else "FAIL"


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _summarize_cell(cell: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    stats_list = [e["stats"] for e in entries if isinstance(e.get("stats"), dict)]
    verdicts = [str(s.get("verdict")) for s in stats_list]
    classes = [_classify(s) for s in stats_list]
    triggers = entries[0].get("triggers") if entries else []
    return {
        "rung_index": int(cell["rung_index"]),
        "model": str(cell["model"]),
        "role": str(cell["role"]),
        "run_number": int(cell["run_number"]),
        "memory_mode": str(cell["memory_mode"]),
        "n": len(entries),
        "triggers_fired": list(triggers or []),
        "verdicts": verdicts,
        "majority_verdict": _majority_verdict(verdicts) if verdicts else None,
        "classes": classes,
        "class_disagreement": len(set(classes)) > 1,
        "median_total_tokens": _median([float(s.get("total_tokens") or 0.0) for s in stats_list]),
        "median_turns": _median([float(s.get("turns") or 0.0) for s in stats_list]),
        "median_wall_seconds": _median([float(s.get("wall_seconds") or 0.0) for s in stats_list]),
        "median_cost_usd": _median([float(s.get("cost_usd") or 0.0) for s in stats_list]),
        "attempts_to_green": [s.get("attempts_to_green") for s in stats_list],
        "assertions": [e.get("assertions") for e in entries],
        "run_ids": [e.get("run_id") for e in entries],
        "scorecards": [e.get("scorecard") for e in entries],
    }

# ---------------------------------------------------------------------------
# Execution: ledger, proxy lifecycle, inner ladder invocation


def _run_ledger(args_list: list[str]) -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "wevibe_bench.stage_ledger", *args_list],
        cwd=str(_repo_root()),
        check=False,
    )
    return int(completed.returncode)


def _ledger_check(estimated_usd: float) -> bool:
    return _run_ledger(["check", "--stage", str(STAGE_NUMBER), "--estimated-usd", f"{estimated_usd:.6f}"]) == 0


def _ledger_record(budget_json: Path) -> bool:
    return _run_ledger(["record", "--stage", str(STAGE_NUMBER), "--budget-json", str(budget_json)]) == 0


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=1.0):
            return True
    except OSError:
        return False


def _build_proxy_cmd(
    *,
    run_id: str,
    model_slug: str,
    params: dict[str, Any],
    port: int,
    proxy_dir: Path,
    stamp: str,
) -> tuple[list[str], Path, Path, Path]:
    proxy_log = proxy_dir / f"{stamp}-{run_id}.log"
    proxy_checkpoint = proxy_dir / f"{run_id}-checkpoint.json"
    token_file = proxy_dir / f"{run_id}.token"
    cmd = [
        sys.executable,
        str(_proxy_script_path()),
        "--run-id",
        run_id,
        "--model",
        model_slug,
        "--profile",
        str(params["profile"]),
    ]
    if params.get("provider_order"):
        cmd.extend(["--provider-order", str(params["provider_order"])])
    if params.get("provider_quant"):
        cmd.extend(["--provider-quant", str(params["provider_quant"])])
    cmd.extend(
        [
            "--cap-usd",
            f"{float(params['cap_usd']):.6f}",
            "--port",
            str(port),
            "--checkpoint",
            str(proxy_checkpoint),
            "--log",
            str(proxy_log),
            "--max-output-tokens",
            str(MAX_OUTPUT_TOKENS),
            "--token-file",
            str(token_file),
            "--authorize",
            "--pricing-input",
            f"{float(params['pricing_input']):.6f}",
            "--pricing-output",
            f"{float(params['pricing_output']):.6f}",
        ]
    )
    return cmd, proxy_log, proxy_checkpoint, token_file


def _start_proxy(cmd: list[str], console_log: Path) -> subprocess.Popen[bytes]:
    console_log.parent.mkdir(parents=True, exist_ok=True)
    handle = console_log.open("ab")
    try:
        return subprocess.Popen(cmd, cwd=str(_repo_root()), stdout=handle, stderr=subprocess.STDOUT)
    finally:
        handle.close()


def _stop_proxy(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=PROXY_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=PROXY_STOP_TIMEOUT_S)


def _build_session_extra_flags(params: dict[str, Any], token_file: Path, port: int) -> str:
    flags = [
        "--max-attempts",
        str(MAX_ATTEMPTS),
        "--max-steps-per-attempt",
        str(MAX_STEPS_PER_ATTEMPT),
        "--cost-limit",
        f"{float(params['cost_limit']):.6f}",
        "--cost-target",
        f"{float(params['cost_target']):.6f}",
        "--run-timeout",
        str(RUN_TIMEOUT_S),
        "--reasoning-effort",
        REASONING_EFFORT,
        "--proxy-base-url",
        f"http://host.docker.internal:{port}/api/v1",
        "--proxy-token-file",
        str(token_file),
    ]
    output_price = params.get("output_price_per_1m")
    if output_price is not None and float(output_price) > 0:
        flags.extend(["--output-price-per-1m", f"{float(output_price):.6f}"])
    return shlex.join(flags)


def _build_inner_cmd(
    *,
    cell: dict[str, Any],
    rep: int,
    run_label: str,
    extra_session_flags: str,
    ladder_runs_dir: Path,
    org_id: str,
) -> list[str]:
    # The inner ladder checkpoint is keyed (run_number, model, phase); reps get
    # a synthetic run number so a repeat never resume-skips the original run.
    inner_run_number = int(cell["run_number"]) * 10 + int(rep)
    cmd = [
        sys.executable,
        str(_ladder_script_path()),
        "--model",
        str(cell["model"]),
        "--run-number",
        str(inner_run_number),
        "--run-label",
        run_label,
        "--phase",
        str(cell["phase"]),
        "--memory-modes",
        str(cell["memory_mode"]),
        "--max-retries",
        "1",
        "--resume",
        "--runs-dir",
        str(ladder_runs_dir),
        "--extra-session-flags",
        extra_session_flags,
    ]
    if str(cell["phase"]) == "all":
        cmd.extend(["--extract-timeout", str(EXTRACT_TIMEOUT_S), "--org-id", org_id])
    return cmd


def _run_inner_tee(cmd: list[str], cell_log: Path) -> int:
    """Run the inner ladder, teeing stdout+stderr to the cell log.

    PROGRESS / RESULT_JSON lines are also relayed to this driver's stdout so a
    single tail of the outer console streams per-unit progress (R-31).
    """

    cell_log.parent.mkdir(parents=True, exist_ok=True)
    with cell_log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_repo_root()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            fh.flush()
            if "PROGRESS" in line or "RESULT_JSON" in line:
                print(line.rstrip("\n"), flush=True)
        proc.stdout.close()
        return int(proc.wait())


def _read_text_or_empty(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _run_cell_rep(
    *,
    cell: dict[str, Any],
    rep: int,
    params: dict[str, Any],
    args: argparse.Namespace,
    trace: str,
    logfile_path: Path,
) -> dict[str, Any]:
    """Execute one physical run (rep) of one cell; returns the checkpoint entry.

    Raises LadderAbort on any condition that must stop the ladder (ledger
    refusal, proxy failure, inner failure, identity mismatch, unproven
    delivery, missing artifacts).
    """

    run_number = int(cell["run_number"])
    model = str(cell["model"])
    slug = _slugify_model(model)
    run_label = f"stage7-run{run_number}-{slug}" + ("" if rep == 1 else f"-rep{rep}")
    stamp = _utc_compact()
    run_id = f"{run_label}-{stamp}"

    runs_dir = Path(str(args.runs_dir)).expanduser().resolve()
    ladder_runs_dir = Path(str(args.ladder_runs_dir)).expanduser().resolve()
    proxy_dir = _repo_root() / "runs" / "openrouter-proxy"
    clone_log = Path(str(args.clone_log)).expanduser()

    def progress(phase: str, extra: str = "") -> None:
        line = (
            f"[{_utc_iso()}] PROGRESS trace={trace} rung={cell['rung_index']} model={model} "
            f"run={run_number} rep={rep} mode={cell['memory_mode']} phase={phase}"
        )
        if extra:
            line += f" {extra}"
        _emit(logfile_path, line)

    cap_usd = float(params["cap_usd"])
    progress("ledger-check", f"cap_usd={cap_usd:.4f}")
    if not _ledger_check(cap_usd):
        raise LadderAbort("ledger_refused", {"run_id": run_id, "cap_usd": cap_usd})

    prebudget_path = runs_dir / f"{run_id}-prebudget.json"
    _save_json_atomic(
        prebudget_path,
        {
            "schema_version": 1,
            "run_id": run_id,
            "accrued_actual_usd": 0.0,
            "committed_unproven_usd": cap_usd,
        },
    )
    if not _ledger_record(prebudget_path):
        raise LadderAbort("ledger_record_refused", {"run_id": run_id})

    clone_offset = clone_log.stat().st_size if clone_log.is_file() else 0

    model_slug_for_proxy = model.removeprefix("openrouter/")
    proxy_cmd, proxy_log, proxy_checkpoint, token_file = _build_proxy_cmd(
        run_id=run_id,
        model_slug=model_slug_for_proxy,
        params=params,
        port=int(args.proxy_port),
        proxy_dir=proxy_dir,
        stamp=stamp,
    )
    progress("proxy-start", f"port={args.proxy_port} run_id={run_id}")
    proxy_proc = _start_proxy(proxy_cmd, runs_dir / "proxy-console.log")
    try:
        deadline = time.monotonic() + PROXY_START_TIMEOUT_S
        while not _port_listening(int(args.proxy_port)):
            if proxy_proc.poll() is not None or time.monotonic() > deadline:
                raise LadderAbort(
                    "proxy_start_failed",
                    {"run_id": run_id, "exit_code": proxy_proc.poll(), "log": str(proxy_log)},
                )
            time.sleep(1.0)
        time.sleep(2.0)

        extra_session_flags = _build_session_extra_flags(params, token_file, int(args.proxy_port))
        inner_cmd = _build_inner_cmd(
            cell=cell,
            rep=rep,
            run_label=run_label,
            extra_session_flags=extra_session_flags,
            ladder_runs_dir=ladder_runs_dir,
            org_id=str(args.org_id),
        )
        cell_log = runs_dir / f"{stamp}-{run_label}-cell.log"
        progress("cell-start", f"cell_log={cell_log}")
        started = time.perf_counter()
        inner_rc = _run_inner_tee(inner_cmd, cell_log)
        dur_s = time.perf_counter() - started
    finally:
        _stop_proxy(proxy_proc)

    if proxy_checkpoint.is_file():
        if not _ledger_record(proxy_checkpoint):
            progress("ledger-post-record-refused", f"checkpoint={proxy_checkpoint}")
    else:
        progress("ledger-post-record-skipped", "proxy_checkpoint_missing=1")

    if inner_rc != 0:
        raise LadderAbort(
            "inner_failed",
            {"run_id": run_id, "exit_code": inner_rc, "cell_log": str(cell_log)},
        )

    scorecard_path = _newest_artifact(ladder_runs_dir, run_label, "scorecard.json", started_wall := (time.time() - dur_s))
    detail_path = _newest_artifact(ladder_runs_dir, run_label, "backgammon-detail.json", started_wall)
    scorecard = _load_json(scorecard_path) or {}
    detail = _load_json(detail_path) or {}
    stats = _extract_stats(scorecard, detail)

    proxy_log_text = _read_text_or_empty(proxy_log)
    cell_log_text = _read_text_or_empty(cell_log)

    assertions: dict[str, Any] = {}
    expected_upstream = params.get("expected_upstream_model")
    if expected_upstream:
        identity = _scan_identity(proxy_log_text, str(expected_upstream))
        assertions["identity"] = identity
        progress(
            "identity-check",
            f"ok={identity['ok']} mismatch={identity['mismatch']} confirmed={identity['confirmed_response_count']}",
        )
        if not identity["ok"]:
            raise LadderAbort(
                "identity_mismatch" if identity["mismatch"] else "identity_unverified",
                {"run_id": run_id, "identity": identity, "proxy_log": str(proxy_log)},
            )

    if str(cell["memory_mode"]) == "on":
        clone_slice = ""
        if clone_log.is_file():
            with clone_log.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(clone_offset)
                clone_slice = fh.read()
        delivery = _scan_delivery(clone_slice, cell_log_text)
        assertions["delivery"] = delivery
        progress(
            "delivery-check",
            f"ok={delivery['ok']} recall_200={len(delivery['recall_200_traces'])} "
            f"env_injection={delivery['recall_env_injection_container']}",
        )
        if not delivery["ok"]:
            raise LadderAbort(
                "delivery_unproven",
                {"run_id": run_id, "delivery": delivery, "clone_log": str(clone_log)},
            )

    anomalies = _detect_anomalies(proxy_log_text, cell_log_text, stats)

    accrued = None
    committed = None
    proxy_cp = _load_json(proxy_checkpoint) if proxy_checkpoint.is_file() else None
    if isinstance(proxy_cp, dict):
        accrued = proxy_cp.get("accrued_actual_usd")
        committed = proxy_cp.get("committed_unproven_usd")

    progress(
        "ok",
        f"verdict={stats['verdict']} tokens={stats['total_tokens']:.0f} "
        f"cost={float(accrued or 0.0):.4f} dur_s={dur_s:.1f}",
    )

    return {
        "rung_index": int(cell["rung_index"]),
        "model": model,
        "run_number": run_number,
        "rep": int(rep),
        "phase": CELL_PHASE,
        "memory_mode": str(cell["memory_mode"]),
        "role": str(cell["role"]),
        "status": "ok",
        "run_id": run_id,
        "run_label": run_label,
        "dur_s": round(dur_s, 3),
        "scorecard": str(scorecard_path),
        "detail": str(detail_path),
        "cell_log": str(cell_log),
        "proxy_log": str(proxy_log),
        "proxy_checkpoint": str(proxy_checkpoint),
        "accrued_usd": accrued,
        "committed_unproven_usd": committed,
        "stats": stats,
        "anomalies": anomalies,
        "assertions": assertions,
        "completed_at": _utc_iso(),
        "trace": trace,
    }

# ---------------------------------------------------------------------------
# Driver


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage-7 scored backgammon ladder driver (roster A).")
    parser.add_argument("--resume", action="store_true", help="Skip cells/reps already marked ok in the checkpoint.")
    parser.add_argument("--runs-dir", default=str(_default_runs_dir()), help="Outer driver runs directory (checkpoint/manifest/logs/summary).")
    parser.add_argument("--ladder-runs-dir", default=str(_default_ladder_runs_dir()), help="--runs-dir passed to scripts/backgammon_ladder.py (scorecards/details live here).")
    parser.add_argument("--rung-params", default=None, help="JSON file with per-model proxy/pricing/cost params (required unless --dry-run).")
    parser.add_argument("--clone-log", default=str(_repo_root() / DEFAULT_CLONE_LOG), help="Recall clone logfile for ON-cell delivery assertion.")
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID)
    parser.add_argument("--dry-run", action="store_true", help="Print the planned cells and exit 0 (no ledger/proxy/spend).")
    parser.add_argument("--start-cell", type=int, default=None, help="Optional run number to force start at.")
    return parser


def _write_escalation(
    runs_dir: Path,
    logfile_path: Path,
    *,
    trace: str,
    cell: dict[str, Any] | None,
    rep: int | None,
    abort: LadderAbort,
    completed_cells: int,
) -> Path:
    payload: dict[str, Any] = {
        "status": "aborted",
        "reason": abort.reason,
        "detail": abort.detail,
        "ts": _utc_iso(),
        "trace": trace,
        "completed_cell_reps": completed_cells,
    }
    if cell is not None:
        payload["failed"] = {
            "rung_index": int(cell["rung_index"]),
            "model": str(cell["model"]),
            "run_number": int(cell["run_number"]),
            "memory_mode": str(cell["memory_mode"]),
            "rep": rep,
        }
    escalate_path = runs_dir / ESCALATE_NAME
    _save_json_atomic(escalate_path, payload)
    _emit(
        logfile_path,
        f"[{_utc_iso()}] SUMMARY trace={trace} status=fail reason={abort.reason} escalate={escalate_path}",
    )
    return escalate_path


def _completed_ok(checkpoint: dict[str, Any]) -> int:
    return sum(
        1
        for entry in _checkpoint_cells(checkpoint)
        if str(entry.get("status") or "") == "ok" and str(entry.get("phase") or "") == CELL_PHASE
    )


def _emit_summary(
    runs_dir: Path,
    plan: list[dict[str, Any]],
    checkpoint: dict[str, Any],
    trace: str,
) -> Path:
    cells_summary: list[dict[str, Any]] = []
    for cell in plan:
        entries = [
            entry
            for entry in _checkpoint_cells(checkpoint)
            if int(entry.get("run_number", -1)) == int(cell["run_number"])
            and str(entry.get("status") or "") == "ok"
        ]
        entries.sort(key=lambda e: int(e.get("rep", 0)))
        if entries:
            cells_summary.append(_summarize_cell(cell, entries))
    summary = {
        "schema_version": int(BACKGAMMON_LADDER_SCHEMA_VERSION),
        "variance_policy": "docs/VARIANCE-POLICY.md (N=1 baseline; borderline T1-T4 -> N=3 majority/median)",
        "cells": cells_summary,
        "generated_at": _utc_iso(),
        "trace": trace,
    }
    summary_path = runs_dir / SUMMARY_NAME
    _save_json_atomic(summary_path, summary)
    return summary_path


def _off_stats_for_rung(plan: list[dict[str, Any]], checkpoint: dict[str, Any], rung_index: int) -> dict[str, Any] | None:
    """Median-composed OFF stats for a rung (T2 baseline), or None if absent."""

    off_cells = [
        c for c in plan if int(c["rung_index"]) == rung_index and str(c["memory_mode"]) == "off"
    ]
    if not off_cells:
        return None
    run_number = int(off_cells[0]["run_number"])
    entries = [
        e
        for e in _checkpoint_cells(checkpoint)
        if int(e.get("run_number", -1)) == run_number and str(e.get("status") or "") == "ok"
    ]
    stats_list = [e["stats"] for e in entries if isinstance(e.get("stats"), dict)]
    if not stats_list:
        return None
    attempts = [s.get("attempts_to_green") for s in stats_list if s.get("attempts_to_green") is not None]
    return {
        "total_tokens": _median([float(s.get("total_tokens") or 0.0) for s in stats_list]),
        "attempts_to_green": int(_median([float(a) for a in attempts])) if attempts else None,
    }


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    if args.start_cell is not None and int(args.start_cell) < 1:
        parser.error("--start-cell must be >= 1")

    plan = _build_plan()
    trace = _new_trace_id()

    runs_dir = Path(str(args.runs_dir)).expanduser().resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    logfile_path = runs_dir / f"{_utc_compact()}.log"
    print(str(logfile_path), flush=True)

    selected = [
        cell
        for cell in plan
        if args.start_cell is None or int(cell["run_number"]) >= int(args.start_cell)
    ]
    if not selected:
        raise RuntimeError("no cells selected to run")

    plan_line = (
        f"[{_utc_iso()}] PLAN trace={trace} total_cells={len(plan)} selected_cells={len(selected)} "
        f"cells={json.dumps([{k: cell[k] for k in ('rung_index', 'model', 'role', 'run_number', 'memory_mode', 'phase')} for cell in plan], separators=(',', ':'))}"
    )

    if args.dry_run:
        _append_log_line(logfile_path, plan_line)
        for cell in selected:
            print(json.dumps(cell, sort_keys=True), flush=True)
        _append_log_line(
            logfile_path,
            f"[{_utc_iso()}] SUMMARY trace={trace} status=dry-run planned_cells={len(selected)}",
        )
        return 0

    if not args.rung_params:
        parser.error("--rung-params is required for a real run")
    rung_params = _load_rung_params(Path(str(args.rung_params)).expanduser(), backgammon_scored_ladder_roster())

    _emit(logfile_path, plan_line)

    checkpoint_path = runs_dir / CHECKPOINT_NAME
    checkpoint = _load_checkpoint(checkpoint_path)
    manifest_path = runs_dir / MANIFEST_NAME
    current_manifest = _build_manifest(plan, rung_params, trace)
    existing_manifest = _load_json(manifest_path)
    if existing_manifest is not None:
        _validate_manifest_or_fail(existing=existing_manifest, current=current_manifest)
    else:
        if _checkpoint_cells(checkpoint):
            raise RuntimeError(
                f"cannot resume: found an existing checkpoint in {runs_dir} but no run manifest "
                f"({MANIFEST_NAME}). Start a fresh run in a new --runs-dir."
            )
        _save_json_atomic(manifest_path, current_manifest)
    _emit(
        logfile_path,
        f"[{_utc_iso()}] MANIFEST trace={trace} schema={current_manifest['schema_version']} "
        f"fingerprint={current_manifest['config_fingerprint']} total_cells={current_manifest['total_cells']} "
        f"path={manifest_path}",
    )

    # Plan-level budget projection: refuse to START if the remaining base cells'
    # caps cannot fit under the stage/global caps (stop BEFORE overrunning).
    projected = 0.0
    for cell in selected:
        prior = _find_entry(checkpoint, int(cell["run_number"]), 1)
        if args.resume and prior is not None and str(prior.get("status") or "") == "ok":
            continue
        projected += float(rung_params[str(cell["model"])]["cap_usd"])
    _emit(logfile_path, f"[{_utc_iso()}] BUDGET trace={trace} projected_caps_usd={projected:.4f}")
    if projected > 0 and not _ledger_check(projected):
        abort = LadderAbort("plan_budget_refused", {"projected_caps_usd": projected})
        _write_escalation(
            runs_dir, logfile_path, trace=trace, cell=None, rep=None, abort=abort,
            completed_cells=_completed_ok(checkpoint),
        )
        return 3

    executed = 0
    skipped = 0
    try:
        for cell in selected:
            model = str(cell["model"])
            params = rung_params[model]
            run_number = int(cell["run_number"])

            # rep 1 (the N=1 baseline).
            rep1 = _find_entry(checkpoint, run_number, 1)
            if args.resume and rep1 is not None and str(rep1.get("status") or "") == "ok":
                skipped += 1
                _emit(
                    logfile_path,
                    f"[{_utc_iso()}] PROGRESS trace={trace} rung={cell['rung_index']} model={model} "
                    f"run={run_number} rep=1 mode={cell['memory_mode']} phase=ok resume_skip=1",
                )
            else:
                entry = _run_cell_rep(
                    cell=cell, rep=1, params=params, args=args, trace=trace, logfile_path=logfile_path,
                )
                executed += 1
                rep1 = entry

                # Variance triggers: evaluated ONCE, immediately after the N=1 run.
                triggers: list[str] = []
                if str(cell["role"]) == "measure":
                    off_stats = (
                        _off_stats_for_rung(plan, checkpoint, int(cell["rung_index"]))
                        if str(cell["memory_mode"]) == "on"
                        else None
                    )
                    triggers = _evaluate_triggers(
                        stats=entry["stats"],
                        off_stats=off_stats,
                        anomalies=entry["anomalies"],
                        recorded_class=cell.get("recorded_class"),
                    )
                entry["triggers"] = triggers
                _upsert_entry(checkpoint, entry)
                _save_json_atomic(checkpoint_path, checkpoint)
                if triggers:
                    _emit(
                        logfile_path,
                        f"[{_utc_iso()}] VARIANCE trace={trace} run={run_number} borderline=1 "
                        f"triggers={','.join(triggers)} repeat_to_n={MAX_REPS}",
                    )

            reps_expected = MAX_REPS if (rep1.get("triggers") or []) else 1
            for rep in range(2, reps_expected + 1):
                prior = _find_entry(checkpoint, run_number, rep)
                if args.resume and prior is not None and str(prior.get("status") or "") == "ok":
                    skipped += 1
                    continue
                entry = _run_cell_rep(
                    cell=cell, rep=rep, params=params, args=args, trace=trace, logfile_path=logfile_path,
                )
                executed += 1
                _upsert_entry(checkpoint, entry)
                _save_json_atomic(checkpoint_path, checkpoint)

            summary_path = _emit_summary(runs_dir, plan, checkpoint, trace)
            _emit(
                logfile_path,
                f"[{_utc_iso()}] CELL-DONE trace={trace} run={run_number} n={reps_expected} summary={summary_path}",
            )
    except LadderAbort as abort:
        _write_escalation(
            runs_dir,
            logfile_path,
            trace=trace,
            cell=cell,
            rep=None,
            abort=abort,
            completed_cells=_completed_ok(checkpoint),
        )
        _emit_summary(runs_dir, plan, checkpoint, trace)
        return 3

    summary_path = _emit_summary(runs_dir, plan, checkpoint, trace)
    _emit(
        logfile_path,
        f"[{_utc_iso()}] SUMMARY trace={trace} status=ok total_cells={len(plan)} executed_reps={executed} "
        f"skipped_reps={skipped} completed_reps={_completed_ok(checkpoint)} summary={summary_path} "
        f"checkpoint={checkpoint_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
