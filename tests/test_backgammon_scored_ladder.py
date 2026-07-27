from __future__ import annotations

import hashlib
import json
import pathlib
import shlex
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from wevibe_bench.config import (
    BACKGAMMON_LADDER_SCHEMA_VERSION,
    backgammon_ladder_roster_fingerprint,
    backgammon_scored_ladder_roster,
)

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import backgammon_scored_ladder as bl


ROSTER = backgammon_scored_ladder_roster()
ALL_MODELS = [r.model for r in ROSTER]


@pytest.fixture(autouse=True)
def _default_pricing_gate_ok(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        bl,
        "verify_pricing",
        lambda **_kwargs: SimpleNamespace(
            ok=True,
            version=bl.PRICING_EXPECTED_VERSION,
            missing_models=[],
            reason="ok",
        ),
    )


def _rung_params_payload() -> dict[str, Any]:
    return {
        "orcarouter/kimi/kimi-k3": {
            "profile": "kimik3",
            "pricing_input": 3.0,
            "pricing_output": 15.0,
            "cap_usd": 12.0,
            "cost_limit": 12.0,
            "cost_target": 10.8,
        },
        "orcarouter/kimi/kimi-k2.7-code": {
            "profile": "kimicode",
            "pricing_input": 0.95,
            "pricing_output": 4.0,
            "cap_usd": 2.7,
            "cost_limit": 2.7,
            "cost_target": 2.4,
        },
        "orcarouter/tencent/hy3": {
            "profile": "hy3",
            "pricing_input": 0.18,
            "pricing_output": 0.59,
            "cap_usd": 2.0,
            "cost_limit": 2.0,
            "cost_target": 1.8,
        },
    }


def _write_rung_params(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "rung-params.json"
    path.write_text(json.dumps(_rung_params_payload()), encoding="utf-8")
    return path


def _seed_manifest(tmp_path: pathlib.Path) -> pathlib.Path:
    params = _rung_params_payload()
    bl._save_json_atomic(tmp_path / bl.MANIFEST_NAME, bl._build_manifest(bl._build_plan(), params, "seed"))
    params_path = tmp_path / "rung-params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")
    return params_path


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_import_fixture(
    tmp_path: pathlib.Path,
    *,
    run_number: int = 1,
    run_id: str | None = None,
    scorecard_model: str | None = None,
    scorecard_condition: str | None = None,
    detail_memory_mode: str | None = None,
) -> tuple[pathlib.Path, dict[str, Any], dict[str, Any]]:
    plan = bl._build_plan()
    cell = next(item for item in plan if int(item["run_number"]) == run_number)
    expected_model = str(cell["model"])
    run_label = f"stage{bl.STAGE_NUMBER}-run{run_number}-{bl._slugify_model(expected_model)}"
    scorecard_model = scorecard_model or expected_model
    scorecard_condition = scorecard_condition or str(cell["memory_mode"]).upper()
    detail_memory_mode = detail_memory_mode or str(cell["memory_mode"])
    run_id = run_id or f"{run_label}-20260721T195407Z"

    scorecard_path = tmp_path / f"import-run{run_number}-scorecard.json"
    detail_path = tmp_path / f"import-run{run_number}-detail.json"
    cell_log_path = tmp_path / f"import-run{run_number}-cell.log"
    proxy_log_path = tmp_path / f"import-run{run_number}-proxy.log"
    proxy_checkpoint_path = tmp_path / f"import-run{run_number}-proxy-checkpoint.json"

    scorecard = {
        "cells": [
            {
                "condition": scorecard_condition,
                "delivery": "N/A",
                "model": scorecard_model,
                "resolved": False,
                "scored": True,
                "total_tokens": 106570.0,
                "turns": 61.0,
                "wall_seconds": 2006.4,
                "wall_cost_usd": 6.1746492500000025,
            }
        ],
        "manifest": {
            "config": {
                "max_attempts": 3,
                "run_label": run_label,
                "schedule": {"all_models": [scorecard_model]},
            }
        },
        "model_diffs": [],
    }
    detail = {
        "cells": [
            {
                "attempt_reports": [
                    {"attempt": 1, "conformed": True, "failed_gates": ["F10"], "n_problems": 1, "verdict": "FAIL"},
                    {"attempt": 2, "conformed": True, "failed_gates": ["F10", "F12"], "n_problems": 2, "verdict": "FAIL"},
                    {
                        "attempt": 3,
                        "conformed": True,
                        "failed_gates": ["F10", "F12", "F14"],
                        "n_problems": 3,
                        "verdict": "FAIL",
                    },
                ],
                "attempts_to_green": "FAIL",
                "conformed": True,
                "failed_gates": ["F10", "F12", "F14"],
                "memory_mode": detail_memory_mode,
                "n_problems": 3,
            }
        ]
    }

    scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")
    detail_path.write_text(json.dumps(detail), encoding="utf-8")
    cell_log_path.write_text("PROGRESS recall_env_injection=container\n", encoding="utf-8")
    proxy_log_path.write_text(
        'model="xiaomi/mimo-v2.5" status=200\n'
        'op=http.request trace=t1 phase=entry method=POST url=/v1/recall\n'
        '[recall] /v1/recall result_count=1\n'
        'op=http.request trace=t1 phase=outcome method=POST url=/v1/recall status=200\n',
        encoding="utf-8",
    )
    proxy_checkpoint_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "accrued_actual_usd": 6.1746492500000025,
                "committed_unproven_usd": 1.3670856,
            }
        ),
        encoding="utf-8",
    )

    payload = {
        "schema_version": 1,
        "run_number": run_number,
        "run_id": run_id,
        "scorecard_path": str(scorecard_path),
        "detail_path": str(detail_path),
        "cell_log_path": str(cell_log_path),
        "proxy_log_path": str(proxy_log_path),
        "proxy_checkpoint_path": str(proxy_checkpoint_path),
        "scorecard_sha256": _sha256(scorecard_path),
        "detail_sha256": _sha256(detail_path),
        "memory": {
            "org_id": "wevibe-org-0",
            "submission_hash": "547b5c0b711fcbdfa8fc7cd8055d30e640a116a062ea2815804d4ef7aed947fd",
            "memory_fp": "0d256a24",
            "approve_status": "committed",
            "delivery": "YES",
        },
        "accrued_usd": 6.1746492500000025,
        "committed_unproven_usd": 1.3670856,
        "note": "synthetic import fixture",
    }
    import_path = tmp_path / f"cell{run_number}-import.json"
    import_path.write_text(json.dumps(payload), encoding="utf-8")
    return import_path, payload, cell


def _stats(
    *,
    verdict: str = "PASS",
    attempts: int | str | None = 2,
    conformed: bool = True,
    failed_gates: list[str] | None = None,
    total_tokens: float = 100000.0,
    max_attempts: int = 3,
    wall_seconds: float = 1000.0,
    turns: float = 50.0,
    cost_usd: float = 1.0,
    termination_reason: str | None = None,
) -> dict[str, Any]:
    stats = {
        "verdict": verdict,
        "scored": True,
        "conformed": conformed,
        "attempts_to_green": attempts,
        "failed_gates": list(failed_gates or []),
        "max_attempts": max_attempts,
        "total_tokens": total_tokens,
        "turns": turns,
        "wall_seconds": wall_seconds,
        "cost_usd": cost_usd,
    }
    if termination_reason is not None:
        stats["termination_reason"] = termination_reason
    return stats


