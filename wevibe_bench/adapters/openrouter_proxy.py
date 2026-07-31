"""OpenRouter proxy policy/ledger primitives.

This module is pure logic (no transport). It enforces one-path policy shaping
(R-13) and pre-alpha observability guardrails (R-37).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import datetime
import hashlib
import json
import os
import threading
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


OPENROUTER_UPSTREAM_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENCODE_ZEN_UPSTREAM_URL = "https://opencode.ai/zen/v1/chat/completions"
ORCAROUTER_UPSTREAM_URL = "https://www.orcarouter.ai/v1/chat/completions"
ORCAROUTER_PRICING_URL = "https://www.orcarouter.ai/api/pricing"
ORCAROUTER_PRICING_VERSION_PIN = "c58e194db3f6a20e7d41b8c9e2f05a17"
ORCAROUTER_ANCHOR_MODEL = "openai/gpt-4o-mini"
ORCAROUTER_ANCHOR_INPUT_USD_PER_1M = 0.15
ORCAROUTER_ANCHOR_OUTPUT_USD_PER_1M = 0.60
PRICING_ANCHOR_ABS_TOL_USD = 1e-6
UPSTREAM_CHAT_COMPLETIONS_URLS = {
    "openrouter": OPENROUTER_UPSTREAM_URL,
    "opencode": OPENCODE_ZEN_UPSTREAM_URL,
    "orcarouter": ORCAROUTER_UPSTREAM_URL,
}
DEFAULT_OPENCODE_AUTH_PATH = "~/.local/share/opencode/auth.json"
PROTECTED_BODY_FIELDS = ("provider",)
ABSOLUTE_MAX_USD = 12.0
RESERVATION_SAFETY_FACTOR = 1.10
FEE_RATE = 0.06
FLAT_FEE_USD = 0.001
PER_MESSAGE_OVERHEAD_TOKENS = 16
CHECKPOINT_SCHEMA_VERSION = 2


class ProxyError(Exception):
    """Base proxy error carrying a machine-readable reason."""

    default_reason = "proxy_error"

    def __init__(self, message: str | None = None, *, reason: str | None = None) -> None:
        self.reason = reason if reason is not None else self.default_reason
        super().__init__(message if message is not None else self.reason)


class CredentialError(ProxyError):
    """Raised when canonical OpenCode credential loading fails."""

    default_reason = "credential_error"


class UnknownModelError(ProxyError):
    """Raised when a model selector cannot be resolved to a known profile."""

    default_reason = "unknown_model"


class ProfileBlockedError(ProxyError):
    """Raised when a profile is intentionally blocked from forwarding."""

    default_reason = "profile_blocked"

    def __init__(self, reason: str = "profile_blocked", message: str | None = None) -> None:
        super().__init__(message=message, reason=reason)


class ProtectedFieldError(ProxyError):
    """Raised when client input attempts to set a protected policy field."""

    default_reason = "protected_field"


class ModelMismatchError(ProxyError):
    """Raised when request model does not match the selected profile model."""

    default_reason = "model_mismatch"


class BudgetExceededError(ProxyError):
    """Raised when an additional reservation would exceed the hard budget ceiling."""

    default_reason = "budget_exceeded"


class PolicyMismatchError(ProxyError):
    """Raised when checkpoint policy binding mismatches the current run binding."""

    default_reason = "policy_mismatch"


class PricingGateError(RuntimeError):
    """Raised when live OrcaRouter pricing cannot be fetched/verified."""


@dataclass(frozen=True)
class ProviderProfile:
    """Provider-routing + pricing profile for a single benchmark model path."""

    name: str
    model_id: str
    provider_object: dict[str, Any] | None
    pricing: dict[str, float] | None
    max_output_tokens: int
    max_reasoning_tokens: int
    upstream: str = "openrouter"
    authorized: bool = False
    pin_constraints: dict[str, Any] | None = None
    # When set, every upstream response's reported ``model`` must equal this id;
    # the proxy server trips a one-way per-run refusal switch on any mismatch
    # (identity check for stealth/alias models, Walter 21-07-26).
    expected_upstream_model: str | None = None
    # When set, proxy startup fails fast if the loaded upstream credential
    # fingerprint (sha256 first-8-hex) differs (credential-rotation trip,
    # Walter 22-07-26).
    expected_upstream_key_fp: str | None = None

    def runnable_reason(self) -> str | None:
        """Return blocking reason or ``None`` if this profile is runnable."""
        if self.upstream == "openrouter" and self.provider_object is None:
            return "provider_pin_missing"
        if self.pricing is None:
            return "pricing_missing"
        if not self.authorized:
            return "not_authorized"
        return None


def normalize_model_selector(sel: str) -> str:
    """Normalize OpenRouter model selectors by stripping one ``openrouter/`` prefix."""
    if sel.startswith("openrouter/"):
        return sel[len("openrouter/") :]
    return sel


def load_upstream_key(provider_id: str, auth_path: str = DEFAULT_OPENCODE_AUTH_PATH) -> str:
    """Load upstream key from one canonical OpenCode auth source (no fallback, R-13)."""

    provider = str(provider_id).strip()
    if not provider:
        raise CredentialError("upstream provider id must be non-empty")

    expanded_path = os.path.expanduser(auth_path)
    if not os.path.exists(expanded_path):
        raise CredentialError(f"opencode auth.json not found: {auth_path}")

    try:
        with open(expanded_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialError(f"failed to load opencode auth.json: {auth_path}") from exc

    if not isinstance(data, dict):
        raise CredentialError(f"invalid opencode auth.json object: {auth_path}")

    provider_entry = data.get(provider)
    if not isinstance(provider_entry, dict):
        if provider == "opencode":
            raise CredentialError(
                f"no 'opencode' entry in {auth_path} — run `opencode /connect` → OpenCode Zen"
            )
        raise CredentialError(f"opencode auth.json missing object at key '{provider}': {auth_path}")

    key = provider_entry.get("key")
    if not isinstance(key, str) or not key.strip():
        raise CredentialError(f"opencode auth.json {provider}.key must be non-empty: {auth_path}")

    return key.strip()


def DEFAULT_PROFILES() -> dict[str, ProviderProfile]:
    """Return the binding default profile map for benchmark roster profiles."""
    return {
        "glm": ProviderProfile(
            name="glm",
            model_id="z-ai/glm-5.2",
            provider_object=None,
            pricing=None,
            # R2 Amendment 1 (Walter 2026-07-25): 8192 -> 32768. The 8192/8192
            # pairing let reasoning consume the entire completion budget
            # (hurdle #2 clamp guillotine fired live on the first GLM worker
            # cell: 3/3 attempts finish=length, <=2 visible tokens, zero work).
            # 32768 with reasoning capped at 8192 guarantees >=24k visible.
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            upstream="orcarouter",
            authorized=False,
            expected_upstream_model="glm-5.2",
        ),
        "mimo": ProviderProfile(
            name="mimo",
            model_id="xiaomi/mimo-v2.5-pro",
            provider_object=None,
            pricing=None,
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            authorized=False,
            pin_constraints={
                "min_max_completion_tokens": 32768,
                "uptime_tier": "Normal",
                "quant_preference": ["fp8"],
                "price_sanity_per_m": {"in": 0.435, "out": 0.87},
                "notes": "Resolve live; prefer xiaomi or atlascloud fp8 endpoints.",
            },
        ),
        "mimo25": ProviderProfile(
            name="mimo25",
            model_id="xiaomi/mimo-v2.5",
            provider_object=None,
            pricing=None,
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            authorized=False,
            pin_constraints={
                "min_max_completion_tokens": 32768,
                "uptime_tier": "Normal",
                "quant_preference": ["fp8"],
                "price_sanity_per_m": {"in": 0.105, "out": 0.28},
                "notes": "Resolve live; prefer xiaomi or atlascloud fp8 endpoints.",
            },
        ),
        "hy3": ProviderProfile(
            name="hy3",
            model_id="tencent/hy3",
            provider_object=None,
            pricing=None,
            # R2 Amendment 1 audit (Walter 2026-07-25): the same structural
            # hazard as glm exists (8192 output == 8192 reasoning = zero
            # guaranteed visible headroom); hy3 merely never tripped it in
            # prior worker runs. Amended 8192 -> 32768 per the ruling's
            # amend-if-structural-hazard condition.
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            upstream="orcarouter",
            authorized=False,
            pin_constraints=None,
            expected_upstream_model="hy3-preview",
        ),
        "kimicode": ProviderProfile(
            name="kimicode",
            model_id="kimi/kimi-k2.7-code",
            provider_object=None,
            pricing=None,
            # R2 Amendment 1 audit (Walter 2026-07-25): same structural hazard
            # as glm (8192/8192 zero-headroom) -> amended to 32768.
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            upstream="orcarouter",
            authorized=False,
            pin_constraints=None,
            expected_upstream_model="kimi-k2.7-code",
        ),
        "kimik3": ProviderProfile(
            name="kimik3",
            model_id="kimi/kimi-k3",
            provider_object=None,
            pricing=None,
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            upstream="orcarouter",
            authorized=False,
            pin_constraints=None,
            expected_upstream_model="kimi-k3",
        ),
        "minimax": ProviderProfile(
            name="minimax",
            model_id="minimax/minimax-m3",
            provider_object=None,
            pricing=None,
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            upstream="orcarouter",
            authorized=False,
            pin_constraints=None,
            expected_upstream_model="minimax-m3",
        ),
        "ring": ProviderProfile(
            name="ring",
            model_id="inclusionai/ring-2.6-1t",
            provider_object=None,
            pricing=None,
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            authorized=False,
            pin_constraints={
                "min_max_completion_tokens": 32768,
                "uptime_tier": "Normal",
                "quant_preference": ["any"],
                "price_sanity_per_m": {"in": 0.075, "out": 0.625},
                "notes": "Floor-anchor probe candidate; quantization unconfirmed, any Normal-tier endpoint.",
            },
        ),
        "opus": ProviderProfile(
            name="opus",
            model_id="anthropic/claude-opus-4.8",
            provider_object={
                "order": ["anthropic"],
                "only": ["anthropic"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
            pricing=None,
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            authorized=False,
        ),
        "bigpickle": ProviderProfile(
            name="bigpickle",
            model_id="opencode/big-pickle",
            provider_object=None,
            pricing=None,
            max_output_tokens=32768,
            max_reasoning_tokens=8192,
            upstream="opencode",
            authorized=False,
            pin_constraints=None,
            # big-pickle IS xiaomi/mimo-v2.5 upstream. Since
            # 2026-07-21T23:50Z Zen echoes alias "big-pickle" in response
            # ``model`` (previously "xiaomi/mimo-v2.5") and dropped the
            # ``provider`` field — cosmetic echo change verified 2026-07-22 by
            # live probe (runs/identity-probe/20260722T085209Z/: model system
            # info self-identifies "MiMo-v2.5 / Xiaomi LLM Core Team"; key fp
            # + endpoint + system-prompt cache prefix unchanged; identical
            # workload fingerprint as the 14:33Z qualification). Walter's
            # Stage-7 lock stands: assert this identity on every response; on
            # ANY deviation abort the cell, never score it.
            expected_upstream_model="big-pickle",
            expected_upstream_key_fp="b5ce6e5e",
        ),
    }


class ProfileRegistry:
    """Index of named profiles and model-selector resolution."""

    def __init__(self, profiles: dict[str, ProviderProfile]) -> None:
        self._profiles = dict(profiles)

    def resolve_by_model(self, selector: str) -> ProviderProfile:
        """Resolve by model selector, after normalization, or raise ``UnknownModelError``."""
        model_id = normalize_model_selector(selector)
        for profile in self._profiles.values():
            if profile.model_id == model_id:
                return profile
        raise UnknownModelError(
            f"unknown model selector: {selector!r}",
            reason="unknown_model",
        )

    def get(self, profile_name: str) -> ProviderProfile:
        """Get a profile by name (``KeyError`` on unknown profile names)."""
        return self._profiles[profile_name]


def apply_policy(client_body: dict[str, Any], profile: ProviderProfile, max_tokens_cap: int) -> dict[str, Any]:
    """Apply hard policy shaping (R-13 one-path) without mutating caller input.

    Rejects protected field injection, enforces model/profile match, injects the
    OpenRouter provider object when needed, and clamps ``max_tokens`` conservatively.
    """

    for field in PROTECTED_BODY_FIELDS:
        if field in client_body:
            raise ProtectedFieldError(reason=field)

    model = normalize_model_selector(client_body.get("model", ""))
    if model != profile.model_id:
        raise ModelMismatchError(
            message=f"request model {model!r} does not match profile model {profile.model_id!r}",
            reason="model_mismatch",
        )

    body = copy.deepcopy(client_body)
    if profile.upstream == "openrouter":
        if profile.provider_object is None:
            raise ProfileBlockedError("provider_pin_missing")
        body["provider"] = copy.deepcopy(profile.provider_object)
    elif profile.upstream == "opencode":
        # Zen API accepts BARE model ids only ("big-pickle"); "opencode/<id>" is
        # the OpenCode-config selector form and is rejected upstream with
        # 401 "Model opencode/big-pickle is not supported" (verified 2026-07-21).
        body["model"] = profile.model_id.removeprefix("opencode/")

    client_value = body.get("max_tokens")
    if isinstance(client_value, int) and client_value > 0:
        body["max_tokens"] = min(client_value, max_tokens_cap)
    else:
        body["max_tokens"] = max_tokens_cap

    if profile.upstream == "orcarouter" and body.get("stream"):
        stream_options = body.get("stream_options")
        if stream_options is None:
            body["stream_options"] = {"include_usage": True}
        elif isinstance(stream_options, dict) and "include_usage" not in stream_options:
            stream_options["include_usage"] = True

    return body


def input_token_upper_bound(body: dict[str, Any]) -> int:
    """Conservative UTF-8-byte input-token upper bound for budget reservation."""
    messages = body.get("messages", [])
    tools = body.get("tools", [])
    raw = json.dumps(messages, separators=(",", ":"), ensure_ascii=False) + json.dumps(
        tools,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return len(raw.encode("utf-8")) + (PER_MESSAGE_OVERHEAD_TOKENS * len(messages))


def worst_case_usd(
    input_tokens_ub: int,
    profile: ProviderProfile,
    max_tokens_cap: int,
    *,
    cached_input_tokens_ub: int = 0,
) -> float:
    """Compute conservative reservation in USD from profile pricing and token bounds.

    ``cached_input_tokens_ub`` is the portion of ``input_tokens_ub`` already
    proven-billed upstream in this run (an established prompt-cache prefix);
    it is priced at the cache-read rate when pricing provides one. Genuinely
    new input keeps the conservative ``max(input, cache_write)`` rate and the
    output window bound is unchanged. Reservation is admission control, not
    billing: the accrued-actual ledger remains the hard backstop for any
    provider-side cache miss.
    """
    pricing = profile.pricing
    if pricing is None:
        raise ProfileBlockedError("pricing_missing")

    fresh_price = float(max(pricing["input"], pricing.get("cache_write", pricing["input"])))
    cached_price = min(float(pricing.get("cache_read", fresh_price)), fresh_price)
    cached_tokens = max(0, min(int(cached_input_tokens_ub), int(input_tokens_ub)))
    fresh_tokens = int(input_tokens_ub) - cached_tokens
    cost_in = ((float(cached_tokens) * cached_price) + (float(fresh_tokens) * fresh_price)) / 1_000_000
    cost_out = (
        float(max_tokens_cap + profile.max_reasoning_tokens)
        * float(pricing["output"])
        / 1_000_000
    )
    subtotal = cost_in + cost_out
    return subtotal * (1.0 + FEE_RATE) * RESERVATION_SAFETY_FACTOR + FLAT_FEE_USD


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_int(value: Any) -> int:
    if _is_number(value):
        return int(value)
    return 0


def derived_cost_breakdown_usd(
    *,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    cache_write_tokens: int = 0,
    pricing: dict,
) -> dict[str, float]:
    """Estimate billed spend from usage evidence and authorized price rows.

    This is the honest billed estimate (no reservation padding, fees, or safety
    factors). OrcaRouter usage exposes prompt/completion plus cached-read prompt
    tokens. Cache-write token counts are optional and only applied when explicitly
    present in usage evidence; otherwise uncached prompt tokens are priced at the
    input rate.
    """

    if not pricing:
        raise ValueError("missing pricing row")

    prompt_n = max(0, _as_int(prompt_tokens))
    cached_n = max(0, _as_int(cached_tokens))
    completion_n = max(0, _as_int(completion_tokens))
    cache_write_n = max(0, _as_int(cache_write_tokens))

    input_rate = float(pricing["input"])
    cache_read_rate = float(pricing.get("cache_read", pricing["input"]))
    cache_write_rate = float(pricing.get("cache_write", pricing["input"]))
    output_rate = float(pricing["output"])

    cached_prompt_tokens = min(prompt_n, cached_n)
    uncached_prompt_tokens = max(0, prompt_n - cached_prompt_tokens)
    cache_write_prompt_tokens = min(uncached_prompt_tokens, cache_write_n)
    uncached_input_prompt_tokens = max(0, uncached_prompt_tokens - cache_write_prompt_tokens)

    uncached_input_usd = (float(uncached_input_prompt_tokens) * input_rate) / 1_000_000
    cache_read_usd = (float(cached_prompt_tokens) * cache_read_rate) / 1_000_000
    cache_write_usd = (float(cache_write_prompt_tokens) * cache_write_rate) / 1_000_000
    output_usd = (float(completion_n) * output_rate) / 1_000_000
    total_usd = uncached_input_usd + cache_read_usd + cache_write_usd + output_usd

    return {
        "total_usd": total_usd,
        "uncached_input_usd": uncached_input_usd,
        "cache_read_usd": cache_read_usd,
        "cache_write_usd": cache_write_usd,
        "output_usd": output_usd,
        "uncached_input_tokens": float(uncached_input_prompt_tokens),
        "cached_prompt_tokens": float(cached_prompt_tokens),
        "cache_write_prompt_tokens": float(cache_write_prompt_tokens),
        "completion_tokens": float(completion_n),
        "input_rate_per_1m": input_rate,
        "cache_read_rate_per_1m": cache_read_rate,
        "cache_write_rate_per_1m": cache_write_rate,
        "output_rate_per_1m": output_rate,
    }


def derived_cost_usd(
    *,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    cache_write_tokens: int = 0,
    pricing: dict,
) -> float:
    """Estimate billed spend from usage evidence and authorized price rows."""

    return float(
        derived_cost_breakdown_usd(
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            cache_write_tokens=cache_write_tokens,
            pricing=pricing,
        )["total_usd"]
    )


def fetch_orcarouter_pricing(*, timeout: float = 10.0) -> dict:
    """Fetch live OrcaRouter pricing payload or raise ``PricingGateError``."""

    req = urllib_request.Request(ORCAROUTER_PRICING_URL, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=float(timeout)) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read()
    except urllib_error.HTTPError as exc:
        raise PricingGateError(
            f"orcarouter pricing fetch failed: {exc.__class__.__name__}: http_status={int(exc.code)}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surfaced as pricing-gate failure.
        raise PricingGateError(
            f"orcarouter pricing fetch failed: {exc.__class__.__name__}: {exc}"
        ) from exc

    if status != 200:
        raise PricingGateError(f"orcarouter pricing fetch failed: http_status={status}")

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PricingGateError(
            f"orcarouter pricing payload parse failed: {exc.__class__.__name__}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise PricingGateError("orcarouter pricing payload invalid: expected JSON object")

    return parsed


def _derive_orcarouter_row_prices(*, row: dict[str, Any], effective_group_ratio_scalar: float) -> dict[str, float]:
    model_ratio = row.get("model_ratio")
    completion_ratio = row.get("completion_ratio")
    if not _is_number(model_ratio) or not _is_number(completion_ratio):
        raise PricingGateError(
            "orcarouter pricing gate failed: row ratios must be numeric"
        )

    input_usd = float(model_ratio) * 2.0 * float(effective_group_ratio_scalar)
    output_usd = input_usd * float(completion_ratio)

    derived: dict[str, float] = {
        "input": input_usd,
        "output": output_usd,
    }
    cache_ratio = row.get("cache_ratio")
    if cache_ratio is not None:
        if not _is_number(cache_ratio):
            raise PricingGateError("orcarouter pricing gate failed: cache_ratio must be numeric")
        derived["cache_read"] = input_usd * float(cache_ratio)
    return derived


def verify_orcarouter_pricing_gate(
    pricing_payload: dict,
    *,
    profile_model: str | None = None,
    expected_pricing: dict[str, float] | None = None,
) -> None:
    """Verify pricing payload pin + anchor integrity before deriving spend."""

    if not isinstance(pricing_payload, dict):
        raise PricingGateError("orcarouter pricing gate failed: payload must be a JSON object")

    if pricing_payload.get("pricing_version") != ORCAROUTER_PRICING_VERSION_PIN:
        raise PricingGateError(
            "orcarouter pricing gate failed: pricing_version pin mismatch"
        )

    model_discounts = pricing_payload.get("model_discounts")
    if model_discounts is None:
        pass
    elif isinstance(model_discounts, list):
        if model_discounts:
            raise PricingGateError("orcarouter pricing gate failed: model_discounts must be neutral")
    elif isinstance(model_discounts, dict):
        if model_discounts:
            raise PricingGateError("orcarouter pricing gate failed: model_discounts must be neutral")
    else:
        raise PricingGateError("orcarouter pricing gate failed: model_discounts must be neutral")

    workspace_discount = pricing_payload.get("workspace_discount")
    if workspace_discount not in (None, 1, 1.0):
        raise PricingGateError(
            "orcarouter pricing gate failed: workspace_discount must be neutral"
        )

    effective_group_ratio = pricing_payload.get("effective_group_ratio")
    if effective_group_ratio not in (None, 1, 1.0):
        raise PricingGateError(
            "orcarouter pricing gate failed: effective_group_ratio must be neutral"
        )

    group_ratio_value = pricing_payload.get("group_ratio", 1.0)
    if _is_number(group_ratio_value):
        if float(group_ratio_value) != 1.0:
            raise PricingGateError("orcarouter pricing gate failed: group_ratio must be neutral")
    elif isinstance(group_ratio_value, dict):
        for ratio in group_ratio_value.values():
            if not _is_number(ratio) or float(ratio) != 1.0:
                raise PricingGateError("orcarouter pricing gate failed: group_ratio must be neutral")
    else:
        raise PricingGateError("orcarouter pricing gate failed: group_ratio must be neutral")

    rows = pricing_payload.get("data")
    if not isinstance(rows, list):
        raise PricingGateError("orcarouter pricing gate failed: data must be a list")

    anchor_row = None
    for row in rows:
        if isinstance(row, dict) and row.get("model_name") == ORCAROUTER_ANCHOR_MODEL:
            anchor_row = row
            break

    if anchor_row is None:
        raise PricingGateError(
            f"orcarouter pricing gate failed: missing anchor model {ORCAROUTER_ANCHOR_MODEL}"
        )

    # group_ratio is per-group metadata; effective_group_ratio is the resolved scalar for this workspace.
    effective_group_ratio_scalar = 1.0 if effective_group_ratio is None else float(effective_group_ratio)
    anchor_prices = _derive_orcarouter_row_prices(
        row=anchor_row,
        effective_group_ratio_scalar=effective_group_ratio_scalar,
    )
    derived_input = float(anchor_prices["input"])
    derived_output = float(anchor_prices["output"])

    if abs(derived_input - ORCAROUTER_ANCHOR_INPUT_USD_PER_1M) > PRICING_ANCHOR_ABS_TOL_USD:
        raise PricingGateError(
            "orcarouter pricing gate failed: anchor input derivation mismatch"
        )
    if abs(derived_output - ORCAROUTER_ANCHOR_OUTPUT_USD_PER_1M) > PRICING_ANCHOR_ABS_TOL_USD:
        raise PricingGateError(
            "orcarouter pricing gate failed: anchor output derivation mismatch"
        )

    if expected_pricing is None:
        return

    expected_model = str(profile_model or "").strip()
    if not expected_model:
        raise PricingGateError("orcarouter pricing gate failed: profile model required for row verification")

    for key in ("input", "output", "cache_read"):
        value = expected_pricing.get(key) if isinstance(expected_pricing, dict) else None
        if not _is_number(value):
            raise PricingGateError(f"orcarouter pricing gate failed: expected pricing key missing/invalid: {key}")

    target_row = None
    for row in rows:
        if isinstance(row, dict) and str(row.get("model_name", "")).strip() == expected_model:
            target_row = row
            break

    if target_row is None:
        raise PricingGateError(f"orcarouter pricing gate failed: missing row for model {expected_model}")

    target_prices = _derive_orcarouter_row_prices(
        row=target_row,
        effective_group_ratio_scalar=effective_group_ratio_scalar,
    )
    if "cache_read" not in target_prices:
        raise PricingGateError(
            f"orcarouter pricing gate failed: model {expected_model} missing cache_ratio row"
        )

    for key in ("input", "output", "cache_read"):
        expected = float(expected_pricing[key])
        observed = float(target_prices[key])
        if abs(observed - expected) > PRICING_ANCHOR_ABS_TOL_USD:
            raise PricingGateError(
                "orcarouter pricing gate failed: "
                f"{expected_model} {key} mismatch (expected={expected:.12f}, observed={observed:.12f})"
            )


class BudgetLedger:
    """Hard-ceiling budget authority for proxy requests.

    Reservations are conservative upper bounds; settlement has three outcomes:
    proven ``usage.cost`` actuals, derived billed estimates from usage × authorized
    pricing, or committed-unproven retention when no usable evidence exists.
    """

    def __init__(
        self,
        run_id: str,
        model_id: str,
        profile_name: str,
        hard_cap_usd: float,
        checkpoint_path: str,
        *,
        operational_target_usd: float | None = None,
        reject_on_equality: bool = False,
        watch_only: bool = False,
    ) -> None:
        self.run_id = run_id
        self.model_id = model_id
        self.profile_name = profile_name
        self.hard_cap = min(float(hard_cap_usd), ABSOLUTE_MAX_USD)
        self.checkpoint_path = checkpoint_path
        self.operational_target_usd = (
            float(operational_target_usd) if operational_target_usd is not None else None
        )
        self.reject_on_equality = bool(reject_on_equality)
        # R2 Amendment 1 (Walter 2026-07-25 budget-watch directive): watch_only
        # downgrades the hard cap from kill-switch to watch-threshold — the
        # reservation/settlement accounting runs UNCHANGED (the trust
        # instrument), but reserve() never refuses. Spend is watched by the
        # poller + coordinator; the run ends only on natural completion,
        # variance-policy completion, integrity abort, hang-kill, or an
        # explicit stop order. Never scored as capability FAIL (hurdle #7).
        self.watch_only = bool(watch_only)

        self._accrued_actual = 0.0
        self._accrued_derived = 0.0
        self._committed_unproven = 0.0
        self._outstanding: dict[str, float] = {}
        self._lock = threading.Lock()

        if os.path.exists(self.checkpoint_path):
            self._load_checkpoint()
        else:
            self._persist()

    def _binding(self) -> tuple[str, str, str, float]:
        return (self.run_id, self.model_id, self.profile_name, self.hard_cap)

    def _load_checkpoint(self) -> None:
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:  # noqa: BLE001 - surfaced as policy mismatch.
            raise PolicyMismatchError(
                message=f"failed to load checkpoint: {exc}",
                reason="checkpoint_invalid",
            ) from exc

        if not isinstance(data, dict):
            raise PolicyMismatchError(reason="checkpoint_invalid")

        schema_version = int(data.get("schema_version", 0))
        if schema_version not in (1, CHECKPOINT_SCHEMA_VERSION):
            raise PolicyMismatchError(reason="checkpoint_schema_mismatch")

        checkpoint_binding = (
            str(data.get("run_id", "")),
            str(data.get("model_id", "")),
            str(data.get("profile_name", "")),
            float(data.get("hard_cap_usd", 0.0)),
        )
        if checkpoint_binding != self._binding():
            raise PolicyMismatchError(reason="policy_mismatch")

        outstanding = data.get("outstanding", {})
        if not isinstance(outstanding, dict):
            raise PolicyMismatchError(reason="checkpoint_invalid")

        self._accrued_actual = max(0.0, float(data.get("accrued_actual_usd", 0.0)))
        if schema_version == 1:
            self._accrued_derived = 0.0
        else:
            self._accrued_derived = max(0.0, float(data.get("accrued_derived_usd", 0.0)))
        self._committed_unproven = max(0.0, float(data.get("committed_unproven_usd", 0.0)))
        self._outstanding = {str(req_id): max(0.0, float(value)) for req_id, value in outstanding.items()}

    def reserve(self, req_id: str, amount: float) -> None:
        """Reserve a conservative upper bound or raise ``BudgetExceededError``.

        Equality to hard-cap is allowed by default because reservations are
        proven upper bounds; set ``reject_on_equality`` to refuse equal totals.
        """

        reservation = max(0.0, float(amount))
        with self._lock:
            projected = (
                self._accrued_actual
                + self._accrued_derived
                + self._committed_unproven
                + sum(self._outstanding.values())
                + reservation
            )
            over = projected >= self.hard_cap if self.reject_on_equality else projected > self.hard_cap
            if over and not self.watch_only:
                raise BudgetExceededError(
                    message=(
                        f"reservation would exceed hard cap: projected={projected:.8f} "
                        f"hard_cap={self.hard_cap:.8f}"
                    ),
                    reason="budget_exceeded",
                )

            self._outstanding[req_id] = reservation
            self._persist()

    def settle_actual(self, req_id: str, actual_usd: float) -> None:
        """Settle a request using a proven upstream actual (``usage.cost``)."""
        with self._lock:
            self._outstanding.pop(req_id, None)
            self._accrued_actual += max(0.0, float(actual_usd))
            self._persist()

    def settle_derived(self, req_id: str, derived_usd: float) -> None:
        """Settle a request using derived billed usage × authorized pricing."""
        with self._lock:
            self._outstanding.pop(req_id, None)
            self._accrued_derived += max(0.0, float(derived_usd))
            self._persist()

    def retain_unproven(self, req_id: str) -> None:
        """Move reserved amount to committed-unproven (never release uncertainty)."""
        with self._lock:
            amount = self._outstanding.pop(req_id, 0.0)
            self._committed_unproven += max(0.0, float(amount))
            self._persist()

    def remaining(self) -> float:
        """Return currently available budget after actual, unproven, and outstanding holds."""
        with self._lock:
            return (
                self.hard_cap
                - self._accrued_actual
                - self._accrued_derived
                - self._committed_unproven
                - sum(self._outstanding.values())
            )

    def snapshot(self) -> dict[str, float]:
        """Return a logging-safe budget snapshot."""
        with self._lock:
            outstanding_total = sum(self._outstanding.values())
            return {
                "accrued": self._accrued_actual,
                "accrued_derived": self._accrued_derived,
                "committed_unproven": self._committed_unproven,
                "outstanding_total": outstanding_total,
                "remaining": self.hard_cap
                - self._accrued_actual
                - self._accrued_derived
                - self._committed_unproven
                - outstanding_total,
                "hard_cap": self.hard_cap,
            }

    def _persist(self) -> None:
        """Atomically persist checkpoint state (tmp + fsync + replace)."""
        payload = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "model_id": self.model_id,
            "profile_name": self.profile_name,
            "hard_cap_usd": self.hard_cap,
            "accrued_actual_usd": self._accrued_actual,
            "accrued_derived_usd": self._accrued_derived,
            "committed_unproven_usd": self._committed_unproven,
            "outstanding": self._outstanding,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        tmp_path = f"{self.checkpoint_path}.tmp"
        directory = os.path.dirname(self.checkpoint_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_path, self.checkpoint_path)


def key_fingerprint(secret: str) -> str:
    """Return the first 8 hex chars of SHA-256 for safe key-identification logs."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


