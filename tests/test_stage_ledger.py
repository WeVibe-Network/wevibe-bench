from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wevibe_bench import stage_ledger


def _write_budget_checkpoint(
    path: Path,
    *,
    run_id: str,
    accrued_actual_usd: float,
    committed_unproven_usd: float,
) -> None:
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "model_id": "z-ai/glm-5.2",
        "profile_name": "glm",
        "hard_cap_usd": 10.0,
        "accrued_actual_usd": accrued_actual_usd,
        "committed_unproven_usd": committed_unproven_usd,
        "outstanding": {},
        "updated_at": "2026-07-20T00:00:00Z",
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _write_stage_ledger(path: Path, *, caps: dict[str, float], stages: dict[str, list[dict[str, object]]]) -> None:
    payload = {
        "schema_version": 1,
        "caps": caps,
        "stages": stages,
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _invoke(argv: list[str], capsys: Any) -> tuple[int, str, str]:
    rc = stage_ledger.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out.strip(), captured.err.strip()


def _patch_spend(monkeypatch: Any, spend_by_run_id: dict[str, float], dsn: str = "postgresql://spend-proxy") -> None:
    class _FakeSpendMeter:
        def __init__(self, _dsn: str) -> None:
            self._dsn = _dsn

        def run_spend(self, session_id: str) -> Any:
            return SimpleNamespace(
                true_usd=float(spend_by_run_id.get(session_id, 0.0)),
            )

    monkeypatch.setattr(stage_ledger, "resolve_spend_db_dsn", lambda: dsn)
    monkeypatch.setattr(stage_ledger, "SpendMeter", _FakeSpendMeter)


def test_record_and_report_sum_across_stages_from_spend_db(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    _patch_spend(monkeypatch, {"stage2-run-a": 2.0, "stage3-run-a": 4.0})
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    rc, _, _ = _invoke(
        ["record", "--stage", "2", "--run-id", "stage2-run-a", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0

    rc, _, _ = _invoke(
        ["record", "--stage", "3", "--run-id", "stage3-run-a", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0

    rc, out, _ = _invoke(["report", "--ledger", str(ledger_path), "--log", str(log_path)], capsys)
    assert rc == 0
    report = json.loads(out)

    assert report["stages"]["stage2"]["accrued_usd"] == 2.0
    assert report["stages"]["stage2"]["committed_unproven_usd"] == 0.0
    assert report["stages"]["stage2"]["sum_usd"] == 2.0
    assert report["stages"]["stage3"]["sum_usd"] == 4.0
    assert report["global"]["sum_usd"] == 6.0

    log_text = log_path.read_text(encoding="utf-8")
    assert 'op="spend_read"' in log_text
    assert 'spend_source="spend_proxy_db"' in log_text


def test_record_is_idempotent_per_run_id_from_spend_db(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    spend = {"stage4-run-a": 1.5}
    _patch_spend(monkeypatch, spend)

    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    rc, _, _ = _invoke(
        ["record", "--stage", "4", "--run-id", "stage4-run-a", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0

    spend["stage4-run-a"] = 2.25
    rc, _, _ = _invoke(
        ["record", "--stage", "4", "--run-id", "stage4-run-a", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger["stages"]["stage4"]) == 1

    rc, out, _ = _invoke(["report", "--ledger", str(ledger_path), "--log", str(log_path)], capsys)
    assert rc == 0
    report = json.loads(out)
    assert report["stages"]["stage4"]["sum_usd"] == 2.25


def test_record_refuses_when_stage_cap_exceeded(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    _patch_spend(monkeypatch, {"stage2-near-cap": 9.0, "stage2-over-cap": 2.0})
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    rc, _, _ = _invoke(
        ["record", "--stage", "2", "--run-id", "stage2-near-cap", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0

    rc, out, _ = _invoke(
        ["record", "--stage", "2", "--run-id", "stage2-over-cap", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 3
    refusal = json.loads(out)
    assert refusal["recorded"] is False
    assert refusal["reason"] == "cap_exceeded"


def test_record_refuses_when_global_cap_exceeded(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    _patch_spend(monkeypatch, {"global-cap-s2": 3.0, "global-cap-s3": 3.0})
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    _write_stage_ledger(
        ledger_path,
        caps={
            "stage2": 10.0,
            "stage3": 10.0,
            "stage4": 10.0,
            "stage5": 10.0,
            "stage7": 40.0,
            "stage8": 32.0,
            "global": 5.0,
        },
        stages={
            "stage2": [],
            "stage3": [],
            "stage4": [],
            "stage5": [],
            "stage7": [],
            "stage8": [],
        },
    )

    rc, _, _ = _invoke(
        ["record", "--stage", "2", "--run-id", "global-cap-s2", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0

    rc, out, _ = _invoke(
        ["record", "--stage", "3", "--run-id", "global-cap-s3", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 3
    refusal = json.loads(out)
    assert refusal["recorded"] is False
    assert refusal["reason"] == "cap_exceeded"
    assert refusal["global_remaining"] < 0


def test_report_backfills_stage8_for_legacy_ledger_shape(tmp_path: Path, capsys: Any) -> None:
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    _write_stage_ledger(
        ledger_path,
        caps={
            "stage2": 10.0,
            "stage3": 25.0,
            "stage4": 40.0,
            "stage5": 40.0,
            "stage7": 40.0,
            "global": 115.0,
        },
        stages={
            "stage2": [],
            "stage3": [],
            "stage4": [],
            "stage5": [],
            "stage7": [],
        },
    )

    rc, out, _ = _invoke(["report", "--ledger", str(ledger_path), "--log", str(log_path)], capsys)
    assert rc == 0
    report = json.loads(out)
    assert report["caps"]["stage8"] == 32.0
    assert report["stages"]["stage8"]["cap_usd"] == 32.0
    assert report["stages"]["stage8"]["sum_usd"] == 0.0


def test_record_with_corrupt_budget_json_logs_error_and_returns_nonzero(tmp_path: Path, capsys: Any) -> None:
    # RETIRED checkpoint path is still accepted as a run_id source only.
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"
    corrupt_budget = tmp_path / "corrupt-budget.json"
    corrupt_budget.write_text("{not valid json", encoding="utf-8")

    rc, _, err = _invoke(
        [
            "record",
            "--stage",
            "2",
            "--budget-json",
            str(corrupt_budget),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )

    assert rc != 0
    assert "budget JSON" in err
    assert not ledger_path.exists()

    log_text = log_path.read_text(encoding="utf-8")
    assert 'op="record"' in log_text
    assert 'outcome="error"' in log_text
    assert 'error_type="ValueError"' in log_text
    assert "traceback=" in log_text


def test_record_legacy_budget_json_still_resolves_run_id_but_uses_db_spend(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    # RETIRED (27-07-26): budget-json amount fields are ignored; only run_id is used.
    _patch_spend(monkeypatch, {"stage2-run-retired": 4.25})
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"
    budget_path = tmp_path / "retired-checkpoint.json"
    _write_budget_checkpoint(
        budget_path,
        run_id="stage2-run-retired",
        accrued_actual_usd=0.01,
        committed_unproven_usd=99.0,
    )

    rc, _, _ = _invoke(
        [
            "record",
            "--stage",
            "2",
            "--budget-json",
            str(budget_path),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 0

    rc, out, _ = _invoke(["report", "--ledger", str(ledger_path), "--log", str(log_path)], capsys)
    assert rc == 0
    report = json.loads(out)
    assert report["stages"]["stage2"]["accrued_usd"] == 4.25
    assert report["stages"]["stage2"]["committed_unproven_usd"] == 0.0
