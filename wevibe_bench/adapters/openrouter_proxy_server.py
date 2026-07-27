"""HTTP transport + CLI for the OpenRouter benchmark proxy.

This module is stdlib-only and depends on ``openrouter_proxy`` for policy,
pricing, budgeting, and logging primitives, including actual/derived/retained
reservation settlement.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import http.server
import itertools
import json
import os
import secrets
import socketserver
import sys
import threading
import urllib.request
import uuid
from typing import Any, Callable, Iterable, NamedTuple

from wevibe_bench.adapters.openrouter_proxy import (
    _as_int,
    _is_number,
    BudgetExceededError,
    BudgetLedger,
    CredentialError,
    DEFAULT_OPENCODE_AUTH_PATH,
    DEFAULT_PROFILES,
    ModelMismatchError,
    ORCAROUTER_PRICING_VERSION_PIN,
    OPENROUTER_UPSTREAM_URL,
    PricingGateError,
    UPSTREAM_CHAT_COMPLETIONS_URLS,
    ProfileBlockedError,
    ProfileRegistry,
    ProtectedFieldError,
    ProxyLogger,
    UnknownModelError,
    apply_policy,
    derived_cost_breakdown_usd,
    fetch_orcarouter_pricing,
    input_token_upper_bound,
    key_fingerprint,
    load_upstream_key,
    normalize_model_selector,
    verify_orcarouter_pricing_gate,
    worst_case_usd,
)


class UpstreamResponse(NamedTuple):
    """Result returned by upstream transport seam."""

    status: int
    headers: dict[str, str]
    body: bytes | None
    stream_lines: Iterable[bytes] | None


UpstreamTransport = Callable[[str, dict[str, str], bytes, bool], UpstreamResponse]


def _iter_response_lines(response: Any) -> Iterable[bytes]:
    """Yield raw upstream line bytes and close the response when exhausted."""

    try:
        for line in response:
            if isinstance(line, bytes):
                yield line
            else:
                yield bytes(line)
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - best effort close.
            pass


def _iter_single_chunk(chunk: bytes) -> Iterable[bytes]:
    """Yield a single chunk as an iterable for stream fallback handling."""

    yield chunk


def urllib_upstream_transport(
    url: str,
    headers: dict[str, str],
    body_bytes: bytes,
    stream: bool,
) -> UpstreamResponse:
    """POST to OpenRouter using urllib.

    For ``stream=True``, returns an iterator over raw response lines and leaves
    settlement decisions to the proxy handler.
    """

    request = urllib.request.Request(url=url, data=body_bytes, headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=60.0)
        response_headers = {str(k): str(v) for k, v in response.headers.items()}
        status = int(response.getcode() or 0)
        if stream:
            return UpstreamResponse(
                status=status,
                headers=response_headers,
                body=None,
                stream_lines=_iter_response_lines(response),
            )

        payload = response.read()
        response.close()
        return UpstreamResponse(status=status, headers=response_headers, body=payload, stream_lines=None)
    except urllib.request.HTTPError as exc:
        exc_headers = {str(k): str(v) for k, v in (exc.headers.items() if exc.headers is not None else [])}
        if stream:
            try:
                payload = exc.read()
            except Exception:  # noqa: BLE001
                payload = b""
            return UpstreamResponse(
                status=int(getattr(exc, "code", 500)),
                headers=exc_headers,
                body=None,
                stream_lines=_iter_single_chunk(payload),
            )

        try:
            error_payload = exc.read()
        except Exception:  # noqa: BLE001
            error_payload = b""
        return UpstreamResponse(
            status=int(getattr(exc, "code", 500)),
            headers=exc_headers,
            body=error_payload,
            stream_lines=None,
        )


def _as_float_or_none(value: Any) -> float | None:
    if _is_number(value):
        return float(value)
    return None


def _parse_json_object(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _provider_evidence(provider_obj: Any) -> tuple[list[str], list[str]]:
    slugs: list[str] = []
    quantizations: list[str] = []

    def _append_unique(dst: list[str], value: str) -> None:
        item = value.strip()
        if item and item not in dst:
            dst.append(item)

    if isinstance(provider_obj, str):
        _append_unique(slugs, provider_obj)
        return slugs, quantizations

    if isinstance(provider_obj, list):
        for item in provider_obj:
            if isinstance(item, str):
                _append_unique(slugs, item)
        return slugs, quantizations

    if not isinstance(provider_obj, dict):
        return slugs, quantizations

    for key in ("slug", "name", "provider"):
        value = provider_obj.get(key)
        if isinstance(value, str):
            _append_unique(slugs, value)

    for key in ("only", "order"):
        value = provider_obj.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    _append_unique(slugs, item)

    value = provider_obj.get("quantizations")
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                _append_unique(quantizations, item)

    return slugs, quantizations


class _UsageEvidence(NamedTuple):
    completion_tokens: int
    reasoning_tokens: int
    cost: float | None
    prompt_tokens: int
    cached_tokens: int
    cache_write_tokens: int


def _usage_evidence(usage_obj: Any) -> _UsageEvidence:
    if not isinstance(usage_obj, dict):
        return _UsageEvidence(
            completion_tokens=0,
            reasoning_tokens=0,
            cost=None,
            prompt_tokens=0,
            cached_tokens=0,
            cache_write_tokens=0,
        )

    completion_tokens = _as_int(usage_obj.get("completion_tokens"))
    reasoning_tokens = 0
    details = usage_obj.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning_tokens = _as_int(details.get("reasoning_tokens"))
    prompt_tokens = _as_int(usage_obj.get("prompt_tokens"))
    cached_tokens = 0
    cache_write_tokens = 0
    prompt_details = usage_obj.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached_tokens = _as_int(prompt_details.get("cached_tokens"))
        cache_write_tokens = _as_int(prompt_details.get("cache_write_tokens"))
    cost = _as_float_or_none(usage_obj.get("cost"))
    return _UsageEvidence(
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        cost=cost,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def _parse_sse_data_json(line: bytes) -> dict[str, Any] | None:
    try:
        text = line.decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return None

    if not text.startswith("data:"):
        return None

    payload = text[5:].strip()
    if not payload or payload == "[DONE]":
        return None

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threading HTTP server for concurrent proxy requests."""

    daemon_threads = True
    allow_reuse_address = True