def _synthetic_entry(cell: dict[str, Any], rep: int, **stats_kwargs: Any) -> dict[str, Any]:
    # Defaults chosen so NO variance trigger fires: OFF/ON tokens differ >15%,
    # attempts_to_green differ, attempts < max_attempts, class stays BRACKET.
    defaults: dict[str, Any] = {"max_attempts": 5}
    if str(cell["memory_mode"]) == "on":
        defaults.update({"attempts": 3, "total_tokens": 100000.0})
    else:
        defaults.update({"attempts": 2, "total_tokens": 200000.0})
    defaults.update(stats_kwargs)
    return {
        "rung_index": int(cell["rung_index"]),
        "model": str(cell["model"]),
        "run_number": int(cell["run_number"]),
        "rep": int(rep),
        "phase": bl.CELL_PHASE,
        "memory_mode": str(cell["memory_mode"]),
        "role": str(cell["role"]),
        "status": "ok",
        "run_id": f"test-{cell['run_number']}-{rep}",
        "run_label": f"test-{cell['run_number']}",
        "dur_s": 1.0,
        "scorecard": "sc.json",
        "detail": "detail.json",
        "cell_log": "cell.log",
        "proxy_log": "proxy.log",
        "proxy_checkpoint": "cp.json",
        "accrued_usd": 0.5,
        "committed_unproven_usd": 0.0,
        "stats": _stats(**defaults),
        "anomalies": {"proxy_402_or_429": False, "resume_mid_cell": False, "wall_near_timeout": False},
        "assertions": {},
        "completed_at": "2026-07-21T00:00:00Z",
        "trace": "test",
    }


def _invoke_main(monkeypatch: Any, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["backgammon_scored_ladder.py", *argv])
    return bl.main()


