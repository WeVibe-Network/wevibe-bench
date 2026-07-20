from __future__ import annotations

import json
from pathlib import Path

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


def _invoke(argv: list[str], capsys) -> tuple[int, str, str]:
    rc = stage_ledger.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out.strip(), captured.err.strip()


def test_record_and_report_sum_across_stages(tmp_path: Path, capsys) -> None:
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    budget_stage2 = tmp_path / "run-stage2.json"
    budget_stage3 = tmp_path / "run-stage3.json"
    _write_budget_checkpoint(
        budget_stage2,
        run_id="stage2-run-a",
        accrued_actual_usd=1.25,
        committed_unproven_usd=0.75,
    )
    _write_budget_checkpoint(
        budget_stage3,
        run_id="stage3-run-a",
        accrued_actual_usd=3.0,
        committed_unproven_usd=1.0,
    )

    rc, _, _ = _invoke(
        [
            "record",
            "--stage",
            "2",
            "--budget-json",
            str(budget_stage2),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 0

    rc, _, _ = _invoke(
        [
            "record",
            "--stage",
            "3",
            "--budget-json",
            str(budget_stage3),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 0

    rc, out, _ = _invoke(
        ["report", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0
    report = json.loads(out)

    assert report["stages"]["stage2"]["accrued_usd"] == 1.25
    assert report["stages"]["stage2"]["committed_unproven_usd"] == 0.75
    assert report["stages"]["stage2"]["sum_usd"] == 2.0
    assert report["stages"]["stage3"]["sum_usd"] == 4.0
    assert report["global"]["sum_usd"] == 6.0


def test_record_is_idempotent_per_run_id(tmp_path: Path, capsys) -> None:
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"
    budget_path = tmp_path / "run-stage4.json"

    _write_budget_checkpoint(
        budget_path,
        run_id="stage4-run-a",
        accrued_actual_usd=1.0,
        committed_unproven_usd=0.5,
    )
    rc, _, _ = _invoke(
        [
            "record",
            "--stage",
            "4",
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

    _write_budget_checkpoint(
        budget_path,
        run_id="stage4-run-a",
        accrued_actual_usd=2.0,
        committed_unproven_usd=0.25,
    )
    rc, _, _ = _invoke(
        [
            "record",
            "--stage",
            "4",
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

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger["stages"]["stage4"]) == 1

    rc, out, _ = _invoke(
        ["report", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0
    report = json.loads(out)
    assert report["stages"]["stage4"]["sum_usd"] == 2.25
    assert report["global"]["sum_usd"] == 2.25


def test_record_refuses_when_stage_cap_exceeded(tmp_path: Path, capsys) -> None:
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    near_cap = tmp_path / "stage2-near-cap.json"
    over_cap = tmp_path / "stage2-over-cap.json"
    _write_budget_checkpoint(
        near_cap,
        run_id="stage2-near-cap",
        accrued_actual_usd=9.0,
        committed_unproven_usd=0.0,
    )
    _write_budget_checkpoint(
        over_cap,
        run_id="stage2-over-cap",
        accrued_actual_usd=2.0,
        committed_unproven_usd=0.0,
    )

    rc, _, _ = _invoke(
        [
            "record",
            "--stage",
            "2",
            "--budget-json",
            str(near_cap),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 0

    rc, out, _ = _invoke(
        [
            "record",
            "--stage",
            "2",
            "--budget-json",
            str(over_cap),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 3
    refusal = json.loads(out)
    assert refusal["recorded"] is False
    assert refusal["reason"] == "cap_exceeded"

    rc, out, _ = _invoke(["report", "--ledger", str(ledger_path), "--log", str(log_path)], capsys)
    assert rc == 0
    report = json.loads(out)
    assert report["stages"]["stage2"]["sum_usd"] == 9.0


def test_record_refuses_when_global_cap_exceeded(tmp_path: Path, capsys) -> None:
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    _write_stage_ledger(
        ledger_path,
        caps={
            "stage2": 10.0,
            "stage3": 10.0,
            "stage4": 10.0,
            "stage5": 10.0,
            "global": 5.0,
        },
        stages={
            "stage2": [],
            "stage3": [],
            "stage4": [],
            "stage5": [],
        },
    )

    stage2_budget = tmp_path / "global-cap-s2.json"
    stage3_budget = tmp_path / "global-cap-s3.json"
    _write_budget_checkpoint(
        stage2_budget,
        run_id="global-cap-s2",
        accrued_actual_usd=3.0,
        committed_unproven_usd=0.0,
    )
    _write_budget_checkpoint(
        stage3_budget,
        run_id="global-cap-s3",
        accrued_actual_usd=3.0,
        committed_unproven_usd=0.0,
    )

    rc, _, _ = _invoke(
        [
            "record",
            "--stage",
            "2",
            "--budget-json",
            str(stage2_budget),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 0

    rc, out, _ = _invoke(
        [
            "record",
            "--stage",
            "3",
            "--budget-json",
            str(stage3_budget),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 3
    refusal = json.loads(out)
    assert refusal["recorded"] is False
    assert refusal["reason"] == "cap_exceeded"
    assert refusal["global_remaining"] < 0


def test_check_admit_and_refuse_boundaries(tmp_path: Path, capsys) -> None:
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"

    rc, out, _ = _invoke(
        ["check", "--stage", "2", "--estimated-usd", "0", "--ledger", str(ledger_path), "--log", str(log_path)],
        capsys,
    )
    assert rc == 0
    fresh = json.loads(out)
    assert fresh["admitted"] is True
    assert fresh["stage_remaining"] == 10.0
    assert fresh["global_remaining"] == 115.0

    exact_budget = tmp_path / "stage2-exact-cap.json"
    _write_budget_checkpoint(
        exact_budget,
        run_id="stage2-exact-cap",
        accrued_actual_usd=10.0,
        committed_unproven_usd=0.0,
    )
    rc, _, _ = _invoke(
        [
            "record",
            "--stage",
            "2",
            "--budget-json",
            str(exact_budget),
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 0

    rc, out, _ = _invoke(
        [
            "check",
            "--stage",
            "2",
            "--estimated-usd",
            "0",
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 0
    on_boundary = json.loads(out)
    assert on_boundary["admitted"] is True
    assert on_boundary["stage_remaining"] == 0.0

    rc, out, _ = _invoke(
        [
            "check",
            "--stage",
            "2",
            "--estimated-usd",
            "0.01",
            "--ledger",
            str(ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 3
    stage_refusal = json.loads(out)
    assert stage_refusal["admitted"] is False

    global_ledger_path = tmp_path / "global-check-ledger.json"
    _write_stage_ledger(
        global_ledger_path,
        caps={
            "stage2": 100.0,
            "stage3": 100.0,
            "stage4": 100.0,
            "stage5": 100.0,
            "global": 1.0,
        },
        stages={
            "stage2": [],
            "stage3": [],
            "stage4": [],
            "stage5": [],
        },
    )

    rc, out, _ = _invoke(
        [
            "check",
            "--stage",
            "2",
            "--estimated-usd",
            "1",
            "--ledger",
            str(global_ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 0
    global_boundary = json.loads(out)
    assert global_boundary["admitted"] is True
    assert global_boundary["global_remaining"] == 0.0

    rc, out, _ = _invoke(
        [
            "check",
            "--stage",
            "2",
            "--estimated-usd",
            "1.01",
            "--ledger",
            str(global_ledger_path),
            "--log",
            str(log_path),
        ],
        capsys,
    )
    assert rc == 3
    global_refusal = json.loads(out)
    assert global_refusal["admitted"] is False
    assert global_refusal["global_remaining"] < 0


def test_report_outputs_expected_totals(tmp_path: Path, capsys) -> None:
    ledger_path = tmp_path / "stage-ledger.json"
    log_path = tmp_path / "stage-ledger.log"
    _write_stage_ledger(
        ledger_path,
        caps={
            "stage2": 10.0,
            "stage3": 25.0,
            "stage4": 40.0,
            "stage5": 40.0,
            "global": 115.0,
        },
        stages={
            "stage2": [
                {
                    "run_id": "r1",
                    "budget_json": "a.json",
                    "accrued_usd": 1.0,
                    "committed_unproven_usd": 2.0,
                    "recorded_at": "2026-07-20T00:00:00Z",
                },
                {
                    "run_id": "r2",
                    "budget_json": "b.json",
                    "accrued_usd": 0.5,
                    "committed_unproven_usd": 0.0,
                    "recorded_at": "2026-07-20T00:00:00Z",
                },
            ],
            "stage3": [
                {
                    "run_id": "r3",
                    "budget_json": "c.json",
                    "accrued_usd": 3.0,
                    "committed_unproven_usd": 1.0,
                    "recorded_at": "2026-07-20T00:00:00Z",
                }
            ],
            "stage4": [],
            "stage5": [],
        },
    )

    rc, out, _ = _invoke(["report", "--ledger", str(ledger_path), "--log", str(log_path)], capsys)
    assert rc == 0
    report = json.loads(out)

    assert report["stages"]["stage2"]["accrued_usd"] == 1.5
    assert report["stages"]["stage2"]["committed_unproven_usd"] == 2.0
    assert report["stages"]["stage2"]["sum_usd"] == 3.5
    assert report["stages"]["stage2"]["remaining_usd"] == 6.5
    assert report["stages"]["stage3"]["sum_usd"] == 4.0
    assert report["global"]["accrued_usd"] == 4.5
    assert report["global"]["committed_unproven_usd"] == 3.0
    assert report["global"]["sum_usd"] == 7.5
    assert report["global"]["remaining_usd"] == 107.5


def test_record_with_corrupt_budget_json_logs_error_and_returns_nonzero(tmp_path: Path, capsys) -> None:
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
    assert 'traceback=' in log_text
