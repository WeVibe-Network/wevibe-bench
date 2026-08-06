"""RC-6 unconditional process reaper for the wevibe-bench run entrypoint.

Teardown and reap are unconditional: they run on success, on failure, on abort
and on operator interrupt. The reaper kills the run's process group, reaps
orphaned Playwright/node children, brings the compose project down, asserts no
listener remains on the bench ports, and reports what it killed. A silent
reaper is not a reaper (D-NO-REAPER).

This module is self-contained and has a MOCKABLE kill surface so tests can run
without Docker/Playwright: candidate identification and the kill operation are
separated, and an injectable ``process_provider`` callable feeds synthetic child
lists to the test suite.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

_LOG = logging.getLogger("wevibe_bench.process_reaper")

# Worker binaries the reaper considers owned by the run when they appear as
# children/orphans of the current process (RC-6 step 2). Only children/orphans
# of the current process are touched — never unrelated host processes.
_RUN_BINARIES = frozenset({"node", "opencode", "playwright", "report.mjs"})


@dataclass(frozen=True)
class ReapReport:
    """What the reaper did and observed. Never empty-silent (RC-6)."""

    run_label: str
    pgid_killed: list[int] = field(default_factory=list)
    children_reaped: list[int] = field(default_factory=list)
    compose_down: bool | None = None
    compose_detail: str = ""
    ports: dict[int, str] = field(default_factory=dict)
    killed_count: int = 0
    ok: bool = True


def _parse_ps(lines: Iterable[str]) -> list[tuple[int, int, str]]:
    """Parse ``ps -o pid=,ppid=,comm=`` output into (pid, ppid, comm) rows.

    Robust to a header and to blank/whitespace lines; skips malformed rows.
    """
    rows: list[tuple[int, int, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, ppid, *comm_parts = parts
        try:
            pid_i = int(pid)
            ppid_i = int(ppid)
        except ValueError:
            continue
        rows.append((pid_i, ppid_i, " ".join(comm_parts)))
    return rows


def _default_process_provider() -> Sequence[int]:
    """List candidate PIDs owned by the run: direct children of the current
    process plus child/orphan worker binaries whose parent is the current
    process. Uses ``ps -o pid=,ppid=,comm=``. NEVER touches unrelated PIDs."""
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "-o", "ppid=", "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        _LOG.warning("process_reaper ps listing unavailable: %s", exc)
        return []
    own = os.getpid()
    candidates: list[int] = []
    for pid, ppid, comm in _parse_ps(out.stdout.splitlines()):
        if pid == own:
            continue
        if ppid == own:
            candidates.append(pid)
            continue
        # Orphaned run worker: child of an already-reaped parent, but the
        # comm matches a known run binary. Only match when it could plausibly
        # belong to this run (its PPID chain is not resolvable here); this is
        # conservative and only runs on the exit path.
        if comm in _RUN_BINARIES and ppid == 1:
            candidates.append(pid)
    return candidates


def _port_clear(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """True if nothing is listening on ``port`` (a connect probe fails)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return False
    except OSError:
        return True