class ProxyServer:
    """OpenRouter proxy endpoint implementing policy, reserve/settle, and logging."""

    _CHAT_PATH = "/api/v1/chat/completions"
    _HOP_BY_HOP_HEADERS = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }

    def __init__(
        self,
        *,
        registry: ProfileRegistry,
        profile_name: str,
        ledger: BudgetLedger,
        upstream_key: str,
        run_token: str,
        logger: ProxyLogger,
        max_tokens_cap: int,
        upstream_url: str = OPENROUTER_UPSTREAM_URL,
        upstream_transport: UpstreamTransport = urllib_upstream_transport,
    ) -> None:
        self.registry = registry
        self.profile_name = profile_name
        self.ledger = ledger
        self.upstream_key = upstream_key
        self.run_token = run_token
        self.logger = logger
        self.max_tokens_cap = int(max_tokens_cap)
        self.upstream_url = str(upstream_url)
        self.upstream_transport = upstream_transport

        self._ordinal_counter = itertools.count(1)
        self._ordinal_lock = threading.Lock()
        self._upstream_key_fp = key_fingerprint(upstream_key)

        # High-water input-token upper bound across proven-billed requests
        # (settled via upstream ``usage.cost`` OR derived from upstream token
        # usage with authorized pricing). This is the established prompt-cache
        # prefix bound for reservation pricing; retained-unproven requests
        # never establish it. In-process only by design: a proxy restart
        # forgets the prefix and reservations degrade to conservative.
        self._proven_billed_input_ub = 0
        self._billed_ub_lock = threading.Lock()

        # Upstream-identity trip switch (Walter 21-07-26): once ANY upstream
        # response reports a model that mismatches the profile's
        # ``expected_upstream_model``, every subsequent request in this run is
        # refused (503 identity_mismatch) — the cell aborts live instead of
        # accumulating unscoreable data from a silently swapped model.
        self._identity_mismatch_observed: str | None = None
        self._identity_lock = threading.Lock()

        # Fail fast on unknown profile wiring.
        self.registry.get(profile_name)

    def _next_ordinal(self) -> int:
        with self._ordinal_lock:
            return int(next(self._ordinal_counter))

    def _cached_input_ub(self, in_tokens_ub: int) -> int:
        """Portion of this request's input UB covered by the proven-billed prefix."""
        with self._billed_ub_lock:
            return min(int(in_tokens_ub), self._proven_billed_input_ub)

    def _record_proven_billed_input_ub(self, in_tokens_ub: int) -> None:
        with self._billed_ub_lock:
            if int(in_tokens_ub) > self._proven_billed_input_ub:
                self._proven_billed_input_ub = int(in_tokens_ub)

    def _check_upstream_identity(self, profile: Any, model_evidence: str, trace_id: str) -> None:
        """Trip the identity switch when the upstream-reported model mismatches.

        No-op unless the profile pins ``expected_upstream_model`` and the
        response carried a model id. The first mismatch is logged loudly with
        expected/observed evidence; the switch is one-way for the process
        lifetime (per-run proxy => per-run scope).
        """

        expected = getattr(profile, "expected_upstream_model", None)
        observed = str(model_evidence or "").strip()
        if not expected or not observed:
            return
        if observed == str(expected):
            return
        with self._identity_lock:
            already = self._identity_mismatch_observed
            if already is None:
                self._identity_mismatch_observed = observed
        if already is None:
            self.logger.event(
                event="identity_mismatch",
                trace_id=trace_id,
                expected_upstream_model=str(expected),
                observed_upstream_model=observed,
            )

    def _identity_tripped(self) -> str | None:
        with self._identity_lock:
            return self._identity_mismatch_observed

    def build_handler_class(self) -> type[http.server.BaseHTTPRequestHandler]:
        proxy = self

        class ProxyHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "WeVibeOpenRouterProxy/0"

            def do_POST(self) -> None:  # noqa: N802 - stdlib hook name.
                proxy._handle_post(self)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 - stdlib hook signature.
                return

        return ProxyHandler

    @staticmethod
    def _parse_bearer_token(auth_header: str) -> str:
        if not auth_header:
            return ""
        parts = auth_header.strip().split(" ", 1)
        if len(parts) != 2:
            return ""
        scheme, token = parts[0].strip(), parts[1].strip()
        if scheme.lower() != "bearer":
            return ""
        return token

    @staticmethod
    def _read_body(handler: http.server.BaseHTTPRequestHandler) -> bytes:
        raw_len = handler.headers.get("Content-Length", "")
        if not raw_len:
            return b""
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("invalid content-length") from exc
        if length < 0:
            raise ValueError("negative content-length")
        if length == 0:
            return b""
        return handler.rfile.read(length)

    def _send_json_response(
        self,
        handler: http.server.BaseHTTPRequestHandler,
        *,
        status: int,
        payload: dict[str, Any],
        trace_id: str,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("X-WeVibe-Trace-Id", trace_id)
        handler.end_headers()
        if body:
            handler.wfile.write(body)

    def _send_error(
        self,
        handler: http.server.BaseHTTPRequestHandler,
        *,
        status: int,
        message: str,
        error_type: str,
        code: str,
        trace_id: str,
    ) -> None:
        self._send_json_response(
            handler,
            status=status,
            payload={"error": {"message": message, "type": error_type, "code": code}},
            trace_id=trace_id,
        )

    def _copy_upstream_headers(
        self,
        handler: http.server.BaseHTTPRequestHandler,
        upstream_headers: dict[str, str],
        *,
        default_content_type: str | None,
    ) -> None:
        sent_content_type = False
        for key, value in upstream_headers.items():
            key_str = str(key)
            value_str = str(value)
            if key_str.lower() in self._HOP_BY_HOP_HEADERS:
                continue
            if key_str.lower() == "content-type":
                sent_content_type = True
            handler.send_header(key_str, value_str)

        if default_content_type is not None and not sent_content_type:
            handler.send_header("Content-Type", default_content_type)

    def _finalize_reservation(
        self,
        *,
        reserved: bool,
        trace_id: str,
        proven_cost: float | None,
        profile: Any,
        usage: _UsageEvidence,
        billed_input_ub: int = 0,
    ) -> tuple[str, dict[str, float] | None]:
        if not reserved:
            return "none", None
        if proven_cost is not None:
            self.ledger.settle_actual(trace_id, proven_cost)
            self._record_proven_billed_input_ub(billed_input_ub)
            return "actual", None

        pricing = getattr(profile, "pricing", None)
        has_authorized_pricing = (
            bool(getattr(profile, "authorized", False))
            and isinstance(pricing, dict)
            and bool(pricing)
            and _is_number(pricing.get("input"))
            and _is_number(pricing.get("output"))
            and _is_number(pricing.get("cache_read"))
        )
        has_usage_evidence = usage.prompt_tokens > 0 or usage.completion_tokens > 0
        if has_authorized_pricing and has_usage_evidence:
            try:
                derived_breakdown = derived_cost_breakdown_usd(
                    prompt_tokens=usage.prompt_tokens,
                    cached_tokens=usage.cached_tokens,
                    completion_tokens=usage.completion_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    pricing=pricing,
                )
                derived = float(derived_breakdown["total_usd"])
                self.ledger.settle_derived(trace_id, derived)
                self.logger.event(
                    event="derived_settlement",
                    trace_id=trace_id,
                    uncached_input_tokens=int(derived_breakdown["uncached_input_tokens"]),
                    cached_input_tokens=int(derived_breakdown["cached_prompt_tokens"]),
                    cache_write_tokens=int(derived_breakdown["cache_write_prompt_tokens"]),
                    completion_tokens=int(derived_breakdown["completion_tokens"]),
                    input_rate_per_1m=derived_breakdown["input_rate_per_1m"],
                    cache_read_rate_per_1m=derived_breakdown["cache_read_rate_per_1m"],
                    cache_write_rate_per_1m=derived_breakdown["cache_write_rate_per_1m"],
                    output_rate_per_1m=derived_breakdown["output_rate_per_1m"],
                    uncached_input_usd=derived_breakdown["uncached_input_usd"],
                    cache_read_usd=derived_breakdown["cache_read_usd"],
                    cache_write_usd=derived_breakdown["cache_write_usd"],
                    output_usd=derived_breakdown["output_usd"],
                    derived_total_usd=derived,
                )
                # Derived settles are proven billed-usage evidence for prefix tracking.
                self._record_proven_billed_input_ub(billed_input_ub)
                return "derived", derived_breakdown
            except Exception:  # noqa: BLE001 - fail closed to retain path.
                pass

        self.ledger.retain_unproven(trace_id)
        return "retained_unproven", None

    def _handle_post(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        started_at = datetime.datetime.now(datetime.timezone.utc)

        trace_id = handler.headers.get("X-WeVibe-Trace-Id", "").strip() or uuid.uuid4().hex
        ordinal: int | None = None
        status = 500
        error_text = ""

        token_fp = key_fingerprint("")
        model_evidence = ""
        provider_slugs: list[str] = []
        quantizations: list[str] = []
        in_bytes = 0
        in_tokens_ub = 0
        cached_in_tokens_ub = 0
        out_tokens = 0
        reasoning_tokens = 0
        reserved_usd = 0.0

        reserved = False
        proven_cost: float | None = None
        prompt_tokens = 0
        cached_prompt_tokens = 0
        settle_state = "none"
        settle_breakdown: dict[str, float] | None = None

        try:
            if handler.path != self._CHAT_PATH:
                status = 404
                error_text = "route not found"
                self._send_error(
                    handler,
                    status=404,
                    message="route not found",
                    error_type="invalid_request_error",
                    code="not_found",
                    trace_id=trace_id,
                )
                return

            # 1) auth with ephemeral run token.
            supplied_token = self._parse_bearer_token(handler.headers.get("Authorization", ""))
            token_fp = key_fingerprint(supplied_token)
            if not supplied_token or not secrets.compare_digest(supplied_token, self.run_token):
                status = 401
                error_text = "invalid bearer token"
                self._send_error(
                    handler,
                    status=401,
                    message="invalid bearer token",
                    error_type="authentication_error",
                    code="invalid_api_key",
                    trace_id=trace_id,
                )
                return

            # 2) parse json + assign ordinal + trace_id fallback.
            try:
                raw_body = self._read_body(handler)
            except ValueError as exc:
                status = 400
                error_text = str(exc)
                self._send_error(
                    handler,
                    status=400,
                    message=str(exc),
                    error_type="invalid_request_error",
                    code="bad_json",
                    trace_id=trace_id,
                )
                return

            in_bytes = len(raw_body)

            try:
                client_body = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                status = 400
                error_text = "bad json"
                self._send_error(
                    handler,
                    status=400,
                    message="request body must be valid JSON object",
                    error_type="invalid_request_error",
                    code="bad_json",
                    trace_id=trace_id,
                )
                return
            if not isinstance(client_body, dict):
                status = 400
                error_text = "bad json object"
                self._send_error(
                    handler,
                    status=400,
                    message="request body must be a JSON object",
                    error_type="invalid_request_error",
                    code="bad_json",
                    trace_id=trace_id,
                )
                return

            ordinal = self._next_ordinal()
            trace_id = handler.headers.get("X-WeVibe-Trace-Id", "").strip() or uuid.uuid4().hex
            model_evidence = str(client_body.get("model", "") or "")

            # 3) profile runnable checks.
            profile = self.registry.get(self.profile_name)
            tripped_model = self._identity_tripped()
            if tripped_model is not None:
                status = 503
                error_text = f"identity_mismatch observed={tripped_model}"
                self._send_error(
                    handler,
                    status=503,
                    message=(
                        "upstream identity mismatch previously observed for this run "
                        f"(observed model {tripped_model!r}); refusing further requests"
                    ),
                    error_type="api_error",
                    code="identity_mismatch",
                    trace_id=trace_id,
                )
                return
            blocked = profile.runnable_reason()
            if blocked is not None:
                status = 403
                error_text = blocked
                self._send_error(
                    handler,
                    status=403,
                    message=f"profile blocked: {blocked}",
                    error_type="invalid_request_error",
                    code=blocked,
                    trace_id=trace_id,
                )
                return

            # 4) apply_policy + error mapping.
            try:
                transformed = apply_policy(client_body, profile, self.max_tokens_cap)
            except ProtectedFieldError as exc:
                status = 400
                error_text = str(exc)
                self._send_error(
                    handler,
                    status=400,
                    message=str(exc),
                    error_type="invalid_request_error",
                    code=exc.reason,
                    trace_id=trace_id,
                )
                return
            except (ModelMismatchError, UnknownModelError) as exc:
                status = 400
                error_text = str(exc)
                self._send_error(
                    handler,
                    status=400,
                    message=str(exc),
                    error_type="invalid_request_error",
                    code=exc.reason,
                    trace_id=trace_id,
                )
                return
            except ProfileBlockedError as exc:
                status = 403
                error_text = str(exc)
                self._send_error(
                    handler,
                    status=403,
                    message=str(exc),
                    error_type="invalid_request_error",
                    code=exc.reason,
                    trace_id=trace_id,
                )
                return

            provider_slugs, quantizations = _provider_evidence(transformed.get("provider"))
            model_evidence = str(transformed.get("model", model_evidence) or model_evidence)

            # 5) input upper bound + worst-case reservation (established
            # proven-billed prefix priced at cache-read rate).
            in_tokens_ub = input_token_upper_bound(transformed)
            cached_in_tokens_ub = self._cached_input_ub(in_tokens_ub)
            reserved_usd = float(
                worst_case_usd(
                    in_tokens_ub,
                    profile,
                    self.max_tokens_cap,
                    cached_input_tokens_ub=cached_in_tokens_ub,
                )
            )

            # 6) reserve budget before forwarding.
            try:
                self.ledger.reserve(trace_id, reserved_usd)
            except BudgetExceededError as exc:
                status = 402
                error_text = str(exc)
                self._send_error(
                    handler,
                    status=402,
                    message=str(exc),
                    error_type="insufficient_quota",
                    code=exc.reason,
                    trace_id=trace_id,
                )
                return
            reserved = True

            # 7) forward using real upstream key.
            upstream_headers = {
                "Authorization": f"Bearer {self.upstream_key}",
                "Content-Type": "application/json",
                # Cloudflare in front of opencode.ai bans urllib's default
                # "Python-urllib/x.y" UA signature (error 1010 -> HTTP 403,
                # verified 2026-07-21); any explicit UA passes. Harmless for
                # OpenRouter, required for Zen — one path (R-13).
                "User-Agent": "wevibe-bench-proxy/1.0",
            }
            for header_name in ("HTTP-Referer", "X-Title"):
                header_value = handler.headers.get(header_name)
                if header_value:
                    upstream_headers[header_name] = header_value

            upstream_body = json.dumps(
                transformed,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            in_bytes = len(upstream_body)
            stream_flag = bool(transformed.get("stream"))

            upstream = self.upstream_transport(
                self.upstream_url,
                upstream_headers,
                upstream_body,
                stream_flag,
            )

            if stream_flag:
                # 8b) stream relay + usage capture while draining upstream.
                status = int(upstream.status)

                lines = upstream.stream_lines
                if lines is None:
                    if upstream.body is None:
                        lines = ()
                    else:
                        lines = _iter_single_chunk(upstream.body)

                client_connected = True
                stream_lines_relayed = 0
                try:
                    handler.close_connection = True
                    handler.send_response(status)
                    self._copy_upstream_headers(
                        handler,
                        upstream.headers,
                        default_content_type="text/event-stream",
                    )
                    handler.send_header("Connection", "close")
                    handler.send_header("X-WeVibe-Trace-Id", trace_id)
                    handler.end_headers()
                except OSError:
                    client_connected = False

                try:
                    for line in lines:
                        stream_lines_relayed += 1
                        payload = _parse_sse_data_json(line)
                        if isinstance(payload, dict):
                            if isinstance(payload.get("model"), str):
                                model_evidence = payload["model"]
                                self._check_upstream_identity(profile, model_evidence, trace_id)

                            resp_provider = payload.get("provider")
                            if resp_provider is not None:
                                provider_slugs, quantizations = _provider_evidence(resp_provider)

                            usage = payload.get("usage")
                            ev = _usage_evidence(usage)
                            if ev.completion_tokens:
                                out_tokens = ev.completion_tokens
                            if ev.reasoning_tokens:
                                reasoning_tokens = ev.reasoning_tokens
                            if ev.prompt_tokens:
                                prompt_tokens = ev.prompt_tokens
                            if ev.cached_tokens:
                                cached_prompt_tokens = ev.cached_tokens
                            if ev.cost is not None:
                                proven_cost = ev.cost

                        if client_connected:
                            try:
                                handler.wfile.write(line)
                                handler.wfile.flush()
                            except OSError:
                                client_connected = False
                except Exception as exc:  # noqa: BLE001 - upstream stream iterator failure.
                    error_text = f"upstream stream failure: {exc}"
                    if proven_cost is None:
                        proven_cost = None

                self.logger.event(
                    event="stream_relay_end",
                    trace_id=trace_id,
                    stream_lines_relayed=stream_lines_relayed,
                    client_connected=client_connected,
                    stream_error=error_text or "",
                    status=status,
                )

                settle_state, settle_breakdown = self._finalize_reservation(
                    reserved=reserved,
                    trace_id=trace_id,
                    proven_cost=proven_cost,
                    profile=profile,
                    usage=_UsageEvidence(
                        completion_tokens=out_tokens,
                        reasoning_tokens=reasoning_tokens,
                        cost=proven_cost,
                        prompt_tokens=prompt_tokens,
                        cached_tokens=cached_prompt_tokens,
                        cache_write_tokens=0,
                    ),
                    billed_input_ub=in_tokens_ub,
                )
                reserved = False
                return

            # 8a) non-stream settle using actual/derived evidence, else retain.
            status = int(upstream.status)
            response_body = upstream.body if upstream.body is not None else b""

            response_payload = _parse_json_object(response_body)
            if isinstance(response_payload.get("model"), str):
                model_evidence = str(response_payload["model"])
                self._check_upstream_identity(profile, model_evidence, trace_id)
            if "provider" in response_payload:
                provider_slugs, quantizations = _provider_evidence(response_payload.get("provider"))

            usage_payload = response_payload.get("usage")
            ev = _usage_evidence(usage_payload)
            if ev.completion_tokens:
                out_tokens = ev.completion_tokens
            if ev.reasoning_tokens:
                reasoning_tokens = ev.reasoning_tokens
            if ev.prompt_tokens:
                prompt_tokens = ev.prompt_tokens
            if ev.cached_tokens:
                cached_prompt_tokens = ev.cached_tokens
            proven_cost = ev.cost

            settle_state, settle_breakdown = self._finalize_reservation(
                reserved=reserved,
                trace_id=trace_id,
                proven_cost=proven_cost,
                profile=profile,
                usage=ev,
                billed_input_ub=in_tokens_ub,
            )
            reserved = False

            handler.send_response(status)
            self._copy_upstream_headers(
                handler,
                upstream.headers,
                default_content_type="application/json",
            )
            handler.send_header("Content-Length", str(len(response_body)))
            handler.send_header("X-WeVibe-Trace-Id", trace_id)
            handler.end_headers()
            if response_body:
                handler.wfile.write(response_body)
            return

        except Exception as exc:  # noqa: BLE001 - fail closed + retain uncertainty.
            if reserved:
                try:
                    self.ledger.retain_unproven(trace_id)
                    settle_state = "retained_unproven"
                except Exception as retain_exc:  # noqa: BLE001
                    if error_text:
                        error_text = f"{error_text}; retain_unproven_failed={retain_exc}"
                    else:
                        error_text = f"retain_unproven_failed={retain_exc}"
                reserved = False

            if not error_text:
                error_text = f"upstream failure: {exc}"
            else:
                error_text = f"{error_text}; exception={exc}"

            status = 502
            try:
                self._send_error(
                    handler,
                    status=502,
                    message="upstream transport failure",
                    error_type="api_error",
                    code="upstream_failure",
                    trace_id=trace_id,
                )
            except Exception:  # noqa: BLE001 - client may already be gone.
                pass
            return

        finally:
            if reserved:
                try:
                    self.ledger.retain_unproven(trace_id)
                    settle_state = "retained_unproven"
                except Exception as retain_exc:  # noqa: BLE001
                    if error_text:
                        error_text = f"{error_text}; retain_unproven_failed={retain_exc}"
                    else:
                        error_text = f"retain_unproven_failed={retain_exc}"

            snapshot = self.ledger.snapshot()
            finished_at = datetime.datetime.now(datetime.timezone.utc)
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)

            self.logger.event(
                trace_id=trace_id,
                ordinal=ordinal,
                model=model_evidence,
                provider_slugs=provider_slugs,
                quantizations=quantizations,
                in_bytes=in_bytes,
                in_tokens_ub=in_tokens_ub,
                cached_in_tokens_ub=cached_in_tokens_ub,
                out_tokens=out_tokens,
                reasoning_tokens=reasoning_tokens,
                reserved_usd=reserved_usd,
                accrued_usd=snapshot.get("accrued", 0.0),
                accrued_derived_usd=snapshot.get("accrued_derived", 0.0),
                committed_unproven_usd=snapshot.get("committed_unproven", 0.0),
                remaining_usd=snapshot.get("remaining", 0.0),
                derived_uncached_input_usd=(
                    0.0 if settle_breakdown is None else float(settle_breakdown.get("uncached_input_usd", 0.0))
                ),
                derived_cache_read_usd=(
                    0.0 if settle_breakdown is None else float(settle_breakdown.get("cache_read_usd", 0.0))
                ),
                derived_cache_write_usd=(
                    0.0 if settle_breakdown is None else float(settle_breakdown.get("cache_write_usd", 0.0))
                ),
                derived_output_usd=(
                    0.0 if settle_breakdown is None else float(settle_breakdown.get("output_usd", 0.0))
                ),
                settle_state=settle_state,
                status=status,
                duration_ms=duration_ms,
                error=error_text,
                upstream_key_fp=self._upstream_key_fp,
                token_fp=token_fp,
            )


def make_server(proxy: ProxyServer, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Create a loopback-bound threaded HTTP server for proxy requests."""

    handler_cls = proxy.build_handler_class()
    return ThreadingHTTPServer((host, int(port)), handler_cls)


def _default_log_path() -> str:
    stamp = (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return os.path.join("runs", "openrouter-proxy", f"{stamp}.log")


def _write_token_file(path: str, token: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def _assert_expected_upstream_key_fp(profile: Any, key_fp: str) -> None:
    """Fail fast when a profile pins upstream key fingerprint and it differs."""

    expected = getattr(profile, "expected_upstream_key_fp", None)
    if expected is None:
        return

    expected_fp = str(expected).strip()
    observed_fp = str(key_fp).strip()
    if expected_fp == observed_fp:
        return

    profile_name = str(getattr(profile, "name", "unknown"))
    raise RuntimeError(
        "UPSTREAM KEY FINGERPRINT MISMATCH "
        f"(profile={profile_name!r}, expected={expected_fp!r}, observed={observed_fp!r})"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for running the local OpenRouter benchmark proxy."""

    parser = argparse.ArgumentParser(description="Run the wevibe-bench OpenRouter proxy server")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--profile",
        required=True,
        choices=("glm", "mimo", "mimo25", "hy3", "kimicode", "kimik3", "ring", "opus", "bigpickle"),
    )
    parser.add_argument("--provider-order", default=None)
    parser.add_argument("--provider-quant", default=None)
    parser.add_argument("--cap-usd", type=float, required=True)
    parser.add_argument("--target-usd", type=float, default=None)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--log", default=_default_log_path())
    parser.add_argument("--max-output-tokens", type=int, required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--auth-path", default=DEFAULT_OPENCODE_AUTH_PATH)
    parser.add_argument("--authorize", action="store_true")
    parser.add_argument(
        "--pricing-input",
        type=float,
        default=None,
        help="USD per 1M input tokens, live-verified",
    )
    parser.add_argument("--pricing-output", type=float, default=None)
    parser.add_argument("--pricing-cache-read", type=float, default=None)
    parser.add_argument("--pricing-cache-write", type=float, default=None)
    parser.add_argument("--reject-on-equality", action="store_true")
    parser.add_argument(
        "--no-hard-cap",
        action="store_true",
        help=(
            "Budget-watch mode (R2 Amendment 1, Walter 2026-07-25): the hard cap "
            "becomes a watch-threshold, never a kill-switch — reservation/"
            "settlement accounting runs unchanged but no request is ever refused "
            "on budget. Spend is watched by the poller + coordinator."
        ),
    )
    args = parser.parse_args(argv)

    if args.cap_usd <= 0:
        parser.error("--cap-usd must be > 0")
    if args.target_usd is not None and args.target_usd <= 0:
        parser.error("--target-usd must be > 0 when provided")
    if args.max_output_tokens <= 0:
        parser.error("--max-output-tokens must be > 0")
    if args.authorize and (
        args.pricing_input is None
        # Walter-directed truthful zero-pricing: free-tier upstreams can be 0.0.
        or args.pricing_input < 0
        or args.pricing_output is None
        or args.pricing_output < 0
        or args.pricing_cache_read is None
        or args.pricing_cache_read < 0
    ):
        parser.error(
            "--authorize requires --pricing-input, --pricing-output, and --pricing-cache-read >= 0 (live-verified)"
        )

    profiles = DEFAULT_PROFILES()
    selected_profile = profiles[args.profile]
    if selected_profile.upstream != "openrouter":
        if args.provider_order is not None:
            parser.error(
                f"--profile {args.profile!r} uses upstream {selected_profile.upstream!r}; "
                "--provider-order is incompatible"
            )
        if args.provider_quant is not None:
            parser.error(
                f"--profile {args.profile!r} uses upstream {selected_profile.upstream!r}; "
                "--provider-quant is incompatible"
            )
    elif selected_profile.provider_object is None:
        if args.provider_order is None or not str(args.provider_order).strip():
            parser.error(
                f"--profile {args.profile!r} requires --provider-order because it has no hardcoded provider pin"
            )

        provider_slug = str(args.provider_order).strip()
        provider_object: dict[str, Any] = {
            "order": [provider_slug],
            "only": [provider_slug],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        if args.provider_quant is not None:
            quant = str(args.provider_quant).strip()
            if not quant:
                parser.error("--provider-quant must be non-empty when provided")
            provider_object["quantizations"] = [quant]

        selected_profile = dataclasses.replace(selected_profile, provider_object=provider_object)
        profiles[args.profile] = selected_profile
    else:
        if args.provider_order is not None:
            parser.error(
                f"--profile {args.profile!r} has a hardcoded provider pin; --provider-order is not allowed"
            )
        if args.provider_quant is not None:
            parser.error("--provider-quant requires --provider-order")

    try:
        upstream_url = UPSTREAM_CHAT_COMPLETIONS_URLS[selected_profile.upstream]
    except KeyError:
        parser.error(
            f"unknown upstream {selected_profile.upstream!r} configured for --profile {args.profile!r}"
        )

    try:
        upstream_key = load_upstream_key(selected_profile.upstream, args.auth_path)
    except CredentialError as exc:
        parser.error(str(exc))
    upstream_key_fp = key_fingerprint(upstream_key)

    if args.authorize:
        pricing: dict[str, float] = {
            "input": float(args.pricing_input),
            "output": float(args.pricing_output),
            "cache_read": float(args.pricing_cache_read),
        }
        if args.pricing_cache_write is not None:
            pricing["cache_write"] = float(args.pricing_cache_write)

        selected_profile = dataclasses.replace(
            selected_profile,
            pricing=pricing,
            authorized=True,
        )
        profiles[args.profile] = selected_profile
    _assert_expected_upstream_key_fp(selected_profile, upstream_key_fp)
    registry = ProfileRegistry(profiles)

    normalized_model = normalize_model_selector(args.model)
    if normalized_model != selected_profile.model_id:
        parser.error(
            f"--model {args.model!r} does not match --profile {args.profile!r} model "
            f"{selected_profile.model_id!r}"
        )

    run_token = secrets.token_urlsafe(32)
    _write_token_file(args.token_file, run_token)

    ledger = BudgetLedger(
        run_id=args.run_id,
        model_id=normalized_model,
        profile_name=args.profile,
        hard_cap_usd=float(args.cap_usd),
        checkpoint_path=args.checkpoint,
        operational_target_usd=args.target_usd,
        reject_on_equality=bool(args.reject_on_equality),
        watch_only=bool(args.no_hard_cap),
    )
    logger = ProxyLogger(args.log)
    if args.authorize and selected_profile.upstream == "orcarouter":
        try:
            pricing_payload = fetch_orcarouter_pricing()
            verify_orcarouter_pricing_gate(
                pricing_payload,
                profile_model=selected_profile.model_id,
                expected_pricing=selected_profile.pricing,
            )
        except PricingGateError as exc:
            logger.event(
                event="pricing_gate",
                status="failed",
                upstream="orcarouter",
                pricing_version_pin=ORCAROUTER_PRICING_VERSION_PIN,
                upstream_key_fp=upstream_key_fp,
                error=str(exc),
            )
            print(
                f"orcarouter pricing gate FAILED; refusing paid calls: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 2
        logger.event(
            event="pricing_gate",
            status="ok",
            upstream="orcarouter",
            pricing_version=ORCAROUTER_PRICING_VERSION_PIN,
            upstream_key_fp=upstream_key_fp,
        )

    proxy = ProxyServer(
        registry=registry,
        profile_name=args.profile,
        ledger=ledger,
        upstream_key=upstream_key,
        run_token=run_token,
        logger=logger,
        max_tokens_cap=int(args.max_output_tokens),
        upstream_url=upstream_url,
    )
    logger.event(
        event="proxy_start",
        run_id=args.run_id,
        profile=args.profile,
        model=normalized_model,
        authorized=bool(args.authorize),
        pricing_present=bool(args.authorize),
        port=int(args.port),
        provider=selected_profile.provider_object,
        upstream_provider=selected_profile.upstream,
        upstream_url=upstream_url,
        upstream_key_fp=upstream_key_fp,
        budget_watch_only=bool(args.no_hard_cap),
        max_tokens_cap=int(args.max_output_tokens),
    )

    server = make_server(proxy, host="127.0.0.1", port=int(args.port))
    print(f"http://host.docker.internal:{int(args.port)}/api/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()


__all__ = [
    "ProxyServer",
    "ThreadingHTTPServer",
    "UpstreamResponse",
    "UpstreamTransport",
    "main",
    "make_server",
    "urllib_upstream_transport",
]
