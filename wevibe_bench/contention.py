from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContentionCovariates:
    http_429_count: int
    http_402_count: int
    retry_count: int
    upstream_error_count: int
    max_request_ms: int | None
    median_request_ms: int | None
    wall_seconds: float | None
    wall_near_timeout: bool

    @classmethod
    def empty(
        cls,
        *,
        retry_count: int = 0,
        wall_seconds: float | None = None,
        wall_near_timeout: bool = False,
    ) -> ContentionCovariates:
        return cls(
            http_429_count=0,
            http_402_count=0,
            retry_count=int(retry_count),
            upstream_error_count=0,
            max_request_ms=None,
            median_request_ms=None,
            wall_seconds=wall_seconds,
            wall_near_timeout=bool(wall_near_timeout),
        )


__all__ = ["ContentionCovariates"]
