from __future__ import annotations

import io
import logging
import os
import re
import subprocess

import pytest

from wevibe_bench.lifecycle.admin_cli import AdminCli
from wevibe_bench.lifecycle.hub_client import HubClient
from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import LifecycleConfig
import wevibe_bench.lifecycle.mcp_process as mcp_process_module
from wevibe_bench.lifecycle.mcp_process import McpInstance, McpProcessManager
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


def test_mcp_process_build_env_sets_required_keys_and_never_logs_raw_seed(tmp_path, monkeypatch) -> None:
    logger, stream = _capture_logger("test.lifecycle.mcp_env")

    leader_seed = "ab" * 32
    contrib_seed = "cd" * 32
    leader_keystore = tmp_path / "leader-keystore"
    contrib_keystore = tmp_path / "contrib-keystore"
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_SEED_HEX", leader_seed)
    monkeypatch.setenv("WEVIBE_BENCH_CONTRIB_SEED_HEX", contrib_seed)
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_KEYSTORE", str(leader_keystore))
    monkeypatch.setenv("WEVIBE_BENCH_CONTRIB_KEYSTORE", str(contrib_keystore))
    cfg = LifecycleConfig()
    manager = McpProcessManager("/workspace", cfg, logger)

    env = manager._build_env(
        name="leader",
        port=4450,
    )

    assert env["WEVIBE_MCP_HTTP_ONLY"] == "1"
    assert env["WEVIBE_MCP_HTTP_PORT"] == "4450"
    assert env["WEVIBE_SEED_BACKEND"] == "file"
    assert env["WEVIBE_IDENTITY_SEED_HEX"] == leader_seed
    assert env["WEVIBE_HOME"] == os.path.abspath(str(leader_keystore))
    assert env["WEVIBE_KEYSTORE_PATH"] == os.path.abspath(str(leader_keystore))
    assert env["WEVIBE_UMBRAL_SIDECAR_BIN"] == "/workspace/wevibe-umbral/target/release/wevibe-umbral"
    assert env["WEVIBE_GUARD_BIN"] == "/workspace/wevibe-guard/target/release/wevibe-guard"
    assert env["WEVIBE_HUB_URL"] == cfg.hub_url

    logs = stream.getvalue()
    assert leader_seed not in logs
    assert contrib_seed not in logs
    assert "seed_fp=" not in logs
    assert "keystore_path=" in logs


def test_mcp_process_build_env_role_specific_keystore_and_seed(tmp_path, monkeypatch) -> None:
    logger, _ = _capture_logger("test.lifecycle.mcp_env.roles")

    leader_seed = "ab" * 32
    contrib_seed = "cd" * 32
    leader_keystore = tmp_path / "leader-keystore"
    contrib_keystore = tmp_path / "contrib-keystore"
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_SEED_HEX", leader_seed)
    monkeypatch.setenv("WEVIBE_BENCH_CONTRIB_SEED_HEX", contrib_seed)
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_KEYSTORE", str(leader_keystore))
    monkeypatch.setenv("WEVIBE_BENCH_CONTRIB_KEYSTORE", str(contrib_keystore))
    cfg = LifecycleConfig()
    manager = McpProcessManager("/workspace", cfg, logger)

    leader_env = manager._build_env(name="leader", port=4450)
    contrib_env = manager._build_env(name="contributor", port=4451)

    assert leader_env["WEVIBE_KEYSTORE_PATH"] == os.path.abspath(str(leader_keystore))
    assert contrib_env["WEVIBE_KEYSTORE_PATH"] == os.path.abspath(str(contrib_keystore))
    assert leader_env["WEVIBE_IDENTITY_SEED_HEX"] == leader_seed
    assert contrib_env["WEVIBE_IDENTITY_SEED_HEX"] == contrib_seed
    assert leader_env["WEVIBE_HOME"] == os.path.abspath(str(leader_keystore))
    assert contrib_env["WEVIBE_HOME"] == os.path.abspath(str(contrib_keystore))

    # The two roles must get distinct keystores and distinct seeds.
    assert leader_env["WEVIBE_KEYSTORE_PATH"] != contrib_env["WEVIBE_KEYSTORE_PATH"]
    assert leader_env["WEVIBE_IDENTITY_SEED_HEX"] != contrib_env["WEVIBE_IDENTITY_SEED_HEX"]
    assert leader_env["WEVIBE_HOME"] == leader_env["WEVIBE_KEYSTORE_PATH"]
    assert contrib_env["WEVIBE_HOME"] == contrib_env["WEVIBE_KEYSTORE_PATH"]


def test_mcp_process_build_env_fails_closed_when_bench_identity_seed_missing(tmp_path, monkeypatch) -> None:
    logger, _ = _capture_logger("test.lifecycle.mcp_env.fail_closed")
    leader_keystore = tmp_path / "leader-keystore"
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_KEYSTORE", str(leader_keystore))
    monkeypatch.delenv("WEVIBE_BENCH_LEADER_SEED_HEX", raising=False)

    manager = McpProcessManager("/workspace", LifecycleConfig(), logger)
    with pytest.raises(RuntimeError, match="WEVIBE_BENCH_LEADER_SEED_HEX"):
        manager._build_env(name="leader", port=4450)

    # With the seed present (cfg constructed under the env), no error is raised.
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_SEED_HEX", "ab" * 32)
    cfg_ok = LifecycleConfig()
    manager_ok = McpProcessManager("/workspace", cfg_ok, logger)
    env = manager_ok._build_env(name="leader", port=4450)
    assert env["WEVIBE_IDENTITY_SEED_HEX"] == "ab" * 32


