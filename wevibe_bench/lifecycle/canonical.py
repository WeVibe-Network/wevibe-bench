"""Canonical WeVibe lifecycle message builders and deterministic hash helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def _sha256_hex(input_text: str) -> str:
    return hashlib.sha256(input_text.encode("utf-8")).hexdigest()


def _render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def canonical_bytes(message: str) -> bytes:
    """Encode a canonical message as UTF-8 bytes for signing."""

    return message.encode("utf-8")


def _canonical_message(tag: str, fields: Mapping[str, Any]) -> str:
    lines = [tag]
    for key in sorted(fields):
        lines.append(f"{key}:{_render_value(fields[key])}")
    return "\n".join(lines)


def fee_model_hash(fee: Mapping[str, Any] | None) -> str:
    """Hash a fixed-order, JSON-like fee model representation."""

    parts: list[str] = []
    if fee:
        tier = fee.get("tier")
        if tier is not None and tier != "":
            parts.append(f'"tier":{json.dumps(str(tier))}')

        monthly_credits = fee.get("monthly_credits")
        if monthly_credits is not None and monthly_credits != "":
            monthly_credits_value = int(monthly_credits)
            if monthly_credits_value != 0:
                parts.append(f'"monthly_credits":{monthly_credits_value}')

        per_query_cost = fee.get("per_query_cost")
        if per_query_cost is not None and per_query_cost != "":
            per_query_cost_value = int(per_query_cost)
            if per_query_cost_value != 0:
                parts.append(f'"per_query_cost":{per_query_cost_value}')

        overage_multiplier = fee.get("overage_multiplier")
        if overage_multiplier is not None and overage_multiplier != "":
            overage_multiplier_value = float(overage_multiplier)
            if overage_multiplier_value != 0.0:
                parts.append(f'"overage_multiplier":{overage_multiplier_value}')

        currency = fee.get("currency")
        if currency is not None and currency != "":
            parts.append(f'"currency":{json.dumps(str(currency))}')

    canonical = "{" + ",".join(parts) + "}"
    return _sha256_hex(canonical)


def keywords_hash(keywords: list[tuple[str, float]]) -> str:
    sorted_keywords = sorted(keywords, key=lambda item: item[0])
    rendered = "\n".join(f"{keyword}:{weight:.6f}" for keyword, weight in sorted_keywords)
    return _sha256_hex(rendered)


def submit_memory_message(
    ciphertext_hash: str,
    contributor_pubkey: str,
    epoch_id: int,
    memory_type: str,
    org_id: str,
    plaintext_hash: str,
    salt: str,
    submission_hash: str,
    wrapped_dek_hash: str,
) -> str:
    fields = {
        "ciphertext_hash": ciphertext_hash,
        "contributor_pubkey": contributor_pubkey,
        "epoch_id": epoch_id,
        "memory_type": memory_type,
        "org_id": org_id,
        "plaintext_hash": plaintext_hash,
        "salt": salt,
        "submission_hash": submission_hash,
        "wrapped_dek_hash": wrapped_dek_hash,
    }
    return _canonical_message("wevibe.submit_memory.v1", fields)


def approve_submission_message(
    epoch_id: int,
    memory_type: str,
    org_id: str,
    signed_by: str,
    submission_hash: str,
) -> str:
    fields = {
        "epoch_id": epoch_id,
        "memory_type": memory_type,
        "org_id": org_id,
        "signed_by": signed_by,
        "submission_hash": submission_hash,
    }
    return _canonical_message("wevibe.approve_submission.v2", fields)


def deny_submission_message(
    org_id: str,
    submission_hash: str,
    reason: str,
    signed_by: str,
) -> str:
    fields = {
        "org_id": org_id,
        "reason": reason,
        "signed_by": signed_by,
        "submission_hash": submission_hash,
    }
    return _canonical_message("wevibe.deny_submission.v1", fields)


def remove_member_message(org_id: str, pubkey: str, signed_by: str) -> str:
    fields = {
        "org_id": org_id,
        "pubkey": pubkey,
        "signed_by": signed_by,
    }
    return _canonical_message("wevibe.remove_member.v1", fields)
