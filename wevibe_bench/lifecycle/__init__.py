"""Lifecycle config, REST recall, and delivery-proof helpers for WeVibe bench flows.

Surviving scope after the memory-production strip: ``LifecycleConfig``
(bench endpoint/path configuration) plus the thin client helpers in
``mcp_rest`` (recall + identity pubkeys), ``m2_proof`` (delivery proofs
and memory fragments), and ``logging_util``. The clone bring-up,
identity-signing, canonical-message, and hub-commit machinery was removed
with the memory-production flow.
"""

from __future__ import annotations

from .lconfig import LifecycleConfig

__all__ = [
    "LifecycleConfig",
]