def _assert_cli_error(monkeypatch: Any, capsys: Any, argv: list[str], needle: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _invoke_main(monkeypatch, argv)
    assert excinfo.value.code == 2
    assert needle in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Plan / roster derivation


def test_no_embedded_roster_derives_from_config() -> None:
    source = (SCRIPTS / "backgammon_scored_ladder.py").read_text(encoding="utf-8")
    assert "LOCKED_RUNGS" not in source

    plan = bl._build_plan()
    ordered_distinct_models = list(dict.fromkeys(str(cell["model"]) for cell in plan))
    assert ordered_distinct_models == ALL_MODELS

    per_model_counts = {
        rung.model: sum(1 for cell in plan if str(cell["model"]) == rung.model) for rung in ROSTER
    }
    assert per_model_counts == {rung.model: len(rung.memory_modes) for rung in ROSTER}


def test_exact_roster_a_cell_allocation() -> None:
    plan = bl._build_plan()
    assert len(plan) == 5

    expected = [
        (1, "orcarouter/kimi/kimi-k3", "source", "off", "all"),
        (2, "orcarouter/kimi/kimi-k2.7-code", "measure", "off", "session"),
        (3, "orcarouter/kimi/kimi-k2.7-code", "measure", "on", "session"),
        (4, "orcarouter/tencent/hy3", "measure", "off", "session"),
        (5, "orcarouter/tencent/hy3", "measure", "on", "session"),
    ]
    actual = [
        (int(c["run_number"]), str(c["model"]), str(c["role"]), str(c["memory_mode"]), str(c["phase"]))
        for c in plan
    ]
    assert actual == expected
    assert [int(c["run_number"]) for c in plan] == list(range(1, 6))


def test_roster_validation_rejects_source_after_measure() -> None:
    from wevibe_bench.config import LadderRung

    bad = (
        LadderRung(model="m1", role="measure", memory_modes=("off", "on")),
        LadderRung(model="m2", role="source", memory_modes=("off",)),
    )
    with pytest.raises(RuntimeError, match="source rung"):
        bl._build_plan(bad)


# ---------------------------------------------------------------------------
# Manifest


def test_manifest_freeze_deterministic_and_fingerprint() -> None:
    plan = bl._build_plan()
    params = _rung_params_payload()
    m1 = bl._build_manifest(plan, params, "t1")
    m2 = bl._build_manifest(plan, params, "t2")

    assert bl._manifest_comparable(m1) == bl._manifest_comparable(m2)
    assert m1["config_fingerprint"] == backgammon_ladder_roster_fingerprint()
    assert m1["schema_version"] == BACKGAMMON_LADDER_SCHEMA_VERSION
    assert m1["total_cells"] == 5
    assert len(m1["cell_allocation"]) == 5

    comparable_for_guard = dict(bl._manifest_comparable(m1))
    comparable_for_guard.pop("preregistration", None)
    comparable_blob = json.dumps(comparable_for_guard).lower()
    for forbidden in ["prompt", "answer", "api_key", "secret", "password", "bearer"]:
        assert forbidden not in comparable_blob

    prereg = m1.get("preregistration")
    assert isinstance(prereg, dict)
    assert prereg.get("task") == "locked backgammon prompt/CONTRACT/oracle"
    assert prereg.get("disclosures") == [
        "2026-07-22: twin-aware delivery probe — harness measurement fix (suppressed-as-twin-of-returned counts delivered, evidence recorded in scorecard); protocol semantics unchanged; disclosed per pre-registration integrity like the 22-07 smoke defect fixes"
    ]
    prereg_blob = json.dumps(prereg).lower()
    for forbidden in ["api_key", "secret", "password", "bearer"]:
        assert forbidden not in prereg_blob


def test_rung_params_validation(tmp_path: pathlib.Path) -> None:
    payload = _rung_params_payload()
    del payload[ALL_MODELS[0]]
    path = tmp_path / "params.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing object for model"):
        bl._load_rung_params(path, ROSTER)

    payload = _rung_params_payload()
    payload[ALL_MODELS[0]]["cost_target"] = 99.0
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="cost_target < cost_limit"):
        bl._load_rung_params(path, ROSTER)

    payload = _rung_params_payload()
    payload[ALL_MODELS[0]]["bogus_field"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unknown fields"):
        bl._load_rung_params(path, ROSTER)


# ---------------------------------------------------------------------------
# Stats extraction / sentinel handling


@pytest.mark.parametrize(
    ("sentinel", "expected_verdict"),
    [("FAIL", "FAIL"), ("BUDGET_STOP", "BUDGET_STOP")],
)
def test_extract_stats_preserves_attempt_sentinels(sentinel: str, expected_verdict: str) -> None:
    scorecard = {
        "cells": [
            {
                "resolved": False,
                "scored": True,
                "total_tokens": 12345.0,
                "turns": 18.0,
                "wall_seconds": 99.0,
                "wall_cost_usd": 0.75,
            }
        ],
        "manifest": {"config": {"max_attempts": 3}},
    }
    detail = {
        "cells": [
            {
                "conformed": True,
                "attempts_to_green": sentinel,
                "failed_gates": ["G07"],
            }
        ]
    }

    stats = bl._extract_stats(scorecard, detail)
    assert stats["attempts_to_green"] == sentinel
    assert stats["verdict"] == expected_verdict


def test_extract_stats_preserves_scorecard_budget_stop_termination_reason() -> None:
    scorecard = {
        "cells": [
            {
                "resolved": False,
                "scored": True,
                "verdict": "BUDGET_STOP",
                "termination_reason": "budget_stop_mid_attempt",
                "total_tokens": 12345.0,
                "turns": 18.0,
                "wall_seconds": 99.0,
                "wall_cost_usd": 0.75,
            }
        ],
        "manifest": {"config": {"max_attempts": 3}},
    }
    detail = {
        "cells": [
            {
                "conformed": False,
                "attempts_to_green": "BUDGET_STOP",
                "failed_gates": [],
            }
        ]
    }

    stats = bl._extract_stats(scorecard, detail)
    assert stats["verdict"] == "BUDGET_STOP"
    assert stats["termination_reason"] == "budget_stop_mid_attempt"


def test_attempts_numeric_handles_new_sentinels_gracefully() -> None:
    assert bl._attempts_numeric("DID_NOT_CONFORM") is None
    assert bl._attempts_numeric("attempt_ceiling_reached") is None


# ---------------------------------------------------------------------------
# Variance triggers


def test_classify_mapping() -> None:
    assert bl._classify(_stats(verdict="PASS", attempts=1)) == "CEILING"
    assert bl._classify(_stats(verdict="PASS", attempts=3)) == "BRACKET"
    assert bl._classify(_stats(verdict="BUDGET_STOP", attempts="BUDGET_STOP")) == "BRACKET"
    assert bl._classify(_stats(verdict="FAIL", conformed=True)) == "BRACKET"
    assert bl._classify(_stats(verdict="FAIL", conformed=False)) == "FLOOR"


def test_classify_and_triggers_ignore_non_numeric_attempt_sentinels() -> None:
    assert bl._classify(_stats(verdict="PASS", attempts="FAIL")) == "BRACKET"
    no_anom = {"a": False}

    fired = bl._evaluate_triggers(
        stats=_stats(verdict="PASS", attempts="FAIL", max_attempts=3, total_tokens=200000.0),
        off_stats={"total_tokens": 100000.0, "attempts_to_green": 3},
        anomalies=no_anom,
        recorded_class=None,
    )
    assert fired == []

    fired = bl._evaluate_triggers(
        stats=_stats(verdict="PASS", attempts=2, max_attempts=3, total_tokens=200000.0),
        off_stats={"total_tokens": 100000.0, "attempts_to_green": "FAIL"},
        anomalies=no_anom,
        recorded_class=None,
    )
    assert fired == []


def test_t1_gate_margin() -> None:
    no_anom = {"a": False}
    fired = bl._evaluate_triggers(
        stats=_stats(verdict="FAIL", failed_gates=["G01"]),
        off_stats=None,
        anomalies=no_anom,
        recorded_class=None,
    )
    assert fired == ["T1"]

    fired = bl._evaluate_triggers(
        stats=_stats(verdict="PASS", attempts=3, max_attempts=3),
        off_stats=None,
        anomalies=no_anom,
        recorded_class=None,
    )
    assert fired == ["T1"]

    fired = bl._evaluate_triggers(
        stats=_stats(verdict="PASS", attempts=2, max_attempts=3),
        off_stats=None,
        anomalies=no_anom,
        recorded_class=None,
    )
    assert fired == []


def test_t2_lift_sign_fragile() -> None:
    no_anom = {"a": False}
    off = {"total_tokens": 100000.0, "attempts_to_green": 3}
    fired = bl._evaluate_triggers(
        stats=_stats(total_tokens=110000.0, attempts=2),
        off_stats=off,
        anomalies=no_anom,
        recorded_class=None,
    )
    assert fired == ["T2"]

    fired = bl._evaluate_triggers(
        stats=_stats(total_tokens=50000.0, attempts=3, max_attempts=5),
        off_stats=off,
        anomalies=no_anom,
        recorded_class=None,
    )
    assert fired == ["T2"]  # equal attempts_to_green

    fired = bl._evaluate_triggers(
        stats=_stats(total_tokens=50000.0, attempts=1),
        off_stats=off,
        anomalies=no_anom,
        recorded_class=None,
    )
    assert fired == []


def test_t3_instrument_anomaly_and_t4_class_flip() -> None:
    fired = bl._evaluate_triggers(
        stats=_stats(),
        off_stats=None,
        anomalies={"proxy_402_or_429": True},
        recorded_class=None,
    )
    assert fired == ["T3"]

    fired = bl._evaluate_triggers(
        stats=_stats(verdict="FAIL", conformed=False, failed_gates=["G01", "G02"]),
        off_stats=None,
        anomalies={"x": False},
        recorded_class="BRACKET",
    )
    assert fired == ["T4"]


def test_off_stats_for_rung_ignores_non_numeric_attempts() -> None:
    plan = bl._build_plan()
    off_cell = next(cell for cell in plan if int(cell["run_number"]) == 2)

    checkpoint = {
        "cells": [
            _synthetic_entry(off_cell, 1, attempts=2, total_tokens=200000.0),
            _synthetic_entry(off_cell, 2, attempts="FAIL", total_tokens=180000.0),
            _synthetic_entry(off_cell, 3, attempts=4, total_tokens=160000.0),
        ]
    }
    off_stats = bl._off_stats_for_rung(plan, checkpoint, int(off_cell["rung_index"]))
    assert off_stats is not None
    assert off_stats["attempts_to_green"] == 3

    checkpoint_all_sentinels = {
        "cells": [
            _synthetic_entry(off_cell, 1, attempts="FAIL"),
            _synthetic_entry(off_cell, 2, attempts="BUDGET_STOP"),
        ]
    }
    off_stats_none = bl._off_stats_for_rung(plan, checkpoint_all_sentinels, int(off_cell["rung_index"]))
    assert off_stats_none is not None
    assert off_stats_none["attempts_to_green"] is None


# ---------------------------------------------------------------------------
# Assertions (identity / delivery)


def test_scan_identity() -> None:
    ok_log = 'model="xiaomi/mimo-v2.5" status=200\nmodel="xiaomi/mimo-v2.5" status=200\n'
    result = bl._scan_identity(ok_log, "xiaomi/mimo-v2.5")
    assert result["ok"] and not result["mismatch"] and result["confirmed_response_count"] == 2

    bad_log = ok_log + 'event="identity_mismatch" expected_upstream_model="xiaomi/mimo-v2.5"\n'
    result = bl._scan_identity(bad_log, "xiaomi/mimo-v2.5")
    assert not result["ok"] and result["mismatch"]

    result = bl._scan_identity("no evidence at all", "xiaomi/mimo-v2.5")
    assert not result["ok"] and not result["mismatch"]


def test_scan_delivery() -> None:
    clone_slice = (
        "2026-07-21T00:00:00Z INFO op=http.request trace=t1 phase=entry method=POST url=/v1/recall\n"
        '[recall] /v1/recall result_count=11\n'
        "2026-07-21T00:00:01Z INFO op=http.request trace=t1 phase=outcome method=POST url=/v1/recall status=200 dur_ms=5\n"
    )
    cell_log = "PROGRESS ... recall_env_injection=container ...\n"
    result = bl._scan_delivery(clone_slice, cell_log)
    assert result["ok"] and result["recall_200_traces"] == ["t1"] and result["result_counts"] == [11]
    assert result["delivery_proof"] is None

    assert not bl._scan_delivery(clone_slice, "no env marker")["ok"]
    no_outcome = clone_slice.replace("phase=outcome", "phase=entry")
    assert not bl._scan_delivery(no_outcome, cell_log)["ok"]
    zero_results = clone_slice.replace("result_count=11", "result_count=0")
    assert not bl._scan_delivery(zero_results, cell_log)["ok"]


def test_scan_delivery_surfaces_sxe_delivery_proof_payload() -> None:
    clone_slice = (
        "2026-07-21T00:00:00Z INFO op=http.request trace=t1 phase=entry method=POST url=/v1/recall\n"
        "[recall] /v1/recall result_count=1\n"
        "2026-07-21T00:00:01Z INFO op=http.request trace=t1 phase=outcome method=POST url=/v1/recall status=200 dur_ms=5\n"
    )
    delivery_proof = {
        "delivery": "YES",
        "matched": False,
        "n_memories": 1,
        "per_memory": [
            {
                "fragment_fp": "aa11bb22",
                "cid": "cc33dd44",
                "matched": False,
                "delivery_mode": "twin_of_returned",
                "suppression": {
                    "winner_cid": "ee55ff66",
                    "dropped_twin_cid": "cc33dd44",
                    "score_gap": 0.0005,
                },
            }
        ],
    }
    cell_log = (
        "PROGRESS ... recall_env_injection=container ...\n"
        + "BACKGAMMON_SXE_RESULT_JSON "
        + json.dumps({"status": "ok", "delivery_proof": delivery_proof}, separators=(",", ":"), sort_keys=True)
        + "\n"
    )

    result = bl._scan_delivery(clone_slice, cell_log)
    assert result["ok"] is True
    assert result["delivery_proof"] == delivery_proof


def test_majority_and_median() -> None:
    assert bl._majority_verdict(["PASS", "FAIL", "PASS"]) == "PASS"
    assert bl._majority_verdict(["PASS", "FAIL"]) == "FAIL"
    assert bl._majority_verdict(["BUDGET_STOP", "BUDGET_STOP", "FAIL"]) == "BUDGET_STOP"
    assert bl._majority_verdict(["PASS", "FAIL", "BUDGET_STOP"]) == "FAIL"
    assert bl._median([3.0, 1.0, 2.0]) == 2.0
    assert bl._median([]) == 0.0


def test_reconciled_cost_limit_prefers_proxy_cap() -> None:
    cap_usd, reconciled_from = bl._reconciled_cost_limit_usd({"cap_usd": 2.7, "cost_limit": 2.4})
    assert cap_usd == pytest.approx(2.7)
    assert reconciled_from == pytest.approx(2.4)

    cap_usd, reconciled_from = bl._reconciled_cost_limit_usd({"cap_usd": 2.7, "cost_limit": 2.7})
    assert cap_usd == pytest.approx(2.7)
    assert reconciled_from is None


def test_build_session_extra_flags_uses_binding_cap_for_cost_limit() -> None:
    flags = bl._build_session_extra_flags(
        {"cost_target": 1.5, "output_price_per_1m": None},
        "token-redacted",
        binding_cap_usd=2.7,
    )
    parts = shlex.split(flags)
    assert parts[parts.index("--cost-limit") + 1] == "2.700000"
    assert parts[parts.index("--cost-target") + 1] == "1.500000"


def test_build_inner_env_sets_proxy_checkpoint_path(monkeypatch: Any) -> None:
    monkeypatch.setenv("WEVIBE_TEST_SENTINEL", "x")
    env = bl._build_inner_env(pathlib.Path("/tmp/proxy-cp.json"))
    assert env["WEVIBE_TEST_SENTINEL"] == "x"
    assert env["WEVIBE_BENCH_PROXY_CHECKPOINT"] == "/tmp/proxy-cp.json"


def test_summarize_cell_uses_true_median_and_proxy_accrued_cost() -> None:
    cell = next(item for item in bl._build_plan() if int(item["run_number"]) == 2)
    entries = [
        _synthetic_entry(cell, 1, wall_seconds=1628.5, total_tokens=120000.0),
        _synthetic_entry(cell, 2, wall_seconds=1830.19, total_tokens=130000.0),
        _synthetic_entry(cell, 3, wall_seconds=1972.44, total_tokens=140000.0),
    ]
    # Deliberately disagreeing non-authoritative wall costs in stats must be ignored.
    entries[0]["stats"]["cost_usd"] = 9.9
    entries[1]["stats"]["cost_usd"] = 8.8
    entries[2]["stats"]["cost_usd"] = 7.7

    # Authoritative cost meter is proxy-accrued_usd on checkpoint entries.
    entries[0]["accrued_usd"] = 1.20001
    entries[1]["accrued_usd"] = 1.66286
    entries[2]["accrued_usd"] = 2.40042

    summary = bl._summarize_cell(cell, entries)
    assert summary["median_wall_seconds"] == pytest.approx(1830.19)
    assert summary["median_wall_seconds"] != pytest.approx(1628.5)
    assert summary["median_cost_usd"] == pytest.approx(1.66286)


# ---------------------------------------------------------------------------
# Import recovery


def test_import_success_writes_checkpoint_and_summary_with_provenance(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, payload, _ = _write_import_fixture(tmp_path)

    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    monkeypatch.setattr(
        bl,
        "_probe_pool_memory",
        lambda org_id, submission_hash: {
            "collection": f"org_{org_id}_memories",
            "found": True,
            "cid": submission_hash,
        },
    )

    exit_code = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--import-cell",
            str(import_path),
            "--start-cell",
            "2",
            "--resume",
        ],
    )
    assert exit_code == 0
    assert recorder == [(2, 1), (3, 1), (4, 1), (5, 1)]

    checkpoint = bl._load_json(tmp_path / bl.CHECKPOINT_NAME)
    assert checkpoint is not None
    imported = next(
        entry
        for entry in checkpoint["cells"]
        if int(entry.get("run_number", -1)) == 1 and int(entry.get("rep", -1)) == 1
    )
    assert imported["status"] == "ok"
    assert imported["imported"] is True
    assert imported["import_source"] == str(import_path)
    assert imported["import_digests"]["scorecard_sha256"] == payload["scorecard_sha256"]
    assert imported["import_digests"]["detail_sha256"] == payload["detail_sha256"]
    assert imported["stats"]["attempts_to_green"] == "FAIL"

    summary = bl._load_json(tmp_path / bl.SUMMARY_NAME)
    assert summary is not None
    by_run = {int(cell["run_number"]): cell for cell in summary["cells"]}
    assert by_run[1]["attempts_to_green"] == ["FAIL"]
    assert by_run[1]["imported"] is True
    assert by_run[1]["import_sources"] == [str(import_path)]


