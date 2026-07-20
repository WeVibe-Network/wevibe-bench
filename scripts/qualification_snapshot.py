"""Build a machine-readable qualification snapshot from Stage 2–5 evidence files."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from wevibe_bench.lifecycle.logging_util import new_trace_id as _new_trace_id
except Exception:  # pragma: no cover - import fallback only used in minimal contexts.

    def _new_trace_id() -> str:
        return f"qs-{uuid4().hex}"


CATALOG_SOURCE = "https://openrouter.ai/api/v1/models"
FORBIDDEN_FIELD_NAMES = {"token", "api_key", "authorization"}
STAGES = {2, 3, 4, 5}

_CANDIDATE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "slug": "z-ai/glm-5.2",
        "recommend": "PRIMARY",
        "context": 1_048_576,
        "max_out": None,
        "price_in": 0.00000042,
        "price_out": 0.00000132,
        "tool_use": True,
    },
    {
        "slug": "xiaomi/mimo-v2.5-pro",
        "recommend": "PRIMARY",
        "context": 1_048_576,
        "max_out": 131_072,
        "price_in": 0.000000435,
        "price_out": 0.00000087,
        "tool_use": True,
    },
    {"slug": "xiaomi/mimo-v2.5", "recommend": "RESERVE"},
    {"slug": "tencent/hy3", "recommend": "RESERVE"},
    {"slug": "moonshotai/kimi-k2.7-code", "recommend": "RESERVE"},
    {"slug": "inclusionai/ring-2.6-1t", "recommend": "FLOOR-ANCHOR-PROBE"},
)

_CANDIDATE_SLUGS = {spec["slug"] for spec in _CANDIDATE_SPECS}


@dataclass(frozen=True)
class EvidenceRecord:
    stage: int
    slug: str
    stamp: str
    path: Path
    payload: dict[str, Any]
    sort_key: tuple[float, str, int]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default_out_path(evidence_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return evidence_dir / f"snapshot-{stamp}.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("runs/qualification"),
        help="Evidence directory (default: runs/qualification)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path (default: <evidence-dir>/snapshot-<UTCstamp>.json)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when no evidence files are consumed.",
    )
    return parser.parse_args(argv)


def _empty_provider() -> dict[str, Any]:
    return {"provider_slug": None, "endpoint": None, "pinned_for_tests": False}


def _new_candidate(spec: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "slug": spec["slug"],
        "providers": [_empty_provider()],
        "context": None,
        "max_out": None,
        "price_in": None,
        "price_out": None,
        "uptime_pct": None,
        "uptime_window": None,
        "routing_tier": None,
        "tool_use": None,
        "streaming": None,
        "latency_p50": None,
        "latency_p90": None,
        "off_spike": None,
        "on_smoke": None,
        "goldilocks_verdict": None,
        "recommend": spec["recommend"],
        "notes": None,
    }
    for key in ("context", "max_out", "price_in", "price_out", "tool_use"):
        if key in spec:
            candidate[key] = spec[key]
    return candidate


def _strip_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _normalize_notes(notes: list[str]) -> str | None:
    seen: set[str] = set()
    cleaned: list[str] = []
    for note in notes:
        text = note.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    if not cleaned:
        return None
    return "; ".join(cleaned)


def _check_pass(check: Any) -> bool | None:
    if not isinstance(check, dict):
        return None
    return _as_bool(check.get("pass"))


def _parse_filename(path: Path) -> tuple[int, str, str] | None:
    name = path.name
    if not name.endswith(".json") or not name.startswith("stage"):
        return None

    stage_token, sep, remainder = name.partition("-")
    if not sep or stage_token not in {"stage2", "stage3", "stage4", "stage5"}:
        return None

    body = remainder[:-5]
    slug_part, sep, stamp = body.rpartition("-")
    if not sep:
        return None

    slug_part = slug_part.strip()
    stamp = stamp.strip()
    if not slug_part or not stamp:
        return None

    return int(stage_token[-1]), slug_part, stamp


def _parse_stamp_epoch(stamp: str) -> float | None:
    formats = (
        "%Y%m%dT%H%M%SZ",
        "%Y%m%dT%H%M%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(stamp, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _sort_key_for(path: Path, stamp: str) -> tuple[float, str, int]:
    parsed = _parse_stamp_epoch(stamp)
    stamp_rank = parsed if parsed is not None else float("-inf")
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return (stamp_rank, stamp, mtime_ns)


def _log_corrupt(trace_id: str, path: Path, exc: Exception) -> None:
    ts = _utc_now_iso()
    err = traceback.format_exc().rstrip()
    print(
        f"[{ts}] qualification_snapshot ERROR trace={trace_id} file={path} err={exc}",
        file=sys.stderr,
        flush=True,
    )
    if err:
        print(err, file=sys.stderr, flush=True)


def _collect_latest_evidence(evidence_dir: Path, trace_id: str) -> tuple[dict[tuple[int, str], EvidenceRecord], list[str]]:
    latest: dict[tuple[int, str], EvidenceRecord] = {}
    warnings: list[str] = []

    if not evidence_dir.exists():
        warnings.append(f"evidence directory not found: {evidence_dir}")
        return latest, warnings

    for path in sorted(evidence_dir.glob("stage*-*.json")):
        parsed = _parse_filename(path)
        if parsed is None:
            continue

        filename_stage, filename_slug, stamp = parsed
        try:
            payload_raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload_raw, dict):
                raise ValueError("root value must be a JSON object")
            payload = payload_raw
        except Exception as exc:  # noqa: BLE001 - keep tolerant behavior with full diagnostics.
            _log_corrupt(trace_id, path, exc)
            warnings.append(f"corrupt evidence skipped: {path}: {exc}")
            continue

        stage_value = payload.get("stage")
        if isinstance(stage_value, int) and stage_value in STAGES:
            stage = stage_value
        else:
            stage = filename_stage

        if stage != filename_stage:
            warnings.append(
                f"stage mismatch for {path}: filename stage={filename_stage}, payload stage={stage}; using payload"
            )

        slug = _strip_string(payload.get("slug")) or filename_slug
        if slug not in _CANDIDATE_SLUGS:
            continue

        sort_key = _sort_key_for(path, stamp)
        key = (stage, slug)
        previous = latest.get(key)
        if previous is None or sort_key > previous.sort_key:
            latest[key] = EvidenceRecord(
                stage=stage,
                slug=slug,
                stamp=stamp,
                path=path,
                payload=payload,
                sort_key=sort_key,
            )

    return latest, warnings


def _worst_routing_tier(observed: Any) -> str | None:
    if not isinstance(observed, list):
        return None

    rank = {"normal": 0, "degraded": 1, "down": 2}
    best: tuple[int, str] | None = None
    for item in observed:
        tier = _strip_string(item)
        if tier is None:
            continue
        score = rank.get(tier.lower(), 3)
        if best is None or score > best[0]:
            best = (score, tier)
    return best[1] if best is not None else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        text = _strip_string(item)
        if text is not None:
            values.append(text)
    return values


def _pinned_from_stage3(stage3: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(stage3, dict):
        return None, None
    provider_slugs = _string_list(stage3.get("provider_slugs"))
    quantizations = _string_list(stage3.get("quantizations"))
    if len(provider_slugs) != 1:
        return None, None
    quantization = quantizations[0] if len(quantizations) == 1 else None
    return provider_slugs[0], quantization


def _providers_from_stage3(stage3: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(stage3, dict):
        return []
    providers = _string_list(stage3.get("provider_slugs"))
    if not providers:
        return []

    pinned_slug, _ = _pinned_from_stage3(stage3)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for provider_slug in providers:
        if provider_slug in seen:
            continue
        seen.add(provider_slug)
        rows.append(
            {
                "provider_slug": provider_slug,
                "endpoint": provider_slug,
                "pinned_for_tests": pinned_slug is not None and provider_slug == pinned_slug,
            }
        )
    return rows


def _match_provider(
    providers: list[dict[str, Any]],
    provider_slug: str | None,
    quantization: str | None,
) -> dict[str, Any] | None:
    if provider_slug is None:
        return None
    matching = [row for row in providers if _strip_string(row.get("provider_slug")) == provider_slug]
    if not matching:
        return None
    if quantization is None:
        return matching[0]
    for row in matching:
        if _strip_string(row.get("quantization")) == quantization:
            return row
    return matching[0]


def _extract_off_spike(payload: dict[str, Any]) -> str | None:
    direct = _strip_string(payload.get("off_spike"))
    if direct is not None:
        return direct

    direct = _strip_string(payload.get("classification"))
    if direct is not None:
        return direct

    bracket = payload.get("bracket")
    if isinstance(bracket, dict):
        nested = _strip_string(bracket.get("classification")) or _strip_string(bracket.get("verdict"))
        if nested is not None:
            return nested
    return None


def _extract_on_smoke(payload: dict[str, Any]) -> str | None:
    for key in ("on_smoke", "delivery_verdict", "smoke_verdict"):
        value = _strip_string(payload.get(key))
        if value is not None:
            return value
    return None


def _apply_stage2(
    candidate: dict[str, Any],
    stage2: dict[str, Any],
    stage3: dict[str, Any] | None,
    notes: list[str],
) -> None:
    window = stage2.get("window")
    if isinstance(window, dict):
        started_at = _strip_string(window.get("started_at"))
        ended_at = _strip_string(window.get("ended_at"))
        if started_at is not None and ended_at is not None:
            candidate["uptime_window"] = f"{started_at}/{ended_at}"

    provider_rows = [row for row in stage2.get("providers", []) if isinstance(row, dict)]

    stage3_pin_slug, stage3_pin_quant = _pinned_from_stage3(stage3)
    pinned_slug = stage3_pin_slug
    pinned_quant = stage3_pin_quant

    recommended_pin = stage2.get("recommended_pin") if isinstance(stage2.get("recommended_pin"), dict) else None
    if pinned_slug is None and isinstance(recommended_pin, dict):
        pinned_slug = _strip_string(recommended_pin.get("provider_slug"))
        pinned_quant = _strip_string(recommended_pin.get("quantization"))
        if pinned_slug is not None:
            notes.append(f"using stage2 recommended_pin as pin source: {pinned_slug}")

    if isinstance(recommended_pin, dict):
        reason = _strip_string(recommended_pin.get("reason"))
        if reason is not None:
            notes.append(f"stage2 recommended_pin reason: {reason}")

    stage2_note = _strip_string(stage2.get("notes"))
    if stage2_note is not None:
        notes.append(f"stage2 notes: {stage2_note}")

    providers_out: list[dict[str, Any]] = []
    seen_provider_slugs: set[str] = set()
    for row in provider_rows:
        provider_slug = _strip_string(row.get("provider_slug"))
        if provider_slug is None or provider_slug in seen_provider_slugs:
            continue
        seen_provider_slugs.add(provider_slug)
        providers_out.append(
            {
                "provider_slug": provider_slug,
                "endpoint": provider_slug,
                "pinned_for_tests": pinned_slug is not None and provider_slug == pinned_slug,
            }
        )

    if not providers_out:
        providers_out = _providers_from_stage3(stage3)

    if providers_out:
        candidate["providers"] = providers_out

    pinned_provider = _match_provider(provider_rows, pinned_slug, pinned_quant)
    if pinned_provider is None:
        if pinned_slug is not None:
            notes.append(f"pinned provider not present in stage2 providers list: {pinned_slug}")
        return

    uptime = _as_float(pinned_provider.get("uptime_observed_mean"))
    if uptime is not None:
        candidate["uptime_pct"] = uptime

    tier = _worst_routing_tier(pinned_provider.get("routing_tier_observed"))
    if tier is not None:
        candidate["routing_tier"] = tier

    latency = pinned_provider.get("fetch_latency_ms")
    if isinstance(latency, dict):
        p50 = _as_float(latency.get("p50"))
        p90 = _as_float(latency.get("p90"))
        if p50 is not None:
            candidate["latency_p50"] = p50
        if p90 is not None:
            candidate["latency_p90"] = p90
        if p50 is not None or p90 is not None:
            notes.append("latency_p50/latency_p90 are provider fetch latency (not TTFT)")

    price_in = _as_float(pinned_provider.get("price_in"))
    price_out = _as_float(pinned_provider.get("price_out"))
    max_out = _as_int(pinned_provider.get("max_completion_tokens"))

    if price_in is not None:
        candidate["price_in"] = price_in
    if price_out is not None:
        candidate["price_out"] = price_out
    if max_out is not None:
        candidate["max_out"] = max_out

    if pinned_slug is not None and (price_in is not None or price_out is not None or max_out is not None):
        notes.append(
            f"candidate-level price_in/price_out/max_out pinned from endpoint {pinned_slug}; provider rows preserved in providers[]"
        )


def _apply_stage3(candidate: dict[str, Any], stage3: dict[str, Any], notes: list[str]) -> None:
    checks = stage3.get("checks")
    if isinstance(checks, dict):
        streaming = _check_pass(checks.get("streaming"))
        if streaming is not None:
            candidate["streaming"] = streaming

        tools = _check_pass(checks.get("tools"))
        if tools is not None:
            candidate["tool_use"] = tools

        structured = _check_pass(checks.get("structured"))
        if structured is not None:
            notes.append(f"stage3 structured check pass={structured}")


def _apply_stage4(candidate: dict[str, Any], stage4: dict[str, Any]) -> None:
    off_spike = _extract_off_spike(stage4)
    if off_spike is not None:
        candidate["off_spike"] = off_spike

    verdict = _strip_string(stage4.get("goldilocks_verdict"))
    if verdict is not None:
        candidate["goldilocks_verdict"] = verdict
    elif off_spike is not None:
        candidate["goldilocks_verdict"] = off_spike


def _apply_stage5(candidate: dict[str, Any], stage5: dict[str, Any]) -> None:
    on_smoke = _extract_on_smoke(stage5)
    if on_smoke is not None:
        candidate["on_smoke"] = on_smoke


def _assert_no_forbidden_keys(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_FIELD_NAMES:
                raise RuntimeError(f"forbidden field key leaked into output: {path}.{key}")
            _assert_no_forbidden_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _assert_no_forbidden_keys(item, f"{path}[{idx}]")


def build_snapshot(evidence_dir: Path, trace_id: str) -> tuple[dict[str, Any], list[str], list[Path]]:
    latest, warnings = _collect_latest_evidence(evidence_dir, trace_id)

    candidates: list[dict[str, Any]] = []
    for spec in _CANDIDATE_SPECS:
        slug = spec["slug"]
        candidate = _new_candidate(spec)
        notes: list[str] = []

        stage2 = latest.get((2, slug))
        stage3 = latest.get((3, slug))
        stage4 = latest.get((4, slug))
        stage5 = latest.get((5, slug))

        stage3_payload = stage3.payload if stage3 is not None else None
        if stage2 is not None:
            _apply_stage2(candidate, stage2.payload, stage3_payload, notes)
        elif stage3_payload is not None:
            providers = _providers_from_stage3(stage3_payload)
            if providers:
                candidate["providers"] = providers

        if stage3_payload is not None:
            _apply_stage3(candidate, stage3_payload, notes)

        if stage4 is not None:
            _apply_stage4(candidate, stage4.payload)

        if stage5 is not None:
            _apply_stage5(candidate, stage5.payload)

        candidate["notes"] = _normalize_notes(notes)
        candidates.append(candidate)

    consumed_paths = sorted(
        {
            record.path.resolve()
            for (stage, slug), record in latest.items()
            if stage in STAGES and slug in _CANDIDATE_SLUGS
        }
    )

    snapshot = {
        "captured_at": _utc_now_iso(),
        "catalog_source": CATALOG_SOURCE,
        "candidates": candidates,
        "evidence_sources": [str(path) for path in consumed_paths],
        "warnings": warnings,
    }
    _assert_no_forbidden_keys(snapshot)
    return snapshot, warnings, consumed_paths


def _write_snapshot(path: Path, payload: dict[str, Any], pretty: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    else:
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    path.write_text(rendered + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    trace_id = _new_trace_id()

    evidence_dir = args.evidence_dir.expanduser().resolve()
    out_path = args.out.expanduser().resolve() if args.out is not None else _default_out_path(evidence_dir).resolve()

    snapshot, warnings, consumed = build_snapshot(evidence_dir, trace_id)
    _write_snapshot(out_path, snapshot, pretty=args.pretty)

    ts = _utc_now_iso()
    print(
        (
            f"[{ts}] qualification_snapshot trace={trace_id} candidates={len(snapshot['candidates'])} "
            f"evidence_found={len(consumed)} warnings={len(warnings)} out={out_path}"
        ),
        flush=True,
    )

    if args.strict and not consumed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