def test_mcp_process_uses_wevibe_bench_mcp_root_env(monkeypatch) -> None:
    logger, _ = _capture_logger("test.lifecycle.mcp_root")
    monkeypatch.setenv("WEVIBE_BENCH_MCP_ROOT", "/workspace/wevibe-mcp-clone")

    manager = McpProcessManager("/workspace", LifecycleConfig(), logger)
    assert manager.mcp_root == "/workspace/wevibe-mcp-clone"


def test_mcp_process_build_dist_skips_when_flag_set(monkeypatch) -> None:
    logger, _ = _capture_logger("test.lifecycle.mcp_build_skip")
    manager = McpProcessManager("/workspace", LifecycleConfig(), logger)
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("WEVIBE_BENCH_SKIP_BUILD", "1")
    monkeypatch.setattr(mcp_process_module.subprocess, "run", fake_run)

    manager.build_dist()
    assert called is False


def test_mcp_process_wait_healthy_returns_true_when_health_endpoint_is_ready(tmp_path) -> None:
    logger, _ = _capture_logger("test.lifecycle.wait_healthy")
    token_path = tmp_path / "token"
    token_path.write_text("session-token-123", encoding="utf-8")
    cfg = LifecycleConfig(session_token_path=str(token_path))

    def transport(url: str, headers: dict[str, str], body: dict[str, object] | None):
        assert url == "http://127.0.0.1:4450/v1/health"
        assert headers["Authorization"] == "Bearer session-token-123"
        assert body is None
        return 200, {"status": "ok"}, True

    manager = McpProcessManager("/workspace", cfg, logger, transport=transport)
    inst = McpInstance(
        name="leader",
        port=4450,
        keystore_path="/tmp/leader.ks",
        log_path="/tmp/leader.log",
        pid=123,
        url="http://127.0.0.1:4450",
    )

    assert manager.wait_healthy(inst, timeout_s=0.1) is True


def test_mcp_process_export_pairing_returns_response_body(tmp_path) -> None:
    logger, _ = _capture_logger("test.lifecycle.export_pairing")
    token_path = tmp_path / "token"
    token_path.write_text("token-x", encoding="utf-8")
    cfg = LifecycleConfig(session_token_path=str(token_path))

    def transport(url: str, headers: dict[str, str], body: dict[str, object] | None):
        assert url == "http://127.0.0.1:4451/v1/identity/export-pairing"
        assert headers["Authorization"] == "Bearer token-x"
        assert body == {}
        return 200, {"code": "PAIR-CODE", "pairing_id": "pair-123"}, True

    manager = McpProcessManager("/workspace", cfg, logger, transport=transport)
    inst = McpInstance(
        name="contributor",
        port=4451,
        keystore_path="/tmp/contrib.ks",
        log_path="/tmp/contrib.log",
        pid=124,
        url="http://127.0.0.1:4451",
    )

    assert manager.export_pairing(inst) == {"code": "PAIR-CODE", "pairing_id": "pair-123"}


def test_admin_cli_create_org_parses_org_id_from_stdout() -> None:
    logger, _ = _capture_logger("test.lifecycle.admin_create_org")
    captured: dict[str, object] = {}

    def runner(*args, **kwargs):
        captured["cmd"] = args[0]
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="Org created: org-test-123\n",
            stderr="",
        )

    cli = AdminCli("/workspace", {"WEVIBE_KEYSTORE_PATH": "/tmp/ks"}, logger, runner=runner)
    result = cli.create_org("Acme", "acme.example", "wallet-abc")

    assert result["org_id"] == "org-test-123"
    assert captured["cmd"] == [
        "node",
        "/workspace/wevibe-mcp/dist/admin.js",
        "create-org",
        "--name",
        "Acme",
        "--domain",
        "acme.example",
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["WEVIBE_LEADER_WALLET"] == "wallet-abc"


def test_admin_cli_uses_wevibe_bench_mcp_root_env(monkeypatch) -> None:
    logger, _ = _capture_logger("test.lifecycle.admin_mcp_root")
    captured: dict[str, object] = {}
    monkeypatch.setenv("WEVIBE_BENCH_MCP_ROOT", "/workspace/wevibe-mcp-clone")

    def runner(*args, **kwargs):
        captured["cmd"] = args[0]
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok\n", stderr="")

    cli = AdminCli("/workspace", {}, logger, runner=runner)
    cli.invite(
        org_id="org-1",
        invitee_pubkey="ed-1",
        invitee_x25519="x-1",
        invitee_pre_pubkey="pre-1",
    )

    assert captured["cmd"][0:2] == ["node", "/workspace/wevibe-mcp-clone/dist/admin.js"]
    assert captured["cwd"] == "/workspace/wevibe-mcp-clone"


def test_admin_cli_raises_on_nonzero_exit() -> None:
    logger, _ = _capture_logger("test.lifecycle.admin_fail")

    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="boom")

    cli = AdminCli("/workspace", {}, logger, runner=runner)
    with pytest.raises(RuntimeError, match="boom"):
        cli.create_org("Acme", "acme.example", "wallet-abc")


