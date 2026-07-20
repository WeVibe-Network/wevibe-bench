"""Stage-2 provider reliability probe for OpenRouter candidate models.

This script uses the free unauthenticated endpoint:
GET {base_url}/models/{slug}/endpoints

Dry-run mode still performs real HTTP fetches unless ``--base-url`` is overridden
to a local/fake server.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OUT_DIR = os.path.join("runs", "qualification")
DEFAULT_WINDOW_SECONDS = 1800.0
DEFAULT_INTERVAL_SECONDS = 300.0
MAX_WINDOW_SECONDS = 24 * 60 * 60
HTTP_TIMEOUT_SECONDS = 30.0

DEFAULT_SLUGS: tuple[str, ...] = (
    "z-ai/glm-5.2",
    "xiaomi/mimo-v2.5-pro",
    "xiaomi/mimo-v2.5",
    "tencent/hy3",
    "moonshotai/kimi-k2.7-code",
    "inclusionai/ring-2.6-1t",
)


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _utc_iso() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_stamp() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


def _sanitize_slug(slug: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-")
    return cleaned or "slug"


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


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])

    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return float(ordered[low])

    fraction = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * fraction)


def _routing_tier_for_uptime(uptime: float) -> str:
    if uptime >= 99.0:
        return "Normal"
    if uptime >= 95.0:
        return "Degraded"
    return "Down"


def _default_provider_state() -> dict[str, Any]:
    return {
        "quantization": None,
        "uptime_samples": [],
        "routing_tier_observed": [],
        "max_completion_tokens": None,
        "price_in": None,
        "price_out": None,
        "fetch_latency_samples": [],
    }


def _default_candidate_state() -> dict[str, Any]:
    return {
        "providers": {},
        "model_level_errors": [],
        "provider_level_errors": [],
    }


def _extract_endpoints(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, dict):
        endpoints = data.get("endpoints")
        if isinstance(endpoints, list):
            return [entry for entry in endpoints if isinstance(entry, dict)]

    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]

    endpoints = payload.get("endpoints")
    if isinstance(endpoints, list):
        return [entry for entry in endpoints if isinstance(entry, dict)]

    return []


def _extract_provider_slug(endpoint: dict[str, Any]) -> str | None:
    for key in ("provider_slug", "tag", "provider_tag"):
        value = endpoint.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    provider_value = endpoint.get("provider")
    if isinstance(provider_value, dict):
        for key in ("slug", "tag", "name"):
            value = provider_value.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif isinstance(provider_value, str) and provider_value.strip():
        return provider_value.strip()

    provider_name = endpoint.get("provider_name")
    if isinstance(provider_name, str) and provider_name.strip():
        return provider_name.strip()
    return None


def _extract_price_in_out(pricing: Any) -> tuple[float | None, float | None]:
    if not isinstance(pricing, dict):
        return None, None

    price_in: float | None = None
    price_out: float | None = None
    for key in ("prompt", "input", "price_in", "input_price"):
        candidate = _to_float(pricing.get(key))
        if candidate is not None:
            price_in = candidate
            break
    for key in ("completion", "output", "price_out", "output_price"):
        candidate = _to_float(pricing.get(key))
        if candidate is not None:
            price_out = candidate
            break
    return price_in, price_out


def _record_error(
    target: list[dict[str, Any]],
    *,
    error_class: str,
    message: str,
    timestamp: str,
) -> None:
    for entry in target:
        if entry.get("class") == error_class and entry.get("message") == message:
            entry["count"] = int(entry.get("count", 0)) + 1
            timestamps = entry.get("timestamps")
            if not isinstance(timestamps, list):
                timestamps = []
                entry["timestamps"] = timestamps
            timestamps.append(timestamp)
            return

    target.append(
        {
            "class": error_class,
            "message": message,
            "count": 1,
            "timestamps": [timestamp],
        }
    )


def _extract_http_error_message(status_code: int, payload: dict[str, Any], fallback: str) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return f"HTTP {status_code}: {msg.strip()}"
    if fallback.strip():
        return f"HTTP {status_code}: {fallback.strip()}"
    return f"HTTP {status_code}"


def _fetch_endpoints(*, base_url: str, slug: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/models/{slug}/endpoints"
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = _parse_json_object(response.read())
            latency_ms = (time.perf_counter() - started) * 1000.0
            return {
                "status": int(response.status),
                "payload": payload,
                "latency_ms": latency_ms,
                "error_class": None,
                "error_message": None,
            }
    except urllib.error.HTTPError as exc:
        payload = _parse_json_object(exc.read())
        latency_ms = (time.perf_counter() - started) * 1000.0
        return {
            "status": int(exc.code),
            "payload": payload,
            "latency_ms": latency_ms,
            "error_class": exc.__class__.__name__,
            "error_message": _extract_http_error_message(int(exc.code), payload, str(exc)),
        }
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        message = str(exc).strip() or exc.__class__.__name__
        return {
            "status": None,
            "payload": {},
            "latency_ms": latency_ms,
            "error_class": exc.__class__.__name__,
            "error_message": message,
        }


def _record_success(
    candidate_state: dict[str, Any],
    payload: dict[str, Any],
    latency_ms: float,
) -> int:
    endpoints = _extract_endpoints(payload)
    providers = candidate_state["providers"]

    for endpoint in endpoints:
        provider_slug = _extract_provider_slug(endpoint)
        if provider_slug is None:
            continue

        provider_state = providers.setdefault(provider_slug, _default_provider_state())

        quantization = endpoint.get("quantization")
        if isinstance(quantization, str) and quantization.strip():
            provider_state["quantization"] = quantization.strip()

        uptime = _to_float(endpoint.get("uptime_last_30m"))
        if uptime is not None:
            provider_state["uptime_samples"].append(uptime)
            provider_state["routing_tier_observed"].append(_routing_tier_for_uptime(uptime))

        max_completion_tokens = _to_int(endpoint.get("max_completion_tokens"))
        if max_completion_tokens is not None:
            provider_state["max_completion_tokens"] = max_completion_tokens

        price_in, price_out = _extract_price_in_out(endpoint.get("pricing"))
        if price_in is not None:
            provider_state["price_in"] = price_in
        if price_out is not None:
            provider_state["price_out"] = price_out

        provider_state["fetch_latency_samples"].append(float(latency_ms))

    return len(endpoints)


def _record_fetch_error(candidate_state: dict[str, Any], fetch: dict[str, Any]) -> int:
    status = fetch.get("status")
    error_class = str(fetch.get("error_class") or "RequestError")
    error_message = str(fetch.get("error_message") or "unknown fetch failure")
    timestamp = _utc_iso()

    if status == 404:
        target = candidate_state["model_level_errors"]
    elif isinstance(status, int) and status >= 500:
        target = candidate_state["provider_level_errors"]
    elif status is None:
        target = candidate_state["provider_level_errors"]
    else:
        target = candidate_state["model_level_errors"]

    _record_error(target, error_class=error_class, message=error_message, timestamp=timestamp)
    return 1


def _finalize_providers(candidate_state: dict[str, Any]) -> list[dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = candidate_state["providers"]
    rows: list[dict[str, Any]] = []

    for provider_slug in sorted(providers):
        provider_state = providers[provider_slug]
        uptime_samples = [float(value) for value in provider_state.get("uptime_samples", [])]
        latency_samples = [float(value) for value in provider_state.get("fetch_latency_samples", [])]

        uptime_min: float | None = min(uptime_samples) if uptime_samples else None
        uptime_mean: float | None = (sum(uptime_samples) / len(uptime_samples)) if uptime_samples else None

        rows.append(
            {
                "provider_slug": provider_slug,
                "quantization": provider_state.get("quantization"),
                "uptime_samples": uptime_samples,
                "uptime_observed_min": uptime_min,
                "uptime_observed_mean": uptime_mean,
                "routing_tier_observed": list(provider_state.get("routing_tier_observed", [])),
                "max_completion_tokens": provider_state.get("max_completion_tokens"),
                "price_in": provider_state.get("price_in"),
                "price_out": provider_state.get("price_out"),
                "fetch_latency_ms": {
                    "p50": _percentile(latency_samples, 50.0),
                    "p90": _percentile(latency_samples, 90.0),
                },
            }
        )

    return rows


def _recommend_pin(provider_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    normal_rows: list[dict[str, Any]] = []
    for row in provider_rows:
        uptime_mean = _to_float(row.get("uptime_observed_mean"))
        if uptime_mean is None:
            continue
        if _routing_tier_for_uptime(uptime_mean) == "Normal":
            normal_rows.append(row)

    if not normal_rows:
        return None

    def sort_key(row: dict[str, Any]) -> tuple[int, float, int, str]:
        max_completion = row.get("max_completion_tokens")
        has_32k = isinstance(max_completion, int) and max_completion >= 32768
        latency = _to_float(row.get("fetch_latency_ms", {}).get("p50"))
        latency_value = latency if latency is not None else float("inf")
        max_completion_value = int(max_completion) if isinstance(max_completion, int) else -1
        return (0 if has_32k else 1, latency_value, -max_completion_value, str(row.get("provider_slug", "")))

    selected = sorted(normal_rows, key=sort_key)[0]
    selected_uptime = float(selected.get("uptime_observed_mean"))
    max_completion = selected.get("max_completion_tokens")
    meets_32k = isinstance(max_completion, int) and max_completion >= 32768
    p50_value = _to_float(selected.get("fetch_latency_ms", {}).get("p50"))
    p50_render = "n/a" if p50_value is None else f"{p50_value:.3f}"
    token_render = str(max_completion) if isinstance(max_completion, int) else "unknown"
    token_clause = "meets" if meets_32k else "below"
    reason = (
        f"Normal tier by observed mean uptime {selected_uptime:.3f}; "
        f"{token_clause} max_completion_tokens>=32768 (value={token_render}); "
        f"lowest p50 fetch latency {p50_render} ms among Normal-tier providers."
    )

    return {
        "provider_slug": selected.get("provider_slug"),
        "quantization": selected.get("quantization"),
        "reason": reason,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}"
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    tmp_path.write_text(rendered, encoding="utf-8")
    os.replace(tmp_path, path)


def _emit_progress(
    *,
    log_path: Path,
    trace: str,
    slug: str,
    tick_idx: int,
    tick_total: int,
    providers: int,
    errors: int,
) -> None:
    line = (
        f"[{_utc_iso()}] PROGRESS trace={trace} slug={slug} "
        f"tick {tick_idx}/{tick_total} providers={providers} errors={errors}"
    )
    print(line, flush=True)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")
        handle.flush()


def _ticks_planned(window_seconds: float, interval_seconds: float, dry_run: bool) -> int:
    if dry_run:
        return 1
    return max(1, int(math.ceil(window_seconds / interval_seconds)))


def _dedupe_preserve_order(slugs: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for slug in slugs:
        cleaned = slug.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def _completed_pairs_from_checkpoint(
    checkpoint: dict[str, Any],
    *,
    valid_slugs: set[str],
    ticks_planned: int,
) -> set[tuple[int, str]]:
    completed: set[tuple[int, str]] = set()
    raw_entries = checkpoint.get("completed_pairs")
    if not isinstance(raw_entries, list):
        return completed

    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        tick = _to_int(entry.get("tick"))
        slug = entry.get("slug")
        if tick is None or not isinstance(slug, str):
            continue
        if tick < 1 or tick > ticks_planned or slug not in valid_slugs:
            continue
        completed.add((tick, slug))

    return completed


def _persist_checkpoint(
    *,
    checkpoint_path: Path,
    trace: str,
    run_stamp: str,
    started_at: str,
    ended_at: str | None,
    ticks_planned: int,
    window_seconds: float,
    interval_seconds: float,
    dry_run: bool,
    base_url: str,
    slugs: list[str],
    completed_pairs: set[tuple[int, str]],
    candidates: dict[str, dict[str, Any]],
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "trace": trace,
        "run_stamp": run_stamp,
        "window_started_at": started_at,
        "window_ended_at": ended_at,
        "ticks_planned": ticks_planned,
        "window_seconds": window_seconds,
        "interval_seconds": interval_seconds,
        "dry_run": dry_run,
        "base_url": base_url,
        "slugs": slugs,
        "completed_pairs": [
            {"tick": tick, "slug": slug} for tick, slug in sorted(completed_pairs, key=lambda item: (item[0], item[1]))
        ],
        "candidates": candidates,
        "updated_at": _utc_iso(),
    }
    _atomic_write_json(checkpoint_path, payload)


def _load_checkpoint(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"failed to read checkpoint {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"checkpoint is invalid JSON at {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"checkpoint must decode to object at {path}")
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Stage-2 provider reliability evidence from OpenRouter endpoints listings.")
    parser.add_argument(
        "--slug",
        action="append",
        default=None,
        help="Candidate model slug. Repeatable. Defaults to the Stage-2 roster candidates.",
    )
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=DEFAULT_WINDOW_SECONDS,
        help=f"Observation window in seconds (0 < value <= {MAX_WINDOW_SECONDS}).",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Interval between observation ticks in seconds (must be > 0).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="OpenRouter API base URL. Override for tests/fakes.",
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help="Output directory for logs, checkpoint, and evidence JSON.",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from an existing checkpoint in --out-dir.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch each slug exactly once, write evidence, and exit. This still performs real HTTP unless --base-url points to a local fake server.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.window_seconds <= 0 or args.window_seconds > MAX_WINDOW_SECONDS:
        parser.error(f"--window-seconds must be > 0 and <= {MAX_WINDOW_SECONDS}")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be > 0")

    selected_slugs = _dedupe_preserve_order(args.slug if args.slug is not None else list(DEFAULT_SLUGS))
    if not selected_slugs:
        parser.error("at least one slug is required")

    ticks_planned = _ticks_planned(float(args.window_seconds), float(args.interval_seconds), bool(args.dry_run))
    base_url = str(args.base_url).strip().rstrip("/")
    if not base_url:
        parser.error("--base-url must be non-empty")

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "stage2-checkpoint.json"

    trace = uuid.uuid4().hex[:12]
    run_stamp = _utc_stamp()
    started_at = _utc_iso()
    candidates: dict[str, dict[str, Any]] = {slug: _default_candidate_state() for slug in selected_slugs}
    completed_pairs: set[tuple[int, str]] = set()

    if args.resume:
        if not checkpoint_path.is_file():
            parser.error(f"--resume requested but checkpoint not found: {checkpoint_path}")
        checkpoint = _load_checkpoint(checkpoint_path)

        if checkpoint.get("schema_version") != SCHEMA_VERSION:
            parser.error(f"unsupported checkpoint schema_version: {checkpoint.get('schema_version')!r}")

        if checkpoint.get("slugs") != selected_slugs:
            parser.error("checkpoint slug list does not match current --slug set/order")
        if checkpoint.get("base_url") != base_url:
            parser.error("checkpoint base_url does not match --base-url")
        if _to_float(checkpoint.get("window_seconds")) != float(args.window_seconds):
            parser.error("checkpoint window_seconds does not match --window-seconds")
        if _to_float(checkpoint.get("interval_seconds")) != float(args.interval_seconds):
            parser.error("checkpoint interval_seconds does not match --interval-seconds")
        if bool(checkpoint.get("dry_run")) != bool(args.dry_run):
            parser.error("checkpoint dry_run does not match --dry-run")
        if _to_int(checkpoint.get("ticks_planned")) != ticks_planned:
            parser.error("checkpoint ticks_planned does not match computed ticks for current settings")

        trace = str(checkpoint.get("trace") or trace)
        run_stamp = str(checkpoint.get("run_stamp") or run_stamp)
        started_at = str(checkpoint.get("window_started_at") or started_at)

        checkpoint_candidates = checkpoint.get("candidates")
        if isinstance(checkpoint_candidates, dict):
            for slug in selected_slugs:
                loaded_state = checkpoint_candidates.get(slug)
                if isinstance(loaded_state, dict):
                    candidates[slug] = loaded_state

        completed_pairs = _completed_pairs_from_checkpoint(
            checkpoint,
            valid_slugs=set(selected_slugs),
            ticks_planned=ticks_planned,
        )

    log_path = out_dir / f"stage2-{run_stamp}.log"

    for tick_idx in range(1, ticks_planned + 1):
        fetched_this_tick = False
        for slug in selected_slugs:
            if (tick_idx, slug) in completed_pairs:
                _emit_progress(
                    log_path=log_path,
                    trace=trace,
                    slug=slug,
                    tick_idx=tick_idx,
                    tick_total=ticks_planned,
                    providers=0,
                    errors=0,
                )
                continue

            fetched_this_tick = True
            fetch = _fetch_endpoints(base_url=base_url, slug=slug)

            provider_count = 0
            error_count = 0
            if fetch.get("status") == 200 and fetch.get("error_class") is None:
                provider_count = _record_success(candidates[slug], fetch.get("payload", {}), float(fetch.get("latency_ms", 0.0)))
            else:
                error_count = _record_fetch_error(candidates[slug], fetch)

            completed_pairs.add((tick_idx, slug))

            _emit_progress(
                log_path=log_path,
                trace=trace,
                slug=slug,
                tick_idx=tick_idx,
                tick_total=ticks_planned,
                providers=provider_count,
                errors=error_count,
            )

            _persist_checkpoint(
                checkpoint_path=checkpoint_path,
                trace=trace,
                run_stamp=run_stamp,
                started_at=started_at,
                ended_at=None,
                ticks_planned=ticks_planned,
                window_seconds=float(args.window_seconds),
                interval_seconds=float(args.interval_seconds),
                dry_run=bool(args.dry_run),
                base_url=base_url,
                slugs=selected_slugs,
                completed_pairs=completed_pairs,
                candidates=candidates,
            )

        _persist_checkpoint(
            checkpoint_path=checkpoint_path,
            trace=trace,
            run_stamp=run_stamp,
            started_at=started_at,
            ended_at=None,
            ticks_planned=ticks_planned,
            window_seconds=float(args.window_seconds),
            interval_seconds=float(args.interval_seconds),
            dry_run=bool(args.dry_run),
            base_url=base_url,
            slugs=selected_slugs,
            completed_pairs=completed_pairs,
            candidates=candidates,
        )

        if not args.dry_run and tick_idx < ticks_planned and fetched_this_tick:
            time.sleep(float(args.interval_seconds))

    ended_at = _utc_iso()

    for slug in selected_slugs:
        provider_rows = _finalize_providers(candidates[slug])
        recommended_pin = _recommend_pin(provider_rows)
        ticks_done = sum(1 for tick in range(1, ticks_planned + 1) if (tick, slug) in completed_pairs)

        notes = [
            "Observation taxonomy (local): uptime_last_30m >= 99 -> Normal; >= 95 and < 99 -> Degraded; < 95 -> Down.",
            "Error attribution (local): HTTP 404 on /models/{slug}/endpoints -> model_level_errors; HTTP 5xx and network/timeout/connection failures -> provider_level_errors.",
            "fetch_latency_ms is the HTTP GET round-trip for /models/{slug}/endpoints and is not model TTFT.",
        ]
        if args.dry_run:
            notes.append(
                "dry_run=true: each slug fetched once and exited; still real HTTP unless --base-url points to a local fake server."
            )

        evidence = {
            "schema_version": SCHEMA_VERSION,
            "stage": 2,
            "slug": slug,
            "captured_at": ended_at,
            "window": {
                "started_at": started_at,
                "ended_at": ended_at,
                "ticks_planned": ticks_planned,
                "ticks_done": ticks_done,
            },
            "providers": provider_rows,
            "model_level_errors": list(candidates[slug].get("model_level_errors", [])),
            "provider_level_errors": list(candidates[slug].get("provider_level_errors", [])),
            "recommended_pin": recommended_pin,
            "notes": notes,
            "trace": trace,
        }

        evidence_path = out_dir / f"stage2-{_sanitize_slug(slug)}-{run_stamp}.json"
        _atomic_write_json(evidence_path, evidence)

    _persist_checkpoint(
        checkpoint_path=checkpoint_path,
        trace=trace,
        run_stamp=run_stamp,
        started_at=started_at,
        ended_at=ended_at,
        ticks_planned=ticks_planned,
        window_seconds=float(args.window_seconds),
        interval_seconds=float(args.interval_seconds),
        dry_run=bool(args.dry_run),
        base_url=base_url,
        slugs=selected_slugs,
        completed_pairs=completed_pairs,
        candidates=candidates,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
