"""RC-6 reaper tests: three observations + before/after survival + ports.

Tests use REAL spawned child processes with evidence, but inject the
``process_provider`` so they never touch Docker or Playwright. The reaper must
never kill unrelated host processes — these tests only ever reap the children
they themselves spawn, and reap them again in finally as a safety net.
"""

from __future__ import annotations

import os
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