def test_hub_client_enable_recall_builds_expected_request() -> None:
    logger, _ = _capture_logger("test.lifecycle.hub_enable")
    identity = Identity.from_hex("11" * 32)
    captured: dict[str, object] = {}

    def transport(url: str, headers: dict[str, str], body: dict[str, object] | None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"status": "ok"}, True

    cfg = LifecycleConfig(hub_url="http://127.0.0.1:4440")
    client = HubClient(cfg, logger, transport=transport)
    response = client.enable_recall(identity, "org-1", "aa" * 32, free=True)

    assert response == {"status": "ok"}
    assert captured["url"] == f"http://127.0.0.1:4440/v1/orgs/org-1/members/{'aa' * 32}/enable-recall"
    assert captured["body"] == {"signed_by": identity.ed_pubkey_hex, "free": True}

    headers = captured["headers"]
    assert isinstance(headers, dict)
    auth = headers["Authorization"]
    pattern = (
        r"^WeVibe-Signed pubkey=[0-9a-f]{64},"
        r"timestamp=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z,"
        r"signature=[0-9a-f]{128}$"
    )
    assert re.fullmatch(pattern, auth)
    assert headers["X-WeVibe-Trace-Id"].startswith("lc-")


def test_mcp_rest_extract_and_recall_build_expected_urls_and_bearer_header(tmp_path) -> None:
    logger, _ = _capture_logger("test.lifecycle.mcp_rest")
    token_path = tmp_path / "token"
    token_path.write_text("bearer-xyz", encoding="utf-8")

    calls: list[dict[str, object]] = []

    def transport(url: str, headers: dict[str, str], body: dict[str, object] | None):
        calls.append({"url": url, "headers": headers, "body": body})
        if url.endswith("/v1/extract"):
            return 202, {"job_id": "job-1", "status": "accepted"}, True
        if url.endswith("/v1/recall"):
            return 200, {"status": "ok", "memories": []}, True
        raise AssertionError(f"unexpected url {url}")

    cfg = LifecycleConfig(session_token_path=str(token_path))
    client = McpRest("http://127.0.0.1:4450", cfg, logger, transport=transport)
    session_db_path = "/tmp/bench-test/session-db/opencode.db"

    job_id = client.extract("model-a", session_db_path, org_id="org-7")
    recall = client.recall("query text", "org-7")

    assert job_id == "job-1"
    assert recall == {"status": "ok", "memories": []}

    assert calls[0]["url"] == "http://127.0.0.1:4450/v1/extract"
    assert calls[0]["body"] == {
        "session_db_path": session_db_path,
        "model": "model-a",
        "org_id": "org-7",
    }

    extract_headers = calls[0]["headers"]
    assert isinstance(extract_headers, dict)
    assert extract_headers["Authorization"] == "Bearer bearer-xyz"

    assert calls[1]["url"] == "http://127.0.0.1:4450/v1/recall"
    assert calls[1]["body"] == {"query": "query text", "org_id": "org-7"}

    recall_headers = calls[1]["headers"]
    assert isinstance(recall_headers, dict)
    assert recall_headers["Authorization"] == "Bearer bearer-xyz"


def test_mcp_rest_extract_includes_session_id_only_when_provided(tmp_path) -> None:
    logger, _ = _capture_logger("test.lifecycle.mcp_rest.session_id")
    token_path = tmp_path / "token"
    token_path.write_text("bearer-xyz", encoding="utf-8")

    calls: list[dict[str, object]] = []

    def transport(url: str, headers: dict[str, str], body: dict[str, object] | None):
        calls.append({"url": url, "headers": headers, "body": body})
        if url.endswith("/v1/extract"):
            return 202, {"job_id": f"job-{len(calls)}", "status": "accepted"}, True
        raise AssertionError(f"unexpected url {url}")

    cfg = LifecycleConfig(session_token_path=str(token_path))
    client = McpRest("http://127.0.0.1:4450", cfg, logger, transport=transport)
    session_db_path = "/tmp/bench-test/session-db/opencode.db"

    assert client.extract("model-a", session_db_path, org_id="org-7", session_id="sess-xyz") == "job-1"
    assert client.extract("model-a", session_db_path, org_id="org-7") == "job-2"
    assert client.extract("model-a", session_db_path, org_id="org-7", session_id=None) == "job-3"

    first_body = calls[0]["body"]
    assert isinstance(first_body, dict)
    assert first_body["session_id"] == "sess-xyz"

    second_body = calls[1]["body"]
    assert isinstance(second_body, dict)
    assert "session_id" not in second_body

    third_body = calls[2]["body"]
    assert isinstance(third_body, dict)
    assert "session_id" not in third_body
