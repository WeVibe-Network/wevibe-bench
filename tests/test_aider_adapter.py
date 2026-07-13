from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from wevibe_bench.adapters.aider_polyglot import (
    AiderPolyglotRunner,
    MockExecutor,
    PolyglotRepoNotFound,
    _parse_aider_usage,
    _parse_cost,
    _parse_token_count,
)
from wevibe_bench.backends.base import RecalledMemory
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import RunConfig
from wevibe_bench.runner import run_ablation


def _fixture_polyglot_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "polyglot"


def _cfg() -> RunConfig:
    return RunConfig(
        model_ladder=("model-a",),
        rng_seed=20260708,
        mcp_recall_url="http://offline.local",
        session_token_path="/tmp/__wevibe_bench_missing_token__",
    )


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def test_loader_enumerates_fixture_exercises_and_missing_root_raises() -> None:
    runner = AiderPolyglotRunner(
        polyglot_dir=_fixture_polyglot_dir(),
        mock_mode=True,
        executor=MockExecutor({}),
    )

    assert runner.task_ids() == ["go/hello-world", "python/two-fer"]

    missing_runner = AiderPolyglotRunner(
        polyglot_dir=_fixture_polyglot_dir() / "__missing__",
        mock_mode=True,
        executor=MockExecutor({}),
    )
    with pytest.raises(PolyglotRepoNotFound):
        missing_runner.task_ids()


def test_build_need_card_keeps_instructions_in_task_and_language_in_keyword_channel() -> None:
    runner = AiderPolyglotRunner(
        polyglot_dir=_fixture_polyglot_dir(),
        mock_mode=True,
        executor=MockExecutor({}),
    )

    card = runner.build_need_card("go/hello-world")
    exercise = runner.load_exercises()["go/hello-world"]
    collapsed_task = _collapse_whitespace(exercise.instructions)

    assert card.intent == "implement"
    assert _collapse_whitespace(card.task) == collapsed_task
    assert card.prompt_digest == f"implement. {collapsed_task}"

    assert card.language == "go"
    assert card.stack == ["go"]
    assert card.files == ["hello_world.go", "hello_world_test.go"]
    assert card.project_name == "hello-world"
    assert card.query == card.task


def test_parse_helpers_handle_suffixes_commas_and_last_usage_line() -> None:
    assert _parse_token_count("1.2k") == 1200
    assert _parse_token_count("2,860") == 2860
    assert _parse_token_count("1.2M") == 1_200_000
    assert _parse_cost("0.0123") == 0.0123

    usage = _parse_aider_usage(
        "\n".join(
            [
                "Tokens: 10 sent, 10 received. Cost: $0.001 message, $0.001 session.",
                "Tokens: 2k sent, 1.5k received. Cost: $0.020 message, $0.030 session.",
            ]
        )
    )
    assert usage == (2000, 1500, 0.03)


def test_run_task_mock_single_attempt_pass_parses_tokens_cost_and_no_memory_read_arg() -> None:
    executor = MockExecutor(
        {
            "python/two-fer": {
                "aider_stdout": [
                    "Tokens: 1.2k sent, 800 received. Cost: $0.0123 message, $0.0123 session."
                ],
                "test_returncodes": [0],
            }
        }
    )
    runner = AiderPolyglotRunner(
        polyglot_dir=_fixture_polyglot_dir(),
        mock_mode=True,
        executor=executor,
    )

    outcome = runner.run_task("model-a", "python/two-fer", injected_memory=[])

    assert outcome.resolved is True
    assert outcome.input_tokens == 1200
    assert outcome.output_tokens == 800
    assert outcome.input_tokens + outcome.output_tokens == 2000
    assert outcome.wall_cost_usd == 0.0123
    assert outcome.turns == 1

    aider_calls = [call for call in executor.calls if call.cmd and call.cmd[0] == "aider"]
    assert len(aider_calls) == 1
    assert "--read" not in aider_calls[0].cmd


