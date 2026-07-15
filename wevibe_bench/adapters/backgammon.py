"""Backgammon worker-runner adapter for the benchmark harness.

This adapter drives a single backgammon cell end-to-end:
- seed a fresh worktree from scaffold
- run either a mock worker (golden/scaffold copy) or headless opencode
- evaluate with the backgammon gate report runner
- apply up to 3 rounds of *problems-only* feedback in the same session
"""

from __future__ import annotations

import collections
from contextlib import nullcontext
from dataclasses import dataclass
import datetime as _dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from wevibe_bench.adapters.aider_polyglot import _format_memory
from wevibe_bench.adapters.cheat_detector import (
    build_oracle_markers,
    scan_events_for_oracle_access,
)
from .docker_worker import (
    DockerCell,
    DockerCellConfig,
    WORKER_IMAGE,
    docker_available,
    image_exists,
)
from wevibe_bench.backends.base import NeedCard, RecalledMemory
from wevibe_bench.runner import AgentRunner, TaskOutcome


BACKGAMMON_PROMPT = (
    "Build me a fully functioning backgammon game that runs on localhost. "
    "When the server is started I should be able to navigate to the URL, start "
    "a game, and play against AI. The game should have all of the makings of a "
    "complete product, with 0 errors and a fully functioning backend. Build it "
    "in Node + TypeScript."
)

BACKGAMMON_REQUIREMENTS: tuple[str, ...] = (
    "Include a doubling cube with AI accept/decline reasoning.",
    "Keep the viewport compact.",
    "Support easy, medium, and hard AI.",
    "Show pip count.",
    "Use standard board orientation.",
    "Provide smooth checker movement and dice animation.",
    "Allow only legal moves, show legal destinations and die attribution, and show a no-legal-move pass notice.",
    "Implement full turn flow including doubles -> 4 moves, use-both-dice, hitting -> bar, bar re-entry before other moves, and bear-off.",
    "Detect and show win/gammon/backgammon, and allow starting a new game without reload.",
    "Run on PORT 8002 and fail with a clear message if the port is taken.",
)


@dataclass(frozen=True)
class _OpencodeRunStats:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    turns: int
    session_id: str | None
    killed_reason: str | None
    exit_code: int | None


@dataclass
class BackgammonCellResult:
    verdict: str
    attempts_to_green: int | str
    conformed: bool
    input_tokens: int
    output_tokens: int
    turns: int
    wall_seconds: float
    delivery: str
    failed_gates: list[str]
    problems_final: list[dict[str, Any]]
    attempt_reports: list[dict[str, Any]]
    worktree: str
    session_id: str | None
    memory_mode: str
    model: str
    cheated: bool = False
    cheat_detail: str = ""


