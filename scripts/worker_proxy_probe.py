from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wevibe_bench.adapters.backgammon import build_worker_opencode_config
from wevibe_bench.adapters.docker_worker import DockerCell, DockerCellConfig, _build_run_argv
from wevibe_bench.spend_key import (
    key_fingerprint,
    resolve_orcarouter_api_key,
    resolve_worker_spend_proxy_base_url,
)


DEFAULT_MODEL = "orcarouter/kimi/kimi-k3"
DEFAULT_PROMPT = (
    "Use the bash tool to run exactly: echo wevibe-worker-proxy-probe-ok "
    "— then reply with the command output and stop."
)
PROBE_MARKER = "wevibe-worker-proxy-probe-ok"


@dataclass(slots=True)
class ProbeVerdict:
    transport_ok: bool
    tool_call_seen: bool
    tool_output_contains_probe_marker: bool
    session_id: str | None
    stop_reason: str | None
    error: str | None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_stamp_file() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("worker_proxy_probe")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("[%(asctime)sZ] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    if stream.formatter is not None:
        stream.formatter.converter = time.gmtime
    logger.addHandler(stream)
    return logger


def _add_file_handler(logger: logging.Logger, log_path: Path) -> None:
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)sZ] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    if fh.formatter is not None:
        fh.formatter.converter = time.gmtime
    logger.addHandler(fh)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _create_run_dirs(repo_root: Path) -> tuple[Path, Path]:
    run_dir = (repo_root / "runs" / "probe" / f"worker-proxy-{_utc_stamp_file()}").resolve()
    worktree = run_dir / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    return run_dir, worktree


def _build_cell_config(*, worktree: Path, container_name: str, worker_base_url: str, token: str) -> DockerCellConfig:
    session_db_dir = worktree.parent / "session-db"
    session_db_dir.mkdir(parents=True, exist_ok=True)
    cfg = DockerCellConfig(
        worktree=worktree,
        memory_mode="off",
        container_name=container_name,
    )
    cfg.session_db_host_path = session_db_dir
    cfg.plugin_state_host_path = str(worktree / ".wevibe" / "state")
    cfg.output_token_max = None
    cfg.proxy_base_url = worker_base_url
    cfg.proxy_token = token
    cfg.worker_logs_dir = worktree.parent / "worker-logs"
    return cfg


