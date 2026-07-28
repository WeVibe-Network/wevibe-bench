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
    BACKGAMMON_PROMPT,
    WORKER_WORKING_STYLE_PREAMBLE,
    _OpencodeRunStats,
    build_worker_opencode_config,
    reconcile_derived_vs_billing,
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
    model: str = "orcarouter/kimi/kimi-k3",
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


def test_write_worker_config_injects_reasoning_effort(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, reasoning_effort="high")
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    runner._write_worker_permission_config(worktree=worktree)
    config = json.loads((worktree / "opencode.json").read_text(encoding="utf-8"))

    model_block = config["provider"]["orcarouter"]["models"]["kimi/kimi-k3"]
    options = model_block["options"]
    assert model_block["name"] == "Kimi K3"
    assert model_block["reasoning"] is True
    assert model_block["tool_call"] is True
    assert model_block["limit"] == {"context": 1_048_576, "output": 128_000}
    assert model_block["interleaved"] == {"field": "reasoning_content"}
    assert options["reasoning"]["effort"] == "high"
    assert "max_tokens" not in options
    assert "permission" in config
    assert config["permission"]["edit"]["*opencode.json"] == "deny"


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


def test_write_worker_config_declares_model_when_reasoning_unset(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, reasoning_effort=None, max_output_tokens=None)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    runner._write_worker_permission_config(worktree=worktree)
    config = json.loads((worktree / "opencode.json").read_text(encoding="utf-8"))

    model_block = config["provider"]["orcarouter"]["models"]["kimi/kimi-k3"]
    assert model_block["name"] == "Kimi K3"
    assert model_block["reasoning"] is True
    assert model_block["tool_call"] is True
    assert model_block["limit"] == {"context": 1_048_576, "output": 128_000}
    assert model_block["interleaved"] == {"field": "reasoning_content"}
    assert model_block["options"]["reasoning"]["effort"] == "low"


def test_build_worker_opencode_config_plain_roster_declares_orcarouter_model() -> None:
    config = build_worker_opencode_config(
        model="orcarouter/kimi/kimi-k3",
        reasoning_effort=None,
        proxy_base_url=None,
        gates_dir="/g",
        golden_dir="/go",
    )

    model_block = config["provider"]["orcarouter"]["models"]["kimi/kimi-k3"]
    assert model_block["name"] == "Kimi K3"
    assert model_block["reasoning"] is True
    assert model_block["tool_call"] is True
    assert model_block["limit"] == {"context": 1_048_576, "output": 128_000}
    assert model_block["interleaved"] == {"field": "reasoning_content"}
    assert config["provider"]["orcarouter"]["options"]["apiKey"] == "{env:ORCAROUTER_API_KEY}"
    assert "options" not in model_block


def test_build_worker_opencode_config_proxy_base_url_and_models_coexist() -> None:
    config = build_worker_opencode_config(
        model="orcarouter/kimi/kimi-k3",
        reasoning_effort=None,
        proxy_base_url="http://127.0.0.1:8999/api/orcarouter",
        gates_dir="/g",
        golden_dir="/go",
    )

    orcarouter = config["provider"]["orcarouter"]
    assert orcarouter["options"]["baseURL"] == "http://127.0.0.1:8999/api/orcarouter"
    assert orcarouter["options"]["apiKey"] == "{env:ORCAROUTER_API_KEY}"
    assert orcarouter["models"]["kimi/kimi-k3"]["name"] == "Kimi K3"


def test_build_worker_opencode_config_reasoning_effort_keeps_name_and_options() -> None:
    config = build_worker_opencode_config(
        model="orcarouter/kimi/kimi-k3",
        reasoning_effort="high",
        proxy_base_url=None,
        gates_dir="/g",
        golden_dir="/go",
    )

    model_block = config["provider"]["orcarouter"]["models"]["kimi/kimi-k3"]
    assert model_block["name"] == "Kimi K3"
    assert model_block["options"]["reasoning"]["effort"] == "high"


