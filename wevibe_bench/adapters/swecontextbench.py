"""SWEContextBench solve-stage adapter for the WeVibe benchmark harness.

This adapter runs a *real* solve attempt per SWEContextBench instance:
- docker solve mode (default): run mini-SWE-agent inside the per-instance
  SWEContextBench image at ``/testbed``
- host solve mode (optional fallback): check out the benchmark repo at
  ``base_commit`` and run locally
- collect a real git unified diff patch and real model telemetry

Scoring (resolved / unresolved) is intentionally deferred to the later
containerized SWEContextBench evaluation stage.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from minisweagent.agents.default import DefaultAgent
from minisweagent.config import get_config_from_spec
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models import get_model

from wevibe_bench.adapters.aider_polyglot import _format_memory
from wevibe_bench.backends.base import NeedCard, RecalledMemory
from wevibe_bench.runner import AgentRunner, TaskOutcome


_OPENROUTER_AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"
_KEEP_WORK_ENV = "KEEP_WORK"
_DOCKER_SOLVE_ENV = {
    "PAGER": "cat",
    "MANPAGER": "cat",
    "LESS": "-R",
    "PIP_PROGRESS_BAR": "off",
    "TQDM_DISABLE": "1",
    "BASH_ENV": "/root/.bashrc",
}


@dataclass(frozen=True)
class SolveResult:
    patch: str
    input_tokens: int
    output_tokens: int
    turns: int
    wall_cost_usd: float
    wall_seconds: float
    model: str
    agent_status: str


class SWEContextBenchRunner(AgentRunner):
    def __init__(
        self,
        *,
        dataset_dir: Path,
        work_root: Path | None = None,
        model: str = "openrouter/qwen/qwen3-coder",
        step_limit: int = 40,
        cost_limit_usd: float = 1.0,
        repo_cache_dir: Path | None = None,
        docker_solve: bool = True,
    ) -> None:
        self.dataset_dir = Path(dataset_dir).expanduser()
        self.work_root = Path(work_root).expanduser() if work_root is not None else None
        self.model = model
        self.step_limit = int(step_limit)
        self.cost_limit_usd = float(cost_limit_usd)
        self.repo_cache_dir = Path(repo_cache_dir).expanduser() if repo_cache_dir is not None else None
        self.docker_solve = bool(docker_solve)
        self._instances = self._load_instances()

        mini_cfg = get_config_from_spec("mini.yaml")
        agent_cfg = mini_cfg.get("agent", {}) if isinstance(mini_cfg, dict) else {}
        env_cfg = mini_cfg.get("environment", {}) if isinstance(mini_cfg, dict) else {}
        model_cfg = mini_cfg.get("model", {}) if isinstance(mini_cfg, dict) else {}

        self._system_template = str(agent_cfg.get("system_template", "You are a helpful assistant."))
        self._instance_template = str(agent_cfg.get("instance_template", "Please solve this issue: {{task}}"))
        self._env_vars = dict(env_cfg.get("env", {})) if isinstance(env_cfg, dict) else {}
        self._model_config = dict(model_cfg) if isinstance(model_cfg, dict) else {}

    def build_need_card(self, instance_id: str) -> NeedCard:
        instance = self._instance(instance_id)
        repo_short = self._repo_short_name(str(instance["repo"]))
        return NeedCard(
            intent="fix",
            task=str(instance["problem_statement"]),
            language="python",
            stack=[repo_short, "python"],
            project_name=repo_short,
        )

    def solve_instance(self, instance_id: str, injected_memory: list[RecalledMemory]) -> SolveResult:
        start = time.monotonic()
        instance = self._instance(instance_id)
        repo = str(instance["repo"])
        base_commit = str(instance["base_commit"])
        selected_model = self.model

        work_dir = self._new_work_dir(instance_id)
        repo_dir = work_dir / self._repo_short_name(repo)
        docker_image = self._docker_image(instance_id) if self.docker_solve else ""

        patch = ""
        input_tokens = 0
        output_tokens = 0
        turns = 0
        wall_cost_usd = 0.0
        agent_status = "error"

        try:
            if self.docker_solve:
                print(
                    "[swecb] checkout_skip "
                    f"instance={instance_id} mode=docker image={docker_image} "
                    f"base_commit={self._short_commit(base_commit)}",
                    flush=True,
                )
            else:
                print(
                    f"[swecb] checkout_start instance={instance_id} repo={repo} base_commit={base_commit}",
                    flush=True,
                )
                self._checkout_repo(repo=repo, base_commit=base_commit, target_dir=repo_dir)

                head = self._run_git(["rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
                status = self._run_git(["status", "--porcelain"], cwd=repo_dir).stdout.strip()
                clean = "yes" if not status else "no"
                print(
                    f"[swecb] checkout_done instance={instance_id} head={head} clean={clean} path={repo_dir}",
                    flush=True,
                )

            task_text = str(instance["problem_statement"])
            memory_blob = _format_memory(injected_memory)
            memory_on = bool(memory_blob)
            if memory_on:
                task_text = (
                    "## Relevant past experience (WeVibe recall)\n"
                    f"{memory_blob}\n\n"
                    "## Task\n"
                    f"{task_text}"
                )

            print(
                "[swecb] agent_start "
                f"instance={instance_id} model={selected_model} memory_on={memory_on} "
                f"step_limit={self.step_limit} cost_limit_usd={self.cost_limit_usd:.3f} "
                f"solve_mode={'docker' if self.docker_solve else 'host'}",
                flush=True,
            )

            run_status, run_error, agent, patch = self._run_agent(
                repo_dir=repo_dir,
                model_name=selected_model,
                task_text=task_text,
                instance_id=instance_id,
                base_commit=base_commit,
                docker_image=docker_image,
            )
            input_tokens, output_tokens = self._sum_token_usage(agent.messages)
            turns = int(agent.n_calls)
            wall_cost_usd = float(agent.cost)
            agent_status = run_status

            print(
                "[swecb] agent_finish "
                f"instance={instance_id} status={agent_status} turns={turns} "
                f"input_tokens={input_tokens} output_tokens={output_tokens} cost_usd={wall_cost_usd:.6f}",
                flush=True,
            )
            if run_error is not None:
                print(
                    f"[swecb] agent_error instance={instance_id} type={type(run_error).__name__} msg={run_error}",
                    flush=True,
                )

            print(
                "[swecb] diff_ready "
                f"instance={instance_id} patch_bytes={len(patch.encode('utf-8'))} patch_lines={len(patch.splitlines())}",
                flush=True,
            )
        finally:
            if os.getenv(_KEEP_WORK_ENV):
                print(f"[swecb] keep_work enabled; preserving {work_dir}", flush=True)
            else:
                shutil.rmtree(work_dir, ignore_errors=True)

        wall_seconds = time.monotonic() - start
        print(
            f"[swecb] solve_done instance={instance_id} wall_seconds={wall_seconds:.3f}",
            flush=True,
        )
        return SolveResult(
            patch=patch,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            turns=turns,
            wall_cost_usd=wall_cost_usd,
            wall_seconds=wall_seconds,
            model=selected_model,
            agent_status=agent_status,
        )

    def run_task(self, model: str, instance_id: str, injected_memory: list[RecalledMemory]) -> TaskOutcome:
        """Run solve-stage generation only.

        ``resolved`` is always False here because solve-stage execution does not run
        SWEContextBench's canonical Docker evaluation; the final resolve verdict is
        produced only by that later eval stage.
        """

        selected_model = model or self.model
        if selected_model == self.model:
            solve = self.solve_instance(instance_id, injected_memory)
        else:
            original_model = self.model
            self.model = selected_model
            try:
                solve = self.solve_instance(instance_id, injected_memory)
            finally:
                self.model = original_model

        return TaskOutcome(
            resolved=False,
            input_tokens=solve.input_tokens,
            output_tokens=solve.output_tokens,
            turns=solve.turns,
            wall_cost_usd=solve.wall_cost_usd,
            wall_seconds=solve.wall_seconds,
        )

    def _load_instances(self) -> dict[str, dict[str, Any]]:
        dataset_path = self.dataset_dir / "heldout_lite.json"
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"expected list in {dataset_path}, got {type(payload).__name__}")

        instances: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(payload):
            if not isinstance(row, dict):
                raise ValueError(f"dataset row {index} is not an object")

            instance_id = row.get("instance_id")
            if not isinstance(instance_id, str) or not instance_id:
                raise ValueError(f"dataset row {index} missing valid instance_id")

            instances[instance_id] = row

        return instances

    def _instance(self, instance_id: str) -> dict[str, Any]:
        try:
            return self._instances[instance_id]
        except KeyError as exc:
            raise KeyError(f"unknown SWEContextBench instance_id={instance_id!r}") from exc

    @staticmethod
    def _repo_short_name(repo: str) -> str:
        parts = repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"invalid repo value {repo!r}; expected owner/name")
        return parts[1]

    def _new_work_dir(self, instance_id: str) -> Path:
        prefix = f"swecb-{instance_id.replace('/', '__')}-"
        if self.work_root is not None:
            self.work_root.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix=prefix, dir=str(self.work_root)))
        return Path(tempfile.mkdtemp(prefix=prefix))

    def _checkout_repo(self, *, repo: str, base_commit: str, target_dir: Path) -> None:
        clone_url = f"https://github.com/{repo}.git"
        if self.repo_cache_dir is not None:
            self.repo_cache_dir.mkdir(parents=True, exist_ok=True)
            cache_dir = self.repo_cache_dir / repo.replace("/", "__")
            if cache_dir.exists():
                if self._is_partial_clone(cache_dir):
                    print(f"[swecb] checkout_cache_reset_partial repo={repo} cache={cache_dir}", flush=True)
                    shutil.rmtree(cache_dir, ignore_errors=True)
                    self._run_git(["clone", "--no-tags", clone_url, str(cache_dir)])
                else:
                    print(f"[swecb] checkout_cache_hit repo={repo} cache={cache_dir}", flush=True)
                    self._run_git(["fetch", "--prune", "origin"], cwd=cache_dir)
            else:
                print(f"[swecb] checkout_cache_miss repo={repo} cache={cache_dir}", flush=True)
                self._run_git(["clone", "--no-tags", clone_url, str(cache_dir)])

            self._run_git(["clone", "--local", str(cache_dir), str(target_dir)])
        else:
            self._run_git(["clone", "--no-tags", clone_url, str(target_dir)])

        if not self._has_commit(target_dir, base_commit):
            self._run_git(["fetch", "--depth=1", "origin", base_commit], cwd=target_dir)

        self._run_git(["checkout", "-f", base_commit], cwd=target_dir)
        self._run_git(["reset", "--hard", base_commit], cwd=target_dir)
        self._run_git(["clean", "-fdx"], cwd=target_dir)

        head = self._run_git(["rev-parse", "HEAD"], cwd=target_dir).stdout.strip()
        if head != base_commit:
            raise RuntimeError(f"checkout mismatch: expected {base_commit}, got {head}")

        dirty = self._run_git(["status", "--porcelain"], cwd=target_dir).stdout.strip()
        if dirty:
            raise RuntimeError("expected clean git tree after checkout, found local changes")

    def _run_agent(
        self,
        *,
        repo_dir: Path,
        model_name: str,
        task_text: str,
        instance_id: str,
        base_commit: str,
        docker_image: str,
    ) -> tuple[str, Exception | None, DefaultAgent, str]:
        if not model_name.startswith("openrouter/"):
            raise ValueError(
                "SWEContextBenchRunner requires a hosted OpenRouter model "
                f"(expected prefix 'openrouter/', got {model_name!r})"
            )

        model_config = dict(self._model_config)
        model_config["model_name"] = model_name

        model = get_model(config=model_config)
        env: Any | None = None
        patch = ""
        run_error: Exception | None = None
        payload: dict[str, Any] = {}

        try:
            if self.docker_solve:
                image = docker_image or self._docker_image(instance_id)
                env = DockerEnvironment(
                    image=image,
                    cwd="/testbed",
                    env=dict(_DOCKER_SOLVE_ENV),
                    interpreter=["bash", "-c"],
                    timeout=120,
                    run_args=["--rm", "--platform", "linux/amd64"],
                    container_timeout="2h",
                    pull_timeout=900,
                )
                self._ensure_docker_repo_commit(
                    env=env,
                    instance_id=instance_id,
                    base_commit=base_commit,
                    image=image,
                )
            else:
                env = LocalEnvironment(cwd=str(repo_dir), env=self._env_vars, timeout=120)

            agent = DefaultAgent(
                model,
                env,
                system_template=self._system_template,
                instance_template=self._instance_template,
                step_limit=self.step_limit,
                cost_limit=self.cost_limit_usd,
            )

            key = self._load_openrouter_key()
            prior_key = os.environ.get("OPENROUTER_API_KEY")
            os.environ["OPENROUTER_API_KEY"] = key
            try:
                payload = agent.run(task_text)
            except Exception as exc:  # noqa: BLE001 - we need explicit status/telemetry on all failures.
                run_error = exc
                if agent.messages and isinstance(agent.messages[-1], dict):
                    extra = agent.messages[-1].get("extra")
                    if isinstance(extra, dict):
                        payload = extra
            finally:
                if prior_key is None:
                    os.environ.pop("OPENROUTER_API_KEY", None)
                else:
                    os.environ["OPENROUTER_API_KEY"] = prior_key

            if self.docker_solve:
                diff_out = env.execute({"command": f"cd /testbed && git diff --binary --no-ext-diff {base_commit}"})
                if int(diff_out.get("returncode", -1)) != 0:
                    raise RuntimeError(
                        "docker git diff failed "
                        f"instance={instance_id} rc={diff_out.get('returncode')} output={diff_out.get('output', '')!r}"
                    )
                patch = self._clean_patch_output(str(diff_out.get("output", "")))
            else:
                patch = self._run_git(["diff", "--binary", "--no-ext-diff", base_commit], cwd=repo_dir).stdout
        finally:
            cleanup = getattr(env, "cleanup", None)
            if callable(cleanup):
                cleanup()

        raw_status = payload.get("exit_status") if isinstance(payload, dict) else None
        status = self._normalize_agent_status(str(raw_status) if raw_status else "", agent, run_error)
        return status, run_error, agent, patch

    @staticmethod
    def _docker_image(instance_id: str) -> str:
        return f"jiayuanz3/swecontextbench:{instance_id.replace('__', '.').lower()}"

    def _ensure_docker_repo_commit(
        self,
        *,
        env: DockerEnvironment,
        instance_id: str,
        base_commit: str,
        image: str,
    ) -> None:
        expected = base_commit.strip().lower()
        head = self._docker_head_or_raise(env=env, instance_id=instance_id)
        if head != expected:
            print(
                "[swecb] docker_head_mismatch "
                f"instance={instance_id} image={image} expected={self._short_commit(expected)} "
                f"actual={self._short_commit(head)} action=checkout",
                flush=True,
            )
            checkout = env.execute({"command": f"cd /testbed && git checkout -f {base_commit}"})
            if int(checkout.get("returncode", -1)) != 0:
                print(
                    "[swecb] docker_checkout_failed "
                    f"instance={instance_id} image={image} expected={self._short_commit(expected)} "
                    f"rc={checkout.get('returncode')} output={str(checkout.get('output', '')).strip()!r}",
                    flush=True,
                )
                raise RuntimeError(
                    "docker checkout failed "
                    f"instance={instance_id} expected={expected} rc={checkout.get('returncode')}"
                )
            head = self._docker_head_or_raise(env=env, instance_id=instance_id)

        if head != expected:
            raise RuntimeError(
                "docker checkout mismatch "
                f"instance={instance_id} expected={expected} actual={head} image={image}"
            )

        print(
            "[swecb] docker_repo_ready "
            f"instance={instance_id} image={image} base_commit={self._short_commit(expected)} "
            f"head={self._short_commit(head)}",
            flush=True,
        )

    def _docker_head_or_raise(self, *, env: DockerEnvironment, instance_id: str) -> str:
        probe = env.execute({"command": "cd /testbed && git rev-parse HEAD"})
        rc = int(probe.get("returncode", -1))
        out = str(probe.get("output", ""))
        if rc != 0:
            raise RuntimeError(f"docker git rev-parse failed instance={instance_id} rc={rc} output={out!r}")

        head = self._extract_commit_hash(out)
        if not head:
            raise RuntimeError(f"docker git rev-parse produced no commit hash instance={instance_id} output={out!r}")
        return head

    @staticmethod
    def _extract_commit_hash(output: str) -> str:
        matches = re.findall(r"\b[0-9a-fA-F]{40}\b", output)
        if matches:
            return matches[-1].lower()
        lines = [line.strip().lower() for line in output.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    @staticmethod
    def _short_commit(commit: str) -> str:
        token = commit.strip()
        if not token:
            return "unknown"
        return token[:8]

    @staticmethod
    def _clean_patch_output(output: str) -> str:
        if not output:
            return ""

        normalized = output.replace("\r\n", "\n")
        start = normalized.find("diff --git ")
        if start == -1:
            return ""

        # Strip only trailing NEWLINES — never a full rstrip(), which would delete a
        # trailing blank-context line that git emits as a lone " " (space). Dropping
        # that line leaves the hunk body one line short of git's (correct) header
        # count, which `git apply` rejects as "corrupt patch" (rc=128).
        patch = normalized[start:].rstrip("\n")
        return f"{patch}\n" if patch else ""

    @staticmethod
    def _sum_token_usage(messages: list[dict[str, Any]]) -> tuple[int, int]:
        input_tokens = 0
        output_tokens = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            extra = message.get("extra")
            if not isinstance(extra, dict):
                continue
            response = extra.get("response")
            if not isinstance(response, dict):
                continue

            usage = response.get("usage")
            if not isinstance(usage, dict):
                continue

            input_tokens += SWEContextBenchRunner._to_int(usage.get("prompt_tokens"))
            output_tokens += SWEContextBenchRunner._to_int(usage.get("completion_tokens"))

        return input_tokens, output_tokens

    @staticmethod
    def _to_int(value: Any) -> int:
        if isinstance(value, bool) or value is None:
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value.strip()))
            except ValueError:
                return 0
        return 0

    def _normalize_agent_status(self, raw_status: str, agent: DefaultAgent, run_error: Exception | None) -> str:
        if run_error is not None:
            return f"error:{type(run_error).__name__}"

        if raw_status == "Submitted":
            return "completed"
        if raw_status == "LimitsExceeded":
            if self.step_limit > 0 and agent.n_calls >= self.step_limit:
                return "step-limit"
            if self.cost_limit_usd > 0 and agent.cost >= self.cost_limit_usd:
                return "cost-limit"
            return "limits-exceeded"
        if raw_status == "TimeExceeded":
            return "time-limit"
        if raw_status:
            return raw_status.replace("_", "-").replace(" ", "-").lower()
        return "unknown"

    @staticmethod
    def _load_openrouter_key() -> str:
        payload = json.loads(_OPENROUTER_AUTH_PATH.read_text(encoding="utf-8"))
        key = payload.get("openrouter", {}).get("key")
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError(f"missing openrouter key at {_OPENROUTER_AUTH_PATH}")
        return key.strip()

    @staticmethod
    def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            command = " ".join(["git", *args])
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            raise RuntimeError(
                f"git command failed ({completed.returncode}): {command}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        return completed

    @staticmethod
    def _has_commit(repo_dir: Path, commit: str) -> bool:
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=str(repo_dir),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return probe.returncode == 0

    @staticmethod
    def _is_partial_clone(repo_dir: Path) -> bool:
        probe = subprocess.run(
            ["git", "config", "--bool", "--get", "remote.origin.promisor"],
            cwd=str(repo_dir),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return probe.returncode == 0 and probe.stdout.strip().lower() == "true"


def write_prediction(instance_id: str, model_name: str, patch: str, out_path: Path | str) -> None:
    payload = {
        instance_id: {
            "model_name_or_path": model_name,
            "instance_id": instance_id,
            "model_patch": patch,
        }
    }
    output_path = Path(out_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


__all__ = ["SWEContextBenchRunner", "SolveResult", "write_prediction"]
