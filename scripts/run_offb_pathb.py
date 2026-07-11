#!/usr/bin/env python3
"""Path-B OFF benchmark scorer for polyglot exercises.

Runs opencode in pure mode by default (plugins OFF / no memory), executes the
exercise's own tests, and appends one JSON row per exercise to a scorecard.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Any


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run_timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def safe_slug_for_filename(slug: str) -> str:
    out = slug.replace("/", "_").replace("\\", "_").replace(" ", "_")
    return out or "unknown"


def to_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_tokens(tokens: Any) -> dict[str, Any]:
    data = tokens if isinstance(tokens, dict) else {}
    cache = data.get("cache") if isinstance(data.get("cache"), dict) else {}
    return {
        "input": to_int(data.get("input")),
        "output": to_int(data.get("output")),
        "reasoning": to_int(data.get("reasoning")),
        "cache": {
            "read": to_int(cache.get("read")),
            "write": to_int(cache.get("write")),
        },
    }


def total_tokens(tokens: dict[str, Any]) -> int:
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    return (
        to_int(tokens.get("input"))
        + to_int(tokens.get("output"))
        + to_int(tokens.get("reasoning"))
        + to_int(cache.get("read"))
        + to_int(cache.get("write"))
    )


def tail_combined(stdout: Any, stderr: Any, max_lines: int = 40) -> str:
    left = stdout if isinstance(stdout, str) else ""
    right = stderr if isinstance(stderr, str) else ""
    combined = f"{left}\n{right}" if left and right else (left or right)
    if not combined:
        return ""
    lines = combined.splitlines()
    return "\n".join(lines[-max_lines:])


def kill_process_group(proc: subprocess.Popen[str], logger: "RunLogger", slug: str) -> None:
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
        logger.log(f"[{slug}] killed process group pgid={pgid}")
    except ProcessLookupError:
        return
    except Exception:
        logger.log(f"[{slug}] failed killing process group:\n{traceback.format_exc()}")


def build_test_command(lang: str, test_python: str, custom: dict[str, Any]) -> list[str] | None:
    if lang == "python":
        return [test_python, "-m", "pytest", "-q"]
    if lang == "go":
        return ["go", "test", "./..."]
    if lang == "rust":
        cmd = ["cargo", "test"]
        if bool(custom.get("test-in-release-mode")):
            cmd.append("--release")
        return cmd
    if lang == "javascript":
        return ["npm", "test"]
    if lang == "java":
        return ["./gradlew", "test"]
    if lang == "cpp":
        return ["sh", "-c", "cmake -B build && cmake --build build && ctest --test-dir build"]
    return None


def render_progress(i: int, n: int, row: dict[str, Any]) -> str:
    if row["not_scored"]:
        pass_part = f"NS({row['reason']})"
    else:
        pass_part = str(bool(row["pass"]))
    token_part = row["total_tokens"] if row["total_tokens"] is not None else "NA"
    work_part = row["work_tokens"] if row["work_tokens"] is not None else "NA"
    return (
        f"[offb {i}/{n}] {row['slug']} pass={pass_part} turns={row['turns']} "
        f"tokens={token_part} work={work_part} dur_run={row['duration_opencode_s']:.1f}s "
        f"dur_test={row['duration_test_s']:.1f}s"
    )


class RunLogger:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8")

    def log(self, message: str) -> None:
        line = f"{iso_now()} {message}"
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
            finally:
                self._fh.close()


def score_exercise(
    *,
    repo: str,
    lang: str,
    slug: str,
    model: str,
    agent: str,
    pure: bool,
    runs_dir: str,
    test_python: str,
    run_timeout: int,
    test_timeout: int,
    token_cap: int,
    logger: RunLogger,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": iso_now(),
        "lang": lang,
        "slug": slug,
        "model": model,
        "agent": agent,
        "pure": pure,
        "not_scored": False,
        "reason": None,
        "pass": None,
        "turns": 0,
        "token_source": "stream_estimate",
        "tokens": normalize_tokens({}),
        "total_tokens": None,
        "work_tokens": None,
        "cost": None,
        "session_id": None,
        "test_exit": None,
        "test_tail": "",
        "duration_opencode_s": 0.0,
        "duration_test_s": 0.0,
        "workdir": "",
        "events_file": "",
    }

    exdir = os.path.join(repo, lang, "exercises", "practice", slug)
    docs_dir = os.path.join(exdir, ".docs")
    meta_dir = os.path.join(exdir, ".meta")
    instructions_path = os.path.join(docs_dir, "instructions.md")
    instructions_append_path = os.path.join(docs_dir, "instructions.append.md")
    config_path = os.path.join(meta_dir, "config.json")

    if not (os.path.isfile(instructions_path) and os.path.isfile(config_path)):
        row["not_scored"] = True
        row["reason"] = "missing_exercise_files"
        logger.log(f"[{slug}] missing required files under {exdir}")
        return row

    op_proc: subprocess.Popen[str] | None = None

    try:
        with open(instructions_path, "r", encoding="utf-8") as fh:
            prompt = fh.read()
        if os.path.isfile(instructions_append_path):
            with open(instructions_append_path, "r", encoding="utf-8") as fh:
                prompt += "\n\n" + fh.read()

        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        files_cfg = cfg.get("files") if isinstance(cfg.get("files"), dict) else {}
        solution_files = files_cfg.get("solution") if isinstance(files_cfg.get("solution"), list) else []
        test_files = files_cfg.get("test") if isinstance(files_cfg.get("test"), list) else []
        custom = cfg.get("custom") if isinstance(cfg.get("custom"), dict) else {}

        solution_csv = ", ".join(str(x) for x in solution_files) if solution_files else "(none declared)"
        prompt += (
            "\n\n"
            f"Implement your solution in the following file(s): {solution_csv}. "
            "Do not modify the test files. When you are done, all tests in this project must pass."
        )

        pathb_work_dir = os.path.join(runs_dir, "pathb-work")
        os.makedirs(pathb_work_dir, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix=f"offb-{lang}-{safe_slug_for_filename(slug)}-", dir=pathb_work_dir)
        row["workdir"] = workdir

        shutil.copytree(exdir, workdir, dirs_exist_ok=True)
        shutil.rmtree(os.path.join(workdir, ".meta"), ignore_errors=True)

        events_path = os.path.join(
            runs_dir,
            f"offb-pathb-{safe_slug_for_filename(slug)}-{run_timestamp()}.events.jsonl",
        )
        row["events_file"] = events_path

        opencode_cmd = [
            "opencode",
            "run",
            prompt,
            "--model",
            model,
            "--agent",
            agent,
            "--dir",
            workdir,
            "--dangerously-skip-permissions",
            "--format",
            "json",
        ]
        if pure:
            opencode_cmd.append("--pure")

        cmd_for_log = opencode_cmd.copy()
        cmd_for_log[2] = f"<prompt:{len(prompt)} chars>"
        logger.log(
            f"[{slug}] start workdir={workdir} prompt_chars={len(prompt)} "
            f"solution_files={solution_files} test_files={test_files}"
        )
        logger.log(f"[{slug}] opencode argv={shlex.join(cmd_for_log)}")

        state_lock = threading.Lock()
        state: dict[str, Any] = {
            "session_id": None,
            "turns": 0,
            "sum_output": 0,
            "sum_reasoning": 0,
            "max_input": 0,
        }
        stderr_tail: collections.deque[str] = collections.deque(maxlen=120)
        reader_failures: list[str] = []

        with open(events_path, "w", encoding="utf-8") as events_fh:
            op_proc = subprocess.Popen(
                opencode_cmd,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )

            def stdout_reader() -> None:
                try:
                    assert op_proc is not None and op_proc.stdout is not None
                    for line in op_proc.stdout:
                        events_fh.write(line)
                        events_fh.flush()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        sid = event.get("sessionID")
                        with state_lock:
                            if sid and not state["session_id"]:
                                state["session_id"] = str(sid)

                        if event.get("type") != "step_finish":
                            continue

                        part = event.get("part") if isinstance(event.get("part"), dict) else {}
                        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                        input_toks = max(0, to_int(tokens.get("input")))
                        output_toks = max(0, to_int(tokens.get("output")))
                        reasoning_toks = max(0, to_int(tokens.get("reasoning")))

                        with state_lock:
                            state["turns"] += 1
                            state["sum_output"] += output_toks
                            state["sum_reasoning"] += reasoning_toks
                            if input_toks > state["max_input"]:
                                state["max_input"] = input_toks
                except Exception:
                    reader_failures.append(f"[{slug}] stdout reader failure:\n{traceback.format_exc()}")
                finally:
                    try:
                        if op_proc and op_proc.stdout:
                            op_proc.stdout.close()
                    except Exception:
                        pass

            def stderr_reader() -> None:
                try:
                    assert op_proc is not None and op_proc.stderr is not None
                    for line in op_proc.stderr:
                        txt = line.rstrip("\n")
                        stderr_tail.append(txt)
                        logger.log(f"[{slug}] opencode stderr: {txt}")
                except Exception:
                    reader_failures.append(f"[{slug}] stderr reader failure:\n{traceback.format_exc()}")
                finally:
                    try:
                        if op_proc and op_proc.stderr:
                            op_proc.stderr.close()
                    except Exception:
                        pass

            t_out = threading.Thread(target=stdout_reader, name=f"offb-stdout-{slug}", daemon=True)
            t_err = threading.Thread(target=stderr_reader, name=f"offb-stderr-{slug}", daemon=True)
            t_out.start()
            t_err.start()

            killed_reason: str | None = None
            run_started = time.monotonic()

            try:
                while True:
                    rc = op_proc.poll()
                    now = time.monotonic()
                    elapsed = now - run_started
                    with state_lock:
                        est = state["sum_output"] + state["sum_reasoning"] + state["max_input"]
                    if rc is not None:
                        break
                    if elapsed > run_timeout:
                        killed_reason = "run_timeout"
                        logger.log(
                            f"[{slug}] run timeout after {elapsed:.2f}s (limit {run_timeout}s); "
                            f"est_tokens={est}; terminating"
                        )
                        kill_process_group(op_proc, logger, slug)
                        break
                    if est > token_cap:
                        killed_reason = "token_cap"
                        logger.log(
                            f"[{slug}] token cap exceeded est={est} > cap={token_cap}; terminating"
                        )
                        kill_process_group(op_proc, logger, slug)
                        break
                    time.sleep(2.0)
            except KeyboardInterrupt:
                logger.log(f"[{slug}] keyboard interrupt during opencode run")
                kill_process_group(op_proc, logger, slug)
                raise

            if op_proc.poll() is None:
                try:
                    op_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    kill_process_group(op_proc, logger, slug)
                    op_proc.wait(timeout=5)

            t_out.join(timeout=5)
            t_err.join(timeout=5)

            row["duration_opencode_s"] = round(time.monotonic() - run_started, 3)

        for failure in reader_failures:
            logger.log(failure)

        with state_lock:
            session_id = state["session_id"]
            turns = to_int(state["turns"])
            sum_output = to_int(state["sum_output"])
            sum_reasoning = to_int(state["sum_reasoning"])
            max_input = to_int(state["max_input"])

        est_tokens = {
            "input": max_input,
            "output": sum_output,
            "reasoning": sum_reasoning,
            "cache": {"read": 0, "write": 0},
        }
        est_total = total_tokens(est_tokens)

        row["session_id"] = session_id
        row["turns"] = turns
        row["tokens"] = est_tokens
        row["total_tokens"] = est_total
        row["work_tokens"] = (
            to_int(est_tokens.get("input"))
            + to_int(est_tokens.get("output"))
            + to_int(est_tokens.get("reasoning"))
        )
        row["token_source"] = "stream_estimate"

        exit_code = op_proc.returncode if op_proc is not None else None
        logger.log(
            f"[{slug}] opencode exit={exit_code} session_id={session_id} "
            f"turns={turns} est_total_tokens={est_total}"
        )

        if killed_reason:
            row["not_scored"] = True
            row["reason"] = killed_reason
            return row

        if exit_code not in (0, None) and not session_id:
            row["not_scored"] = True
            row["reason"] = "opencode_error"
            if stderr_tail:
                logger.log(f"[{slug}] opencode stderr tail:\n" + "\n".join(stderr_tail))
            return row

        if session_id:
            export_ok = False
            export_failure_reason = "unknown"
            for attempt in range(1, 3):
                export_path: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tmp_fh:
                        export_path = tmp_fh.name
                        export_proc = subprocess.run(
                            ["opencode", "export", session_id],
                            stdout=tmp_fh,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=60,
                            check=False,
                        )

                    if export_proc.returncode != 0:
                        stderr_txt = (export_proc.stderr or "").strip()
                        raise RuntimeError(f"rc={export_proc.returncode}; stderr={stderr_txt}")

                    if not export_path:
                        raise RuntimeError("missing export temp path")

                    with open(export_path, "r", encoding="utf-8") as fh:
                        payload = json.load(fh)

                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    if "tokens" not in info or info.get("tokens") is None:
                        raise RuntimeError("export payload missing info.tokens")

                    export_tokens = normalize_tokens(info.get("tokens"))
                    row["tokens"] = export_tokens
                    row["total_tokens"] = total_tokens(export_tokens)
                    row["work_tokens"] = (
                        to_int(export_tokens.get("input"))
                        + to_int(export_tokens.get("output"))
                        + to_int(export_tokens.get("reasoning"))
                    )
                    row["token_source"] = "export"
                    cost = info.get("cost")
                    row["cost"] = float(cost) if cost is not None else None
                    export_ok = True
                    break
                except Exception as exc:
                    export_failure_reason = str(exc) or exc.__class__.__name__
                    logger.log(
                        f"[{slug}] opencode export attempt {attempt}/2 failed; "
                        f"fallback_pending={attempt < 2}; reason={export_failure_reason}"
                    )
                    if attempt < 2:
                        time.sleep(1.5)
                finally:
                    if export_path and os.path.exists(export_path):
                        try:
                            os.remove(export_path)
                        except Exception:
                            logger.log(f"[{slug}] failed to delete temp export file {export_path}")

            if not export_ok:
                logger.log(
                    f"[{slug}] opencode export unavailable after 2 attempts; "
                    f"using stream_estimate tokens; reason={export_failure_reason}"
                )

        test_cmd = build_test_command(lang, test_python, custom)
        if test_cmd is None:
            row["not_scored"] = True
            row["reason"] = "unsupported_lang"
            return row

        logger.log(f"[{slug}] test command: {shlex.join(test_cmd)}")
        test_started = time.monotonic()
        try:
            test_proc = subprocess.run(
                test_cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=test_timeout,
                check=False,
            )
            row["duration_test_s"] = round(time.monotonic() - test_started, 3)
            row["test_exit"] = test_proc.returncode
            row["pass"] = test_proc.returncode == 0
            row["test_tail"] = "" if row["pass"] else tail_combined(test_proc.stdout, test_proc.stderr)
        except subprocess.TimeoutExpired as exc:
            row["duration_test_s"] = round(time.monotonic() - test_started, 3)
            row["pass"] = False
            row["reason"] = "test_timeout"
            row["test_exit"] = None
            row["test_tail"] = tail_combined(exc.stdout, exc.stderr)

        logger.log(
            f"[{slug}] test_exit={row['test_exit']} pass={row['pass']} "
            f"duration_test_s={row['duration_test_s']:.3f} reason={row['reason']}"
        )
        return row

    except KeyboardInterrupt:
        logger.log(f"[{slug}] keyboard interrupt; cleaning up")
        if op_proc is not None:
            kill_process_group(op_proc, logger, slug)
        raise
    except Exception:
        logger.log(f"[{slug}] exception:\n{traceback.format_exc()}")
        if op_proc is not None:
            kill_process_group(op_proc, logger, slug)
        row["not_scored"] = True
        row["reason"] = "exception"
        row["pass"] = None
        return row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Path-B OFF scorer over polyglot benchmark exercises.")
    parser.add_argument(
        "--repo",
        default=os.path.expanduser("~/Desktop/wevibe-workspace/wevibe-bench/polyglot-benchmark"),
        help="Path to polyglot-benchmark root",
    )
    parser.add_argument("--lang", default="python", help="Exercise language")
    parser.add_argument("--slug", action="append", required=True, help="Exercise slug (repeatable)")
    parser.add_argument("--model", default="opencode/big-pickle", help="Model for opencode run")
    parser.add_argument("--agent", default="build", help="Agent for opencode run")
    parser.add_argument(
        "--runs-dir",
        default=os.path.expanduser("~/Desktop/benchmark/runs"),
        help="Directory for scorecards, logs, and workdirs",
    )
    parser.add_argument(
        "--test-python",
        default=sys.executable,
        help="Python interpreter used for python exercise tests",
    )
    parser.add_argument("--run-timeout", type=int, default=600, help="Per-exercise opencode timeout (seconds)")
    parser.add_argument("--test-timeout", type=int, default=240, help="Per-exercise test timeout (seconds)")
    parser.add_argument("--token-cap", type=int, default=120000, help="Live token estimate cap")
    parser.add_argument(
        "--no-pure",
        action="store_true",
        help="Disable --pure for opencode run (default keeps pure mode on)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    pure = not args.no_pure

    os.makedirs(args.runs_dir, exist_ok=True)
    runts = run_timestamp()
    scorecard_path = os.path.join(args.runs_dir, f"offb-pathb-scorecard-{runts}.jsonl")
    log_path = os.path.join(args.runs_dir, f"offb-pathb-run-{runts}.log")

    logger = RunLogger(log_path)
    rows: list[dict[str, Any]] = []

    opencode_template = [
        "opencode",
        "run",
        "<prompt>",
        "--model",
        args.model,
        "--agent",
        args.agent,
        "--dir",
        "<workdir>",
        "--dangerously-skip-permissions",
        "--format",
        "json",
    ]
    if pure:
        opencode_template.append("--pure")

    logger.log("OFFB Path-B run start")
    logger.log(
        f"repo={args.repo} lang={args.lang} model={args.model} agent={args.agent} "
        f"pure={pure} runs_dir={args.runs_dir}"
    )
    logger.log(f"slugs={args.slug}")
    logger.log(
        f"token_cap={args.token_cap} run_timeout={args.run_timeout}s test_timeout={args.test_timeout}s "
        f"test_python={args.test_python}"
    )
    logger.log(f"opencode argv template: {shlex.join(opencode_template)}")

    try:
        with open(scorecard_path, "a", encoding="utf-8") as score_fh:
            total = len(args.slug)
            for i, slug in enumerate(args.slug, start=1):
                row = score_exercise(
                    repo=args.repo,
                    lang=args.lang,
                    slug=slug,
                    model=args.model,
                    agent=args.agent,
                    pure=pure,
                    runs_dir=args.runs_dir,
                    test_python=args.test_python,
                    run_timeout=args.run_timeout,
                    test_timeout=args.test_timeout,
                    token_cap=args.token_cap,
                    logger=logger,
                )
                score_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                score_fh.flush()

                rows.append(row)
                progress = render_progress(i, total, row)
                print(progress, flush=True)
                logger.log(progress)

        n_pass = sum(1 for r in rows if not r["not_scored"] and r["pass"] is True)
        n_fail = sum(1 for r in rows if not r["not_scored"] and r["pass"] is False)
        n_not_scored = sum(1 for r in rows if r["not_scored"])

        summary = {
            "scorecard": scorecard_path,
            "log": log_path,
            "runts": runts,
            "n": len(rows),
            "n_pass": n_pass,
            "n_fail": n_fail,
            "n_not_scored": n_not_scored,
            "rows": [
                {
                    "slug": r["slug"],
                    "pass": r["pass"],
                    "turns": r["turns"],
                    "total_tokens": r["total_tokens"],
                    "work_tokens": r["work_tokens"],
                    "reason": r["reason"],
                }
                for r in rows
            ],
        }
        done_line = "OFFB_PATHB_DONE " + json.dumps(summary, ensure_ascii=False)
        print(done_line, flush=True)
        logger.log(done_line)
        return 0
    except KeyboardInterrupt:
        logger.log("KeyboardInterrupt in main; exiting")
        return 130
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
