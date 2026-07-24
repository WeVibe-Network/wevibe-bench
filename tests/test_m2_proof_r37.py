from __future__ import annotations

import io
import json
import logging
import subprocess
from typing import Any, Callable

from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import LifecycleConfig
from wevibe_bench.lifecycle.logging_util import fp
from wevibe_bench.lifecycle.m2_proof import M2Proof


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    return logger, stream


def test_m2_proof_run_result_and_m2_result_json_are_content_free() -> None:
    logger, stream = _capture_logger("test.lifecycle.m2.r37.content-free")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("33" * 32)
    contributor = Identity.from_hex("44" * 32)

    plaintext_sentinel = "PLAINTEXT_SENTINEL_XYZZY"
    ciphertext_sentinel = "CIPHERTEXT_SENTINEL_ABCDE"
    memory_text = f"memory body {plaintext_sentinel} for R-37 coverage"
    events_payload = [{"kind": "user", "time": 1700000000000, "seq": 0, "text": "task prompt"}]

    class FakeContributorRest:
        def extract(
            self,
            events: list[dict[str, Any]],
            model: str,
            project_context: dict[str, Any] | None = None,
            org_id: str | None = None,
            **_kwargs: Any,
        ) -> str:
            assert events == events_payload
            assert model == "model-a"
            assert project_context == {"project": "ctx", "api_key_present": True}
            assert org_id == "org-77"
            return "job-1"

        def wait_extract(
            self,
            job_id: str,
            timeout_s: float = 30,
            interval_s: float = 0.5,
        ) -> dict[str, Any]:
            assert job_id == "job-1"
            return {
                "status": "completed",
                "result": {
                    "memories": [
                        {
                            "text": memory_text,
                            "keywords": ["alpha"],
                            "stack_hint": "python",
                        }
                    ]
                },
            }

        def submit(
            self,
            org_id: str,
            plaintext: str,
            memory_type: str = "memory",
            epoch_id: int | None = None,
            stack_hint: str | None = None,
            keywords: list[str] | None = None,
            mc_version: int = 1,
        ) -> dict[str, Any]:
            assert org_id == "org-77"
            assert plaintext_sentinel in plaintext
            assert stack_hint == ["python"]
            assert keywords == ["alpha"]
            assert memory_type == "memory"
            assert epoch_id is None
            assert mc_version == 1
            return {"status": "ok", "submission_hash": "sub-1"}

    class FakeLeaderRest:
        def mod_embed_retrieval_card(self, items: list[dict[str, Any]], org_id: str | None = None) -> list[dict[str, Any]]:
            assert org_id == "org-77"
            assert items[0]["id"] == "sub-1"
            assert items[0]["ciphertext_hex"] == ciphertext_sentinel
            return [
                {
                    "id": "sub-1",
                    "vector": [0.1, 0.2],
                    "embedding_model_id": "nomic-embed-text:v1.5",
                    "embedding_schema_version": 1,
                    "umbral_capsule": "cap-1",
                    "umbral_ciphertext": ciphertext_sentinel,
                }
            ]

        def recall(self, query: str, org_id: str, **_kw: Any) -> dict[str, Any]:
            assert org_id == "org-77"
            return {
                "status": "ok",
                "memories": [
                    {"text": f"...{query}..."},
                ],
            }

    class FakeHubClient:
        def __init__(self) -> None:
            self._commit_status_calls = 0

        def moderation_queue(self, identity: Identity, org_id: str) -> Any:
            assert identity is leader
            assert org_id == "org-77"
            return [
                {
                    "submission_hash": "sub-1",
                    "ciphertext_hex": ciphertext_sentinel,
                    "wrapped_dek_mod": ciphertext_sentinel,
                    "epoch_id": 0,
                    "stack_hint": "python",
                }
            ]

        def submit_keyword_results(
            self,
            identity: Identity,
            org_id: str,
            submission_hash: str,
            classified: list[dict[str, Any]],
        ) -> Any:
            assert identity is leader
            assert org_id == "org-77"
            assert submission_hash == "sub-1"
            assert classified
            return {
                "verified": 1,
                "results": [
                    {
                        "submission_hash": "sub-1",
                        "passed": True,
                        "code": "ok",
                        "error": "",
                    }
                ],
            }

        def verify_keywords(self, identity: Identity, org_id: str, entries: list[dict[str, Any]]) -> Any:
            assert identity is leader
            assert org_id == "org-77"
            assert entries[0]["submission_hash"] == "sub-1"
            return {
                "verified": 1,
                "results": [
                    {
                        "submission_hash": "sub-1",
                        "passed": True,
                        "code": "ok",
                        "error": "",
                    }
                ],
            }

        def batch_submit(self, identity: Identity, org_id: str) -> Any:
            assert identity is leader
            assert org_id == "org-77"
            return {
                "batch": [
                    {
                        "submission_hash": "sub-1",
                        "vector": [0.1, 0.2],
                    }
                ],
                "verification": {"status": "ok"},
            }

        def commit_status(self, identity: Identity, org_id: str) -> Any:
            assert identity is leader
            assert org_id == "org-77"
            self._commit_status_calls += 1
            if self._commit_status_calls == 1:
                return {
                    "submissions": [
                        {
                            "submission_hash": "sub-1",
                            "status": "pending",
                            "commit_error": "",
                        }
                    ]
                }
            return {
                "submissions": [
                    {
                        "submission_hash": "sub-1",
                        "status": "committed",
                        "commit_error": "",
                    }
                ]
            }

    contributor_rest = FakeContributorRest()
    leader_rest = FakeLeaderRest()

    def mcp_rest_factory(base_url: str):
        if base_url.endswith(":4551"):
            return contributor_rest
        if base_url.endswith(":4550"):
            return leader_rest
        raise AssertionError(f"unexpected base_url {base_url}")

    snapshots = [
        {"org-77-memory": 3, "other": 10},
        {"org-77-memory": 4, "other": 10},
    ]

    def snapshot_fn(_url: str) -> dict[str, int]:
        return snapshots.pop(0)

    class FakeOrchestrator:
        org_id = "org-77"

    def run_cmd(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"tx_hash":"tx-abc","code":0,"msg_count":2}\n',
            stderr="",
        )

    proof = M2Proof(
        cfg=cfg,
        orchestrator=FakeOrchestrator(),
        leader=leader,
        contributor=contributor,
        logger=logger,
        mcp_rest_factory=mcp_rest_factory,
        hub_client=FakeHubClient(),
        snapshot_fn=snapshot_fn,
        find_collection_fn=lambda _url, _org_id: "org-77-memory",
        sleep_fn=lambda _seconds: None,
        run_cmd=run_cmd,
    )

    result = proof.run(
        events=events_payload,
        model="model-a",
        api_key="api-key",
        project_context={"project": "ctx"},
    )

    serialized_result = json.dumps(result, sort_keys=True)
    logs = stream.getvalue()

    assert plaintext_sentinel not in serialized_result
    assert ciphertext_sentinel not in serialized_result
    assert plaintext_sentinel not in logs
    assert ciphertext_sentinel not in logs

    assert result["memory"]["memory_fp"]
    assert result["memory"]["text_size"] == len(memory_text.encode("utf-8"))
    assert result["memory"]["keyword_count"] == 1
    assert result["submission_hash"] == "sub-1"
    assert result["delivery"]["delivery"] == "YES"

    m2_result_lines = [line for line in logs.splitlines() if "M2_RESULT_JSON " in line]
    assert len(m2_result_lines) == 1
    logged_payload = json.loads(m2_result_lines[0].split("M2_RESULT_JSON ", 1)[1])
    assert logged_payload == result
    assert "memory_fp" in m2_result_lines[0]


