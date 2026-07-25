from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from wevibe_bench.ledger.model import (
    Cadence,
    Extraction,
    GoalEntry,
    GoalReceipts,
    Integrity,
    OpsCoverage,
    ProblemEntry,
    RunLedger,
    UtilizationPair,
    UtilizationProxy,
)
from wevibe_bench.ledger.parsers import (
    extraction_integrity_records,
    parse_leader_signer_log,
    parse_ops_log,
    parse_serve_inject_lines,
    parse_spool_jsonl,
    read_json_file,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "ledger" / "gstv-run-v1.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "gstv-ledger"

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


def test_schema_is_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def test_parse_ops_log_records_and_skipped_lines() -> None:
    parsed = parse_ops_log(FIXTURES / "ops-sample.log")
    assert len(parsed.records) == 3
    assert parsed.skipped_lines == 1
    assert parsed.records[0].op == "episode.open"
    assert parsed.records[1].op == "gstv.goal.close"
    assert parsed.records[1].fields["goal_id"] == "goal-1"
    assert parsed.records[2].op == "episode.close"


def test_parse_spool_jsonl_tolerates_truncated_final_line() -> None:
    parsed = parse_spool_jsonl(FIXTURES / "spool-sample.jsonl")
    assert len(parsed.envelopes) == 3
    assert parsed.truncated_final is True
    assert parsed.envelopes[0]["event"] == "session.created"


def test_extraction_integrity_records_filters_and_types() -> None:
    parsed = parse_ops_log(FIXTURES / "extraction-integrity.log")
    records = extraction_integrity_records(parsed)
    assert len(records) == 2
    assert records[0]["job_id"] == "job-1"
    assert records[0]["resolved_problem_count"] == 2
    assert records[0]["invariant_violation"] is False
    assert records[1]["outcome"] == "failed"
    assert records[1]["resolved_problem_count"] is None
    assert records[1]["invariant_violation"] is True


def test_parse_leader_signer_log_defensive_decision_extraction() -> None:
    parsed = parse_leader_signer_log(FIXTURES / "leader-signer.log")
    assert parsed.skipped_lines == 1
    assert len(parsed.decisions) == 2
    assert parsed.decisions[0]["decision"] == "verify"
    assert parsed.decisions[0]["trace"] == "tr-1"
    assert parsed.decisions[1]["decision"] == "deny"


def test_parse_serve_inject_lines_counts_and_receipt_failures() -> None:
    lines = (FIXTURES / "serve-inject.log").read_text(encoding="utf-8").splitlines()
    parsed = parse_serve_inject_lines(lines)
    assert parsed.serves == 1
    assert parsed.injections == 3
    assert parsed.block_chars_total == 1700
    assert parsed.serve_receipt_failures == [{"status": 400, "cid_fp": "abcdef12"}]


def test_missing_files_are_honest_absence_not_exceptions(tmp_path: Path) -> None:
    missing = tmp_path / "missing.log"
    assert parse_ops_log(missing).records == []
    assert parse_ops_log(missing).skipped_lines == 0
    assert parse_spool_jsonl(missing).envelopes == []
    assert parse_spool_jsonl(missing).truncated_final is False
    assert parse_leader_signer_log(missing).decisions == []
    assert parse_leader_signer_log(missing).skipped_lines == 0
    assert read_json_file(missing) is None


def test_read_json_file_defensive_contract() -> None:
    assert read_json_file(FIXTURES / "scorecard.json") == {
        "run_id": "run-1",
        "label": "sample",
        "score": 0.5,
    }
    assert read_json_file(FIXTURES / "backgammon-detail.json") == {
        "cells": [{"attempts_to_green": 2, "delivery": "green"}]
    }


def test_minimal_run_ledger_model_validates_against_schema() -> None:
    ledger = RunLedger(
        run_id="run-1",
        generated_at="2026-07-26T12:00:00.000Z",
        signal_key_mode="absent",
        goals=[
            GoalEntry(
                goal_id="goal-1",
                seal_fp=None,
                closed=True,
                attempts_to_green=None,
                sessions=0,
                links=0,
                gaps=0,
                red_boundaries=None,
                receipts=GoalReceipts(predicate_fps=[], negative_fps=[]),
                unlock_fp=None,
                signal_key_mode="absent",
            )
        ],
        problems=[
            ProblemEntry(
                signal_key="missing/signal",
                episode_id=None,
                attempt_diff_fp=None,
                candidate_hash=None,
                leader_decision=None,
                committed_cid=None,
                injected_memory_overlap=None,
            )
        ],
        cadence=Cadence(
            recalls=0,
            gate_events=0,
            injections=0,
            serves=0,
            unattributed_vector_only=0,
            basis="ops+spool absent",
        ),
        extraction=Extraction(
            resolved=0,
            emitted=0,
            empty_reason=None,
            invariant_violation=False,
        ),
        utilization_proxy=UtilizationProxy(
            pairs=[
                UtilizationPair(
                    memory_fp="mem00001",
                    attempt_diff_fp="adiff001",
                    similarity=0.0,
                )
            ]
        ),
        integrity=Integrity(
            gaps_disclosed=["missing:ops-log", "missing:leader-signer-log"],
            ops_coverage=OpsCoverage(present=["gstv.goal.close"], absent=["gstv.run_summary"]),
        ),
    )
    errors = list(VALIDATOR.iter_errors(ledger.to_dict()))
    assert not errors, [error.message for error in errors]