def test_import_refuses_tampered_scorecard_digest(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, payload, _ = _write_import_fixture(tmp_path)
    payload["scorecard_sha256"] = "0" * 64
    import_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="scorecard digest mismatch"):
        _invoke_main(
            monkeypatch,
            [
                "--runs-dir",
                str(tmp_path),
                "--rung-params",
                str(params_path),
                "--import-cell",
                str(import_path),
                "--dry-run",
            ],
        )
    assert not (tmp_path / bl.CHECKPOINT_NAME).exists()


def test_import_refuses_tampered_detail_digest(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, payload, _ = _write_import_fixture(tmp_path)
    payload["detail_sha256"] = "f" * 64
    import_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="detail digest mismatch"):
        _invoke_main(
            monkeypatch,
            [
                "--runs-dir",
                str(tmp_path),
                "--rung-params",
                str(params_path),
                "--import-cell",
                str(import_path),
                "--dry-run",
            ],
        )
    assert not (tmp_path / bl.CHECKPOINT_NAME).exists()


def test_import_refuses_wrong_run_id(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, payload, _ = _write_import_fixture(tmp_path)
    payload["run_id"] = f"stage{bl.STAGE_NUMBER}-run9-orcarouter-anthropic-claude-opus-4-8-20260721T195407Z"
    import_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="run_id mismatch"):
        _invoke_main(
            monkeypatch,
            [
                "--runs-dir",
                str(tmp_path),
                "--rung-params",
                str(params_path),
                "--import-cell",
                str(import_path),
                "--dry-run",
            ],
        )


def test_import_refuses_scorecard_model_mismatch_vs_manifest(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, payload, _ = _write_import_fixture(
        tmp_path,
        scorecard_model="orcarouter/moonshotai/kimi-k2.7-code",
    )
    payload["scorecard_sha256"] = _sha256(pathlib.Path(payload["scorecard_path"]))
    import_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="scorecard model mismatch"):
        _invoke_main(
            monkeypatch,
            [
                "--runs-dir",
                str(tmp_path),
                "--rung-params",
                str(params_path),
                "--import-cell",
                str(import_path),
                "--dry-run",
            ],
        )


def test_import_refuses_manifest_roster_drift(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params = _rung_params_payload()
    manifest = bl._build_manifest(bl._build_plan(), params, "seed")
    manifest["config_fingerprint"] = "deadbeef"
    bl._save_json_atomic(tmp_path / bl.MANIFEST_NAME, manifest)
    params_path = tmp_path / "rung-params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")
    import_path, _, _ = _write_import_fixture(tmp_path)

    with pytest.raises(RuntimeError, match="roster has changed"):
        _invoke_main(
            monkeypatch,
            [
                "--runs-dir",
                str(tmp_path),
                "--rung-params",
                str(params_path),
                "--import-cell",
                str(import_path),
                "--dry-run",
            ],
        )