def test_m2_proof_direct_memory_log_uses_memory_fp_and_not_text_prefix() -> None:
    logger, stream = _capture_logger("test.lifecycle.m2.r37.direct-memory")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("55" * 32)
    contributor = Identity.from_hex("66" * 32)

    plaintext_sentinel = "PLAINTEXT_SENTINEL_XYZZY"
    mcp_factory_called = False

    class ExtractShouldNotRun:
        def extract(
            self,
            events: list[dict[str, Any]],
            model: str,
            project_context: dict[str, Any] | None = None,
            org_id: str | None = None,
            **_kwargs: Any,
        ) -> str:
            raise AssertionError("extract must not run when direct memory is provided")

    def mcp_rest_factory(_base_url: str) -> ExtractShouldNotRun:
        nonlocal mcp_factory_called
        mcp_factory_called = True
        return ExtractShouldNotRun()

    class FakeOrchestrator:
        org_id = "org-99"

    proof = M2Proof(
        cfg=cfg,
        orchestrator=FakeOrchestrator(),
        leader=leader,
        contributor=contributor,
        logger=logger,
        mcp_rest_factory=mcp_rest_factory,
        hub_client=object(),
        direct_memory={"text": plaintext_sentinel, "keywords": ["a", "b"], "stack_hint": "python"},
    )

    memory = proof.produce_memory(
        events=[{"kind": "user", "time": 1, "seq": 0, "text": "ignored"}],
        model="ignored",
        api_key="",
        project_context={"project": "ctx"},
        org_id="org-99",
    )

    assert mcp_factory_called is False
    assert memory["text"] == plaintext_sentinel

    logs = stream.getvalue()
    assert "op=lifecycle.m2.direct_memory" in logs
    assert "memory_fp=" in logs
    assert "text_prefix=" not in logs
    assert plaintext_sentinel not in logs


