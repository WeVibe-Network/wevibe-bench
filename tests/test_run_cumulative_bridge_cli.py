import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wevibe_bench.cumulative.bridge_state import (
    WorkerLease,
    atomic_write_state,
    load_state,
    resume_or_create_state,
)
from wevibe_bench.cumulative.consumer_bridge import manifest_inbox_name
from wevibe_bench.cumulative.consumer_decision import (
    CONSUMER_DECISION_SCHEMA_VERSION,
    ConsumerCandidateDecision,
    ConsumerDecisionManifest,
)
from wevibe_bench.cumulative.consumer_gate import (
    DECISIONS_FILENAME,
    HEARTBEAT_FILENAME,
    QUEUE_FILENAME,
)
from wevibe_bench.cumulative.types import SessionRecord


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke_main(module: Any, argv: list[str]) -> int:
    prior_argv = list(sys.argv)
    try:
        sys.argv = ["run_cumulative.py", *argv]
        return int(module.main())
    finally:
        sys.argv = prior_argv


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_until(predicate: Any, *, timeout_s: float = 5.0, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if bool(predicate()):
            return True
        time.sleep(interval_s)
    return bool(predicate())


def _pid_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        waited_pid = 0
    if waited_pid == pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_last_json_stdout(capsys: Any) -> dict[str, Any]:
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "expected JSON output on stdout"
    return dict(json.loads(lines[-1]))


def _bridge_argv(
    *,
    manifest_path: Path,
    action: str,
    run_id: str = "",
    session_id: str = "",
    state_dir: Path | None = None,
    manifest_inbox: Path | None = None,
    served_store: Path | None = None,
    lease_ttl_ms: int | None = None,
    poll_interval_ms: int | None = None,
    heartbeat_cadence_ms: int | None = None,
    max_cycles: int | None = None,
) -> list[str]:
    argv = ["--manifest", str(manifest_path), "bridge", action]
    if run_id:
        argv.extend(["--run-id", run_id])
    if session_id:
        argv.extend(["--session-id", session_id])
    if state_dir is not None:
        argv.extend(["--state-dir", str(state_dir)])
    if manifest_inbox is not None:
        argv.extend(["--manifest-inbox", str(manifest_inbox)])
    if served_store is not None:
        argv.extend(["--served-store", str(served_store)])
    if lease_ttl_ms is not None:
        argv.extend(["--lease-ttl-ms", str(lease_ttl_ms)])
    if poll_interval_ms is not None:
        argv.extend(["--poll-interval-ms", str(poll_interval_ms)])
    if heartbeat_cadence_ms is not None:
        argv.extend(["--heartbeat-cadence-ms", str(heartbeat_cadence_ms)])
    if max_cycles is not None:
        argv.extend(["--max-cycles", str(max_cycles)])
    return argv


def _manifest(
    *,
    run_id: str,
    session_id: str,
    coordinator_trace: str,
    default_fate: str,
    decisions: tuple[ConsumerCandidateDecision, ...],
) -> ConsumerDecisionManifest:
    return ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id=run_id,
        policy_id="consumer-policy-bridge-cli",
        default_fate=default_fate,
        decisions=decisions,
        coordinator_trace=coordinator_trace,
    )


def test_assert_bridge_ready_requires_live_active_lease(tmp_path: Path) -> None:
    module = _load_run_cumulative_module()
    state_path = tmp_path / "bridge-state.json"

    with pytest.raises(RuntimeError, match="Start bridge first"):
        module.assert_bridge_ready(state_path, now_ms=100)

    state = resume_or_create_state(
        state_path,
        run_id="run-ready",
        session_id="session-ready",
        session_fp=SessionRecord.session_fp_of("session-ready"),
        container_name="bridge-ready",
    )

    state.lease = WorkerLease(pid=1234, started_at_ms=0, ttl_ms=10_000, expires_at_ms=10_000)
    state.resume_marker = "stopped"
    atomic_write_state(state_path, state)
    with pytest.raises(RuntimeError, match="not active"):
        module.assert_bridge_ready(state_path, now_ms=100)

    state.resume_marker = "active"
    state.lease = WorkerLease(pid=1234, started_at_ms=0, ttl_ms=100, expires_at_ms=100)
    atomic_write_state(state_path, state)
    with pytest.raises(RuntimeError, match="expired"):
        module.assert_bridge_ready(state_path, now_ms=200)

    state.lease = WorkerLease(pid=1234, started_at_ms=0, ttl_ms=10_000, expires_at_ms=10_000)
    atomic_write_state(state_path, state)
    module.assert_bridge_ready(state_path, now_ms=200)


