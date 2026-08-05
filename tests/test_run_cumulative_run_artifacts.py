from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from wevibe_bench.cumulative.types import PhaseGroup, SessionRecord


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cell_result() -> Any:
    class _R:
        verdict = "PASS"
        termination_reason = "gates_green"
        attempts_to_green = 1
        conformed = True
        input_tokens = 100
        output_tokens = 50
        turns = 3
        attempt_reports = [
            {
                "attempt": 1,
                "verdict": "PASS",
                "conformed": True,
                "n_problems": 0,
                "failed_gates": [],
                "attempt_cost_usd": 0.0,
            }
        ]
        session_id = "sid-1"
        memory_mode = "off"
        model = "orcarouter/x"
        tool_calls = 5
        test_invocations = 2
        agentic_cycles = 1
        problems_before = 3
        problems_after = 0
        worker_image_fingerprint = None

    return _R()


def _build_runner(module: Any, tmp_path: Path, *, runs_dir: Path | None = None) -> Any:
    runs_dir = runs_dir or (tmp_path / "runs")
    runner = module.RealSessionRunner.__new__(module.RealSessionRunner)
    runner._session_states = {}
    runner._runs_dir = runs_dir
    runner._task_dir = tmp_path / "task"
    runner._task = "backgammon"
    runner._max_attempts = 1
    runner._bridge_state_path = None
    runner._proxy_base_url = "http://127.0.0.1:11434/v1"
    runner._proxy_token = "proxy-token-value"
    runner._progress = lambda message: None
    runner._org_id = "org-test"
    runner._repo_root = tmp_path
    runner._run_manifest_base_path = str(runs_dir / "manifest.json")
    runner._run_manifest_written = False
    runner._runner_cls = _FakeRunner
    runner._spend_meter = _FakeSpendMeter()
    return runner


class _FakeSpendMeter:
    def __init__(self) -> None:
        self.identities: list[Any] = []
        self.raise_on_model_identity = False

    def model_identity(self, session_id: str) -> list[Any]:
        if self.raise_on_model_identity:
            raise RuntimeError("spend db unavailable")
        return self.identities

    def contention_covariates(self, *args: Any, **kwargs: Any) -> Any:
        from wevibe_bench.contention import ContentionCovariates

        return ContentionCovariates.empty()


class _FakeRunner:
    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    def run_cell(self, run_label: str, run_dir: Path, task_id: str = "backgammon") -> Any:
        return _cell_result()


def _session(sequence_index: int) -> SessionRecord:
    return SessionRecord(
        sequence_index=sequence_index,
        model="orcarouter/x",
        provider_pin="local",
        memory_mode="off",
        phase_group=PhaseGroup.OFF_BASELINE.value,
        phase="RUN_SESSION",
    )


def _read_manifest(runs_dir: Path) -> dict[str, Any]:
    path = runs_dir / "manifest.run-manifest.json"
    assert path.is_file(), f"run manifest not created at {path}"
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_status_records(runs_dir: Path) -> list[dict[str, Any]]:
    path = runs_dir / "manifest.status.jsonl"
    assert path.is_file(), f"status stream not created at {path}"
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def test_run_manifest_and_status_stream_written(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    runs_dir = tmp_path / "runs"
    runner = _build_runner(module, tmp_path, runs_dir=runs_dir)
    runner._runner_cls = _FakeRunner

    session = _session(0)
    returned = runner.run_session(session)

    # (d) behaviour unchanged: same result object returned.
    assert returned.verdict == "PASS"

    # (a) run manifest created with correct identity fields.
    manifest = _read_manifest(runs_dir)
    assert manifest["memory_mode"] == "off"
    assert manifest["org_id"] == "org-test"
    assert manifest["requested_model"] == "orcarouter/x"
    assert manifest["served_model"] is None  # fake meter returns []
    assert manifest["run_id"] == runs_dir.name

    # (b) status stream exists with >=1 parseable record, each with required keys.
    records = _read_status_records(runs_dir)
    assert len(records) >= 1
    required_keys = {
        "type",
        "sequence_index",
        "memory_mode",
        "org_id",
        "served_model",
        "verdict",
        "progress",
        "work_input_tokens",
        "work_output_tokens",
        "work_total_tokens",
        "injected_block_est_tokens",
        "injected_count",
        "injected_block_chars",
        "consumer_injected_count",
        "extraction_state",
        "terminal_outcome",
        "session_fp",
        "session_id",
    }
    for record in records:
        assert set(required_keys) <= set(record), f"missing keys in {record}"
        assert record["type"] == "attempt"
        assert record["schema_version"] == 1
        assert record["sequence_index"] == 0
        assert record["memory_mode"] == "off"
        assert record["org_id"] == "org-test"
        assert record["extraction_state"] == "unknown"
        assert record["terminal_outcome"] is None
        assert record["work_input_tokens"] == 100
        assert record["work_output_tokens"] == 50
        assert record["work_total_tokens"] == 150

    # (c) last record's progress equals the cell's final progress mapping
    # (computed from the actual returned result, contention populated).
    last = records[-1]
    final_progress = module.progress_from_cell_result(returned).to_dict()
    assert last["progress"] == final_progress


def test_run_manifest_carries_runner_seed(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    runs_dir = tmp_path / "runs"
    runner = _build_runner(module, tmp_path, runs_dir=runs_dir)
    runner._runner_cls = _FakeRunner
    runner._seed = 12345

    runner.run_session(_session(0))

    manifest = _read_manifest(runs_dir)
    assert manifest["seed"] == 12345


def test_run_manifest_write_once_and_stream_append_only(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    runs_dir = tmp_path / "runs"
    runner = _build_runner(module, tmp_path, runs_dir=runs_dir)
    runner._runner_cls = _FakeRunner

    runner.run_session(_session(0))
    manifest_path = runs_dir / "manifest.run-manifest.json"
    first_content = manifest_path.read_bytes()

    runner.run_session(_session(1))

    # Run manifest written only once, same content, no error.
    assert manifest_path.read_bytes() == first_content
    assert manifest_path.read_bytes().count(b"schema_version") == 1

    # Status stream has records from both sessions, order preserved.
    records = _read_status_records(runs_dir)
    assert len(records) == 2
    assert [r["sequence_index"] for r in records] == [0, 1]


def test_served_model_capture_and_failure_tolerance(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    runs_dir = tmp_path / "runs"

    class _Identity:
        model = "x"
        upstream_model = "served-y"
        calls = 1

    runner = _build_runner(module, tmp_path, runs_dir=runs_dir)
    runner._runner_cls = _FakeRunner
    runner._spend_meter.identities = [_Identity()]
    runner.run_session(_session(0))

    records = _read_status_records(runs_dir)
    served_model = records[0]["served_model"]
    assert served_model["upstream_model"] == "served-y"
    assert served_model["model"] == "orcarouter/x"

    # Failure path: model_identity raises -> served_model None, run succeeds.
    runner2 = _build_runner(module, tmp_path, runs_dir=tmp_path / "runs2")
    runner2._runner_cls = _FakeRunner
    runner2._spend_meter.raise_on_model_identity = True
    result = runner2.run_session(_session(0))
    assert result.verdict == "PASS"
    records2 = _read_status_records(tmp_path / "runs2")
    assert records2[0]["served_model"] is None