"""Lifecycle orchestrator + M2 proof coverage (MCP-REST and qdrant probe flows)."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
from typing import Any

import pytest

from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import LifecycleConfig
from wevibe_bench.lifecycle.m2_proof import M2Proof
from wevibe_bench.lifecycle.mcp_rest import McpRest
from wevibe_bench.lifecycle.orchestrator import LifecycleOrchestrator
from wevibe_bench.lifecycle.qdrant_probe import find_org_collection, snapshot_counts


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    return logger, stream


def test_qdrant_snapshot_counts_parses_collections_and_detects_plus_one_delta() -> None:
    responses: dict[str, tuple[int, Any, bool]] = {
        "http://127.0.0.1:6333/collections": (
            200,
            {
                "result": {
                    "collections": [
                        {"name": "org-42-memory"},
                        {"name": "shared"},
                    ]
                }
            },
            True,
        ),
        "http://127.0.0.1:6333/collections/org-42-memory": (
            200,
            {"result": {"points_count": 10}},
            True,
        ),
        "http://127.0.0.1:6333/collections/shared": (
            200,
            {"result": {"points_count": 4}},
            True,
        ),
    }

    def transport(url: str) -> tuple[int, Any, bool]:
        return responses[url]

    before = snapshot_counts("http://127.0.0.1:6333", transport=transport)
    assert before == {"org-42-memory": 10, "shared": 4}
    assert find_org_collection("http://127.0.0.1:6333", "org-42", transport=transport) == "org-42-memory"

    responses["http://127.0.0.1:6333/collections/org-42-memory"] = (
        200,
        {"result": {"points_count": 11}},
        True,
    )
    after = snapshot_counts("http://127.0.0.1:6333", transport=transport)
    delta = {name: after.get(name, 0) - before.get(name, 0) for name in sorted(set(before) | set(after))}

    assert delta["org-42-memory"] == 1
    assert any(change == 1 for change in delta.values())


def test_mcp_rest_identity_pubkeys_and_submit_use_expected_url_body_and_bearer(tmp_path) -> None:
    logger, _ = _capture_logger("test.lifecycle.mcp_rest.submit")
    token_path = tmp_path / "token"
    token_path.write_text("bearer-abc", encoding="utf-8")

    calls: list[dict[str, Any]] = []

    def transport(url: str, headers: dict[str, str], body: dict[str, Any] | None):
        calls.append({"url": url, "headers": headers, "body": body})
        if url.endswith("/v1/identity/pubkeys"):
            return 200, {"ed25519": "ed", "x25519": "x", "pre_pubkey": "pre"}, True
        if url.endswith("/v1/submit"):
            return 200, {"status": "ok", "submission_hash": "sub-1"}, True
        raise AssertionError(f"unexpected url {url}")

    cfg = LifecycleConfig(session_token_path=str(token_path))
    rest = McpRest("http://127.0.0.1:4551", cfg, logger, transport=transport)

    pubkeys = rest.identity_pubkeys()
    submit = rest.submit(
        org_id="org-1",
        plaintext="hello memory",
        memory_type="memory",
        epoch_id=7,
        stack_hint="python",
        keywords=["debug", "stack"],
        mc_version=1,
    )

    assert pubkeys == {"ed25519": "ed", "x25519": "x", "pre_pubkey": "pre"}
    assert submit == {"status": "ok", "submission_hash": "sub-1"}

    assert calls[0]["url"] == "http://127.0.0.1:4551/v1/identity/pubkeys"
    assert calls[0]["body"] is None
    assert calls[0]["headers"]["Authorization"] == "Bearer bearer-abc"

    assert calls[1]["url"] == "http://127.0.0.1:4551/v1/submit"
    assert calls[1]["headers"]["Authorization"] == "Bearer bearer-abc"
    assert calls[1]["body"] == {
        "org_id": "org-1",
        "plaintext": "hello memory",
        "memory_type": "memory",
        "epoch_id": 7,
        "stack_hint": "python",
        "keywords": ["debug", "stack"],
        "mc_version": 1,
    }


def test_orchestrator_run_m1_executes_expected_sequence_with_injected_fakes() -> None:
    logger, _ = _capture_logger("test.lifecycle.orchestrator.m1")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
        session_token_path="~/test-session-token",
        leader_signer_dir="/opt/leader-signer",
    )
    leader = Identity.from_hex("11" * 32)
    contributor = Identity.from_hex("22" * 32)

    calls: list[str] = []
    signer_calls: list[dict[str, Any]] = []

    class FakeAdminCli:
        def __init__(self, env: dict[str, str]) -> None:
            self._env = env

        def invite(
            self,
            org_id: str,
            invitee_pubkey: str,
            invitee_x25519: str,
            invitee_pre_pubkey: str,
            can_contribute: bool = True,
            can_moderate: bool = False,
        ) -> str:
            calls.append("invite")
            assert org_id == "org-123"
            assert invitee_pubkey == "ed-contrib"
            assert invitee_x25519 == "x-contrib"
            assert invitee_pre_pubkey == "pre-contrib"
            assert can_contribute is True
            assert can_moderate is False
            return "invited"

        def provision_recall(self, org_id: str) -> str:
            calls.append("provision_recall")
            assert org_id == "org-123"
            return "provisioned"

    class FakeHubClient:
        def __init__(self) -> None:
            self._contributor_member_orgs_checks = 0

        def enable_recall(self, identity: Identity, org_id: str, member_pubkey: str, free: bool = True) -> Any:
            calls.append("enable_recall")
            assert identity is leader
            assert org_id == "org-123"
            assert member_pubkey == "ed-contrib"
            assert free is True
            return {"status": "ok"}

        def member_orgs(self, identity: Identity) -> Any:
            if identity is leader:
                return []
            assert identity is contributor
            self._contributor_member_orgs_checks += 1
            # run_m1 checks membership before invite/add-member, then checks again while polling.
            if self._contributor_member_orgs_checks == 1:
                return []
            calls.append("poll_membership")
            return [{"org_id": "org-123"}]

    class FakeMcpRest:
        def identity_pubkeys(self) -> dict[str, str]:
            calls.append("contributor_pubkeys")
            return {"ed25519": "ed-contrib", "x25519": "x-contrib", "pre_pubkey": "pre-contrib"}

    class FakeProcman:
        pass

    def run_cmd(*args, **kwargs):
        cmd = args[0]
        signer_calls.append(
            {
                "cmd": cmd,
                "cwd": kwargs.get("cwd"),
                "env": kwargs.get("env"),
            }
        )
        if "register-org" in cmd:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout='register-org done\n{"org_id":"org-123","tx_hash":"tx-1","leader_wallet":"wallet-1","hub_serving_key":"hub-key"}\n',
                stderr="",
            )
        if "add-member" in cmd:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout='add-member done\n{"tx_hash":"tx-add-1","code":0}\n',
                stderr="",
            )
        raise AssertionError(f"unexpected command {cmd}")

    orchestrator = LifecycleOrchestrator(
        cfg=cfg,
        wevibe_root="/workspace",
        leader=leader,
        contributor=contributor,
        leader_keystore="/tmp/leader.ks",
        contributor_keystore="/tmp/contrib.ks",
        leader_wallet="wallet-1",
        logger=logger,
        procman=FakeProcman(),
        admin_cli_factory=lambda env: FakeAdminCli(env),
        hub_client=FakeHubClient(),
        mcp_rest_factory=lambda _url: FakeMcpRest(),
        sleep_fn=lambda _seconds: None,
        run_cmd=run_cmd,
    )

    result = orchestrator.run_m1()

    assert result["org_id"] == "org-123"
    assert result["contributor_pk"] == {
        "ed25519": "ed-contrib",
        "x25519": "x-contrib",
        "pre_pubkey": "pre-contrib",
    }
    assert [step["step"] for step in result["steps"]] == [
        "create_org",
        "contributor_pubkeys",
        "invite",
        "add_member_onchain",
        "enable_recall",
        "provision_recall",
        "poll_membership",
    ]
    assert calls == [
        "contributor_pubkeys",
        "invite",
        "enable_recall",
        "provision_recall",
        "poll_membership",
    ]

    assert len(signer_calls) == 2
    assert signer_calls[0]["cmd"] == [
        "node",
        "/opt/leader-signer/dist/cli.js",
        "register-org",
        "--org-name",
        "wevibe-bench-lifecycle",
        "--domain",
        "bench.wevibe.local",
    ]
    assert signer_calls[0]["cwd"] == "/opt/leader-signer"
    env = signer_calls[0]["env"]
    assert isinstance(env, dict)
    assert env["WEVIBE_IDENTITY_SEED_HEX"] == leader.seed_hex
    assert env["HUB_URL"] == cfg.hub_url
    assert env["WEVIBE_MCP_URL"] == cfg.leader_mcp_url
    assert env["WEVIBE_MCP_TOKEN_FILE"] == os.path.expanduser(cfg.session_token_path)
    assert env["WEVIBE_CHAIN_RPC"] == "http://localhost:26657"
    assert env["WEVIBE_CHAIN_REST"] == "http://localhost:1317"

    assert signer_calls[1]["cmd"] == [
        "node",
        "/opt/leader-signer/dist/cli.js",
        "add-member",
        "--org-id",
        "org-123",
        "--member-pubkey",
        "ed-contrib",
        "--x25519",
        "x-contrib",
        "--role",
        "member",
        "--can-contribute",
        "true",
        "--can-moderate",
        "false",
    ]
    assert signer_calls[1]["cwd"] == "/opt/leader-signer"
    add_member_env = signer_calls[1]["env"]
    assert isinstance(add_member_env, dict)
    assert add_member_env["WEVIBE_IDENTITY_SEED_HEX"] == leader.seed_hex
    assert add_member_env["WEVIBE_CHAIN_RPC"] == "http://localhost:26657"


def test_orchestrator_run_m1_reuses_existing_org_membership() -> None:
    logger, _ = _capture_logger("test.lifecycle.orchestrator.m1.reuse")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
        session_token_path="~/test-session-token",
        leader_signer_dir="/opt/leader-signer",
    )
    leader = Identity.from_hex("11" * 32)
    contributor = Identity.from_hex("22" * 32)

    calls: list[str] = []
    signer_calls: list[dict[str, Any]] = []

    class FakeAdminCli:
        def __init__(self, env: dict[str, str]) -> None:
            self._env = env

        def invite(
            self,
            org_id: str,
            invitee_pubkey: str,
            invitee_x25519: str,
            invitee_pre_pubkey: str,
            can_contribute: bool = True,
            can_moderate: bool = False,
        ) -> str:
            raise AssertionError("invite should be skipped when contributor is already a member")

        def provision_recall(self, org_id: str) -> str:
            calls.append("provision_recall")
            assert org_id == "org-123"
            return "provisioned"

    class FakeHubClient:
        def __init__(self) -> None:
            self._contributor_member_orgs_checks = 0

        def enable_recall(self, identity: Identity, org_id: str, member_pubkey: str, free: bool = True) -> Any:
            calls.append("enable_recall")
            assert identity is leader
            assert org_id == "org-123"
            assert member_pubkey == "ed-contrib"
            assert free is True
            return {"status": "ok"}

        def member_orgs(self, identity: Identity) -> Any:
            if identity is leader:
                return []
            assert identity is contributor
            self._contributor_member_orgs_checks += 1
            # run_m1 checks membership before invite/add-member, then checks again while polling.
            if self._contributor_member_orgs_checks == 1:
                return [{"org_id": "org-123"}]
            calls.append("poll_membership")
            return [{"org_id": "org-123"}]

    class FakeMcpRest:
        def identity_pubkeys(self) -> dict[str, str]:
            calls.append("contributor_pubkeys")
            return {"ed25519": "ed-contrib", "x25519": "x-contrib", "pre_pubkey": "pre-contrib"}

    class FakeProcman:
        pass

    def run_cmd(*args, **kwargs):
        cmd = args[0]
        signer_calls.append(
            {
                "cmd": cmd,
                "cwd": kwargs.get("cwd"),
                "env": kwargs.get("env"),
            }
        )
        if "register-org" in cmd:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout='register-org done\n{"org_id":"org-123","tx_hash":"tx-1","leader_wallet":"wallet-1","hub_serving_key":"hub-key"}\n',
                stderr="",
            )
        if "add-member" in cmd:
            raise AssertionError("add-member should be skipped when contributor is already a member")
        raise AssertionError(f"unexpected command {cmd}")

    orchestrator = LifecycleOrchestrator(
        cfg=cfg,
        wevibe_root="/workspace",
        leader=leader,
        contributor=contributor,
        leader_keystore="/tmp/leader.ks",
        contributor_keystore="/tmp/contrib.ks",
        leader_wallet="wallet-1",
        logger=logger,
        procman=FakeProcman(),
        admin_cli_factory=lambda env: FakeAdminCli(env),
        hub_client=FakeHubClient(),
        mcp_rest_factory=lambda _url: FakeMcpRest(),
        sleep_fn=lambda _seconds: None,
        run_cmd=run_cmd,
    )

    result = orchestrator.run_m1()

    assert result["org_id"] == "org-123"
    assert [step["step"] for step in result["steps"]] == [
        "create_org",
        "contributor_pubkeys",
        "enable_recall",
        "provision_recall",
        "poll_membership",
    ]
    assert calls == [
        "contributor_pubkeys",
        "enable_recall",
        "provision_recall",
        "poll_membership",
    ]

    assert len(signer_calls) == 1
    assert signer_calls[0]["cmd"] == [
        "node",
        "/opt/leader-signer/dist/cli.js",
        "register-org",
        "--org-name",
        "wevibe-bench-lifecycle",
        "--domain",
        "bench.wevibe.local",
    ]
    assert all("add-member" not in call["cmd"] for call in signer_calls)


def test_m2_proof_produce_memory_uses_direct_memory_without_extract() -> None:
    logger, stream = _capture_logger("test.lifecycle.m2.direct-memory")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("55" * 32)
    contributor = Identity.from_hex("66" * 32)

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

        def wait_extract(
            self,
            job_id: str,
            timeout_s: float = 30,
            interval_s: float = 0.5,
        ) -> dict[str, Any]:
            raise AssertionError("wait_extract must not run when direct memory is provided")

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
        direct_memory={"text": "hello world", "keywords": ["a", "b"], "stack_hint": "python"},
    )

    memory = proof.produce_memory(
        events=[{"kind": "user", "time": 1, "seq": 0, "text": "ignored"}],
        model="ignored",
        api_key="",
        project_context={"project": "ctx"},
        org_id="org-99",
    )

    assert mcp_factory_called is False
    assert memory == {
        "text": "hello world",
        "keywords": ["a", "b"],
        "stack_hint": "python",
    }
    logs = stream.getvalue()
    assert "op=lifecycle.m2.direct_memory" in logs
    assert "text_size=11" in logs
    assert "keyword_count=2" in logs


def test_m2_proof_build_classified_keywords_filters_invalid_and_sums_exactly_to_one() -> None:
    classified_five = M2Proof._build_classified_keywords(
        [
            "Redis",
            "PYTHON",
            "bad-key",
            "1invalid",
            "redis",
            "Go_lang",
            "node2",
            "ML_7",
            "",
        ]
    )

    assert [item["keyword"] for item in classified_five] == [
        "redis",
        "python",
        "go_lang",
        "node2",
        "ml_7",
    ]
    assert sum(item["weight"] for item in classified_five) == 1.0
    assert all(item["weight"] == item["base_weight"] for item in classified_five)

    classified_three = M2Proof._build_classified_keywords(["Alpha", "alpha", "bad keyword", "beta", "gamma"])
    assert [item["keyword"] for item in classified_three] == ["alpha", "beta", "gamma"]
    assert sum(item["weight"] for item in classified_three) == 1.0

    with pytest.raises(RuntimeError, match="at least one valid classified keyword"):
        M2Proof._build_classified_keywords(["", "1bad", "bad-key"])


def test_m2_proof_run_executes_verify_commit_hops_and_reports_delivery_yes() -> None:
    logger, _ = _capture_logger("test.lifecycle.m2.run")
    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    leader = Identity.from_hex("33" * 32)
    contributor = Identity.from_hex("44" * 32)

    calls: list[str] = []
    commit_batch_call: dict[str, Any] = {}

    class FakeContributorRest:
        def extract(self, events: list[dict[str, Any]], model: str, project_context: dict[str, Any] | None = None, org_id: str | None = None, **_kwargs: Any) -> str:
            calls.append("extract")
            assert events == [{"kind": "user", "time": 1700000000000, "seq": 0, "text": "task prompt"}]
            assert model == "model-a"
            assert org_id == "org-77"
            return "job-1"

        def wait_extract(self, job_id: str, timeout_s: float = 30, interval_s: float = 0.5) -> dict[str, Any]:
            calls.append("wait_extract")
            assert job_id == "job-1"
            return {
                "status": "completed",
                "result": {
                    "memories": [
                        {
                            "text": "expected delivery fragment in memory",
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
            calls.append("submit")
            assert org_id == "org-77"
            assert plaintext.startswith("expected delivery")
            assert stack_hint == ["python"]
            assert keywords == ["alpha"]
            return {"status": "ok", "submission_hash": "sub-1"}

    class FakeLeaderRest:
        def mod_embed_retrieval_card(self, items: list[dict[str, Any]], org_id: str | None = None) -> list[dict[str, Any]]:
            calls.append("mod_embed_retrieval_card")
            assert org_id == "org-77"
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

        def recall(self, query: str, org_id: str, **kw: Any) -> dict[str, Any]:
            calls.append("recall")
            assert org_id == "org-77"
            return {
                "status": "ok",
                "memories": [
                    {"text": f"... {query} ..."},
                ],
            }

    class FakeHubClient:
        def __init__(self) -> None:
            self._commit_status_calls = 0

        def moderation_queue(self, identity: Identity, org_id: str) -> Any:
            calls.append("moderation_queue")
            assert identity is leader
            assert org_id == "org-77"
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
            assert org_id == "org-77"
            assert submission_hash == "sub-1"
            assert classified
            assert sum(item["weight"] for item in classified) == 1.0
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
            calls.append("batch_submit")
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
            calls.append("commit_status")
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
        commit_batch_call["cmd"] = args[0]
        commit_batch_call["cwd"] = kwargs.get("cwd")
        commit_batch_call["env"] = kwargs.get("env")
        commit_batch_call["input"] = kwargs.get("input")
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
        events=[{"kind": "user", "time": 1700000000000, "seq": 0, "text": "task prompt"}],
        model="model-a",
        api_key="api-key",
        project_context={"project": "ctx"},
    )

    assert result["submission_hash"] == "sub-1"
    assert result["delivery"]["delivery"] == "YES"
    assert result["qdrant_delta"]["saw_plus_one"] is True

    expected_hops = [
        "commit_status",
        "moderation_queue",
        "mod_embed_retrieval_card",
        "submit_keyword_results",
        "verify_keywords",
        "batch_submit",
        "commit_status",
    ]
    observed_hops = [name for name in calls if name in set(expected_hops)]
    assert observed_hops == expected_hops

    expected_signer_dir = os.path.expanduser(cfg.leader_signer_dir)
    expected_signer_cli = os.path.join(expected_signer_dir, "dist", "cli.js")

    assert commit_batch_call["cmd"] == [
        "node",
        expected_signer_cli,
        "commit-batch",
        "--org-id",
        "org-77",
        "--producer-model-id",
        "model-a",
    ]
    assert commit_batch_call["cwd"] == expected_signer_dir
    env = commit_batch_call["env"]
    assert isinstance(env, dict)
    assert env["WEVIBE_IDENTITY_SEED_HEX"] == leader.seed_hex
    assert env["WEVIBE_CHAIN_RPC"] == "http://localhost:26657"
    batch_input = commit_batch_call["input"]
    assert isinstance(batch_input, str)
    parsed_input = json.loads(batch_input)
    assert isinstance(parsed_input, dict)
    assert isinstance(parsed_input.get("batch"), list)
    assert parsed_input["batch"]

