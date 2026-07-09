"""OFF control backend implementation for the benchmark harness."""

from __future__ import annotations

from wevibe_bench.backends.base import DeliveryVerdict, MemoryBackend, NeedCard, RecallResult
from wevibe_bench.config import RunConfig


class NoneBackend(MemoryBackend):
    """OFF control backend that intentionally primes nothing and recalls nothing."""

    def prime_session(self, session_id: str) -> None:
        """No-op: OFF control condition primes nothing."""

        _ = session_id

    def recall(self, need: NeedCard, cfg: RunConfig) -> RecallResult:
        """Return the fixed OFF-control recall result (always no memories)."""

        _ = (need, cfg)
        return RecallResult(
            memories=[],
            status="ok",
            reason_code="off_control",
            reachable=True,
            http_status=None,
        )

    def verify_delivery(self, result: RecallResult) -> DeliveryVerdict:
        """Return NO because OFF delivery is N/A and cannot produce delivered recall content.

        The runner does not gate OFF cells on delivery; this method exists only to satisfy
        the backend interface contract.
        """

        _ = result
        return DeliveryVerdict.NO