def test_import_refuses_when_pool_probe_missing(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, _, _ = _write_import_fixture(tmp_path)

    def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("no cell execution expected when import pool probe fails")

    monkeypatch.setattr(bl, "_run_cell_rep", _boom)
    monkeypatch.setattr(bl, "_ledger_check", lambda estimated_usd: True)
    monkeypatch.setattr(
        bl,
        "_probe_pool_memory",
        lambda org_id, submission_hash: {
            "collection": f"org_{org_id}_memories",
            "found": False,
            "cid": submission_hash,
        },
    )

    with pytest.raises(RuntimeError, match="pool probe missing"):
        _invoke_main(
            monkeypatch,
            [
                "--runs-dir",
                str(tmp_path),
                "--rung-params",
                str(params_path),
                "--import-cell",
                str(import_path),
                "--resume",
            ],
        )

    checkpoint = bl._load_json(tmp_path / bl.CHECKPOINT_NAME)
    assert checkpoint is None or checkpoint.get("cells") == []


def test_import_refuses_when_pool_probe_cid_mismatch(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, _, _ = _write_import_fixture(tmp_path)

    monkeypatch.setattr(bl, "_run_cell_rep", lambda **_kwargs: _synthetic_entry(bl._build_plan()[1], 1))
    monkeypatch.setattr(bl, "_ledger_check", lambda estimated_usd: True)
    monkeypatch.setattr(
        bl,
        "_probe_pool_memory",
        lambda org_id, submission_hash: {
            "collection": f"org_{org_id}_memories",
            "found": True,
            "cid": submission_hash[:-1] + "0",
        },
    )

    with pytest.raises(RuntimeError, match="pool probe cid mismatch"):
        _invoke_main(
            monkeypatch,
            [
                "--runs-dir",
                str(tmp_path),
                "--rung-params",
                str(params_path),
                "--import-cell",
                str(import_path),
                "--resume",
            ],
        )

    checkpoint = bl._load_json(tmp_path / bl.CHECKPOINT_NAME)
    assert checkpoint is None or checkpoint.get("cells") == []


def test_import_refuses_double_import(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, _, _ = _write_import_fixture(tmp_path)
    plan = bl._build_plan()
    existing = _synthetic_entry(plan[0], 1)
    bl._save_json_atomic(tmp_path / bl.CHECKPOINT_NAME, {"cells": [existing]})

    with pytest.raises(RuntimeError, match="already has run_number=1"):
        _invoke_main(
            monkeypatch,
            [
                "--runs-dir",
                str(tmp_path),
                "--rung-params",
                str(params_path),
                "--import-cell",
                str(import_path),
                "--dry-run",
            ],
        )


def test_import_dry_run_validates_and_skips_probe_without_writing_checkpoint(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, _, _ = _write_import_fixture(tmp_path)
    probe_calls: list[tuple[str, str]] = []

    def _probe(org_id: str, submission_hash: str) -> dict[str, Any]:
        probe_calls.append((org_id, submission_hash))
        return {"collection": "x", "found": True, "cid": submission_hash}

    monkeypatch.setattr(bl, "_probe_pool_memory", _probe)

    exit_code = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--import-cell",
            str(import_path),
            "--start-cell",
            "2",
            "--resume",
            "--dry-run",
        ],
    )
    assert exit_code == 0
    assert probe_calls == []
    assert not (tmp_path / bl.CHECKPOINT_NAME).exists()

    out = capsys.readouterr().out
    json_lines = [line for line in out.splitlines() if line.startswith("{")]
    run_numbers = [int(json.loads(line)["run_number"]) for line in json_lines]
    assert run_numbers == [2, 3, 4, 5]
    assert "pool_probe=skipped" in out


def test_import_does_not_touch_ledger_in_dry_run(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, _, _ = _write_import_fixture(tmp_path)
    ledger_path = tmp_path / "stage-ledger.json"
    ledger_path.write_text('{"sentinel":1}\n', encoding="utf-8")
    before = ledger_path.read_text(encoding="utf-8")

    def _ledger_boom(args_list: list[str]) -> int:
        raise AssertionError(f"ledger must not run during import dry-run: {args_list}")

    monkeypatch.setattr(bl, "_run_ledger", _ledger_boom)

    exit_code = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--import-cell",
            str(import_path),
            "--start-cell",
            "2",
            "--resume",
            "--dry-run",
        ],
    )
    assert exit_code == 0
    assert ledger_path.read_text(encoding="utf-8") == before


def test_import_checkpoint_round_trip_resume_skips_imported_cell(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    params_path = _seed_manifest(tmp_path)
    import_path, _, _ = _write_import_fixture(tmp_path)

    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    monkeypatch.setattr(
        bl,
        "_probe_pool_memory",
        lambda org_id, submission_hash: {
            "collection": f"org_{org_id}_memories",
            "found": True,
            "cid": submission_hash,
        },
    )

    first_exit = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--import-cell",
            str(import_path),
            "--resume",
        ],
    )
    assert first_exit == 0
    assert recorder == [(2, 1), (3, 1), (4, 1), (5, 1)]

    checkpoint = bl._load_json(tmp_path / bl.CHECKPOINT_NAME)
    assert checkpoint is not None
    imported = next(
        entry
        for entry in checkpoint["cells"]
        if int(entry.get("run_number", -1)) == 1 and int(entry.get("rep", -1)) == 1
    )
    assert imported["imported"] is True

    recorder_second: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder_second)
    second_exit = _invoke_main(
        monkeypatch,
        ["--runs-dir", str(tmp_path), "--rung-params", str(params_path), "--resume"],
    )
    assert second_exit == 0
    assert recorder_second == []


# ---------------------------------------------------------------------------
# Main loop (mocked execution)


def _patch_execution(
    monkeypatch: Any,
    recorder: list[tuple[int, int]],
    *,
    stats_for: dict[tuple[int, int], dict[str, Any]] | None = None,
) -> None:
    def _fake_run_cell_rep(*, cell: dict[str, Any], rep: int, params: dict[str, Any], args: Any, trace: str, logfile_path: Any) -> dict[str, Any]:
        del params, args, trace, logfile_path
        recorder.append((int(cell["run_number"]), int(rep)))
        entry = _synthetic_entry(cell, rep)
        if stats_for and (int(cell["run_number"]), int(rep)) in stats_for:
            entry["stats"] = stats_for[(int(cell["run_number"]), int(rep))]
        return entry

    monkeypatch.setattr(bl, "_run_cell_rep", _fake_run_cell_rep)
    monkeypatch.setattr(bl, "_ledger_check", lambda estimated_usd: True)


def test_dry_run_prints_plan_without_execution(tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any) -> None:
    def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("must not execute cells in dry-run")

    monkeypatch.setattr(bl, "_run_cell_rep", _boom)
    exit_code = _invoke_main(monkeypatch, ["--runs-dir", str(tmp_path), "--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "kimi-k3" in out and "kimi-k2.7-code" in out and "hy3" in out
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert rows
    assert all(row["binding_budget_meter"] == bl.BINDING_BUDGET_METER for row in rows)
    assert all(row["binding_budget_usd"] is None for row in rows)


def test_dry_run_with_rung_params_discloses_binding_budget(tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any) -> None:
    def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("must not execute cells in dry-run")

    monkeypatch.setattr(bl, "_run_cell_rep", _boom)
    params_path = _write_rung_params(tmp_path)
    exit_code = _invoke_main(
        monkeypatch,
        ["--runs-dir", str(tmp_path), "--rung-params", str(params_path), "--dry-run"],
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert rows
    assert all(row["binding_budget_meter"] == bl.BINDING_BUDGET_METER for row in rows)
    assert [float(row["binding_budget_usd"]) for row in rows] == pytest.approx([12.0, 2.7, 2.7, 2.0, 2.0])


@pytest.mark.parametrize(
    ("only_reps", "needle"),
    [
        ("1", "--only-reps token is malformed"),
        ("99:1", "--only-reps run_number 99 is not in the stage-8 plan"),
        ("1:0", "--only-reps rep must be >= 1"),
        ("1:4", "--only-reps rep must be <= 3"),
    ],
)
def test_only_reps_validation_errors(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
    capsys: Any,
    only_reps: str,
    needle: str,
) -> None:
    _assert_cli_error(
        monkeypatch,
        capsys,
        ["--runs-dir", str(tmp_path), "--dry-run", "--only-reps", only_reps],
        needle,
    )


def test_only_reps_conflicts_with_start_cell(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    _assert_cli_error(
        monkeypatch,
        capsys,
        [
            "--runs-dir",
            str(tmp_path),
            "--dry-run",
            "--only-reps",
            "1:1",
            "--start-cell",
            "2",
        ],
        "--only-reps cannot be combined with --start-cell",
    )


def test_session_only_runs_rejects_unknown_run(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    _assert_cli_error(
        monkeypatch,
        capsys,
        ["--runs-dir", str(tmp_path), "--dry-run", "--session-only-runs", "99"],
        "--session-only-runs run_number 99 is not in the stage-8 plan",
    )


def test_only_reps_dry_run_prints_exact_selected_reps(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    params_path = _write_rung_params(tmp_path)
    exit_code = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--dry-run",
            "--only-reps",
            "1:1,2:2,4:1",
        ],
    )
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "PLAN-REPS" in out
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert [(int(row["run_number"]), int(row.get("rep", 1))) for row in rows] == [
        (1, 1),
        (2, 2),
        (4, 1),
    ]


def test_only_reps_executes_exact_selected_pairs(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    params_path = _write_rung_params(tmp_path)

    exit_code = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--only-reps",
            "1:1,2:2,4:1",
        ],
    )
    assert exit_code == 0
    assert recorder == [(1, 1), (2, 2), (4, 1)]


def test_only_reps_rep2_only_bypasses_trigger_evaluation(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)

    def _trigger_boom(*, stats: dict[str, Any], off_stats: dict[str, Any] | None, anomalies: dict[str, bool], recorded_class: str | None) -> list[str]:
        del stats, off_stats, anomalies, recorded_class
        raise AssertionError("trigger evaluation must be bypassed in --only-reps mode")

    monkeypatch.setattr(bl, "_evaluate_triggers", _trigger_boom)
    params_path = _write_rung_params(tmp_path)
    exit_code = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--only-reps",
            "2:2",
        ],
    )
    assert exit_code == 0
    assert recorder == [(2, 2)]


def test_session_only_runs_overrides_phase_and_emits_plan_note(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    params_path = _write_rung_params(tmp_path)
    exit_code = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--dry-run",
            "--session-only-runs",
            "1",
        ],
    )
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "PLAN-SESSION-ONLY" in out
    assert "runs=[1]" in out
    assert '"run_number":1' in out
    assert '"phase":"session"' in out

    manifest = bl._load_json(tmp_path / bl.MANIFEST_NAME)
    assert manifest is not None
    run1 = next(
        cell
        for cell in manifest["cell_allocation"]
        if int(cell.get("run_number", -1)) == 1
    )
    assert run1["phase"] == "session"


