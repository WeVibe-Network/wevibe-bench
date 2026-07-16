from __future__ import annotations

from wevibe_bench.backends.base import DeliveryVerdict, NeedCard
from wevibe_bench.backends.none_backend import NoneBackend
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import RunConfig


def _cfg() -> RunConfig:
    return RunConfig(
        model_ladder=("model-a",),
        mcp_recall_url="http://offline.local",
        session_token_path="/tmp/__wevibe_bench_missing_token__",
    )


def test_none_backend_returns_off_control_empty_memories_and_verify_delivery_no() -> None:
    cfg = _cfg()
    backend = NoneBackend()
    need = NeedCard(intent="intent", task="task", query="query")

    result = backend.recall(need, cfg)

    assert result.memories == []
    assert result.status == "ok"
    assert result.reason_code == "off_control"
    assert result.reachable is True
    # OFF delivery is N/A at the scorecard cell level; backend verdict is always NO.
    assert backend.verify_delivery(result) == DeliveryVerdict.NO


def test_wevibe_backend_parses_200_response_verifies_yes_and_sends_expected_wire_body() -> None:
    cfg = _cfg()
    captured: dict[str, object] = {}

    def transport(url: str, headers: dict[str, str], body: dict[str, object]):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return (
            200,
            {
                "status": "ok",
                "memories": [
                    {
                        "cid": "cid-123",
                        "score": 0.91,
                        "breakdown": {
                            "vector_score": 0.44,
                            "combined_score": 0.91,
                            "keyword_score": 0.47,
                        },
                        "matched_keywords": ["alpha", "beta"],
                        "text": "decrypted memory text",
                    }
                ],
            },
            True,
        )

    backend = WeVibeBackend(cfg, transport=transport)
    backend.prime_session("session-xyz")
    need = NeedCard(
        intent="Implement feature",
        task="Fix parser",
        query="raw query",
        stack=["python"],
        deps=["pydantic"],
        error_strings=["ERR_PARSE"],
        files=["parser.py"],
        directory="/repo",
        project_name="bench-project",
    )

    result = backend.recall(need, cfg)

    assert result.reachable is True
    assert result.http_status == 200
    assert result.status == "ok"
    assert len(result.memories) == 1

    memory = result.memories[0]
    assert memory.cid == "cid-123"
    assert memory.score == 0.91
    assert memory.vector_score == 0.44
    assert memory.combined_score == 0.91
    assert memory.keyword_score == 0.47
    assert memory.matched_keywords == ["alpha", "beta"]
    assert memory.text == "decrypted memory text"
    assert backend.verify_delivery(result) == DeliveryVerdict.YES

    sent_body = captured["body"]
    assert isinstance(sent_body, dict)
    assert captured["url"] == "http://offline.local/v1/recall"

    assert sent_body["query"] == "raw query"
    assert sent_body["intent"] == "Implement feature"
    assert sent_body["task"] == "Fix parser"
    assert sent_body["org_id"] == cfg.org_id
    assert sent_body["mc_version"] == cfg.mc_version
    assert sent_body["session_id"] == "session-xyz"
    assert sent_body["relevance_floor"] == cfg.relevance_floor()
    assert sent_body["surface_budget"] == cfg.surface_budget
    # Recall fetches a wide candidate set (limit) and surfaces only the top surface_budget; the two are intentionally distinct.
    assert sent_body["limit"] == cfg.deterministic_recall_limit
    assert sent_body["limit"] != sent_body["surface_budget"]
    assert sent_body["errorStrings"] == ["ERR_PARSE"]
    assert sent_body["projectName"] == "bench-project"
    assert "prompt_digest" not in sent_body


def test_wevibe_backend_unreachable_transport_returns_no_and_never_fabricates_memories() -> None:
    cfg = _cfg()

    backend = WeVibeBackend(cfg, transport=lambda url, headers, body: (0, {}, False))
    result = backend.recall(NeedCard(intent="i", task="t", query="q"), cfg)

    assert result.reachable is False
    assert result.status == "error"
    assert result.http_status == 0
    assert result.memories == []
    assert len(result.memories) == 0
    assert backend.verify_delivery(result) == DeliveryVerdict.NO


def test_wevibe_backend_decrypt_failed_with_no_memories_maps_to_called() -> None:
    cfg = _cfg()

    backend = WeVibeBackend(
        cfg,
        transport=lambda url, headers, body: (
            200,
            {"status": "ok", "reason_code": "decrypt_failed", "memories": []},
            True,
        ),
    )

    result = backend.recall(NeedCard(intent="i", task="t", query="q"), cfg)

    assert result.reachable is True
    assert result.http_status == 200
    assert result.memories == []
    assert backend.verify_delivery(result) == DeliveryVerdict.CALLED
