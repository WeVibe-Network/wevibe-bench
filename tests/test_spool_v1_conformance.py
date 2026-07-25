from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "ledger" / "spool-v1.schema.json"
FIXTURE_PATH = REPO_ROOT / "scaffold" / "wevibe-mcp-clone" / "tests" / "fixtures" / "spool-v1.plugin-produced.jsonl"

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


def envelope(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": "spool-v1",
        "seq": 0,
        "ts": "2026-07-26T00:00:00.000Z",
        "session_id": "s",
        "trace_id": None,
        "event": event,
        "payload": payload,
    }


def _assert_valid(record: dict[str, Any]) -> None:
    errors = sorted(VALIDATOR.iter_errors(record), key=lambda err: list(err.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        pytest.fail(f"Expected valid record, got validation errors: {details}")


def _assert_invalid(record: dict[str, Any]) -> None:
    errors = list(VALIDATOR.iter_errors(record))
    assert errors, "Expected validation failure, but record validated successfully"


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_strings(nested)
        return
    if isinstance(value, list):
        for nested in value:
            yield from _iter_strings(nested)


def test_spool_v1_schema_is_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def test_plugin_produced_fixture_validates_and_pins_contract_expectations() -> None:
    assert FIXTURE_PATH.exists(), f"Missing fixture file: {FIXTURE_PATH}"

    lines = [line for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 13, f"Expected 13 non-empty fixture lines, got {len(lines)}"

    records = [json.loads(line) for line in lines]

    for idx, record in enumerate(records, start=1):
        errors = list(VALIDATOR.iter_errors(record))
        assert not errors, f"Fixture line {idx} failed schema validation: {[e.message for e in errors]}"

    expected_events = {
        "session.created",
        "session.idle",
        "session.error",
        "tool.execute.before",
        "tool.execute.after",
        "file.edited",
        "file.watcher.updated",
        "lsp.client.diagnostics",
        "command.executed",
        "gstv.attach.attempt",
        "gstv.boundary.run",
    }
    observed_events = {record["event"] for record in records}
    assert observed_events == expected_events

    has_2060_truncated_string = any(
        len(s) == 2060 and s.endswith("…[truncated]")
        for record in records
        for s in _iter_strings(record.get("payload", {}))
    )
    assert has_2060_truncated_string, "Expected at least one payload string to be exactly 2060 chars and truncated"

    has_empty_session_error = any(
        record.get("event") == "session.error" and record.get("payload") == {}
        for record in records
    )
    assert has_empty_session_error, "Expected at least one session.error payload to be {}"


def test_stale_shape_records_are_rejected() -> None:
    bad_records = [
        envelope("session.created", {"repo_root": "/x"}),
        envelope("session.created", {"directory": "/x", "repo_root": "/x"}),
        envelope("session.created", {}),
        envelope("session.error", {"error": "x"}),
        envelope("tool.execute.before", {"call_id": "c", "tool": "t", "unexpected": 1}),
        envelope("lsp.client.diagnostics", {"path": "p", "diagnostics": []}),
        envelope("command.executed", {"command": "c", "exit_code": 0}),
        envelope("command.executed", {"command": "c", "args_excerpt": "a", "exit_code": 0}),
        {**envelope("session.idle", {}), "unexpected_top_level": True},
    ]

    for record in bad_records:
        _assert_invalid(record)


def test_excerpt_length_boundaries() -> None:
    truncated_2060 = "x" * 2048 + "…[truncated]"
    too_long_2061 = "y" * 2061

    _assert_valid(
        envelope(
            "tool.execute.after",
            {
                "call_id": "c",
                "tool": "t",
                "output_excerpt": truncated_2060,
            },
        )
    )
    _assert_invalid(
        envelope(
            "tool.execute.after",
            {
                "call_id": "c",
                "tool": "t",
                "output_excerpt": too_long_2061,
            },
        )
    )
    _assert_valid(envelope("tool.execute.before", {"call_id": "c", "tool": "t", "args_excerpt": truncated_2060}))
    _assert_valid(envelope("command.executed", {"command": "c", "args_excerpt": truncated_2060}))


def test_conformant_positive_examples() -> None:
    _assert_valid(envelope("session.created", {"directory": "/x"}))
    _assert_valid(envelope("session.created", {"directory": "/x", "worktree": "/y"}))
    _assert_valid(envelope("command.executed", {"command": "c"}))
    _assert_valid(envelope("session.error", {}))