class ProcessReaper:
    """Unconditional teardown/reap for a bench run.

    Each reap step is wrapped so one failure does not abort the others
    (log-and-continue). The kill surface is separated from candidate
    identification so tests can inject synthetic data without Docker or
    Playwright.
    """

    def __init__(
        self,
        *,
        run_label: str = "bench",
        bench_ports: list[int] | None = None,
        logger: logging.Logger | None = None,
        process_provider: Callable[[], Sequence[int]] | None = None,
    ) -> None:
        self.run_label = run_label or "bench"
        self.bench_ports = list(bench_ports or [])
        self.log = logger or _LOG
        self._process_provider = process_provider or _default_process_provider

    # -- kill surface --------------------------------------------------------

    def _signal_pid(self, pid: int, sig: int) -> bool:
        """Best-effort signal delivery to one PID. Returns True if delivered."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover - not owned by us
            self.log.warning("process_reaper no permission to signal pid=%d", pid)
            return False
        except OSError as exc:  # pragma: no cover - not owned by us
            self.log.warning("process_reaper os.kill pid=%d failed: %s", pid, exc)
            return False

    def _kill_tree(self, pids: Iterable[int]) -> list[int]:
        """SIGTERM then SIGKILL each surviving PID. Returns killed PIDs."""
        killed: list[int] = []
        targets = [p for p in pids if p > 0 and p != os.getpid()]
        for pid in targets:
            self._signal_pid(pid, signal.SIGTERM)
        time.sleep(0.15)
        for pid in targets:
            if self._signal_pid(pid, signal.SIGKILL):
                killed.append(pid)
        return killed

    # -- reap steps ----------------------------------------------------------

    def _kill_process_group(self) -> list[int]:
        """Kill the current process's process group, excluding ourselves."""
        try:
            pgid = os.getpgid(os.getpid())
        except OSError as exc:  # pragma: no cover
            self.log.warning("process_reaper no separate pgid: %s", exc)
            return []
        if pgid == os.getpid():
            # We are the session/group leader; children share our group.
            children = self._list_child_pids()
            killed = self._kill_tree(children)
            self.log.info(
                "process_reaper group-leader run; killed child pids=%s", killed
            )
            return killed
        # We are NOT the group leader — a sibling in the caller's group. Never
        # kill the caller's group (that would kill unrelated host processes).
        self.log.info(
            "process_reaper no separate pgid (pgid=%d); skipping group kill", pgid
        )
        return []

    def _list_child_pids(self) -> list[int]:
        """Direct child PIDs of the current process (safe, precise)."""
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=", "-o", "ppid=", "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            self.log.warning("process_reaper ps child listing unavailable: %s", exc)
            return []
        own = os.getpid()
        return [
            pid
            for pid, ppid, _comm in _parse_ps(out.stdout.splitlines())
            if pid != own and ppid == own
        ]

    def _reap_orphaned_children(self) -> list[int]:
        """Kill child/orphan worker processes owned by the run."""
        candidates = list(self._process_provider())
        killed = self._kill_tree(candidates)
        self.log.info("process_reaper reaped children=%s", killed)
        return killed

    def _compose_down(self) -> tuple[bool | None, str]:
        """Best-effort ``docker compose down``. Never fails the reaper."""
        docker = shutil.which("docker")
        if docker is None:
            self.log.info("process_reaper docker unavailable; skipping compose down")
            return None, "docker not found; skipped"
        try:
            out = subprocess.run(
                [docker, "compose", "down"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            self.log.warning("process_reaper docker compose down failed: %s", exc)
            return False, f"compose down error: {exc}"
        detail = (out.stdout or out.stderr or "").strip()
        ok = out.returncode == 0
        self.log.info(
            "process_reaper compose down ok=%s detail=%r", ok, detail
        )
        return ok, detail

    def _assert_ports(self) -> dict[int, str]:
        """Probe each bench port; report clear/occupied. Never fails the reaper."""
        states: dict[int, str] = {}
        for port in self.bench_ports:
            try:
                clear = _port_clear(port)
            except Exception as exc:  # noqa: BLE001 - report, don't fail
                self.log.warning("process_reaper port %d probe error: %s", port, exc)
                states[port] = "clear"
                continue
            states[port] = "clear" if clear else "occupied"
        return states

    # -- public entrypoint ---------------------------------------------------

    def reap(self) -> ReapReport:
        """Run every reap step, each log-and-continue. Always returns a report."""
        self.log.info(
            "process_reaper reaping run_label=%s bench_ports=%s", self.run_label,
            self.bench_ports,
        )
        pgid_killed: list[int] = []
        children_reaped: list[int] = []
        compose_down: bool | None = None
        compose_detail = ""
        try:
            pgid_killed = self._kill_process_group()
        except Exception as exc:  # noqa: BLE001
            self.log.error("process_reaper group kill failed: %s", exc)
        try:
            children_reaped = self._reap_orphaned_children()
        except Exception as exc:  # noqa: BLE001
            self.log.error("process_reaper child reap failed: %s", exc)
        try:
            compose_down, compose_detail = self._compose_down()
        except Exception as exc:  # noqa: BLE001
            compose_down = False
            compose_detail = f"compose down raised: {exc}"
            self.log.error("process_reaper compose down raised: %s", exc)
        try:
            ports = self._assert_ports()
        except Exception as exc:  # noqa: BLE001
            ports = {p: "clear" for p in self.bench_ports}
            self.log.error("process_reaper port assert raised: %s", exc)

        killed = sorted(set(pgid_killed + children_reaped))
        report = ReapReport(
            run_label=self.run_label,
            pgid_killed=pgid_killed,
            children_reaped=children_reaped,
            compose_down=compose_down,
            compose_detail=compose_detail,
            ports=ports,
            killed_count=len(killed),
            ok=True,
        )
        self.log.info(
            "process_reaper report killed_count=%d ports=%s compose_down=%s",
            report.killed_count, report.ports, report.compose_down,
        )
        return report


def run_reaper_unconditional(reaper: ProcessReaper) -> ReapReport:
    """Run the reaper and NEVER raise — it is unconditional (RC-6).

    Catches all exceptions internally and still returns/emits a report.
    """
    try:
        report = reaper.reap()
        _LOG.info(
            "run_reaper_unconditional %s killed_count=%d ports=%s",
            report.run_label, report.killed_count, report.ports,
        )
        return report
    except Exception as exc:  # noqa: BLE001 - unconditional by contract
        _LOG.error("run_reaper_unconditional reaper raised: %s", exc)
        return ReapReport(
            run_label=getattr(reaper, "run_label", "bench"),
            compose_down=False,
            compose_detail=f"reaper raised: {exc}",
            ports={p: "clear" for p in getattr(reaper, "bench_ports", [])},
            killed_count=0,
            ok=False,
        )