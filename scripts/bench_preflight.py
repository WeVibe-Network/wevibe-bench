#!/usr/bin/env python3
"""One-command preflight for a benchmark run.

WHY THIS EXISTS
---------------
Starting a cell requires ~6 checks that were previously scattered across
RUNBOOK §0/§2.1/§7 and the workspace AGENTS.md. Doing them by hand costs an
operator (or an agent) a long, error-prone discovery pass every single time,
and the most important check — asserting the clone identities at the seam —
was effectively undiscoverable because `/v1/identity/pubkeys` is bearer-gated
and returns `{"status":"error","error":"unauthorized"}` to a plain curl.

Run this instead. It prints a GO / NO-GO verdict and, on GO, the exact
launch command with `< /dev/null` and the flag ordering already correct.

    .venv/bin/python scripts/bench_preflight.py --model qwen3.6-35b-a3b-bench

Exit codes: 0 = GO, 1 = NO-GO (a blocking check failed).

This script only READS and reports. It never archives, wipes, launches, or
mutates anything — those stay operator decisions (RUNBOOK §0 step 3a, §2).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The expected served identities. Source: workspace AGENTS.md §2.1 (SETTLED
# 2026-08-11) + RUNBOOK §7. These are fp(ed_pubkey_bytes) — NOT fp(seed_bytes),
# which is a DIFFERENT value for the same identity (leader seed fp = f534aa02).
# Comparing a fingerprint without naming its hashed input is how a real
# identity mismatch got wrongly dismissed once already.
EXPECTED_LEADER_FP = "f7733d6e"
EXPECTED_CONTRIB_FP = "5292550d"

# :4450 is the operator's REAL host wevibe-mcp (interactive keychain identity).
# Pointing any bench component at it mints orgs under the operator's identity
# and the run dies ~30s later with "leader membership did not include org_id".
FORBIDDEN_PORT = 4450


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str, bool]] = []

    def add(self, name: str, ok: bool, detail: str, blocking: bool = True) -> None:
        self.rows.append((name, ok, detail, blocking))

    @property
    def blocking_failures(self) -> list[tuple[str, bool, str, bool]]:
        return [r for r in self.rows if not r[1] and r[3]]

    def render(self) -> None:
        width = max(len(r[0]) for r in self.rows)
        for name, ok, detail, blocking in self.rows:
            mark = "PASS" if ok else ("FAIL" if blocking else "WARN")
            print(f"  [{mark}] {name.ljust(width)}  {detail}")


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_ports(c: Check) -> None:
    for port, what in ((4545, "local relay"), (4550, "leader clone MCP"),
                       (4451, "contributor clone MCP"), (4440, "hub")):
        ok = port_open(port)
        hint = "" if ok else "  -> see RUNBOOK §7 for bring-up"
        c.add(f"port {port} ({what})", ok, ("open" if ok else "CLOSED") + hint)


def check_identity(c: Check) -> None:
    """Assert identity AT THE SEAM. Liveness is not identity (AGENTS.md §2.1).

    A port answering and health returning 200 proved nothing in two separate
    real failures. Anything that mints, signs, or attributes must have its
    identity asserted, never inferred.
    """
    try:
        sys.path.insert(0, str(REPO))
        from wevibe_bench.lifecycle.lconfig import LifecycleConfig
        from wevibe_bench.lifecycle.mcp_rest import McpRest
    except Exception as exc:  # noqa: BLE001
        c.add("identity assertion", False, f"cannot import lifecycle client: {exc}")
        return

    logging.basicConfig(level=logging.CRITICAL)
    log = logging.getLogger("preflight")
    cfg = LifecycleConfig()

    for label, url, expected in (
        ("leader", cfg.leader_mcp_url, EXPECTED_LEADER_FP),
        ("contributor", cfg.contributor_mcp_url, EXPECTED_CONTRIB_FP),
    ):
        if f":{FORBIDDEN_PORT}" in url:
            c.add(
                f"identity {label}",
                False,
                f"{url} TARGETS THE OPERATOR HOST MCP :{FORBIDDEN_PORT} — "
                "this mints orgs under the operator's keychain identity. "
                "See AGENTS.md §2.1.",
            )
            continue
        try:
            payload = McpRest(url, cfg, log).identity_pubkeys()
            ed = (
                payload.get("ed25519")
                or payload.get("ed25519_pubkey")
                or payload.get("edPubkey")
                or ""
            )
            fp = hashlib.sha256(bytes.fromhex(ed)).hexdigest()[:8] if ed else ""
            if not fp:
                c.add(f"identity {label}", False, f"{url} no ed25519 key in response")
            elif fp == expected:
                c.add(f"identity {label}", True, f"{url} fp(ed_pubkey)={fp}")
            else:
                c.add(
                    f"identity {label}",
                    False,
                    f"{url} fp(ed_pubkey)={fp} EXPECTED {expected} — wrong identity "
                    "on the seam; a run on an unverified seam is VOID-INSTRUMENT "
                    "(RUNBOOK §6)",
                )
        except Exception as exc:  # noqa: BLE001
            # Unreachable is a HARD failure, never a skip (AGENTS.md §2.1).
            c.add(f"identity {label}", False, f"{url} UNREACHABLE/failed: {exc}")


def check_image(c: Check) -> None:
    if shutil.which("docker") is None:
        c.add("worker image", False, "docker not on PATH")
        return
    proc = subprocess.run(
        ["docker", "image", "inspect", "wevibe-bench-worker:v1", "--format", "{{.Created}}"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        c.add("worker image", False,
              "wevibe-bench-worker:v1 MISSING -> docker build -t wevibe-bench-worker:v1 docker/worker")
        return
    created = proc.stdout.strip()

    worker_dir = REPO / "docker" / "worker"
    newest = 0.0
    newest_path = ""
    for path in worker_dir.rglob("*"):
        if path.is_file():
            mtime = path.stat().st_mtime
            if mtime > newest:
                newest, newest_path = mtime, str(path.relative_to(REPO))

    import datetime as _dt
    img_ts = _dt.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    stale = newest > img_ts
    # The vendored opencode plugin is baked in at build time, so a stale image
    # silently runs a stale plugin — no error, just wrong behaviour.
    c.add(
        "worker image",
        not stale,
        f"built {created[:19]}"
        + (f" but {newest_path} is NEWER -> rebuild: docker build -t wevibe-bench-worker:v1 docker/worker"
           if stale else " (newer than docker/worker/ — no rebuild needed)"),
    )


def check_run_dir(c: Check) -> None:
    """runs/cumulative must be ABSENT. Archive, never delete (RUNBOOK §0 3a)."""
    cumulative = REPO / "runs" / "cumulative"
    if not cumulative.exists():
        c.add("runs/cumulative", True, "absent — clean slate")
        return
    stamp = "$(date +%Y%m%dT%H%M%S)"
    c.add(
        "runs/cumulative",
        False,
        "EXISTS — a prior campaign occupies the slot. ARCHIVE IT, NEVER DELETE:\n"
        f"         mv runs/cumulative runs/cumulative.<why>-{stamp}\n"
        "         (the server corpus is untouched; RUNBOOK §2 governs that)",
    )


def check_disk(c: Check) -> None:
    usage = shutil.disk_usage(REPO)
    free_gb = usage.free / 1e9
    c.add("disk free", free_gb > 10,
          f"{free_gb:.1f} GB free" + ("" if free_gb > 10 else " — LOW, a run can fill this"),
          blocking=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight for a benchmark cell.")
    ap.add_argument("--model", default="qwen3.6-35b-a3b-bench",
                    help="subject model alias pinned for the run")
    ap.add_argument("--mode", default="off", choices=("off", "on"))
    ap.add_argument("--org", default=None, help="org id (ON cells only)")
    args = ap.parse_args()

    print("\nBENCH PREFLIGHT  (RUNBOOK §0 + AGENTS.md §2.1)\n")
    c = Check()
    check_ports(c)
    check_identity(c)
    check_image(c)
    check_run_dir(c)
    check_disk(c)
    c.render()

    failures = c.blocking_failures
    if failures:
        print(f"\nNO-GO — {len(failures)} blocking check(s) failed. Resolve the above, re-run.\n")
        return 1

    on_flags = f" --mode on --org {args.org or '<org>'}" if args.mode == "on" else " --mode off"
    print("\nGO — all blocking checks passed. Launch:\n")
    print("  TS=$(date +%Y%m%dT%H%M%S) && nohup .venv/bin/python scripts/run_cumulative.py \\")
    print(f"    --model {args.model} run --until-review{on_flags} \\")
    print('    < /dev/null > "runs/off-cell-$TS.log" 2>&1 & disown')
    print("\n  `< /dev/null` is MANDATORY (zsh suspends the job on stdin touch).")
    print("  Main-parser flags go BEFORE `run` — argparse exits 2 otherwise.")
    print("  --model must match on EVERY later subcommand or you get 'roster hash drift'.\n")
    print("  Then:  grep -E 'session_id|attach_cmd' runs/off-cell-<ts>.log | tail -3\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
