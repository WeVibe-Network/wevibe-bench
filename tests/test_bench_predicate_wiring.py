from __future__ import annotations

import json
from pathlib import Path

from wevibe_bench.adapters.backgammon import BackgammonRunner
from wevibe_bench.adapters.bench_report import parse_failing_ids


def _make_runner(*, tmp_path: Path, task_dir: Path, memory_mode: str = "on") -> tuple[BackgammonRunner, Path]:
    work_root = tmp_path / "work_root"
    runner = BackgammonRunner(
        task_dir=task_dir,
        work_root=work_root,
        model="test-model",
        memory_mode=memory_mode,
    )
    # _repo_root is derived from __file__ and would point at the real package
    # root; pin it to the test tmp root so the source org.json resolves locally.
    runner._repo_root = tmp_path
    return runner, work_root


def _write_org_json(tmp_path: Path) -> Path:
    org_dir = tmp_path / ".wevibe"
    org_dir.mkdir(parents=True, exist_ok=True)
    org_path = org_dir / "org.json"
    org_path.write_text('{"org": "test-org"}', encoding="utf-8")
    return org_path


def _write_runner_source(task_dir: Path) -> Path:
    bench_dir = task_dir / "bench"
    bench_dir.mkdir(parents=True, exist_ok=True)
    runner_path = bench_dir / "bench-check.mjs"
    runner_path.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return runner_path


def _predicate_dict(worktree: Path) -> dict[str, str]:
    predicate_path = worktree / ".wevibe" / "predicate.json"
    return json.loads(predicate_path.read_text(encoding="utf-8"))


def test_predicate_json_written_with_exact_shape_when_runner_present(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    _write_runner_source(task_dir)
    _write_org_json(tmp_path)
    runner, _work_root = _make_runner(tmp_path=tmp_path, task_dir=task_dir)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    runner._prepare_memory_mode(worktree=worktree)

    assert (worktree / ".wevibe" / "predicate.json").is_file()
    assert _predicate_dict(worktree) == {
        "reporter": "bench-fixture",
        "command": "node bench-check.mjs",
    }
    # runner copied to the worktree root when the source exists
    assert (worktree / "bench-check.mjs").is_file()


def test_predicate_json_written_when_runner_source_missing(tmp_path: Path) -> None:
    # task_dir has no bench/bench-check.mjs -> degradation path, no crash
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_org_json(tmp_path)
    runner, _work_root = _make_runner(tmp_path=tmp_path, task_dir=task_dir)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    runner._prepare_memory_mode(worktree=worktree)

    # predicate.json still written even when the runner source is missing
    assert (worktree / ".wevibe" / "predicate.json").is_file()
    assert _predicate_dict(worktree) == {
        "reporter": "bench-fixture",
        "command": "node bench-check.mjs",
    }
    assert not (worktree / "bench-check.mjs").exists()


def test_parse_failing_ids_matches_expected_order_ignoring_noise(tmp_path: Path) -> None:
    report = "\n".join(
        [
            "WEVIBE-BENCH-REPORT v1",
            '{"test":"REQ-PIP","status":"pass"}',
            '{"test":"REQ-TURN","status":"fail"}',
            "not-json-at-all",
            "",
            '{"test":"REQ-CUBE-STATE","status":"fail"}',
            '{"test":"REQ-TURN","status":"fail"}',  # dup -> first-wins, ignored
            '{"test":"","status":"fail"}',  # empty test -> ignored
            '{"test":"REQ-DEBUG","status":"skip"}',  # invalid status -> ignored
            '{"test":"REQ-COMPLETE","status":"pass"}',
            '{"test":"REQ-INIT","status":"fail"}',
        ]
    )

    assert parse_failing_ids(report) == [
        "REQ-TURN",
        "REQ-CUBE-STATE",
        "REQ-INIT",
    ]


def test_parse_failing_ids_returns_empty_when_header_absent() -> None:
    report = '\n'.join(
        [
            '{"test":"REQ-TURN","status":"fail"}',
            '{"test":"REQ-PIP","status":"pass"}',
        ]
    )

    assert parse_failing_ids(report) == []


def test_parse_failing_ids_returns_empty_when_header_is_blank_only() -> None:
    report = "\n\n\n"

    assert parse_failing_ids(report) == []


def test_parse_failing_ids_returns_empty_on_all_pass() -> None:
    report = "\n".join(
        [
            "WEVIBE-BENCH-REPORT v1",
            '{"test":"REQ-PIP","status":"pass"}',
            '{"test":"REQ-TURN","status":"pass"}',
        ]
    )

    assert parse_failing_ids(report) == []


def test_parse_failing_ids_dedupes_first_wins(tmp_path: Path) -> None:
    report = "\n".join(
        [
            "WEVIBE-BENCH-REPORT v1",
            '{"test":"REQ-TURN","status":"fail"}',
            '{"test":"REQ-TURN","status":"fail"}',
            '{"test":"REQ-TURN","status":"pass"}',
        ]
    )

    assert parse_failing_ids(report) == ["REQ-TURN"]


def test_parse_failing_ids_handles_leading_blank_lines_before_header() -> None:
    report = "\n".join(
        [
            "",
            "  WEVIBE-BENCH-REPORT v1  ",
            '{"test":"REQ-TURN","status":"fail"}',
        ]
    )

    assert parse_failing_ids(report) == ["REQ-TURN"]