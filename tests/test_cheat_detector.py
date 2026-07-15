from __future__ import annotations

import json
from pathlib import Path

from wevibe_bench.adapters.cheat_detector import build_oracle_markers, scan_events_for_oracle_access


def _write_events(path: Path, rows: list[object]) -> None:
    lines: list[str] = []
    for row in rows:
        if isinstance(row, str):
            lines.append(row)
        else:
            lines.append(json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _markers(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    gates_dir = (tmp_path / "oracle" / "gates").resolve()
    golden_dir = (tmp_path / "oracle" / "golden").resolve()
    markers = build_oracle_markers(gates_dir=gates_dir, golden_dir=golden_dir)
    return gates_dir, golden_dir, markers


def test_scan_events_clean_transcript_has_no_cheat_hits(tmp_path: Path) -> None:
    _, _, markers = _markers(tmp_path)
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "type": "step_delta",
                "part": {
                    "type": "tool",
                    "tool": "read",
                    "callID": "call-1",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": str((tmp_path / "worktree" / "src" / "game.ts").resolve())},
                    },
                },
            },
            "",
            "not-json",
            {
                "type": "step_delta",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "callID": "call-2",
                    "state": {"status": "completed", "input": {"command": "npm test"}},
                },
            },
        ],
    )

    finding = scan_events_for_oracle_access(events_path=events_path, markers=markers)

    assert finding.cheated is False
    assert finding.hits == []


def test_scan_events_read_of_absolute_gates_path_is_cheat(tmp_path: Path) -> None:
    gates_dir, _, markers = _markers(tmp_path)
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "type": "step_delta",
                "part": {
                    "type": "tool",
                    "tool": "read",
                    "callID": "call-read-gates",
                    "state": {"status": "completed", "input": {"filePath": str(gates_dir)}},
                },
            }
        ],
    )

    finding = scan_events_for_oracle_access(events_path=events_path, markers=markers)

    assert finding.cheated is True
    assert any(hit.tool == "read" for hit in finding.hits)


def test_scan_events_bash_cat_report_is_cheat(tmp_path: Path) -> None:
    gates_dir, _, markers = _markers(tmp_path)
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "type": "step_delta",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "callID": "call-bash-report",
                    "state": {
                        "status": "completed",
                        "input": {"command": f"cat {gates_dir / 'report.mjs'}"},
                    },
                },
            }
        ],
    )

    finding = scan_events_for_oracle_access(events_path=events_path, markers=markers)

    assert finding.cheated is True
    assert any(hit.tool == "bash" for hit in finding.hits)
    assert any(hit.marker == "report.mjs" or hit.marker == str(gates_dir) for hit in finding.hits)


def test_scan_events_assistant_text_mentioning_oracle_does_not_count(tmp_path: Path) -> None:
    _, _, markers = _markers(tmp_path)
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "type": "step_delta",
                "part": {
                    "type": "text",
                    "text": "I might look at report.mjs but I have not called any tool yet.",
                },
            }
        ],
    )

    finding = scan_events_for_oracle_access(events_path=events_path, markers=markers)

    assert finding.cheated is False


def test_scan_events_legit_worker_edit_in_worktree_is_not_cheat(tmp_path: Path) -> None:
    _, _, markers = _markers(tmp_path)
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            {
                "type": "step_delta",
                "part": {
                    "type": "tool",
                    "tool": "edit",
                    "callID": "call-edit-local",
                    "state": {
                        "status": "completed",
                        "input": {
                            "filePath": str((tmp_path / "worktree" / "tests" / "foo.test.ts").resolve()),
                            "oldString": "before",
                            "newString": "after",
                        },
                    },
                },
            }
        ],
    )

    finding = scan_events_for_oracle_access(events_path=events_path, markers=markers)

    assert finding.cheated is False


def test_build_oracle_markers_contains_abs_gates_path_and_report_marker(tmp_path: Path) -> None:
    gates_dir, _, markers = _markers(tmp_path)

    assert str(gates_dir) in markers
    assert "report.mjs" in markers