class ProxyLogger:
    """Structured per-request logger with strict secret/content field guards (R-37)."""

    _FORBIDDEN_FIELDS = {
        "messages",
        "body",
        "prompt",
        "response",
        "authorization",
        "api_key",
        "key",
    }

    def __init__(self, log_path: str) -> None:
        self.log_path = log_path
        directory = os.path.dirname(log_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = open(log_path, "a", encoding="utf-8", buffering=1)

    def event(self, **fields: Any) -> None:
        """Write one timestamped structured line.

        Raises ``ValueError`` when forbidden payload/secret field names are
        passed by a caller.
        """

        for field_name in fields:
            if field_name.lower() in self._FORBIDDEN_FIELDS:
                raise ValueError(f"forbidden log field: {field_name}")

        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chunks: list[str] = []
        for key in sorted(fields):
            value = fields[key]
            chunks.append(f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")

        line = f"{timestamp} |"
        if chunks:
            line = f"{line} {' '.join(chunks)}"

        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()


__all__ = [
    "ABSOLUTE_MAX_USD",
    "BudgetExceededError",
    "BudgetLedger",
    "CHECKPOINT_SCHEMA_VERSION",
    "CredentialError",
    "DEFAULT_PROFILES",
    "DEFAULT_OPENCODE_AUTH_PATH",
    "FEE_RATE",
    "FLAT_FEE_USD",
    "ORCAROUTER_ANCHOR_INPUT_USD_PER_1M",
    "ORCAROUTER_ANCHOR_MODEL",
    "ORCAROUTER_ANCHOR_OUTPUT_USD_PER_1M",
    "ORCAROUTER_PRICING_URL",
    "ORCAROUTER_PRICING_VERSION_PIN",
    "ModelMismatchError",
    "OPENCODE_ZEN_UPSTREAM_URL",
    "ORCAROUTER_UPSTREAM_URL",
    "OPENROUTER_UPSTREAM_URL",
    "PER_MESSAGE_OVERHEAD_TOKENS",
    "PRICING_ANCHOR_ABS_TOL_USD",
    "PricingGateError",
    "PROTECTED_BODY_FIELDS",
    "PolicyMismatchError",
    "ProfileBlockedError",
    "ProfileRegistry",
    "ProtectedFieldError",
    "ProviderProfile",
    "ProxyError",
    "ProxyLogger",
    "RESERVATION_SAFETY_FACTOR",
    "UPSTREAM_CHAT_COMPLETIONS_URLS",
    "UnknownModelError",
    "apply_policy",
    "derived_cost_usd",
    "derived_cost_breakdown_usd",
    "fetch_orcarouter_pricing",
    "input_token_upper_bound",
    "key_fingerprint",
    "load_upstream_key",
    "normalize_model_selector",
    "verify_orcarouter_pricing_gate",
    "worst_case_usd",
]
