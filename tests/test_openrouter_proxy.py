from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading
from typing import Any

import pytest

import wevibe_bench.adapters.openrouter_proxy_server as proxy_server
from wevibe_bench.adapters.openrouter_proxy import (
    BudgetExceededError,
    BudgetLedger,
    CHECKPOINT_SCHEMA_VERSION,
    CredentialError,
    OPENCODE_ZEN_UPSTREAM_URL,
    ModelMismatchError,
    ORCAROUTER_UPSTREAM_URL,
    OPENROUTER_UPSTREAM_URL,
    PricingGateError,
    PolicyMismatchError,
    ProfileBlockedError,
    ProtectedFieldError,
    ProxyLogger,
    UPSTREAM_CHAT_COMPLETIONS_URLS,
    DEFAULT_PROFILES,
    apply_policy,
    derived_cost_usd,
    fetch_orcarouter_pricing,
    input_token_upper_bound,
    key_fingerprint,
    load_upstream_key,
    verify_orcarouter_pricing_gate,
    worst_case_usd,
)


class _MainTestServer:
    def __init__(self) -> None:
        self.closed = False
        self.serve_calls = 0

    def serve_forever(self) -> None:
        self.serve_calls += 1
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


def _write_auth_json(tmp_path: Path, *, key: str = "sk-or-test-main") -> Path:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "openrouter": {"type": "api", "key": key},
                "orcarouter": {"type": "api", "key": key},
            }
        ),
        encoding="utf-8",
    )
    return auth_path


def _main_argv(tmp_path: Path, auth_path: Path) -> list[str]:
    return [
        "--run-id",
        "run-main-test",
        "--model",
        "openrouter/z-ai/glm-5.2",
        "--profile",
        "glm",
        "--cap-usd",
        "12",
        "--port",
        "8789",
        "--checkpoint",
        str(tmp_path / "main-ledger.json"),
        "--log",
        str(tmp_path / "main-proxy.log"),
        "--max-output-tokens",
        "256",
        "--token-file",
        str(tmp_path / "main-proxy.token"),
        "--auth-path",
        str(auth_path),
    ]


def _run_main_with_fake_server(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> Any:
    captured: dict[str, Any] = {}

    def _fake_make_server(proxy, host: str = "127.0.0.1", port: int = 0) -> _MainTestServer:
        server = _MainTestServer()
        captured["proxy"] = proxy
        captured["server"] = server
        captured["host"] = host
        captured["port"] = port
        return server

    monkeypatch.setattr(proxy_server, "make_server", _fake_make_server)

    assert proxy_server.main(argv) == 0

    server = captured["server"]
    assert isinstance(server, _MainTestServer)
    assert server.serve_calls == 1
    assert server.closed is True

    proxy = captured["proxy"]
    proxy.logger._handle.close()
    return proxy


def _valid_orcarouter_payload_with_model_row(
    *,
    model_name: str,
    model_ratio: float,
    completion_ratio: float,
    cache_ratio: float,
) -> dict[str, Any]:
    return {
        "pricing_version": "c58e194db3f6a20e7d41b8c9e2f05a17",
        "model_discounts": {},
        "workspace_discount": 1,
        "effective_group_ratio": 1,
        "group_ratio": {"default": 1, "vip": 1},
        "data": [
            {
                "model_name": "openai/gpt-4o-mini",
                "model_ratio": 0.075,
                "completion_ratio": 4.0,
            },
            {
                "model_name": model_name,
                "model_ratio": model_ratio,
                "completion_ratio": completion_ratio,
                "cache_ratio": cache_ratio,
            },
        ],
    }


def test_load_upstream_key_returns_stripped_openrouter_key(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"openrouter": {"type": "api", "key": "   sk-or-test-key   "}}),
        encoding="utf-8",
    )

    assert load_upstream_key("openrouter", str(auth_path)) == "sk-or-test-key"


def test_load_upstream_key_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing-auth.json"

    with pytest.raises(CredentialError, match="opencode auth.json not found"):
        load_upstream_key("openrouter", str(missing))


def test_load_upstream_key_rejects_missing_openrouter_object(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"other": {"key": "sk-or-test-key"}}), encoding="utf-8")

    with pytest.raises(CredentialError, match="missing object at key 'openrouter'"):
        load_upstream_key("openrouter", str(auth_path))


def test_load_upstream_key_rejects_empty_openrouter_key(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"openrouter": {"type": "api", "key": "    "}}),
        encoding="utf-8",
    )

    with pytest.raises(CredentialError, match="openrouter.key must be non-empty"):
        load_upstream_key("openrouter", str(auth_path))


def test_load_upstream_key_returns_stripped_opencode_key(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"opencode": {"type": "api", "key": "   sk-zen-test-key   "}}),
        encoding="utf-8",
    )

    assert load_upstream_key("opencode", str(auth_path)) == "sk-zen-test-key"


def test_load_upstream_key_rejects_missing_opencode_entry_with_connect_guidance(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"openrouter": {"type": "api", "key": "sk-or-test-key"}}),
        encoding="utf-8",
    )

    with pytest.raises(CredentialError, match=r"no 'opencode' entry") as excinfo:
        load_upstream_key("opencode", str(auth_path))

    assert "opencode /connect" in str(excinfo.value)