def test_init_rejects_bad_reasoning_effort(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _make_runner(tmp_path, reasoning_effort="ultra")


def test_build_task_prompt_prepends_working_style_preamble_in_all_paths(tmp_path: Path) -> None:
    runner_off = _make_runner(tmp_path)
    prompt_off = runner_off._build_task_prompt(injected_memory=[])
    assert prompt_off.startswith(WORKER_WORKING_STYLE_PREAMBLE)
    assert BACKGAMMON_PROMPT in prompt_off
    assert "Contract:" in prompt_off
    assert "=== CAPTURE & COMPLIANCE PROTOCOL ===" in prompt_off

    runner_on = BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root-on",
        model="orcarouter/kimi/kimi-k3",
        memory_mode="on",
    )
    prompt_on = runner_on._build_task_prompt(injected_memory=[])
    assert prompt_on.startswith(WORKER_WORKING_STYLE_PREAMBLE)
    assert BACKGAMMON_PROMPT in prompt_on
    assert "Contract:" in prompt_on
    assert "=== CAPTURE & COMPLIANCE PROTOCOL ===" in prompt_on

    prompt_with_memory = runner_off._build_task_prompt(
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
    assert prompt_with_memory.startswith(WORKER_WORKING_STYLE_PREAMBLE)
    assert prompt_with_memory.find("# WEVIBE MEMORY CONTEXT") > 0


def test_init_defaults_reasoning_effort_low_for_reasoning_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("WEVIBE_BENCH_REASONING_EFFORT", raising=False)
    runner = _make_runner(tmp_path, model="orcarouter/kimi/kimi-k3", reasoning_effort=None)

    assert runner.reasoning_effort == "low"

    worktree = tmp_path / "reasoning-default-worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    runner._write_worker_permission_config(worktree=worktree)
    config = json.loads((worktree / "opencode.json").read_text(encoding="utf-8"))
    effort = config["provider"]["orcarouter"]["models"]["kimi/kimi-k3"]["options"]["reasoning"]["effort"]
    assert effort == "low"


def test_init_explicit_reasoning_effort_beats_default(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, model="orcarouter/kimi/kimi-k3", reasoning_effort="high")
    assert runner.reasoning_effort == "high"


def test_init_env_reasoning_effort_beats_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEVIBE_BENCH_REASONING_EFFORT", "medium")
    runner = _make_runner(tmp_path, model="orcarouter/kimi/kimi-k3", reasoning_effort=None)
    assert runner.reasoning_effort == "medium"


def test_init_non_reasoning_model_keeps_reasoning_effort_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("WEVIBE_BENCH_REASONING_EFFORT", raising=False)
    runner = _make_runner(tmp_path, model="orcarouter/kimi/kimi-k2.7-code", reasoning_effort=None)

    assert runner.reasoning_effort is None

    worktree = tmp_path / "non-reasoning-worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    runner._write_worker_permission_config(worktree=worktree)
    config = json.loads((worktree / "opencode.json").read_text(encoding="utf-8"))
    model_block = config["provider"]["orcarouter"]["models"]["kimi/kimi-k2.7-code"]
    assert "options" not in model_block


def test_init_rejects_invalid_env_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEVIBE_BENCH_REASONING_EFFORT", "ultra")
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        _make_runner(tmp_path, model="orcarouter/kimi/kimi-k3", reasoning_effort=None)


@pytest.mark.parametrize("bad_limit", [0, -1])
def test_init_rejects_nonpositive_cost_limit(tmp_path: Path, bad_limit: float) -> None:
    with pytest.raises(ValueError):
        _make_runner(tmp_path, cost_limit_usd=bad_limit)


def test_init_rejects_cost_target_at_or_above_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cost_target_usd must be < cost_limit_usd"):
        _make_runner(
            tmp_path,
            cost_limit_usd=5.0,
            cost_target_usd=5.0,
            max_output_tokens=1000,
            max_steps_per_attempt=2,
        )


def test_pricing_table_contains_truthful_big_pickle_zero_row() -> None:
    assert backgammon_mod._MODEL_PRICING_USD_PER_1M["opencode/big-pickle"] == {
        "input": 0.0,
        "output": 0.0,
    }


@pytest.mark.parametrize(
    "model_selector",
    [
        "opencode/big-pickle",
        "orcarouter/opencode/big-pickle",
    ],
)
def test_zero_price_model_fallback_attempt_estimate_is_zero(
    tmp_path: Path,
    model_selector: str,
) -> None:
    runner = _make_runner(
        tmp_path,
        model=model_selector,
        cost_limit_usd=1.0,
        max_output_tokens=8192,
        max_steps_per_attempt=3,
    )
    assert runner._fallback_attempt_estimate_usd == pytest.approx(0.0)


@pytest.mark.parametrize(
    "unknown_model",
    [
        "orcarouter/not-in-table/model-x",
        "opencode/not-in-table",
    ],
)
def test_init_rejects_missing_pricing_without_override(tmp_path: Path, unknown_model: str) -> None:
    with pytest.raises(ValueError, match="missing authoritative output pricing"):
        _make_runner(
            tmp_path,
            model=unknown_model,
            cost_limit_usd=1.0,
            max_output_tokens=1000,
            max_steps_per_attempt=2,
            output_price_per_1m=None,
        )


def test_estimated_attempt_cost_prefers_observed_then_fallback() -> None:
    assert BackgammonRunner._estimate_full_attempt_cost_usd([], fallback_usd=0.6) == pytest.approx(0.6)
    assert BackgammonRunner._estimate_full_attempt_cost_usd([0.4, 0.7], fallback_usd=0.6) == pytest.approx(0.7)
    assert BackgammonRunner._estimate_full_attempt_cost_usd([0.0, -1.0], fallback_usd=0.3) == pytest.approx(0.3)


def test_budget_decision_allows_when_proxy_remaining_covers_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=2.4)
    checkpoint = tmp_path / "proxy-checkpoint.json"
    _write_checkpoint(checkpoint, hard=2.4, accrued=1.5, committed=0.2)  # remaining=0.7
    monkeypatch.setenv("WEVIBE_BENCH_PROXY_CHECKPOINT", str(checkpoint))

    decision = runner._budget_decision_for_attempt(
        run_label="allow-check",
        attempt=2,
        observed_attempt_costs=[0.6],
    )
    assert decision == "allow"


