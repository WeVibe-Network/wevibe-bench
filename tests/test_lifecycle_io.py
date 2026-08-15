from __future__ import annotations

import io
import logging

from wevibe_bench.lifecycle.lconfig import LifecycleConfig
from wevibe_bench.lifecycle.mcp_rest import McpRest


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    return logger, stream


def test_mcp_rest_recall_builds_expected_url_and_bearer_header(tmp_path) -> None:
    logger, _ = _capture_logger("test.lifecycle.mcp_rest.recall")
    token_path = tmp_path / "token"
    token_path.write_text("bearer-xyz", encoding="utf-8")

    calls: list[dict[str, object]] = []

    def transport(url: str, headers: dict[str, str], body: dict[str, object] | None):
        calls.append({"url": url, "headers": headers, "body": body})
        if url.endswith("/v1/recall"):
            return 200, {"status": "ok", "memories": []}, True
        raise AssertionError(f"unexpected url {url}")

    cfg = LifecycleConfig(session_token_path=str(token_path))
    client = McpRest("http://127.0.0.1:4450", cfg, logger, transport=transport)

    recall = client.recall("query text", "org-7")

    assert recall == {"status": "ok", "memories": []}

    assert len(calls) == 1
    recall_call = calls[0]
    assert recall_call["url"] == "http://127.0.0.1:4450/v1/recall"
    # recall is a POST: McpRest._urllib_transport treats a non-None body as POST.
    assert recall_call["body"] == {"query": "query text", "org_id": "org-7"}

    recall_headers = recall_call["headers"]
    assert isinstance(recall_headers, dict)
    assert recall_headers["Authorization"] == "Bearer bearer-xyz"
