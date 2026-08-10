from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

import wevibe_bench.adapters.backgammon as backgammon_mod
from wevibe_bench.adapters.backgammon import (
    DEFAULT_ATTEMPT_HARD_CEILING,
    DEFAULT_MAX_STEPS_PER_ATTEMPT,
    DEFAULT_RUN_TIMEOUT_S,
    BackgammonRunner,
    CHUNK_MARKER,
    _OpencodeRunStats,
    build_worker_opencode_config,
)
from wevibe_bench.backends.base import RecalledMemory
from wevibe_bench.adapters.docker_worker import DockerCellConfig, _build_run_argv
from wevibe_bench.config import RunConfig


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def _contains_pair(argv: list[str], left: str, right: str) -> bool:
    for idx, item in enumerate(argv[:-1]):
        if item == left and argv[idx + 1] == right:
            return True
    return False


def _make_runner(
    tmp_path: Path,
    *,
    model: str = "local-llm-proxy/kimi/kimi-k3",
    reasoning_effort: str | None = None,
    cost_limit_usd: float | None = None,
    cost_target_usd: float | None = None,
    max_output_tokens: int | None = None,
    max_steps_per_attempt: int | None = None,
    output_price_per_1m: float | None = None,
    max_attempts: int = DEFAULT_ATTEMPT_HARD_CEILING,
) -> BackgammonRunner:
    return BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model=model,
        max_attempts=max_attempts,
        reasoning_effort=reasoning_effort,
        cost_limit_usd=cost_limit_usd,
        cost_target_usd=cost_target_usd,
        max_output_tokens=max_output_tokens,
        max_steps_per_attempt=max_steps_per_attempt,
        output_price_per_1m=output_price_per_1m,
    )


def _write_checkpoint(path: Path, *, hard: float, accrued: float, committed: float) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "test-run",
                "model_id": "anthropic/claude-opus-4.8",
                "profile_name": "opus",
                "hard_cap_usd": hard,
                "accrued_actual_usd": accrued,
                "committed_unproven_usd": committed,
                "outstanding": {},
                "updated_at": "2026-07-22T00:00:00+00:00",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )




def test_worker_run_argv_injects_output_token_env(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, max_output_tokens=8192)
    cfg = DockerCellConfig(
        worktree=tmp_path / "worktree",
        memory_mode="off",
        container_name="wevibe-bench-cell-output-cap-check",
        output_token_max=runner.max_output_tokens,
    )
    run_argv = _build_run_argv(config=cfg, worktree=cfg.worktree, uid=501, gid=20, memory_mode="off")

    assert _contains_pair(run_argv, "-e", "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=8192")


def test_worker_run_argv_omits_output_token_env_when_unclamped(tmp_path: Path) -> None:
    cfg = DockerCellConfig(
        worktree=tmp_path / "worktree",
        memory_mode="off",
        container_name="wevibe-bench-cell-unclamped-check",
        output_token_max=None,
    )
    run_argv = _build_run_argv(config=cfg, worktree=cfg.worktree, uid=501, gid=20, memory_mode="off")

    assert not any(item.startswith("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=") for item in run_argv)


def test_attempt_ceiling_clamps_to_canonical_hard_cap(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, max_attempts=999)
    assert runner.max_attempts == DEFAULT_ATTEMPT_HARD_CEILING

    runner_small = _make_runner(tmp_path, max_attempts=3)
    assert runner_small.max_attempts == 3


def test_canonical_step_cap_is_100_and_cli_default_carries_it() -> None:
    assert DEFAULT_MAX_STEPS_PER_ATTEMPT == 100

    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        run_backgammon = importlib.import_module("run_backgammon")
    finally:
        sys.path.remove(str(scripts_dir))

    parser = run_backgammon._build_arg_parser()
    args = parser.parse_args(["--run-label", "cap-default-check"])
    assert args.max_steps_per_attempt == DEFAULT_MAX_STEPS_PER_ATTEMPT


def test_canonical_run_timeout_is_5400_and_cli_default_carries_it() -> None:
    assert DEFAULT_RUN_TIMEOUT_S == 5400

    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        run_backgammon = importlib.import_module("run_backgammon")
    finally:
        sys.path.remove(str(scripts_dir))

    parser = run_backgammon._build_arg_parser()
    args = parser.parse_args(["--run-label", "timeout-default-check"])
    assert args.run_timeout == DEFAULT_RUN_TIMEOUT_S












def test_load_chunk_prompts_in_order_with_protocol_on_first_chunk(tmp_path: Path) -> None:
    runner_off = _make_runner(tmp_path)
    chunks = runner_off._load_chunk_prompts(injected_memory=[])
    assert len(chunks) == 6
    assert "=== CAPTURE & COMPLIANCE PROTOCOL ===" in chunks[0]
    assert all("=== CAPTURE & COMPLIANCE PROTOCOL ===" not in c for c in chunks[1:])
    assert all(CHUNK_MARKER in c for c in chunks)
    assert not chunks[0].startswith("WORKING STYLE")

    runner_on = BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root-on",
        model="local-llm-proxy/kimi/kimi-k3",
        memory_mode="on",
    )
    chunks_on = runner_on._load_chunk_prompts(injected_memory=[])
    assert "=== CAPTURE & COMPLIANCE PROTOCOL ===" in chunks_on[0]

    chunks_mem = runner_off._load_chunk_prompts(
        injected_memory=[
            RecalledMemory(
                cid="cid-123",
                score=0.95,
                vector_score=0.90,
                combined_score=0.93,
                keyword_score=0.89,
                matched_keywords=["doubling", "bear-off"],
                text="Remember legal move ordering and bar re-entry priority.",
            )
        ]
    )
    assert chunks_mem[0].startswith("# WEVIBE MEMORY CONTEXT")
    assert "# WEVIBE MEMORY CONTEXT" not in chunks_mem[1]


def test_load_chunk_prompts_missing_dir_is_loud(tmp_path: Path) -> None:
    runner = BackgammonRunner(
        task_dir=tmp_path / "no-such-task",
        work_root=tmp_path / "work-root",
        model="local-llm-proxy/kimi/kimi-k3",
        memory_mode="off",
    )
    with pytest.raises(RuntimeError, match="chunked prompts"):
        runner._load_chunk_prompts(injected_memory=[])














































