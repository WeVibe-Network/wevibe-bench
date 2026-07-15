from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from wevibe_bench.adapters.docker_worker import (
    DockerCell,
    DockerCellConfig,
    WORKER_IMAGE,
    docker_available,
    image_exists,
)


HANG_TIMEOUT_S = 60


class SmokeFailure(RuntimeError):
    pass


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_trace_id() -> str:
    return uuid.uuid4().hex


def _sanitize(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= 240:
        return cleaned
    return cleaned[:240] + "…"


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _runs_dir() -> Path:
    return (_repo_root() / "runs").resolve()


def _run_cmd(
    argv: list[str],
    *,
    step: str,
    log: Callable[[str], None],
    timeout_s: int = HANG_TIMEOUT_S,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    log(f"PROGRESS step={step} phase=start timeout_s={timeout_s} argv={json.dumps(argv)}")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"step hung >{timeout_s}s: {step}") from exc

    dur_ms = int((time.monotonic() - started) * 1000)
    out = completed.stdout or ""
    err = completed.stderr or ""
    log(
        "PROGRESS "
        f"step={step} phase=done rc={completed.returncode} dur_ms={dur_ms} "
        f"stdout_bytes={len(out.encode('utf-8'))} stderr_bytes={len(err.encode('utf-8'))}"
    )
    if out.strip():
        log(f"PROGRESS step={step} stdout_preview={_sanitize(out)}")
    if err.strip():
        log(f"PROGRESS step={step} stderr_preview={_sanitize(err)}")

    if check and completed.returncode != 0:
        detail = _sanitize(err.strip() or out.strip() or f"exit={completed.returncode}")
        raise SmokeFailure(f"step failed: {step} rc={completed.returncode} detail={detail}")
    return completed


def _assert_not_hung(step: str, elapsed_s: float) -> None:
    if elapsed_s > HANG_TIMEOUT_S:
        raise SmokeFailure(f"step exceeded {HANG_TIMEOUT_S}s and is treated as hung: {step} ({elapsed_s:.2f}s)")


def _dummy_host_oracle(worktree: Path) -> tuple[bool, str]:
    answer_path = worktree / "answer.txt"
    if not answer_path.is_file():
        return False, "answer.txt missing"

    answer = answer_path.read_text(encoding="utf-8").strip()
    answer_fp = _fingerprint(answer)
    if answer == "SOLVED":
        return True, f"answer_len={len(answer)} answer_fp={answer_fp}"
    return False, f"unexpected answer content answer_len={len(answer)} answer_fp={answer_fp}"


def main() -> int:
    trace_id = _new_trace_id()
    runs_dir = _runs_dir()
    runs_dir.mkdir(parents=True, exist_ok=True)
    logfile = runs_dir / f"docker-isolation-smoke-{_utc_iso()}.log"

    def log(message: str) -> None:
        line = f"[{_utc_iso()}] trace={trace_id} {message}"
        print(line, flush=True)
        with logfile.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    log(f"PROGRESS step=smoke-start phase=entry logfile={logfile}")

    docker_ok, docker_detail = docker_available()
    log(
        "PROGRESS "
        f"step=preflight-docker phase=result available={str(docker_ok).lower()} detail={_sanitize(docker_detail)}"
    )
    if not docker_ok:
        print("SMOKE RESULT: FAIL", flush=True)
        print(f"SMOKE LOGFILE: {logfile}", flush=True)
        return 2

    if not image_exists(WORKER_IMAGE):
        build_cmd = f"docker build -t {WORKER_IMAGE} docker/worker"
        log(f"PROGRESS step=preflight-image phase=missing build_cmd={build_cmd}")
        print(f"Missing Docker image: {WORKER_IMAGE}", flush=True)
        print(f"Build it with: {build_cmd}", flush=True)
        print("SMOKE RESULT: FAIL", flush=True)
        print(f"SMOKE LOGFILE: {logfile}", flush=True)
        return 3

    host_golden = (_repo_root() / "tasks" / "backgammon" / "golden").resolve()
    host_runner = (_repo_root() / "scripts" / "run_backgammon.py").resolve()
    with tempfile.TemporaryDirectory(prefix="wevibe-bench-smoke-") as tmp:
        tmp_root = Path(tmp).resolve()
        worktree = tmp_root / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)

        (worktree / "index.txt").write_text("index\n", encoding="utf-8")
        (worktree / "solution.txt").write_text("placeholder\n", encoding="utf-8")
        log(
            "PROGRESS "
            f"step=seed-worktree phase=done worktree={worktree} files=2 "
            f"worktree_fp={_fingerprint(str(worktree))}"
        )

        container_name = f"wevibe-bench-smoke-{uuid.uuid4().hex[:12]}"
        cell = DockerCell(
            DockerCellConfig(
                worktree=worktree,
                memory_mode="off",
                container_name=container_name,
            )
        )

        smoke_ok = False
        failure_reason = ""

        try:
            start_t0 = time.monotonic()
            cell.__enter__()
            start_elapsed = time.monotonic() - start_t0
            _assert_not_hung("cell-start", start_elapsed)
            log(
                "PROGRESS "
                f"step=cell-start phase=done container={container_name} elapsed_s={start_elapsed:.3f}"
            )

            checks: list[tuple[str, list[str], int]] = [
                ("work-mount-exists", cell.exec_argv(["test", "-d", "/work"]), 20),
                ("seed-visible-index", cell.exec_argv(["test", "-f", "/work/index.txt"]), 20),
                ("seed-visible-solution", cell.exec_argv(["test", "-f", "/work/solution.txt"]), 20),
                ("gates-absent", cell.exec_argv(["test", "!", "-e", "/work/gates"]), 20),
                ("golden-absent", cell.exec_argv(["test", "!", "-e", "/work/golden"]), 20),
                (
                    "host-golden-path-absent",
                    cell.exec_argv(["test", "!", "-e", str(host_golden)]),
                    20,
                ),
                (
                    "host-runner-path-absent",
                    cell.exec_argv(["test", "!", "-e", str(host_runner)]),
                    20,
                ),
                (
                    "oracle-scripts-absent-anywhere",
                    cell.exec_argv(
                        [
                            "sh",
                            "-lc",
                            "if find / \\( -name report.mjs -o -name run.mjs \\) 2>/dev/null | grep -q .; "
                            "then exit 1; fi",
                        ]
                    ),
                    45,
                ),
                (
                    "users-not-mounted",
                    cell.exec_argv(
                        ["sh", "-lc", "if grep -q ' /Users ' /proc/self/mountinfo; then exit 1; fi"]
                    ),
                    20,
                ),
                # --- TASK-4 Docker runtime fixes: no-paid-model regression probes ---
                # fix1: HOME + /tmp tmpfs are writable by the non-root worker (mode=1777).
                (
                    "home-tmpfs-writable",
                    cell.exec_argv(
                        ["sh", "-lc", 'echo probe > "$HOME/.smoke-probe" && test -s "$HOME/.smoke-probe"']
                    ),
                    20,
                ),
                (
                    "tmp-tmpfs-writable",
                    cell.exec_argv(["sh", "-lc", "echo probe > /tmp/.smoke-probe && test -s /tmp/.smoke-probe"]),
                    20,
                ),
                # fix2: XDG + opencode state dirs resolve UNDER the writable HOME (not the read-only /etc/xdg).
                (
                    "xdg-state-under-home",
                    cell.exec_argv(
                        [
                            "sh",
                            "-lc",
                            'for v in "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$OPENCODE_CONFIG_DIR"; do '
                            'case "$v" in "$HOME"/*) ;; *) echo "not under HOME: $v"; exit 1;; esac; done',
                        ]
                    ),
                    20,
                ),
                # fix2: the redirected opencode config dir is actually writable (the .gitignore-write path).
                (
                    "opencode-config-dir-writable",
                    cell.exec_argv(
                        [
                            "sh",
                            "-lc",
                            'mkdir -p "$OPENCODE_CONFIG_DIR" && echo x > "$OPENCODE_CONFIG_DIR/.smoke-probe" '
                            '&& test -s "$OPENCODE_CONFIG_DIR/.smoke-probe"',
                        ]
                    ),
                    20,
                ),
                # fix2: the baked config file on the read-only root stays READABLE.
                (
                    "baked-opencode-config-readable",
                    cell.exec_argv(["test", "-r", "/etc/xdg/opencode/opencode.json"]),
                    20,
                ),
            ]

            for check_name, argv, timeout_s in checks:
                _run_cmd(argv, step=f"check-{check_name}", log=log, timeout_s=timeout_s)
                log(f"PROGRESS step=check-{check_name} phase=result result=PASS")

            # fix3: an in-container opencode STARTUP probe must initialize cleanly under the
            # redirected env — NO EACCES / read-only FileSystem.writeFile (the prior blockers).
            # `--version` is a no-model, no-network startup path (NOT `opencode run`, which is
            # the paid benchmark path). The writable-config-dir property itself is asserted by
            # the `opencode-config-dir-writable` probe above.
            oc = _run_cmd(
                cell.exec_argv(["opencode", "--version"]),
                step="opencode-startup",
                log=log,
                timeout_s=60,
            )
            oc_combined = (oc.stdout or "") + (oc.stderr or "")
            for forbidden in ("EACCES", "FileSystem.writeFile", "read-only file system", "EROFS", "Permission denied"):
                if forbidden in oc_combined:
                    raise SmokeFailure(
                        f"opencode startup emitted forbidden error {forbidden!r}: {_sanitize(oc_combined)}"
                    )
            log("PROGRESS step=opencode-startup phase=result result=PASS")

            mounts_raw = _run_cmd(
                ["docker", "inspect", container_name, "--format", "{{json .Mounts}}"],
                step="inspect-mounts",
                log=log,
                timeout_s=20,
            ).stdout.strip()
            mounts = json.loads(mounts_raw or "[]")
            if not isinstance(mounts, list) or not mounts:
                raise SmokeFailure("docker inspect returned empty/non-list mounts")

            destinations = {str(mount.get("Destination", "")) for mount in mounts}
            if destinations != {"/work"}:
                raise SmokeFailure(f"unexpected mount destinations: {sorted(destinations)}")
            log("PROGRESS step=inspect-mounts phase=result result=PASS")

            _run_cmd(
                cell.exec_argv(["sh", "-lc", "echo SOLVED > /work/answer.txt"]),
                step="write-answer",
                log=log,
                timeout_s=20,
            )
            _run_cmd(
                cell.exec_argv(["test", "-f", "/work/answer.txt"]),
                step="verify-answer-in-container",
                log=log,
                timeout_s=20,
            )

            smoke_ok = True
        except Exception as exc:  # noqa: BLE001 - smoke must convert every failure to explicit FAIL output.
            failure_reason = str(exc)
            log(f"PROGRESS step=smoke phase=error detail={_sanitize(failure_reason)}")
            smoke_ok = False
        finally:
            teardown_t0 = time.monotonic()
            cell.teardown()
            teardown_elapsed = time.monotonic() - teardown_t0
            try:
                _assert_not_hung("cell-teardown", teardown_elapsed)
            except SmokeFailure as exc:
                log(f"PROGRESS step=cell-teardown phase=error detail={_sanitize(str(exc))}")
                smoke_ok = False
                if not failure_reason:
                    failure_reason = str(exc)
            else:
                log(
                    "PROGRESS "
                    f"step=cell-teardown phase=done container={container_name} elapsed_s={teardown_elapsed:.3f}"
                )

        if smoke_ok:
            oracle_ok, oracle_detail = _dummy_host_oracle(worktree)
            log(
                "PROGRESS "
                f"step=dummy-oracle phase=done pass={str(oracle_ok).lower()} detail={_sanitize(oracle_detail)}"
            )
            smoke_ok = oracle_ok
            if not oracle_ok:
                failure_reason = oracle_detail

        if smoke_ok:
            print("SMOKE RESULT: PASS", flush=True)
            print(f"SMOKE LOGFILE: {logfile}", flush=True)
            return 0

        log(f"PROGRESS step=smoke-end phase=fail reason={_sanitize(failure_reason or 'unknown failure')}")
        print("SMOKE RESULT: FAIL", flush=True)
        print(f"SMOKE LOGFILE: {logfile}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
