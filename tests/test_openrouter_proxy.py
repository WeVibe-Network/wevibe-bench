from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

from wevibe_bench.adapters.openrouter_proxy import (
    BudgetExceededError,
    BudgetLedger,
    CHECKPOINT_SCHEMA_VERSION,
    CredentialError,
    OPENCODE_ZEN_UPSTREAM_URL,
    ModelMismatchError,
    ORCAROUTER_UPSTREAM_URL,
    OPENROUTER_UPSTREAM_URL,
    PolicyMismatchError,
    ProfileBlockedError,
    ProtectedFieldError,
    ProxyLogger,
    UPSTREAM_CHAT_COMPLETIONS_URLS,
    DEFAULT_PROFILES,
    apply_policy,
    input_token_upper_bound,
    key_fingerprint,
    load_upstream_key,
    worst_case_usd,
)


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
