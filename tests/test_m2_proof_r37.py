from __future__ import annotations

import io
import logging
from typing import Any, Callable

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


def _build_delivery_proof(
    *,
    logger_name: str,
    recall_fn: Callable[[str], dict[str, Any]],
) -> M2Proof:
    logger, _ = _capture_logger(logger_name)
    cfg = LifecycleConfig(leader_mcp_url="http://127.0.0.1:4550")

    class FakeLeaderRest:
        def recall(self, query: str, org_id: str, **_kw: Any) -> dict[str, Any]:
            assert org_id == "org-9"
            return recall_fn(query)

    class FakeOrchestrator:
        org_id = "org-9"

    def mcp_rest_factory(base_url: str):
        # Matches M2Proof.prove_delivery's real call path:
        # self._leader_rest() == self._mcp_rest_factory(cfg.leader_mcp_url).
        assert base_url == cfg.leader_mcp_url
        return FakeLeaderRest()

    return M2Proof(
        cfg=cfg,
        orchestrator=FakeOrchestrator(),
        logger=logger,
        mcp_rest_factory=mcp_rest_factory,
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
