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
    _default_process_provider,
    _is_run_binary,
    _parse_ps,
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


def test_cell_container_sweep_is_scoped_to_run_label(monkeypatch):
    """The container sweep filters by THIS reaper's run label, never the bare
    ``wevibe-bench-cell-`` prefix. Regression: an unscoped sweep force-removed
    other xdist workers' live docker-isolation cells mid-test (the recurring
    'container is not running' flake class)."""
    import wevibe_bench.process_reaper as pr

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(pr.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(pr.subprocess, "run", fake_run)

    reaper = ProcessReaper(run_label="my-run-42")
    removed = reaper._remove_cell_containers()

    assert removed == []
    assert calls == [
        ["/fake/docker", "ps", "-aq", "--filter", "name=wevibe-bench-cell-my-run-42"]
    ]


def test_reap_report_fields_and_unconditional():
    """ReapReport shape + unconditional wrapper never raises."""
    report = ReapReport(run_label="x")
    assert report.pgid_killed == []
    assert report.children_reaped == []
    assert report.cell_containers_removed == []
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

# ── THE ORPHAN BRANCH ────────────────────────────────────────────────────────
#
# Every other test in this file injects a `process_provider`, so
# `_default_process_provider` — the function that decides what is reapable on a
# real host — was never exercised, and a bug in it was invisible to the whole
# suite. It carried one: `ps -o comm=` prints an ABSOLUTE PATH on darwin, so the
# orphan match was False for every real worker and the branch never fired. These
# tests run against real `ps` output shapes rather than an injected list.


@pytest.mark.parametrize(
    "comm",
    [
        "node",
        "opencode",
        "playwright",
        "report.mjs",
        "/opt/homebrew/opt/node@22/bin/node",
        "/opt/homebrew/Cellar/node@22/22.22.2_2/bin/node",
        "/usr/local/bin/opencode",
        "/some/path/to/playwright",
    ],
)
def test_run_binaries_match_bare_name_and_absolute_path(comm):
    """The measured darwin failure: comm is a path, not a basename."""
    assert _is_run_binary(comm) is True


@pytest.mark.parametrize(
    "comm",
    [
        "-zsh",
        "/bin/bash",
        "/usr/bin/python3",
        "postgres",
        "/Applications/Docker.app/Contents/MacOS/Docker",
        "nodemon",  # NOT node: the basename must match whole, never as a prefix
        "/usr/bin/node-inspector",
        "",
    ],
)
def test_unrelated_binaries_are_never_reapable(comm):
    """The match must not widen. An unrelated host process is never a candidate."""
    assert _is_run_binary(comm) is False


def test_default_provider_finds_an_orphaned_worker_by_absolute_path(monkeypatch):
    """An orphan (ppid == 1) named by absolute path is a candidate.

    This is the exact row shape measured on this host, and the case that burned
    341 CPU-minutes on 2026-08-12: the gate's npm/vitest/playwright workers
    reparent to PID 1 when their parent dies, and the reaper walked past them.
    """
    own = os.getpid()
    ps_out = "\n".join(
        [
            f" {own}  1660 /usr/bin/python3",
            " 4001     1 /opt/homebrew/Cellar/node@22/22.22.2_2/bin/node",
            " 4002     1 /usr/local/bin/opencode",
            " 4003     1 /bin/bash",  # orphan, but not ours — must be skipped
            " 4004  9999 /opt/homebrew/opt/node@22/bin/node",  # ours? no: not an orphan, not our child
            f" 4005 {own} /usr/bin/python3",  # direct child — always a candidate
        ]
    )

    class _Result:
        stdout = ps_out

    monkeypatch.setattr(
        "wevibe_bench.process_reaper.subprocess.run",
        lambda *a, **k: _Result(),
    )
    pids = set(_default_process_provider())

    assert 4001 in pids, "an orphaned node named by absolute path must be reaped"
    assert 4002 in pids, "an orphaned opencode named by absolute path must be reaped"
    assert 4005 in pids, "a direct child is reaped regardless of its binary"
    assert 4003 not in pids, "an unrelated orphan must never be touched"
    assert 4004 not in pids, "a live process owned by another parent is not ours"
    assert own not in pids, "the reaper must never list itself"


def test_parse_ps_keeps_absolute_paths_intact():
    rows = _parse_ps(
        [
            "",
            " 1660  1649 -zsh",
            " 2247  2242 /opt/homebrew/opt/node@22/bin/node",
            "garbage",
            " x y z",
        ]
    )
    assert rows == [
        (1660, 1649, "-zsh"),
        (2247, 2242, "/opt/homebrew/opt/node@22/bin/node"),
    ]