def _default_progress(message: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[bg] {stamp} {message}", flush=True)


class BackgammonRunner(AgentRunner):
    def __init__(
        self,
        *,
        task_dir: Path,
        work_root: Path,
        model: str,
        memory_mode: str = "off",
        mock: str | None = None,
        max_attempts: int = 3,
        token_cap: int = 200000,
        run_timeout_s: int = 1200,
        agent: str = "build",
        logger: Any = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.task_dir = Path(task_dir).expanduser().resolve()
        self.work_root = Path(work_root).expanduser().resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)

        self.model = str(model)
        self.memory_mode = str(memory_mode)
        self.mock = mock
        self.max_attempts = int(max_attempts)
        self.token_cap = int(token_cap)
        self.run_timeout_s = int(run_timeout_s)
        self.agent = str(agent)

        self.logger = logger
        self._progress_cb = progress or _default_progress
        self._repo_root = Path(__file__).resolve().parents[2]

        if self.memory_mode not in {"off", "on"}:
            raise ValueError("memory_mode must be 'off' or 'on'")
        if self.mock not in {None, "golden", "scaffold"}:
            raise ValueError("mock must be one of: None, 'golden', 'scaffold'")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.token_cap < 1:
            raise ValueError("token_cap must be >= 1")
        if self.run_timeout_s < 1:
            raise ValueError("run_timeout_s must be >= 1")

    def build_need_card(self, task_id: str) -> NeedCard:
        intent = "debug" if "debug" in task_id.lower() else "build"
        return NeedCard(
            intent=intent,
            task="build a complete playable backgammon game with Node + TypeScript and backend APIs",
            language="typescript",
            stack=["backgammon", "node", "typescript"],
        )

    def run_cell(self, run_label: str, run_dir: Path, task_id: str = "backgammon") -> BackgammonCellResult:
        return self._run_cell_impl(
            run_label=run_label,
            run_dir=run_dir,
            task_id=task_id,
            injected_memory=[],
        )

    def run_task(self, model: str, task_id: str, injected_memory: list[RecalledMemory]) -> TaskOutcome:
        selected_model = str(model or self.model)
        original_model = self.model
        self.model = selected_model
        try:
            with tempfile.TemporaryDirectory(prefix="bg-run-task-", dir=str(self.work_root)) as temp_dir:
                result = self._run_cell_impl(
                    run_label=f"run-task-{task_id}",
                    run_dir=Path(temp_dir),
                    task_id=task_id,
                    injected_memory=injected_memory,
                )
        finally:
            self.model = original_model

        return TaskOutcome(
            resolved=(result.verdict == "PASS"),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            turns=result.turns,
            wall_cost_usd=0.0,
            wall_seconds=result.wall_seconds,
        )

    def _run_cell_impl(
        self,
        *,
        run_label: str,
        run_dir: Path,
        task_id: str,
        injected_memory: list[RecalledMemory],
    ) -> BackgammonCellResult:
        started = time.monotonic()
        run_dir = Path(run_dir).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)

        worktree = run_dir / "worktree"
        if worktree.exists():
            shutil.rmtree(worktree)
        worktree.mkdir(parents=True, exist_ok=True)

        self._copy_tree_contents(self.task_dir / "scaffold", worktree)
        self._progress(
            f"PROGRESS run_label={run_label} step=worktree-seed src={self.task_dir / 'scaffold'} dst={worktree}"
        )

        pure = self._prepare_memory_mode(worktree=worktree)
        run_env = os.environ.copy()

        session_id: str | None = None
        input_tokens_total = 0
        output_tokens_total = 0
        turns_total = 0
        events_path = Path(f"{worktree}.events.jsonl")

        attempt_reports: list[dict[str, Any]] = []
        final_report: dict[str, Any] = {}
        verdict = "FAIL"
        attempts_to_green: int | str = "FAIL"

        worker_killed_reason: str | None = None
        active_cell: DockerCell | None = None
        cell_context: Any = nullcontext()

        if self.mock in {"golden", "scaffold"}:
            mock_src = self.task_dir / str(self.mock)
            self._copy_tree_contents(mock_src, worktree)
            self._progress(
                f"PROGRESS run_label={run_label} step=worker-launch mode=mock mock={self.mock}"
            )
        else:
            docker_ok, docker_detail = docker_available()
            if not docker_ok:
                raise RuntimeError(
                    "Docker required for isolated worker; "
                    f"docker preflight failed: {docker_detail}"
                )
            if not image_exists():
                raise RuntimeError(
                    "Docker worker image missing. "
                    "Build it with: docker build -t wevibe-bench-worker:v1 docker/worker"
                )

            sanitized_label = re.sub(r"[^a-zA-Z0-9_.-]", "-", run_label)
            container_name = f"wevibe-bench-cell-{sanitized_label}"
            stale_rm = subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            stale_detail = (stale_rm.stderr or stale_rm.stdout or "").strip()
            if stale_rm.returncode == 0:
                self._progress(
                    "PROGRESS run_label="
                    f"{run_label} step=docker-stale-remove name={container_name} detail={stale_detail or 'removed'}"
                )
            elif "no such container" in stale_detail.lower():
                self._progress(
                    f"PROGRESS run_label={run_label} step=docker-stale-remove name={container_name} detail=already-absent"
                )
            else:
                raise RuntimeError(
                    f"failed to remove stale docker container name={container_name}: "
                    f"{stale_detail or f'exit={stale_rm.returncode}'}"
                )

            self._progress(
                f"PROGRESS run_label={run_label} step=worker-isolation isolation=docker "
                f"image={WORKER_IMAGE} memory_mode={self.memory_mode} container={container_name}"
            )
            cell_context = DockerCell(
                DockerCellConfig(
                    worktree=worktree,
                    memory_mode=self.memory_mode,
                    container_name=container_name,
                ),
                progress=self._progress,
            )

        with cell_context as managed_cell:
            if self.mock is None:
                if not isinstance(managed_cell, DockerCell):
                    raise RuntimeError("docker worker context did not yield a DockerCell")
                active_cell = managed_cell

                task_prompt = self._build_task_prompt(injected_memory=injected_memory)
                self._progress(
                    f"PROGRESS run_label={run_label} step=worker-launch-start mode=real model={self.model} "
                    f"pure={pure} prompt_chars={len(task_prompt)}"
                )
                self._write_worker_permission_config(worktree=worktree)
                initial_inner = [
                    "opencode",
                    "run",
                    task_prompt,
                    "--model",
                    self.model,
                    "--agent",
                    self.agent,
                    "--dir",
                    "/work",
                    "--format",
                    "json",
                ]
                if pure:
                    initial_inner.append("--pure")

                first_run = self._run_opencode(
                    cmd=active_cell.exec_argv(initial_inner),
                    worktree=worktree,
                    events_path=events_path,
                    env=run_env,
                    run_label=run_label,
                    phase="initial",
                    fallback_session_id=None,
                    kill_hook=active_cell.force_kill,
                )
                session_id = first_run.session_id
                input_tokens_total += first_run.input_tokens
                output_tokens_total += first_run.output_tokens + first_run.reasoning_tokens
                turns_total += first_run.turns
                worker_killed_reason = first_run.killed_reason
                self._progress(
                    f"PROGRESS run_label={run_label} step=worker-launch-end mode=real "
                    f"exit={first_run.exit_code} killed={first_run.killed_reason or 'none'} "
                    f"turns={first_run.turns} input={first_run.input_tokens} "
                    f"output={first_run.output_tokens} reasoning={first_run.reasoning_tokens} "
                    f"session_id={session_id or 'none'}"
                )

            for attempt in range(1, self.max_attempts + 1):
                report_json = run_dir / f"attempt-{attempt}-report.json"
                gate_log = run_dir / f"attempt-{attempt}-gate.log"
                self._progress(
                    f"PROGRESS run_label={run_label} step=gate-attempt-start attempt={attempt} target={worktree}"
                )
                report = self._run_gate_report(
                    worktree=worktree,
                    report_path=report_json,
                    log_path=gate_log,
                )
                final_report = report

                attempt_verdict = str(report.get("verdict", "FAIL"))
                conformed = bool(report.get("conformed", False))
                problems = report.get("problems") if isinstance(report.get("problems"), list) else []
                failed_gates_raw = report.get("failed_gates")
                failed_gates = [str(item) for item in failed_gates_raw] if isinstance(failed_gates_raw, list) else []

                attempt_reports.append(
                    {
                        "attempt": attempt,
                        "verdict": attempt_verdict,
                        "conformed": conformed,
                        "n_problems": len(problems),
                        "failed_gates": failed_gates,
                    }
                )
                self._progress(
                    f"PROGRESS gate attempt={attempt} verdict={attempt_verdict} "
                    f"conformed={conformed} problems={len(problems)}"
                )

                if attempt_verdict == "PASS":
                    verdict = "PASS"
                    attempts_to_green = attempt - 1
                    break

                if worker_killed_reason is not None:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-stop attempt={attempt} "
                        f"reason=container-dead killed={worker_killed_reason}"
                    )
                    verdict = "FAIL"
                    attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                    break

                if attempt < self.max_attempts:
                    if self.mock is not None:
                        self._progress(
                            f"PROGRESS run_label={run_label} step=feedback-skip attempt={attempt} reason=mock_mode"
                        )
                        continue

                    if active_cell is None:
                        raise RuntimeError("feedback loop requires an active docker cell")

                    if not session_id:
                        raise RuntimeError("feedback loop requires a session_id, but none was captured")

                    feedback_checks = [
                        str(p.get("check", "")).strip()
                        for p in problems
                        if isinstance(p, dict) and str(p.get("check", "")).strip()
                    ]
                    feedback = self._build_feedback_prompt(checks=feedback_checks)
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-problems-only-built attempt={attempt} "
                        f"checks={len(feedback_checks)}"
                    )
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-injection attempt={attempt} "
                        f"problem_count={len(problems)} session_id={session_id}"
                    )
                    self._write_worker_permission_config(worktree=worktree)
                    feedback_inner = [
                        "opencode",
                        "run",
                        feedback,
                        "--session",
                        session_id,
                        "--dir",
                        "/work",
                        "--format",
                        "json",
                    ]
                    if pure:
                        feedback_inner.append("--pure")

                    feedback_run = self._run_opencode(
                        cmd=active_cell.exec_argv(feedback_inner),
                        worktree=worktree,
                        events_path=events_path,
                        env=run_env,
                        run_label=run_label,
                        phase=f"feedback-{attempt}",
                        fallback_session_id=session_id,
                        kill_hook=active_cell.force_kill,
                    )
                    if feedback_run.session_id:
                        session_id = feedback_run.session_id

                    input_tokens_total += feedback_run.input_tokens
                    output_tokens_total += feedback_run.output_tokens + feedback_run.reasoning_tokens
                    turns_total += feedback_run.turns
                    if feedback_run.killed_reason:
                        worker_killed_reason = feedback_run.killed_reason
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-injection-done attempt={attempt} "
                        f"exit={feedback_run.exit_code} killed={feedback_run.killed_reason or 'none'} "
                        f"turns={feedback_run.turns} input={feedback_run.input_tokens} "
                        f"output={feedback_run.output_tokens} reasoning={feedback_run.reasoning_tokens}"
                    )
                    continue

                verdict = "FAIL"
                attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                break

        wall_seconds = time.monotonic() - started
        problems_final = self._normalize_problems(final_report.get("problems"))
        failed_gates_final = self._normalize_string_list(final_report.get("failed_gates"))
        oracle_markers = build_oracle_markers(
            gates_dir=(self.task_dir / "gates").resolve(),
            golden_dir=(self.task_dir / "golden").resolve(),
        )
        cheat_finding = scan_events_for_oracle_access(events_path=events_path, markers=oracle_markers)
        cheated = cheat_finding.cheated
        cheat_detail = cheat_finding.summary()
        if cheated:
            verdict = "CHEAT"
            cheat_marker = run_dir / "CHEAT.json"
            cheat_marker.write_text(
                json.dumps(
                    {
                        "run_label": run_label,
                        "verdict": "CHEAT",
                        "summary": cheat_detail,
                        "hits": [
                            {
                                "tool": hit.tool,
                                "marker": hit.marker,
                                "call_id": hit.call_id,
                                "excerpt": hit.excerpt,
                            }
                            for hit in cheat_finding.hits
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if self.logger is not None:
                error = getattr(self.logger, "error", None)
                if callable(error):
                    error(
                        "CHEAT DETECTED run_label=%s verdict=CHEAT summary=%s hits=%s",
                        run_label,
                        cheat_detail,
                        len(cheat_finding.hits),
                    )
            self._progress(
                f"PROGRESS run_label={run_label} step=cheat-detected verdict=CHEAT "
                f"hits={len(cheat_finding.hits)} summary={cheat_detail} marker={cheat_marker}"
            )

        return BackgammonCellResult(
            verdict=verdict,
            attempts_to_green=attempts_to_green,
            conformed=bool(final_report.get("conformed", False)),
            input_tokens=input_tokens_total,
            output_tokens=output_tokens_total,
            turns=turns_total,
            wall_seconds=wall_seconds,
            delivery="N/A",
            failed_gates=failed_gates_final,
            problems_final=problems_final,
            attempt_reports=attempt_reports,
            worktree=str(worktree),
            session_id=session_id,
            memory_mode=self.memory_mode,
            model=self.model,
            cheated=cheated,
            cheat_detail=cheat_detail,
        )

    def _write_worker_permission_config(self, *, worktree: Path) -> None:
        gates_dir = str((self.task_dir / "gates").resolve())
        golden_dir = str((self.task_dir / "golden").resolve())
        config = {
            "$schema": "https://opencode.ai/config.json",
            "permission": {
                "*": "allow",
                "external_directory": {"*": "deny"},
                "bash": {
                    "*": "allow",
                    f"*{gates_dir}*": "deny",
                    f"*{golden_dir}*": "deny",
                    "*report.mjs*": "deny",
                    "*run.mjs*": "deny",
                },
                "edit": {"*": "allow", "*opencode.json": "deny"},
                "doom_loop": "deny",
                "question": "deny",
            },
        }
        (worktree / "opencode.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        self._progress(
            "PROGRESS step=worker-permission-config external_directory=deny "
            "oracle_bash_deny=active skip_permissions_removed=true"
        )

    def _prepare_memory_mode(self, *, worktree: Path) -> bool:

        if self.memory_mode == "on":
            source_org = self._repo_root / ".wevibe" / "org.json"
            if not source_org.is_file():
                raise FileNotFoundError(f"missing required memory marker: {source_org}")

            marker_dir = worktree / ".wevibe"
            marker_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_org, marker_dir / "org.json")
            self._progress(
                f"PROGRESS step=memory-mode mode=on marker={marker_dir / 'org.json'} "
                "recall_env_injection=container"
            )
            return False

        shutil.rmtree(worktree / ".wevibe", ignore_errors=True)
        self._progress("PROGRESS step=memory-mode mode=off pure=true")
        return True

    def _build_task_prompt(self, *, injected_memory: list[RecalledMemory]) -> str:
        contract_path = self.task_dir / "CONTRACT.md"
        contract_text = contract_path.read_text(encoding="utf-8")
        requirements_text = "\n".join(f"- {item}" for item in BACKGAMMON_REQUIREMENTS)
        base_prompt = (
            f"{BACKGAMMON_PROMPT}\n\n"
            "Requirements:\n"
            f"{requirements_text}\n\n"
            "Contract:\n"
            f"{contract_text}"
        )

        s_prompt_path = self._repo_root / "scaffold" / "sxe-candidate" / "S-fork-reasoning.md"
        if not s_prompt_path.is_file():
            raise RuntimeError(
                f"producer capture/compliance protocol missing: {s_prompt_path}"
            )
        s_prompt_text = s_prompt_path.read_text(encoding="utf-8")
        if not s_prompt_text.strip():
            raise RuntimeError(
                f"producer capture/compliance protocol empty: {s_prompt_path}"
            )
        self._progress(
            f"PROGRESS step=producer-s-load path={s_prompt_path} chars={len(s_prompt_text)}"
        )
        base_prompt = (
            f"{base_prompt}\n\n"
            "=== CAPTURE & COMPLIANCE PROTOCOL ===\n"
            f"{s_prompt_text}"
        )

        if self.memory_mode == "on":
            return base_prompt

        memory_blob = _format_memory(injected_memory)
        if not memory_blob:
            return base_prompt
        return f"{memory_blob}\n{base_prompt}"

    @staticmethod
    def _build_feedback_prompt(*, checks: list[str]) -> str:
        header = (
            "The following gate checks are failing. Fix the implementation so they pass. "
            "Do not explain, just edit the code."
        )
        deduped: list[str] = []
        seen: set[str] = set()
        for item in checks:
            first_line = str(item).split("\n", 1)[0]
            sanitized = " ".join(first_line.split())
            if len(sanitized) > 120:
                sanitized = sanitized[:120]
            if not sanitized or sanitized in seen:
                continue
            seen.add(sanitized)
            deduped.append(sanitized)

        lines: list[str] = [header, ""]
        if not deduped:
            lines.append("- (gate runner reported FAIL with no itemised checks): FAILING")
            return "\n".join(lines)

        for label in deduped:
            lines.append(f"- {label}: FAILING")
        return "\n".join(lines)

    def _run_gate_report(self, *, worktree: Path, report_path: Path, log_path: Path) -> dict[str, Any]:
        gate_cmd = [
            "node",
            "report.mjs",
            "--target",
            str(worktree.resolve()),
            "--out",
            str(report_path.resolve()),
        ]
        gate_started = time.monotonic()
        completed = subprocess.run(
            gate_cmd,
            cwd=str((self.task_dir / "gates").resolve()),
            capture_output=True,
            text=True,
            check=False,
        )
        gate_wall = time.monotonic() - gate_started

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(
                [
                    f"cmd: {gate_cmd}",
                    f"cwd: {(self.task_dir / 'gates').resolve()}",
                    f"exit: {completed.returncode}",
                    f"wall_seconds: {gate_wall:.3f}",
                    "--- stdout ---",
                    completed.stdout,
                    "--- stderr ---",
                    completed.stderr,
                    "",
                ]
            ),
            encoding="utf-8",
        )

        if not report_path.is_file():
            raise RuntimeError(
                f"gate report missing at {report_path} (exit={completed.returncode})"
            )

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"gate report must be an object: {report_path}")
        return payload

    def _run_opencode(
        self,
        *,
        cmd: list[str],
        worktree: Path,
        events_path: Path,
        env: dict[str, str],
        run_label: str,
        phase: str,
        fallback_session_id: str | None,
        kill_hook: Callable[[], None] | None = None,
    ) -> _OpencodeRunStats:
        state_lock = threading.Lock()
        state: dict[str, Any] = {
            "session_id": fallback_session_id,
            "turns": 0,
            "sum_output": 0,
            "sum_reasoning": 0,
            "max_input": 0,
        }
        stderr_tail: collections.deque[str] = collections.deque(maxlen=120)
        reader_failures: list[str] = []

        started = time.monotonic()
        events_path.parent.mkdir(parents=True, exist_ok=True)

        with events_path.open("a", encoding="utf-8") as events_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
                env=env,
            )

            def stdout_reader() -> None:
                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
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
                        input_tokens = max(0, self._to_int(tokens.get("input")))
                        output_tokens = max(0, self._to_int(tokens.get("output")))
                        reasoning_tokens = max(0, self._to_int(tokens.get("reasoning")))

                        with state_lock:
                            state["turns"] += 1
                            state["sum_output"] += output_tokens
                            state["sum_reasoning"] += reasoning_tokens
                            if input_tokens > state["max_input"]:
                                state["max_input"] = input_tokens
                except Exception as exc:  # noqa: BLE001 - log and continue teardown.
                    reader_failures.append(f"stdout reader failure ({phase}): {exc}")
                finally:
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass

            def stderr_reader() -> None:
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        text = line.rstrip("\n")
                        stderr_tail.append(text)
                        self._progress(
                            f"PROGRESS run_label={run_label} step=worker-stderr phase={phase} line={text}"
                        )
                except Exception as exc:  # noqa: BLE001 - log and continue teardown.
                    reader_failures.append(f"stderr reader failure ({phase}): {exc}")
                finally:
                    try:
                        if proc.stderr:
                            proc.stderr.close()
                    except Exception:
                        pass

            stdout_thread = threading.Thread(target=stdout_reader, name=f"bg-stdout-{phase}", daemon=True)
            stderr_thread = threading.Thread(target=stderr_reader, name=f"bg-stderr-{phase}", daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            killed_reason: str | None = None
            kill_hook_ran = False

            def run_kill_hook(*, reason: str) -> None:
                nonlocal kill_hook_ran
                if kill_hook is None or kill_hook_ran:
                    return
                kill_hook_ran = True
                try:
                    kill_hook()
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill-hook phase={phase} "
                        f"reason={reason} status=ok"
                    )
                except Exception as exc:  # noqa: BLE001 - surface and continue teardown.
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill-hook phase={phase} "
                        f"reason={reason} status=error detail={exc}"
                    )

            while True:
                rc = proc.poll()
                elapsed = time.monotonic() - started
                with state_lock:
                    est_tokens = state["sum_output"] + state["sum_reasoning"] + state["max_input"]

                if rc is not None:
                    break
                if elapsed > self.run_timeout_s:
                    killed_reason = "run_timeout"
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill phase={phase} "
                        f"reason=run_timeout elapsed={elapsed:.2f}s limit={self.run_timeout_s}s"
                    )
                    self._kill_process_group(proc)
                    run_kill_hook(reason="run_timeout")
                    break
                if est_tokens > self.token_cap:
                    killed_reason = "token_cap"
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill phase={phase} "
                        f"reason=token_cap est_tokens={est_tokens} cap={self.token_cap}"
                    )
                    self._kill_process_group(proc)
                    run_kill_hook(reason="token_cap")
                    break
                time.sleep(2.0)

            if proc.poll() is None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._kill_process_group(proc)
                    proc.wait(timeout=5)

            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            exit_code = proc.returncode

        for failure in reader_failures:
            self._progress(f"PROGRESS run_label={run_label} step=reader-failure phase={phase} detail={failure}")

        with state_lock:
            session_id = state["session_id"]
            turns = self._to_int(state["turns"])
            input_tokens = self._to_int(state["max_input"])
            output_tokens = self._to_int(state["sum_output"])
            reasoning_tokens = self._to_int(state["sum_reasoning"])

        if exit_code not in (0, None) and stderr_tail:
            self._progress(
                f"PROGRESS run_label={run_label} step=worker-nonzero phase={phase} exit={exit_code} "
                f"stderr_tail={' | '.join(stderr_tail)}"
            )

        return _OpencodeRunStats(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            turns=turns,
            session_id=session_id,
            killed_reason=killed_reason,
            exit_code=exit_code,
        )

    @staticmethod
    def _copy_tree_contents(src_dir: Path, dst_dir: Path) -> None:
        if not src_dir.is_dir():
            raise FileNotFoundError(f"source directory does not exist: {src_dir}")

        dst_dir.mkdir(parents=True, exist_ok=True)
        for item in src_dir.iterdir():
            target = dst_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def _progress(self, message: str) -> None:
        self._progress_cb(message)
        if self.logger is None:
            return

        info = getattr(self.logger, "info", None)
        if callable(info):
            info(message)

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
        return out

    @staticmethod
    def _normalize_problems(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                normalized.append({"check": "unknown", "expected": "", "observed": str(item)})
                continue
            normalized.append(
                {
                    "check": str(item.get("check", "unknown")),
                    "expected": str(item.get("expected", "")),
                    "observed": str(item.get("observed", "")),
                }
            )
        return normalized