def test_m2_proof_produce_memories_keeps_atomic_candidates_separate() -> None:
    logger, _ = _capture_logger("test.lifecycle.m2.atomic-candidates")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("11" * 32)
    contributor = Identity.from_hex("22" * 32)

    events_payload = [{"kind": "user", "time": 1_700_000_000_000, "seq": 0, "text": "task prompt"}]

    class FakeContributorRest:
        def extract(
            self,
            events: list[dict[str, Any]],
            model: str,
            project_context: dict[str, Any] | None = None,
            org_id: str | None = None,
            **_kwargs: Any,
        ) -> str:
            assert events == events_payload
            assert model == "model-a"
            assert project_context == {"project": "ctx", "api_key_present": True}
            assert org_id == "org-1"
            return "job-atomic"

        def wait_extract(
            self,
            job_id: str,
            timeout_s: float = 30,
            interval_s: float = 0.5,
        ) -> dict[str, Any]:
            assert job_id == "job-atomic"
            return {
                "status": "completed",
                "result": {
                    "memories": [
                        {
                            "implement": "Add a retry budget guard before replaying moves.",
                            "context": "Node 22 monorepo runner",
                            "dnd": "Do not retry unbounded loops.",
                            "stack": ["typescript", "node"],
                            "memory_type": "memory",
                            "keywords": {
                                "classified": [{"keyword": "retry_budget"}],
                                "suggestions": [{"keyword": "loop_guard"}],
                            },
                        },
                        {
                            "text": "already-rendered memory text",
                            "stack": ["python"],
                            "memory_type": "memory",
                            "keywords": {
                                "classified": [{"keyword": "rendered_text"}],
                                "suggestions": [{"keyword": "python_stack"}],
                            },
                        },
                    ]
                },
            }

    class FakeOrchestrator:
        org_id = "org-1"

    proof = M2Proof(
        cfg=cfg,
        orchestrator=FakeOrchestrator(),
        leader=leader,
        contributor=contributor,
        logger=logger,
        mcp_rest_factory=lambda _base_url: FakeContributorRest(),
        hub_client=object(),
    )

    memories = proof.produce_memories(
        events=events_payload,
        model="model-a",
        api_key="api-key",
        project_context={"project": "ctx"},
        org_id="org-1",
    )

    assert len(memories) == 2
    assert memories[0] == {
        "text": (
            "Implement: Add a retry budget guard before replaying moves.\n"
            "Context: Node 22 monorepo runner\n"
            "Stack: typescript, node\n"
            "Avoid: Do not retry unbounded loops."
        ),
        "keywords": ["retry_budget", "loop_guard"],
        "stack_hint": ["typescript", "node"],
        "memory_type": "memory",
    }
    assert memories[1] == {
        "text": "already-rendered memory text",
        "keywords": ["rendered_text", "python_stack"],
        "stack_hint": ["python"],
        "memory_type": "memory",
    }


