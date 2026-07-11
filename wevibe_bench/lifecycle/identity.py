"""Lifecycle identity primitive: Ed25519 seed actor for canonical body + transport signing."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import cached_property

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class Identity:
    """Seed-backed actor identity used for WeVibe signing operations."""

    seed: bytes

    def __post_init__(self) -> None:
        if len(self.seed) != 32:
            raise ValueError("seed must be exactly 32 bytes")

    @classmethod
    def generate(cls) -> "Identity":
        return cls(seed=os.urandom(32))

    @classmethod
    def from_hex(cls, hex64: str) -> "Identity":
        if not _HEX64_RE.fullmatch(hex64):
            raise ValueError("hex64 must match ^[0-9a-fA-F]{64}$")
        return cls(seed=bytes.fromhex(hex64))

    @property
    def seed_hex(self) -> str:
        return self.seed.hex()

    @cached_property
    def _signing_key(self) -> ed25519.Ed25519PrivateKey:
        return ed25519.Ed25519PrivateKey.from_private_bytes(self.seed)

    @cached_property
    def _ed_pubkey_bytes(self) -> bytes:
        return self._signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def ed_pubkey_hex(self) -> str:
        return self._ed_pubkey_bytes.hex()

    def sign(self, message: bytes) -> bytes:
        return self._signing_key.sign(message)

    def sign_hex(self, message: bytes) -> str:
        return self.sign(message).hex()

    def seed_fp(self) -> str:
        return hashlib.sha256(self.seed).hexdigest()[:8]

    def ed_pub_fp(self) -> str:
        return hashlib.sha256(self._ed_pubkey_bytes).hexdigest()[:8]