def test_extra_disclosure_appends_to_manifest_preregistration(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    params_path = _write_rung_params(tmp_path)
    extra_1 = "2026-07-22: targeted rerun disclosure A"
    extra_2 = "2026-07-22: targeted rerun disclosure B"
    exit_code = _invoke_main(
        monkeypatch,
        [
            "--runs-dir",
            str(tmp_path),
            "--rung-params",
            str(params_path),
            "--dry-run",
            "--extra-disclosure",
            extra_1,
            "--extra-disclosure",
            extra_2,
        ],
    )
    assert exit_code == 0

    manifest = bl._load_json(tmp_path / bl.MANIFEST_NAME)
    assert manifest is not None
    prereg = manifest.get("preregistration")
    assert isinstance(prereg, dict)
    disclosures = prereg.get("disclosures")
    assert isinstance(disclosures, list)
    assert disclosures[-2:] == [extra_1, extra_2]


def test_fresh_run_writes_manifest_and_runs_all_cells(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    params_path = _write_rung_params(tmp_path)

    exit_code = _invoke_main(
        monkeypatch,
        ["--runs-dir", str(tmp_path), "--rung-params", str(params_path)],
    )
    assert exit_code == 0
    assert recorder == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1)]

    manifest = bl._load_json(tmp_path / bl.MANIFEST_NAME)
    assert manifest is not None
    assert manifest["config_fingerprint"] == backgammon_ladder_roster_fingerprint()
    assert len(manifest["cell_allocation"]) == 5

    summary = bl._load_json(tmp_path / bl.SUMMARY_NAME)
    assert summary is not None
    assert [c["n"] for c in summary["cells"]] == [1, 1, 1, 1, 1]
    assert all(c["triggers_fired"] == [] for c in summary["cells"])


