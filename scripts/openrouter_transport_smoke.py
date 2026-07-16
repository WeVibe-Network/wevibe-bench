"""Run one minimal real-transport smoke request through the local OpenRouter proxy."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from wevibe_bench.adapters.openrouter_proxy import key_fingerprint


def _utc_stamp() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_log_path() -> str:
    return os.path.join("runs", "openrouter-proxy", f"smoke-{_utc_stamp()}.log")


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


def _append_log_json_line(path: str, line: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one OpenRouter proxy transport smoke request")
    parser.add_argument("--proxy-base-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=16)
    parser.add_argument("--prompt", default="Reply with exactly: OK")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--log", default=_default_log_path())
    args = parser.parse_args(argv)

    if args.max_output_tokens <= 0:
        parser.error("--max-output-tokens must be > 0")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")

    try:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        parser.error(f"failed to read --token-file: {exc}")

    if not token:
        parser.error("--token-file must contain a non-empty token")

    request_body = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_output_tokens,
        "stream": False,
        "usage": {"include": True},
    }

    url = f"{args.proxy_base_url.rstrip('/')}/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(request_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    http_status: int | None = None
    payload: dict[str, Any] = {}
    transport_ok = False
    transport_error_message: str | None = None

    try:
        with urllib.request.urlopen(req, timeout=float(args.timeout)) as response:
            http_status = int(response.status)
            payload = _parse_json_object(response.read())
            transport_ok = True
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        payload = _parse_json_object(exc.read())
        transport_ok = True
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        transport_ok = False
        transport_error_message = str(exc)

    provider = _extract_provider(payload)
    echoed_model = payload.get("model") if isinstance(payload.get("model"), str) else None
    finish_reason, content_present, content_len = _extract_message_metrics(payload)

    usage = payload.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = _to_int(usage.get("prompt_tokens"))
        completion_tokens = _to_int(usage.get("completion_tokens"))
        reasoning_tokens = _to_int(usage.get("reasoning_tokens"))
        if reasoning_tokens is None:
            output_details = usage.get("output_tokens_details")
            if isinstance(output_details, dict):
                reasoning_tokens = _to_int(output_details.get("reasoning_tokens"))
        cost_usd = _extract_cost_usd(usage)
    else:
        prompt_tokens = None
        completion_tokens = None
        reasoning_tokens = None
        cost_usd = None

    error_code, error_message = _extract_error_fields(payload)
    if error_message is None:
        error_message = transport_error_message

    completion_ok = transport_ok and http_status == 200 and content_present

    summary: dict[str, Any] = {
        "timestamp": _utc_stamp(),
        "model": echoed_model or args.model,
        "http_status": http_status,
        "provider": provider,
        "transport_ok": transport_ok,
        "completion_ok": completion_ok,
        "finish_reason": finish_reason,
        "content_present": content_present,
        "content_len": content_len,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cost_usd": cost_usd,
        "error_code": error_code,
        "error_message": error_message,
        "token_fp": key_fingerprint(token),
    }

    line = json.dumps(summary, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    print(line)
    _append_log_json_line(args.log, line)

    if completion_ok:
        return 0
    if transport_ok:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