def test_budget_decision_stops_when_proxy_remaining_below_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=2.4)
    checkpoint = tmp_path / "proxy-checkpoint.json"
    _write_checkpoint(checkpoint, hard=2.4, accrued=1.61, committed=0.2)  # remaining=0.59
    monkeypatch.setenv("WEVIBE_BENCH_PROXY_CHECKPOINT", str(checkpoint))

    decision = runner._budget_decision_for_attempt(
        run_label="stop-check",
        attempt=2,
        observed_attempt_costs=[0.6],
    )
    assert decision == "budget_stop"


def test_budget_decision_requires_checkpoint_env_when_budgeted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=2.4)
    monkeypatch.delenv("WEVIBE_BENCH_PROXY_CHECKPOINT", raising=False)

    decision = runner._budget_decision_for_attempt(
        run_label="missing-env",
        attempt=1,
        observed_attempt_costs=[0.2],
    )
    assert decision == "harness_error"


def test_budget_decision_unbounded_mode_allows_without_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _make_runner(tmp_path, cost_limit_usd=None)
    monkeypatch.delenv("WEVIBE_BENCH_PROXY_CHECKPOINT", raising=False)

    decision = runner._budget_decision_for_attempt(
        run_label="unbounded",
        attempt=1,
        observed_attempt_costs=[],
    )
    assert decision == "allow"


def test_run_config_carries_new_cost_fields() -> None:
    cfg = RunConfig(
        cost_limit_usd=10.5,
        cost_target_usd=9.5,
        max_output_tokens=4096,
        max_steps_per_attempt=4,
        output_price_per_1m=25.0,
        reasoning_effort="high",
    )
    payload = cfg.to_dict()

    assert payload["cost_limit_usd"] == 10.5
    assert payload["cost_target_usd"] == 9.5
    assert payload["max_output_tokens"] == 4096
    assert payload["max_steps_per_attempt"] == 4
    assert payload["output_price_per_1m"] == 25.0
    assert payload["reasoning_effort"] == "high"


def test_opencode_run_stats_has_budget_stop_fields() -> None:
    stats = _OpencodeRunStats(
        input_tokens=11,
        output_tokens=22,
        reasoning_tokens=33,
        turns=4,
        session_id="sess-1",
        killed_reason=None,
        exit_code=0,
        cost_usd=1.25,
    )

    assert stats.cost_usd == pytest.approx(1.25)
    assert stats.budget_stop_detected is False
    assert stats.budget_stop_signature is None


def test_reconcile_derived_vs_billing_ok_case() -> None:
    result = reconcile_derived_vs_billing(
        settled_usd=0.001,
        baseline_cents=1000.0,
        final_cents=1000.1,
    )

    assert result["status"] == "ok"
    assert result["divergence_pct"] == pytest.approx(0.0, abs=1e-9)


def test_reconcile_derived_vs_billing_ok_at_tolerance_boundary() -> None:
    result = reconcile_derived_vs_billing(
        settled_usd=0.001,
        baseline_cents=1000.0,
        final_cents=1000.095,
        tolerance=0.05,
    )

    assert result["status"] == "ok"
    assert result["divergence_pct"] == pytest.approx(5.0)


def test_reconcile_derived_vs_billing_diverged_case() -> None:
    result = reconcile_derived_vs_billing(
        settled_usd=0.001,
        baseline_cents=1000.0,
        final_cents=1000.2,
    )

    assert result["status"] == "diverged"
    assert result["divergence_pct"] == pytest.approx(50.0)


def test_reconcile_derived_vs_billing_skipped_when_baseline_missing() -> None:
    result = reconcile_derived_vs_billing(
        settled_usd=0.001,
        baseline_cents=None,
        final_cents=1000.1,
    )

    assert result["status"] == "skipped"
    assert result["delta_counter_usd"] is None


def test_reconcile_derived_vs_billing_error_when_counter_backwards() -> None:
    result = reconcile_derived_vs_billing(
        settled_usd=0.001,
        baseline_cents=1000.1,
        final_cents=1000.0,
    )

    assert result["status"] == "error"
    assert "backwards" in result["reason"]


@pytest.mark.parametrize(
    "result",
    [
        reconcile_derived_vs_billing(
            settled_usd=0.001,
            baseline_cents=1000.0,
            final_cents=1000.1,
        ),
        reconcile_derived_vs_billing(
            settled_usd=0.001,
            baseline_cents=1000.0,
            final_cents=1000.2,
        ),
        reconcile_derived_vs_billing(
            settled_usd=0.001,
            baseline_cents=None,
            final_cents=1000.1,
        ),
        reconcile_derived_vs_billing(
            settled_usd=0.001,
            baseline_cents=1000.1,
            final_cents=1000.0,
        ),
    ],
)
def test_reconcile_derived_vs_billing_always_sets_workspace_aggregate_confound_note(
    result: dict,
) -> None:
    assert isinstance(result["confound_note"], str)
    assert result["confound_note"]
    assert "workspace-aggregate" in result["confound_note"]
