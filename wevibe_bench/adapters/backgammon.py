"""Backgammon worker-runner adapter for the benchmark harness.

This adapter drives a single backgammon cell end-to-end:
- seed a fresh worktree from scaffold
- run either a mock worker (golden/scaffold copy) or headless opencode
- evaluate with the backgammon gate report runner
- apply budget-bounded rounds of *problems-only* feedback in the same session
"""

from __future__ import annotations

import collections
from contextlib import nullcontext
from dataclasses import dataclass
import datetime as _dt
import hashlib
import json
import logging
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
import urllib.error
import urllib.request

from wevibe_bench.adapters._memory_format import _format_memory
from wevibe_bench.adapters.cheat_detector import (
    build_oracle_markers,
    scan_events_for_oracle_access,
)
from wevibe_bench.config import WORKER_MODEL_REGISTRY
from .docker_worker import (
    DockerCell,
    DockerCellConfig,
    WORKER_IMAGE,
    docker_available,
    image_exists,
)
from wevibe_bench.backends.base import NeedCard, RecalledMemory
from wevibe_bench.runner import AgentRunner, TaskOutcome


_LOG = logging.getLogger(__name__)


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

# Harness-declared verification/test commands for the backgammon task.
# Gate runner = `node report.mjs` (tasks/backgammon/gates/). Worker-invoked
# test commands are observed via bash tool_use events. test_invocations counts
# bash tool_use events whose command contains any declared string.
DECLARED_TEST_COMMANDS: tuple[str, ...] = (
    "node report.mjs",
    "npx vitest",
    "npx playwright",
    "npm test",
    "npm run test",
    "vitest",
    "playwright test",
)

# Source: published provider pricing cards (USD per 1M tokens), including:
# - https://www.orcarouter.ai/api/pricing
#   (pricing_version c58e194db3f6a20e7d41b8c9e2f05a17, fetched 2026-07-24T12:45Z;
#   input USD/Mtok = model_ratio × $2 × group_ratio(=1), output = input × completion_ratio)
# - https://openrouter.ai/anthropic/claude-opus-4.8 (snapshot used in bench guard reports)
# - https://opencode.ai/docs/zen-models (Zen free/free row for big-pickle)
# Walter-pinned: keep the free/free big-pickle row at truthful zero pricing.
_MODEL_PRICING_USD_PER_1M: dict[str, dict[str, float]] = {
    "z-ai/glm-5.2": {
        "input": 1.4,
        "output": 4.4,
        "cache_read": 0.26,
        "cache_write": 1.4,  # OrcaRouter has no cache-write field; use input rate.
    },
    "kimi/kimi-k3": {
        # OrcaRouter pricing_version c58e194db3f6a20e7d41b8c9e2f05a17
        # fetched 2026-07-27 (model_ratio=1.5, completion_ratio=5, cache_ratio=0.1).
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.0,  # OrcaRouter has no cache-write field; use input rate.
    },
    "kimi/kimi-k2.7-code": {
        "input": 0.95,
        "output": 4.0,
        "cache_read": 0.19,
        "cache_write": 0.95,  # OrcaRouter has no cache-write field; use input rate.
    },
    "tencent/hy3": {
        "input": 0.18,
        "output": 0.59,
        "cache_read": 0.059,
        "cache_write": 0.18,  # OrcaRouter has no cache-write field; use input rate.
    },
    "anthropic/claude-opus-4.8": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    },
    "opencode/big-pickle": {
        "input": 0.0,
        "output": 0.0,
    }
}

_RESERVATION_SAFETY_FACTOR = 1.10
_HARNESS_LIMIT_REASONS = {"run_timeout", "max_steps_per_attempt", "token_cap"}
_PROXY_CHECKPOINT_ENV = "WEVIBE_BENCH_PROXY_CHECKPOINT"
MAX_ZERO_TOOL_RESUMES = 2
ZERO_TOOL_RESUME_NUDGE = "Continue. Edit files with tools — do not explain."
_FILE_WRITE_TOOL_NAMES = frozenset({"write", "edit", "apply_patch", "multi_edit"})

# Canonical budget-bounded attempt ceiling.
# Fixture evidence (runs/backgammon/*.scorecard.json):
# - stage7 kimi-k2.7 cells at cap=2.4 spent 1.37-1.69 over 3 attempts
#   (~$0.46-$0.56 per attempt, so ~4 attempts inside a $2.4 cap).
# - stage7 opus-4.8 at cap=11 spent 6.17 over 3 attempts
#   (~$2.06 per attempt, so ~6 attempts inside an ~$12 cap).
# 8 is a bounded safety margin above both observed envelopes.
DEFAULT_ATTEMPT_HARD_CEILING = 8

# Canonical per-attempt step cap (runaway-loop guard, NOT a budget instrument).
# Budget enforcement is the accrued usage.cost kill plus the proxy's hard-cap
# reservation; this cap exists only to stop a fast runaway tool-call loop.
# Evidence for 100: the healthy 15-07 un-clamped baseline used 77 turns across a
# full run (~25-40 per attempt; 19b initial attempt = 37), while the clamp-era
# value of 40 killed smoke 19c at turn 41 mid-work, UNGRADED. 100 = baseline +
# margin. Programmatic `max_steps_per_attempt=None` still means "no cap"; the
# CLI driver defaults to this constant.
DEFAULT_MAX_STEPS_PER_ATTEMPT = 100

# Canonical per-attempt wall-clock timeout (guard, NOT a scoring signal).
# Evidence for 5400: smoke 19d observed ~3060s wall on a healthy 68-turn Opus
# PASS. Stage-4 at the old 1800s default killed converging near-pass runs
# (kimi-k2.7-code: 52 turns with 26/29 gates green; mimo-v2.5-pro: 35 turns).
# int4/fp8 pins run slower than Opus, so the canonical default carries ~1.75x
# headroom over the slowest healthy observed wall (3060 * 1.75 ~= 5355 -> 5400).
DEFAULT_RUN_TIMEOUT_S = 5400


def _scan_cell_delivery(worktree: Path) -> str | None:
    plugin_log = worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log"
    try:
        payload = plugin_log.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    matches = re.findall(r"\[inject\] injected count=(\d+)", payload)
    if not matches:
        return None
    if any(int(count) >= 1 for count in matches):
        return "YES"
    return "NO"


def _scan_injected_block_chars(worktree: Path) -> int | None:
    plugin_log = worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log"
    try:
        payload = plugin_log.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    block_matches = re.findall(r"\[inject\] injected[^\n]*\bblock_chars=(\d+)", payload)
    if block_matches:
        return sum(int(chars) for chars in block_matches)

    legacy_matches = re.findall(r"\[inject\] injected[^\n]*\bchars=(\d+)", payload)
    if legacy_matches:
        return sum(int(chars) for chars in legacy_matches)

    return None


@dataclass(frozen=True)
class RecallFunnelScan:
    recall_fired_total: int = 0
    recall_fired_user_message: int = 0
    recall_fired_tool_failure: int = 0
    recall_returned_total: int = 0
    recall_returned_count_sum: int = 0
    no_keywords_count: int = 0
    injected_count: int = 0
    served_attempted: int = 0
    served_failed: int = 0
    served_confirmed: int = 0


