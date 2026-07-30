"""OFF-baseline prerun concurrency core for cumulative benchmark cells."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import inspect
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol

from wevibe_bench.adapters.backgammon import BackgammonCellResult

from .progress import progress_from_cell_result

_LOG = logging.getLogger(__name__)

OFF_CONCURRENCY_ENV = "WEVIBE_BENCH_OFF_CONCURRENCY"
DEFAULT_OFF_CONCURRENCY = 3

_LOCAL_LLM_MARKERS = (
    "ollama",
    "lm-studio",
    "lmstudio",
    "localhost:1234",
    "localhost:11434",
)


class _SessionRunnerLike(Protocol):
    def prepare_fixture(self, session: Any) -> None: ...

    def run_session(self, session: Any) -> object: ...

    def extract(self, session: Any) -> dict[str, Any]: ...

    def index_ready(self, session: Any) -> bool: ...


def resolve_off_concurrency(value: str | int | None = None) -> int:
    """Resolve the OFF-baseline concurrency knob; invalid strings are fatal."""

    raw: str | int | None = os.environ.get(OFF_CONCURRENCY_ENV) if value is None else value
    if raw is None or raw == "":
        return DEFAULT_OFF_CONCURRENCY
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{OFF_CONCURRENCY_ENV} must be an integer >= 1") from exc
    return max(1, parsed)


def is_local_llm(provider_pin_or_model: str) -> bool:
    """Return True when a provider/model string points at a local LLM lane."""

    text = str(provider_pin_or_model or "").lower()
    return any(marker in text for marker in _LOCAL_LLM_MARKERS)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _checkpoint_path(checkpoint_dir: str | os.PathLike[str], sequence_index: int) -> Path:
    return Path(checkpoint_dir) / f"prerun-{int(sequence_index)}.json"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def load_prerun_checkpoint(
    checkpoint_dir: str | os.PathLike[str],
    sequence_index: int,
) -> dict[str, Any] | None:
    """Load a prerun checkpoint for later sequencer wiring."""

    path = _checkpoint_path(checkpoint_dir, sequence_index)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid prerun checkpoint payload: {path}")
    return payload


def _cell_string(session: Any, name: str, default: str = "") -> str:
    value = getattr(session, name, default)
    return default if value is None else str(value)


def _cell_sequence_index(session: Any) -> int:
    return int(getattr(session, "sequence_index"))


def _is_local_cell(session: Any) -> bool:
    return is_local_llm(_cell_string(session, "provider_pin")) or is_local_llm(_cell_string(session, "model"))


def cell_result_to_dict(result: Any) -> dict[str, Any]:
    """Serialize BackgammonCellResult-shaped telemetry without editing adapter code."""

    if isinstance(result, Mapping):
        return dict(result)
    if is_dataclass(result):
        return {field.name: getattr(result, field.name) for field in fields(result)}
    return {
        field.name: getattr(result, field.name)
        for field in fields(BackgammonCellResult)
        if hasattr(result, field.name)
    }


def cell_result_from_dict(payload: Mapping[str, Any]) -> BackgammonCellResult:
    """Deserialize cached BackgammonCellResult telemetry."""

    allowed = {field.name for field in fields(BackgammonCellResult)}
    kwargs = {name: payload[name] for name in allowed if name in payload}
    return BackgammonCellResult(**kwargs)


def _make_runner(runner_factory: Callable[..., _SessionRunnerLike], session: Any) -> _SessionRunnerLike:
    signature = inspect.signature(runner_factory)
    if len(signature.parameters) == 0:
        return runner_factory()
    return runner_factory(session)


def _done_checkpoint(checkpoint_dir: str | os.PathLike[str], session: Any) -> dict[str, Any] | None:
    checkpoint = load_prerun_checkpoint(checkpoint_dir, _cell_sequence_index(session))
    if checkpoint and checkpoint.get("status") == "done":
        return checkpoint
    return None


def _run_one_cell(
    session: Any,
    runner_factory: Callable[..., _SessionRunnerLike],
    checkpoint_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    sequence_index = _cell_sequence_index(session)
    model = _cell_string(session, "model")
    run_label = _cell_string(session, "run_label", "") or _cell_string(session, "run_id", "")
    existing = _done_checkpoint(checkpoint_dir, session)
    if existing is not None:
        _LOG.info(
            "prerun.cell_skip_checkpoint sequence_index=%s model=%s",
            sequence_index,
            model,
        )
        return existing

    started_at = _utc_now_iso()
    started = time.monotonic()
    _LOG.info("prerun.cell_start sequence_index=%s model=%s", sequence_index, model)
    try:
        runner = _make_runner(runner_factory, session)
        runner.prepare_fixture(session)
        telemetry = runner.run_session(session)
        result_dict = cell_result_to_dict(telemetry)
        progress = progress_from_cell_result(telemetry).to_dict()
        wall_s = time.monotonic() - started
        cost_usd = float(result_dict.get("wall_cost_usd") or progress.get("wall_cost_usd") or 0.0)
        checkpoint = {
            "sequence_index": sequence_index,
            "model": model,
            "run_label": run_label,
            "run_id": _cell_string(session, "run_id", ""),
            "telemetry": result_dict,
            "progress": progress,
            "status": "done",
            "error": "",
            "started_at": started_at,
            "finished_at": _utc_now_iso(),
        }
        _atomic_write_json(_checkpoint_path(checkpoint_dir, sequence_index), checkpoint)
        _LOG.info(
            "prerun.cell_done sequence_index=%s model=%s wall_s=%.3f cost_usd=%.6f",
            sequence_index,
            model,
            wall_s,
            cost_usd,
        )
        return checkpoint
    except Exception as exc:  # noqa: BLE001 - checkpoint failures and keep pool alive.
        wall_s = time.monotonic() - started
        _LOG.exception(
            "prerun.cell_failed sequence_index=%s model=%s wall_s=%.3f",
            sequence_index,
            model,
            wall_s,
        )
        checkpoint = {
            "sequence_index": sequence_index,
            "model": model,
            "run_label": run_label,
            "run_id": _cell_string(session, "run_id", ""),
            "telemetry": {},
            "progress": {},
            "status": "failed",
            "error": str(exc),
            "started_at": started_at,
            "finished_at": _utc_now_iso(),
        }
        return checkpoint


def prerun_off_cells(
    sessions: list[Any],
    runner_factory: Callable[..., _SessionRunnerLike],
    checkpoint_dir: str | os.PathLike[str],
    concurrency: int | None = None,
) -> list[dict[str, Any]]:
    """Run pending OFF cells with API concurrency and a hard-serial local-LLM lane."""

    max_workers = resolve_off_concurrency(concurrency)
    api_cells = [session for session in sessions if not _is_local_cell(session)]
    local_cells = [session for session in sessions if _is_local_cell(session)]
    results: dict[int, dict[str, Any]] = {}

    if api_cells:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_run_one_cell, session, runner_factory, checkpoint_dir): session
                for session in api_cells
            }
            for future in as_completed(futures):
                session = futures[future]
                results[_cell_sequence_index(session)] = future.result()

    for session in local_cells:
        results[_cell_sequence_index(session)] = _run_one_cell(session, runner_factory, checkpoint_dir)

    ordered = [results[_cell_sequence_index(session)] for session in sessions]
    done_count = sum(1 for item in ordered if item.get("status") == "done")
    failed_count = sum(1 for item in ordered if item.get("status") == "failed")
    _LOG.info(
        "prerun.pool_done cells=%d api_cells=%d local_cells=%d concurrency=%d done=%d failed=%d",
        len(sessions),
        len(api_cells),
        len(local_cells),
        max_workers,
        done_count,
        failed_count,
    )
    return ordered


class CachedSessionRunner:
    """SessionRunner wrapper that returns prerun OFF telemetry when checkpointed."""

    def __init__(self, runner: _SessionRunnerLike, checkpoint_dir: str | os.PathLike[str]) -> None:
        self._runner = runner
        self._checkpoint_dir = checkpoint_dir

    def _done_checkpoint(self, session: Any) -> dict[str, Any] | None:
        return _done_checkpoint(self._checkpoint_dir, session)

    def prepare_fixture(self, session: Any) -> None:
        if self._done_checkpoint(session) is not None:
            return None
        return self._runner.prepare_fixture(session)

    def run_session(self, session: Any) -> object:
        checkpoint = self._done_checkpoint(session)
        if checkpoint is not None:
            telemetry = checkpoint.get("telemetry")
            if not isinstance(telemetry, Mapping):
                raise ValueError("done prerun checkpoint missing telemetry mapping")
            return cell_result_from_dict(telemetry)
        return self._runner.run_session(session)

    def extract(self, session: Any) -> dict[str, Any]:
        return self._runner.extract(session)

    def index_ready(self, session: Any) -> bool:
        return self._runner.index_ready(session)

    def consumer_gate_outcome(self, session: Any) -> Any:
        return self._runner.consumer_gate_outcome(session)


__all__ = [
    "CachedSessionRunner",
    "DEFAULT_OFF_CONCURRENCY",
    "OFF_CONCURRENCY_ENV",
    "cell_result_from_dict",
    "cell_result_to_dict",
    "is_local_llm",
    "load_prerun_checkpoint",
    "prerun_off_cells",
    "resolve_off_concurrency",
]