def test_bridge_lifecycle_start_status_stop_and_resume(tmp_path: Path, capsys: Any) -> None:
    module = _load_run_cumulative_module()

    manifest_path = tmp_path / "runs" / "manifest.json"
    state_dir = tmp_path / "plugin-state"
    served_store = tmp_path / "served-store.json"
    run_id = "run-bridge-lifecycle"
    session_id = "session-bridge-lifecycle"

    start_argv = _bridge_argv(
        manifest_path=manifest_path,
        action="start",
        run_id=run_id,
        session_id=session_id,
        state_dir=state_dir,
        served_store=served_store,
        lease_ttl_ms=20_000,
        poll_interval_ms=100,
        heartbeat_cadence_ms=100,
    )
    start_rc = _invoke_main(module, start_argv)
    start_payload = _parse_last_json_stdout(capsys)
    assert start_rc == 0

    state_path = Path(start_payload["state_path"])
    pidfile = Path(start_payload["pidfile"])
    logfile = Path(start_payload["logfile"])
    bridge_inbox = Path(start_payload["manifest_inbox"])
    pid = int(start_payload["pid"])

    expected_base = manifest_path.resolve().parent / "bridge"
    assert state_path == expected_base / "bridge-state.json"
    assert pidfile == expected_base / "bridge.pid"
    assert bridge_inbox == expected_base / "inbox"

    assert _wait_until(lambda: state_path.exists())
    assert _wait_until(lambda: pidfile.exists())
    assert _wait_until(lambda: logfile.exists())

    try:
        status_rc = _invoke_main(
            module,
            _bridge_argv(
                manifest_path=manifest_path,
                action="status",
                state_dir=state_dir,
                served_store=served_store,
            ),
        )
        status_payload = _parse_last_json_stdout(capsys)
        assert status_rc == 0
        assert status_payload["running"] is True
        assert status_payload["pid_alive"] is True
        assert int(status_payload["pid"]) == pid

        stop_rc = _invoke_main(
            module,
            _bridge_argv(
                manifest_path=manifest_path,
                action="stop",
                state_dir=state_dir,
                served_store=served_store,
            ),
        )
        stop_payload = _parse_last_json_stdout(capsys)
        assert stop_rc == 0
        assert stop_payload["status"] == "stopped"

        assert not pidfile.exists()
        assert not _pid_is_alive(pid)

        stopped_state = load_state(state_path)
        assert stopped_state is not None
        assert stopped_state.resume_marker == "stopped"
        assert stopped_state.lease is None

        resume_rc = _invoke_main(
            module,
            _bridge_argv(
                manifest_path=manifest_path,
                action="resume",
                run_id=run_id,
                session_id=session_id,
                state_dir=state_dir,
                served_store=served_store,
                max_cycles=1,
                poll_interval_ms=5,
                heartbeat_cadence_ms=5,
            ),
        )
        resume_payload = _parse_last_json_stdout(capsys)
        assert resume_rc == 0

        resume_pidfile = Path(resume_payload["pidfile"])
        assert _wait_until(lambda: not resume_pidfile.exists(), timeout_s=10.0)
        resumed_state = load_state(state_path)
        assert resumed_state is not None
        assert resumed_state.resume_marker == "stopped"
    finally:
        _invoke_main(
            module,
            _bridge_argv(
                manifest_path=manifest_path,
                action="stop",
                state_dir=state_dir,
                served_store=served_store,
            ),
        )
        capsys.readouterr()


