"""Stage-8 scored backgammon ladder driver (roster A).

Runs the scored OFF/ON ladder by invoking scripts/backgammon_ladder.py once per
cell in strict roster order, deriving the roster from wevibe_bench/config.py
(``backgammon_scored_ladder_roster()``). The SOURCE rung (GLM-5.2) runs session +
self-extraction to populate the org pool; MEASURE rungs run scored OFF/ON
session-only cells consuming that pool (no extraction below the source, so the
pool is frozen for repeats).

Per cell this driver owns:
- stage-ledger admission (stage 8) + prebudget/post-run recording,
- per-cell OpenRouter proxy lifecycle (pinned profile, live pricing, hard cap),
- upstream-identity assertion (expected_upstream_model rungs, e.g. tencent/hy3),
- delivery log-assertion for memory-ON cells (clone /v1/recall 200s +
  recall_env_injection=container),
- variance policy N-logic (docs/VARIANCE-POLICY.md): N=1 baseline, borderline
  triggers T1-T4 repeat the cell to N=3, majority/median summary, N disclosed
  per cell in scored-ladder-summary.json,
- checkpoint + --resume per cell/rep (never restart from zero).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from wevibe_bench.adapters.backgammon import (
    DEFAULT_ATTEMPT_HARD_CEILING,
    fetch_orcarouter_billing_usage_cents,
    reconcile_derived_vs_billing,
)
from wevibe_bench.adapters.openrouter_proxy import (
    DEFAULT_OPENCODE_AUTH_PATH,
    DEFAULT_PROFILES,
    key_fingerprint,
    load_upstream_key,
)
from wevibe_bench.config import (
    BACKGAMMON_LADDER_SCHEMA_VERSION,
    LadderRung,
    backgammon_ladder_roster_fingerprint,
    backgammon_scored_ladder_roster,
)
from wevibe_bench.lifecycle import qdrant_probe


STAGE_NUMBER = 8
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
BINDING_BUDGET_METER = "proxy_budget_ledger.hard_cap_usd"
PROXY_CHECKPOINT_ENV = "WEVIBE_BENCH_PROXY_CHECKPOINT"

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

IMPORT_SCHEMA_VERSION = 1
QDRANT_DEFAULT_URL = "http://127.0.0.1:6333"
_IMPORT_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IMPORT_RUN_ID_SUFFIX_RE = re.compile(r"^\d{8}T\d{6}Z$")
_IMPORT_REQUIRED_FIELDS = {
    "schema_version",
    "run_number",
    "run_id",
    "scorecard_path",
    "detail_path",
    "cell_log_path",
    "proxy_log_path",
    "proxy_checkpoint_path",
    "scorecard_sha256",
    "detail_sha256",
    "memory",
    "accrued_usd",
    "committed_unproven_usd",
    "note",
}
_IMPORT_MEMORY_REQUIRED_FIELDS = {
    "org_id",
    "submission_hash",
    "memory_fp",
    "approve_status",
    "delivery",
}


class LadderAbort(RuntimeError):
    """Raised to abort the ladder with a structured escalation payload."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


class CellRunUnexpectedError(RuntimeError):
    """Raised when a per-cell run crashes unexpectedly after starting execution."""

    def __init__(self, message: str, entry: dict[str, Any]) -> None:
        super().__init__(message)
        self.entry = dict(entry)


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
    return f"stage{STAGE_NUMBER}-ladder-{_utc_compact()}-{uuid4().hex[:10]}"


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


def _parse_only_reps(raw: str, *, valid_runs: set[int]) -> dict[int, list[int]]:
    tokens = [token.strip() for token in str(raw).split(",")]
    if not tokens or any(not token for token in tokens):
        raise RuntimeError("--only-reps token is malformed (expected RUN:REP)")

    selected: dict[int, set[int]] = {}
    for token in tokens:
        parts = token.split(":")
        if len(parts) != 2:
            raise RuntimeError(f"--only-reps token is malformed: {token!r} (expected RUN:REP)")
        run_text, rep_text = parts
        try:
            run_number = int(run_text)
            rep = int(rep_text)
        except ValueError as exc:
            raise RuntimeError(
                f"--only-reps token is malformed: {token!r} (expected RUN:REP)"
            ) from exc

        if run_number not in valid_runs:
            raise RuntimeError(
                f"--only-reps run_number {run_number} is not in the stage-{STAGE_NUMBER} plan"
            )
        if rep < 1:
            raise RuntimeError("--only-reps rep must be >= 1")
        if rep > MAX_REPS:
            raise RuntimeError(f"--only-reps rep must be <= {MAX_REPS}")

        selected.setdefault(run_number, set()).add(rep)

    return {
        run_number: sorted(reps)
        for run_number, reps in sorted(selected.items(), key=lambda item: item[0])
    }


def _parse_run_number_csv(raw: str, *, flag: str, valid_runs: set[int]) -> list[int]:
    tokens = [token.strip() for token in str(raw).split(",")]
    if not tokens or any(not token for token in tokens):
        raise RuntimeError(f"{flag} token is malformed (expected run number)")

    run_numbers: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        try:
            run_number = int(token)
        except ValueError as exc:
            raise RuntimeError(f"{flag} token is malformed: {token!r} (expected run number)") from exc
        if run_number not in valid_runs:
            raise RuntimeError(f"{flag} run_number {run_number} is not in the stage-{STAGE_NUMBER} plan")
        if run_number not in seen:
            run_numbers.append(run_number)
            seen.add(run_number)
    return sorted(run_numbers)