def _build_delivery_proof(
    *,
    logger_name: str,
    recall_fn: Callable[[str], dict[str, Any]],
) -> M2Proof:
    logger, _ = _capture_logger(logger_name)
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("77" * 32)
    contributor = Identity.from_hex("88" * 32)

    class FakeLeaderRest:
        def recall(self, query: str, org_id: str, **_kw: Any) -> dict[str, Any]:
            assert org_id == "org-9"
            return recall_fn(query)

    class FakeOrchestrator:
        org_id = "org-9"

    def mcp_rest_factory(base_url: str):
        if base_url.endswith(":4550"):
            return FakeLeaderRest()
        return object()

    return M2Proof(
        cfg=cfg,
        orchestrator=FakeOrchestrator(),
        leader=leader,
        contributor=contributor,
        logger=logger,
        mcp_rest_factory=mcp_rest_factory,
        hub_client=object(),
    )


def test_m2_proof_prove_delivery_twin_of_returned_counts_as_delivered() -> None:
    seen_queries: list[str] = []

    def recall_fn(query: str) -> dict[str, Any]:
        seen_queries.append(query)
        if query == "fragment-one":
            return {
                "status": "ok",
                "memories": [{"cid": "cid-a", "text": "...fragment-one..."}],
            }
        if query == "fragment-two":
            return {
                "status": "ok",
                "memories": [{"cid": "cid-winner", "text": "winner memory text"}],
                "suppression": {
                    "contested": True,
                    "winner_cid": "cid-winner",
                    "dropped_twin_cid": "cid-b",
                    "score_gap": 0.0007,
                },
            }
        raise AssertionError(f"unexpected query {query!r}")

    proof = _build_delivery_proof(
        logger_name="test.lifecycle.m2.delivery.twin-of-returned",
        recall_fn=recall_fn,
    )
    delivery = proof.prove_delivery(
        "org-9",
        [
            {"fragment": "fragment-one", "cid": "cid-a"},
            {"fragment": "fragment-two", "cid": "cid-b"},
        ],
    )

    assert seen_queries == ["fragment-one", "fragment-two"]
    assert delivery["delivery"] == "YES"
    assert delivery["matched"] is False
    assert delivery["any_matched"] is True
    assert delivery["n_memories"] == 2

    per_memory = delivery["per_memory"]
    assert isinstance(per_memory, list)
    assert per_memory[0]["delivery_mode"] == "matched"
    assert per_memory[1]["delivery_mode"] == "twin_of_returned"
    assert per_memory[1]["matched"] is False
    assert per_memory[1]["delivered"] is True
    assert per_memory[1]["cid"] == fp("cid-b")
    suppression = per_memory[1]["suppression"]
    assert suppression["winner_cid"] == fp("cid-winner")
    assert suppression["dropped_twin_cid"] == fp("cid-b")
    assert suppression["score_gap"] == 0.0007


