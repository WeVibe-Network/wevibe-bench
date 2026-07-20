"""Run transport smoke checks through the local OpenRouter proxy."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wevibe_bench.adapters.openrouter_proxy import key_fingerprint

_ALLOWED_CHECKS = ("shape", "streaming", "tools", "structured", "require-params")
_TOOL_NAME = "get_weather"
_UNSUPPORTED_PARAMETER_HINTS = (
    "unsupported",
    "not supported",
    "unknown parameter",
    "unsupported parameter",
    "does not support",
)


@dataclass(slots=True)
class _RequestResult:
    transport_ok: bool
    http_status: int | None
    payload: dict[str, Any]
    transport_error_message: str | None = None


@dataclass(slots=True)
class _CheckResult:
    name: str
    passed: bool
    failure_kind: str | None
    transport_ok: bool
    completion_ok: bool
    http_status: int | None
    provider: str | None
    finish_reason: str | None = None
    content_present: bool = False
    content_len: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    error_code: int | str | None = None
    error_message: str | None = None
    quantizations: set[str] = field(default_factory=set)
    details: dict[str, Any] = field(default_factory=dict)


def _utc_stamp(now: _dt.datetime | None = None) -> str:
    current = now or _dt.datetime.now(_dt.timezone.utc)
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_file_stamp(now: _dt.datetime | None = None) -> str:
    current = now or _dt.datetime.now(_dt.timezone.utc)
    return current.strftime("%Y%m%dT%H%M%SZ")


def _utc_stamp_from_epoch(epoch_s: float) -> str:
    return _utc_stamp(_dt.datetime.fromtimestamp(epoch_s, tz=_dt.timezone.utc))


def _sanitize_slug(value: str) -> str:
    pieces: list[str] = []
    last_dash = False
    for char in value.lower():
        if char.isalnum():
            pieces.append(char)
            last_dash = False
            continue
        if not last_dash:
            pieces.append("-")
        last_dash = True
    slug = "".join(pieces).strip("-")
    return slug or "model"


def _default_log_path() -> str:
    return os.path.join("runs", "openrouter-proxy", f"smoke-{_utc_stamp()}.log")


def _default_evidence_path(model_slug: str) -> str:
    safe_slug = _sanitize_slug(model_slug)
    return os.path.join("runs", "qualification", f"stage3-{safe_slug}-{_utc_file_stamp()}.json")


def _parse_json_object(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if isinstance(decoded, dict):
        return decoded
    return {}


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _extract_cost_usd(usage: dict[str, Any]) -> float | None:
    direct_cost = _to_float(usage.get("cost"))
    if direct_cost is not None:
        return direct_cost

    details = usage.get("cost_details")
    if not isinstance(details, dict):
        return None

    for key in ("cost", "total_cost", "total", "usd"):
        candidate = _to_float(details.get(key))
        if candidate is not None:
            return candidate

    subtotal = 0.0
    has_numeric = False
    for value in details.values():
        amount = _to_float(value)
        if amount is None:
            continue
        subtotal += amount
        has_numeric = True
    if has_numeric:
        return subtotal
    return None


def _extract_provider(payload: dict[str, Any]) -> str | None:
    provider = payload.get("provider")
    if isinstance(provider, str):
        return provider
    if isinstance(provider, dict):
        slug = provider.get("slug")
        if isinstance(slug, str):
            return slug
        name = provider.get("name")
        if isinstance(name, str):
            return name
        return json.dumps(provider, separators=(",", ":"), ensure_ascii=False)
    return None


def _extract_quantizations(payload: dict[str, Any]) -> set[str]:
    quantizations: set[str] = set()

    top_level = payload.get("quantization")
    if isinstance(top_level, str) and top_level:
        quantizations.add(top_level)

    provider = payload.get("provider")
    if isinstance(provider, dict):
        direct = provider.get("quantization")
        if isinstance(direct, str) and direct:
            quantizations.add(direct)
        plural = provider.get("quantizations")
        if isinstance(plural, list):
            for entry in plural:
                if isinstance(entry, str) and entry:
                    quantizations.add(entry)

    return quantizations


def _extract_message_metrics(payload: dict[str, Any]) -> tuple[str | None, bool, int]:
    finish_reason: str | None = None
    content_present = False
    content_len = 0

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return finish_reason, content_present, content_len

    first = choices[0]
    if not isinstance(first, dict):
        return finish_reason, content_present, content_len

    raw_finish = first.get("finish_reason")
    if isinstance(raw_finish, str):
        finish_reason = raw_finish

    message = first.get("message")
    if not isinstance(message, dict):
        return finish_reason, content_present, content_len

    content = message.get("content")
    if content is None:
        return finish_reason, content_present, content_len

    if isinstance(content, str):
        content_len = len(content)
    else:
        encoded = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
        content_len = len(encoded)

    content_present = content_len > 0
    return finish_reason, content_present, content_len


def _extract_usage_metrics(
    payload: dict[str, Any],
) -> tuple[int | None, int | None, int | None, int | None, float | None]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None, None, None, None

    prompt_tokens = _to_int(usage.get("prompt_tokens"))
    completion_tokens = _to_int(usage.get("completion_tokens"))
    reasoning_tokens = _to_int(usage.get("reasoning_tokens"))
    if reasoning_tokens is None:
        output_details = usage.get("output_tokens_details")
        if isinstance(output_details, dict):
            reasoning_tokens = _to_int(output_details.get("reasoning_tokens"))

    total_tokens = _to_int(usage.get("total_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    return prompt_tokens, completion_tokens, reasoning_tokens, total_tokens, _extract_cost_usd(usage)


def _extract_error_fields(payload: dict[str, Any]) -> tuple[int | str | None, str | None]:
    err = payload.get("error")
    if not isinstance(err, dict):
        return None, None

    raw_code = err.get("code")
    code: int | str | None
    if isinstance(raw_code, (int, str)):
        code = raw_code
    elif isinstance(raw_code, float) and raw_code.is_integer():
        code = int(raw_code)
    elif raw_code is None:
        code = None
    else:
        code = str(raw_code)

    raw_message = err.get("message")
    if isinstance(raw_message, str):
        message = raw_message
    elif raw_message is None:
        message = None
    else:
        message = str(raw_message)

    return code, message


def _extract_delta_content(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None

    delta = first.get("delta")
    if isinstance(delta, dict):
        value = delta.get("content")
        if isinstance(value, str):
            return value

    message = first.get("message")
    if isinstance(message, dict):
        value = message.get("content")
        if isinstance(value, str):
            return value

    return None


def _extract_first_message_content(payload: dict[str, Any]) -> Any:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    return message.get("content")


def _append_log_json_line(path: str, line: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def _write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _parse_checks(raw: str) -> list[str]:
    parsed = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not parsed:
        raise ValueError("--checks must include at least one check")

    deduped: list[str] = []
    seen: set[str] = set()
    for check in parsed:
        if check in seen:
            continue
        seen.add(check)
        deduped.append(check)

    invalid = [check for check in deduped if check not in _ALLOWED_CHECKS]
    if invalid:
        available = ",".join(_ALLOWED_CHECKS)
        bad = ",".join(invalid)
        raise ValueError(f"unknown checks: {bad}; allowed: {available}")

    return deduped


def _headers(token: str, trace: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-WeVibe-Trace-Id": trace,
    }


def _post_json(
    *,
    url: str,
    token: str,
    request_body: dict[str, Any],
    timeout: float,
    trace: str,
) -> _RequestResult:
    req = urllib.request.Request(
        url,
        data=json.dumps(request_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=_headers(token, trace),
    )

    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as response:
            return _RequestResult(
                transport_ok=True,
                http_status=int(response.status),
                payload=_parse_json_object(response.read()),
            )
    except urllib.error.HTTPError as exc:
        return _RequestResult(
            transport_ok=True,
            http_status=int(exc.code),
            payload=_parse_json_object(exc.read()),
        )
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        return _RequestResult(
            transport_ok=False,
            http_status=None,
            payload={},
            transport_error_message=str(exc),
        )


def _is_provider_limitation(code: int | str | None, message: str | None) -> bool:
    text = f"{code or ''} {message or ''}".lower()
    return any(hint in text for hint in _UNSUPPORTED_PARAMETER_HINTS)


def _tool_request_body(model: str, max_tokens: int, prompt: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": _TOOL_NAME,
                    "description": "Get weather for a location.",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": _TOOL_NAME}},
        "usage": {"include": True},
    }


def _extract_tool_call(payload: dict[str, Any], expected_name: str) -> tuple[bool, bool, str | None]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False, False, "missing choices for tool-call response"

    first = choices[0]
    if not isinstance(first, dict):
        return False, False, "invalid first choice for tool-call response"

    message = first.get("message")
    if not isinstance(message, dict):
        return False, False, "missing message object for tool-call response"

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return False, False, "missing tool_calls (silent parameter drop)"

    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        tool_name = function.get("name")
        if tool_name != expected_name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            return True, True, None
        if not isinstance(arguments, str):
            return True, False, "tool_call arguments are missing"
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError:
            return True, False, "tool_call arguments are not parseable JSON"
        if not isinstance(decoded, dict):
            return True, False, "tool_call arguments JSON must decode to an object"
        return True, True, None

    return False, False, f"tool_calls missing required function '{expected_name}'"


def _validate_structured_payload(payload: dict[str, Any]) -> tuple[bool, bool, str | None]:
    raw_content = _extract_first_message_content(payload)
    if raw_content is None:
        return False, False, "missing structured response content"

    if isinstance(raw_content, dict):
        decoded = raw_content
    elif isinstance(raw_content, str):
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError:
            return False, False, "structured response content is not valid JSON"
        if not isinstance(parsed, dict):
            return True, False, "structured response JSON is not an object"
        decoded = parsed
    else:
        return True, False, "structured response content is not JSON text"

    if set(decoded.keys()) != {"answer"}:
        return True, False, "structured response JSON must contain only 'answer'"
    if not isinstance(decoded.get("answer"), str):
        return True, False, "structured response 'answer' must be a string"
    return True, True, None


def _run_shape_check(
    *,
    url: str,
    token: str,
    model: str,
    max_tokens: int,
    prompt: str,
    timeout: float,
    trace: str,
) -> _CheckResult:
    request_body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
        "usage": {"include": True},
    }

    req = _post_json(url=url, token=token, request_body=request_body, timeout=timeout, trace=trace)
    payload = req.payload
    provider = _extract_provider(payload)
    echoed_model = payload.get("model") if isinstance(payload.get("model"), str) else None
    finish_reason, content_present, content_len = _extract_message_metrics(payload)
    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens, cost_usd = _extract_usage_metrics(payload)
    error_code, error_message = _extract_error_fields(payload)
    if error_message is None:
        error_message = req.transport_error_message

    completion_ok = req.transport_ok and req.http_status == 200 and content_present
    if not req.transport_ok:
        failure_kind = "transport"
    elif completion_ok:
        failure_kind = None
    else:
        failure_kind = "completion"

    return _CheckResult(
        name="shape",
        passed=completion_ok,
        failure_kind=failure_kind,
        transport_ok=req.transport_ok,
        completion_ok=completion_ok,
        http_status=req.http_status,
        provider=provider,
        finish_reason=finish_reason,
        content_present=content_present,
        content_len=content_len,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        error_code=error_code,
        error_message=error_message,
        quantizations=_extract_quantizations(payload),
        details={"echoed_model": echoed_model},
    )


def _run_tools_like_check(
    *,
    check_name: str,
    url: str,
    token: str,
    model: str,
    max_tokens: int,
    timeout: float,
    trace: str,
) -> _CheckResult:
    request_body = _tool_request_body(
        model=model,
        max_tokens=max_tokens,
        prompt="What is the weather in Paris? Use the tool.",
    )
    req = _post_json(url=url, token=token, request_body=request_body, timeout=timeout, trace=trace)
    payload = req.payload

    provider = _extract_provider(payload)
    finish_reason, content_present, content_len = _extract_message_metrics(payload)
    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens, cost_usd = _extract_usage_metrics(payload)
    error_code, error_message = _extract_error_fields(payload)
    if error_message is None:
        error_message = req.transport_error_message

    details: dict[str, Any] = {
        "tool_name": _TOOL_NAME,
        "tool_calls_present": False,
        "arguments_json_ok": False,
    }

    completion_ok = req.transport_ok and req.http_status == 200
    detail_text: str | None = None
    if not req.transport_ok:
        failure_kind = "transport"
        passed = False
    elif req.http_status != 200:
        if _is_provider_limitation(error_code, error_message):
            failure_kind = "assertion"
            passed = False
            detail_text = f"provider limitation: {error_message or error_code}"
        else:
            failure_kind = "completion"
            passed = False
            detail_text = error_message or f"unexpected http status {req.http_status}"
    else:
        tool_calls_present, arguments_json_ok, tool_error = _extract_tool_call(payload, _TOOL_NAME)
        details["tool_calls_present"] = tool_calls_present
        details["arguments_json_ok"] = arguments_json_ok
        if tool_calls_present and arguments_json_ok:
            failure_kind = None
            passed = True
            detail_text = "tool-call honored"
        else:
            failure_kind = "assertion"
            passed = False
            detail_text = tool_error or "tool-call contract was not honored"

    details["detail"] = detail_text

    effective_error = error_message
    if failure_kind == "assertion" and detail_text:
        effective_error = detail_text

    return _CheckResult(
        name=check_name,
        passed=passed,
        failure_kind=failure_kind,
        transport_ok=req.transport_ok,
        completion_ok=completion_ok,
        http_status=req.http_status,
        provider=provider,
        finish_reason=finish_reason,
        content_present=content_present,
        content_len=content_len,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        error_code=error_code,
        error_message=effective_error,
        quantizations=_extract_quantizations(payload),
        details=details,
    )


def _run_structured_check(
    *,
    url: str,
    token: str,
    model: str,
    max_tokens: int,
    timeout: float,
    trace: str,
) -> _CheckResult:
    request_body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Respond with JSON only: {\"answer\": \"...\"}",
            }
        ],
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "stage3_answer",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
        },
        "usage": {"include": True},
    }

    req = _post_json(url=url, token=token, request_body=request_body, timeout=timeout, trace=trace)
    payload = req.payload

    provider = _extract_provider(payload)
    finish_reason, content_present, content_len = _extract_message_metrics(payload)
    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens, cost_usd = _extract_usage_metrics(payload)
    error_code, error_message = _extract_error_fields(payload)
    if error_message is None:
        error_message = req.transport_error_message

    details: dict[str, Any] = {
        "schema_ok": False,
        "json_parse_ok": False,
        "detail": None,
    }

    completion_ok = req.transport_ok and req.http_status == 200
    if not req.transport_ok:
        failure_kind = "transport"
        passed = False
        detail_text = req.transport_error_message
    elif req.http_status != 200:
        if _is_provider_limitation(error_code, error_message):
            failure_kind = "assertion"
            passed = False
            detail_text = f"provider limitation: {error_message or error_code}"
        else:
            failure_kind = "completion"
            passed = False
            detail_text = error_message or f"unexpected http status {req.http_status}"
    else:
        json_parse_ok, schema_ok, structured_error = _validate_structured_payload(payload)
        details["json_parse_ok"] = json_parse_ok
        details["schema_ok"] = schema_ok
        if schema_ok:
            failure_kind = None
            passed = True
            detail_text = "schema honored"
        else:
            failure_kind = "assertion"
            passed = False
            detail_text = structured_error or "schema not honored"

    details["detail"] = detail_text
    effective_error = error_message
    if failure_kind == "assertion" and detail_text:
        effective_error = detail_text

    return _CheckResult(
        name="structured",
        passed=passed,
        failure_kind=failure_kind,
        transport_ok=req.transport_ok,
        completion_ok=completion_ok,
        http_status=req.http_status,
        provider=provider,
        finish_reason=finish_reason,
        content_present=content_present,
        content_len=content_len,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        error_code=error_code,
        error_message=effective_error,
        quantizations=_extract_quantizations(payload),
        details=details,
    )


def _run_require_params_check(
    *,
    mode: str,
    url: str,
    token: str,
    model: str,
    max_tokens: int,
    timeout: float,
    trace: str,
) -> _CheckResult:
    if mode == "structured":
        base = _run_structured_check(
            url=url,
            token=token,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            trace=trace,
        )
        return _CheckResult(
            name="require-params",
            passed=base.passed,
            failure_kind=base.failure_kind,
            transport_ok=base.transport_ok,
            completion_ok=base.completion_ok,
            http_status=base.http_status,
            provider=base.provider,
            finish_reason=base.finish_reason,
            content_present=base.content_present,
            content_len=base.content_len,
            prompt_tokens=base.prompt_tokens,
            completion_tokens=base.completion_tokens,
            reasoning_tokens=base.reasoning_tokens,
            total_tokens=base.total_tokens,
            cost_usd=base.cost_usd,
            error_code=base.error_code,
            error_message=base.error_message,
            quantizations=base.quantizations,
            details={
                "mode": "structured",
                "schema_ok": base.details.get("schema_ok"),
                "detail": base.details.get("detail"),
            },
        )

    base = _run_tools_like_check(
        check_name="require-params",
        url=url,
        token=token,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout,
        trace=trace,
    )
    base.details["mode"] = "tools"
    return base


def _run_streaming_check(
    *,
    url: str,
    token: str,
    model: str,
    max_tokens: int,
    timeout: float,
    trace: str,
) -> _CheckResult:
    request_body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": max_tokens,
        "stream": True,
        "usage": {"include": True},
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(request_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=_headers(token, trace),
    )

    http_status: int | None = None
    provider: str | None = None
    quantizations: set[str] = set()
    finish_reason: str | None = None
    chunk_count = 0
    done_seen = False
    first_chunk_at: float | None = None
    last_chunk_at: float | None = None
    parse_error: str | None = None
    delta_parts: list[str] = []
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    error_code: int | str | None = None
    error_message: str | None = None
    transport_ok = False

    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as response:
            transport_ok = True
            http_status = int(response.status)
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue

                data_segment = line[5:].strip()
                timestamp = time.time()
                if first_chunk_at is None:
                    first_chunk_at = timestamp
                last_chunk_at = timestamp

                if data_segment == "[DONE]":
                    done_seen = True
                    continue

                try:
                    event_payload = json.loads(data_segment)
                except json.JSONDecodeError as exc:
                    parse_error = f"stream frame not parseable JSON: {exc}"
                    break

                if not isinstance(event_payload, dict):
                    parse_error = "stream frame JSON must decode to an object"
                    break

                chunk_count += 1
                event_provider = _extract_provider(event_payload)
                if event_provider is not None:
                    provider = event_provider
                quantizations.update(_extract_quantizations(event_payload))

                event_prompt, event_completion, event_reasoning, event_total, event_cost = _extract_usage_metrics(
                    event_payload
                )
                if event_prompt is not None:
                    prompt_tokens = event_prompt
                if event_completion is not None:
                    completion_tokens = event_completion
                if event_reasoning is not None:
                    reasoning_tokens = event_reasoning
                if event_total is not None:
                    total_tokens = event_total
                if event_cost is not None:
                    cost_usd = event_cost

                choices = event_payload.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    raw_finish = choices[0].get("finish_reason")
                    if isinstance(raw_finish, str):
                        finish_reason = raw_finish

                delta_content = _extract_delta_content(event_payload)
                if isinstance(delta_content, str) and delta_content:
                    delta_parts.append(delta_content)
    except urllib.error.HTTPError as exc:
        transport_ok = True
        http_status = int(exc.code)
        payload = _parse_json_object(exc.read())
        provider = _extract_provider(payload)
        quantizations.update(_extract_quantizations(payload))
        (
            prompt_tokens,
            completion_tokens,
            reasoning_tokens,
            total_tokens,
            cost_usd,
        ) = _extract_usage_metrics(payload)
        error_code, error_message = _extract_error_fields(payload)
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        transport_ok = False
        error_message = str(exc)

    content_len = len("".join(delta_parts))
    content_present = content_len > 0
    completion_ok = transport_ok and http_status == 200 and chunk_count > 0 and done_seen and content_present

    if not transport_ok:
        failure_kind = "transport"
    elif http_status != 200:
        failure_kind = "completion"
    elif parse_error is not None:
        failure_kind = "assertion"
        error_message = parse_error
    elif chunk_count == 0:
        failure_kind = "assertion"
        error_message = "stream did not emit any parseable data frames"
    elif not done_seen:
        failure_kind = "assertion"
        error_message = "stream did not terminate with [DONE]"
    elif not content_present:
        failure_kind = "assertion"
        error_message = "stream emitted no assistant delta content"
    else:
        failure_kind = None

    duration_ms: int | None = None
    if first_chunk_at is not None and last_chunk_at is not None:
        duration_ms = int((last_chunk_at - first_chunk_at) * 1000)

    details = {
        "chunks": chunk_count,
        "done": done_seen,
        "first_chunk_at": _utc_stamp_from_epoch(first_chunk_at) if first_chunk_at is not None else None,
        "last_chunk_at": _utc_stamp_from_epoch(last_chunk_at) if last_chunk_at is not None else None,
        "duration_ms": duration_ms,
    }
    if parse_error is not None:
        details["parse_error"] = parse_error

    return _CheckResult(
        name="streaming",
        passed=failure_kind is None,
        failure_kind=failure_kind,
        transport_ok=transport_ok,
        completion_ok=completion_ok,
        http_status=http_status,
        provider=provider,
        finish_reason=finish_reason,
        content_present=content_present,
        content_len=content_len,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        error_code=error_code,
        error_message=error_message,
        quantizations=quantizations,
        details=details,
    )


def _check_summary(result: _CheckResult) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "pass": result.passed,
        "transport_ok": result.transport_ok,
        "completion_ok": result.completion_ok,
        "failure_kind": result.failure_kind,
        "http_status": result.http_status,
        "provider": result.provider,
        "total_tokens": result.total_tokens,
        "cost_usd": result.cost_usd,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }

    if result.name == "shape":
        summary.update(
            {
                "finish_reason": result.finish_reason,
                "content_present": result.content_present,
                "content_len": result.content_len,
            }
        )
    elif result.name == "streaming":
        summary.update(
            {
                "chunks": result.details.get("chunks"),
                "done": result.details.get("done"),
                "first_chunk_at": result.details.get("first_chunk_at"),
                "last_chunk_at": result.details.get("last_chunk_at"),
                "ms": result.details.get("duration_ms"),
            }
        )
    elif result.name == "tools":
        summary.update(
            {
                "tool_name": result.details.get("tool_name"),
                "tool_calls_present": result.details.get("tool_calls_present"),
                "arguments_json_ok": result.details.get("arguments_json_ok"),
                "detail": result.details.get("detail"),
            }
        )
    elif result.name == "require-params":
        summary.update(
            {
                "mode": result.details.get("mode"),
                "detail": result.details.get("detail"),
                "schema_ok": result.details.get("schema_ok"),
                "tool_name": result.details.get("tool_name"),
            }
        )
    elif result.name == "structured":
        summary.update(
            {
                "schema_ok": result.details.get("schema_ok"),
                "detail": result.details.get("detail"),
            }
        )

    return summary


def _evidence_check_summary(result: _CheckResult) -> dict[str, Any]:
    if result.name == "shape":
        return {
            "pass": result.passed,
            "transport_ok": result.transport_ok,
            "completion_ok": result.completion_ok,
            "http_status": result.http_status,
        }
    if result.name == "streaming":
        return {
            "pass": result.passed,
            "chunks": result.details.get("chunks"),
            "ms": result.details.get("duration_ms"),
            "first_chunk_at": result.details.get("first_chunk_at"),
            "last_chunk_at": result.details.get("last_chunk_at"),
        }
    if result.name == "tools":
        return {
            "pass": result.passed,
            "tool_name": result.details.get("tool_name"),
            "arguments_json_ok": result.details.get("arguments_json_ok"),
        }
    if result.name == "structured":
        return {
            "pass": result.passed,
            "schema_ok": result.details.get("schema_ok"),
        }
    return {
        "pass": result.passed,
        "detail": result.details.get("detail"),
    }


def _shape_compat_summary(result: _CheckResult, model: str, token: str) -> dict[str, Any]:
    echoed_model = result.details.get("echoed_model")
    effective_model = echoed_model if isinstance(echoed_model, str) else model
    return {
        "timestamp": _utc_stamp(),
        "model": effective_model,
        "http_status": result.http_status,
        "provider": result.provider,
        "transport_ok": result.transport_ok,
        "completion_ok": result.completion_ok,
        "finish_reason": result.finish_reason,
        "content_present": result.content_present,
        "content_len": result.content_len,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "reasoning_tokens": result.reasoning_tokens,
        "cost_usd": result.cost_usd,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "token_fp": key_fingerprint(token),
    }


def _run_check(
    check: str,
    *,
    url: str,
    token: str,
    model: str,
    max_tokens: int,
    prompt: str,
    timeout: float,
    trace: str,
    require_params_mode: str,
) -> _CheckResult:
    if check == "shape":
        return _run_shape_check(
            url=url,
            token=token,
            model=model,
            max_tokens=max_tokens,
            prompt=prompt,
            timeout=timeout,
            trace=trace,
        )
    if check == "streaming":
        return _run_streaming_check(
            url=url,
            token=token,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            trace=trace,
        )
    if check == "tools":
        return _run_tools_like_check(
            check_name="tools",
            url=url,
            token=token,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            trace=trace,
        )
    if check == "structured":
        return _run_structured_check(
            url=url,
            token=token,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            trace=trace,
        )
    if check == "require-params":
        return _run_require_params_check(
            mode=require_params_mode,
            url=url,
            token=token,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            trace=trace,
        )
    raise ValueError(f"unsupported check '{check}'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OpenRouter proxy transport smoke checks")
    parser.add_argument("--proxy-base-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=16)
    parser.add_argument("--prompt", default="Reply with exactly: OK")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--log", default=_default_log_path())
    parser.add_argument("--checks", default="shape")
    parser.add_argument("--token-budget", type=int, default=8000)
    parser.add_argument("--evidence-out")
    args = parser.parse_args(argv)

    if args.max_output_tokens <= 0:
        parser.error("--max-output-tokens must be > 0")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    if args.token_budget <= 0:
        parser.error("--token-budget must be > 0")

    try:
        checks = _parse_checks(args.checks)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        parser.error(f"failed to read --token-file: {exc}")

    if not token:
        parser.error("--token-file must contain a non-empty token")

    url = f"{args.proxy_base_url.rstrip('/')}/chat/completions"
    trace = f"smoke-{_utc_file_stamp()}-{os.getpid()}"
    if "tools" in checks:
        require_params_mode = "tools"
    elif "structured" in checks:
        require_params_mode = "structured"
    else:
        require_params_mode = "tools"

    ordered_results: dict[str, _CheckResult] = {}
    skipped_checks: list[str] = []
    provider_slugs: set[str] = set()
    quantizations: set[str] = set()
    errors: list[str] = []
    tokens_used_total = 0
    budget_exceeded = False
    budget_ok = True
    cost_usd_total = 0.0
    saw_cost = False

    for idx, check in enumerate(checks):
        planned_tokens = max(1, int(args.max_output_tokens))
        if tokens_used_total + planned_tokens > args.token_budget:
            budget_exceeded = True
            skipped_checks.extend(checks[idx:])
            break

        result = _run_check(
            check,
            url=url,
            token=token,
            model=args.model,
            max_tokens=args.max_output_tokens,
            prompt=args.prompt,
            timeout=args.timeout,
            trace=trace,
            require_params_mode=require_params_mode,
        )
        ordered_results[check] = result

        if result.provider:
            provider_slugs.add(result.provider)
        quantizations.update(result.quantizations)

        if result.total_tokens is not None:
            tokens_used_total += result.total_tokens
        if result.cost_usd is not None:
            cost_usd_total += result.cost_usd
            saw_cost = True

        if result.failure_kind is not None:
            detail = result.error_message or result.details.get("detail")
            if isinstance(detail, str) and detail:
                errors.append(f"{check}: {detail}")
            else:
                errors.append(f"{check}: failed ({result.failure_kind})")

        if tokens_used_total > args.token_budget:
            budget_ok = False
            budget_exceeded = True
            skipped_checks.extend(checks[idx + 1 :])
            break

    if not budget_ok:
        errors.append(f"token budget exceeded: used={tokens_used_total} budget={args.token_budget}")

    if checks == ["shape"] and "shape" in ordered_results:
        summary = _shape_compat_summary(ordered_results["shape"], args.model, token)
    else:
        checks_summary: dict[str, Any] = {}
        for check in checks:
            result = ordered_results.get(check)
            if result is None:
                checks_summary[check] = {
                    "pass": None,
                    "skipped": True,
                    "reason": "token-budget",
                }
                continue
            checks_summary[check] = _check_summary(result)

        checks_passed = all(result.passed for result in ordered_results.values())
        transport_ok = all(result.transport_ok for result in ordered_results.values())
        completion_ok = checks_passed and budget_ok and not any(
            result.failure_kind in {"completion", "transport"} for result in ordered_results.values()
        )

        summary = {
            "timestamp": _utc_stamp(),
            "model": args.model,
            "transport_ok": transport_ok,
            "completion_ok": completion_ok,
            "checks": checks_summary,
            "tokens_used_total": tokens_used_total,
            "token_budget": args.token_budget,
            "budget_exceeded": budget_exceeded,
            "budget_ok": budget_ok,
            "cost_usd_total": cost_usd_total if saw_cost else None,
            "provider_slugs": sorted(provider_slugs),
            "quantizations": sorted(quantizations),
            "errors": errors,
            "skipped_checks": skipped_checks,
            "token_fp": key_fingerprint(token),
            "trace": trace,
        }

    evidence_path = args.evidence_out
    if evidence_path is None and any(check != "shape" for check in checks):
        evidence_path = _default_evidence_path(args.model)

    if evidence_path is not None:
        evidence_checks: dict[str, Any] = {}
        for check in checks:
            result = ordered_results.get(check)
            if result is None:
                evidence_checks[check] = {
                    "pass": None,
                    "skipped": True,
                    "reason": "token-budget",
                }
                continue
            evidence_checks[check] = _evidence_check_summary(result)

        evidence = {
            "schema_version": 1,
            "stage": 3,
            "slug": args.model,
            "captured_at": _utc_stamp(),
            "checks": evidence_checks,
            "tokens_used_total": tokens_used_total,
            "token_budget": args.token_budget,
            "budget_ok": budget_ok,
            "budget_exceeded": budget_exceeded,
            "cost_usd_total": cost_usd_total if saw_cost else None,
            "provider_slugs": sorted(provider_slugs),
            "quantizations": sorted(quantizations),
            "errors": errors,
            "trace": trace,
        }
        _write_json(evidence_path, evidence)

    line = json.dumps(summary, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    print(line)
    _append_log_json_line(args.log, line)

    if not budget_ok:
        return 4

    assertion_failed = any(result.failure_kind == "assertion" for result in ordered_results.values())
    transport_failed = any(result.failure_kind == "transport" for result in ordered_results.values())
    completion_failed = any(result.failure_kind == "completion" for result in ordered_results.values())

    if assertion_failed:
        return 4
    if transport_failed:
        return 1
    if completion_failed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