def test_bridge_signal_shutdown_stops_heartbeat_refresh(tmp_path: Path, capsys: Any) -> None:
    module = _load_run_cumulative_module()

    manifest_path = tmp_path / "runs" / "manifest.json"
    state_dir = tmp_path / "plugin-state"
    served_store = tmp_path / "served-store.json"

    start_rc = _invoke_main(
        module,
        _bridge_argv(
            manifest_path=manifest_path,
            action="start",
            run_id="run-signal",
            session_id="session-signal",
            state_dir=state_dir,
            served_store=served_store,
            lease_ttl_ms=20_000,
            poll_interval_ms=50,
            heartbeat_cadence_ms=50,
        ),
    )
    start_payload = _parse_last_json_stdout(capsys)
    assert start_rc == 0

    state_path = Path(start_payload["state_path"])
    pidfile = Path(start_payload["pidfile"])
    heartbeat_path = state_dir / HEARTBEAT_FILENAME
    pid = int(start_payload["pid"])

    assert _wait_until(lambda: heartbeat_path.exists(), timeout_s=10.0)
    assert _wait_until(lambda: _pid_is_alive(pid), timeout_s=5.0)

    try:
        os.kill(pid, signal.SIGTERM)
        assert _wait_until(lambda: not _pid_is_alive(pid), timeout_s=15.0)
        assert _wait_until(lambda: not pidfile.exists(), timeout_s=10.0)

        ts_after_exit = int(_read_json(heartbeat_path)["ts"])
        time.sleep(0.25)
        ts_later = int(_read_json(heartbeat_path)["ts"])
        assert ts_later == ts_after_exit

        state = load_state(state_path)
        assert state is not None
        assert state.resume_marker == "stopped"
    finally:
        _invoke_main(
            module,
            _bridge_argv(
                manifest_path=manifest_path,
                action="stop",
                state_dir=state_dir,
                served_store=served_store,
            ),
        )
        capsys.readouterr()


def test_run_session_on_requires_bridge_ready_before_run_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_run_cumulative_module()
    module._load_sxe_helpers = lambda _repo_root: (
        lambda **_kwargs: ([], {}, []),
        lambda _session_dir: {},
        lambda _session_dir, **_kwargs: "session-from-events",
    )

    monkeypatch.setenv("WEVIBE_BENCH_CONSUMER_STATE_DIR", str(tmp_path / "plugin-state"))

    calls = {"count": 0}

    class _SentinelRunner:
        def __init__(self, **_kwargs: Any) -> None:
            return

        def run_cell(self, *_args: Any, **_kwargs: Any) -> Any:
            calls["count"] += 1
            return SimpleNamespace(session_id="session-off", verdict="ok")

    runner = module.RealSessionRunner(
        task="backgammon",
        org_id="org-test",
        runs_dir=tmp_path / "runs",
        repo_root=Path(__file__).resolve().parents[1],
        proof=SimpleNamespace(),
        hub_client=SimpleNamespace(),
        leader=SimpleNamespace(),
        contributor_rest=SimpleNamespace(last_job_id=None),
        extract_api_key="extract-key",
        extract_api_key_source="unit-test",
        extract_base_url=None,
        extract_num_ctx=None,
        extract_timeout_s=10,
        consumer_decision_manifest=None,
        served_store_host_path=tmp_path / "served-store.json",
        bridge_state_path=tmp_path / "missing-bridge-state.json",
    )
    monkeypatch.setattr(runner, "_runner_cls", _SentinelRunner)

    on_session = SessionRecord(
        sequence_index=1,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="on",
        phase_group="on",
        phase="RUN_SESSION",
    )
    with pytest.raises(RuntimeError, match="Start bridge first"):
        runner.run_session(on_session)
    assert calls["count"] == 0

    off_session = SessionRecord(
        sequence_index=0,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="off",
        phase_group="off_baseline",
        phase="RUN_SESSION",
    )
    result = runner.run_session(off_session)
    assert result.session_id == "session-off"
    assert calls["count"] == 1


