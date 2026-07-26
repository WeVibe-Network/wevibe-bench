from __future__ import annotations

import io
import logging
import re
from typing import Any

import pytest

from wevibe_bench.lifecycle.hub_client import HubClient
from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import (
    DEFAULT_ORG_DESCRIPTION,
    DEFAULT_ORG_FOCUS_AREAS,
    DEFAULT_ORG_KEYWORDS,
    DEFAULT_ORG_TECH_STACK,
    LifecycleConfig,
)
from wevibe_bench.preflight import PreflightError, verify_org_checklist


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(logging.StreamHandler(stream))
    return logger, stream


def test_hub_client_add_keyword_posts_expected_path_body_and_signed_auth() -> None:
    logger, _ = _capture_logger("test.lifecycle.hub_add_keyword")
    identity = Identity.from_hex("11" * 32)
    captured: dict[str, object] = {}

    def transport(url: str, headers: dict[str, str], body: dict[str, object] | None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"status": "ok"}, True

    cfg = LifecycleConfig(hub_url="http://127.0.0.1:4440")
    client = HubClient(cfg, logger, transport=transport)

    response = client.add_keyword(identity, "org-1", "backgammon")

    assert response == {"status": "ok"}
    assert captured["url"] == "http://127.0.0.1:4440/v1/orgs/org-1/keywords"
    assert captured["body"] == {"keyword": "backgammon"}
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"].startswith("WeVibe-Signed ")


def test_verify_org_checklist_passes_with_active_keyword_and_description() -> None:
    identity = Identity.from_hex("22" * 32)
    calls: list[dict[str, Any]] = []

    def fake_http_get(
        url: str,
        token: str | None,
        timeout: float = 5.0,
        headers: dict[str, str] | None = None,
        parse_any_json: bool = False,
    ) -> tuple[int, Any, bool]:
        calls.append({"url": url, "token": token, "headers": headers, "parse_any_json": parse_any_json})
        if url.endswith("/keywords"):
            return 200, [{"keyword": "backgammon", "deprecated": False}], True
        return 200, {"description": "Valid org description"}, True

    verify_org_checklist(
        hub_url="http://127.0.0.1:4440",
        org_id="org-1",
        identity=identity,
        http_get=fake_http_get,
    )

    assert len(calls) == 2
    assert calls[0]["url"].endswith("/v1/orgs/org-1/keywords")
    assert calls[0]["parse_any_json"] is True
    assert isinstance(calls[0]["headers"], dict)
    auth = calls[0]["headers"]["Authorization"]
    assert isinstance(auth, str)
    assert auth.startswith("WeVibe-Signed ")
    assert calls[1]["url"].endswith("/v1/orgs/org-1")


@pytest.mark.parametrize(
    ("kw_status", "kw_body", "kw_reachable", "org_status", "org_body", "org_reachable", "match"),
    [
        (200, [], True, 200, {"description": "ok"}, True, "check=keywords"),
        (200, [{"keyword": "k", "deprecated": True}], True, 200, {"description": "ok"}, True, "check=keywords"),
        (503, [], True, 200, {"description": "ok"}, True, "check=keywords"),
        (200, [{"keyword": "k", "deprecated": False}], True, 503, {"description": "ok"}, True, "check=org_profile"),
        (200, [{"keyword": "k", "deprecated": False}], True, 200, {"description": ""}, True, "check=org_profile"),
        (200, [{"keyword": "k", "deprecated": False}], True, 200, {"description": "x" * 501}, True, "check=org_profile"),
    ],
)
def test_verify_org_checklist_failures_raise_with_named_check(
    kw_status: int,
    kw_body: Any,
    kw_reachable: bool,
    org_status: int,
    org_body: Any,
    org_reachable: bool,
    match: str,
) -> None:
    identity = Identity.from_hex("33" * 32)

    def fake_http_get(
        url: str,
        token: str | None,
        timeout: float = 5.0,
        headers: dict[str, str] | None = None,
        parse_any_json: bool = False,
    ) -> tuple[int, Any, bool]:
        if url.endswith("/keywords"):
            return kw_status, kw_body, kw_reachable
        return org_status, org_body, org_reachable

    with pytest.raises(PreflightError, match=match):
        verify_org_checklist(
            hub_url="http://127.0.0.1:4440",
            org_id="org-1",
            identity=identity,
            http_get=fake_http_get,
        )


def test_lifecycle_config_defaults_and_org_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEVIBE_BENCH_ORG_DESCRIPTION", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_ORG_TECH_STACK", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_ORG_FOCUS_AREAS", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_ORG_KEYWORDS", raising=False)

    cfg = LifecycleConfig()
    assert cfg.org_description == DEFAULT_ORG_DESCRIPTION
    assert cfg.org_tech_stack == DEFAULT_ORG_TECH_STACK
    assert cfg.org_focus_areas == DEFAULT_ORG_FOCUS_AREAS
    assert cfg.org_keywords == DEFAULT_ORG_KEYWORDS

    assert len(cfg.org_description) <= 500
    assert len(cfg.org_tech_stack) <= 200
    assert len(cfg.org_focus_areas) <= 200

    assert len(cfg.org_keywords) == 20
    assert all(re.fullmatch(r"^[a-z][a-z0-9_]{1,39}$", keyword) for keyword in cfg.org_keywords)

    monkeypatch.setenv("WEVIBE_BENCH_ORG_KEYWORDS", "foo, bar,,baz")
    cfg_keywords = LifecycleConfig()
    assert cfg_keywords.org_keywords == ("foo", "bar", "baz")

    monkeypatch.setenv("WEVIBE_BENCH_ORG_DESCRIPTION", "Org override")
    cfg_desc = LifecycleConfig()
    assert cfg_desc.org_description == "Org override"
