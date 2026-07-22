"""Fixture-backed substrate conformance tests for canonical backgammon SxE mapping."""

from __future__ import annotations

import json
import math
import pathlib
import shutil
import sys
from typing import Any

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import backgammon_sxe as sx  # noqa: E402


FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
REAL_EVENTS_FIXTURE = FIXTURES_DIR / "substrate_stage7_kimi_off.events.jsonl"
REAL_USER_FIXTURE = FIXTURES_DIR / "substrate_stage7_kimi_off.user-events.jsonl"
ALLOWED_KINDS = {"user", "assistant", "reasoning", "tool", "edit"}


def _jsonl_records(path: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        payload = json.loads(line)
        assert isinstance(payload, dict), f"expected JSON object at {path}:{line_no}"
        records.append(payload)
    return records


def _event_time_like_builder(entry: dict[str, Any]) -> int:
    part = entry.get("part")
    if not isinstance(part, dict):
        raise AssertionError(f"fixture record missing part object: {entry}")
    part_time = part.get("time")
    if isinstance(part_time, dict) and "start" in part_time:
        return int(float(part_time["start"]))
    return int(float(entry["timestamp"]))


def _independent_kind_counts(
    *,
    events_path: pathlib.Path,
    sidecar_path: pathlib.Path,
) -> dict[str, int]:
    counts = {kind: 0 for kind in ALLOWED_KINDS}

    for user_entry in _jsonl_records(sidecar_path):
        if user_entry.get("type") != "user":
            continue
        if isinstance(user_entry.get("text"), str):
            counts["user"] += 1

    for entry in _jsonl_records(events_path):
        event_type = entry.get("type")
        if event_type in {"step_start", "step_finish"}:
            continue

        part = entry.get("part")
        if not isinstance(part, dict):
            raise AssertionError(f"fixture record missing part object: {entry}")

        metadata = part.get("metadata")
        openrouter_meta = metadata.get("openrouter") if isinstance(metadata, dict) else None
        reasoning_details = (
            openrouter_meta.get("reasoning_details") if isinstance(openrouter_meta, dict) else None
        )
        if isinstance(reasoning_details, list):
            for detail in reasoning_details:
                if not isinstance(detail, dict):
                    continue
                if detail.get("type") != "reasoning.text":
                    continue
                text = detail.get("text")
                if isinstance(text, str) and text.strip():
                    counts["reasoning"] += 1

        if event_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                counts["assistant"] += 1
            continue

        if event_type != "tool_use":
            continue

        tool_name = part.get("tool")
        state = part.get("state")
        state_input = state.get("input") if isinstance(state, dict) else None
        if tool_name in {"edit", "write"} and isinstance(state_input, dict):
            detail_key = "newString" if tool_name == "edit" else "content"
            if (
                isinstance(state_input.get("filePath"), str)
                and state_input["filePath"].strip()
                and isinstance(state_input.get(detail_key), str)
            ):
                counts["edit"] += 1
                continue

        counts["tool"] += 1

    return counts


def _materialize_real_fixture_session(
    tmp_path: pathlib.Path,
    *,
    include_sidecar: bool,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    session_dir = tmp_path / "session-root"
    worker_dir = session_dir / "nested" / "attempt-1"
    worker_dir.mkdir(parents=True, exist_ok=True)

    events_file = worker_dir / "worktree.events.jsonl"
    sidecar_file = worker_dir / "worktree.user-events.jsonl"
    shutil.copy2(REAL_EVENTS_FIXTURE, events_file)
    if include_sidecar:
        shutil.copy2(REAL_USER_FIXTURE, sidecar_file)

    return session_dir, events_file, sidecar_file


def _tool_entry_by_call_id(events_path: pathlib.Path, *, call_id: str, tool: str) -> dict[str, Any]:
    for entry in _jsonl_records(events_path):
        if entry.get("type") != "tool_use":
            continue
        part = entry.get("part")
        if not isinstance(part, dict):
            continue
        if part.get("callID") == call_id and part.get("tool") == tool:
            return entry
    raise AssertionError(f"expected fixture to contain tool_use callID={call_id!r} tool={tool!r}")


def _expected_edit_entries(events_path: pathlib.Path) -> list[dict[str, Any]]:
    edits: list[dict[str, Any]] = []
    for entry in _jsonl_records(events_path):
        if entry.get("type") != "tool_use":
            continue
        part = entry.get("part")
        if not isinstance(part, dict):
            continue

        tool_name = part.get("tool")
        if tool_name not in {"edit", "write"}:
            continue

        state = part.get("state")
        state_input = state.get("input") if isinstance(state, dict) else None
        if not isinstance(state_input, dict):
            continue

        file_path = state_input.get("filePath")
        detail_key = "newString" if tool_name == "edit" else "content"
        detail = state_input.get(detail_key)
        if isinstance(file_path, str) and file_path.strip() and isinstance(detail, str):
            edits.append({"time": _event_time_like_builder(entry), "file": file_path, "detail": detail})

    return edits


def _mcp_invalid_event_indexes(events: list[Any]) -> list[int]:
    invalid: list[int] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            invalid.append(index)
            continue

        kind = event.get("kind")
        time_value = event.get("time")
        seq_value = event.get("seq")
        if kind not in ALLOWED_KINDS:
            invalid.append(index)
            continue
        if isinstance(time_value, bool) or not isinstance(time_value, (int, float)) or not math.isfinite(float(time_value)):
            invalid.append(index)
            continue
        if isinstance(seq_value, bool) or not isinstance(seq_value, (int, float)) or not math.isfinite(float(seq_value)):
            invalid.append(index)
            continue
    return invalid


def test_real_session_construction_matches_independent_counts_and_shape(tmp_path: pathlib.Path) -> None:
    session_dir, copied_events_file, _ = _materialize_real_fixture_session(tmp_path, include_sidecar=True)

    events, stats, events_files = sx._build_substrate_events(session_dir=session_dir)

    assert events_files == [copied_events_file.resolve()]

    expected_counts = _independent_kind_counts(
        events_path=REAL_EVENTS_FIXTURE,
        sidecar_path=REAL_USER_FIXTURE,
    )
    assert stats["kind_counts"] == expected_counts
    assert stats["event_count"] == sum(expected_counts.values())
    assert len(events) == sum(expected_counts.values())

    computed_counts = {kind: 0 for kind in ALLOWED_KINDS}
    seqs: list[int] = []
    for event in events:
        kind = event.get("kind")
        assert kind in ALLOWED_KINDS

        time_value = event.get("time")
        assert not isinstance(time_value, bool)
        assert isinstance(time_value, (int, float))
        assert math.isfinite(float(time_value))

        seq_value = event.get("seq")
        assert isinstance(seq_value, int)
        assert seq_value >= 0
        seqs.append(seq_value)

        computed_counts[kind] += 1

    assert computed_counts == expected_counts
    assert seqs == sorted(seqs)
    assert seqs == list(range(len(events)))

    ordered = sorted(events, key=lambda item: (item["time"], item["seq"]))
    ordered_pairs = [(item["time"], item["seq"]) for item in ordered]
    assert ordered_pairs == sorted(ordered_pairs)


def test_sidecar_user_messages_are_preserved_exactly(tmp_path: pathlib.Path) -> None:
    session_dir, _, _ = _materialize_real_fixture_session(tmp_path, include_sidecar=True)
    events, _, _ = sx._build_substrate_events(session_dir=session_dir)

    user_records = _jsonl_records(REAL_USER_FIXTURE)
    expected_user_text = [entry["text"] for entry in user_records]
    expected_user_timestamps = [int(entry["timestamp"]) for entry in user_records]

    user_events = [event for event in events if event["kind"] == "user"]
    assert [event["text"] for event in user_events] == expected_user_text
    assert [event["time"] for event in user_events] == expected_user_timestamps


def test_specific_tool_call_maps_input_output_exit_and_status(tmp_path: pathlib.Path) -> None:
    # Fixture probe: line 33 carries callID=functions.bash:15, tool=bash.
    source = _tool_entry_by_call_id(REAL_EVENTS_FIXTURE, call_id="functions.bash:15", tool="bash")
    part = source["part"]
    state = part["state"]
    expected_input = json.dumps(state["input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected_time = _event_time_like_builder(source)

    session_dir, _, _ = _materialize_real_fixture_session(tmp_path, include_sidecar=True)
    events, _, _ = sx._build_substrate_events(session_dir=session_dir)

    matching = [
        event
        for event in events
        if event.get("kind") == "tool"
        and event.get("name") == "bash"
        and event.get("time") == expected_time
        and event.get("input") == expected_input
    ]
    assert matching, "expected mapped tool event for fixture callID=functions.bash:15 tool=bash"

    event = matching[0]
    metadata = state.get("metadata") if isinstance(state, dict) else None
    assert event["output"] == state["output"]
    assert event["exit"] == (metadata.get("exit") if isinstance(metadata, dict) else None)
    assert event["status"] == state["status"]


def test_edit_events_emit_file_and_detail_or_fallback_synthetic_case(tmp_path: pathlib.Path) -> None:
    expected_edits = _expected_edit_entries(REAL_EVENTS_FIXTURE)
    if expected_edits:
        session_dir, _, _ = _materialize_real_fixture_session(tmp_path, include_sidecar=True)
        events, _, _ = sx._build_substrate_events(session_dir=session_dir)

        edit_events = [event for event in events if event.get("kind") == "edit"]
        assert edit_events, "fixture contains edit/write tool calls; mapped edit events must exist"
        assert all(isinstance(event.get("file"), str) and event["file"].strip() for event in edit_events)
        assert all(isinstance(event.get("detail"), str) for event in edit_events)

        probe = min(expected_edits, key=lambda entry: len(entry["detail"]))
        assert any(
            event.get("file") == probe["file"] and event.get("detail") == probe["detail"]
            for event in edit_events
        )
        return

    # Fallback branch required by the canon conformance task if a real fixture has no edits.
    session_dir = tmp_path / "synthetic-session"
    worker_dir = session_dir / "nested"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "worktree.user-events.jsonl").write_text(
        '{"type":"user","timestamp":1700000000000,"attempt":1,"text":"initial prompt"}\n',
        encoding="utf-8",
    )
    (worker_dir / "worktree.events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1700000000001,
                        "part": {
                            "tool": "edit",
                            "state": {
                                "status": "completed",
                                "input": {"filePath": "src/a.ts", "newString": "new body"},
                                "output": "ok",
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1700000000002,
                        "part": {
                            "tool": "write",
                            "state": {
                                "status": "completed",
                                "input": {"filePath": "src/b.ts", "content": "export const b = 1;"},
                                "output": "ok",
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events, _, _ = sx._build_substrate_events(session_dir=session_dir)
    edit_events = [event for event in events if event.get("kind") == "edit"]
    assert len(edit_events) == 2
    assert {(event["file"], event["detail"]) for event in edit_events} == {
        ("src/a.ts", "new body"),
        ("src/b.ts", "export const b = 1;"),
    }


def test_substrate_excludes_oracle_or_gate_runner_private_markers(tmp_path: pathlib.Path) -> None:
    session_dir, _, _ = _materialize_real_fixture_session(tmp_path, include_sidecar=True)
    events, _, _ = sx._build_substrate_events(session_dir=session_dir)

    text_blob = "\n".join(
        value
        for event in events
        for value in (
            event.get("text"),
            event.get("name"),
            event.get("input"),
            event.get("output"),
            event.get("error"),
            event.get("file"),
            event.get("detail"),
        )
        if isinstance(value, str)
    )
    lower_blob = text_blob.lower()

    # This fixture legitimately contains "tasks/backgammon/golden" in a user-space
    # config read; conformance excludes private oracle/gate-runner internals.
    assert "ORACLE" not in text_blob
    assert "backgammon_oracle" not in lower_blob
    assert "gate-runner" not in lower_blob
    assert "gate_runner" not in lower_blob
    assert "oracle verdict" not in lower_blob


def test_missing_sidecar_is_hard_error_no_transcript_fallback(tmp_path: pathlib.Path) -> None:
    session_dir, _, _ = _materialize_real_fixture_session(tmp_path, include_sidecar=False)
    with pytest.raises(RuntimeError, match="missing user sidecar for worker events file"):
        sx._build_substrate_events(session_dir=session_dir)


def test_built_events_satisfy_mcp_invalid_events_contract_roundtrip(tmp_path: pathlib.Path) -> None:
    session_dir, _, _ = _materialize_real_fixture_session(tmp_path, include_sidecar=True)
    events, _, _ = sx._build_substrate_events(session_dir=session_dir)

    assert _mcp_invalid_event_indexes(events) == []

    payload = json.loads(json.dumps({"events": events}, ensure_ascii=False))
    assert isinstance(payload.get("events"), list)
    assert _mcp_invalid_event_indexes(payload["events"]) == []
