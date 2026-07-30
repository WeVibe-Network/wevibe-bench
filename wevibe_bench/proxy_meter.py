from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import statistics
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import psycopg

from wevibe_bench.contention import ContentionCovariates
from wevibe_bench.spend_key import (
    key_fingerprint,
    resolve_orcarouter_api_key,
    resolve_spend_proxy_base_url,
)


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


@dataclass(frozen=True)
class PricingVerdict:
    ok: bool
    version: str
    missing_models: list[str]
    reason: str


def _basename(model: str | None) -> str:
    if model is None:
        return ""
    value = model.strip()
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


def _origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid proxy base_url: {base_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _json_get(url: str, *, headers: dict[str, str], timeout_s: float) -> dict[str, Any]:
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout_s) as response:
        data = response.read()
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid JSON payload type from {url}: {type(payload).__name__}")
    return payload


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
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (session_id,))
                row = cur.fetchone()

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


def verify_pricing(
    *,
    roster_models: list[str],
    expected_version: str = "c58e194db3f6a20e7d41b8c9e2f05a17",
    base_url: str | None = None,
    bearer_token: str | None = None,
    timeout_s: float = 10.0,
) -> PricingVerdict:
    resolved_base_url = base_url or resolve_spend_proxy_base_url()
    origin = _origin(resolved_base_url)

    health_url = f"{origin}/health"
    try:
        health = _json_get(health_url, headers={}, timeout_s=timeout_s)
    except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        verdict = PricingVerdict(
            ok=False,
            version="",
            missing_models=[],
            reason=f"health-check-failed: {type(exc).__name__}",
        )
        logger.info(
            "proxy_meter.verify_pricing op=verify_pricing verdict=%s reason=%s",
            verdict.ok,
            verdict.reason,
        )
        return verdict

    health_ok = bool(health.get("ok")) and bool(health.get("db_ok")) and bool(health.get("pricing_ok"))
    health_version = str(health.get("pricing_version") or "")
    if not health_ok:
        verdict = PricingVerdict(
            ok=False,
            version=health_version,
            missing_models=[],
            reason="gate-down",
        )
        logger.info(
            "proxy_meter.verify_pricing op=verify_pricing verdict=%s version=%s missing_models=%s reason=%s",
            verdict.ok,
            verdict.version,
            len(verdict.missing_models),
            verdict.reason,
        )
        return verdict

    token = bearer_token
    token_source = "arg"
    if token is None:
        token, token_source = resolve_orcarouter_api_key()
    token_fp = key_fingerprint(token)

    unique_models: list[str] = []
    seen_models: set[str] = set()
    for model in roster_models:
        if model and model not in seen_models:
            unique_models.append(model)
            seen_models.add(model)

    query = urlencode([("model", model) for model in unique_models])
    pricing_url = f"{origin}/v1/pricing/models"
    if query:
        pricing_url = f"{pricing_url}?{query}"

    try:
        payload = _json_get(
            pricing_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout_s=timeout_s,
        )
    except HTTPError as exc:
        reason = "gate-down" if exc.code == 503 else f"pricing-http-{exc.code}"
        verdict = PricingVerdict(ok=False, version=health_version, missing_models=[], reason=reason)
        logger.info(
            "proxy_meter.verify_pricing op=verify_pricing verdict=%s version=%s missing_models=%s reason=%s token_source=%s token_fp=%s",
            verdict.ok,
            verdict.version,
            len(verdict.missing_models),
            verdict.reason,
            token_source,
            token_fp,
        )
        return verdict
    except (URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        verdict = PricingVerdict(
            ok=False,
            version=health_version,
            missing_models=[],
            reason=f"pricing-fetch-failed: {type(exc).__name__}",
        )
        logger.info(
            "proxy_meter.verify_pricing op=verify_pricing verdict=%s version=%s missing_models=%s reason=%s token_source=%s token_fp=%s",
            verdict.ok,
            verdict.version,
            len(verdict.missing_models),
            verdict.reason,
            token_source,
            token_fp,
        )
        return verdict

    pricing = payload.get("pricing")
    pricing_data = pricing if isinstance(pricing, dict) else {}
    version = str(pricing_data.get("version") or health_version)
    pricing_ok = bool(pricing_data.get("ok"))
    models_data = payload.get("models")
    models = models_data if isinstance(models_data, dict) else {}
    missing_models = [model for model in unique_models if model not in models]

    if not pricing_ok:
        reason = "gate-down"
        ok = False
    elif version != expected_version:
        reason = f"version-mismatch got {version} want {expected_version}"
        ok = False
    elif missing_models:
        reason = f"missing models: {', '.join(missing_models)}"
        ok = False
    else:
        reason = "ok"
        ok = True

    verdict = PricingVerdict(ok=ok, version=version, missing_models=missing_models, reason=reason)
    logger.info(
        "proxy_meter.verify_pricing op=verify_pricing verdict=%s version=%s missing_models=%s reason=%s token_source=%s token_fp=%s",
        verdict.ok,
        verdict.version,
        len(verdict.missing_models),
        verdict.reason,
        token_source,
        token_fp,
    )
    return verdict