def test_m2_proof_prove_delivery_suppressed_winner_absent_is_not_delivered() -> None:
    def recall_fn(query: str) -> dict[str, Any]:
        if query == "fragment-two":
            return {
                "status": "ok",
                "memories": [{"cid": "cid-other", "text": "winner memory text"}],
                "suppression": {
                    "contested": True,
                    "winner_cid": "cid-winner",
                    "dropped_twin_cid": "cid-b",
                    "score_gap": 0.0009,
                },
            }
        raise AssertionError(f"unexpected query {query!r}")

    proof = _build_delivery_proof(
        logger_name="test.lifecycle.m2.delivery.suppressed-winner-absent",
        recall_fn=recall_fn,
    )
    delivery = proof.prove_delivery(
        "org-9",
        [{"fragment": "fragment-two", "cid": "cid-b"}],
    )

    assert delivery["delivery"] == "NO"
    assert delivery["matched"] is False
    assert delivery["any_matched"] is False
    per_memory = delivery["per_memory"]
    assert isinstance(per_memory, list)
    assert len(per_memory) == 1
    assert per_memory[0]["delivery_mode"] == "suppressed_winner_absent"
    assert per_memory[0]["matched"] is False
    assert per_memory[0]["delivered"] is False
    suppression = per_memory[0]["suppression"]
    assert suppression["winner_cid"] == fp("cid-winner")
    assert suppression["dropped_twin_cid"] == fp("cid-b")
    assert suppression["score_gap"] == 0.0009


def test_m2_proof_prove_delivery_no_suppression_keeps_legacy_unmatched_behavior() -> None:
    seen_queries: list[str] = []

    def recall_fn(query: str) -> dict[str, Any]:
        seen_queries.append(query)
        if query == "fragment-one":
            return {"status": "ok", "memories": [{"text": "...fragment-one..."}]}
        if query == "fragment-two":
            return {"status": "ok", "memories": [{"text": "different payload"}]}
        raise AssertionError(f"unexpected query {query!r}")

    proof = _build_delivery_proof(
        logger_name="test.lifecycle.m2.delivery.legacy",
        recall_fn=recall_fn,
    )
    delivery = proof.prove_delivery("org-9", ["fragment-one", "fragment-two"])

    assert seen_queries == ["fragment-one", "fragment-two"]
    assert delivery["delivery"] == "NO"
    assert delivery["matched"] is False
    assert delivery["any_matched"] is True
    assert delivery["n_memories"] == 2
    per_memory = delivery["per_memory"]
    assert per_memory[0]["delivery_mode"] == "matched"
    assert per_memory[1]["delivery_mode"] == "unmatched"
    assert per_memory[0]["cid"] is None
    assert "suppression" not in per_memory[1]


def test_m2_proof_prove_delivery_all_matched_is_yes() -> None:
    def recall_fn(query: str) -> dict[str, Any]:
        return {
            "status": "ok",
            "memories": [{"cid": f"cid-{query}", "text": f"...{query}..."}],
        }

    proof = _build_delivery_proof(
        logger_name="test.lifecycle.m2.delivery.all-matched",
        recall_fn=recall_fn,
    )
    delivery = proof.prove_delivery(
        "org-9",
        [
            {"fragment": "fragment-one", "cid": "cid-fragment-one"},
            {"fragment": "fragment-two", "cid": "cid-fragment-two"},
        ],
    )

    assert delivery["delivery"] == "YES"
    assert delivery["matched"] is True
    assert delivery["any_matched"] is True
    assert delivery["n_memories"] == 2
    per_memory = delivery["per_memory"]
    assert all(entry["matched"] is True for entry in per_memory)
    assert all(entry["delivery_mode"] == "matched" for entry in per_memory)
    assert all(isinstance(entry["fragment_fp"], str) and len(entry["fragment_fp"]) == 8 for entry in per_memory)