def test_run_task_mock_retry_sums_totals_and_uses_memory_read_arg_when_on() -> None:
    executor = MockExecutor(
        {
            "python/two-fer": {
                "aider_stdout": [
                    "Tokens: 500 sent, 400 received. Cost: $0.0100 message, $0.0100 session.",
                    "Tokens: 700 sent, 600 received. Cost: $0.0200 message, $0.0200 session.",
                ],
                "test_returncodes": [1, 0],
                "test_stdout": ["FAILED test_two_fer", ""],
                "test_stderr": ["AssertionError: mismatch", ""],
            }
        }
    )
    runner = AiderPolyglotRunner(
        polyglot_dir=_fixture_polyglot_dir(),
        mock_mode=True,
        executor=executor,
    )

    memories = [
        RecalledMemory(
            cid="cid-two-fer",
            score=0.9,
            vector_score=0.6,
            combined_score=0.9,
            keyword_score=0.3,
            matched_keywords=["two-fer"],
            text="Remember default should be you when name is missing.",
        )
    ]

    outcome = runner.run_task("model-a", "python/two-fer", injected_memory=memories)

    assert outcome.resolved is True
    assert outcome.turns == 2
    assert outcome.input_tokens == 1200
    assert outcome.output_tokens == 1000
    assert outcome.wall_cost_usd == 0.03

    aider_calls = [call for call in executor.calls if call.cmd and call.cmd[0] == "aider"]
    assert len(aider_calls) == 2
    assert all("--read" in call.cmd for call in aider_calls)


def test_run_ablation_off_on_round_trip_with_real_runner_and_mock_executor() -> None:
    executor = MockExecutor(
        {
            "go/hello-world": {
                "aider_stdout": [
                    "Tokens: 900 sent, 100 received. Cost: $0.0090 message, $0.0090 session.",
                    "Tokens: 1.0k sent, 120 received. Cost: $0.0100 message, $0.0100 session.",
                ],
                "test_returncodes": [0, 0],
            },
            "python/two-fer": {
                "aider_stdout": [
                    "Tokens: 1,200 sent, 300 received. Cost: $0.0150 message, $0.0150 session.",
                    "Tokens: 1.3k sent, 250 received. Cost: $0.0160 message, $0.0160 session.",
                ],
                "test_returncodes": [0, 0],
            },
        }
    )
    runner = AiderPolyglotRunner(
        polyglot_dir=_fixture_polyglot_dir(),
        mock_mode=True,
        executor=executor,
    )
    cfg = _cfg()

    def transport(url: str, headers: dict[str, str], body: dict[str, object]):
        _ = (url, headers)
        task_id = str(body["query"])
        return (
            200,
            {
                "status": "ok",
                "memories": [
                    {
                        "cid": f"cid-{task_id}",
                        "score": 0.95,
                        "breakdown": {
                            "vector_score": 0.6,
                            "combined_score": 0.95,
                            "keyword_score": 0.35,
                        },
                        "matched_keywords": [task_id],
                        "text": f"memory for {task_id}",
                    }
                ],
            },
            True,
        )

    scorecard = run_ablation(
        cfg,
        tasks=runner.task_ids(),
        agent=runner,
        split_disclosure={"fixture": True},
        on_backend=WeVibeBackend(cfg, transport=transport),
    )

    payload = json.loads(scorecard.to_json())
    assert payload == json.loads(json.dumps(payload, sort_keys=True))
    assert payload["manifest"]["split_disclosure"] == {"fixture": True}

    cells = payload["cells"]
    assert len(cells) == len(runner.task_ids()) * 2 * len(cfg.model_ladder)
    assert {cell["condition"] for cell in cells} == {"OFF", "ON"}
    for task_id in runner.task_ids():
        assert {cell["condition"] for cell in cells if cell["task_id"] == task_id} == {"OFF", "ON"}

    aider_calls = [call for call in executor.calls if call.cmd and call.cmd[0] == "aider"]
    assert len(aider_calls) == len(runner.task_ids()) * 2
    assert sum(1 for call in aider_calls if "--read" in call.cmd) == len(runner.task_ids())
