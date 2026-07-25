from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from wevibe_bench.ledger.generate import generate_run_ledger


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO_ROOT / "ledger" / "gstv-run-v1.schema.json").read_text(encoding="utf-8"))
CATALOG = json.loads((REPO_ROOT / "ledger" / "gstv-ops-catalog.json").read_text(encoding="utf-8"))
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "gstv-ledger-run"


def _validate(ledger: dict) -> None:
    errors = list(Draft202012Validator(SCHEMA).iter_errors(ledger))
    assert not errors, [error.message for error in errors]


def _summary_fields() -> list[str]:
    for row in CATALOG["ops"]:
        if row["op"] == "gstv.run_summary":
            return row["fields"]
    raise AssertionError("gstv.run_summary not found in catalog")


def _parse_summary_line(line: str) -> tuple[str, dict[str, str]]:
    parts = line.strip().split()
    assert parts[1] == "INFO"
    kv: dict[str, str] = {}
    for token in parts[2:]:
        key, value = token.split("=", 1)
        kv[key] = value
    return parts[0], kv


def test_full_fixture_counts_schema_and_summary_log(tmp_path: Path) -> None:
    full = FIXTURES / "full"
    ledger = generate_run_ledger(
        "run-full",
        ops_dir=str(full / "ops"),
        leader_log_paths=[str(full / "leader.log")],
        serve_inject_log_paths=[str(full / "serve_inject.log")],
        scorecard_paths=[str(full / "scorecard.json")],
        out_dir=str(tmp_path),
    )

    _validate(ledger)
    assert len(ledger["goals"]) == 1
    goal = ledger["goals"][0]
    assert goal == {
        "goal_id": "goal-1",
        "seal_fp": "seal1111",
        "closed": True,
        "attempts_to_green": 1,
        "sessions": 1,
        "links": 2,
        "gaps": 0,
        "red_boundaries": 1,
        "receipts": {"predicate_fps": ["pred1111"], "negative_fps": []},
        "unlock_fp": "unlock111",
        "signal_key_mode": "parsed",
    }

    assert ledger["signal_key_mode"] == "parsed"
    assert ledger["cadence"]["recalls"] == 0
    assert ledger["cadence"]["gate_events"] == 1
    assert ledger["cadence"]["injections"] == 2
    assert ledger["cadence"]["serves"] == 1
    assert ledger["cadence"]["unattributed_vector_only"] == 1
    assert ledger["extraction"] == {
        "resolved": 1,
        "emitted": 2,
        "empty_reason": None,
        "invariant_violation": False,
    }

    assert len(ledger["problems"]) == 1
    problem = ledger["problems"][0]
    assert problem["signal_key"] == "sig/key"
    assert problem["episode_id"] == "ep-1"
    assert problem["attempt_diff_fp"] == "adiff123"
    assert problem["candidate_hash"] == "cand1111"
    assert problem["leader_decision"] == "verify"
    assert problem["committed_cid"] == "abc123ef"
    assert problem["injected_memory_overlap"] is True

    assert "spool input absent" in ledger["integrity"]["gaps_disclosed"]
    assert "leader input absent" not in ledger["integrity"]["gaps_disclosed"]
    assert "serve/inject inputs absent" not in ledger["integrity"]["gaps_disclosed"]
    assert "extraction.integrity input absent" not in ledger["integrity"]["gaps_disclosed"]

    summary_path = tmp_path / "gstv-run-run-full.run_summary.log"
    assert summary_path.exists()
    ts, values = _parse_summary_line(summary_path.read_text(encoding="utf-8").strip())
    assert ts.endswith("Z")
    assert list(values.keys()) == ["op", *_summary_fields()]
    assert values["op"] == "gstv.run_summary"
    assert values["trace"] == "ledger-run-full"
    assert values["goals"] == "1"
    assert values["episodes_open"] == "1"
    assert values["episodes_closed"] == "1"
    assert values["coincidental"] == "0"
    assert values["receipts_predicate"] == "1"
    assert values["receipts_negative"] == "0"
    assert values["unattributed_vector_only"] == "1"
    assert values["signal_key_mode"] == "parsed"
    assert values["status"] == "ok"


def test_sparse_fixture_honest_absence_and_ops_coverage() -> None:
    sparse = FIXTURES / "sparse"
    ledger = generate_run_ledger("run-sparse", ops_dir=str(sparse / "ops"))
    _validate(ledger)

    assert len(ledger["goals"]) == 1
    goal = ledger["goals"][0]
    assert goal["closed"] is False
    assert goal["attempts_to_green"] is None
    assert goal["sessions"] == 0
    assert goal["links"] == 0
    assert goal["gaps"] == 0
    assert goal["red_boundaries"] is None
    assert goal["unlock_fp"] is None

    gaps = set(ledger["integrity"]["gaps_disclosed"])
    assert "spool input absent" in gaps
    assert "leader input absent" in gaps
    assert "serve/inject inputs absent" in gaps
    assert "extraction.integrity input absent" in gaps
    assert "utilization_proxy inputs absent (side inputs not provided)" in gaps

    absent = set(ledger["integrity"]["ops_coverage"]["absent"])
    assert "gstv.goal.close" in absent
    assert "gstv.extraction.unlock" in absent
    assert "negative.receipt" in absent
    assert "predicate.receipt" in absent


def test_empty_ops_dir_all_absent_validates(tmp_path: Path) -> None:
    ledger = generate_run_ledger("run-empty", ops_dir=str(tmp_path))
    _validate(ledger)
    assert ledger["goals"] == []
    assert ledger["problems"] == []
    assert "ops input absent" in ledger["integrity"]["gaps_disclosed"]


def test_utilization_proxy_with_and_without_side_inputs() -> None:
    full = FIXTURES / "full"

    no_inputs = generate_run_ledger(
        "run-no-inputs",
        ops_dir=str(full / "ops"),
        leader_log_paths=[str(full / "leader.log")],
        serve_inject_log_paths=[str(full / "serve_inject.log")],
    )
    assert no_inputs["utilization_proxy"]["pairs"] == []
    assert "utilization_proxy inputs absent (side inputs not provided)" in no_inputs["integrity"]["gaps_disclosed"]

    with_inputs = generate_run_ledger(
        "run-with-inputs",
        ops_dir=str(full / "ops"),
        leader_log_paths=[str(full / "leader.log")],
        serve_inject_log_paths=[str(full / "serve_inject.log")],
        memory_implement_texts={"abc123ef": "alpha beta gamma"},
        attempt_diff_texts={"adiff123": "alpha beta delta"},
    )
    assert with_inputs["utilization_proxy"]["pairs"] == [
        {
            "memory_fp": "abc123ef",
            "attempt_diff_fp": "adiff123",
            "similarity": 0.5,
        }
    ]


def test_problems_row_untraceable_candidate_is_null(tmp_path: Path) -> None:
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    (ops_dir / "ops.log").write_text(
        "\n".join(
            [
                "2026-07-26T14:00:00.000Z INFO op=episode.open trace=tr-x session_id=s-x episode_id=ep-x signal_key=s/x signal_key_mode=parsed status=ok",
                "2026-07-26T14:00:00.100Z INFO op=episode.close trace=tr-x session_id=s-x episode_id=ep-x signal_key=s/x outcome=resolved attempt_diff_fp=- coincidental_flip=false status=ok",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ledger = generate_run_ledger("run-problem-null", ops_dir=str(ops_dir))
    _validate(ledger)
    problem = ledger["problems"][0]
    assert problem["attempt_diff_fp"] is None
    assert problem["candidate_hash"] is None
    assert problem["committed_cid"] is None
