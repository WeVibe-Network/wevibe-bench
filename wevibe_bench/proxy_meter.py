from __future__ import annotations

from dataclasses import dataclass
import logging
import statistics
from typing import Any

import psycopg

from wevibe_bench.contention import ContentionCovariates


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSpend:
    calls: int
    true_usd: float
    benchmark_usd: float
    uncached_input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    unmetered_calls: int
    last_call_at: str | None


@dataclass(frozen=True)
class ModelIdentity:
    model: str
    upstream_model: str | None
    calls: int


def _basename(model: str | None) -> str:
    if model is None:
        return ""
    value = model.strip()
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


class SpendMeter:
    """Read-only spend meter over spend-proxy Postgres events.

    TRUE spend means `actual_spend_usd` (cache-discounted real spend used for
    budget decisions). BENCHMARK spend means `theoretical_spend_usd`
    (full-price synthetic comparator used for scoring only).
    """

    def __init__(self, dsn: str, *, connect_timeout_s: int = 5) -> None:
        self._dsn = dsn
        self._connect_timeout_s = connect_timeout_s

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._dsn, connect_timeout=self._connect_timeout_s)

    def run_spend(self, session_id: str) -> RunSpend:
        query = (
            "SELECT COUNT(*), "
            "COALESCE(SUM(actual_spend_usd),0), "
            "COALESCE(SUM(theoretical_spend_usd),0), "
            "COALESCE(SUM(uncached_input_tokens),0), "
            "COALESCE(SUM(cached_input_tokens),0), "
            "COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(reasoning_tokens),0), "
            "COUNT(*) FILTER (WHERE meter_state='unmetered'), "
            "MAX(ts) "
            "FROM spend_events WHERE session_id=%s"
        )
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (session_id,))
                    row = cur.fetchone()
        except (psycopg.OperationalError, OSError) as exc:
            logger.warning(
                "proxy_meter.run_spend op=run_spend session_id=%s outcome=unmetered reason=meter_unavailable error_type=%s",
                session_id,
                type(exc).__name__,
            )
            return RunSpend(
                calls=0,
                true_usd=0.0,
                benchmark_usd=0.0,
                uncached_input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                unmetered_calls=0,
                last_call_at=None,
            )

        if row is None:
            row = (0, 0, 0, 0, 0, 0, 0, 0, None)

        last_call_at = row[8].isoformat() if row[8] is not None else None
        spend = RunSpend(
            calls=int(row[0]),
            true_usd=float(row[1]),
            benchmark_usd=float(row[2]),
            uncached_input_tokens=int(row[3]),
            cached_input_tokens=int(row[4]),
            output_tokens=int(row[5]),
            reasoning_tokens=int(row[6]),
            unmetered_calls=int(row[7]),
            last_call_at=last_call_at,
        )
        logger.info(
            "proxy_meter.run_spend op=run_spend session_id=%s calls=%s true_usd=%.8f benchmark_usd=%.8f unmetered_calls=%s",
            session_id,
            spend.calls,
            spend.true_usd,
            spend.benchmark_usd,
            spend.unmetered_calls,
        )
        return spend

    def contention_covariates(
        self,
        session_id: str | None,
        *,
        retry_count: int = 0,
        wall_seconds: float | None = None,
        wall_near_timeout: bool = False,
    ) -> ContentionCovariates:
        if not session_id:
            return ContentionCovariates.empty(
                retry_count=retry_count,
                wall_seconds=wall_seconds,
                wall_near_timeout=wall_near_timeout,
            )

        query = (
            "SELECT "
            "COUNT(*) FILTER (WHERE upstream_status=429), "
            "COUNT(*) FILTER (WHERE upstream_status=402), "
            "COUNT(*) FILTER (WHERE err IS NOT NULL), "
            "MAX(request_ms), "
            "ARRAY_AGG(request_ms ORDER BY request_ms) FILTER (WHERE request_ms IS NOT NULL) "
            "FROM spend_events WHERE session_id=%s"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (session_id,))
                row = cur.fetchone()

        if row is None:
            row = (0, 0, 0, None, [])

        request_ms_values = [int(value) for value in (row[4] or [])]
        median_request_ms = (
            int(statistics.median(request_ms_values))
            if request_ms_values
            else None
        )
        covariates = ContentionCovariates(
            http_429_count=int(row[0]),
            http_402_count=int(row[1]),
            retry_count=int(retry_count),
            upstream_error_count=int(row[2]),
            max_request_ms=int(row[3]) if row[3] is not None else None,
            median_request_ms=median_request_ms,
            wall_seconds=wall_seconds,
            wall_near_timeout=bool(wall_near_timeout),
        )
        logger.info(
            "proxy_meter.contention_covariates op=contention_covariates session_id=%s http_429_count=%s http_402_count=%s upstream_error_count=%s max_request_ms=%s median_request_ms=%s retry_count=%s wall_near_timeout=%s",
            session_id,
            covariates.http_429_count,
            covariates.http_402_count,
            covariates.upstream_error_count,
            covariates.max_request_ms,
            covariates.median_request_ms,
            covariates.retry_count,
            covariates.wall_near_timeout,
        )
        return covariates

    def model_identity(self, session_id: str) -> list[ModelIdentity]:
        query = (
            "SELECT model, upstream_model, COUNT(*) "
            "FROM spend_events WHERE session_id=%s GROUP BY model, upstream_model"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (session_id,))
                rows = cur.fetchall()

        identities = [
            ModelIdentity(model=str(model), upstream_model=upstream_model, calls=int(calls))
            for model, upstream_model, calls in rows
        ]
        logger.info(
            "proxy_meter.model_identity op=model_identity session_id=%s identity_rows=%s",
            session_id,
            len(identities),
        )
        return identities

    def model_identity_mismatches(self, session_id: str) -> list[ModelIdentity]:
        mismatches = [
            row
            for row in self.model_identity(session_id)
            if not row.upstream_model or _basename(row.upstream_model) != _basename(row.model)
        ]
        logger.info(
            "proxy_meter.model_identity_mismatches op=model_identity_mismatches session_id=%s mismatch_count=%s",
            session_id,
            len(mismatches),
        )
        return mismatches
