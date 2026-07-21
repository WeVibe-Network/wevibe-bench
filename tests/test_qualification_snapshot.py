from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import qualification_snapshot as qs


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _candidate_by_slug(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["slug"]: row for row in snapshot["candidates"]}


def test_snapshot_emits_roster_with_tolerant_evidence_ingest(tmp_path: pathlib.Path) -> None:
    evidence_dir = tmp_path / "runs" / "qualification"
    evidence_dir.mkdir(parents=True)

    glm_old = {
        "schema_version": 1,
        "stage": 2,
        "slug": "z-ai/glm-5.2",
        "captured_at": "2026-07-20T01:00:00Z",
        "window": {
            "started_at": "2026-07-20T00:00:00Z",
            "ended_at": "2026-07-20T00:30:00Z",
            "ticks_planned": 6,
            "ticks_done": 6,
        },
        "providers": [
            {
                "provider_slug": "glm/provider-a",
                "quantization": "fp8",
                "uptime_observed_mean": 91.0,
                "routing_tier_observed": ["Normal"],
                "max_completion_tokens": 48000,
                "price_in": 0.00000080,
                "price_out": 0.00000240,
                "fetch_latency_ms": {"p50": 300, "p90": 450},
            },
            {
                "provider_slug": "glm/provider-b",
                "quantization": "fp8",
                "uptime_observed_mean": 89.0,
                "routing_tier_observed": ["Degraded"],
                "max_completion_tokens": 32000,
                "price_in": 0.00000070,
                "price_out": 0.00000210,
                "fetch_latency_ms": {"p50": 280, "p90": 430},
            },
        ],
        "recommended_pin": {
            "provider_slug": "glm/provider-a",
            "quantization": "fp8",
            "reason": "stable fallback",
        },
        "notes": "older stage2 sample",
        "trace": "trace-glm-old",
    }
    _write_json(evidence_dir / "stage2-z-ai_glm-5.2-20260720T010000Z.json", glm_old)

    glm_new = {
        "schema_version": 1,
        "stage": 2,
        "slug": "z-ai/glm-5.2",
        "captured_at": "2026-07-20T02:00:00Z",
        "window": {
            "started_at": "2026-07-20T01:00:00Z",
            "ended_at": "2026-07-20T01:40:00Z",
            "ticks_planned": 8,
            "ticks_done": 8,
        },
        "providers": [
            {
                "provider_slug": "glm/provider-a",
                "quantization": "fp8",
                "uptime_observed_mean": 92.0,
                "routing_tier_observed": ["Normal"],
                "max_completion_tokens": 50000,
                "price_in": 0.00000075,
                "price_out": 0.00000220,
                "fetch_latency_ms": {"p50": 260, "p90": 410},
            },
            {
                "provider_slug": "glm/provider-b",
                "quantization": "fp8",
                "uptime_observed_mean": 97.5,
                "routing_tier_observed": ["Normal", "Down"],
                "max_completion_tokens": 64000,
                "price_in": 0.00000061,
                "price_out": 0.00000195,
                "fetch_latency_ms": {"p50": 210, "p90": 350},
            },
        ],
        "recommended_pin": {
            "provider_slug": "glm/provider-a",
            "quantization": "fp8",
            "reason": "stable fallback",
        },
        "notes": "newer stage2 sample",
        "trace": "trace-glm-new",
    }
    _write_json(evidence_dir / "stage2-z-ai_glm-5.2-20260720T020000Z.json", glm_new)

    hy3_stage2 = {
        "schema_version": 1,
        "stage": 2,
        "slug": "tencent/hy3",
        "captured_at": "2026-07-20T02:30:00Z",
        "window": {
            "started_at": "2026-07-20T02:00:00Z",
            "ended_at": "2026-07-20T02:20:00Z",
            "ticks_planned": 4,
            "ticks_done": 4,
        },
        "providers": [
            {
                "provider_slug": "hy3/provider-main",
                "quantization": "int4",
                "uptime_observed_mean": 93.25,
                "routing_tier_observed": ["Normal", "Degraded"],
                "max_completion_tokens": 4096,
                "price_in": 0.00000014,
                "price_out": 0.00000058,
                "fetch_latency_ms": {"p50": 190, "p90": 280},
                "token": "should-not-leak",
                "api_key": "should-not-leak",
            }
        ],
        "recommended_pin": {
            "provider_slug": "hy3/provider-main",
            "quantization": "int4",
            "reason": "only healthy endpoint",
        },
        "authorization": "should-not-leak",
        "trace": "trace-hy3",
    }
    _write_json(evidence_dir / "stage2-tencent_hy3-20260720T023000Z.json", hy3_stage2)

    glm_stage3 = {
        "schema_version": 1,
        "stage": 3,
        "slug": "z-ai/glm-5.2",
        "captured_at": "2026-07-20T03:00:00Z",
        "checks": {
            "shape": {"pass": True},
            "streaming": {"pass": True},
            "tools": {"pass": True},
            "structured": {"pass": False},
            "require-params": {"pass": True},
        },
        "tokens_used_total": 321,
        "token_budget": 6000,
        "budget_ok": True,
        "cost_usd_total": 0.028,
        "provider_slugs": ["glm/provider-b"],
        "quantizations": ["fp8"],
        "errors": [],
        "trace": "trace-glm-stage3",
    }
    _write_json(evidence_dir / "stage3-z-ai_glm-5.2-20260720T030000Z.json", glm_stage3)

    big_pickle_stage3 = {
        "schema_version": 1,
        "stage": 3,
        "slug": "opencode/big-pickle",
        "captured_at": "2026-07-20T03:20:00Z",
        "checks": {
            "shape": {"pass": True},
            "streaming": {"pass": True},
            "tools": {"pass": True},
            "structured": {"pass": True},
            "require-params": {"pass": True},
        },
        "tokens_used_total": 212,
        "token_budget": 8000,
        "budget_ok": True,
        "cost_usd_total": 0.0,
        "provider_slugs": ["opencode/zen-free"],
        "quantizations": [],
        "errors": [],
        "trace": "trace-big-pickle-stage3",
    }
    _write_json(evidence_dir / "stage3-opencode_big-pickle-20260720T032000Z.json", big_pickle_stage3)

    (evidence_dir / "stage2-moonshotai_kimi-k2.7-code-20260720T031500Z.json").write_text(
        "{ not valid json",
        encoding="utf-8",
    )

    out_path = tmp_path / "snapshot.json"
    rc = qs.main(["--evidence-dir", str(evidence_dir), "--out", str(out_path), "--pretty"])

    assert rc == 0

    snapshot = json.loads(out_path.read_text(encoding="utf-8"))
    assert snapshot["catalog_source"] == "https://openrouter.ai/api/v1/models"
    assert [row["slug"] for row in snapshot["candidates"]] == [
        "z-ai/glm-5.2",
        "xiaomi/mimo-v2.5-pro",
        "xiaomi/mimo-v2.5",
        "tencent/hy3",
        "moonshotai/kimi-k2.7-code",
        "inclusionai/ring-2.6-1t",
        "opencode/big-pickle",
    ]

    evidence_sources = set(snapshot["evidence_sources"])
    assert str((evidence_dir / "stage2-z-ai_glm-5.2-20260720T020000Z.json").resolve()) in evidence_sources
    assert str((evidence_dir / "stage3-z-ai_glm-5.2-20260720T030000Z.json").resolve()) in evidence_sources
    assert str((evidence_dir / "stage3-opencode_big-pickle-20260720T032000Z.json").resolve()) in evidence_sources
    assert str((evidence_dir / "stage2-tencent_hy3-20260720T023000Z.json").resolve()) in evidence_sources
    assert str((evidence_dir / "stage2-z-ai_glm-5.2-20260720T010000Z.json").resolve()) not in evidence_sources

    warnings = snapshot["warnings"]
    assert any("corrupt evidence skipped" in warning for warning in warnings)

    by_slug = _candidate_by_slug(snapshot)

    glm = by_slug["z-ai/glm-5.2"]
    assert glm["providers"] == [
        {"provider_slug": "glm/provider-a", "endpoint": "glm/provider-a", "pinned_for_tests": False},
        {"provider_slug": "glm/provider-b", "endpoint": "glm/provider-b", "pinned_for_tests": True},
    ]
    assert glm["uptime_pct"] == 97.5
    assert glm["uptime_window"] == "2026-07-20T01:00:00Z/2026-07-20T01:40:00Z"
    assert glm["routing_tier"] == "Down"
    assert glm["latency_p50"] == 210.0
    assert glm["latency_p90"] == 350.0
    assert glm["price_in"] == 0.00000061
    assert glm["price_out"] == 0.00000195
    assert glm["max_out"] == 64000
    assert glm["streaming"] is True
    assert glm["tool_use"] is True
    assert "stage2 recommended_pin reason: stable fallback" in str(glm["notes"])
    assert "latency_p50/latency_p90 are provider fetch latency (not TTFT)" in str(glm["notes"])
    assert "stage3 structured check pass=False" in str(glm["notes"])

    hy3 = by_slug["tencent/hy3"]
    assert hy3["providers"] == [
        {
            "provider_slug": "hy3/provider-main",
            "endpoint": "hy3/provider-main",
            "pinned_for_tests": True,
        }
    ]
    assert hy3["uptime_pct"] == 93.25
    assert hy3["routing_tier"] == "Degraded"
    assert hy3["latency_p50"] == 190.0
    assert hy3["latency_p90"] == 280.0
    assert hy3["price_in"] == 0.00000014
    assert hy3["price_out"] == 0.00000058
    assert hy3["max_out"] == 4096

    mimo_pro = by_slug["xiaomi/mimo-v2.5-pro"]
    assert mimo_pro["context"] == 1_048_576
    assert mimo_pro["max_out"] == 131_072
    assert mimo_pro["price_in"] == 0.000000435
    assert mimo_pro["price_out"] == 0.00000087
    assert mimo_pro["tool_use"] is True
    assert mimo_pro["uptime_pct"] is None
    assert mimo_pro["streaming"] is None
    assert mimo_pro["providers"] == [{"provider_slug": None, "endpoint": None, "pinned_for_tests": False}]

    ring = by_slug["inclusionai/ring-2.6-1t"]
    assert ring["context"] is None
    assert ring["tool_use"] is None

    big_pickle = by_slug["opencode/big-pickle"]
    assert big_pickle["context"] is None
    assert big_pickle["max_out"] == 8192
    assert big_pickle["price_in"] == 0.0
    assert big_pickle["price_out"] == 0.0
    assert big_pickle["streaming"] is True
    assert big_pickle["tool_use"] is True
    assert big_pickle["providers"] == [
        {
            "provider_slug": "opencode/zen-free",
            "endpoint": "opencode/zen-free",
            "pinned_for_tests": True,
        }
    ]
    assert "Smoke-only lowest rung (Walter-pinned 2026-07-21); NO stage-4 OFF-spike; OpenCode Zen free/free pricing." in str(
        big_pickle["notes"]
    )

    rendered = json.dumps(snapshot, sort_keys=True)
    assert '"token"' not in rendered
    assert '"api_key"' not in rendered
    assert '"authorization"' not in rendered


def test_strict_returns_nonzero_when_no_evidence(tmp_path: pathlib.Path) -> None:
    evidence_dir = tmp_path / "runs" / "qualification"
    evidence_dir.mkdir(parents=True)
    out_path = tmp_path / "snapshot-empty.json"

    rc_tolerant = qs.main(["--evidence-dir", str(evidence_dir), "--out", str(out_path)])
    assert rc_tolerant == 0

    rc_strict = qs.main(["--evidence-dir", str(evidence_dir), "--out", str(out_path), "--strict"])
    assert rc_strict == 2

    snapshot = json.loads(out_path.read_text(encoding="utf-8"))
    assert snapshot["evidence_sources"] == []