def test_upstream_chat_completion_url_map_is_canonical() -> None:
    assert OPENROUTER_UPSTREAM_URL == "https://openrouter.ai/api/v1/chat/completions"
    assert OPENCODE_ZEN_UPSTREAM_URL == "https://opencode.ai/zen/v1/chat/completions"
    assert ORCAROUTER_UPSTREAM_URL == "https://www.orcarouter.ai/v1/chat/completions"
    assert UPSTREAM_CHAT_COMPLETIONS_URLS == {
        "openrouter": OPENROUTER_UPSTREAM_URL,
        "opencode": OPENCODE_ZEN_UPSTREAM_URL,
        "orcarouter": ORCAROUTER_UPSTREAM_URL,
    }


def test_apply_policy_rejects_client_provider_override() -> None:
    glm = DEFAULT_PROFILES()["glm"]
    body = {
        "model": glm.model_id,
        "provider": {"order": ["other"]},
    }

    with pytest.raises(ProtectedFieldError) as excinfo:
        apply_policy(body, glm, max_tokens_cap=1024)

    assert excinfo.value.reason == "provider"


def test_apply_policy_for_orcarouter_profile_skips_provider_injection() -> None:
    glm = DEFAULT_PROFILES()["glm"]
    forwarded_selector = f"openrouter/{glm.model_id}"

    transformed = apply_policy(
        {
            "model": forwarded_selector,
            "messages": [{"role": "user", "content": "hello"}],
        },
        glm,
        max_tokens_cap=1024,
    )

    assert "provider" not in transformed
    assert transformed["model"] == forwarded_selector


def test_default_profiles_include_roster_candidates_with_constraints() -> None:
    profiles = DEFAULT_PROFILES()

    assert set(profiles) == {"glm", "mimo", "mimo25", "hy3", "kimicode", "ring", "opus", "bigpickle"}
    assert list(profiles.keys())[-1] == "bigpickle"
    assert profiles["glm"].model_id == "z-ai/glm-5.2"
    assert profiles["glm"].upstream == "orcarouter"
    assert profiles["mimo"].model_id == "xiaomi/mimo-v2.5-pro"
    assert profiles["mimo25"].model_id == "xiaomi/mimo-v2.5"
    assert profiles["hy3"].model_id == "tencent/hy3"
    assert profiles["hy3"].upstream == "orcarouter"
    assert profiles["kimicode"].model_id == "kimi/kimi-k2.7-code"
    assert profiles["kimicode"].upstream == "orcarouter"
    assert profiles["ring"].model_id == "inclusionai/ring-2.6-1t"
    assert profiles["opus"].model_id == "anthropic/claude-opus-4.8"
    assert profiles["bigpickle"].model_id == "opencode/big-pickle"
    assert profiles["bigpickle"].upstream == "opencode"
    assert profiles["bigpickle"].provider_object is None
    assert profiles["bigpickle"].pin_constraints is None
    assert profiles["bigpickle"].pricing is None
    assert profiles["bigpickle"].authorized is False
    assert profiles["bigpickle"].max_output_tokens == 8192
    assert profiles["bigpickle"].max_reasoning_tokens == 8192
    assert profiles["bigpickle"].runnable_reason() == "pricing_missing"

    expected_constraints = {
        "mimo": {
            "quant_preference": ["fp8"],
            "price_sanity_per_m": {"in": 0.435, "out": 0.87},
        },
        "mimo25": {
            "quant_preference": ["fp8"],
            "price_sanity_per_m": {"in": 0.105, "out": 0.28},
        },
        "ring": {
            "quant_preference": ["any"],
            "price_sanity_per_m": {"in": 0.075, "out": 0.625},
        },
    }

    for profile_name, expected in expected_constraints.items():
        profile = profiles[profile_name]
        assert profile.provider_object is None
        assert profile.pin_constraints is not None
        assert profile.pin_constraints["min_max_completion_tokens"] == 32768
        assert profile.pin_constraints["uptime_tier"] == "Normal"
        assert profile.pin_constraints["quant_preference"] == expected["quant_preference"]
        assert profile.pin_constraints["price_sanity_per_m"] == expected["price_sanity_per_m"]
        assert isinstance(profile.pin_constraints["notes"], str)
        assert profile.pin_constraints["notes"]

    assert profiles["glm"].pin_constraints is None
    assert profiles["hy3"].pin_constraints is None
    assert profiles["kimicode"].pin_constraints is None
    assert profiles["opus"].pin_constraints is None


def test_apply_policy_for_zen_profile_skips_provider_injection() -> None:
    bigpickle = DEFAULT_PROFILES()["bigpickle"]

    transformed = apply_policy(
        {
            "model": "opencode/big-pickle",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 999_999,
        },
        bigpickle,
        max_tokens_cap=2048,
    )

    assert "provider" not in transformed
    assert transformed["max_tokens"] == 2048


def test_apply_policy_rewrites_zen_model_to_bare_id() -> None:
    # Zen rejects the "opencode/<id>" selector form upstream (401 "not supported");
    # the upstream body must carry the bare Zen model id (verified 2026-07-21).
    bigpickle = DEFAULT_PROFILES()["bigpickle"]

    transformed = apply_policy(
        {
            "model": "opencode/big-pickle",
            "messages": [{"role": "user", "content": "hello"}],
        },
        bigpickle,
        max_tokens_cap=1024,
    )

    assert transformed["model"] == "big-pickle"


