from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

from wevibe_bench.adapters.openrouter_proxy import (
    BudgetExceededError,
    BudgetLedger,
    CredentialError,
    ModelMismatchError,
    PolicyMismatchError,
    ProfileBlockedError,
    ProtectedFieldError,
    ProxyLogger,
    DEFAULT_PROFILES,
    apply_policy,
    input_token_upper_bound,
    key_fingerprint,
    load_openrouter_upstream_key,
    worst_case_usd,
)


def test_load_openrouter_upstream_key_returns_stripped_key(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"openrouter": {"type": "api", "key": "   sk-or-test-key   "}}),
        encoding="utf-8",
    )

    assert load_openrouter_upstream_key(str(auth_path)) == "sk-or-test-key"


def test_load_openrouter_upstream_key_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing-auth.json"

    with pytest.raises(CredentialError, match="opencode auth.json not found"):
        load_openrouter_upstream_key(str(missing))


def test_load_openrouter_upstream_key_rejects_missing_openrouter_object(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"other": {"key": "sk-or-test-key"}}), encoding="utf-8")

    with pytest.raises(CredentialError, match="missing object at key 'openrouter'"):
        load_openrouter_upstream_key(str(auth_path))


def test_load_openrouter_upstream_key_rejects_empty_key(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"openrouter": {"type": "api", "key": "    "}}),
        encoding="utf-8",
    )

    with pytest.raises(CredentialError, match="openrouter.key must be non-empty"):
        load_openrouter_upstream_key(str(auth_path))


def test_apply_policy_rejects_client_provider_override() -> None:
    glm = DEFAULT_PROFILES()["glm"]
    body = {
        "model": glm.model_id,
        "provider": {"order": ["other"]},
    }

    with pytest.raises(ProtectedFieldError) as excinfo:
        apply_policy(body, glm, max_tokens_cap=1024)

    assert excinfo.value.reason == "provider"


def test_apply_policy_injects_exact_glm_provider_object() -> None:
    glm = DEFAULT_PROFILES()["glm"]

    transformed = apply_policy(
        {
            "model": f"openrouter/{glm.model_id}",
            "messages": [{"role": "user", "content": "hello"}],
        },
        glm,
        max_tokens_cap=1024,
    )

    assert transformed["provider"] == {
        "order": ["novita"],
        "only": ["novita"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "quantizations": ["fp8"],
    }


def test_apply_policy_injects_exact_mimo_provider_without_quantizations() -> None:
    mimo = DEFAULT_PROFILES()["mimo"]

    transformed = apply_policy(
        {
            "model": mimo.model_id,
            "messages": [{"role": "user", "content": "hello"}],
        },
        mimo,
        max_tokens_cap=1024,
    )

    assert transformed["provider"] == {
        "order": ["deepinfra"],
        "only": ["deepinfra"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert "quantizations" not in transformed["provider"]


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
