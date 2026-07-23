from __future__ import annotations

import json

from wevibe_bench.backends.base import NeedCard
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import BenchmarkSchedule, BenchmarkWave, RunConfig
from wevibe_bench.runner import MockAgentRunner, run_ablation


def _cfg(seed: int) -> RunConfig:
    return RunConfig(
        schedule=BenchmarkSchedule(waves=(BenchmarkWave(wave_id="multi", models=("model-a", "model-b"),),),),
        rng_seed=seed,
        mcp_recall_url="http://offline.local",
        session_token_path="/tmp/__wevibe_bench_missing_token__",
    )


def _agent() -> MockAgentRunner:
    return MockAgentRunner(
        need_card_builder=lambda task_id: NeedCard(
            intent="implement",
            task=f"solve {task_id}",
            query=task_id,
            stack=["python"],
        )
    )


def _fresh_backend(cfg: RunConfig) -> WeVibeBackend:
    def transport(url: str, headers: dict[str, str], body: dict[str, object]):
        _ = (url, headers)
        task = str(body["query"])
        return (
            200,
            {
                "status": "ok",
                "memories": [
                    {
                        "cid": f"cid-{task}",
                        "score": 0.9,
                        "breakdown": {
                            "vector_score": 0.6,
                            "combined_score": 0.9,
                            "keyword_score": 0.3,
                        },
                        "matched_keywords": [task],
                        "text": f"memory for {task}",
                    }
                ],
            },
            True,
        )

    return WeVibeBackend(cfg, transport=transport)


def _normalized_scorecard(scorecard_json: str) -> dict:
    payload = json.loads(scorecard_json)
    payload["manifest"].pop("created_at", None)
    return payload


def test_same_rng_seed_produces_identical_scorecard_minus_timestamp() -> None:
    cfg = _cfg(seed=777)
    tasks = ["task-1", "task-2", "task-3"]
    agent = _agent()

    first = run_ablation(
        cfg,
        tasks=tasks,
        agent=agent,
        split_disclosure=None,
        on_backend=_fresh_backend(cfg),
    )
    second = run_ablation(
        cfg,
        tasks=tasks,
        agent=agent,
        split_disclosure=None,
        on_backend=_fresh_backend(cfg),
    )

    assert _normalized_scorecard(first.to_json()) == _normalized_scorecard(second.to_json())


def test_each_distinct_seed_is_still_deterministic_when_repeated() -> None:
    tasks = ["task-a", "task-b"]
    agent = _agent()

    def run_once(seed: int) -> dict:
        cfg = _cfg(seed=seed)
        scorecard = run_ablation(
            cfg,
            tasks=tasks,
            agent=agent,
            split_disclosure=None,
            on_backend=_fresh_backend(cfg),
        )
        return _normalized_scorecard(scorecard.to_json())

    seed_111_a = run_once(111)
    seed_111_b = run_once(111)
    seed_222_a = run_once(222)
    seed_222_b = run_once(222)

    assert seed_111_a == seed_111_b
    assert seed_222_a == seed_222_b