def _scan_recall_funnel(worktree: Path) -> RecallFunnelScan | None:
    plugin_log = worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log"
    try:
        payload = plugin_log.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    fired_matches = re.findall(r"\brecall_fired\s+trigger=(user_message|tool_failure)\b", payload)
    recall_fired_user_message = sum(1 for trigger in fired_matches if trigger == "user_message")
    recall_fired_tool_failure = sum(1 for trigger in fired_matches if trigger == "tool_failure")

    returned_matches = re.findall(
        r"\brecall_returned\s+status=\S+\s+count=(\d+)\s+reason_code=(\S+)\s+dur_ms=\d+\s+error=",
        payload,
    )
    recall_returned_count_sum = sum(int(count) for count, _reason_code in returned_matches)
    no_keywords_count = sum(1 for _count, reason_code in returned_matches if reason_code == "no_keywords")

    injected_matches = re.findall(r"\[inject\]\s+injected\s+count=(\d+)", payload)
    injected_count = sum(int(count) for count in injected_matches)

    served_attempted = len(re.findall(r"\[serve\]\s+upsert\s+cid=", payload))
    served_failed = len(re.findall(r"\[serve\]\s+receipt\s+failed\b", payload))

    return RecallFunnelScan(
        recall_fired_total=len(fired_matches),
        recall_fired_user_message=recall_fired_user_message,
        recall_fired_tool_failure=recall_fired_tool_failure,
        recall_returned_total=len(returned_matches),
        recall_returned_count_sum=recall_returned_count_sum,
        no_keywords_count=no_keywords_count,
        injected_count=injected_count,
        served_attempted=served_attempted,
        served_failed=served_failed,
        served_confirmed=served_attempted - served_failed,
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
    cost_usd: float
    budget_stop_detected: bool = False
    budget_stop_signature: str | None = None
    truncations: int = 0
    zero_tool_turns: int = 0
    terminal_zero_tool_turn: bool = False
    zero_tool_resumes: int = 0
    zero_tool_turn_honest_fail: bool = False


@dataclass(frozen=True)
class _ProxyBudgetSnapshot:
    hard_cap_usd: float
    accrued_actual_usd: float
    accrued_derived_usd: float
    committed_unproven_usd: float
    remaining_usd: float
    checkpoint_path: str


def fetch_orcarouter_billing_usage_cents(*, api_key: str, timeout: float = 10.0) -> float:
    """Fetch OrcaRouter cumulative billing counter (total_usage, in cents)."""

    key = str(api_key or "").strip()
    if not key:
        raise RuntimeError("orcarouter billing usage fetch failed: empty api key")

    request = urllib.request.Request(
        "https://www.orcarouter.ai/v1/dashboard/billing/usage",
        method="GET",
    )
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            status = int(response.getcode() or 0)
            payload_bytes = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "orcarouter billing usage fetch failed: "
            f"status={int(getattr(exc, 'code', 0))} error_class={exc.__class__.__name__}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            "orcarouter billing usage fetch failed: "
            f"status=transport error_class={exc.__class__.__name__}"
        ) from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "orcarouter billing usage fetch failed: "
            f"status={status} error_class={exc.__class__.__name__}"
        ) from exc

    if status != 200:
        raise RuntimeError(
            "orcarouter billing usage fetch failed: "
            f"status={status} error_class=unexpected_http_status"
        )
    if not isinstance(payload, dict):
        raise RuntimeError(
            "orcarouter billing usage fetch failed: "
            "status=200 error_class=invalid_payload_type"
        )

    try:
        return float(payload["total_usage"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "orcarouter billing usage fetch failed: "
            f"status=200 error_class={exc.__class__.__name__}"
        ) from exc


def reconcile_derived_vs_billing(
    *,
    settled_usd: float,
    baseline_cents: float | None,
    final_cents: float | None,
    tolerance: float = 0.05,
) -> dict[str, Any]:
    """Compare settled USD against OrcaRouter cumulative billing-counter delta."""

    confound_note = (
        "counter is workspace-aggregate; any concurrent workspace/CLI traffic inflates delta; "
        "reconciliation requires a quiet workspace"
    )
    settled_value = float(settled_usd)
    baseline_value = None if baseline_cents is None else float(baseline_cents)
    final_value = None if final_cents is None else float(final_cents)

    if baseline_value is None or final_value is None:
        return {
            "status": "skipped",
            "reason": "baseline or final counter unavailable",
            "settled_usd": settled_value,
            "delta_counter_usd": None,
            "divergence_pct": None,
            "tolerance": float(tolerance),
            "baseline_cents": baseline_value,
            "final_cents": final_value,
            "confound_note": confound_note,
        }

    delta_usd = (final_value - baseline_value) / 100.0
    if delta_usd < 0:
        return {
            "status": "error",
            "reason": "counter went backwards",
            "settled_usd": settled_value,
            "delta_counter_usd": delta_usd,
            "divergence_pct": None,
            "tolerance": float(tolerance),
            "baseline_cents": baseline_value,
            "final_cents": final_value,
            "confound_note": confound_note,
        }

    denominator = max(delta_usd, settled_value, 1e-9)
    divergence = abs(settled_value - delta_usd) / denominator
    return {
        "status": "ok" if divergence <= float(tolerance) else "diverged",
        "settled_usd": settled_value,
        "delta_counter_usd": delta_usd,
        "divergence_pct": divergence * 100.0,
        "tolerance": float(tolerance),
        "baseline_cents": baseline_value,
        "final_cents": final_value,
        "confound_note": confound_note,
    }


@dataclass
class BackgammonCellResult:
    verdict: str
    attempts_to_green: int | str
    termination_reason: str
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
    wall_cost_usd: float = 0.0
    cheated: bool = False
    cheat_detail: str = ""
    tool_calls: int | None = None
    test_invocations: int | None = None
    agentic_cycles: int | None = None
    problems_before: int | None = None
    injected_block_chars: int | None = None
    injected_block_est_tokens: int | None = None
    recall_fired_total: int | None = None
    recall_fired_user_message: int | None = None
    recall_fired_tool_failure: int | None = None
    recall_returned_total: int | None = None
    recall_returned_count_sum: int | None = None
    no_keywords_count: int | None = None
    injected_count: int | None = None
    served_attempted: int | None = None
    served_failed: int | None = None
    served_confirmed: int | None = None
    truncations: int = 0
    zero_tool_turns: int = 0
    zero_tool_resumes: int = 0
    zero_tool_turn_honest_fails: int = 0


def _default_progress(message: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[bg] {stamp} {message}", flush=True)


def build_worker_opencode_config(
    *,
    model: str,
    reasoning_effort: str | None,
    proxy_base_url: str | None,
    gates_dir: str,
    golden_dir: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "small_model": model,
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
    provider_id, _, model_id = model.partition("/")
    if not provider_id or not model_id:
        return config

    model_registry = WORKER_MODEL_REGISTRY.get(model_id)
    if model_registry is None:
        raise ValueError(f"unsupported worker model_id for opencode config: {model_id!r}")

    provider_options: dict[str, Any] = {
        "apiKey": "{env:ORCAROUTER_API_KEY}",
    }
    if proxy_base_url is not None:
        provider_options["baseURL"] = proxy_base_url

    model_block: dict[str, Any] = dict(model_registry)
    # NOTE: Never force tool_choice="required" here. Moonshot/kimi rejects it (hard 400),
    # and harness policy is to allow normal tool autonomy.
    model_block["interleaved"] = {"field": "reasoning_content"}
    if session_id:
        model_block["headers"] = {"X-Session-Id": session_id}
    if reasoning_effort is not None:
        options = model_block.setdefault("options", {})
        options["reasoning"] = {"effort": reasoning_effort}

    provider_config: dict[str, Any] = {
        provider_id: {
            "options": provider_options,
            "models": {
                model_id: model_block,
            },
        }
    }

    if provider_config:
        config["provider"] = provider_config
    return config


class BackgammonRunner(AgentRunner):
    def __init__(
        self,
        *,
        task_dir: Path,
        work_root: Path,
        model: str,
        memory_mode: str = "off",
        mock: str | None = None,
        max_attempts: int = DEFAULT_ATTEMPT_HARD_CEILING,
        token_cap: int = 200000,
        run_timeout_s: int = DEFAULT_RUN_TIMEOUT_S,
        completion_grace_s: int = 30,
        cost_limit_usd: float | None = None,
        cost_target_usd: float | None = None,
        max_output_tokens: int | None = None,
        max_steps_per_attempt: int | None = None,
        output_price_per_1m: float | None = None,
        reasoning_effort: str | None = None,
        proxy_base_url: str | None = None,
        proxy_token: str | None = None,
        session_id: str | None = None,
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
        requested_max_attempts = int(max_attempts)
        if requested_max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = min(requested_max_attempts, DEFAULT_ATTEMPT_HARD_CEILING)
        self.token_cap = int(token_cap)
        self.run_timeout_s = int(run_timeout_s)
        self.completion_grace_s = int(completion_grace_s)
        self.cost_limit_usd = None if cost_limit_usd is None else float(cost_limit_usd)
        self.cost_target_usd = None if cost_target_usd is None else float(cost_target_usd)
        self.max_output_tokens = None if max_output_tokens is None else int(max_output_tokens)
        self.max_steps_per_attempt = None if max_steps_per_attempt is None else int(max_steps_per_attempt)
        self.output_price_per_1m = None if output_price_per_1m is None else float(output_price_per_1m)
        self.reasoning_effort = None if reasoning_effort is None else str(reasoning_effort)
        self.proxy_base_url = None if proxy_base_url is None else str(proxy_base_url)
        self.proxy_token = None if proxy_token is None else str(proxy_token)
        self.session_id = None if session_id is None else str(session_id)
        self.agent = str(agent)

        self._effective_output_price_per_1m = 0.0
        self._cache_write_allowance_usd = 0.0
        self._fallback_attempt_estimate_usd = 0.0

        self.logger = logger
        self._progress_cb = progress or _default_progress
        self._repo_root = Path(__file__).resolve().parents[2]

        if self.memory_mode not in {"off", "on"}:
            raise ValueError("memory_mode must be 'off' or 'on'")
        if self.mock not in {None, "golden", "scaffold"}:
            raise ValueError("mock must be one of: None, 'golden', 'scaffold'")
        if self.token_cap < 1:
            raise ValueError("token_cap must be >= 1")
        if self.run_timeout_s < 1:
            raise ValueError("run_timeout_s must be >= 1")
        if self.completion_grace_s < 1:
            raise ValueError("completion_grace_s must be >= 1")
        if self.cost_limit_usd is not None and self.cost_limit_usd <= 0:
            raise ValueError("cost_limit_usd must be > 0")
        if self.cost_target_usd is not None and self.cost_target_usd <= 0:
            raise ValueError("cost_target_usd must be > 0")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be > 0")
        if self.max_steps_per_attempt is not None and self.max_steps_per_attempt <= 0:
            raise ValueError("max_steps_per_attempt must be > 0")
        if self.output_price_per_1m is not None and self.output_price_per_1m <= 0:
            raise ValueError("output_price_per_1m must be > 0")
        if self.cost_limit_usd is not None and self.cost_target_usd is not None:
            if self.cost_target_usd >= self.cost_limit_usd:
                raise ValueError("cost_target_usd must be < cost_limit_usd")

        # Single-meter budget design: proxy ledger is authoritative. The adapter keeps
        # only a conservative fallback *estimate* for attempt-cost forecasting.
        if (
            self.cost_limit_usd is not None
            and self.max_output_tokens is not None
            and self.max_steps_per_attempt is not None
        ):
            self._effective_output_price_per_1m = self._resolve_output_price_per_1m(
                model=self.model,
                explicit_output_price_per_1m=self.output_price_per_1m,
            )
            cache_write_price_per_1m = self._resolve_cache_write_price_per_1m(
                model=self.model,
                fallback_price_per_1m=self._effective_output_price_per_1m,
            )
            self._cache_write_allowance_usd = (
                float(self.max_output_tokens) * cache_write_price_per_1m / 1_000_000.0
            )
            self._fallback_attempt_estimate_usd = self._worst_case_reservation_usd(
                max_steps=self.max_steps_per_attempt,
                max_output_tokens=self.max_output_tokens,
                output_price_per_1m=self._effective_output_price_per_1m,
                safety_factor=_RESERVATION_SAFETY_FACTOR,
                cache_write_allowance_usd=self._cache_write_allowance_usd,
            )

        allowed_reasoning_efforts = {"minimal", "low", "medium", "high", "xhigh", "none"}
        if self.reasoning_effort is not None and self.reasoning_effort not in allowed_reasoning_efforts:
            allowed = ", ".join(sorted(allowed_reasoning_efforts))
            raise ValueError(f"reasoning_effort must be one of: {allowed}")

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
            wall_cost_usd=result.wall_cost_usd,
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
        cell_cost_usd = 0.0
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
        truncations_total = 0
        zero_tool_turns_total = 0
        zero_tool_resumes_total = 0
        zero_tool_turn_honest_fails_total = 0
        events_path = Path(f"{worktree}.events.jsonl")
        user_events_path = Path(f"{worktree}.user-events.jsonl")

        attempt_reports: list[dict[str, Any]] = []
        final_report: dict[str, Any] = {}
        verdict = "FAIL"
        attempts_to_green: int | str = "FAIL"
        termination_reason = "pending"

        worker_killed_reason: str | None = None
        observed_attempt_costs: list[float] = []
        attempt_costs_usd: dict[int, float] = {}
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
            self._init_worktree_git(worktree=worktree)
            cell_config = self._build_cell_config(
                worktree=worktree,
                container_name=container_name,
            )
            cell_context = DockerCell(
                cell_config,
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
                    f"pure={pure} prompt_chars={len(task_prompt)} prompt_delivery=stdin"
                )
                initial_inner = [
                    "opencode",
                    "run",
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

                self._emit_cost_target_warning_if_reached(
                    run_label=run_label,
                    phase="initial",
                    cumulative_cost_usd=cell_cost_usd,
                )

                budget_decision = self._budget_decision_for_attempt(
                    run_label=run_label,
                    attempt=1,
                    observed_attempt_costs=observed_attempt_costs,
                )
                if budget_decision == "harness_error":
                    verdict = "FAIL"
                    attempts_to_green = "FAIL"
                    termination_reason = "harness_error"
                elif budget_decision == "budget_stop":
                    verdict = "BUDGET_STOP"
                    attempts_to_green = "BUDGET_STOP"
                    termination_reason = "attempts_exhausted_by_budget"
                else:
                    self._append_user_event(
                        run_label=run_label,
                        sidecar_path=user_events_path,
                        attempt=1,
                        text=task_prompt,
                    )
                    self._write_worker_permission_config(worktree=worktree)
                    first_run = self._run_opencode_with_zero_tool_resumes(
                        active_cell=active_cell,
                        initial_inner=initial_inner,
                        pure=pure,
                        worktree=worktree,
                        events_path=events_path,
                        env=run_env,
                        run_label=run_label,
                        phase="initial",
                        fallback_session_id=None,
                        prior_cost_usd=cell_cost_usd,
                        kill_hook=active_cell.kill_worker_processes,
                        stdin_text=task_prompt,
                    )
                    attempt_costs_usd[1] = first_run.cost_usd
                    observed_attempt_costs.append(first_run.cost_usd)
                    cell_cost_usd += first_run.cost_usd
                    session_id = first_run.session_id
                    input_tokens_total += first_run.input_tokens
                    output_tokens_total += first_run.output_tokens + first_run.reasoning_tokens
                    turns_total += first_run.turns
                    truncations_total += first_run.truncations
                    zero_tool_turns_total += first_run.zero_tool_turns
                    zero_tool_resumes_total += first_run.zero_tool_resumes
                    if first_run.zero_tool_turn_honest_fail:
                        zero_tool_turn_honest_fails_total += 1
                    worker_killed_reason = first_run.killed_reason
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-launch-end mode=real "
                        f"exit={first_run.exit_code} killed={first_run.killed_reason or 'none'} "
                        f"turns={first_run.turns} input={first_run.input_tokens} "
                        f"output={first_run.output_tokens} reasoning={first_run.reasoning_tokens} "
                        f"session_id={session_id or 'none'} cost_usd={first_run.cost_usd:.4f} "
                        f"cell_cost_usd={cell_cost_usd:.4f}"
                    )

                    if first_run.budget_stop_detected:
                        verdict = "BUDGET_STOP"
                        attempts_to_green = "BUDGET_STOP"
                        termination_reason = "budget_stop_mid_attempt"
                    elif first_run.zero_tool_turn_honest_fail:
                        verdict = "FAIL"
                        attempts_to_green = "FAIL"
                        termination_reason = "zero_tool_turn_honest_fail"
                    elif (
                        first_run.exit_code not in (0, None)
                        and first_run.killed_reason not in _HARNESS_LIMIT_REASONS
                    ):
                        verdict = "FAIL"
                        attempts_to_green = "FAIL"
                        termination_reason = "harness_error"
            else:
                attempt_costs_usd[1] = 0.0

            if termination_reason == "pending":
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
                            "attempt_cost_usd": float(attempt_costs_usd.get(attempt, 0.0)),
                        }
                    )
                    self._progress(
                        f"PROGRESS gate attempt={attempt} verdict={attempt_verdict} "
                        f"conformed={conformed} problems={len(problems)}"
                    )

                    if attempt_verdict == "PASS":
                        verdict = "PASS"
                        attempts_to_green = attempt - 1
                        termination_reason = "gates_green"
                        break

                    if attempt >= self.max_attempts:
                        verdict = "FAIL"
                        attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                        termination_reason = "attempt_ceiling_reached"
                        break

                    if worker_killed_reason in _HARNESS_LIMIT_REASONS:
                        self._progress(
                            f"PROGRESS run_label={run_label} step=attempt-harness-limit attempt={attempt} "
                            f"reason={worker_killed_reason} decision=continue_if_budget"
                        )
                    elif worker_killed_reason is not None:
                        self._progress(
                            f"PROGRESS run_label={run_label} step=attempt-harness-limit attempt={attempt} "
                            f"reason={worker_killed_reason} decision=stop"
                        )
                        verdict = "FAIL"
                        attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                        termination_reason = "harness_error"
                        break

                    if self.mock is not None:
                        self._progress(
                            f"PROGRESS run_label={run_label} step=feedback-skip attempt={attempt} reason=mock_mode"
                        )
                        continue

                    if active_cell is None:
                        self._progress(
                            f"PROGRESS run_label={run_label} step=feedback-stop attempt={attempt} "
                            "reason=active_cell_missing"
                        )
                        verdict = "FAIL"
                        attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                        termination_reason = "harness_error"
                        break

                    if not session_id:
                        self._progress(
                            f"PROGRESS run_label={run_label} step=feedback-stop attempt={attempt} "
                            "reason=session_id_missing"
                        )
                        verdict = "FAIL"
                        attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                        termination_reason = "harness_error"
                        break

                    next_attempt = attempt + 1
                    budget_decision = self._budget_decision_for_attempt(
                        run_label=run_label,
                        attempt=next_attempt,
                        observed_attempt_costs=observed_attempt_costs,
                    )
                    if budget_decision == "harness_error":
                        verdict = "FAIL"
                        attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                        termination_reason = "harness_error"
                        break
                    if budget_decision == "budget_stop":
                        verdict = "BUDGET_STOP"
                        attempts_to_green = "BUDGET_STOP"
                        termination_reason = "attempts_exhausted_by_budget"
                        break

                    feedback_inner = [
                        "opencode",
                        "run",
                        "--session",
                        session_id,
                        "--dir",
                        "/work",
                        "--format",
                        "json",
                    ]
                    if pure:
                        feedback_inner.append("--pure")

                    # Never pass tool_choice="required" via worker config/CLI for these
                    # runs; provider path rejects it and the harness guard test enforces this.

                    newly_passing = (
                        sorted(
                            set(attempt_reports[-2]["failed_gates"])
                            - set(attempt_reports[-1]["failed_gates"])
                        )
                        if len(attempt_reports) >= 2
                        else []
                    )
                    still_failing = sorted(set(attempt_reports[-1]["failed_gates"]))
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-verdict-composed attempt={attempt} "
                        f"newly_passing_count={len(newly_passing)} still_failing_count={len(still_failing)}"
                    )

                    next_attempt_cost_usd = 0.0
                    if newly_passing:
                        pass_verdict = self._build_pass_verdict(newly_passing=newly_passing)
                        if pass_verdict:
                            self._progress(
                                f"PROGRESS run_label={run_label} step=feedback-pass-injection attempt={attempt} "
                                f"newly_passing_count={len(newly_passing)} session_id={session_id}"
                            )
                            self._emit_cost_target_warning_if_reached(
                                run_label=run_label,
                                phase=f"verdict-pass-{attempt}",
                                cumulative_cost_usd=cell_cost_usd,
                            )
                            self._append_user_event(
                                run_label=run_label,
                                sidecar_path=user_events_path,
                                attempt=next_attempt,
                                text=pass_verdict,
                            )
                            self._write_worker_permission_config(worktree=worktree)
                            pass_run = self._run_opencode_with_zero_tool_resumes(
                                active_cell=active_cell,
                                initial_inner=feedback_inner,
                                pure=pure,
                                worktree=worktree,
                                events_path=events_path,
                                env=run_env,
                                run_label=run_label,
                                phase=f"verdict-pass-{attempt}",
                                fallback_session_id=session_id,
                                prior_cost_usd=cell_cost_usd,
                                kill_hook=active_cell.kill_worker_processes,
                                stdin_text=pass_verdict,
                            )
                            next_attempt_cost_usd += pass_run.cost_usd
                            cell_cost_usd += pass_run.cost_usd
                            if pass_run.session_id:
                                session_id = pass_run.session_id

                            input_tokens_total += pass_run.input_tokens
                            output_tokens_total += pass_run.output_tokens + pass_run.reasoning_tokens
                            turns_total += pass_run.turns
                            truncations_total += pass_run.truncations
                            zero_tool_turns_total += pass_run.zero_tool_turns
                            zero_tool_resumes_total += pass_run.zero_tool_resumes
                            if pass_run.zero_tool_turn_honest_fail:
                                zero_tool_turn_honest_fails_total += 1
                            worker_killed_reason = pass_run.killed_reason
                            self._progress(
                                f"PROGRESS run_label={run_label} step=feedback-pass-injection-done attempt={attempt} "
                                f"exit={pass_run.exit_code} killed={pass_run.killed_reason or 'none'} "
                                f"turns={pass_run.turns} input={pass_run.input_tokens} "
                                f"output={pass_run.output_tokens} reasoning={pass_run.reasoning_tokens} "
                                f"cost_usd={pass_run.cost_usd:.4f} cell_cost_usd={cell_cost_usd:.4f}"
                            )
                            if pass_run.budget_stop_detected:
                                verdict = "BUDGET_STOP"
                                attempts_to_green = "BUDGET_STOP"
                                termination_reason = "budget_stop_mid_attempt"
                                break
                            if pass_run.zero_tool_turn_honest_fail:
                                verdict = "FAIL"
                                attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                                termination_reason = "zero_tool_turn_honest_fail"
                                if attempt_reports:
                                    attempt_reports[-1]["zero_tool_turn_honest_fail"] = True
                                break
                            if (
                                pass_run.exit_code not in (0, None)
                                and pass_run.killed_reason not in _HARNESS_LIMIT_REASONS
                            ):
                                verdict = "FAIL"
                                attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                                termination_reason = "harness_error"
                                break

                    feedback_checks = [
                        str(p.get("check", "")).strip()
                        for p in problems
                        if isinstance(p, dict) and str(p.get("check", "")).strip()
                    ]
                    feedback = self._build_feedback_prompt(
                        checks=feedback_checks,
                        had_pass_verdict=bool(newly_passing),
                    )
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-problems-only-built attempt={attempt} "
                        f"checks={len(feedback_checks)}"
                    )
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-injection attempt={attempt} "
                        f"problem_count={len(problems)} session_id={session_id}"
                    )

                    self._emit_cost_target_warning_if_reached(
                        run_label=run_label,
                        phase=f"feedback-{attempt}",
                        cumulative_cost_usd=cell_cost_usd,
                    )

                    self._append_user_event(
                        run_label=run_label,
                        sidecar_path=user_events_path,
                        attempt=next_attempt,
                        text=feedback,
                    )

                    self._write_worker_permission_config(worktree=worktree)

                    feedback_run = self._run_opencode_with_zero_tool_resumes(
                        active_cell=active_cell,
                        initial_inner=feedback_inner,
                        pure=pure,
                        worktree=worktree,
                        events_path=events_path,
                        env=run_env,
                        run_label=run_label,
                        phase=f"feedback-{attempt}",
                        fallback_session_id=session_id,
                        prior_cost_usd=cell_cost_usd,
                        kill_hook=active_cell.kill_worker_processes,
                        stdin_text=feedback,
                    )
                    next_attempt_cost_usd += feedback_run.cost_usd
                    attempt_costs_usd[next_attempt] = next_attempt_cost_usd
                    observed_attempt_costs.append(next_attempt_cost_usd)
                    cell_cost_usd += feedback_run.cost_usd
                    if feedback_run.session_id:
                        session_id = feedback_run.session_id

                    input_tokens_total += feedback_run.input_tokens
                    output_tokens_total += feedback_run.output_tokens + feedback_run.reasoning_tokens
                    turns_total += feedback_run.turns
                    truncations_total += feedback_run.truncations
                    zero_tool_turns_total += feedback_run.zero_tool_turns
                    zero_tool_resumes_total += feedback_run.zero_tool_resumes
                    if feedback_run.zero_tool_turn_honest_fail:
                        zero_tool_turn_honest_fails_total += 1
                    worker_killed_reason = feedback_run.killed_reason
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-injection-done attempt={attempt} "
                        f"exit={feedback_run.exit_code} killed={feedback_run.killed_reason or 'none'} "
                        f"turns={feedback_run.turns} input={feedback_run.input_tokens} "
                        f"output={feedback_run.output_tokens} reasoning={feedback_run.reasoning_tokens} "
                        f"cost_usd={feedback_run.cost_usd:.4f} cell_cost_usd={cell_cost_usd:.4f}"
                    )
                    if feedback_run.budget_stop_detected:
                        verdict = "BUDGET_STOP"
                        attempts_to_green = "BUDGET_STOP"
                        termination_reason = "budget_stop_mid_attempt"
                        break
                    if feedback_run.zero_tool_turn_honest_fail:
                        verdict = "FAIL"
                        attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                        termination_reason = "zero_tool_turn_honest_fail"
                        if attempt_reports:
                            attempt_reports[-1]["zero_tool_turn_honest_fail"] = True
                        break
                    if (
                        feedback_run.exit_code not in (0, None)
                        and feedback_run.killed_reason not in _HARNESS_LIMIT_REASONS
                    ):
                        verdict = "FAIL"
                        attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                        termination_reason = "harness_error"
                        break

        if termination_reason == "pending":
            verdict = "FAIL"
            attempts_to_green = "FAIL"
            termination_reason = "harness_error"

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
            termination_reason = "cheat_detected"
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

        if attempt_reports:
            attempt_reports[-1]["termination_reason"] = termination_reason

        tool_calls_count, test_invocations_count = self._extract_event_counts(events_path)
        agentic_cycles_count = self._extract_agentic_cycles(user_events_path)
        problems_before_count: int | None = None
        if attempt_reports:
            first_n_problems = attempt_reports[0].get("n_problems")
            if isinstance(first_n_problems, int):
                problems_before_count = first_n_problems

        if str(self.memory_mode).strip().lower() == "on":
            scanned_delivery = _scan_cell_delivery(worktree)
            delivery = scanned_delivery if scanned_delivery is not None else "not_measured"
            injected_block_chars = _scan_injected_block_chars(worktree)
            injected_block_est_tokens = (
                round(injected_block_chars / 4)
                if injected_block_chars is not None
                else None
            )
            funnel = _scan_recall_funnel(worktree)
            recall_fired_total = funnel.recall_fired_total if funnel is not None else None
            recall_fired_user_message = (
                funnel.recall_fired_user_message if funnel is not None else None
            )
            recall_fired_tool_failure = (
                funnel.recall_fired_tool_failure if funnel is not None else None
            )
            recall_returned_total = funnel.recall_returned_total if funnel is not None else None
            recall_returned_count_sum = (
                funnel.recall_returned_count_sum if funnel is not None else None
            )
            no_keywords_count = funnel.no_keywords_count if funnel is not None else None
            injected_count = funnel.injected_count if funnel is not None else None
            served_attempted = funnel.served_attempted if funnel is not None else None
            served_failed = funnel.served_failed if funnel is not None else None
            served_confirmed = funnel.served_confirmed if funnel is not None else None
        else:
            delivery = "N/A"
            injected_block_chars = None
            injected_block_est_tokens = None
            recall_fired_total = None
            recall_fired_user_message = None
            recall_fired_tool_failure = None
            recall_returned_total = None
            recall_returned_count_sum = None
            no_keywords_count = None
            injected_count = None
            served_attempted = None
            served_failed = None
            served_confirmed = None
        self._progress(
            f"PROGRESS run_label={run_label} step=delivery-scan delivery={delivery} "
            f"memory_mode={self.memory_mode}"
        )

        return BackgammonCellResult(
            verdict=verdict,
            attempts_to_green=attempts_to_green,
            termination_reason=termination_reason,
            conformed=bool(final_report.get("conformed", False)),
            input_tokens=input_tokens_total,
            output_tokens=output_tokens_total,
            turns=turns_total,
            wall_seconds=wall_seconds,
            delivery=delivery,
            failed_gates=failed_gates_final,
            problems_final=problems_final,
            attempt_reports=attempt_reports,
            worktree=str(worktree),
            session_id=session_id,
            memory_mode=self.memory_mode,
            model=self.model,
            wall_cost_usd=cell_cost_usd,
            cheated=cheated,
            cheat_detail=cheat_detail,
            tool_calls=tool_calls_count,
            test_invocations=test_invocations_count,
            agentic_cycles=agentic_cycles_count,
            problems_before=problems_before_count,
            injected_block_chars=injected_block_chars,
            injected_block_est_tokens=injected_block_est_tokens,
            recall_fired_total=recall_fired_total,
            recall_fired_user_message=recall_fired_user_message,
            recall_fired_tool_failure=recall_fired_tool_failure,
            recall_returned_total=recall_returned_total,
            recall_returned_count_sum=recall_returned_count_sum,
            no_keywords_count=no_keywords_count,
            injected_count=injected_count,
            served_attempted=served_attempted,
            served_failed=served_failed,
            served_confirmed=served_confirmed,
            truncations=truncations_total,
            zero_tool_turns=zero_tool_turns_total,
            zero_tool_resumes=zero_tool_resumes_total,
            zero_tool_turn_honest_fails=zero_tool_turn_honest_fails_total,
        )

    def _run_opencode_with_zero_tool_resumes(
        self,
        *,
        active_cell: DockerCell,
        initial_inner: list[str],
        pure: bool,
        worktree: Path,
        events_path: Path,
        env: dict[str, str],
        run_label: str,
        phase: str,
        fallback_session_id: str | None,
        prior_cost_usd: float = 0.0,
        kill_hook: Callable[[], None] | None = None,
        stdin_text: str | None = None,
    ) -> _OpencodeRunStats:
        aggregate = self._run_opencode(
            cmd=active_cell.exec_argv(initial_inner),
            worktree=worktree,
            events_path=events_path,
            env=env,
            run_label=run_label,
            phase=phase,
            fallback_session_id=fallback_session_id,
            prior_cost_usd=prior_cost_usd,
            kill_hook=kill_hook,
            stdin_text=stdin_text,
        )

        resume_count = 0
        current = aggregate
        budget_stop_detected_any = bool(aggregate.budget_stop_detected)
        budget_stop_signature = aggregate.budget_stop_signature

        while current.terminal_zero_tool_turn and resume_count < MAX_ZERO_TOOL_RESUMES:
            resume_session_id = current.session_id or aggregate.session_id or fallback_session_id
            if not resume_session_id:
                break
            resume_count += 1
            self._progress(
                f"PROGRESS run_label={run_label} step=zero-tool-turn-resume phase={phase} "
                f"resume={resume_count}/{MAX_ZERO_TOOL_RESUMES} session_id={resume_session_id}"
            )
            resume_inner = [
                "opencode",
                "run",
                "--session",
                str(resume_session_id),
                "--dir",
                "/work",
                "--format",
                "json",
            ]
            if pure:
                resume_inner.append("--pure")

            resumed = self._run_opencode(
                cmd=active_cell.exec_argv(resume_inner),
                worktree=worktree,
                events_path=events_path,
                env=env,
                run_label=run_label,
                phase=f"{phase}-zero-tool-resume-{resume_count}",
                fallback_session_id=str(resume_session_id),
                prior_cost_usd=prior_cost_usd + aggregate.cost_usd,
                kill_hook=kill_hook,
                stdin_text=ZERO_TOOL_RESUME_NUDGE,
            )

            budget_stop_detected_any = budget_stop_detected_any or resumed.budget_stop_detected
            if budget_stop_signature is None and resumed.budget_stop_signature is not None:
                budget_stop_signature = resumed.budget_stop_signature

            aggregate = _OpencodeRunStats(
                input_tokens=aggregate.input_tokens + resumed.input_tokens,
                output_tokens=aggregate.output_tokens + resumed.output_tokens,
                reasoning_tokens=aggregate.reasoning_tokens + resumed.reasoning_tokens,
                turns=aggregate.turns + resumed.turns,
                session_id=resumed.session_id or aggregate.session_id,
                killed_reason=resumed.killed_reason or aggregate.killed_reason,
                exit_code=resumed.exit_code,
                cost_usd=aggregate.cost_usd + resumed.cost_usd,
                budget_stop_detected=budget_stop_detected_any,
                budget_stop_signature=budget_stop_signature,
                truncations=aggregate.truncations + resumed.truncations,
                zero_tool_turns=aggregate.zero_tool_turns + resumed.zero_tool_turns,
                terminal_zero_tool_turn=resumed.terminal_zero_tool_turn,
                zero_tool_resumes=resume_count,
                zero_tool_turn_honest_fail=False,
            )
            current = resumed

        honest_fail = bool(aggregate.terminal_zero_tool_turn)
        if honest_fail:
            self._progress(
                f"PROGRESS run_label={run_label} step=zero-tool-turn outcome=honest-fail phase={phase} "
                f"resumes={resume_count}/{MAX_ZERO_TOOL_RESUMES}"
            )

        return _OpencodeRunStats(
            input_tokens=aggregate.input_tokens,
            output_tokens=aggregate.output_tokens,
            reasoning_tokens=aggregate.reasoning_tokens,
            turns=aggregate.turns,
            session_id=aggregate.session_id,
            killed_reason=aggregate.killed_reason,
            exit_code=aggregate.exit_code,
            cost_usd=aggregate.cost_usd,
            budget_stop_detected=aggregate.budget_stop_detected,
            budget_stop_signature=aggregate.budget_stop_signature,
            truncations=aggregate.truncations,
            zero_tool_turns=aggregate.zero_tool_turns,
            terminal_zero_tool_turn=aggregate.terminal_zero_tool_turn,
            zero_tool_resumes=resume_count,
            zero_tool_turn_honest_fail=honest_fail,
        )

    def _write_worker_permission_config(self, *, worktree: Path) -> None:
        gates_dir = str((self.task_dir / "gates").resolve())
        golden_dir = str((self.task_dir / "golden").resolve())
        config = build_worker_opencode_config(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            proxy_base_url=self.proxy_base_url,
            gates_dir=gates_dir,
            golden_dir=golden_dir,
            session_id=self.session_id,
        )
        session_header_set = bool(self.session_id)
        provider_id, _, model_id = self.model.partition("/")
        if provider_id and model_id:
            self._progress(
                "PROGRESS step=worker-permission-config "
                f"model_declared={model_id} provider={provider_id} "
                f"session_header_set={str(session_header_set).lower()}"
            )

        self._progress(
            "PROGRESS step=worker-permission-config-provider "
            f"provider={provider_id or 'none'} model={model_id or 'none'} "
            f"proxy_base_url_set={str(bool(self.proxy_base_url)).lower()} "
            f"session_header_set={str(session_header_set).lower()}"
        )

        # Output token caps are enforced via Docker env
        # OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX, not model `options.max_tokens`
        # in opencode.json.
        if provider_id and model_id and self.reasoning_effort is not None:
            self._progress(
                "PROGRESS step=worker-permission-config "
                f"reasoning_effort={self.reasoning_effort} model={self.model}"
            )
        (worktree / "opencode.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        self._progress(
            "PROGRESS step=worker-permission-config external_directory=deny "
            "oracle_bash_deny=active skip_permissions_removed=true"
        )

    def _build_cell_config(self, *, worktree: Path, container_name: str) -> DockerCellConfig:
        cell_config = DockerCellConfig(
            worktree=worktree,
            memory_mode=self.memory_mode,
            container_name=container_name,
        )
        cell_config.plugin_state_host_path = str(worktree / ".wevibe" / "state")
        cell_config.output_token_max = self.max_output_tokens
        cell_config.proxy_base_url = self.proxy_base_url
        cell_config.proxy_token = self.proxy_token
        cell_config.worker_logs_dir = worktree.parent / "worker-logs"
        return cell_config

    def _init_worktree_git(self, *, worktree: Path) -> None:
        # opencode resolves the session worktree by walking up from --dir /work
        # looking for .git; with no .git at/above the bind-mount root it falls
        # back to "/", so the wevibe plugin reads /.wevibe/org.json (absent)
        # and the session stays DORMANT. git-init the seeded worktree so the
        # plugin resolves worktree=/work and reads /work/.wevibe/org.json.
        subprocess.run(
            ["git", "init"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "bench@wevibe.local"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "wevibe-bench"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "bench cell seed"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
        self._progress(f"PROGRESS step=worktree-git-init path={worktree}")

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
    def _build_pass_verdict(*, newly_passing: list[str]) -> str:
        if not newly_passing:
            return ""

        deduped: list[str] = []
        seen: set[str] = set()
        for item in newly_passing:
            first_line = str(item).split("\n", 1)[0]
            sanitized = " ".join(first_line.split())
            if len(sanitized) > 80:
                sanitized = sanitized[:80]
            if not sanitized or sanitized in seen:
                continue
            seen.add(sanitized)
            deduped.append(sanitized)

        if not deduped:
            return ""
        if len(deduped) == 1:
            return f"That fixed it — {deduped[0]} works now."

        shown = deduped[:3]
        joined = ", ".join(shown)
        remaining = len(deduped) - len(shown)
        if remaining > 0:
            joined = f"{joined} and {remaining} more"
        return f"That fixed it — {joined} all pass now."

    @staticmethod
    def _build_feedback_prompt(*, checks: list[str], had_pass_verdict: bool = False) -> str:
        header = (
            "The rest are still failing — fix the implementation so they pass. Do not explain, just edit the code."
            if had_pass_verdict
            else "These are still failing — fix the implementation so they pass. Do not explain, just edit the code."
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
        prior_cost_usd: float = 0.0,
        kill_hook: Callable[[], None] | None = None,
        stdin_text: str | None = None,
    ) -> _OpencodeRunStats:
        state_lock = threading.Lock()
        state: dict[str, Any] = {
            "session_id": fallback_session_id,
            "turns": 0,
            "sum_output": 0,
            "sum_reasoning": 0,
            "max_input": 0,
            "sum_cost": 0.0,
            "completed_at": None,
            "budget_stop_detected": False,
            "budget_stop_signature": None,
            "truncations": 0,
            "zero_tool_turns": 0,
            "terminal_zero_tool_turn": False,
            "active_tool_uses": 0,
            "active_file_writes": 0,
            "active_step_index": 0,
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
                stdin=subprocess.PIPE if stdin_text is not None else None,
                text=True,
                bufsize=1,
                start_new_session=True,
                env=env,
            )

            stdin_writer_thread: threading.Thread | None = None
            if stdin_text is not None:
                payload = str(stdin_text)
                payload_chars = len(payload)
                payload_fp = self._fingerprint_text(payload)

                def stdin_writer() -> None:
                    try:
                        if proc.stdin is None:
                            self._progress(
                                f"INFO op=worker-stdin-write run_label={run_label} phase={phase} "
                                "status=skipped reason=stdin_not_available"
                            )
                            return
                        proc.stdin.write(payload)
                        proc.stdin.flush()
                        self._progress(
                            f"PROGRESS op=worker-stdin-write run_label={run_label} phase={phase} "
                            f"status=ok chars={payload_chars} text_fp={payload_fp}"
                        )
                    except BrokenPipeError:
                        self._progress(
                            f"INFO op=worker-stdin-write run_label={run_label} phase={phase} "
                            f"status=broken_pipe chars={payload_chars} text_fp={payload_fp}"
                        )
                    except Exception as exc:  # noqa: BLE001 - surface and continue teardown.
                        reader_failures.append(f"stdin writer failure ({phase}): {exc}")
                    finally:
                        try:
                            if proc.stdin:
                                proc.stdin.close()
                        except Exception:
                            pass

                stdin_writer_thread = threading.Thread(
                    target=stdin_writer,
                    name=f"bg-stdin-{phase}",
                    daemon=True,
                )
                stdin_writer_thread.start()

            def stdout_reader() -> None:
                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        events_fh.write(line)
                        events_fh.flush()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            if self._line_indicates_budget_stop(line):
                                line_fp = self._fingerprint_text(line)
                                with state_lock:
                                    state["budget_stop_detected"] = True
                                    if not state["budget_stop_signature"]:
                                        state["budget_stop_signature"] = f"stdout_line_fp={line_fp}"
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=budget-stop-detected phase={phase} "
                                    f"source=stdout-unparsed line_fp={line_fp}"
                                )
                            continue

                        sid = event.get("sessionID")
                        with state_lock:
                            if sid and not state["session_id"]:
                                state["session_id"] = str(sid)

                        event_type = event.get("type")
                        if event_type == "error":
                            signal = self._budget_stop_signature_from_event(event)
                            if signal is not None:
                                with state_lock:
                                    state["budget_stop_detected"] = True
                                    if not state["budget_stop_signature"]:
                                        state["budget_stop_signature"] = signal
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=budget-stop-detected phase={phase} "
                                    f"source=event-error signal={signal}"
                                )
                            continue
                        if event_type == "step_start":
                            with state_lock:
                                state["completed_at"] = None
                                state["active_tool_uses"] = 0
                                state["active_file_writes"] = 0
                                state["active_step_index"] = self._to_int(state.get("active_step_index", 0)) + 1
                                state["terminal_zero_tool_turn"] = False
                            continue
                        if event_type == "tool_use":
                            part = event.get("part") if isinstance(event.get("part"), dict) else {}
                            tool_name = part.get("tool")
                            if not isinstance(tool_name, str):
                                tool_name = part.get("name")
                            tool_name_normalized = str(tool_name).strip().lower()
                            with state_lock:
                                state["active_tool_uses"] = self._to_int(state.get("active_tool_uses", 0)) + 1
                                if tool_name_normalized in _FILE_WRITE_TOOL_NAMES:
                                    state["active_file_writes"] = self._to_int(state.get("active_file_writes", 0)) + 1
                            continue
                        if event_type != "step_finish":
                            continue

                        part = event.get("part") if isinstance(event.get("part"), dict) else {}
                        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                        reason = part.get("reason")
                        input_tokens = max(0, self._to_int(tokens.get("input")))
                        output_tokens = max(0, self._to_int(tokens.get("output")))
                        reasoning_tokens = max(0, self._to_int(tokens.get("reasoning")))
                        step_cost = part.get("cost")

                        with state_lock:
                            state["turns"] += 1
                            state["sum_output"] += output_tokens
                            state["sum_reasoning"] += reasoning_tokens
                            state["sum_cost"] += float(step_cost) if isinstance(step_cost, (int, float)) else 0.0
                            if input_tokens > state["max_input"]:
                                state["max_input"] = input_tokens
                            if reason == "stop":
                                state["completed_at"] = time.monotonic()
                            elif reason == "length":
                                state["truncations"] = self._to_int(state.get("truncations", 0)) + 1
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=TRUNCATION phase={phase} "
                                    f"turn={state['turns']} reason=length"
                                )

                            tool_uses = self._to_int(state.get("active_tool_uses", 0))
                            file_writes = self._to_int(state.get("active_file_writes", 0))
                            if tool_uses == 0 and file_writes == 0:
                                state["zero_tool_turns"] = self._to_int(state.get("zero_tool_turns", 0)) + 1
                                state["terminal_zero_tool_turn"] = True
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=zero-tool-turn phase={phase} "
                                    f"turn={state['turns']} tool_uses=0 file_writes=0"
                                )
                            else:
                                state["terminal_zero_tool_turn"] = False

                            state["active_tool_uses"] = 0
                            state["active_file_writes"] = 0
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
                        if self._line_indicates_budget_stop(text):
                            line_fp = self._fingerprint_text(text)
                            with state_lock:
                                state["budget_stop_detected"] = True
                                if not state["budget_stop_signature"]:
                                    state["budget_stop_signature"] = f"stderr_line_fp={line_fp}"
                            self._progress(
                                f"PROGRESS run_label={run_label} step=budget-stop-detected phase={phase} "
                                f"source=stderr line_fp={line_fp}"
                            )
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
            target_warning_emitted = False

            if self.cost_target_usd is not None and prior_cost_usd >= self.cost_target_usd:
                target_warning_emitted = True
                self._progress(
                    f"WARNING run_label={run_label} step=cost-target phase={phase} "
                    f"reason=cost_target_reached cumulative_cost_usd={prior_cost_usd:.4f} "
                    f"target_usd={self.cost_target_usd:.4f}"
                )

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
                    turns = self._to_int(state["turns"])
                    sum_cost = float(state["sum_cost"])
                    cumulative_cost = prior_cost_usd + sum_cost

                if (
                    self.cost_target_usd is not None
                    and not target_warning_emitted
                    and cumulative_cost >= self.cost_target_usd
                ):
                    target_warning_emitted = True
                    self._progress(
                        f"WARNING run_label={run_label} step=cost-target phase={phase} "
                        f"reason=cost_target_reached cumulative_cost_usd={cumulative_cost:.4f} "
                        f"target_usd={self.cost_target_usd:.4f}"
                    )

                if rc is not None:
                    break
                # Bound process teardown after a final stop while allowing any subsequent resumed step to reset the grace window.
                with state_lock:
                    completed_at = state["completed_at"]
                if completed_at is not None and (time.monotonic() - completed_at) >= self.completion_grace_s:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-complete phase={phase} "
                        f"reason=idle_after_stop grace={self.completion_grace_s}s elapsed={elapsed:.2f}s"
                    )
                    self._kill_process_group(proc)
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
                if self.max_steps_per_attempt is not None and turns > self.max_steps_per_attempt:
                    killed_reason = "max_steps_per_attempt"
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill phase={phase} "
                        f"reason=max_steps_per_attempt turns={turns} max_steps_per_attempt={self.max_steps_per_attempt}"
                    )
                    self._kill_process_group(proc)
                    run_kill_hook(reason="max_steps_per_attempt")
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
            if stdin_writer_thread is not None:
                stdin_writer_thread.join(timeout=5)
                if stdin_writer_thread.is_alive():
                    self._progress(
                        f"INFO op=worker-stdin-write run_label={run_label} phase={phase} "
                        "status=join_timeout timeout_s=5"
                    )
            exit_code = proc.returncode

        if (
            self.cost_target_usd is not None
            and not target_warning_emitted
            and (prior_cost_usd + float(state.get("sum_cost", 0.0))) >= self.cost_target_usd
        ):
            self._progress(
                f"WARNING run_label={run_label} step=cost-target phase={phase} "
                f"reason=cost_target_reached cumulative_cost_usd={(prior_cost_usd + float(state.get('sum_cost', 0.0))):.4f} "
                f"target_usd={self.cost_target_usd:.4f}"
            )

        for failure in reader_failures:
            self._progress(f"PROGRESS run_label={run_label} step=reader-failure phase={phase} detail={failure}")

        with state_lock:
            session_id = state["session_id"]
            turns = self._to_int(state["turns"])
            input_tokens = self._to_int(state["max_input"])
            output_tokens = self._to_int(state["sum_output"])
            reasoning_tokens = self._to_int(state["sum_reasoning"])
            cost_usd = float(state["sum_cost"])
            budget_stop_detected = bool(state.get("budget_stop_detected", False))
            budget_stop_signature = state.get("budget_stop_signature")
            truncations = self._to_int(state.get("truncations", 0))
            zero_tool_turns = self._to_int(state.get("zero_tool_turns", 0))
            terminal_zero_tool_turn = bool(state.get("terminal_zero_tool_turn", False))

        if exit_code not in (0, None) and stderr_tail:
            self._progress(
                f"PROGRESS run_label={run_label} step=worker-nonzero phase={phase} exit={exit_code} "
                f"stderr_tail={' | '.join(stderr_tail)}"
            )

        external_signal = self._external_signal_from_exit_code(exit_code)
        if (
            exit_code not in (0, None)
            and killed_reason is None
            and not kill_hook_ran
            and external_signal is not None
        ):
            self._progress(
                f"PROGRESS op=worker.external_exit run_label={run_label} phase={phase} "
                f"exit={exit_code} signal={external_signal} attribution=external killed_reason=none"
            )

        if not budget_stop_detected and stderr_tail:
            stderr_blob = "\n".join(stderr_tail)
            if self._line_indicates_budget_stop(stderr_blob):
                budget_stop_detected = True
                budget_stop_signature = f"stderr_tail_fp={self._fingerprint_text(stderr_blob)}"
                self._progress(
                    f"PROGRESS run_label={run_label} step=budget-stop-detected phase={phase} "
                    f"source=stderr-tail signal={budget_stop_signature}"
                )

        return _OpencodeRunStats(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            turns=turns,
            session_id=session_id,
            killed_reason=killed_reason,
            exit_code=exit_code,
            cost_usd=cost_usd,
            budget_stop_detected=budget_stop_detected,
            budget_stop_signature=None if budget_stop_signature is None else str(budget_stop_signature),
            truncations=truncations,
            zero_tool_turns=zero_tool_turns,
            terminal_zero_tool_turn=terminal_zero_tool_turn,
        )

    def _emit_cost_target_warning_if_reached(
        self,
        *,
        run_label: str,
        phase: str,
        cumulative_cost_usd: float,
    ) -> None:
        if self.cost_target_usd is None:
            return
        if cumulative_cost_usd < self.cost_target_usd:
            return
        self._progress(
            f"WARNING run_label={run_label} step=cost-target phase={phase} "
            f"reason=cost_target_reached cumulative_cost_usd={cumulative_cost_usd:.4f} "
            f"target_usd={self.cost_target_usd:.4f}"
        )

    def _append_user_event(
        self,
        *,
        run_label: str,
        sidecar_path: Path,
        attempt: int,
        text: str,
    ) -> None:
        payload = {
            "type": "user",
            "timestamp": int(time.time() * 1000),
            "attempt": int(attempt),
            "text": str(text),
        }
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        with sidecar_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._progress(
            f"PROGRESS run_label={run_label} step=user-event-sidecar attempt={attempt} "
            f"chars={len(text)} text_fp={self._fingerprint_text(text)} path={sidecar_path}"
        )

    def _extract_event_counts(self, events_path: Path) -> tuple[int | None, int | None]:
        """Return (tool_calls, test_invocations) from an opencode events jsonl file.

        test_invocations counts bash tool_use events where state.input.command
        contains any DECLARED_TEST_COMMANDS entry (plain case-sensitive
        substring match).
        """
        malformed_lines = 0
        tool_calls = 0
        test_invocations = 0

        try:
            with events_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue

                    if not isinstance(payload, dict):
                        malformed_lines += 1
                        continue

                    if payload.get("type") != "tool_use":
                        continue

                    tool_calls += 1
                    part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
                    if part.get("tool") != "bash":
                        continue

                    state = part.get("state") if isinstance(part.get("state"), dict) else {}
                    tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
                    command = tool_input.get("command")
                    if not isinstance(command, str):
                        continue

                    if any(declared in command for declared in DECLARED_TEST_COMMANDS):
                        test_invocations += 1
        except OSError as exc:
            _LOG.warning(
                "backgammon event telemetry unavailable path=%s error_class=%s",
                events_path,
                exc.__class__.__name__,
            )
            return None, None

        if malformed_lines > 0:
            _LOG.warning(
                "backgammon event telemetry malformed_lines=%d path=%s",
                malformed_lines,
                events_path,
            )

        return tool_calls, test_invocations

    def _extract_agentic_cycles(self, user_events_path: Path) -> int | None:
        """Return number of context-submission cycles from user-events jsonl.

        One cycle equals one user context submission (initial prompt plus each
        feedback injection). When attempt fields exist, cycles are counted as
        distinct attempt values; if parsed user lines have no attempt fields,
        fallback is the number of parsed user lines.
        """
        malformed_lines = 0
        user_line_count = 0
        attempts: set[int] = set()
        saw_attempt_field = False

        try:
            with user_events_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue

                    if not isinstance(payload, dict):
                        malformed_lines += 1
                        continue
                    if payload.get("type") != "user":
                        continue

                    user_line_count += 1
                    if "attempt" not in payload:
                        continue

                    attempt = payload.get("attempt")
                    try:
                        attempts.add(int(attempt))
                        saw_attempt_field = True
                    except (TypeError, ValueError):
                        continue
        except OSError as exc:
            _LOG.warning(
                "backgammon user-event telemetry unavailable path=%s error_class=%s",
                user_events_path,
                exc.__class__.__name__,
            )
            return None

        if malformed_lines > 0:
            _LOG.warning(
                "backgammon user-event telemetry malformed_lines=%d path=%s",
                malformed_lines,
                user_events_path,
            )

        if saw_attempt_field:
            return len(attempts)
        return user_line_count

    def _budget_decision_for_attempt(
        self,
        *,
        run_label: str,
        attempt: int,
        observed_attempt_costs: list[float],
    ) -> str:
        estimate_usd = self._estimate_full_attempt_cost_usd(
            observed_attempt_costs,
            fallback_usd=self._fallback_attempt_estimate_usd,
        )
        checkpoint_path = self._proxy_checkpoint_path()

        if checkpoint_path is None:
            if self.cost_limit_usd is None:
                self._progress(
                    f"PROGRESS run_label={run_label} step=budget-decision attempt={attempt} "
                    f"decision=allow source=unbounded remaining_usd=inf estimate_attempt_usd={estimate_usd:.6f}"
                )
                return "allow"
            self._progress(
                f"PROGRESS run_label={run_label} step=budget-decision attempt={attempt} "
                f"decision=harness_error reason=missing_checkpoint_env env={_PROXY_CHECKPOINT_ENV} "
                f"estimate_attempt_usd={estimate_usd:.6f}"
            )
            return "harness_error"

        try:
            snapshot = self._read_proxy_budget_snapshot(checkpoint_path=checkpoint_path)
        except Exception as exc:  # noqa: BLE001 - classified as harness_error upstream.
            self._progress(
                f"PROGRESS run_label={run_label} step=budget-decision attempt={attempt} "
                f"decision=harness_error reason=checkpoint_read_error checkpoint={checkpoint_path} "
                f"error_fp={self._fingerprint_text(str(exc))}"
            )
            return "harness_error"

        decision = "allow" if snapshot.remaining_usd >= estimate_usd else "budget_stop"
        configured_cap = "none" if self.cost_limit_usd is None else f"{self.cost_limit_usd:.6f}"
        self._progress(
            f"PROGRESS run_label={run_label} step=budget-decision attempt={attempt} decision={decision} "
            f"remaining_usd={snapshot.remaining_usd:.6f} estimate_attempt_usd={estimate_usd:.6f} "
            f"hard_cap_usd={snapshot.hard_cap_usd:.6f} accrued_actual_usd={snapshot.accrued_actual_usd:.6f} "
            f"accrued_derived_usd={snapshot.accrued_derived_usd:.6f} "
            f"committed_unproven_usd={snapshot.committed_unproven_usd:.6f} cost_limit_usd={configured_cap} "
            f"checkpoint={snapshot.checkpoint_path}"
        )
        return decision

    @staticmethod
    def _proxy_checkpoint_path() -> Path | None:
        raw = os.environ.get(_PROXY_CHECKPOINT_ENV, "").strip()
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    @staticmethod
    def _estimate_full_attempt_cost_usd(observed_attempt_costs: list[float], fallback_usd: float = 0.0) -> float:
        observed_max = 0.0
        for value in observed_attempt_costs:
            if isinstance(value, (int, float)):
                observed_max = max(observed_max, float(value))
        return max(observed_max, float(fallback_usd), 0.0)

    def _read_proxy_budget_snapshot(self, *, checkpoint_path: Path) -> _ProxyBudgetSnapshot:
        if not checkpoint_path.is_file():
            raise RuntimeError(f"proxy checkpoint missing: {checkpoint_path}")

        last_error: Exception | None = None
        for _ in range(3):
            try:
                payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError("proxy checkpoint payload is not an object")
                hard_cap_usd = float(payload["hard_cap_usd"])
                accrued_actual_usd = float(payload["accrued_actual_usd"])
                accrued_derived_usd = float(payload.get("accrued_derived_usd", 0.0))
                committed_unproven_usd = float(payload["committed_unproven_usd"])
                remaining_usd = (
                    hard_cap_usd - accrued_actual_usd - accrued_derived_usd - committed_unproven_usd
                )
                return _ProxyBudgetSnapshot(
                    hard_cap_usd=hard_cap_usd,
                    accrued_actual_usd=accrued_actual_usd,
                    accrued_derived_usd=accrued_derived_usd,
                    committed_unproven_usd=committed_unproven_usd,
                    remaining_usd=remaining_usd,
                    checkpoint_path=str(checkpoint_path),
                )
            except Exception as exc:  # noqa: BLE001 - retries for concurrent writes.
                last_error = exc
                time.sleep(0.1)

        raise RuntimeError(
            f"failed reading proxy checkpoint {checkpoint_path}: {last_error}"
        )

    @staticmethod
    def _line_indicates_budget_stop(text: str) -> bool:
        lower = str(text).lower()
        if "budget_exceeded" in lower or "insufficient_quota" in lower:
            return True
        if "statuscode\":402" in lower or "status code: 402" in lower or "status=402" in lower:
            return True
        if "reservation would exceed hard cap" in lower:
            return True
        return False

    @staticmethod
    def _external_signal_from_exit_code(exit_code: int | None) -> str | None:
        if exit_code is None:
            return None
        if exit_code < 128:
            return None

        signal_number = exit_code - 128
        if signal_number < 1:
            return None

        try:
            return signal.Signals(signal_number).name
        except ValueError:
            return f"SIGNAL_{signal_number}"

    def _budget_stop_signature_from_event(self, event: dict[str, Any]) -> str | None:
        if str(event.get("type", "")).strip().lower() != "error":
            return None
        error_block = event.get("error") if isinstance(event.get("error"), dict) else {}
        data = error_block.get("data") if isinstance(error_block.get("data"), dict) else {}
        status_code = self._to_int(data.get("statusCode"))
        message = str(data.get("message", ""))
        response_body = str(data.get("responseBody", ""))

        error_type = ""
        error_code = ""
        if response_body:
            try:
                body_payload = json.loads(response_body)
            except json.JSONDecodeError:
                body_payload = None
            if isinstance(body_payload, dict):
                body_error = body_payload.get("error") if isinstance(body_payload.get("error"), dict) else {}
                error_type = str(body_error.get("type", "")).strip()
                error_code = str(body_error.get("code", "")).strip()
                if not message:
                    message = str(body_error.get("message", ""))

        haystack = " ".join((message, response_body, error_type, error_code)).lower()
        if status_code == 402 or "budget_exceeded" in haystack or "insufficient_quota" in haystack:
            return (
                f"status_code={status_code or 'none'} "
                f"error_type={error_type or 'none'} "
                f"error_code={error_code or 'none'} "
                f"message_fp={self._fingerprint_text(message)} "
                f"body_fp={self._fingerprint_text(response_body)}"
            )
        return None

    @staticmethod
    def _fingerprint_text(text: str) -> str:
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _model_id_from_selector(model: str) -> str:
        provider_id, sep, model_id = str(model).partition("/")
        if sep and model_id:
            return model_id
        return provider_id

    @classmethod
    def _pricing_row_for_model(cls, model: str) -> dict[str, float] | None:
        selector = str(model)
        return _MODEL_PRICING_USD_PER_1M.get(selector) or _MODEL_PRICING_USD_PER_1M.get(
            cls._model_id_from_selector(selector)
        )

    @classmethod
    def _resolve_output_price_per_1m(
        cls,
        *,
        model: str,
        explicit_output_price_per_1m: float | None,
    ) -> float:
        if explicit_output_price_per_1m is not None:
            return float(explicit_output_price_per_1m)
        pricing = cls._pricing_row_for_model(model)
        if pricing is None:
            model_id = cls._model_id_from_selector(model)
            raise ValueError(
                "missing authoritative output pricing for "
                f"model_id={model_id!r}; set output_price_per_1m override to run with cost_limit_usd"
            )
        return float(pricing["output"])

    @classmethod
    def _resolve_cache_write_price_per_1m(
        cls,
        *,
        model: str,
        fallback_price_per_1m: float,
    ) -> float:
        pricing = cls._pricing_row_for_model(model)
        if pricing is not None and "cache_write" in pricing:
            return float(pricing["cache_write"])
        return float(fallback_price_per_1m)

    @staticmethod
    def _worst_case_reservation_usd(
        max_steps: int,
        max_output_tokens: int,
        output_price_per_1m: float,
        safety_factor: float,
        cache_write_allowance_usd: float,
    ) -> float:
        output_price_per_token = float(output_price_per_1m) / 1_000_000.0
        return (
            float(max_steps) * float(max_output_tokens) * output_price_per_token * float(safety_factor)
            + float(cache_write_allowance_usd)
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