def test_leader_verify_and_commit_precheck_committed_short_circuits_without_broadcast() -> None:
    logger, _ = _capture_logger("test.lifecycle.m2.precheck.committed")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("99" * 32)
    contributor = Identity.from_hex("aa" * 32)

    class FakeOrchestrator:
        org_id = "org-1"

    class FakeHubClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def commit_status(self, identity: Identity, org_id: str) -> Any:
            self.calls.append("commit_status")
            assert identity is leader
            assert org_id == "org-1"
            return {
                "submissions": [
                    {
                        "submission_hash": "sub-1",
                        "status": "committed",
                        "commit_error": "",
                    }
                ]
            }

        def moderation_queue(self, identity: Identity, org_id: str) -> Any:
            raise AssertionError("moderation_queue must not run for already committed submissions")

    hub_client = FakeHubClient()
    proof = M2Proof(
        cfg=cfg,
        orchestrator=FakeOrchestrator(),
        leader=leader,
        contributor=contributor,
        logger=logger,
        mcp_rest_factory=lambda _base_url: object(),
        hub_client=hub_client,
    )

    commit_batch_called = False

    def _unexpected_commit_batch(_org_id: str, _batch_payload: Any) -> dict[str, Any]:
        nonlocal commit_batch_called
        commit_batch_called = True
        raise AssertionError("_commit_batch must not run for already committed submissions")

    proof._commit_batch = _unexpected_commit_batch  # type: ignore[method-assign]

    result = proof.leader_verify_and_commit("org-1", "sub-1", ["python"])

    assert hub_client.calls == ["commit_status"]
    assert commit_batch_called is False
    assert result["hops"] == ["commit_precheck"]
    assert result["queue_item"] is None
    assert result["embed_card"] is None
    assert result["submit_keyword_results"] is None
    assert result["verify_keywords"] is None
    assert result["batch_submit"] is None
    assert result["commit_batch"] is None
    assert result["already_committed"] is True
    assert result["commit_status"] == {
        "submissions": [
            {
                "submission_hash": "sub-1",
                "status": "committed",
                "commit_error": "",
            }
        ]
    }


def test_leader_verify_and_commit_precheck_non_committed_runs_full_flow() -> None:
    logger, _ = _capture_logger("test.lifecycle.m2.precheck.non-committed")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("bb" * 32)
    contributor = Identity.from_hex("cc" * 32)
    calls: list[str] = []

    class FakeOrchestrator:
        org_id = "org-1"

    class FakeLeaderRest:
        def mod_embed_retrieval_card(self, items: list[dict[str, Any]], org_id: str | None = None) -> list[dict[str, Any]]:
            calls.append("mod_embed_retrieval_card")
            assert org_id == "org-1"
            assert items[0]["id"] == "sub-1"
            return [
                {
                    "id": "sub-1",
                    "vector": [0.1, 0.2],
                    "embedding_model_id": "nomic-embed-text:v1.5",
                    "embedding_schema_version": 1,
                    "umbral_capsule": "cap-1",
                    "umbral_ciphertext": "cipher-1",
                }
            ]

    class FakeHubClient:
        def __init__(self) -> None:
            self.commit_status_calls = 0

        def commit_status(self, identity: Identity, org_id: str) -> Any:
            calls.append("commit_status")
            assert identity is leader
            assert org_id == "org-1"
            self.commit_status_calls += 1
            if self.commit_status_calls == 1:
                return {
                    "submissions": [
                        {
                            "submission_hash": "sub-1",
                            "status": "pending",
                            "commit_error": "",
                        }
                    ]
                }
            return {
                "submissions": [
                    {
                        "submission_hash": "sub-1",
                        "status": "committed",
                        "commit_error": "",
                    }
                ]
            }

        def moderation_queue(self, identity: Identity, org_id: str) -> Any:
            calls.append("moderation_queue")
            assert identity is leader
            assert org_id == "org-1"
            return [
                {
                    "submission_hash": "sub-1",
                    "ciphertext_hex": "cafe",
                    "wrapped_dek_mod": "babe",
                    "epoch_id": 0,
                    "stack_hint": "python",
                }
            ]

        def submit_keyword_results(
            self,
            identity: Identity,
            org_id: str,
            submission_hash: str,
            classified: list[dict[str, Any]],
        ) -> Any:
            calls.append("submit_keyword_results")
            assert identity is leader
            assert org_id == "org-1"
            assert submission_hash == "sub-1"
            assert classified
            return {
                "verified": 1,
                "results": [
                    {
                        "submission_hash": "sub-1",
                        "passed": True,
                        "code": "ok",
                        "error": "",
                    }
                ],
            }

        def verify_keywords(self, identity: Identity, org_id: str, entries: list[dict[str, Any]]) -> Any:
            calls.append("verify_keywords")
            assert identity is leader
            assert org_id == "org-1"
            assert entries[0]["submission_hash"] == "sub-1"
            return {
                "verified": 1,
                "results": [
                    {
                        "submission_hash": "sub-1",
                        "passed": True,
                        "code": "ok",
                        "error": "",
                    }
                ],
            }

        def batch_submit(self, identity: Identity, org_id: str) -> Any:
            calls.append("batch_submit")
            assert identity is leader
            assert org_id == "org-1"
            return {
                "batch": [
                    {
                        "submission_hash": "sub-1",
                        "vector": [0.1, 0.2],
                    }
                ],
                "verification": {"status": "ok"},
            }

    hub_client = FakeHubClient()
    proof = M2Proof(
        cfg=cfg,
        orchestrator=FakeOrchestrator(),
        leader=leader,
        contributor=contributor,
        logger=logger,
        mcp_rest_factory=lambda _base_url: FakeLeaderRest(),
        hub_client=hub_client,
        sleep_fn=lambda _seconds: None,
    )

    commit_batch_payloads: list[dict[str, Any]] = []

    def _fake_commit_batch(_org_id: str, batch_payload: Any) -> dict[str, Any]:
        calls.append("commit_batch")
        assert isinstance(batch_payload, dict)
        commit_batch_payloads.append(batch_payload)
        return {"tx_hash": "tx-1", "code": 0, "msg_count": 1}

    proof._commit_batch = _fake_commit_batch  # type: ignore[method-assign]

    result = proof.leader_verify_and_commit("org-1", "sub-1", ["python"])

    assert calls == [
        "commit_status",
        "moderation_queue",
        "mod_embed_retrieval_card",
        "submit_keyword_results",
        "verify_keywords",
        "batch_submit",
        "commit_batch",
        "commit_status",
    ]
    assert result["hops"] == [
        "commit_precheck",
        "moderation_queue",
        "mod_embed_retrieval_card",
        "submit_keyword_results",
        "verify_keywords",
        "batch_submit",
        "commit_batch",
        "commit_status",
    ]
    assert "already_committed" not in result
    assert commit_batch_payloads and commit_batch_payloads[0]["batch"][0]["submission_hash"] == "sub-1"