def test_run_session_on_drops_live_manifest_in_bridge_inbox_before_run_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_run_cumulative_module()
    module._load_sxe_helpers = lambda _repo_root: (
        lambda **_kwargs: ([], {}, []),
        lambda _session_dir: {},
        lambda _session_dir, **_kwargs: "session-from-events",
    )

    monkeypatch.setenv("WEVIBE_BENCH_CONSUMER_STATE_DIR", str(tmp_path / "plugin-state"))

    run_id = "cumulative-0001-on-openrouter-model-a"
    session_id = "session-live-drop"
    session_fp = SessionRecord.session_fp_of(session_id)

    bridge_state_path = tmp_path / "bridge" / "bridge-state.json"
    bridge_state = resume_or_create_state(
        bridge_state_path,
        run_id=run_id,
        session_id=session_id,
        session_fp=session_fp,
        container_name="bridge-live-drop",
    )
    now_ms = int(time.time() * 1000)
    bridge_state.lease = WorkerLease(
        pid=1234,
        started_at_ms=now_ms,
        ttl_ms=20_000,
        expires_at_ms=now_ms + 20_000,
    )
    bridge_state.resume_marker = "active"
    atomic_write_state(bridge_state_path, bridge_state)

    inbox = bridge_state_path.parent / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    expected_name = manifest_inbox_name(run_id, session_fp)
    expected_path = inbox / expected_name

    calls = {"count": 0}

    class _SentinelRunner:
        def __init__(self, **_kwargs: Any) -> None:
            return

        def run_cell(self, *_args: Any, **_kwargs: Any) -> Any:
            calls["count"] += 1
            assert expected_path.is_file(), "live manifest drop must happen before run_cell"
            payload = _read_json(expected_path)
            assert payload["schema_version"] == 1
            assert payload["policy_id"] == "primary-auto-accept-eligible-v1"
            assert payload["default_fate"] == "accept"
            assert payload["decisions"] == []
            assert payload["run_id"] == run_id
            assert payload["coordinator_trace"] == f"consumer-gate://{run_id}/{session_id}"
            return SimpleNamespace(session_id=session_id, verdict="ok")

    runner = module.RealSessionRunner(
        task="backgammon",
        org_id="org-test",
        runs_dir=tmp_path / "runs",
        repo_root=Path(__file__).resolve().parents[1],
        proof=SimpleNamespace(),
        hub_client=SimpleNamespace(),
        leader=SimpleNamespace(),
        contributor_rest=SimpleNamespace(last_job_id=None),
        extract_api_key="extract-key",
        extract_api_key_source="unit-test",
        extract_base_url=None,
        extract_num_ctx=None,
        extract_timeout_s=10,
        consumer_decision_manifest=None,
        served_store_host_path=tmp_path / "served-store.json",
        bridge_state_path=bridge_state_path,
    )
    monkeypatch.setattr(runner, "_runner_cls", _SentinelRunner)

    on_session = SessionRecord(
        sequence_index=1,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="on",
        phase_group="on",
        phase="RUN_SESSION",
    )
    on_result = runner.run_session(on_session)
    assert on_result.session_id == session_id
    assert calls["count"] == 1
    assert expected_path.is_file()

    off_session = SessionRecord(
        sequence_index=0,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="off",
        phase_group="off_baseline",
        phase="RUN_SESSION",
    )
    off_result = runner.run_session(off_session)
    assert off_result.session_id == session_id
    assert calls["count"] == 2
    assert not (inbox / manifest_inbox_name("cumulative-0000-off-model-a", session_fp)).exists()


