from __future__ import annotations

from wevibe_bench.backends.base import NeedCard
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import RunConfig
from wevibe_bench.runner import MockAgentRunner, TaskOutcome, run_ablation


def _cfg(*, seed: int = 1234) -> RunConfig:
    return RunConfig(
        model_ladder=("model-x",),
        rng_seed=seed,
        mcp_recall_url="http://offline.local",
        session_token_path="/tmp/__wevibe_bench_missing_token__",
    )


def _agent() -> MockAgentRunner:
    return MockAgentRunner(
        need_card_builder=lambda task_id: NeedCard(
            intent="offline bench",
            task=f"solve {task_id}",
            query=task_id,
            stack=["python"],
            deps=["pytest"],
            error_strings=["E_TOKEN"],
            files=["task.py"],
            directory="/repo",
            project_name="bench-proj",
        )
    )


def test_run_ablation_marks_non_yes_on_cells_not_scored_and_excludes_them_from_model_diffs() -> None:
    cfg = _cfg(seed=100)

    def transport(url: str, headers: dict[str, str], body: dict[str, object]):
        _ = (url, headers)
        query = body["query"]
        if query == "yes-task":
            return (
                200,
                {
                    "status": "ok",
                    "memories": [
                        {
                            "cid": "cid-yes",
                            "score": 0.9,
                            "breakdown": {
                                "vector_score": 0.5,
                                "combined_score": 0.9,
                                "keyword_score": 0.4,
                            },
                            "matched_keywords": ["solve"],
                            "text": "delivered plaintext",
                        }
                    ],
                },
                True,
            )
        if query == "offline-task":
            return (0, {}, False)
        if query == "decrypt-task":
            return (
                200,
                {"status": "ok", "reason_code": "decrypt_failed", "memories": []},
                True,
            )
        raise AssertionError(f"unexpected query: {query}")

    scorecard = run_ablation(
        cfg,
        tasks=["yes-task", "offline-task", "decrypt-task"],
        agent=_agent(),
        split_disclosure=None,
        on_backend=WeVibeBackend(cfg, transport=transport),
    )

    on_cells = [cell for cell in scorecard.cells if cell.condition == "ON"]
    on_by_task = {cell.task_id: cell for cell in on_cells}

    assert on_by_task["yes-task"].scored is True
    assert on_by_task["yes-task"].delivery == "YES"

    assert on_by_task["offline-task"].scored is False
    assert on_by_task["offline-task"].not_scored_reason == "delivery=NO"
    assert on_by_task["offline-task"].delivery == "NO"

    assert on_by_task["decrypt-task"].scored is False
    assert on_by_task["decrypt-task"].not_scored_reason == "delivery=CALLED"
    assert on_by_task["decrypt-task"].delivery == "CALLED"

    diff = scorecard.model_diffs()[0]
    assert diff.on_scored_n == 1
    assert diff.on_not_scored_n == 2
    assert {cell.delivery for cell in on_cells if cell.scored} == {"YES"}
    assert {cell.delivery for cell in on_cells if not cell.scored} == {"NO", "CALLED"}
    assert diff.on_total_tokens == sum(cell.total_tokens for cell in on_cells if cell.scored)


def test_not_scored_on_cell_never_counts_as_hit_or_tokens_even_if_resolved_and_huge() -> None:
    cfg = _cfg(seed=101)

    def transport(url: str, headers: dict[str, str], body: dict[str, object]):
        _ = (url, headers)
        if body["query"] == "scoreable":
            return (
                200,
                {
                    "status": "ok",
                    "memories": [{"cid": "cid", "text": "hit", "breakdown": {}}],
                },
                True,
            )
        if body["query"] == "blocked":
            return (0, {}, False)
        raise AssertionError("unexpected task")

    def outcome_builder(model: str, task_id: str, memory_count: int) -> TaskOutcome:
        _ = model
        if task_id == "scoreable":
            # This is the ONLY scored ON cell and is unresolved.
            if memory_count > 0:
                return TaskOutcome(
                    resolved=False,
                    input_tokens=10,
                    output_tokens=10,
                    turns=1,
                    wall_cost_usd=1.0,
                    wall_seconds=1.0,
                )
            return TaskOutcome(
                resolved=False,
                input_tokens=11,
                output_tokens=11,
                turns=1,
                wall_cost_usd=1.1,
                wall_seconds=1.1,
            )

        # This task's ON cell is not_scored (delivery=NO), but looks like a giant "win".
        return TaskOutcome(
            resolved=True,
            input_tokens=50_000,
            output_tokens=50_000,
            turns=99,
            wall_cost_usd=99.0,
            wall_seconds=99.0,
        )

    agent = MockAgentRunner(need_card_builder=_agent().build_need_card, outcome_builder=outcome_builder)
    scorecard = run_ablation(
        cfg,
        tasks=["scoreable", "blocked"],
        agent=agent,
        split_disclosure=None,
        on_backend=WeVibeBackend(cfg, transport=transport),
    )

    on_cells = [cell for cell in scorecard.cells if cell.condition == "ON"]
    blocked_on = next(cell for cell in on_cells if cell.task_id == "blocked")
    scoreable_on = next(cell for cell in on_cells if cell.task_id == "scoreable")
    diff = scorecard.model_diffs()[0]

    assert blocked_on.scored is False
    assert blocked_on.resolved is True
    assert blocked_on.total_tokens == 100_000

    assert scoreable_on.scored is True
    assert scoreable_on.resolved is False

    # BENCHMARK INTEGRITY: unresolved scored ON means pass-rate 0, regardless of blocked_on.
    assert diff.on_pass_rate == 0.0
    assert diff.on_total_tokens == scoreable_on.total_tokens


def test_need_card_inv6_prompt_digest_and_wire_shape() -> None:
    cfg = _cfg(seed=102)
    need = NeedCard(
        intent="INTENT_TOKEN",
        task="TASK_TOKEN",
        language="python",
        stack=["STACK_TOKEN"],
        frameworks=["FW_TOKEN"],
        deps=["DEPS_TOKEN"],
        error_strings=["ERRORS_TOKEN"],
        files=["FILES_TOKEN"],
        directory="DIRECTORY_TOKEN",
        project_name="PROJECT_TOKEN",
        query="QUERY_TOKEN",
    )

    digest = need.prompt_digest
    assert digest == "INTENT_TOKEN. TASK_TOKEN"
    assert "STACK_TOKEN" not in digest
    assert "DEPS_TOKEN" not in digest
    assert "ERRORS_TOKEN" not in digest
    assert "FILES_TOKEN" not in digest
    assert "DIRECTORY_TOKEN" not in digest
    assert "PROJECT_TOKEN" not in digest

    wire = need.to_wire(cfg, session_id="session-1")
    assert "prompt_digest" not in wire
    assert wire["query"] == "QUERY_TOKEN"
    assert wire["intent"] == "INTENT_TOKEN"
    assert wire["task"] == "TASK_TOKEN"
    assert wire["stack"] == ["STACK_TOKEN"]
    assert wire["frameworks"] == ["FW_TOKEN"]
    assert wire["deps"] == ["DEPS_TOKEN"]
    assert wire["errorStrings"] == ["ERRORS_TOKEN"]
    assert wire["files"] == ["FILES_TOKEN"]
    assert wire["directory"] == "DIRECTORY_TOKEN"
    assert wire["projectName"] == "PROJECT_TOKEN"
