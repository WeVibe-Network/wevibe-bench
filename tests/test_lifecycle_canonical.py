from __future__ import annotations

import hashlib
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from wevibe_bench.lifecycle.canonical import (
    create_org_message,
    deny_submission_message,
    keywords_hash,
    remove_member_message,
)
from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.signing import wevibe_signed_headers


def test_deny_submission_message_matches_expected_vector() -> None:
    message = deny_submission_message("org-1", "hash123", "spam", "cc" * 32)
    assert message == "\n".join(
        [
            "wevibe.deny_submission.v1",
            "org_id:org-1",
            "reason:spam",
            f"signed_by:{'cc' * 32}",
            "submission_hash:hash123",
        ]
    )


def test_remove_member_message_matches_expected_vector() -> None:
    message = remove_member_message("org-1", "member_pubkey_hex", "leader_pubkey_hex")
    assert message == "\n".join(
        [
            "wevibe.remove_member.v1",
            "org_id:org-1",
            "pubkey:member_pubkey_hex",
            "signed_by:leader_pubkey_hex",
        ]
    )


def test_keywords_hash_matches_expected_vector() -> None:
    expected = hashlib.sha256(b"docker:0.600000\nnginx:0.800000").hexdigest()
    assert keywords_hash([("nginx", 0.8), ("docker", 0.6)]) == expected


def test_create_org_message_has_sorted_keys_and_is_deterministic() -> None:
    message = create_org_message(
        leader_pubkey="aa" * 32,
        leader_x25519_pubkey="aa" * 32,
        org_name="Test Org",
        domain="test.com",
        enc_envelope="enc_env_hex",
        search_envelope="search_env_hex",
        mod_envelope="mod_env_hex",
        pk_mod="pk_mod_hex",
        fee_model={
            "tier": "starter",
            "monthly_credits": 1000,
            "per_query_cost": 1,
            "currency": "USD",
        },
    )

    assert message.startswith("wevibe.create_org.v1\n")
    lines = message.split("\n")
    keys = [line.split(":", 1)[0] for line in lines[1:]]
    assert keys == sorted(keys)

    message_again = create_org_message(
        leader_pubkey="aa" * 32,
        leader_x25519_pubkey="aa" * 32,
        org_name="Test Org",
        domain="test.com",
        enc_envelope="enc_env_hex",
        search_envelope="search_env_hex",
        mod_envelope="mod_env_hex",
        pk_mod="pk_mod_hex",
        fee_model={
            "tier": "starter",
            "monthly_credits": 1000,
            "per_query_cost": 1,
            "currency": "USD",
        },
    )
    assert message.encode("utf-8") == message_again.encode("utf-8")


def test_identity_round_trip_signing_and_verification() -> None:
    identity = Identity.from_hex("11" * 32)

    assert re.fullmatch(r"[0-9a-f]{64}", identity.ed_pubkey_hex)

    signature = identity.sign(b"x")
    assert len(signature) == 64
    assert signature == identity.sign(b"x")

    pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(identity.ed_pubkey_hex))
    pubkey.verify(signature, b"x")


def test_wevibe_signed_headers_match_wire_format_and_verify_signature() -> None:
    identity = Identity.from_hex("22" * 32)
    headers = wevibe_signed_headers(identity)

    auth = headers["Authorization"]
    pattern = (
        r"^WeVibe-Signed pubkey=[0-9a-f]{64},"
        r"timestamp=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z,"
        r"signature=[0-9a-f]{128}$"
    )
    assert re.fullmatch(pattern, auth)

    details = re.fullmatch(
        r"^WeVibe-Signed pubkey=([0-9a-f]{64}),"
        r"timestamp=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z),"
        r"signature=([0-9a-f]{128})$",
        auth,
    )
    assert details is not None

    pubkey_hex, timestamp, sig_hex = details.groups()
    assert headers["Content-Type"] == "application/json"
    assert pubkey_hex == identity.ed_pubkey_hex

    pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
    pubkey.verify(bytes.fromhex(sig_hex), timestamp.encode("utf-8"))