def test_bridge_run_foreground_writes_all_four_fates_shape(tmp_path: Path, capsys: Any) -> None:
    module = _load_run_cumulative_module()

    manifest_path = tmp_path / "runs" / "manifest.json"
    state_dir = tmp_path / "plugin-state"
    inbox = tmp_path / "manifest-inbox"
    served_store = tmp_path / "served-store.json"
    run_id = "run-four-fates"
    session_id = "session-four-fates"

    _write_json(
        state_dir / QUEUE_FILENAME,
        [
            {"id": "cid-accept", "cid": "cid-accept", "text": "a", "source": "recall"},
            {"id": "cid-deny", "cid": "cid-deny", "text": "b", "source": "recall"},
            {"id": "cid-block", "cid": "cid-block", "text": "c", "source": "recall"},
            {"id": "cid-report", "cid": "cid-report", "text": "d", "source": "recall"},
        ],
    )

    manifest = _manifest(
        run_id=run_id,
        session_id=session_id,
        coordinator_trace="trace-four-fates",
        default_fate="accept",
        decisions=(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-deny",
                fate="deny",
                coordinator_trace="trace-four-fates",
                reason="deny",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-block",
                fate="block",
                coordinator_trace="trace-four-fates",
                reason="block",
            ),
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid="cid-report",
                fate="report",
                coordinator_trace="trace-four-fates",
                reason="report",
            ),
        ),
    )
    session_fp = SessionRecord.session_fp_of(session_id)
    _write_json(inbox / manifest_inbox_name(run_id, session_fp), manifest.to_dict())

    rc = _invoke_main(
        module,
        _bridge_argv(
            manifest_path=manifest_path,
            action="run-foreground",
            run_id=run_id,
            session_id=session_id,
            state_dir=state_dir,
            manifest_inbox=inbox,
            served_store=served_store,
            max_cycles=1,
            poll_interval_ms=5,
            heartbeat_cadence_ms=5,
        ),
    )
    capsys.readouterr()
    assert rc == 0

    decisions = _read_json(state_dir / DECISIONS_FILENAME)
    assert isinstance(decisions, list)
    assert len(decisions) == 4
    expected_keys = {"memoryID", "action", "reason", "note", "timestamp"}
    assert all(set(entry.keys()) == expected_keys for entry in decisions)
    assert {entry["memoryID"]: entry["action"] for entry in decisions} == {
        "cid-accept": "accept",
        "cid-deny": "deny",
        "cid-block": "block",
        "cid-report": "report",
    }


def test_bridge_with_no_manifest_emits_no_decisions(tmp_path: Path, capsys: Any) -> None:
    module = _load_run_cumulative_module()

    manifest_path = tmp_path / "runs" / "manifest.json"
    state_dir = tmp_path / "plugin-state"
    inbox = tmp_path / "manifest-inbox"
    served_store = tmp_path / "served-store.json"

    _write_json(
        state_dir / QUEUE_FILENAME,
        [{"id": "cid-1", "cid": "cid-1", "text": "a", "source": "recall"}],
    )
    _write_json(state_dir / DECISIONS_FILENAME, [])
    inbox.mkdir(parents=True, exist_ok=True)

    rc = _invoke_main(
        module,
        _bridge_argv(
            manifest_path=manifest_path,
            action="run-foreground",
            run_id="run-no-manifest",
            session_id="session-no-manifest",
            state_dir=state_dir,
            manifest_inbox=inbox,
            served_store=served_store,
            max_cycles=1,
            poll_interval_ms=5,
            heartbeat_cadence_ms=5,
        ),
    )
    capsys.readouterr()
    assert rc == 0

    decisions = _read_json(state_dir / DECISIONS_FILENAME)
    assert decisions == []


