"""Ed25519 signing helpers for WeVibe transport/body authentication."""

from __future__ import annotations

from datetime import datetime, timezone

from .identity import Identity


def wevibe_signed_headers(identity: Identity, trace_id: str | None = None) -> dict[str, str]:
    """Build WeVibe-Signed transport headers (timestamp-only signed payload)."""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature_hex = identity.sign_hex(timestamp.encode("utf-8"))

    headers = {
        "Authorization": (
            f"WeVibe-Signed pubkey={identity.ed_pubkey_hex},"
            f"timestamp={timestamp},signature={signature_hex}"
        ),
        "Content-Type": "application/json",
    }
    if trace_id is not None:
        headers["X-WeVibe-Trace-Id"] = trace_id
    return headers


def body_sign(identity: Identity, canonical_message: str) -> str:
    """Sign a canonical message string for body-level signature fields."""

    return identity.sign_hex(canonical_message.encode("utf-8"))