def _parse_events(events_path: Path, probe_marker: str) -> ProbeVerdict:
    saw_step_finish = False
    saw_text = False
    saw_bash_tool = False
    saw_marker = False
    stop_reason: str | None = None
    session_id: str | None = None
    error_summary: str | None = None

    for raw in events_path.read_text(encoding="utf-8").splitlines():
        if probe_marker in raw:
            saw_marker = True
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue

        sid = event.get("sessionID")
        if isinstance(sid, str) and sid and session_id is None:
            session_id = sid

        event_type = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}

        if event_type == "error" and error_summary is None:
            msg = part.get("text") or event.get("message") or event.get("error") or "error event"
            error_summary = str(msg)

        if event_type == "tool_use":
            tool_name = part.get("tool") or part.get("name")
            if isinstance(tool_name, str) and tool_name.strip().lower() == "bash":
                saw_bash_tool = True

        if event_type == "text":
            saw_text = True
            text_value = part.get("text")
            if isinstance(text_value, str) and probe_marker in text_value:
                saw_marker = True

        if event_type == "step_finish":
            saw_step_finish = True
            reason = part.get("reason")
            if isinstance(reason, str):
                stop_reason = reason
            text_value = part.get("text")
            if isinstance(text_value, str) and probe_marker in text_value:
                saw_marker = True

    transport_ok = error_summary is None and (saw_step_finish or saw_text)
    return ProbeVerdict(
        transport_ok=transport_ok,
        tool_call_seen=saw_bash_tool,
        tool_output_contains_probe_marker=saw_marker,
        session_id=session_id,
        stop_reason=stop_reason,
        error=error_summary,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe worker-cell reachability to spend proxy via one opencode turn")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout-s", type=int, default=180)
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logger = _setup_logger()
    logger.info("step=resolve-secrets status=start")
    token, token_source = resolve_orcarouter_api_key()
    worker_base_url = resolve_worker_spend_proxy_base_url()
    token_fp = key_fingerprint(token)
    logger.info(
        "step=resolve-secrets status=ok token_source=%s token_fp=%s worker_base_url=%s",
        token_source,
        token_fp,
        worker_base_url,
    )

    repo_root = _repo_root()
    run_dir, worktree = _create_run_dirs(repo_root)
    probe_log = run_dir / "probe.log"
    _add_file_handler(logger, probe_log)
    logger.info(
        "step=resolve-secrets-captured status=ok token_source=%s token_fp=%s worker_base_url=%s",
        token_source,
        token_fp,
        worker_base_url,
    )
    logger.info("step=run-dir status=ok run_dir=%s worktree=%s", run_dir, worktree)

    (worktree / "README.md").write_text("worker proxy probe scratch worktree\n", encoding="utf-8")
    logger.info("step=seed-worktree status=ok readme=%s", worktree / "README.md")

    task_dir = (repo_root / "tasks" / "backgammon").resolve()
    gates_dir = str((task_dir / "gates").resolve())
    golden_dir = str((task_dir / "golden").resolve())
    config = build_worker_opencode_config(
        model=args.model,
        reasoning_effort=None,
        proxy_base_url=worker_base_url,
        gates_dir=gates_dir,
        golden_dir=golden_dir,
        session_id=None,
    )
    opencode_path = worktree / "opencode.json"
    opencode_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    logger.info("step=write-opencode-config status=ok path=%s", opencode_path)

    container_name = f"wevibe-bench-probe-worker-proxy-{_utc_stamp_file().lower()}"
    cell_cfg = _build_cell_config(
        worktree=worktree,
        container_name=container_name,
        worker_base_url=worker_base_url,
        token=token,
    )

    run_argv = _build_run_argv(
        config=cell_cfg,
        worktree=worktree,
        uid=os.getuid() if hasattr(os, "getuid") else 0,
        gid=os.getgid() if hasattr(os, "getgid") else 0,
        memory_mode="off",
    )

    if args.dry_run:
        print("DRY_RUN worker_base_url:", worker_base_url)
        print("DRY_RUN docker_run_argv:", json.dumps(run_argv))
        print("DRY_RUN opencode_json_path:", str(opencode_path))
        print("DRY_RUN opencode_json_contents:")
        print(opencode_path.read_text(encoding="utf-8"), end="")
        logger.info("step=dry-run status=ok run_dir=%s container_name=%s", run_dir, container_name)
        return 0

    inner_argv = [
        "opencode",
        "run",
        "--model",
        args.model,
        "--agent",
        "build",
        "--dir",
        "/work",
        "--format",
        "json",
        "--pure",
    ]
    exec_cmd = [*DockerCell(cell_cfg).exec_argv(inner_argv)]
    events_path = run_dir / "events.jsonl"

    logger.info("step=probe-exec status=start container_name=%s timeout_s=%d", container_name, args.timeout_s)

    if args.keep_container:
        cell = DockerCell(cell_cfg)
        cell.__enter__()
        logger.info("step=container-enter status=ok container_name=%s keep_container=true", container_name)
        completed = subprocess.run(
            exec_cmd,
            input=args.prompt,
            text=True,
            capture_output=True,
            timeout=args.timeout_s,
            check=False,
        )
        events_path.write_text(completed.stdout or "", encoding="utf-8")
    else:
        with DockerCell(cell_cfg) as cell:
            completed = subprocess.run(
                cell.exec_argv(inner_argv),
                input=args.prompt,
                text=True,
                capture_output=True,
                timeout=args.timeout_s,
                check=False,
            )
            events_path.write_text(completed.stdout or "", encoding="utf-8")

    stderr_path = run_dir / "opencode.stderr.log"
    stderr_path.write_text(completed.stderr or "", encoding="utf-8")
    logger.info(
        "step=probe-exec status=done rc=%d stdout_bytes=%d stderr_bytes=%d events=%s",
        completed.returncode,
        len((completed.stdout or "").encode("utf-8")),
        len((completed.stderr or "").encode("utf-8")),
        events_path,
    )

    verdict = _parse_events(events_path, PROBE_MARKER)
    logger.info(
        "step=verdict transport_ok=%s tool_call_seen=%s marker_seen=%s session_id=%s stop_reason=%s error=%s",
        verdict.transport_ok,
        verdict.tool_call_seen,
        verdict.tool_output_contains_probe_marker,
        verdict.session_id or "none",
        verdict.stop_reason or "none",
        verdict.error or "none",
    )

    print("VERDICT")
    print(f"transport_ok: {str(verdict.transport_ok).lower()}")
    print(f"tool_call_seen: {str(verdict.tool_call_seen).lower()}")
    print(f"tool_output_contains_probe_marker: {str(verdict.tool_output_contains_probe_marker).lower()}")
    print(f"session_id: {verdict.session_id or 'none'}")
    print(f"stop_reason: {verdict.stop_reason or 'none'}")
    print(f"error: {verdict.error or 'none'}")
    print(f"worker_base_url: {worker_base_url}")
    print(f"container_name: {container_name}")
    print(f"run_dir: {run_dir}")

    if verdict.error is not None or not verdict.transport_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