def test_bridge_status_surfaces_side_effect_timeouts(tmp_path: Path, capsys: Any) -> None:
    module = _load_run_cumulative_module()

    manifest_path = tmp_path / "runs" / "manifest.json"
    state_dir = tmp_path / "plugin-state"
    inbox = tmp_path / "manifest-inbox"
    served_store = tmp_path / "served-store.json"
    run_id = "run-timeout"
    session_id = "session-timeout"
    cid = "cid-timeout"

    _write_json(
        state_dir / QUEUE_FILENAME,
        [{"id": cid, "cid": cid, "text": "a", "source": "recall"}],
    )
    _write_json(served_store, {"version": 1, "memories": {}})

    manifest = _manifest(
        run_id=run_id,
        session_id=session_id,
        coordinator_trace="trace-timeout",
        default_fate="accept",
        decisions=(),
    )
    _write_json(
        inbox / manifest_inbox_name(run_id, SessionRecord.session_fp_of(session_id)),
        manifest.to_dict(),
    )

    run_rc = _invoke_main(
        module,
        _bridge_argv(
            manifest_path=manifest_path,
            action="run-foreground",
            run_id=run_id,
            session_id=session_id,
            state_dir=state_dir,
            manifest_inbox=inbox,
            served_store=served_store,
            max_cycles=1,
            poll_interval_ms=5,
            heartbeat_cadence_ms=5,
        ),
    )
    capsys.readouterr()
    assert run_rc == 0

    status_rc = _invoke_main(
        module,
        _bridge_argv(
            manifest_path=manifest_path,
            action="status",
            state_dir=state_dir,
            manifest_inbox=inbox,
            served_store=served_store,
        ),
    )
    status_payload = _parse_last_json_stdout(capsys)
    assert status_rc == 0
    assert cid in status_payload["side_effect_timeouts"]

    state = load_state(Path(status_payload["state_path"]))
    assert state is not None
    assert cid in state.plugin_outcome_refs["side_effect_timeouts"]


def test_primary_path_remains_prod_for_bridge_cli() -> None:
    module = _load_run_cumulative_module()
    module.assert_primary_path()
    assert str(module.config.RunConfig().primary_recall_mode).strip().lower() == "prod"


def test_bridge_child_env_prepends_repo_root_to_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_run_cumulative_module()
    repo_root = Path(__file__).resolve().parents[1]

    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = module._bridge_child_env(repo_root)
    assert env["PYTHONPATH"] == str(repo_root)

    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["/preexisting", "/other"]))
    env = module._bridge_child_env(repo_root)
    assert env["PYTHONPATH"] == os.pathsep.join(
        [str(repo_root), "/preexisting", "/other"]
    )
    # The rest of the environment is preserved.
    assert env.get("PATH") == os.environ.get("PATH")


def test_bridge_start_child_imports_wevibe_bench_without_installed_package(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate an environment where the package is not importable via any
    # inherited PYTHONPATH: the spawned child must still start because the spawn
    # derives the repo root from __file__ and injects it onto the child's
    # PYTHONPATH. Before the durable fix this child died at startup with
    # ModuleNotFoundError: No module named 'wevibe_bench'.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    module = _load_run_cumulative_module()

    manifest_path = tmp_path / "runs" / "manifest.json"
    state_dir = tmp_path / "plugin-state"
    served_store = tmp_path / "served-store.json"

    start_rc = _invoke_main(
        module,
        _bridge_argv(
            manifest_path=manifest_path,
            action="start",
            run_id="run-uninstalled-import",
            session_id="session-uninstalled-import",
            state_dir=state_dir,
            served_store=served_store,
            lease_ttl_ms=20_000,
            poll_interval_ms=50,
            heartbeat_cadence_ms=50,
        ),
    )
    start_payload = _parse_last_json_stdout(capsys)
    assert start_rc == 0

    state_path = Path(start_payload["state_path"])
    pidfile = Path(start_payload["pidfile"])
    pid = int(start_payload["pid"])

    try:
        # The child imported wevibe_bench and started the daemon: it wrote its
        # state file and refreshed the plugin heartbeat.
        assert _wait_until(lambda: state_path.exists(), timeout_s=10.0)
        heartbeat_path = state_dir / HEARTBEAT_FILENAME
        assert _wait_until(lambda: heartbeat_path.exists(), timeout_s=10.0)
        assert _pid_is_alive(pid)
    finally:
        _invoke_main(
            module,
            _bridge_argv(
                manifest_path=manifest_path,
                action="stop",
                state_dir=state_dir,
                served_store=served_store,
            ),
        )
        capsys.readouterr()
