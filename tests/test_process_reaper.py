"""RC-6 reaper tests: three observations + before/after survival + ports.

Tests use REAL spawned child processes with evidence, but inject the
``process_provider`` so they never touch Docker or Playwright. The reaper must
never kill unrelated host processes — these tests only ever reap the children
they themselves spawn, and reap them again in finally as a safety net.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time

import pytest

from wevibe_bench.process_reaper import (
    ProcessReaper,
    ReapReport,
    run_reaper_unconditional,
)


def _spawn_sleeper() -> subprocess.Popen:
    """Spawn a long-sleeping child of the current process."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap_quietly(reaper: ProcessReaper) -> ReapReport:
    """Run the reaper and always ensure cleanup; never raise on leftover."""
    try:
        return run_reaper_unconditional(reaper)
    finally:
        # Safety net: kill any child we may have spawned even if the reaper
        # itself misbehaved — never leave an orphan behind on a test host.
        for pid in getattr(reaper, "_known_pids", []):
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001
                pass


def test_normal_exit_reaps():
    """NORMAL exit: a spawned child is dead after reap, and reported."""
    child = _spawn_sleeper()
    assert child.poll() is None  # alive before

    def provider() -> list[int]:
        return [child.pid]

    reaper = ProcessReaper(
        run_label="test-normal", process_provider=provider
    )
    reaper._known_pids = [child.pid]
    report = _reap_quietly(reaper)

    assert child.pid in report.children_reaped
    assert report.killed_count >= 1
    assert child.poll() is not None  # dead after reap


def test_failure_path_reaps():
    """FAILURE path: reaper runs after an exception; exception preserved."""
    child = _spawn_sleeper()
    assert child.poll() is None  # alive before

    def provider() -> list[int]:
        return [child.pid]

    reaper = ProcessReaper(
        run_label="test-failure", process_provider=provider
    )
    reaper._known_pids = [child.pid]

    class Boom(Exception):
        pass

    report = None
    original = Boom("worker blew up")
    with pytest.raises(Boom) as excinfo:
        try:
            raise original
        finally:
            report = _reap_quietly(reaper)

    # The reaper did not swallow or change the failure.
    assert excinfo.value is original
    assert report is not None
    assert child.pid in report.children_reaped
    assert report.killed_count >= 1
    assert child.poll() is not None  # dead after reap


def test_kill_interrupt_reaps():
    """OPERATOR INTERRUPT (KeyboardInterrupt): reaper runs; child reaped."""
    child = _spawn_sleeper()
    assert child.poll() is None  # alive before

    def provider() -> list[int]:
        return [child.pid]

    reaper = ProcessReaper(
        run_label="test-interrupt", process_provider=provider
    )
    reaper._known_pids = [child.pid]

    report = None
    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt()
        finally:
            report = _reap_quietly(reaper)

    assert report is not None
    assert child.pid in report.children_reaped
    assert report.killed_count >= 1
    assert child.poll() is not None  # dead after reap


def test_before_after_survival():
    """BEFORE/AFTER survival statement: alive before, dead after reap."""
    child = _spawn_sleeper()
    # BEFORE: process is alive.
    assert child.poll() is None, "child must be alive BEFORE reap"

    def provider() -> list[int]:
        return [child.pid]

    reaper = ProcessReaper(
        run_label="test-survival", process_provider=provider
    )
    reaper._known_pids = [child.pid]
    report = _reap_quietly(reaper)

    # AFTER: process is dead.
    assert child.poll() is not None, "child must be dead AFTER reap"
    assert child.pid in report.children_reaped
    assert report.killed_count >= 1


def test_ports_report():
    """PORT assertion: a clearly-free high port is reported 'clear'."""
    reaper = ProcessReaper(
        run_label="test-ports", bench_ports=[59999]
    )
    report = _reap_quietly(reaper)
    assert report.ports == {59999: "clear"}
    assert report.run_label == "test-ports"
    # No child was reaped and nothing on the host was touched.
    assert report.children_reaped == []
    assert report.killed_count == 0


def test_occupied_port_is_loud_and_recorded(caplog):
    """PORT assertion: a really-listening port is 'occupied' AND logged loudly.

    Bind a live TCP listener on an ephemeral port and let the reaper probe it.
    The port must be recorded 'occupied' in the report and an ERROR-level log
    naming the occupied port must be emitted (a silent reaper is not a reaper).
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        caplog.set_level(logging.ERROR)
        reaper = ProcessReaper(
            run_label="test-occupied", bench_ports=[port]
        )
        report = _reap_quietly(reaper)
        assert report.ports == {port: "occupied"}
        assert report.children_reaped == []
        assert any(
            rec.levelno == logging.ERROR and "occupied" in rec.message
            for rec in caplog.records
        ), "occupied port must be logged loudly at ERROR level"
    finally:
        srv.close()


def test_probe_error_is_recorded_error_not_clear(monkeypatch):
    """PORT assertion: a persistent probe error is 'error', never 'clear'.

    Force ``_port_clear`` to raise ConnectionError (the probe-error contract);
    the reaper must surface "error" for that port and must NOT mask it as a
    false "clear".
    """
    import wevibe_bench.process_reaper as pr

    port = 59998

    def always_error(
        port_, host="127.0.0.1", timeout=0.5, attempts=3, delay=0.5
    ):
        raise ConnectionError(f"port {port_} probe error")

    monkeypatch.setattr(pr, "_port_clear", always_error)
    reaper = ProcessReaper(
        run_label="test-probe-error", bench_ports=[port]
    )
    report = _reap_quietly(reaper)
    assert report.ports == {port: "error"}
    assert report.ports[port] != "clear"


def test_transient_probe_error_retries_to_clear(monkeypatch):
    """PORT assertion: a transient non-refusal probe error retries to 'clear'.

    A first-N-1 socket.timeout (an OSError, but NOT ConnectionRefusedError)
    must be retried inside ``_port_clear`` up to ``_PORT_CLEAR_ATTEMPTS``; when
    a later attempt finally gets a definitive refusal, the port recovers to
    clear. Proves bounded retry on a transient probe error never produces a
    false error — it settles on the correct final state.
    """
    import wevibe_bench.process_reaper as pr

    calls = {"n": 0}

    def flaky_conn(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < pr._PORT_CLEAR_ATTEMPTS:
            raise socket.timeout("transient probe timeout")
        raise ConnectionRefusedError("now clear")

    monkeypatch.setattr(socket, "create_connection", flaky_conn)
    # Retried internally, then recovered to clear; tiny delay keeps it fast.
    assert pr._port_clear(59997, delay=0.001) is True
    assert calls["n"] == pr._PORT_CLEAR_ATTEMPTS


def test_reap_report_fields_and_unconditional():
    """ReapReport shape + unconditional wrapper never raises."""
    report = ReapReport(run_label="x")
    assert report.pgid_killed == []
    assert report.children_reaped == []
    assert report.compose_down is None
    assert report.ports == {}
    assert report.killed_count == 0
    assert report.ok is True

    # run_reaper_unconditional always returns a report, never raises.
    child = _spawn_sleeper()

    def provider() -> list[int]:
        return [child.pid]

    reaper = ProcessReaper(run_label="x", process_provider=provider)
    reaper._known_pids = [child.pid]
    try:
        out = run_reaper_unconditional(reaper)
        assert isinstance(out, ReapReport)
        assert child.pid in out.children_reaped
        assert out.killed_count >= 1
    finally:
        try:
            os.kill(child.pid, 15)
        except ProcessLookupError:
            pass