def test_borderline_cell_repeats_to_n3_and_discloses_n(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    recorder: list[tuple[int, int]] = []
    # kimi OFF (run 2): FAIL one gate short -> T1 fires -> N=3.
    stats_for = {
        (2, 1): _stats(verdict="FAIL", failed_gates=["G07"], attempts=None, total_tokens=200000.0),
        (2, 2): _stats(verdict="PASS", attempts=2, total_tokens=200000.0),
        (2, 3): _stats(verdict="FAIL", failed_gates=["G07"], attempts=None, total_tokens=200000.0),
    }
    _patch_execution(monkeypatch, recorder, stats_for=stats_for)
    params_path = _write_rung_params(tmp_path)

    exit_code = _invoke_main(
        monkeypatch,
        ["--runs-dir", str(tmp_path), "--rung-params", str(params_path)],
    )
    assert exit_code == 0
    assert recorder == [(1, 1), (2, 1), (2, 2), (2, 3), (3, 1), (4, 1), (5, 1)]

    summary = bl._load_json(tmp_path / bl.SUMMARY_NAME)
    assert summary is not None
    by_run = {int(c["run_number"]): c for c in summary["cells"]}
    assert by_run[2]["n"] == 3
    assert by_run[2]["triggers_fired"] == ["T1"]
    assert by_run[2]["majority_verdict"] == "FAIL"
    assert by_run[3]["n"] == 1


def test_source_cell_never_triggers_variance(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    recorder: list[tuple[int, int]] = []
    # Source cell (run 1) FAILs one gate short — role=source must NOT repeat.
    stats_for = {(1, 1): _stats(verdict="FAIL", failed_gates=["G07"], attempts=None)}
    _patch_execution(monkeypatch, recorder, stats_for=stats_for)
    params_path = _write_rung_params(tmp_path)

    exit_code = _invoke_main(
        monkeypatch,
        ["--runs-dir", str(tmp_path), "--rung-params", str(params_path)],
    )
    assert exit_code == 0
    assert (1, 2) not in recorder and (1, 3) not in recorder


def test_fail_sentinel_round_trips_checkpoint_and_summary(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    recorder: list[tuple[int, int]] = []
    stats_for = {
        (1, 1): _stats(
            verdict="FAIL",
            attempts="FAIL",
            conformed=True,
            failed_gates=["G07"],
        )
    }
    _patch_execution(monkeypatch, recorder, stats_for=stats_for)
    params_path = _write_rung_params(tmp_path)

    exit_code = _invoke_main(
        monkeypatch,
        ["--runs-dir", str(tmp_path), "--rung-params", str(params_path)],
    )
    assert exit_code == 0

    checkpoint = bl._load_json(tmp_path / bl.CHECKPOINT_NAME)
    assert checkpoint is not None
    run1 = next(
        entry
        for entry in checkpoint["cells"]
        if int(entry.get("run_number", -1)) == 1 and int(entry.get("rep", -1)) == 1
    )
    assert run1["stats"]["attempts_to_green"] == "FAIL"

    summary = bl._load_json(tmp_path / bl.SUMMARY_NAME)
    assert summary is not None
    by_run = {int(cell["run_number"]): cell for cell in summary["cells"]}
    assert by_run[1]["attempts_to_green"] == ["FAIL"]


def test_valid_resume_skips_completed_and_proceeds(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    plan = bl._build_plan()
    params = _rung_params_payload()
    bl._save_json_atomic(tmp_path / bl.MANIFEST_NAME, bl._build_manifest(plan, params, "seed"))
    checkpoint = {"cells": [_synthetic_entry(plan[0], 1)]}
    checkpoint["cells"][0]["triggers"] = []
    bl._save_json_atomic(tmp_path / bl.CHECKPOINT_NAME, checkpoint)

    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    params_path = tmp_path / "rung-params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")

    exit_code = _invoke_main(
        monkeypatch,
        ["--runs-dir", str(tmp_path), "--rung-params", str(params_path), "--resume"],
    )
    assert exit_code == 0
    assert recorder == [(2, 1), (3, 1), (4, 1), (5, 1)]


def test_checkpoint_round_trip_accepts_new_and_historical_entry_shapes(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    plan = bl._build_plan()
    params = _rung_params_payload()
    bl._save_json_atomic(tmp_path / bl.MANIFEST_NAME, bl._build_manifest(plan, params, "seed"))

    new_shape = _synthetic_entry(
        plan[0],
        1,
        verdict="BUDGET_STOP",
        attempts="BUDGET_STOP",
        termination_reason="attempts_exhausted_by_budget",
    )
    old_shape = _synthetic_entry(plan[1], 1)
    old_shape["stats"].pop("termination_reason", None)

    bl._save_json_atomic(tmp_path / bl.CHECKPOINT_NAME, {"cells": [new_shape, old_shape]})
    loaded = bl._load_checkpoint(tmp_path / bl.CHECKPOINT_NAME)
    assert len(loaded["cells"]) == 2
    by_run = {int(entry["run_number"]): entry for entry in loaded["cells"]}
    assert by_run[1]["stats"]["termination_reason"] == "attempts_exhausted_by_budget"
    assert "termination_reason" not in by_run[2]["stats"]

    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    params_path = tmp_path / "rung-params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")

    exit_code = _invoke_main(
        monkeypatch,
        ["--runs-dir", str(tmp_path), "--rung-params", str(params_path), "--resume"],
    )
    assert exit_code == 0
    assert recorder == [(3, 1), (4, 1), (5, 1)]


def test_resume_rejects_roster_fingerprint_drift(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params = _rung_params_payload()
    manifest = bl._build_manifest(bl._build_plan(), params, "seed")
    manifest["config_fingerprint"] = "deadbeef"
    bl._save_json_atomic(tmp_path / bl.MANIFEST_NAME, manifest)

    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    params_path = tmp_path / "rung-params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")

    with pytest.raises(RuntimeError, match="roster has changed"):
        _invoke_main(monkeypatch, ["--runs-dir", str(tmp_path), "--rung-params", str(params_path), "--resume"])
    assert recorder == []


def test_resume_rejects_rung_params_drift(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params = _rung_params_payload()
    manifest = bl._build_manifest(bl._build_plan(), params, "seed")
    bl._save_json_atomic(tmp_path / bl.MANIFEST_NAME, manifest)

    drifted = _rung_params_payload()
    drifted[ALL_MODELS[0]]["pricing_input"] = 9.9
    params_path = tmp_path / "rung-params.json"
    params_path.write_text(json.dumps(drifted), encoding="utf-8")

    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    with pytest.raises(RuntimeError, match="rung params"):
        _invoke_main(monkeypatch, ["--runs-dir", str(tmp_path), "--rung-params", str(params_path), "--resume"])
    assert recorder == []


def test_resume_rejects_schema_mismatch(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    params = _rung_params_payload()
    manifest = bl._build_manifest(bl._build_plan(), params, "seed")
    manifest["schema_version"] = BACKGAMMON_LADDER_SCHEMA_VERSION + 999
    bl._save_json_atomic(tmp_path / bl.MANIFEST_NAME, manifest)

    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    params_path = tmp_path / "rung-params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")

    with pytest.raises(RuntimeError, match="schema"):
        _invoke_main(monkeypatch, ["--runs-dir", str(tmp_path), "--rung-params", str(params_path), "--resume"])
    assert recorder == []


def test_resume_rejects_legacy_checkpoint_without_manifest(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    plan = bl._build_plan()
    bl._save_json_atomic(tmp_path / bl.CHECKPOINT_NAME, {"cells": [_synthetic_entry(plan[0], 1)]})

    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)
    params_path = _write_rung_params(tmp_path)

    with pytest.raises(RuntimeError, match="no run manifest"):
        _invoke_main(monkeypatch, ["--runs-dir", str(tmp_path), "--rung-params", str(params_path), "--resume"])
    assert recorder == []


def test_ladder_abort_writes_escalation(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    def _abort_run(*, cell: dict[str, Any], rep: int, params: dict[str, Any], args: Any, trace: str, logfile_path: Any) -> dict[str, Any]:
        del rep, params, args, trace, logfile_path
        if int(cell["run_number"]) == 4:
            raise bl.LadderAbort("identity_mismatch", {"observed": "someone-else"})
        return _synthetic_entry(cell, 1)

    monkeypatch.setattr(bl, "_run_cell_rep", _abort_run)
    monkeypatch.setattr(bl, "_ledger_check", lambda estimated_usd: True)
    params_path = _write_rung_params(tmp_path)

    exit_code = _invoke_main(monkeypatch, ["--runs-dir", str(tmp_path), "--rung-params", str(params_path)])
    assert exit_code == 3

    escalation = bl._load_json(tmp_path / bl.ESCALATE_NAME)
    assert escalation is not None
    assert escalation["reason"] == "identity_mismatch"
    assert escalation["failed"]["run_number"] == 4
    # Cells 1-3 completed before the abort and stay checkpointed (R-32).
    checkpoint = bl._load_json(tmp_path / bl.CHECKPOINT_NAME)
    assert checkpoint is not None and len(checkpoint["cells"]) == 3


def test_unexpected_post_run_exception_persists_error_entry_with_artifacts(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    recorder: list[tuple[int, int]] = []
    _patch_execution(monkeypatch, recorder)

    def _raise_trigger_error(*, stats: dict[str, Any], off_stats: dict[str, Any] | None, anomalies: dict[str, bool], recorded_class: str | None) -> list[str]:
        del stats, off_stats, anomalies, recorded_class
        raise RuntimeError("trigger explode")

    monkeypatch.setattr(bl, "_evaluate_triggers", _raise_trigger_error)
    params_path = _write_rung_params(tmp_path)

    with pytest.raises(RuntimeError, match="trigger explode"):
        _invoke_main(
            monkeypatch,
            ["--runs-dir", str(tmp_path), "--rung-params", str(params_path)],
        )

    checkpoint = bl._load_json(tmp_path / bl.CHECKPOINT_NAME)
    assert checkpoint is not None

    run2 = next(
        entry
        for entry in checkpoint["cells"]
        if int(entry.get("run_number", -1)) == 2 and int(entry.get("rep", -1)) == 1
    )
    assert run2["status"] == "error"
    assert run2["scorecard"] == "sc.json"
    assert run2["detail"] == "detail.json"
    assert run2["cell_log"] == "cell.log"
    assert run2["proxy_log"] == "proxy.log"
    assert run2["proxy_checkpoint"] == "cp.json"
    assert "trigger explode" in str(run2.get("error"))


def test_plan_budget_refusal_stops_before_any_cell(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    recorder: list[tuple[int, int]] = []

    def _fake_run(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("no cell may run after budget refusal")

    monkeypatch.setattr(bl, "_run_cell_rep", _fake_run)
    monkeypatch.setattr(bl, "_ledger_check", lambda estimated_usd: False)
    params_path = _write_rung_params(tmp_path)

    exit_code = _invoke_main(monkeypatch, ["--runs-dir", str(tmp_path), "--rung-params", str(params_path)])
    assert exit_code == 3
    escalation = bl._load_json(tmp_path / bl.ESCALATE_NAME)
    assert escalation is not None and escalation["reason"] == "plan_budget_refused"
    assert recorder == []


@pytest.mark.parametrize(
    "reason_payload",
    [
        {"reason": "gate-down", "version": "", "missing_models": []},
        {
            "reason": f"version-mismatch got old want {bl.PRICING_EXPECTED_VERSION}",
            "version": "old",
            "missing_models": [],
        },
        {
            "reason": "missing models: tencent/hy3",
            "version": bl.PRICING_EXPECTED_VERSION,
            "missing_models": ["tencent/hy3"],
        },
    ],
)
def test_pricing_drift_aborts_before_any_paid_cell(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
    reason_payload: dict[str, Any],
) -> None:
    captured_roster_models: list[str] = []

    def _fake_verify_pricing(*, roster_models: list[str], **_kwargs: Any) -> Any:
        captured_roster_models.extend(roster_models)
        return SimpleNamespace(
            ok=False,
            version=reason_payload["version"],
            missing_models=list(reason_payload["missing_models"]),
            reason=reason_payload["reason"],
        )

    def _boom_run_cell(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("pricing drift must abort before cell execution")

    monkeypatch.setattr(bl, "verify_pricing", _fake_verify_pricing)
    monkeypatch.setattr(bl, "_run_cell_rep", _boom_run_cell)
    monkeypatch.setattr(bl, "_ledger_check", lambda estimated_usd: True)
    monkeypatch.setattr(bl, "resolve_orcarouter_api_key", lambda: ("token-redacted", "test"))
    monkeypatch.setattr(bl, "resolve_spend_proxy_base_url", lambda: "http://127.0.0.1:4480/v1")
    monkeypatch.setattr(bl, "resolve_spend_db_dsn", lambda: "postgresql://spend-proxy")

    params_path = _write_rung_params(tmp_path)
    exit_code = _invoke_main(monkeypatch, ["--runs-dir", str(tmp_path), "--rung-params", str(params_path)])
    assert exit_code == 3
    assert captured_roster_models == ["kimi/kimi-k3", "kimi/kimi-k2.7-code", "tencent/hy3"]

    escalation = bl._load_json(tmp_path / bl.ESCALATE_NAME)
    assert escalation is not None
    assert escalation["reason"] == "pricing_verify_failed"
    assert escalation["detail"]["reason"] == reason_payload["reason"]


def test_run_budget_cap_poll_aborts_cell_when_db_sum_reaches_cap(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    cell = next(item for item in bl._build_plan() if int(item["run_number"]) == 2)
    params = _rung_params_payload()[str(cell["model"])]
    runs_dir = tmp_path / "runs"
    ladder_runs_dir = tmp_path / "ladder"
    clone_log = tmp_path / "clone.log"
    clone_log.write_text("", encoding="utf-8")
    logfile_path = tmp_path / "driver.log"

    args = SimpleNamespace(
        runs_dir=str(runs_dir),
        ladder_runs_dir=str(ladder_runs_dir),
        clone_log=str(clone_log),
        proxy_token="token-redacted",
        spend_db_dsn="postgresql://spend-proxy",
        org_id="wevibe-org-0",
    )

    class _FakeSpendMeter:
        def __init__(self, _dsn: str) -> None:
            self._dsn = _dsn

        def run_spend(self, session_id: str) -> Any:
            del session_id
            return SimpleNamespace(true_usd=2.7, benchmark_usd=0.0)

        def model_identity_mismatches(self, session_id: str) -> list[Any]:
            del session_id
            return []

    def _fake_run_inner_tee(
        cmd: list[str],
        cell_log: pathlib.Path,
        *,
        env: dict[str, str] | None = None,
        budget_watch: Any | None = None,
        budget_poll_interval_s: float = bl.BUDGET_POLL_INTERVAL_S,
    ) -> tuple[int, dict[str, Any] | None]:
        del cmd, env, budget_poll_interval_s
        assert budget_watch is not None
        cell_log.write_text("PROGRESS inner\n", encoding="utf-8")
        budget_abort = budget_watch()
        assert budget_abort is not None
        return 143, budget_abort

    monkeypatch.setattr(bl, "SpendMeter", _FakeSpendMeter)
    monkeypatch.setattr(bl, "_ledger_check", lambda estimated_usd: True)
    monkeypatch.setattr(bl, "_ledger_record", lambda budget_json: True)
    monkeypatch.setattr(bl, "_build_inner_cmd", lambda **_kwargs: ["python", "-V"])
    monkeypatch.setattr(bl, "_run_inner_tee", _fake_run_inner_tee)

    with pytest.raises(bl.LadderAbort, match="run_budget_cap_reached") as excinfo:
        bl._run_cell_rep(
            cell=cell,
            rep=1,
            params=params,
            args=args,
            trace="trace-budget",
            logfile_path=logfile_path,
        )

    assert excinfo.value.reason == "run_budget_cap_reached"
    assert float(excinfo.value.detail["true_usd"]) >= float(params["cap_usd"])


def test_model_identity_mismatch_watch_aborts_cell(
    tmp_path: pathlib.Path,
    monkeypatch: Any,
) -> None:
    cell = next(item for item in bl._build_plan() if int(item["run_number"]) == 2)
    params = _rung_params_payload()[str(cell["model"])]
    runs_dir = tmp_path / "runs"
    ladder_runs_dir = tmp_path / "ladder"
    clone_log = tmp_path / "clone.log"
    clone_log.write_text("", encoding="utf-8")
    logfile_path = tmp_path / "driver.log"
    scorecard_path = tmp_path / "scorecard.json"
    detail_path = tmp_path / "detail.json"
    scorecard_path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "resolved": True,
                        "scored": True,
                        "total_tokens": 10,
                        "turns": 2,
                        "wall_seconds": 1,
                        "wall_cost_usd": 0.1,
                    }
                ],
                "manifest": {"config": {"max_attempts": 3}},
            }
        ),
        encoding="utf-8",
    )
    detail_path.write_text(
        json.dumps({"cells": [{"conformed": True, "attempts_to_green": 1, "failed_gates": []}]}),
        encoding="utf-8",
    )

    args = SimpleNamespace(
        runs_dir=str(runs_dir),
        ladder_runs_dir=str(ladder_runs_dir),
        clone_log=str(clone_log),
        proxy_token="token-redacted",
        spend_db_dsn="postgresql://spend-proxy",
        org_id="wevibe-org-0",
    )

    class _FakeSpendMeter:
        def __init__(self, _dsn: str) -> None:
            self._dsn = _dsn

        def run_spend(self, session_id: str) -> Any:
            del session_id
            return SimpleNamespace(
                calls=1,
                true_usd=0.5,
                benchmark_usd=0.6,
                uncached_input_tokens=10,
                cached_input_tokens=0,
                output_tokens=5,
                reasoning_tokens=0,
                unmetered_calls=0,
                last_call_at=None,
            )

        def model_identity_mismatches(self, session_id: str) -> list[Any]:
            del session_id
            return [
                SimpleNamespace(
                    model="kimi/kimi-k2.7-code",
                    upstream_model="tencent/hy3",
                    calls=1,
                )
            ]

    def _fake_newest_artifact(
        ladder_runs_dir_in: pathlib.Path,
        run_label: str,
        suffix: str,
        not_before: float,
    ) -> pathlib.Path:
        del ladder_runs_dir_in, run_label, not_before
        if suffix == "scorecard.json":
            return scorecard_path
        if suffix == "backgammon-detail.json":
            return detail_path
        raise AssertionError(f"unexpected suffix {suffix}")

    def _fake_run_inner_tee(
        cmd: list[str],
        cell_log: pathlib.Path,
        *,
        env: dict[str, str] | None = None,
        budget_watch: Any | None = None,
        budget_poll_interval_s: float = bl.BUDGET_POLL_INTERVAL_S,
    ) -> tuple[int, dict[str, Any] | None]:
        del cmd, env, budget_poll_interval_s
        if budget_watch is not None:
            assert budget_watch() is None
        cell_log.write_text("PROGRESS inner\n", encoding="utf-8")
        return 0, None

    monkeypatch.setattr(bl, "SpendMeter", _FakeSpendMeter)
    monkeypatch.setattr(bl, "_ledger_check", lambda estimated_usd: True)
    monkeypatch.setattr(bl, "_ledger_record", lambda budget_json: True)
    monkeypatch.setattr(bl, "_build_inner_cmd", lambda **_kwargs: ["python", "-V"])
    monkeypatch.setattr(bl, "_run_inner_tee", _fake_run_inner_tee)
    monkeypatch.setattr(bl, "_newest_artifact", _fake_newest_artifact)

    with pytest.raises(bl.LadderAbort, match="served_model_mismatch") as excinfo:
        bl._run_cell_rep(
            cell=cell,
            rep=1,
            params=params,
            args=args,
            trace="trace-model-watch",
            logfile_path=logfile_path,
        )

    assert excinfo.value.reason == "served_model_mismatch"
    mismatch = excinfo.value.detail["mismatches"][0]
    assert mismatch["requested_model"] == "kimi/kimi-k2.7-code"
    assert mismatch["upstream_model"] == "tencent/hy3"