def test_apply_policy_keeps_openrouter_model_selector_unchanged() -> None:
    glm = DEFAULT_PROFILES()["glm"]

    transformed = apply_policy(
        {
            "model": glm.model_id,
            "messages": [{"role": "user", "content": "hello"}],
        },
        glm,
        max_tokens_cap=1024,
    )

    assert transformed["model"] == glm.model_id


def test_apply_policy_rejects_unpinned_mimo_profile_with_provider_pin_missing() -> None:
    mimo = DEFAULT_PROFILES()["mimo"]

    with pytest.raises(ProfileBlockedError) as excinfo:
        apply_policy(
            {
                "model": mimo.model_id,
                "messages": [{"role": "user", "content": "hello"}],
            },
            mimo,
            max_tokens_cap=1024,
        )

    assert excinfo.value.reason == "provider_pin_missing"


def test_opus_profile_is_blocked_for_missing_pricing() -> None:
    opus = DEFAULT_PROFILES()["opus"]
    assert opus.runnable_reason() == "pricing_missing"


def test_apply_policy_injects_exact_opus_provider_without_quantizations() -> None:
    opus = DEFAULT_PROFILES()["opus"]

    transformed = apply_policy(
        {
            "model": opus.model_id,
            "messages": [{"role": "user", "content": "hello"}],
        },
        opus,
        max_tokens_cap=1024,
    )

    assert transformed["provider"] == {
        "order": ["anthropic"],
        "only": ["anthropic"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert "quantizations" not in transformed["provider"]


def test_apply_policy_rejects_providerless_profile_with_provider_pin_missing() -> None:
    opus = DEFAULT_PROFILES()["opus"]
    providerless = replace(opus, provider_object=None)

    with pytest.raises(ProfileBlockedError) as excinfo:
        apply_policy({"model": providerless.model_id}, providerless, max_tokens_cap=512)

    assert excinfo.value.reason == "provider_pin_missing"


def test_apply_policy_hard_clamps_max_tokens_to_cap() -> None:
    glm = DEFAULT_PROFILES()["glm"]
    cap = 2048

    transformed_high = apply_policy(
        {"model": glm.model_id, "max_tokens": 999_999},
        glm,
        max_tokens_cap=cap,
    )
    transformed_missing = apply_policy(
        {"model": glm.model_id},
        glm,
        max_tokens_cap=cap,
    )
    transformed_low = apply_policy(
        {"model": glm.model_id, "max_tokens": 77},
        glm,
        max_tokens_cap=cap,
    )

    assert transformed_high["max_tokens"] == cap
    assert transformed_missing["max_tokens"] == cap
    assert transformed_low["max_tokens"] == 77


def test_apply_policy_rejects_model_mismatch_after_normalization() -> None:
    glm = DEFAULT_PROFILES()["glm"]

    with pytest.raises(ModelMismatchError):
        apply_policy(
            {"model": "openrouter/xiaomi/mimo-v2.5-pro"},
            glm,
            max_tokens_cap=1024,
        )


def test_input_token_upper_bound_is_utf8_safe_and_monotonic() -> None:
    multibyte = "é漢🙂"
    body_ascii = {
        "messages": [{"role": "user", "content": "a" * len(multibyte)}],
        "tools": [],
    }
    body_small = {
        "messages": [{"role": "user", "content": multibyte}],
        "tools": [],
    }
    body_large = {
        "messages": [{"role": "user", "content": multibyte * 8}],
        "tools": [],
    }

    ub_ascii = input_token_upper_bound(body_ascii)
    ub_small = input_token_upper_bound(body_small)
    ub_large = input_token_upper_bound(body_large)

    assert ub_small >= len(multibyte)
    assert ub_small >= len(multibyte.encode("utf-8"))
    assert ub_small > ub_ascii
    assert ub_large > ub_small


def test_worst_case_usd_uses_cache_write_and_output_plus_reasoning() -> None:
    glm = DEFAULT_PROFILES()["glm"]
    priced = replace(
        glm,
        pricing={
            "input": 1.0,
            "output": 4.0,
            "cache_read": 0.1,
            "cache_write": 2.5,
        },
        max_reasoning_tokens=512,
        authorized=True,
    )

    input_tokens_ub = 1234
    max_tokens_cap = 2048
    expected = (
        ((input_tokens_ub * 2.5) / 1_000_000)
        + (((max_tokens_cap + 512) * 4.0) / 1_000_000)
    ) * 1.06 * 1.10 + 0.001

    assert worst_case_usd(input_tokens_ub, priced, max_tokens_cap) == pytest.approx(expected)


def test_worst_case_usd_prices_cached_prefix_at_cache_read() -> None:
    glm = DEFAULT_PROFILES()["glm"]
    priced = replace(
        glm,
        pricing={
            "input": 1.0,
            "output": 4.0,
            "cache_read": 0.1,
            "cache_write": 2.5,
        },
        max_reasoning_tokens=512,
        authorized=True,
    )

    input_tokens_ub = 10_000
    cached = 6_000
    max_tokens_cap = 2048
    expected = (
        (((cached * 0.1) + ((input_tokens_ub - cached) * 2.5)) / 1_000_000)
        + (((max_tokens_cap + 512) * 4.0) / 1_000_000)
    ) * 1.06 * 1.10 + 0.001

    got = worst_case_usd(
        input_tokens_ub,
        priced,
        max_tokens_cap,
        cached_input_tokens_ub=cached,
    )
    assert got == pytest.approx(expected)
    assert got < worst_case_usd(input_tokens_ub, priced, max_tokens_cap)

    # Cached bound clamps to the request's own input UB.
    fully_cached_expected = (
        ((input_tokens_ub * 0.1) / 1_000_000)
        + (((max_tokens_cap + 512) * 4.0) / 1_000_000)
    ) * 1.06 * 1.10 + 0.001
    assert worst_case_usd(
        input_tokens_ub,
        priced,
        max_tokens_cap,
        cached_input_tokens_ub=10 * input_tokens_ub,
    ) == pytest.approx(fully_cached_expected)

    # Default (no cache history) is byte-identical to cached_input_tokens_ub=0.
    assert worst_case_usd(input_tokens_ub, priced, max_tokens_cap) == pytest.approx(
        worst_case_usd(input_tokens_ub, priced, max_tokens_cap, cached_input_tokens_ub=0)
    )

    # No cache-read pricing => the cached prefix earns no discount.
    no_cache_read = replace(
        priced,
        pricing={"input": 1.0, "output": 4.0, "cache_write": 2.5},
    )
    assert worst_case_usd(
        input_tokens_ub,
        no_cache_read,
        max_tokens_cap,
        cached_input_tokens_ub=cached,
    ) == pytest.approx(worst_case_usd(input_tokens_ub, no_cache_read, max_tokens_cap))


def test_cached_prefix_reservation_admits_feedback_round_at_19b_numbers(tmp_path: Path) -> None:
    """Regression for the opus48-smoke-19b feedback refusals (ordinals 39-41).

    Numbers verbatim from runs/openrouter-proxy/20260719T180215Z-opus48-smoke-19b.log:
    accrued=5.331406, committed_unproven=1.4157078, refused in_tokens_ub=569500
    at reserved_usd=5.32282805, prior proven-billed in_tokens_ub=566665
    (ordinal 38, status=200), hard cap 12, pricing 5/25/0.5/6.25,
    max_tokens_cap=32000, max_reasoning_tokens=8192.
    """
    opus = replace(
        DEFAULT_PROFILES()["opus"],
        pricing={
            "input": 5.0,
            "output": 25.0,
            "cache_read": 0.5,
            "cache_write": 6.25,
        },
        max_reasoning_tokens=8192,
        authorized=True,
    )
    max_tokens_cap = 32000
    in_tokens_ub = 569_500
    proven_billed_prefix = 566_665

    ledger = BudgetLedger(
        run_id="opus48-smoke-19b-shape",
        model_id="anthropic/claude-opus-4.8",
        profile_name="opus",
        hard_cap_usd=12.0,
        checkpoint_path=str(tmp_path / "ledger-19b-shape.json"),
    )
    ledger.reserve("history", 5.331406)
    ledger.settle_actual("history", 5.331406)
    ledger.reserve("aux-404", 1.4157078)
    ledger.retain_unproven("aux-404")

    # Without cache history the reservation stays conservative and still refuses.
    conservative = worst_case_usd(in_tokens_ub, opus, max_tokens_cap)
    assert conservative == pytest.approx(5.32282805, abs=1e-6)
    with pytest.raises(BudgetExceededError):
        ledger.reserve("feedback-conservative", conservative)

    # With the proven-billed prefix priced at cache-read, the round is admitted.
    cached = worst_case_usd(
        in_tokens_ub,
        opus,
        max_tokens_cap,
        cached_input_tokens_ub=proven_billed_prefix,
    )
    assert cached < conservative
    ledger.reserve("feedback-cached", cached)
    ledger.settle_actual("feedback-cached", cached)

    # The accrued hard-cap backstop is untouched: once actual spend has grown,
    # even a conservative-shaped ask is still refused at the same $12 ceiling.
    with pytest.raises(BudgetExceededError):
        ledger.reserve("post-feedback-conservative", conservative)


def test_budget_ledger_boundary_and_equality_policy(tmp_path: Path) -> None:
    checkpoint = tmp_path / "ledger-boundary.json"
    ledger = BudgetLedger(
        run_id="run-boundary",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=1.0,
        checkpoint_path=str(checkpoint),
    )

    ledger.reserve("under", 0.99)
    snapshot_before_refusal = ledger.snapshot()

    with pytest.raises(BudgetExceededError):
        ledger.reserve("over", 0.02)

    assert ledger.snapshot() == snapshot_before_refusal

    ledger.reserve("equal", 0.01)
    assert ledger.remaining() == pytest.approx(0.0)

    strict = BudgetLedger(
        run_id="run-boundary-strict",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=1.0,
        checkpoint_path=str(tmp_path / "ledger-boundary-strict.json"),
        reject_on_equality=True,
    )
    strict.reserve("under", 0.99)
    with pytest.raises(BudgetExceededError):
        strict.reserve("equal-refused", 0.01)


def test_budget_ledger_watch_only_never_refuses(tmp_path: Path) -> None:
    """R2 Amendment 1 (Walter 2026-07-25): watch_only downgrades the hard cap
    to a watch-threshold — accounting runs unchanged, reserve() never refuses."""
    ledger = BudgetLedger(
        run_id="run-watch",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=1.0,
        checkpoint_path=str(tmp_path / "ledger-watch.json"),
        watch_only=True,
    )
    ledger.reserve("under", 0.99)
    ledger.settle_derived("under", 0.99)
    # Far beyond the cap: admitted and fully accounted, never refused.
    ledger.reserve("over-1", 5.0)
    ledger.settle_derived("over-1", 5.0)
    ledger.reserve("over-2", 50.0)
    ledger.settle_derived("over-2", 50.0)
    snapshot = ledger.snapshot()
    assert snapshot["accrued_derived"] == pytest.approx(55.99)
    assert snapshot["hard_cap"] == pytest.approx(1.0)


def test_budget_ledger_sequential_retries_and_settle_vs_retain(tmp_path: Path) -> None:
    ledger = BudgetLedger(
        run_id="run-seq",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=1.0,
        checkpoint_path=str(tmp_path / "ledger-seq.json"),
    )

    granted: list[str] = []
    for idx in range(10):
        req_id = f"seq-{idx}"
        try:
            ledger.reserve(req_id, 0.2)
            granted.append(req_id)
        except BudgetExceededError:
            break

    assert len(granted) == 5
    with pytest.raises(BudgetExceededError):
        ledger.reserve("seq-over", 0.2)

    assert ledger.remaining() == pytest.approx(0.0)

    ledger.settle_actual(granted[0], 0.1)
    assert ledger.remaining() == pytest.approx(0.1)

    remaining_before_retain = ledger.remaining()
    ledger.retain_unproven(granted[1])
    assert ledger.remaining() == pytest.approx(remaining_before_retain)


def test_budget_ledger_threaded_burst_never_exceeds_cap(tmp_path: Path) -> None:
    ledger = BudgetLedger(
        run_id="run-threaded",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=1.0,
        checkpoint_path=str(tmp_path / "ledger-threaded.json"),
    )

    amount = 0.125
    granted: list[str] = []
    refused: list[str] = []
    errors: list[Exception] = []
    gate = threading.Lock()

    def _reserve(req_id: str) -> None:
        try:
            ledger.reserve(req_id, amount)
            with gate:
                granted.append(req_id)
        except BudgetExceededError:
            with gate:
                refused.append(req_id)
        except Exception as exc:  # noqa: BLE001 - explicit failure surfaced below.
            with gate:
                errors.append(exc)

    threads = [threading.Thread(target=_reserve, args=(f"req-{idx}",)) for idx in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(granted) + len(refused) == 40
    assert refused, "burst should eventually refuse once the cap is full"

    snapshot = ledger.snapshot()
    assert snapshot["outstanding_total"] == pytest.approx(len(granted) * amount)
    total_committed = snapshot["accrued"] + snapshot["committed_unproven"] + snapshot["outstanding_total"]
    assert total_committed <= snapshot["hard_cap"] + 1e-12


def test_budget_ledger_init_persists_zero_checkpoint_and_round_trip(tmp_path: Path) -> None:
    checkpoint = tmp_path / "ledger-init.json"
    assert not checkpoint.exists()

    BudgetLedger(
        run_id="run-init",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=0.5,
        checkpoint_path=str(checkpoint),
    )

    assert checkpoint.is_file()
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert payload["run_id"] == "run-init"
    assert payload["model_id"] == "z-ai/glm-5.2"
    assert payload["profile_name"] == "glm"
    assert payload["hard_cap_usd"] == pytest.approx(0.5)
    assert payload["accrued_actual_usd"] == pytest.approx(0.0)
    assert payload["committed_unproven_usd"] == pytest.approx(0.0)
    assert payload["outstanding"] == {}
    assert isinstance(payload["updated_at"], str)
    assert payload["updated_at"]

    resumed = BudgetLedger(
        run_id="run-init",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=0.5,
        checkpoint_path=str(checkpoint),
    )

    snapshot = resumed.snapshot()
    assert snapshot["accrued"] == pytest.approx(0.0)
    assert snapshot["committed_unproven"] == pytest.approx(0.0)
    assert snapshot["outstanding_total"] == pytest.approx(0.0)
    assert snapshot["remaining"] == pytest.approx(0.5)
    assert snapshot["hard_cap"] == pytest.approx(0.5)


def test_budget_ledger_checkpoint_resume_restores_spend(tmp_path: Path) -> None:
    checkpoint = tmp_path / "ledger-resume.json"

    ledger = BudgetLedger(
        run_id="run-resume",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=0.5,
        checkpoint_path=str(checkpoint),
    )
    ledger.reserve("settled", 0.30)
    ledger.settle_actual("settled", 0.25)
    ledger.reserve("retained", 0.20)
    ledger.retain_unproven("retained")

    resumed = BudgetLedger(
        run_id="run-resume",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=0.5,
        checkpoint_path=str(checkpoint),
    )

    snapshot = resumed.snapshot()
    assert snapshot["accrued"] == pytest.approx(0.25)
    assert snapshot["committed_unproven"] == pytest.approx(0.20)
    assert snapshot["remaining"] == pytest.approx(0.05)

    with pytest.raises(BudgetExceededError):
        resumed.reserve("would-overrun", 0.051)


@pytest.mark.parametrize(
    ("run_id", "model_id", "profile_name", "hard_cap_usd"),
    [
        ("run-other", "z-ai/glm-5.2", "glm", 1.0),
        ("run-policy", "different/model", "glm", 1.0),
        ("run-policy", "z-ai/glm-5.2", "mimo", 1.0),
        ("run-policy", "z-ai/glm-5.2", "glm", 1.1),
    ],
)
def test_budget_ledger_resume_rejects_policy_mismatch(
    tmp_path: Path,
    run_id: str,
    model_id: str,
    profile_name: str,
    hard_cap_usd: float,
) -> None:
    checkpoint = tmp_path / "ledger-policy-mismatch.json"
    baseline = BudgetLedger(
        run_id="run-policy",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=1.0,
        checkpoint_path=str(checkpoint),
    )
    baseline.reserve("seed", 0.1)

    with pytest.raises(PolicyMismatchError):
        BudgetLedger(
            run_id=run_id,
            model_id=model_id,
            profile_name=profile_name,
            hard_cap_usd=hard_cap_usd,
            checkpoint_path=str(checkpoint),
        )


def test_proxy_logger_excludes_secrets_prompts_and_rejects_forbidden_fields(tmp_path: Path) -> None:
    logfile = tmp_path / "proxy.log"
    logger = ProxyLogger(str(logfile))

    prompt_text = "NEVER-LOG-THIS-PROMPT"
    raw_upstream_key = "sk-live-real-openrouter-key"
    run_token = "ephemeral-proxy-token"

    upstream_fp = key_fingerprint(raw_upstream_key)
    token_fp = key_fingerprint(run_token)

    logger.event(
        trace_id="trace-1",
        ordinal=1,
        model="z-ai/glm-5.2",
        provider_slugs=["novita"],
        in_tokens_ub=123,
        reserved_usd=0.01,
        status="ok",
        duration_ms=12,
        upstream_key_fp=upstream_fp,
        token_fp=token_fp,
    )

    text = logfile.read_text(encoding="utf-8")
    assert upstream_fp in text
    assert token_fp in text
    assert prompt_text not in text
    assert raw_upstream_key not in text

    forbidden = ("messages", "body", "prompt", "response", "authorization", "api_key", "key")
    for field in forbidden:
        with pytest.raises(ValueError):
            logger.event(**{field: prompt_text})

    logger._handle.close()


def test_bigpickle_profile_pins_expected_upstream_identity() -> None:
    profiles = DEFAULT_PROFILES()
    # bigpickle + orcarouter profiles pin upstream identity; only bigpickle also
    # pins upstream key fingerprint.
    assert profiles["bigpickle"].expected_upstream_model == "big-pickle"
    assert profiles["bigpickle"].expected_upstream_key_fp == "b5ce6e5e"
    assert profiles["glm"].expected_upstream_model == "glm-5.2"
    assert profiles["hy3"].expected_upstream_model == "hy3-preview"
    assert profiles["kimicode"].expected_upstream_model == "kimi-k2.7-code"
    for name in ("glm", "hy3", "kimicode"):
        assert profiles[name].expected_upstream_key_fp is None


def test_derived_cost_usd_exact_value_cases() -> None:
    assert derived_cost_usd(
        prompt_tokens=16,
        cached_tokens=0,
        completion_tokens=8,
        pricing={"input": 0.18, "output": 0.59, "cache_read": 0.059},
    ) == pytest.approx(7.6e-6)

    assert derived_cost_usd(
        prompt_tokens=1000,
        cached_tokens=400,
        completion_tokens=100,
        pricing={"input": 1.4, "output": 4.4, "cache_read": 0.26},
    ) == pytest.approx(1384e-6)

    assert derived_cost_usd(
        prompt_tokens=10,
        cached_tokens=0,
        completion_tokens=50,
        pricing={"input": 0.95, "output": 4.0, "cache_read": 0.19},
    ) == pytest.approx(209.5e-6)

    assert derived_cost_usd(
        prompt_tokens=10,
        cached_tokens=10,
        completion_tokens=0,
        pricing={"input": 0.18, "output": 0.59, "cache_read": 0.059},
    ) == pytest.approx(0.59e-6)

    assert derived_cost_usd(
        prompt_tokens=0,
        cached_tokens=0,
        completion_tokens=0,
        pricing={"input": 0.18, "output": 0.59, "cache_read": 0.059},
    ) == pytest.approx(0.0)

    assert derived_cost_usd(
        prompt_tokens=10,
        cached_tokens=4,
        completion_tokens=0,
        pricing={"input": 2.0, "output": 3.0},
    ) == pytest.approx(20e-6)


def test_derived_cost_usd_prices_cached_prompt_tokens_at_cache_read_rate() -> None:
    got = derived_cost_usd(
        prompt_tokens=2_000_000,
        cached_tokens=1_000_000,
        completion_tokens=1_000_000,
        pricing={"input": 0.18, "output": 0.59, "cache_read": 0.059},
    )

    assert got == pytest.approx(0.829)


def test_derived_cost_usd_zero_cached_tokens_matches_legacy_input_output_formula() -> None:
    got = derived_cost_usd(
        prompt_tokens=1_000_000,
        cached_tokens=0,
        completion_tokens=1_000_000,
        pricing={"input": 0.18, "output": 0.59, "cache_read": 0.059},
    )

    assert got == pytest.approx(0.77)


@pytest.mark.parametrize("pricing", [{}, None])
def test_derived_cost_usd_rejects_missing_pricing(pricing: dict | None) -> None:
    with pytest.raises(ValueError):
        derived_cost_usd(
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            pricing=pricing,
        )


def test_derived_cost_usd_missing_output_key_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        derived_cost_usd(
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            pricing={"input": 1.0},
        )


def _valid_orcarouter_payload() -> dict:
    return {
        "pricing_version": "c58e194db3f6a20e7d41b8c9e2f05a17",
        "model_discounts": {},
        "workspace_discount": 1,
        "effective_group_ratio": 1,
        "group_ratio": {"default": 1, "vip": 1},
        "data": [
            {
                "model_name": "openai/gpt-4o-mini",
                "model_ratio": 0.075,
                "completion_ratio": 4.0,
            }
        ],
    }


def test_verify_orcarouter_pricing_gate_accepts_minimal_valid_payload() -> None:
    verify_orcarouter_pricing_gate(_valid_orcarouter_payload())


def test_verify_orcarouter_pricing_gate_verifies_expected_model_pricing_including_cache_read() -> None:
    payload = _valid_orcarouter_payload_with_model_row(
        model_name="z-ai/glm-5.2",
        model_ratio=0.5,
        completion_ratio=3.0,
        cache_ratio=0.2,
    )

    verify_orcarouter_pricing_gate(
        payload,
        profile_model="z-ai/glm-5.2",
        expected_pricing={"input": 1.0, "output": 3.0, "cache_read": 0.2},
    )


def test_verify_orcarouter_pricing_gate_rejects_cache_read_mismatch() -> None:
    payload = _valid_orcarouter_payload_with_model_row(
        model_name="z-ai/glm-5.2",
        model_ratio=0.5,
        completion_ratio=3.0,
        cache_ratio=0.21,
    )

    with pytest.raises(PricingGateError, match="cache_read mismatch"):
        verify_orcarouter_pricing_gate(
            payload,
            profile_model="z-ai/glm-5.2",
            expected_pricing={"input": 1.0, "output": 3.0, "cache_read": 0.2},
        )


def test_main_authorize_requires_cache_read_pricing_flag(tmp_path: Path) -> None:
    auth_path = _write_auth_json(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        proxy_server.main(
            _main_argv(tmp_path, auth_path)
            + [
                "--authorize",
                "--pricing-input",
                "1.0",
                "--pricing-output",
                "3.0",
            ]
        )

    assert excinfo.value.code == 2


def test_main_authorize_pricing_gate_rejects_wrong_cache_read_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth_path = _write_auth_json(tmp_path)
    argv = _main_argv(tmp_path, auth_path) + [
        "--authorize",
        "--pricing-input",
        "1.0",
        "--pricing-output",
        "3.0",
        "--pricing-cache-read",
        "0.2",
    ]

    make_server_calls = 0

    def _fake_make_server(_proxy, host: str = "127.0.0.1", port: int = 0) -> _MainTestServer:
        nonlocal make_server_calls
        make_server_calls += 1
        return _MainTestServer()

    monkeypatch.setattr(proxy_server, "make_server", _fake_make_server)
    monkeypatch.setattr(
        proxy_server,
        "fetch_orcarouter_pricing",
        lambda: _valid_orcarouter_payload_with_model_row(
            model_name="z-ai/glm-5.2",
            model_ratio=0.5,
            completion_ratio=3.0,
            cache_ratio=0.21,
        ),
    )

    assert proxy_server.main(argv) == 2
    assert make_server_calls == 0
    assert "cache_read mismatch" in capsys.readouterr().err


def test_main_authorize_pricing_gate_accepts_matching_cache_read_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        proxy_server,
        "fetch_orcarouter_pricing",
        lambda: _valid_orcarouter_payload_with_model_row(
            model_name="z-ai/glm-5.2",
            model_ratio=0.5,
            completion_ratio=3.0,
            cache_ratio=0.2,
        ),
    )

    auth_path = _write_auth_json(tmp_path)
    proxy = _run_main_with_fake_server(
        monkeypatch,
        _main_argv(tmp_path, auth_path)
        + [
            "--authorize",
            "--pricing-input",
            "1.0",
            "--pricing-output",
            "3.0",
            "--pricing-cache-read",
            "0.2",
        ],
    )

    selected_profile = proxy.registry.get("glm")
    assert selected_profile.authorized is True
    assert selected_profile.pricing == {
        "input": pytest.approx(1.0),
        "output": pytest.approx(3.0),
        "cache_read": pytest.approx(0.2),
    }


@pytest.mark.parametrize("group_ratio", [1, 1.0])
def test_verify_orcarouter_pricing_gate_accepts_neutral_scalar_group_ratio(group_ratio: float) -> None:
    payload = _valid_orcarouter_payload()
    payload["group_ratio"] = group_ratio

    verify_orcarouter_pricing_gate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"pricing_version": "wrong"}),
        lambda payload: payload.update({"model_discounts": {"openai/gpt-4o-mini": 0.5}}),
        lambda payload: payload.update({"workspace_discount": 0.9}),
        lambda payload: payload.update({"effective_group_ratio": 2}),
        lambda payload: payload.update({"group_ratio": 2}),
        lambda payload: payload.update({"group_ratio": {"default": 1, "vip": 2}}),
        lambda payload: payload.update({"group_ratio": {"default": "high"}}),
        lambda payload: payload.update({"group_ratio": "high"}),
        lambda payload: payload.update({"data": {}}),
        lambda payload: payload.update({"data": []}),
        lambda payload: payload["data"][0].update({"model_ratio": "cheap"}),
        lambda payload: payload["data"][0].update({"model_ratio": 0.08}),
        lambda payload: payload["data"][0].update({"completion_ratio": 5.0}),
    ],
)
def test_verify_orcarouter_pricing_gate_fail_closed_cases(mutate) -> None:
    payload = _valid_orcarouter_payload()
    mutate(payload)

    with pytest.raises(PricingGateError):
        verify_orcarouter_pricing_gate(payload)