def test_leader_verify_and_commit_precheck_commit_error_raises_without_broadcast() -> None:
    logger, _ = _capture_logger("test.lifecycle.m2.precheck.commit-error")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("dd" * 32)
    contributor = Identity.from_hex("ee" * 32)

    class FakeOrchestrator:
        org_id = "org-1"

    class FakeHubClient:
        def __init__(self) -> None:
            self.moderation_queue_calls = 0

        def commit_status(self, identity: Identity, org_id: str) -> Any:
            assert identity is leader
            assert org_id == "org-1"
            return {
                "submissions": [
                    {
                        "submission_hash": "sub-1",
                        "status": "failed",
                        "commit_error": "ErrMemoryExists",
                    }
                ]
            }

        def moderation_queue(self, identity: Identity, org_id: str) -> Any:
            self.moderation_queue_calls += 1
            raise AssertionError("moderation_queue must not run when commit_error is already recorded")

    hub_client = FakeHubClient()
    proof = M2Proof(
        cfg=cfg,
        orchestrator=FakeOrchestrator(),
        leader=leader,
        contributor=contributor,
        logger=logger,
        mcp_rest_factory=lambda _base_url: object(),
        hub_client=hub_client,
    )

    commit_batch_called = False

    def _unexpected_commit_batch(_org_id: str, _batch_payload: Any) -> dict[str, Any]:
        nonlocal commit_batch_called
        commit_batch_called = True
        raise AssertionError("_commit_batch must not run when precheck has commit_error")

    proof._commit_batch = _unexpected_commit_batch  # type: ignore[method-assign]

    try:
        proof.leader_verify_and_commit("org-1", "sub-1", ["python"])
        raise AssertionError("expected RuntimeError for stored commit_error")
    except RuntimeError as exc:
        assert "ErrMemoryExists" in str(exc)

    assert hub_client.moderation_queue_calls == 0
    assert commit_batch_called is False
