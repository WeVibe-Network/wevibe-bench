from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
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


@pytest.fixture(autouse=True)
def _empty_proxy_runs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the relay-proxy run-log source (WEVIBE_PROXY_RUNS_DIR, read at call
    time inside _read_proxy_served_identity) at an EMPTY temp dir.

    This makes the spend-DB-fallback / NULL-when-meter-empty behaviour
    deterministic on every machine regardless of whether a real proxy log
    exists at DEFAULT_PROXY_RUNS_DIR. Tests that intentionally exercise the
    proxy-log path override this env (their monkeypatch.setenv runs after the
    fixture's).
    """
    empty = tmp_path / "empty-proxy-runs"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WEVIBE_PROXY_RUNS_DIR", str(empty))
    return empty


def _build_runner(module: Any, tmp_path: Path, *, runs_dir: Path | None = None) -> Any:
    runs_dir = runs_dir or (tmp_path / "runs")
    runner = module.RealSessionRunner.__new__(module.RealSessionRunner)
    runner._session_states = {}
    runner._runs_dir = runs_dir
    runner._task_dir = tmp_path / "task"
    runner._task = "backgammon"
    runner._max_attempts = 1
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
        # WO-TRUNC-1: terminal outcome is recorded, never placeholder-null.
        assert record["terminal_outcome"] is True  # verdict PASS
        assert record["terminal_reason"] == "gates_green"
        assert record["length_truncations"] == 0
        assert record["truncated_turns"] == 0
        assert record["truncated_turns_retried"] == 0
        assert record["unmetered_turns"] == 0
        assert record["unmetered_turn_wall_s"] == 0.0
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


def test_turn_terminal_records_appended_for_truncated_turns(tmp_path: Path) -> None:
    """WO-TRUNC-1: a truncated turn lands in the status stream as turn_terminal."""
    module = _load_run_cumulative_module()
    runs_dir = tmp_path / "runs"

    result = _cell_result()
    result.verdict = "FAIL"
    result.termination_reason = "transport_incomplete"
    result.truncations = 1
    result.truncated_turns = 1
    result.truncated_turns_retried = 1
    result.unmetered_turns = 1
    result.unmetered_turn_wall_s = 60.0
    result.turn_anomalies = [
        {
            "phase": "initial",
            "turn_index": 7,
            "terminal": "truncated_no_signal",
            "reason": "unknown",
            "tool_uses": 0,
            "file_writes": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
            "tokens_unmetered": True,
            "wall_seconds": 60.0,
            "retried": True,
            "retry_kind": "client_auto",
            "session_id": "sid-1",
        }
    ]

    class _TruncRunner:
        def __init__(self, **kwargs: Any) -> None:
            self._kwargs = kwargs

        def run_cell(self, run_label: str, run_dir: Path, task_id: str = "backgammon") -> Any:
            return result

    runner = _build_runner(module, tmp_path, runs_dir=runs_dir)
    runner._runner_cls = _TruncRunner
    runner.run_session(_session(0))

    records = _read_status_records(runs_dir)
    attempt_records = [r for r in records if r["type"] == "attempt"]
    turn_records = [r for r in records if r["type"] == "turn_terminal"]

    assert len(attempt_records) == 1
    attempt = attempt_records[0]
    assert attempt["terminal_outcome"] is False  # FAIL, not a placeholder null
    assert attempt["terminal_reason"] == "transport_incomplete"
    assert attempt["length_truncations"] == 1
    assert attempt["truncated_turns"] == 1
    assert attempt["truncated_turns_retried"] == 1
    assert attempt["unmetered_turns"] == 1
    assert attempt["unmetered_turn_wall_s"] == 60.0

    assert len(turn_records) == 1
    turn = turn_records[0]
    assert turn["schema_version"] == 1
    assert turn["sequence_index"] == 0
    assert turn["memory_mode"] == "off"
    assert turn["org_id"] == "org-test"
    assert turn["terminal"] == "truncated_no_signal"
    assert turn["reason"] == "unknown"
    assert turn["turn_index"] == 7
    assert turn["phase"] == "initial"
    assert turn["tokens_unmetered"] is True
    assert turn["wall_seconds"] == 60.0
    assert turn["retried"] is True
    assert turn["retry_kind"] == "client_auto"
    assert turn["session_id"] == "sid-1"


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


def test_local_proxy_log_served_identity_lands_in_manifest_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WO-NIGHT2-1d: the relay proxy's API-reported served identity (genuine
    upstreamModel) lands in BOTH the run manifest and the attempt status
    records, while alias-echo rows and non-request rows are rejected. The spend
    DB stays empty here, so the proxy log is the sole source of identity."""
    module = _load_run_cumulative_module()
    runs_dir = tmp_path / "runs"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    proxy_dir = tmp_path / "proxy-runs"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    log_path = proxy_dir / f"{today}.jsonl"
    rows = [
        # Alias-echo row: upstream == requested -> must be REJECTED.
        {
            "type": "request",
            "ts": f"{today}T00:00:01Z",
            "requestedModel": "auto (Local LLM Proxy - oMLX)",
            "upstreamModel": "auto (Local LLM Proxy - oMLX)",
        },
        # Non-request row: skipped by type.
        {
            "type": "heartbeat",
            "ts": f"{today}T00:00:03Z",
            "requestedModel": "auto (Local LLM Proxy - oMLX)",
        },
        # Genuine row, latest by ts -> selected.
        {
            "type": "request",
            "ts": f"{today}T00:00:05Z",
            "requestedModel": "auto (Local LLM Proxy - oMLX)",
            "upstreamModel": "Vontra--DeepSeek-V4-Flash-0731-MXFP4-MLX",
        },
    ]
    log_path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    # Override the autouse empty-dir fixture: this test exercises the real
    # proxy-log path.
    monkeypatch.setenv("WEVIBE_PROXY_RUNS_DIR", str(proxy_dir))

    runner = _build_runner(module, tmp_path, runs_dir=runs_dir)
    runner._runner_cls = _FakeRunner
    # Empty spend meter (no identities) so the proxy log is the sole source.
    runner._spend_meter.identities = []
    runner.run_session(_session(0))

    # Manifest carries the genuine served identity (NON-NULL).
    manifest = _read_manifest(runs_dir)
    assert (
        manifest["served_model"] == "Vontra--DeepSeek-V4-Flash-0731-MXFP4-MLX"
    )

    # Status records carry the served-model dict with the genuine upstream.
    records = _read_status_records(runs_dir)
    assert len(records) >= 1
    for record in records:
        assert record["type"] == "attempt"
        assert (
            record["served_model"]["upstream_model"]
            == "Vontra--DeepSeek-V4-Flash-0731-MXFP4-MLX"
        )
        assert record["served_model"]["model"] == "orcarouter/x"

    # Direct reader assertions: genuine identity via the fixture dir, None when
    # the source dir is empty (fallback degrades).
    assert (
        module._read_proxy_served_identity(proxy_dir)
        == "Vontra--DeepSeek-V4-Flash-0731-MXFP4-MLX"
    )
    empty_dir = tmp_path / "empty-proxy-runs"
    empty_dir.mkdir(parents=True, exist_ok=True)
    assert module._read_proxy_served_identity(empty_dir) is None