def test_fetch_orcarouter_pricing_returns_dict_on_http_200_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{"pricing_version": "x"}'

    def _fake_urlopen(*args, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(
        "wevibe_bench.adapters.openrouter_proxy.urllib_request.urlopen",
        _fake_urlopen,
    )

    assert fetch_orcarouter_pricing(timeout=0.1) == {"pricing_version": "x"}


def test_fetch_orcarouter_pricing_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def _fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("https://x", 500, "err", {}, None)

    monkeypatch.setattr(
        "wevibe_bench.adapters.openrouter_proxy.urllib_request.urlopen",
        _fake_urlopen,
    )

    with pytest.raises(PricingGateError):
        fetch_orcarouter_pricing(timeout=0.1)


def test_fetch_orcarouter_pricing_rejects_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b"not json"

    monkeypatch.setattr(
        "wevibe_bench.adapters.openrouter_proxy.urllib_request.urlopen",
        lambda *args, **kwargs: _FakeResponse(),
    )

    with pytest.raises(PricingGateError):
        fetch_orcarouter_pricing(timeout=0.1)


def test_fetch_orcarouter_pricing_rejects_non_200_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        status = 503

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{"pricing_version": "x"}'

    monkeypatch.setattr(
        "wevibe_bench.adapters.openrouter_proxy.urllib_request.urlopen",
        lambda *args, **kwargs: _FakeResponse(),
    )

    with pytest.raises(PricingGateError):
        fetch_orcarouter_pricing(timeout=0.1)


def test_fetch_orcarouter_pricing_rejects_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b"[1]"

    monkeypatch.setattr(
        "wevibe_bench.adapters.openrouter_proxy.urllib_request.urlopen",
        lambda *args, **kwargs: _FakeResponse(),
    )

    with pytest.raises(PricingGateError):
        fetch_orcarouter_pricing(timeout=0.1)


def test_budget_ledger_settle_derived_releases_reservation_and_accrues_derived(
    tmp_path: Path,
) -> None:
    hard_cap = 0.10
    checkpoint = tmp_path / "ledger-derived.json"
    ledger = BudgetLedger(
        run_id="run-derived",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=hard_cap,
        checkpoint_path=str(checkpoint),
    )

    ledger.reserve("r1", 0.05)
    ledger.settle_derived("r1", 0.001)

    snapshot = ledger.snapshot()
    assert snapshot["accrued_derived"] == pytest.approx(0.001)
    assert snapshot["outstanding_total"] == pytest.approx(0.0)
    assert ledger.remaining() == pytest.approx(hard_cap - 0.001)


def test_budget_ledger_settle_derived_negative_clamps_to_zero_and_releases(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "ledger-derived-negative.json"
    ledger = BudgetLedger(
        run_id="run-derived-negative",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=0.10,
        checkpoint_path=str(checkpoint),
    )

    ledger.reserve("r1", 0.05)
    ledger.settle_derived("r1", -5.0)

    snapshot = ledger.snapshot()
    assert snapshot["accrued_derived"] == pytest.approx(0.0)
    assert snapshot["outstanding_total"] == pytest.approx(0.0)
    assert ledger.remaining() == pytest.approx(0.10)


def test_budget_ledger_checkpoint_round_trip_persists_derived_accrual(tmp_path: Path) -> None:
    checkpoint = tmp_path / "ledger-derived-round-trip.json"
    ledger = BudgetLedger(
        run_id="run-derived-round-trip",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=0.10,
        checkpoint_path=str(checkpoint),
    )

    ledger.reserve("r1", 0.05)
    ledger.settle_derived("r1", 0.001)

    resumed = BudgetLedger(
        run_id="run-derived-round-trip",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=0.10,
        checkpoint_path=str(checkpoint),
    )
    assert resumed.snapshot()["accrued_derived"] == pytest.approx(0.001)


def test_budget_ledger_v1_checkpoint_loads_with_zero_derived(tmp_path: Path) -> None:
    checkpoint = tmp_path / "ledger-v1.json"
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-v1",
                "model_id": "z-ai/glm-5.2",
                "profile_name": "glm",
                "hard_cap_usd": 0.5,
                "accrued_actual_usd": 0.25,
                "committed_unproven_usd": 0.20,
                "outstanding": {},
                "updated_at": "2026-07-22T00:00:00+00:00",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    resumed = BudgetLedger(
        run_id="run-v1",
        model_id="z-ai/glm-5.2",
        profile_name="glm",
        hard_cap_usd=0.5,
        checkpoint_path=str(checkpoint),
    )
    assert resumed.snapshot()["accrued_derived"] == pytest.approx(0.0)