def _build_manifest(
    cells: list[dict[str, Any]],
    rung_params: dict[str, dict[str, Any]],
    trace: str,
    extra_disclosures: list[str] | None = None,
) -> dict[str, Any]:
    roster = backgammon_scored_ladder_roster()
    disclosures = [
        "2026-07-22: twin-aware delivery probe — harness measurement fix (suppressed-as-twin-of-returned counts delivered, evidence recorded in scorecard); protocol semantics unchanged; disclosed per pre-registration integrity like the 22-07 smoke defect fixes"
    ]
    if extra_disclosures:
        disclosures.extend(str(disclosure) for disclosure in extra_disclosures)

    manifest = {
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
    manifest["preregistration"] = {
        "roster": "GLM-5.2 SOURCE OFF (+ self-extraction) -> kimi-k2.7-code MEASURE OFF/ON -> hy3 MEASURE OFF/ON; OrcaRouter upstream via host proxy profiles (glm, kimicode, hy3); memories flow down",
        "task": "locked backgammon prompt/CONTRACT/oracle",
        "feedback": "problems-only",
        "disclosures": disclosures,
        "attempts": {
            "policy": "budget-bounded",
            "ceiling": int(DEFAULT_ATTEMPT_HARD_CEILING),
        },
        "termination_labels": ["gates_green", "attempt_ceiling_reached", "BUDGET_STOP", "harness_error"],
        "variance_policy": "N=1 baseline; borderline -> N=3; N disclosed per cell (docs/VARIANCE-POLICY.md)",
        "headline_metrics": ["attempts-to-green", "total tokens", "gate trajectory"],
        "budget_meter": BINDING_BUDGET_METER,
        "llm_judge": "none",
        "baseline": "fresh; stage-7 cells are historical evidence only, never merged",
    }
    return manifest


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


def _reconciled_cost_limit_usd(params: dict[str, Any]) -> tuple[float, float | None]:
    """Return (binding hard cap, mismatched rung cost_limit if reconciliation happened)."""

    hard_cap_usd = float(params["cap_usd"])
    rung_cost_limit_usd = float(params["cost_limit"])
    if abs(rung_cost_limit_usd - hard_cap_usd) <= 1e-9:
        return hard_cap_usd, None
    return hard_cap_usd, rung_cost_limit_usd


def _dry_run_cell_payload(
    cell: dict[str, Any],
    rung_params: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    payload = dict(cell)
    payload["binding_budget_meter"] = BINDING_BUDGET_METER
    binding_budget_usd = None
    if rung_params is not None:
        params = rung_params.get(str(cell["model"]))
        if isinstance(params, dict) and params.get("cap_usd") is not None:
            binding_budget_usd = float(params["cap_usd"])
    payload["binding_budget_usd"] = binding_budget_usd
    return payload

# ---------------------------------------------------------------------------
# Checkpoint


# Entry schema is additive: new entries may include
# stats.verdict == "BUDGET_STOP" and stats.termination_reason while historical
# entries without these fields remain loadable/resumable.


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_import_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = _repo_root() / path
    return path.resolve()


def _require_object(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return value


def _require_exact_fields(name: str, payload: dict[str, Any], required: set[str]) -> None:
    keys = set(payload)
    missing = sorted(required - keys)
    unknown = sorted(keys - required)
    if missing or unknown:
        bits: list[str] = []
        if missing:
            bits.append(f"missing={missing}")
        if unknown:
            bits.append(f"unknown={unknown}")
        raise RuntimeError(f"{name} has invalid fields ({'; '.join(bits)})")


def _require_nonempty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{name} must be a non-empty string")
    return value


def _require_number(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"{name} must be numeric")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be numeric") from exc


def _require_digest(name: str, value: Any) -> str:
    raw = _require_nonempty_string(name, value).strip()
    if not _IMPORT_DIGEST_RE.fullmatch(raw):
        raise RuntimeError(f"{name} must be 64 hex characters")
    return raw.lower()


def _load_import_cell_payload(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if payload is None:
        raise RuntimeError(f"import cell file not found: {path}")
    _require_exact_fields("import-cell", payload, _IMPORT_REQUIRED_FIELDS)

    schema_version_raw = payload.get("schema_version")
    if isinstance(schema_version_raw, bool):
        raise RuntimeError("import-cell schema_version must be integer 1")
    try:
        schema_version = int(schema_version_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("import-cell schema_version must be integer 1") from exc
    if schema_version != IMPORT_SCHEMA_VERSION:
        raise RuntimeError(
            f"import-cell schema_version must be {IMPORT_SCHEMA_VERSION}; got {schema_version}"
        )

    run_number_raw = payload.get("run_number")
    if isinstance(run_number_raw, bool):
        raise RuntimeError("import-cell run_number must be an integer >= 1")
    try:
        run_number = int(run_number_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("import-cell run_number must be an integer >= 1") from exc
    if run_number < 1:
        raise RuntimeError("import-cell run_number must be an integer >= 1")

    memory = _require_object("import-cell.memory", payload.get("memory"))
    _require_exact_fields("import-cell.memory", memory, _IMPORT_MEMORY_REQUIRED_FIELDS)

    normalized = dict(payload)
    normalized["schema_version"] = schema_version
    normalized["run_number"] = run_number
    normalized["run_id"] = _require_nonempty_string("import-cell.run_id", payload.get("run_id")).strip()
    normalized["scorecard_path"] = _require_nonempty_string(
        "import-cell.scorecard_path", payload.get("scorecard_path")
    )
    normalized["detail_path"] = _require_nonempty_string(
        "import-cell.detail_path", payload.get("detail_path")
    )
    normalized["cell_log_path"] = _require_nonempty_string(
        "import-cell.cell_log_path", payload.get("cell_log_path")
    )
    normalized["proxy_log_path"] = _require_nonempty_string(
        "import-cell.proxy_log_path", payload.get("proxy_log_path")
    )
    normalized["proxy_checkpoint_path"] = _require_nonempty_string(
        "import-cell.proxy_checkpoint_path", payload.get("proxy_checkpoint_path")
    )
    normalized["scorecard_sha256"] = _require_digest(
        "import-cell.scorecard_sha256", payload.get("scorecard_sha256")
    )
    normalized["detail_sha256"] = _require_digest(
        "import-cell.detail_sha256", payload.get("detail_sha256")
    )
    normalized["accrued_usd"] = _require_number("import-cell.accrued_usd", payload.get("accrued_usd"))
    normalized["committed_unproven_usd"] = _require_number(
        "import-cell.committed_unproven_usd", payload.get("committed_unproven_usd")
    )
    normalized["note"] = _require_nonempty_string("import-cell.note", payload.get("note"))

    normalized_memory = dict(memory)
    normalized_memory["org_id"] = _require_nonempty_string(
        "import-cell.memory.org_id", memory.get("org_id")
    ).strip()
    normalized_memory["submission_hash"] = _require_digest(
        "import-cell.memory.submission_hash", memory.get("submission_hash")
    )
    normalized_memory["memory_fp"] = _require_nonempty_string(
        "import-cell.memory.memory_fp", memory.get("memory_fp")
    ).strip()
    normalized_memory["approve_status"] = _require_nonempty_string(
        "import-cell.memory.approve_status", memory.get("approve_status")
    ).strip()
    normalized_memory["delivery"] = _require_nonempty_string(
        "import-cell.memory.delivery", memory.get("delivery")
    ).strip()
    normalized["memory"] = normalized_memory
    return normalized


def _probe_pool_memory(org_id: str, submission_hash: str) -> dict[str, Any]:
    collection = f"org_{org_id}_memories"
    qdrant_url = (
        os.environ.get("WEVIBE_BENCH_QDRANT_URL", QDRANT_DEFAULT_URL).strip() or QDRANT_DEFAULT_URL
    )
    encoded_collection = urllib.parse.quote(collection, safe="")
    url = f"{qdrant_url.rstrip('/')}/collections/{encoded_collection}/points/scroll"
    body = {
        "filter": {"must": [{"key": "cid", "match": {"value": submission_hash}}]},
        "limit": 1,
        "with_payload": True,
        "with_vectors": False,
    }
    request = urllib.request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    api_key = qdrant_probe._qdrant_api_key()
    if api_key:
        request.add_header("api-key", api_key)

    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            status = int(response.getcode() or 0)
            payload_bytes = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        raise RuntimeError(
            f"qdrant pool probe failed status={exc.code} collection={collection} detail={detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"qdrant pool probe unreachable for collection={collection} url={url}"
        ) from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"qdrant pool probe returned invalid JSON for collection={collection}") from exc

    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(
            f"qdrant pool probe bad response status={status} collection={collection} payload={payload}"
        )

    result = payload.get("result") if isinstance(payload, dict) else None
    points = result.get("points") if isinstance(result, dict) else None
    if not isinstance(points, list):
        raise RuntimeError(
            f"qdrant pool probe missing points array collection={collection} payload={payload}"
        )
    if not points:
        return {"collection": collection, "found": False, "cid": None}

    point = points[0] if isinstance(points[0], dict) else {}
    point_payload = point.get("payload") if isinstance(point, dict) else None
    cid = point_payload.get("cid") if isinstance(point_payload, dict) else None
    return {
        "collection": collection,
        "found": True,
        "cid": str(cid) if cid is not None else None,
    }


def _build_import_entry(
    *,
    import_source: Path,
    checkpoint: dict[str, Any],
    plan: list[dict[str, Any]],
    manifest: dict[str, Any],
    dry_run: bool,
    trace: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _load_import_cell_payload(import_source)
    run_number = int(payload["run_number"])
    run_id = str(payload["run_id"])

    plan_cell = next((c for c in plan if int(c["run_number"]) == run_number), None)
    if plan_cell is None:
        raise RuntimeError(f"import-cell run_number {run_number} is not in the current ladder plan")

    for existing in _checkpoint_cells(checkpoint):
        if int(existing.get("run_number", -1)) == run_number:
            raise RuntimeError(f"import-cell refused: checkpoint already has run_number={run_number}")

    allocation = manifest.get("cell_allocation")
    if not isinstance(allocation, list):
        raise RuntimeError("frozen manifest missing cell_allocation for import validation")
    manifest_cell = next(
        (
            cell
            for cell in allocation
            if isinstance(cell, dict) and int(cell.get("run_number", -1)) == run_number
        ),
        None,
    )
    if manifest_cell is None:
        raise RuntimeError(
            f"import-cell run_number {run_number} not found in frozen manifest cell_allocation"
        )

    for key in ("model", "memory_mode", "phase", "role", "rung_index"):
        if str(manifest_cell.get(key)) != str(plan_cell.get(key)):
            raise RuntimeError(
                "frozen manifest cell allocation drifted from plan for run_number "
                f"{run_number} on field {key!r}"
            )

    expected_model = str(manifest_cell["model"])
    expected_memory_mode = str(manifest_cell["memory_mode"])
    expected_phase = str(manifest_cell["phase"])
    expected_role = str(manifest_cell["role"])
    expected_rung_index = int(manifest_cell["rung_index"])
    expected_run_label = f"stage{STAGE_NUMBER}-run{run_number}-{_slugify_model(expected_model)}"
    expected_prefix = f"{expected_run_label}-"
    if not run_id.startswith(expected_prefix):
        raise RuntimeError(
            f"import-cell run_id mismatch: expected prefix {expected_prefix!r}, got {run_id!r}"
        )
    suffix = run_id[len(expected_prefix) :]
    if not _IMPORT_RUN_ID_SUFFIX_RE.fullmatch(suffix):
        raise RuntimeError(
            f"import-cell run_id timestamp suffix must be YYYYMMDDTHHMMSSZ; got {suffix!r}"
        )

    scorecard_path = _resolve_import_path(str(payload["scorecard_path"]))
    detail_path = _resolve_import_path(str(payload["detail_path"]))
    cell_log_path = _resolve_import_path(str(payload["cell_log_path"]))
    proxy_log_path = _resolve_import_path(str(payload["proxy_log_path"]))
    proxy_checkpoint_path = _resolve_import_path(str(payload["proxy_checkpoint_path"]))
    for name, artifact_path in (
        ("scorecard_path", scorecard_path),
        ("detail_path", detail_path),
        ("cell_log_path", cell_log_path),
        ("proxy_log_path", proxy_log_path),
        ("proxy_checkpoint_path", proxy_checkpoint_path),
    ):
        if not artifact_path.is_file():
            raise RuntimeError(f"import-cell {name} is not a file: {artifact_path}")

    scorecard_digest = _sha256_file(scorecard_path)
    detail_digest = _sha256_file(detail_path)
    if scorecard_digest != str(payload["scorecard_sha256"]):
        raise RuntimeError(
            "import-cell scorecard digest mismatch: "
            f"expected {payload['scorecard_sha256']}, got {scorecard_digest}"
        )
    if detail_digest != str(payload["detail_sha256"]):
        raise RuntimeError(
            f"import-cell detail digest mismatch: expected {payload['detail_sha256']}, got {detail_digest}"
        )

    scorecard = _load_json(scorecard_path)
    detail = _load_json(detail_path)
    if scorecard is None or detail is None:
        raise RuntimeError("import-cell scorecard/detail artifacts could not be loaded")

    scorecard_cells = scorecard.get("cells")
    detail_cells = detail.get("cells")
    if not isinstance(scorecard_cells, list) or len(scorecard_cells) != 1:
        raise RuntimeError("import-cell scorecard must contain exactly one cell")
    if not isinstance(detail_cells, list) or len(detail_cells) != 1:
        raise RuntimeError("import-cell detail must contain exactly one cell")
    scorecard_cell = _require_object("import-cell.scorecard.cells[0]", scorecard_cells[0])
    detail_cell = _require_object("import-cell.detail.cells[0]", detail_cells[0])

    scorecard_model = str(scorecard_cell.get("model") or "")
    if scorecard_model != expected_model:
        raise RuntimeError(
            f"import-cell scorecard model mismatch: expected {expected_model!r}, got {scorecard_model!r}"
        )

    condition = str(scorecard_cell.get("condition") or "").strip().lower()
    condition_map = {"off": "off", "on": "on"}
    scorecard_memory_mode = condition_map.get(condition)
    if scorecard_memory_mode != expected_memory_mode:
        raise RuntimeError(
            "import-cell memory mode mismatch: "
            f"expected {expected_memory_mode!r}, scorecard condition={condition!r}"
        )

    detail_memory_mode = str(detail_cell.get("memory_mode") or "").strip().lower()
    if detail_memory_mode != expected_memory_mode:
        raise RuntimeError(
            "import-cell detail memory_mode mismatch: "
            f"expected {expected_memory_mode!r}, got {detail_memory_mode!r}"
        )

    scorecard_manifest = _require_object("import-cell.scorecard.manifest", scorecard.get("manifest"))
    scorecard_config = _require_object("import-cell.scorecard.manifest.config", scorecard_manifest.get("config"))
    config_run_label = str(scorecard_config.get("run_label") or "")
    if config_run_label != expected_run_label:
        raise RuntimeError(
            "import-cell scorecard run_label mismatch: "
            f"expected {expected_run_label!r}, got {config_run_label!r}"
        )

    schedule_data = scorecard_config.get("schedule")
    if not isinstance(schedule_data, dict) or str(expected_model) not in schedule_data.get("all_models", []):
        raise RuntimeError(
            "import-cell scorecard manifest.config.schedule mismatch: "
            f"expected model {expected_model!r} in schedule.all_models(), got {schedule_data}"
        )

    stats = _extract_stats(scorecard, detail)
    try:
        stats["cost_usd"] = float(payload["accrued_usd"])
    except (TypeError, ValueError):
        pass

    proxy_log_text = _read_text_or_empty(proxy_log_path)
    cell_log_text = _read_text_or_empty(cell_log_path)

    assertions: dict[str, Any] = {}
    rung_params = manifest.get("rung_params") if isinstance(manifest.get("rung_params"), dict) else {}
    expected_upstream = None
    if isinstance(rung_params, dict):
        model_params = rung_params.get(expected_model)
        if isinstance(model_params, dict) and model_params.get("expected_upstream_model"):
            expected_upstream = str(model_params["expected_upstream_model"])
    if expected_upstream:
        assertions["identity"] = _scan_identity(proxy_log_text, expected_upstream)
    if expected_memory_mode == "on":
        assertions["delivery"] = _scan_delivery(proxy_log_text, cell_log_text)

    anomalies = _detect_anomalies(proxy_log_text, cell_log_text, stats)

    memory = _require_object("import-cell.memory", payload.get("memory"))
    org_id = str(memory["org_id"])
    submission_hash = str(memory["submission_hash"])
    probe_mode = "live"
    probe_result: dict[str, Any]
    if dry_run:
        probe_mode = "skipped_dry_run"
        probe_result = {
            "collection": f"org_{org_id}_memories",
            "found": None,
            "cid": None,
        }
    else:
        probe_result = _probe_pool_memory(org_id, submission_hash)
        if not probe_result.get("found"):
            raise RuntimeError(
                "import-cell pool probe missing preserved memory: "
                f"collection={probe_result.get('collection')} submission_hash={submission_hash}"
            )
        observed_cid = str(probe_result.get("cid") or "")
        if observed_cid != submission_hash:
            raise RuntimeError(
                "import-cell pool probe cid mismatch: "
                f"expected {submission_hash}, got {observed_cid}"
            )

    entry = {
        "rung_index": expected_rung_index,
        "model": expected_model,
        "run_number": run_number,
        "rep": 1,
        "phase": CELL_PHASE,
        "memory_mode": expected_memory_mode,
        "role": expected_role,
        "status": "ok",
        "run_id": run_id,
        "run_label": expected_run_label,
        "dur_s": round(float(stats.get("wall_seconds") or 0.0), 3),
        "scorecard": str(scorecard_path),
        "detail": str(detail_path),
        "cell_log": str(cell_log_path),
        "proxy_log": str(proxy_log_path),
        "proxy_checkpoint": str(proxy_checkpoint_path),
        "accrued_usd": payload["accrued_usd"],
        "committed_unproven_usd": payload["committed_unproven_usd"],
        "stats": stats,
        "anomalies": anomalies,
        "assertions": assertions,
        "completed_at": _utc_iso(),
        "trace": trace,
        "imported": True,
        "import_source": str(import_source),
        "import_digests": {
            "scorecard_sha256": scorecard_digest,
            "detail_sha256": detail_digest,
        },
        "import_note": str(payload["note"]),
    }
    context = {
        "run_number": run_number,
        "run_id": run_id,
        "phase": expected_phase,
        "probe_mode": probe_mode,
        "probe_collection": str(probe_result.get("collection") or ""),
        "probe_found": probe_result.get("found"),
        "scorecard": str(scorecard_path),
        "detail": str(detail_path),
        "proxy_log": str(proxy_log_path),
    }
    return entry, context


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


def _attempts_numeric(value: Any) -> int | None:
    """Return integer attempts when representable; sentinels/non-numeric map to None."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            try:
                numeric = float(stripped)
            except ValueError:
                return None
            return int(numeric) if numeric.is_integer() else None
    return None


def _normalize_verdict(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    verdict = value.strip().upper()
    if verdict in {"PASS", "FAIL", "BUDGET_STOP"}:
        return verdict
    return None


def _extract_termination_reason(scorecard_cell: dict[str, Any], detail_cell: dict[str, Any]) -> str | None:
    from_scorecard = scorecard_cell.get("termination_reason")
    if isinstance(from_scorecard, str) and from_scorecard.strip():
        return from_scorecard.strip()

    from_detail = detail_cell.get("termination_reason")
    if isinstance(from_detail, str) and from_detail.strip():
        return from_detail.strip()

    reports = detail_cell.get("attempt_reports")
    if isinstance(reports, list):
        for report in reversed(reports):
            if isinstance(report, dict):
                candidate = report.get("termination_reason")
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
    return None


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

    attempts_to_green = dc.get("attempts_to_green")
    verdict = _normalize_verdict(sc.get("verdict"))
    if verdict is None:
        verdict = _normalize_verdict(dc.get("verdict"))
    if verdict is None and isinstance(attempts_to_green, str) and attempts_to_green.strip().upper() == "BUDGET_STOP":
        verdict = "BUDGET_STOP"
    if verdict is None:
        verdict = "PASS" if bool(sc.get("resolved")) else "FAIL"

    return {
        "verdict": verdict,
        "termination_reason": _extract_termination_reason(sc, dc),
        "scored": bool(sc.get("scored")),
        "conformed": bool(dc.get("conformed")),
        "attempts_to_green": attempts_to_green,
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
_SXE_RESULT_PREFIX = "BACKGAMMON_SXE_RESULT_JSON "


def _extract_sxe_delivery_proof(cell_log_text: str) -> dict[str, Any] | None:
    for raw_line in reversed(cell_log_text.splitlines()):
        line = raw_line.strip()
        if not line.startswith(_SXE_RESULT_PREFIX):
            continue
        payload_raw = line[len(_SXE_RESULT_PREFIX) :].strip()
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        delivery_proof = payload.get("delivery_proof")
        return delivery_proof if isinstance(delivery_proof, dict) else None
    return None


def _scan_delivery(clone_log_slice: str, cell_log_text: str) -> dict[str, Any]:
    entries = set(_CLONE_ENTRY_RE.findall(clone_log_slice))
    outcomes_200 = {
        trace for trace, status in _CLONE_OUTCOME_RE.findall(clone_log_slice) if status == "200"
    }
    continuous = sorted(entries & outcomes_200)
    result_counts = [int(n) for n in _CLONE_RESULT_COUNT_RE.findall(clone_log_slice)]
    injected_env = "recall_env_injection=container" in cell_log_text
    delivery_proof = _extract_sxe_delivery_proof(cell_log_text)
    ok = bool(continuous) and any(n > 0 for n in result_counts) and injected_env
    return {
        "recall_200_traces": continuous,
        "result_counts": result_counts,
        "recall_env_injection_container": injected_env,
        "delivery_proof": delivery_proof,
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

    attempts_numeric = _attempts_numeric(stats.get("attempts_to_green"))
    if stats["verdict"] == "PASS":
        if attempts_numeric == 1:
            return "CEILING"
        return "BRACKET"
    if stats["verdict"] == "BUDGET_STOP":
        # Budget-stop is an instrument outcome, not a capability fail class.
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
    attempts_numeric = _attempts_numeric(stats.get("attempts_to_green"))
    max_attempts_numeric = _attempts_numeric(stats.get("max_attempts"))
    if max_attempts_numeric is None:
        max_attempts_numeric = MAX_ATTEMPTS

    # T1 — gate margin <= 1.
    if stats["verdict"] == "FAIL" and len(stats.get("failed_gates") or []) == 1:
        fired.append("T1")
    elif (
        stats["verdict"] == "PASS"
        and attempts_numeric is not None
        and attempts_numeric == max_attempts_numeric
    ):
        fired.append("T1")

    # T2 — lift sign fragile (ON cells with an OFF baseline on the same rung).
    if off_stats is not None:
        off_tokens = float(off_stats.get("total_tokens") or 0.0)
        if off_tokens > 0:
            delta = abs(float(stats.get("total_tokens") or 0.0) - off_tokens) / off_tokens
            if delta < TOKEN_DELTA_FRAGILE:
                fired.append("T2")
        off_attempts_numeric = _attempts_numeric(off_stats.get("attempts_to_green"))
        if (
            "T2" not in fired
            and attempts_numeric is not None
            and off_attempts_numeric is not None
            and attempts_numeric == off_attempts_numeric
        ):
            fired.append("T2")

    # T3 — instrument anomaly while the cell still produced a scored verdict.
    # BUDGET_STOP itself does not trigger T3; only explicit anomalies do.
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
    if not verdicts:
        return "FAIL"

    counts = {"PASS": 0, "FAIL": 0, "BUDGET_STOP": 0}
    for verdict_raw in verdicts:
        verdict = _normalize_verdict(verdict_raw)
        counts[verdict or "FAIL"] += 1

    winner, winner_count = max(counts.items(), key=lambda item: item[1])
    return winner if winner_count * 2 > len(verdicts) else "FAIL"


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _entry_accrued_usd(entry: dict[str, Any]) -> float | None:
    value = entry.get("accrued_usd")
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entry_settled_usd(entry: dict[str, Any]) -> float:
    stats = entry.get("stats") if isinstance(entry.get("stats"), dict) else {}
    actual = stats.get("cost_actual_usd")
    derived = stats.get("cost_derived_usd")
    if isinstance(actual, bool) or isinstance(derived, bool):
        return 0.0
    try:
        return float(actual or 0.0) + float(derived or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _cell_routes_orcarouter(*, cell: dict[str, Any], params: dict[str, Any]) -> bool:
    profile_name = str(params.get("profile") or "").strip()
    if not profile_name:
        return False
    profile = DEFAULT_PROFILES().get(profile_name)
    if profile is None:
        return False
    return str(profile.upstream) == "orcarouter"


def _summarize_cell(cell: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    stats_list = [e["stats"] for e in entries if isinstance(e.get("stats"), dict)]
    accrued_costs = [
        accrued
        for entry in entries
        if (accrued := _entry_accrued_usd(entry)) is not None
    ]
    verdicts = [str(s.get("verdict")) for s in stats_list]
    classes = [_classify(s) for s in stats_list]
    triggers = entries[0].get("triggers") if entries else []
    imported_entries = [entry for entry in entries if bool(entry.get("imported"))]
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
        "median_cost_usd": _median(accrued_costs),
        "attempts_to_green": [s.get("attempts_to_green") for s in stats_list],
        "assertions": [e.get("assertions") for e in entries],
        "run_ids": [e.get("run_id") for e in entries],
        "scorecards": [e.get("scorecard") for e in entries],
        "imported": bool(imported_entries),
        "import_sources": [
            str(source)
            for source in (entry.get("import_source") for entry in imported_entries)
            if isinstance(source, str) and source
        ],
        "import_digests": [
            digests
            for digests in (entry.get("import_digests") for entry in imported_entries)
            if isinstance(digests, dict)
        ],
        "import_notes": [
            str(note)
            for note in (entry.get("import_note") for entry in imported_entries)
            if isinstance(note, str) and note
        ],
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


def _build_session_extra_flags(
    params: dict[str, Any],
    token_file: Path,
    port: int,
    *,
    binding_cap_usd: float,
) -> str:
    # Single-meter canonical path: proxy BudgetLedger hard cap is authoritative.
    # Mirror that same hard cap into adapter --cost-limit for informational parity.
    flags = [
        "--max-attempts",
        str(MAX_ATTEMPTS),
        "--max-steps-per-attempt",
        str(MAX_STEPS_PER_ATTEMPT),
        "--cost-limit",
        f"{float(binding_cap_usd):.6f}",
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


def _build_inner_env(proxy_checkpoint: Path) -> dict[str, str]:
    env = os.environ.copy()
    env[PROXY_CHECKPOINT_ENV] = str(proxy_checkpoint)
    return env


def _build_error_entry(
    *,
    cell: dict[str, Any],
    rep: int,
    trace: str,
    error: Exception,
    run_id: str | None = None,
    run_label: str | None = None,
    scorecard: str | None = None,
    detail: str | None = None,
    cell_log: str | None = None,
    proxy_log: str | None = None,
    proxy_checkpoint: str | None = None,
    accrued_usd: Any = None,
    committed_unproven_usd: Any = None,
    stats: dict[str, Any] | None = None,
    anomalies: dict[str, Any] | None = None,
    assertions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "rung_index": int(cell["rung_index"]),
        "model": str(cell["model"]),
        "run_number": int(cell["run_number"]),
        "rep": int(rep),
        "phase": CELL_PHASE,
        "memory_mode": str(cell["memory_mode"]),
        "role": str(cell["role"]),
        "status": "error",
        "run_id": run_id,
        "run_label": run_label,
        "scorecard": scorecard,
        "detail": detail,
        "cell_log": cell_log,
        "proxy_log": proxy_log,
        "proxy_checkpoint": proxy_checkpoint,
        "accrued_usd": accrued_usd,
        "committed_unproven_usd": committed_unproven_usd,
        "error": str(error),
        "completed_at": _utc_iso(),
        "trace": trace,
    }
    if isinstance(stats, dict):
        entry["stats"] = stats
    if isinstance(anomalies, dict):
        entry["anomalies"] = anomalies
    if isinstance(assertions, dict):
        entry["assertions"] = assertions
    return entry


def _build_error_entry_from_existing(
    *,
    cell: dict[str, Any],
    rep: int,
    trace: str,
    error: Exception,
    entry: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = entry if isinstance(entry, dict) else {}
    run_id = payload.get("run_id")
    run_label = payload.get("run_label")
    scorecard = payload.get("scorecard")
    detail = payload.get("detail")
    cell_log = payload.get("cell_log")
    proxy_log = payload.get("proxy_log")
    proxy_checkpoint = payload.get("proxy_checkpoint")
    return _build_error_entry(
        cell=cell,
        rep=rep,
        trace=trace,
        error=error,
        run_id=str(run_id) if run_id is not None else None,
        run_label=str(run_label) if run_label is not None else None,
        scorecard=str(scorecard) if scorecard is not None else None,
        detail=str(detail) if detail is not None else None,
        cell_log=str(cell_log) if cell_log is not None else None,
        proxy_log=str(proxy_log) if proxy_log is not None else None,
        proxy_checkpoint=str(proxy_checkpoint) if proxy_checkpoint is not None else None,
        accrued_usd=payload.get("accrued_usd"),
        committed_unproven_usd=payload.get("committed_unproven_usd"),
        stats=payload.get("stats") if isinstance(payload.get("stats"), dict) else None,
        anomalies=payload.get("anomalies") if isinstance(payload.get("anomalies"), dict) else None,
        assertions=payload.get("assertions") if isinstance(payload.get("assertions"), dict) else None,
    )


def _persist_error_entry(checkpoint: dict[str, Any], checkpoint_path: Path, entry: dict[str, Any]) -> None:
    _upsert_entry(checkpoint, entry)
    _save_json_atomic(checkpoint_path, checkpoint)


def _run_inner_tee(cmd: list[str], cell_log: Path, *, env: dict[str, str] | None = None) -> int:
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
            env=env,
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
    run_label = f"stage{STAGE_NUMBER}-run{run_number}-{slug}" + ("" if rep == 1 else f"-rep{rep}")
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

    proxy_log: Path | None = None
    proxy_checkpoint: Path | None = None
    cell_log: Path | None = None
    scorecard_path: Path | None = None
    detail_path: Path | None = None
    accrued: Any = None
    accrued_derived: Any = 0.0
    committed: Any = None
    stats: dict[str, Any] | None = None
    anomalies: dict[str, Any] | None = None
    assertions: dict[str, Any] | None = None

    try:
        binding_cap_usd, reconciled_cost_limit = _reconciled_cost_limit_usd(params)
        if reconciled_cost_limit is not None:
            progress(
                "budget-reconcile",
                f"meter={BINDING_BUDGET_METER} hard_cap_usd={binding_cap_usd:.6f} "
                f"rung_cost_limit_usd={reconciled_cost_limit:.6f} decision=use_cap_usd",
            )

        progress("ledger-check", f"cap_usd={binding_cap_usd:.4f}")
        if not _ledger_check(binding_cap_usd):
            raise LadderAbort("ledger_refused", {"run_id": run_id, "cap_usd": binding_cap_usd})

        prebudget_path = runs_dir / f"{run_id}-prebudget.json"
        _save_json_atomic(
            prebudget_path,
            {
                "schema_version": 1,
                "run_id": run_id,
                "accrued_actual_usd": 0.0,
                "committed_unproven_usd": binding_cap_usd,
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

            extra_session_flags = _build_session_extra_flags(
                params,
                token_file,
                int(args.proxy_port),
                binding_cap_usd=binding_cap_usd,
            )
            inner_cmd = _build_inner_cmd(
                cell=cell,
                rep=rep,
                run_label=run_label,
                extra_session_flags=extra_session_flags,
                ladder_runs_dir=ladder_runs_dir,
                org_id=str(args.org_id),
            )
            inner_env = _build_inner_env(proxy_checkpoint)
            cell_log = runs_dir / f"{stamp}-{run_label}-cell.log"
            progress("cell-start", f"cell_log={cell_log}")
            started = time.perf_counter()
            inner_rc = _run_inner_tee(inner_cmd, cell_log, env=inner_env)
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

        scorecard_path = _newest_artifact(
            ladder_runs_dir,
            run_label,
            "scorecard.json",
            started_wall := (time.time() - dur_s),
        )
        detail_path = _newest_artifact(ladder_runs_dir, run_label, "backgammon-detail.json", started_wall)
        scorecard = _load_json(scorecard_path) or {}
        detail = _load_json(detail_path) or {}
        stats = _extract_stats(scorecard, detail)

        proxy_log_text = _read_text_or_empty(proxy_log)
        cell_log_text = _read_text_or_empty(cell_log)

        assertions = {}
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

        proxy_cp = _load_json(proxy_checkpoint) if proxy_checkpoint.is_file() else None
        if isinstance(proxy_cp, dict):
            accrued = proxy_cp.get("accrued_actual_usd")
            accrued_derived = proxy_cp.get("accrued_derived_usd", 0.0)
            committed = proxy_cp.get("committed_unproven_usd")
            if stats is not None:
                try:
                    actual_cost = float(accrued or 0.0)
                    derived_cost = float(accrued_derived or 0.0)
                    stats["cost_usd"] = actual_cost + derived_cost
                    stats["cost_actual_usd"] = actual_cost
                    stats["cost_derived_usd"] = derived_cost
                except (TypeError, ValueError):
                    pass

        try:
            progress_cost_actual = float(accrued or 0.0)
            progress_cost_derived = float(accrued_derived or 0.0)
            progress_cost_settled = progress_cost_actual + progress_cost_derived
        except (TypeError, ValueError):
            progress_cost_actual = 0.0
            progress_cost_derived = 0.0
            progress_cost_settled = 0.0
        progress(
            "ok",
            f"verdict={stats['verdict']} tokens={stats['total_tokens']:.0f} "
            f"cost_actual_usd={progress_cost_actual:.4f} "
            f"cost_derived_usd={progress_cost_derived:.4f} "
            f"cost_settled_usd={progress_cost_settled:.4f} dur_s={dur_s:.1f}",
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
            "accrued_derived_usd": accrued_derived,
            "committed_unproven_usd": committed,
            "stats": stats,
            "anomalies": anomalies,
            "assertions": assertions,
            "completed_at": _utc_iso(),
            "trace": trace,
        }
    except LadderAbort:
        raise
    except Exception as exc:
        error_entry = _build_error_entry(
            cell=cell,
            rep=rep,
            trace=trace,
            error=exc,
            run_id=run_id,
            run_label=run_label,
            scorecard=str(scorecard_path) if scorecard_path is not None else None,
            detail=str(detail_path) if detail_path is not None else None,
            cell_log=str(cell_log) if cell_log is not None else None,
            proxy_log=str(proxy_log) if proxy_log is not None else None,
            proxy_checkpoint=str(proxy_checkpoint) if proxy_checkpoint is not None else None,
            accrued_usd=accrued,
            committed_unproven_usd=committed,
            stats=stats,
            anomalies=anomalies,
            assertions=assertions,
        )
        raise CellRunUnexpectedError(str(exc), error_entry) from exc

# ---------------------------------------------------------------------------
# Driver


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage-8 scored backgammon ladder driver (roster A).",
        epilog=(
            "Import recovery note: --resume already skips imported status=ok cells; "
            "--start-cell 2 is belt-and-suspenders for the known Cell-1 crash recovery."
        ),
    )
    parser.add_argument("--resume", action="store_true", help="Skip cells/reps already marked ok in the checkpoint.")
    parser.add_argument("--runs-dir", default=str(_default_runs_dir()), help="Outer driver runs directory (checkpoint/manifest/logs/summary).")
    parser.add_argument("--ladder-runs-dir", default=str(_default_ladder_runs_dir()), help="--runs-dir passed to scripts/backgammon_ladder.py (scorecards/details live here).")
    parser.add_argument("--rung-params", default=None, help="JSON file with per-model proxy/pricing/cost params (required unless --dry-run).")
    parser.add_argument("--clone-log", default=str(_repo_root() / DEFAULT_CLONE_LOG), help="Recall clone logfile for ON-cell delivery assertion.")
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT)
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID)
    parser.add_argument(
        "--import-cell",
        default=None,
        help="Strict recovery import JSON for one pre-existing cell artifact set.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the planned cells and exit 0 (no ledger/proxy/spend).")
    parser.add_argument("--start-cell", type=int, default=None, help="Optional run number to force start at.")
    parser.add_argument(
        "--only-reps",
        default=None,
        help='Restrict execution to explicit RUN:REP pairs (example: "1:1,2:2,4:1").',
    )
    parser.add_argument(
        "--session-only-runs",
        default=None,
        help="Comma-separated run numbers forced to phase=session before selection and manifest freeze.",
    )
    parser.add_argument(
        "--extra-disclosure",
        action="append",
        default=[],
        help="Append preregistration disclosure text to the manifest (repeatable).",
    )
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
    attempts = [
        numeric
        for s in stats_list
        if (numeric := _attempts_numeric(s.get("attempts_to_green"))) is not None
    ]
    return {
        "total_tokens": _median([float(s.get("total_tokens") or 0.0) for s in stats_list]),
        "attempts_to_green": int(_median([float(a) for a in attempts])) if attempts else None,
    }


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()
    if args.only_reps and args.start_cell is not None:
        parser.error("--only-reps cannot be combined with --start-cell")
    if args.start_cell is not None and int(args.start_cell) < 1:
        parser.error("--start-cell must be >= 1")

    plan = _build_plan()
    valid_runs = {int(cell["run_number"]) for cell in plan}

    only_reps: dict[int, list[int]] | None = None
    if args.only_reps:
        try:
            only_reps = _parse_only_reps(str(args.only_reps), valid_runs=valid_runs)
        except RuntimeError as exc:
            parser.error(str(exc))

    session_only_runs: list[int] = []
    if args.session_only_runs:
        try:
            session_only_runs = _parse_run_number_csv(
                str(args.session_only_runs),
                flag="--session-only-runs",
                valid_runs=valid_runs,
            )
        except RuntimeError as exc:
            parser.error(str(exc))
    if session_only_runs:
        session_only_set = set(session_only_runs)
        for cell in plan:
            if int(cell["run_number"]) in session_only_set:
                cell["phase"] = "session"

    trace = _new_trace_id()

    runs_dir = Path(str(args.runs_dir)).expanduser().resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    logfile_path = runs_dir / f"{_utc_compact()}.log"
    print(str(logfile_path), flush=True)

    if only_reps is None:
        selected = [
            cell
            for cell in plan
            if args.start_cell is None or int(cell["run_number"]) >= int(args.start_cell)
        ]
    else:
        selected = [cell for cell in plan if int(cell["run_number"]) in only_reps]
    if not selected:
        raise RuntimeError("no cells selected to run")

    selected_reps_payload: list[dict[str, Any]] = []
    selected_reps_total = 0
    for cell in selected:
        run_number = int(cell["run_number"])
        reps = [1] if only_reps is None else list(only_reps[run_number])
        selected_reps_total += len(reps)
        selected_reps_payload.append(
            {
                "run_number": run_number,
                "model": str(cell["model"]),
                "memory_mode": str(cell["memory_mode"]),
                "phase": str(cell["phase"]),
                "reps": reps,
            }
        )

    plan_line = (
        f"[{_utc_iso()}] PLAN trace={trace} total_cells={len(plan)} selected_cells={len(selected)} "
        f"cells={json.dumps([{k: cell[k] for k in ('rung_index', 'model', 'role', 'run_number', 'memory_mode', 'phase')} for cell in plan], separators=(',', ':'))}"
    )
    plan_reps_line = (
        f"[{_utc_iso()}] PLAN-REPS trace={trace} selected_reps={selected_reps_total} "
        f"cells={json.dumps(selected_reps_payload, separators=(',', ':'))}"
    )
    session_only_line = (
        f"[{_utc_iso()}] PLAN-SESSION-ONLY trace={trace} runs={json.dumps(session_only_runs, separators=(',', ':'))}"
        if session_only_runs
        else None
    )

    rung_params: dict[str, dict[str, Any]] | None = None
    if args.rung_params:
        rung_params = _load_rung_params(
            Path(str(args.rung_params)).expanduser(),
            backgammon_scored_ladder_roster(),
        )
    elif not args.dry_run:
        parser.error("--rung-params is required for a real run")

    def _print_dry_run_rows() -> None:
        for cell in selected:
            reps = [1] if only_reps is None else only_reps[int(cell["run_number"])]
            for rep in reps:
                payload = _dry_run_cell_payload(cell, rung_params)
                if only_reps is not None:
                    payload["rep"] = int(rep)
                print(json.dumps(payload, sort_keys=True), flush=True)

    if args.dry_run and not args.import_cell and rung_params is None:
        _append_log_line(logfile_path, plan_line)
        _append_log_line(logfile_path, plan_reps_line)
        if session_only_line is not None:
            _append_log_line(logfile_path, session_only_line)
        _print_dry_run_rows()
        _append_log_line(
            logfile_path,
            f"[{_utc_iso()}] SUMMARY trace={trace} status=dry-run planned_cells={len(selected)} "
            f"planned_reps={selected_reps_total}",
        )
        return 0

    _emit(logfile_path, plan_line)
    _emit(logfile_path, plan_reps_line)
    if session_only_line is not None:
        _emit(logfile_path, session_only_line)

    checkpoint_path = runs_dir / CHECKPOINT_NAME
    checkpoint = _load_checkpoint(checkpoint_path)
    manifest_path = runs_dir / MANIFEST_NAME
    existing_manifest = _load_json(manifest_path)

    current_manifest: dict[str, Any]
    if existing_manifest is None:
        if args.import_cell:
            raise RuntimeError(
                f"cannot import cell without a frozen manifest at {manifest_path}"
            )
        if rung_params is None:
            raise RuntimeError(
                "cannot build scored-ladder manifest without --rung-params"
            )
        current_manifest = _build_manifest(
            plan,
            rung_params,
            trace,
            extra_disclosures=list(args.extra_disclosure or []),
        )
        if _checkpoint_cells(checkpoint):
            raise RuntimeError(
                f"cannot resume: found an existing checkpoint in {runs_dir} but no run manifest "
                f"({MANIFEST_NAME}). Start a fresh run in a new --runs-dir."
            )
        _save_json_atomic(manifest_path, current_manifest)
    else:
        if rung_params is not None:
            current_manifest = _build_manifest(
                plan,
                rung_params,
                trace,
                extra_disclosures=list(args.extra_disclosure or []),
            )
            _validate_manifest_or_fail(existing=existing_manifest, current=current_manifest)
        else:
            current_manifest = existing_manifest

    _emit(
        logfile_path,
        f"[{_utc_iso()}] MANIFEST trace={trace} schema={current_manifest['schema_version']} "
        f"fingerprint={current_manifest['config_fingerprint']} total_cells={current_manifest['total_cells']} "
        f"path={manifest_path}",
    )

    if args.import_cell:
        import_source = Path(str(args.import_cell)).expanduser().resolve()
        imported_entry, import_context = _build_import_entry(
            import_source=import_source,
            checkpoint=checkpoint,
            plan=plan,
            manifest=current_manifest,
            dry_run=bool(args.dry_run),
            trace=trace,
        )
        _emit(
            logfile_path,
            f"[{_utc_iso()}] IMPORT trace={trace} run={import_context['run_number']} "
            f"run_id={import_context['run_id']} status=validated probe={import_context['probe_mode']} "
            f"source={import_source}",
        )
        if not args.dry_run:
            _upsert_entry(checkpoint, imported_entry)
            _save_json_atomic(checkpoint_path, checkpoint)
            _emit(
                logfile_path,
                f"[{_utc_iso()}] IMPORT trace={trace} run={import_context['run_number']} status=checkpointed "
                f"checkpoint={checkpoint_path} probe_collection={import_context['probe_collection']}",
            )
        else:
            _emit(
                logfile_path,
                f"[{_utc_iso()}] IMPORT trace={trace} run={import_context['run_number']} "
                "checkpoint_write=0 dry_run=1 pool_probe=skipped",
            )

    if args.dry_run:
        _print_dry_run_rows()
        _emit(
            logfile_path,
            f"[{_utc_iso()}] SUMMARY trace={trace} status=dry-run planned_cells={len(selected)} "
            f"planned_reps={selected_reps_total}",
        )
        return 0

    if rung_params is None:
        raise RuntimeError("--rung-params is required for a real run")

    any_orcarouter_selected = any(
        _cell_routes_orcarouter(cell=cell, params=rung_params[str(cell["model"])])
        for cell in selected
    )
    billing_baseline_cents: float | None = None
    if any_orcarouter_selected:
        try:
            orcarouter_api_key = load_upstream_key("orcarouter", DEFAULT_OPENCODE_AUTH_PATH)
            orcarouter_key_fp = key_fingerprint(orcarouter_api_key)
            billing_baseline_cents = fetch_orcarouter_billing_usage_cents(api_key=orcarouter_api_key)
            _emit(
                logfile_path,
                f"[{_utc_iso()}] BILLING-BASELINE trace={trace} upstream=orcarouter "
                f"auth_path={DEFAULT_OPENCODE_AUTH_PATH} key_fp={orcarouter_key_fp} "
                f"baseline_cents={billing_baseline_cents:.4f}",
            )
        except Exception as exc:
            _emit(
                logfile_path,
                f"[{_utc_iso()}] WARNING trace={trace} event=billing_baseline_fetch_failed "
                f"upstream=orcarouter status=non_fatal error_class={exc.__class__.__name__} "
                f"detail={str(exc)}",
            )

    # Plan-level budget projection: refuse to START if the selected reps'
    # caps cannot fit under the stage/global caps (stop BEFORE overrunning).
    projected = 0.0
    if only_reps is None:
        for cell in selected:
            prior = _find_entry(checkpoint, int(cell["run_number"]), 1)
            if args.resume and prior is not None and str(prior.get("status") or "") == "ok":
                continue
            projected += float(rung_params[str(cell["model"])]["cap_usd"])
    else:
        for cell in selected:
            run_number = int(cell["run_number"])
            for rep in only_reps[run_number]:
                prior = _find_entry(checkpoint, run_number, int(rep))
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
    settled_total_usd = 0.0
    try:
        for cell in selected:
            model = str(cell["model"])
            params = rung_params[model]
            cell_is_orcarouter = _cell_routes_orcarouter(cell=cell, params=params)
            run_number = int(cell["run_number"])

            if only_reps is not None:
                reps_selected = only_reps[run_number]
                for rep in reps_selected:
                    prior = _find_entry(checkpoint, run_number, int(rep))
                    if args.resume and prior is not None and str(prior.get("status") or "") == "ok":
                        skipped += 1
                        _emit(
                            logfile_path,
                            f"[{_utc_iso()}] PROGRESS trace={trace} rung={cell['rung_index']} model={model} "
                            f"run={run_number} rep={rep} mode={cell['memory_mode']} phase=ok resume_skip=1",
                        )
                        continue

                    entry = None
                    try:
                        entry = _run_cell_rep(
                            cell=cell, rep=int(rep), params=params, args=args, trace=trace, logfile_path=logfile_path,
                        )
                        executed += 1
                        if cell_is_orcarouter:
                            settled_total_usd += _entry_settled_usd(entry)
                    except LadderAbort:
                        raise
                    except CellRunUnexpectedError as exc:
                        _persist_error_entry(checkpoint, checkpoint_path, exc.entry)
                        raise
                    except Exception as exc:
                        _persist_error_entry(
                            checkpoint,
                            checkpoint_path,
                            _build_error_entry_from_existing(
                                cell=cell,
                                rep=int(rep),
                                trace=trace,
                                error=exc,
                                entry=entry,
                            ),
                        )
                        raise

                    _upsert_entry(checkpoint, entry)
                    _save_json_atomic(checkpoint_path, checkpoint)

                summary_path = _emit_summary(runs_dir, plan, checkpoint, trace)
                _emit(
                    logfile_path,
                    f"[{_utc_iso()}] CELL-DONE trace={trace} run={run_number} n={len(reps_selected)} "
                    f"summary={summary_path}",
                )
                continue

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
                entry: dict[str, Any] | None = None
                try:
                    entry = _run_cell_rep(
                        cell=cell, rep=1, params=params, args=args, trace=trace, logfile_path=logfile_path,
                    )
                    executed += 1
                    if cell_is_orcarouter:
                        settled_total_usd += _entry_settled_usd(entry)
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
                except LadderAbort:
                    raise
                except CellRunUnexpectedError as exc:
                    _persist_error_entry(checkpoint, checkpoint_path, exc.entry)
                    raise
                except Exception as exc:
                    _persist_error_entry(
                        checkpoint,
                        checkpoint_path,
                        _build_error_entry_from_existing(
                            cell=cell,
                            rep=1,
                            trace=trace,
                            error=exc,
                            entry=entry,
                        ),
                    )
                    raise

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
                entry = None
                try:
                    entry = _run_cell_rep(
                        cell=cell, rep=rep, params=params, args=args, trace=trace, logfile_path=logfile_path,
                    )
                    executed += 1
                    if cell_is_orcarouter:
                        settled_total_usd += _entry_settled_usd(entry)
                except LadderAbort:
                    raise
                except CellRunUnexpectedError as exc:
                    _persist_error_entry(checkpoint, checkpoint_path, exc.entry)
                    raise
                except Exception as exc:
                    _persist_error_entry(
                        checkpoint,
                        checkpoint_path,
                        _build_error_entry_from_existing(
                            cell=cell,
                            rep=rep,
                            trace=trace,
                            error=exc,
                            entry=entry,
                        ),
                    )
                    raise
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

    if any_orcarouter_selected:
        billing_final_cents: float | None = None
        try:
            orcarouter_api_key = load_upstream_key("orcarouter", DEFAULT_OPENCODE_AUTH_PATH)
            orcarouter_key_fp = key_fingerprint(orcarouter_api_key)
            billing_final_cents = fetch_orcarouter_billing_usage_cents(api_key=orcarouter_api_key)
            _emit(
                logfile_path,
                f"[{_utc_iso()}] BILLING-FINAL trace={trace} upstream=orcarouter "
                f"auth_path={DEFAULT_OPENCODE_AUTH_PATH} key_fp={orcarouter_key_fp} "
                f"final_cents={billing_final_cents:.4f}",
            )
        except Exception as exc:
            _emit(
                logfile_path,
                f"[{_utc_iso()}] WARNING trace={trace} event=billing_final_fetch_failed "
                f"upstream=orcarouter status=non_fatal error_class={exc.__class__.__name__} "
                f"detail={str(exc)}",
            )

        reconciliation = reconcile_derived_vs_billing(
            settled_usd=settled_total_usd,
            baseline_cents=billing_baseline_cents,
            final_cents=billing_final_cents,
        )
        _emit(
            logfile_path,
            f"[{_utc_iso()}] BILLING-RECONCILE trace={trace} "
            f"result={json.dumps(reconciliation, sort_keys=True, separators=(',', ':'))}",
        )

    summary_path = _emit_summary(runs_dir, plan, checkpoint, trace)
    if any_orcarouter_selected and str(reconciliation.get("status") or "") == "diverged":
        _emit(
            logfile_path,
            f"[{_utc_iso()}] WARNING trace={trace} SUMMARY billing_reconciliation=diverged "
            f"settled_usd={float(reconciliation.get('settled_usd') or 0.0):.6f} "
            f"delta_counter_usd={float(reconciliation.get('delta_counter_usd') or 0.0):.6f} "
            f"divergence_pct={float(reconciliation.get('divergence_pct') or 0.0):.4f} "
            f"confound_note={json.dumps(str(reconciliation.get('confound_note') or ''), ensure_ascii=False)}",
        )
    _emit(
        logfile_path,
        f"[{_utc_iso()}] SUMMARY trace={trace} status=ok total_cells={len(plan)} executed_reps={executed} "
        f"skipped_reps={skipped} completed_reps={_completed_ok(checkpoint)} summary={summary_path} "
        f"checkpoint={checkpoint_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
