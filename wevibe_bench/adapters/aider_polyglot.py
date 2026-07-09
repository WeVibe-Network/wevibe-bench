"""Aider polyglot substrate adapter for the benchmark harness.

Fidelity note (memory injection seam):
- Live plugin path injects recalled memory through a chat system-context transform.
- Aider CLI faithful analog is ``--read <WEVIBE_MEMORY.md>`` read-only context.
- Both mechanisms place recalled memory in model-visible context (not a bespoke
  user prompt slot).

Token/cost accounting in this module uses aider's terminal usage line as the
working contract. For exact per-call accounting in live runs, prefer aider's
analytics JSONL output.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Iterator

from wevibe_bench.backends.base import NeedCard, RecalledMemory
from wevibe_bench.config import RunConfig
from wevibe_bench.runner import AgentRunner, TaskOutcome


LOGGER = logging.getLogger("wevibe_bench.adapters.aider_polyglot")

_SUPPORTED_LANGUAGES: tuple[str, ...] = ("cpp", "go", "java", "javascript", "python", "rust")

# LIVE-CONFIRM against the real polyglot-benchmark repository.
DEFAULT_TEST_COMMANDS: dict[str, list[str]] = {
    "python": ["python", "-m", "pytest", "-q"],
    "rust": ["cargo", "test"],
    "go": ["go", "test", "./..."],
    "javascript": ["npm", "test"],
    "java": ["./gradlew", "test"],
    "cpp": ["sh", "-c", "cmake -B build && cmake --build build && ctest --test-dir build"],
}

_TOKEN_VALUE_RE = re.compile(r"^\s*(?P<number>[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?P<suffix>[kKmM]?)\s*$")
_COST_VALUE_RE = re.compile(r"^\s*\$?(?P<number>[0-9][0-9,]*(?:\.[0-9]+)?)\s*$")
_AIDER_USAGE_RE = re.compile(
    r"Tokens:\s*(?P<sent>[0-9][0-9,]*(?:\.[0-9]+)?[kKmM]?)\s*sent,\s*"
    r"(?P<received>[0-9][0-9,]*(?:\.[0-9]+)?[kKmM]?)\s*received\.\s*"
    r"Cost:\s*\$(?P<message>[0-9][0-9,]*(?:\.[0-9]+)?)\s*message,\s*"
    r"\$(?P<session>[0-9][0-9,]*(?:\.[0-9]+)?)\s*session\.",
    re.IGNORECASE,
)


class PolyglotRepoNotFound(Exception):
    """Raised when the configured polyglot benchmark repository is missing."""


@dataclass(frozen=True)
class Exercise:
    task_id: str
    language: str
    slug: str
    dir: Path
    instructions: str
    solution_files: list[str]
    test_files: list[str]


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


Executor = Callable[[list[str], Path, dict[str, str] | None], ExecResult]


@dataclass(frozen=True)
class MockCall:
    cmd: list[str]
    cwd: Path
    env: dict[str, str]


@dataclass
class _MockPlan:
    aider_stdout: list[str]
    test_returncodes: list[int]
    aider_returncodes: list[int] = field(default_factory=list)
    test_stdout: list[str] = field(default_factory=list)
    test_stderr: list[str] = field(default_factory=list)


class SubprocessExecutor:
    """Real executor seam for live runs."""

    def __call__(self, cmd: list[str], cwd: Path, env_overrides: dict[str, str] | None) -> ExecResult:
        run_env = os.environ.copy()
        if env_overrides:
            run_env.update(env_overrides)

        try:
            completed = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
                env=run_env,
            )
        except Exception:
            LOGGER.exception("subprocess executor failed cmd=%s cwd=%s", cmd, str(cwd))
            raise

        return ExecResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class MockExecutor:
    """Deterministic executor for offline tests.

    Configure per task_id with canned aider output lines and test return codes:
    {
      "python/two-fer": {
         "aider_stdout": ["Tokens: ... session."],
         "test_returncodes": [0],
         # optional: "aider_returncodes", "test_stdout", "test_stderr"
      }
    }
    """

    def __init__(self, plans: dict[str, dict[str, object] | _MockPlan]) -> None:
        self._plans: dict[str, _MockPlan] = {}
        self._aider_index: dict[str, int] = {}
        self._test_index: dict[str, int] = {}
        self.calls: list[MockCall] = []

        for task_id, raw_plan in plans.items():
            plan = self._coerce_plan(task_id, raw_plan)
            self._plans[task_id] = plan
            self._aider_index[task_id] = 0
            self._test_index[task_id] = 0

    def _coerce_plan(self, task_id: str, raw_plan: dict[str, object] | _MockPlan) -> _MockPlan:
        if isinstance(raw_plan, _MockPlan):
            plan = _MockPlan(
                aider_stdout=list(raw_plan.aider_stdout),
                test_returncodes=list(raw_plan.test_returncodes),
                aider_returncodes=list(raw_plan.aider_returncodes),
                test_stdout=list(raw_plan.test_stdout),
                test_stderr=list(raw_plan.test_stderr),
            )
        elif isinstance(raw_plan, dict):
            aider_stdout = _coerce_str_list(task_id, "aider_stdout", raw_plan.get("aider_stdout"))
            test_returncodes = _coerce_int_list(task_id, "test_returncodes", raw_plan.get("test_returncodes"))
            aider_returncodes_raw = raw_plan.get("aider_returncodes")
            test_stdout_raw = raw_plan.get("test_stdout")
            test_stderr_raw = raw_plan.get("test_stderr")

            aider_returncodes = (
                _coerce_int_list(task_id, "aider_returncodes", aider_returncodes_raw)
                if aider_returncodes_raw is not None
                else [0 for _ in aider_stdout]
            )
            test_stdout = (
                _coerce_str_list(task_id, "test_stdout", test_stdout_raw)
                if test_stdout_raw is not None
                else ["" for _ in test_returncodes]
            )
            test_stderr = (
                _coerce_str_list(task_id, "test_stderr", test_stderr_raw)
                if test_stderr_raw is not None
                else ["" for _ in test_returncodes]
            )

            plan = _MockPlan(
                aider_stdout=aider_stdout,
                test_returncodes=test_returncodes,
                aider_returncodes=aider_returncodes,
                test_stdout=test_stdout,
                test_stderr=test_stderr,
            )
        else:
            raise TypeError(f"mock plan for {task_id!r} must be dict or _MockPlan")

        if len(plan.aider_returncodes) != len(plan.aider_stdout):
            raise ValueError(
                f"mock plan {task_id!r} aider_returncodes length {len(plan.aider_returncodes)} "
                f"must equal aider_stdout length {len(plan.aider_stdout)}"
            )
        if len(plan.test_stdout) != len(plan.test_returncodes):
            raise ValueError(
                f"mock plan {task_id!r} test_stdout length {len(plan.test_stdout)} "
                f"must equal test_returncodes length {len(plan.test_returncodes)}"
            )
        if len(plan.test_stderr) != len(plan.test_returncodes):
            raise ValueError(
                f"mock plan {task_id!r} test_stderr length {len(plan.test_stderr)} "
                f"must equal test_returncodes length {len(plan.test_returncodes)}"
            )

        return plan

    def __call__(self, cmd: list[str], cwd: Path, env_overrides: dict[str, str] | None) -> ExecResult:
        env = dict(env_overrides or {})
        self.calls.append(MockCall(cmd=list(cmd), cwd=cwd, env=env))

        task_id = env.get("WEVIBE_TASK_ID")
        if not task_id:
            raise RuntimeError("MockExecutor requires env override WEVIBE_TASK_ID")
        if task_id not in self._plans:
            raise RuntimeError(f"MockExecutor missing plan for task_id={task_id}")

        if not cmd:
            raise RuntimeError("MockExecutor got empty command")

        plan = self._plans[task_id]
        if cmd[0] == "aider":
            index = self._aider_index[task_id]
            if index >= len(plan.aider_stdout):
                raise RuntimeError(f"MockExecutor exhausted aider_stdout for task_id={task_id}")

            self._aider_index[task_id] = index + 1
            return ExecResult(
                returncode=plan.aider_returncodes[index],
                stdout=plan.aider_stdout[index],
                stderr="",
            )

        index = self._test_index[task_id]
        if index >= len(plan.test_returncodes):
            raise RuntimeError(f"MockExecutor exhausted test_returncodes for task_id={task_id}")

        self._test_index[task_id] = index + 1
        return ExecResult(
            returncode=plan.test_returncodes[index],
            stdout=plan.test_stdout[index],
            stderr=plan.test_stderr[index],
        )


class AiderPolyglotRunner(AgentRunner):
    """Adapter that executes polyglot benchmark tasks through aider CLI."""

    def __init__(
        self,
        *,
        polyglot_dir: str | Path | None = None,
        cfg: RunConfig | None = None,
        executor: Executor | None = None,
        mock_mode: bool = False,
        test_commands: dict[str, list[str]] | None = None,
        work_root: str | Path | None = None,
    ) -> None:
        if mock_mode and executor is None:
            raise ValueError("mock_mode=True requires an injected executor (MockExecutor)")

        self._cfg = cfg or RunConfig()
        self._polyglot_dir = Path(polyglot_dir).expanduser() if polyglot_dir is not None else None
        self._mock_mode = mock_mode
        self._executor = executor or SubprocessExecutor()
        self._work_root = Path(work_root).expanduser() if work_root is not None else None

        merged = {language: list(command) for language, command in DEFAULT_TEST_COMMANDS.items()}
        if test_commands:
            for language, command in test_commands.items():
                merged[language] = [str(part) for part in command]
        self._test_commands = merged

        self._exercise_cache: dict[str, Exercise] | None = None

    def _resolve_polyglot_root(self) -> Path:
        if self._polyglot_dir is not None:
            root = self._polyglot_dir
        else:
            env_value = os.environ.get("AIDER_POLYGLOT_DIR")
            root = Path(env_value).expanduser() if env_value else None

        shown = str(root) if root is not None else "<unset>"
        if root is None or not root.exists() or not root.is_dir():
            raise PolyglotRepoNotFound(
                "seam: clone the aider polyglot-benchmark repo and set AIDER_POLYGLOT_DIR "
                f"— {shown} not found"
            )

        return root

    def load_exercises(self) -> dict[str, Exercise]:
        if self._exercise_cache is not None:
            return self._exercise_cache

        root = self._resolve_polyglot_root()
        exercises: dict[str, Exercise] = {}

        for language in _SUPPORTED_LANGUAGES:
            practice_dir = root / language / "exercises" / "practice"
            if not practice_dir.is_dir():
                continue

            for slug_dir in sorted(path for path in practice_dir.iterdir() if path.is_dir()):
                instructions_path = slug_dir / ".docs" / "instructions.md"
                append_path = slug_dir / ".docs" / "instructions.append.md"
                config_path = slug_dir / ".meta" / "config.json"
                if not instructions_path.is_file() or not config_path.is_file():
                    continue

                instructions = instructions_path.read_text(encoding="utf-8").strip()
                if append_path.is_file():
                    append_text = append_path.read_text(encoding="utf-8").strip()
                    if append_text:
                        instructions = f"{instructions}\n\n{append_text}" if instructions else append_text

                config_payload = json.loads(config_path.read_text(encoding="utf-8"))
                files_payload = config_payload.get("files") if isinstance(config_payload, dict) else {}
                files_payload = files_payload if isinstance(files_payload, dict) else {}

                solution_files = _coerce_path_list(files_payload.get("solution"))
                test_files = _coerce_path_list(files_payload.get("test"))

                slug = slug_dir.name
                task_id = f"{language}/{slug}"
                exercises[task_id] = Exercise(
                    task_id=task_id,
                    language=language,
                    slug=slug,
                    dir=slug_dir,
                    instructions=instructions,
                    solution_files=solution_files,
                    test_files=test_files,
                )

        self._exercise_cache = exercises
        LOGGER.info("polyglot_task_load root=%s count=%d", str(root), len(exercises))
        return exercises

    def task_ids(self) -> list[str]:
        return sorted(self.load_exercises().keys())

    def build_need_card(self, task_id: str) -> NeedCard:
        """Build INV-6 compliant need-card.

        ``run_ablation`` calls this once for the task context; second-attempt debug
        framing is injected in ``run_task`` after a real failing test run.
        """

        exercise = self._exercise_for_task(task_id)
        return NeedCard(
            intent="implement",
            task=exercise.instructions,
            language=exercise.language,
            stack=[exercise.language],
            files=list(exercise.solution_files),
            project_name=exercise.slug,
        )

    def run_task(self, model: str, task_id: str, injected_memory: list[RecalledMemory]) -> TaskOutcome:
        exercise = self._exercise_for_task(task_id)
        if not exercise.solution_files:
            raise ValueError(f"exercise {task_id} has no solution files in .meta/config.json")

        started_at = time.monotonic()
        input_tokens = 0
        output_tokens = 0
        wall_cost_usd = 0.0
        turns = 0
        resolved = False

        with self._materialize_work_copy(exercise) as work_dir:
            read_path: Path | None = None
            memory_blob = _format_memory(injected_memory)
            if memory_blob:
                read_path = work_dir / "WEVIBE_MEMORY.md"
                read_path.write_text(memory_blob, encoding="utf-8")

            attempt_message = exercise.instructions
            env_overrides = {"WEVIBE_TASK_ID": task_id}

            for attempt in (1, 2):
                turns = attempt
                LOGGER.info(
                    "aider_attempt_start task_id=%s model=%s attempt=%d memory_context=%s",
                    task_id,
                    model,
                    attempt,
                    bool(read_path),
                )

                message_file = work_dir / f"WEVIBE_AIDER_MESSAGE_{attempt}.txt"
                message_file.write_text(attempt_message, encoding="utf-8")

                aider_cmd = [
                    "aider",
                    "--model",
                    model,
                    "--yes",
                    "--no-stream",
                    "--no-auto-commits",
                    "--no-git",
                ]
                if read_path is not None:
                    aider_cmd.extend(["--read", str(read_path)])
                aider_cmd.extend(["--message-file", str(message_file)])
                aider_cmd.extend(exercise.solution_files)

                aider_result = self._executor(aider_cmd, work_dir, env_overrides)

                # LIVE-CONFIRM: prefer --analytics-log for exact counts.
                usage = _parse_aider_usage(_join_streams(aider_result.stdout, aider_result.stderr))
                if usage is None:
                    mode_label = "mock" if self._mock_mode else "live"
                    raise RuntimeError(
                        "aider usage line missing; cannot derive non-fabricated token/cost totals "
                        f"for {mode_label} task_id={task_id} attempt={attempt}"
                    )

                sent_tokens, received_tokens, session_cost = usage
                input_tokens += sent_tokens
                output_tokens += received_tokens
                wall_cost_usd += session_cost

                LOGGER.info(
                    "aider_usage_parsed task_id=%s attempt=%d sent=%d received=%d session_cost_usd=%.6f",
                    task_id,
                    attempt,
                    sent_tokens,
                    received_tokens,
                    session_cost,
                )

                if aider_result.returncode != 0:
                    raise RuntimeError(
                        f"aider failed task_id={task_id} attempt={attempt} returncode={aider_result.returncode}"
                    )

                test_cmd = self._test_command_for_language(exercise.language)
                test_result = self._executor(test_cmd, work_dir, env_overrides)
                if test_result.returncode == 0:
                    resolved = True
                    break

                if attempt == 2:
                    break

                test_output = _join_streams(test_result.stdout, test_result.stderr).strip()
                if not test_output:
                    test_output = "(no test output captured)"

                attempt_message = (
                    f"{exercise.instructions}\n\n"
                    "####\n"
                    "The tests failed. Test output:\n"
                    f"{test_output}\n"
                    "Please fix the code."
                )

        outcome = TaskOutcome(
            resolved=resolved,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            turns=turns,
            wall_cost_usd=round(wall_cost_usd, 10),
            wall_seconds=time.monotonic() - started_at,
        )
        LOGGER.info(
            "aider_task_outcome task_id=%s model=%s resolved=%s turns=%d input_tokens=%d output_tokens=%d wall_cost_usd=%.6f wall_seconds=%.3f",
            task_id,
            model,
            outcome.resolved,
            outcome.turns,
            outcome.input_tokens,
            outcome.output_tokens,
            outcome.wall_cost_usd,
            outcome.wall_seconds,
        )
        return outcome

    def _exercise_for_task(self, task_id: str) -> Exercise:
        exercises = self.load_exercises()
        if task_id not in exercises:
            raise KeyError(f"unknown polyglot task_id={task_id}")
        return exercises[task_id]

    def _test_command_for_language(self, language: str) -> list[str]:
        command = self._test_commands.get(language)
        if not command:
            raise KeyError(f"no test command configured for language={language}")
        return list(command)

    @contextmanager
    def _materialize_work_copy(self, exercise: Exercise) -> Iterator[Path]:
        if self._work_root is not None:
            self._work_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="aider-polyglot-", dir=str(self._work_root)) as temp_dir:
                work_dir = Path(temp_dir) / exercise.slug
                shutil.copytree(exercise.dir, work_dir)
                yield work_dir
        else:
            with tempfile.TemporaryDirectory(prefix="aider-polyglot-") as temp_dir:
                work_dir = Path(temp_dir) / exercise.slug
                shutil.copytree(exercise.dir, work_dir)
                yield work_dir


def _coerce_str_list(task_id: str, field_name: str, raw: object) -> list[str]:
    if not isinstance(raw, list):
        raise TypeError(f"mock plan {task_id!r} field {field_name!r} must be a list")
    return [str(item) for item in raw]


def _coerce_int_list(task_id: str, field_name: str, raw: object) -> list[int]:
    if not isinstance(raw, list):
        raise TypeError(f"mock plan {task_id!r} field {field_name!r} must be a list")
    return [int(item) for item in raw]


def _coerce_path_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        value = str(item).strip()
        if value:
            values.append(value)
    return values


def _join_streams(stdout: str, stderr: str) -> str:
    if stdout and stderr:
        return f"{stdout}\n{stderr}"
    if stdout:
        return stdout
    return stderr


def _parse_token_count(raw: str) -> int:
    match = _TOKEN_VALUE_RE.fullmatch(raw)
    if not match:
        raise ValueError(f"invalid token count format: {raw!r}")

    base = float(match.group("number").replace(",", ""))
    suffix = match.group("suffix").lower()

    multiplier = 1
    if suffix == "k":
        multiplier = 1_000
    elif suffix == "m":
        multiplier = 1_000_000

    return int(base * multiplier)


def _parse_cost(raw: str) -> float:
    match = _COST_VALUE_RE.fullmatch(raw)
    if not match:
        raise ValueError(f"invalid cost format: {raw!r}")
    return float(match.group("number").replace(",", ""))


def _parse_aider_usage(stdout: str) -> tuple[int, int, float] | None:
    last_match: re.Match[str] | None = None
    for match in _AIDER_USAGE_RE.finditer(stdout):
        last_match = match

    if last_match is None:
        return None

    sent_tokens = _parse_token_count(last_match.group("sent"))
    received_tokens = _parse_token_count(last_match.group("received"))
    session_cost = _parse_cost(last_match.group("session"))
    return (sent_tokens, received_tokens, session_cost)


def _format_memory(memories: list[RecalledMemory]) -> str:
    lines = [
        "# WEVIBE MEMORY CONTEXT",
        "# Read-only context loaded via aider --read",
    ]
    included = 0
    for memory in memories:
        if not memory.has_content():
            continue

        included += 1
        cid = memory.cid or "unknown"
        keywords = ",".join(memory.matched_keywords) if memory.matched_keywords else "none"
        text = re.sub(r"\s+", " ", memory.text.strip())
        lines.append(f"- m{included} cid={cid} kw={keywords} text={text}")

    if included == 0:
        return ""

    return "\n".join(lines) + "\n"


__all__ = [
    "AiderPolyglotRunner",
    "DEFAULT_TEST_COMMANDS",
    "ExecResult",
    "Executor",
    "Exercise",
    "MockCall",
    "MockExecutor",
    "PolyglotRepoNotFound",
    "SubprocessExecutor",
    "_format_memory",
    "_parse_aider_usage",
    "_parse_cost",
    "_parse_token_count",
]
