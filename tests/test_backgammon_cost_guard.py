from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import wevibe_bench.adapters.backgammon as backgammon_mod
from wevibe_bench.adapters.backgammon import BackgammonRunner, _OpencodeRunStats
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
    model: str = "openrouter/anthropic/claude-opus-4.8",
    reasoning_effort: str | None = None,
    cost_limit_usd: float | None = None,
    cost_target_usd: float | None = None,
    max_output_tokens: int | None = None,
    max_steps_per_attempt: int | None = None,
    output_price_per_1m: float | None = None,
    max_attempts: int = 3,
    progress: Any = None,
) -> BackgammonRunner:
    if cost_limit_usd is not None and max_output_tokens is None:
        max_output_tokens = 2000
    if cost_limit_usd is not None and max_steps_per_attempt is None:
        max_steps_per_attempt = 3

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
        progress=progress,
    )


def _patch_fake_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeDockerCellConfig:
        def __init__(
            self,
            *,
            worktree: Path,
            memory_mode: str,
            container_name: str,
            output_token_max: int | None = None,
        ) -> None:
            self.worktree = worktree
            self.memory_mode = memory_mode
            self.container_name = container_name
            self.output_token_max = output_token_max

    class _FakeDockerCell:
        def __init__(self, config: _FakeDockerCellConfig, progress: Any) -> None:
            self.config = config
            self.progress = progress

        def __enter__(self) -> "_FakeDockerCell":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

        def exec_argv(self, inner: list[str]) -> list[str]:
            return ["python", "-m", "fake", *inner]

        def force_kill(self) -> None:
            return None

    monkeypatch.setattr(backgammon_mod, "DockerCellConfig", _FakeDockerCellConfig)
    monkeypatch.setattr(backgammon_mod, "DockerCell", _FakeDockerCell)
    monkeypatch.setattr(backgammon_mod, "docker_available", lambda: (True, "ok"))
    monkeypatch.setattr(backgammon_mod, "image_exists", lambda: True)
    monkeypatch.setattr(
        backgammon_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="No such container",
        ),
    )


def test_write_worker_config_injects_reasoning_effort(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, reasoning_effort="high", max_output_tokens=8192)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    runner._write_worker_permission_config(worktree=worktree)
    config = json.loads((worktree / "opencode.json").read_text(encoding="utf-8"))

    options = config["provider"]["openrouter"]["models"]["anthropic/claude-opus-4.8"]["options"]
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


def test_write_worker_config_no_provider_when_options_unset(tmp_path: Path) -> None:
    runner = _make_runner(tmp_path, reasoning_effort=None, max_output_tokens=None)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    runner._write_worker_permission_config(worktree=worktree)
    config = json.loads((worktree / "opencode.json").read_text(encoding="utf-8"))

    assert "provider" not in config


def test_init_rejects_bad_reasoning_effort(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _make_runner(tmp_path, reasoning_effort="ultra")


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


def test_init_rejects_missing_pricing_without_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing authoritative output pricing"):
        _make_runner(
            tmp_path,
            model="openrouter/not-in-table/model-x",
            cost_limit_usd=1.0,
            max_output_tokens=1000,
            max_steps_per_attempt=2,
            output_price_per_1m=None,
        )


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


def test_opencode_run_stats_has_cost_field() -> None:
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


def test_reservation_predicate_boundaries() -> None:
    assert BackgammonRunner._reservation_would_exceed(1.0, 0.4, 1.5) is False
    assert BackgammonRunner._reservation_would_exceed(1.0, 0.5, 1.5) is False
    assert BackgammonRunner._reservation_would_exceed(1.0, 0.6, 1.5) is True


def test_worst_case_reservation_math() -> None:
    reservation = BackgammonRunner._worst_case_reservation_usd(
        max_steps=3,
        max_output_tokens=2000,
        output_price_per_1m=25.0,
        safety_factor=1.1,
        cache_write_allowance_usd=0.0125,
    )
    assert reservation == pytest.approx(0.1775)


def test_cost_accumulation_from_part_cost_with_one_step_headroom() -> None:
    assert BackgammonRunner._cost_limit_exceeded(0.0, 0.0, 0.0, None) is False
    assert BackgammonRunner._cost_limit_exceeded(1.0, 0.4, 0.5, 2.0) is False
    assert BackgammonRunner._cost_limit_exceeded(1.0, 0.5, 0.5, 2.0) is False
    assert BackgammonRunner._cost_limit_exceeded(1.0, 0.6, 0.5, 2.0) is True


def test_initial_reservation_refusal_skips_opencode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(
        tmp_path,
        cost_limit_usd=0.20,
        max_output_tokens=10000,
        max_steps_per_attempt=3,
        progress=progress_lines.append,
    )
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: "prompt")

    called = {"count": 0}

    def _unexpected_opencode(**kwargs: Any) -> _OpencodeRunStats:
        called["count"] += 1
        raise AssertionError("_run_opencode must not run when reservation is refused")

    monkeypatch.setattr(runner, "_run_opencode", _unexpected_opencode)

    result = runner._run_cell_impl(
        run_label="reservation-refused",
        run_dir=tmp_path / "reservation-refused",
        task_id="backgammon",
        injected_memory=[],
    )

    assert called["count"] == 0
    assert result.verdict == "BUDGET_STOP"
    assert result.wall_cost_usd == pytest.approx(0.0)
    assert any("reason=cost_reservation_refused" in line for line in progress_lines)


def test_partial_usage_is_counted_in_next_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    progress_lines: list[str] = []
    runner = _make_runner(
        tmp_path,
        cost_limit_usd=4.50,
        max_output_tokens=10000,
        max_steps_per_attempt=3,
        max_attempts=2,
        progress=progress_lines.append,
    )
    _patch_fake_docker(monkeypatch)
    monkeypatch.setattr(runner, "_build_task_prompt", lambda *, injected_memory: "prompt")

    call_count = {"count": 0}

    def _fake_opencode(**kwargs: Any) -> _OpencodeRunStats:
        call_count["count"] += 1
        return _OpencodeRunStats(
            input_tokens=100,
            output_tokens=200,
            reasoning_tokens=50,
            turns=1,
            session_id="sess-1",
            killed_reason=None,
            exit_code=0,
            cost_usd=4.0,
        )

    monkeypatch.setattr(runner, "_run_opencode", _fake_opencode)
    monkeypatch.setattr(
        runner,
        "_run_gate_report",
        lambda **kwargs: {
            "verdict": "FAIL",
            "conformed": True,
            "problems": [{"check": "gate-check"}],
            "failed_gates": ["gate-check"],
        },
    )

    result = runner._run_cell_impl(
        run_label="partial-counted",
        run_dir=tmp_path / "partial-counted",
        task_id="backgammon",
        injected_memory=[],
    )

    assert call_count["count"] == 1
    assert result.wall_cost_usd == pytest.approx(4.0)
    assert result.verdict == "BUDGET_STOP"
    assert any(
        "reason=cost_reservation_refused" in line and "accrued_cost_usd=4.0000" in line
        for line in progress_lines
    )
