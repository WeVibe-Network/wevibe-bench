"""Unit tests for wevibe_bench.cumulative.run_artifacts (WO-RUNSTATUS-1 chunk A)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from wevibe_bench.cumulative.run_artifacts import (
    RUN_ARTIFACTS_SCHEMA_VERSION,
    RunManifest,
    StatusStream,
    build_scorecard,
    default_run_manifest_path,
    default_status_stream_path,
    load_run_manifest,
    write_run_manifest,
)


def _manifest() -> RunManifest:
    return RunManifest(
        run_id="run-abc",
        created_at="2026-08-05T00:00:00Z",
        served_model="qwen3.6-35b-bench",
        requested_model="local-llm-proxy/qwen3.6-35b-bench",
        memory_mode="on",
        org_id="org-1",
        source_commit="deadbeef1234",
        worker_image_fingerprint={"sha256": "abcd1234"},
        seed=42,
        template_hash="tpl-1",
        roster_fingerprint="rost-1",
    )


def _progress(*, full_green: bool, resolved: int, total_tokens: int) -> dict:
    return {
        "problems_before": 5,
        "problems_after": 2,
        "resolved_count": resolved,
        "remaining_count": 1,
        "full_green": full_green,
        "attempts_to_green": 1,
        "turns": 10,
        "total_tokens": total_tokens,
        "wall_seconds": 12.5,
        "wall_cost_usd": 0.0,
        "tool_calls": 20,
        "test_invocations": 5,
        "agentic_cycles": 3,
    }


def _status_record(
    *,
    sequence_index: int,
    session_fp: str,
    session_id: str,
    progress: dict | None,
    verdict: str = "pass",
) -> dict:
    return {
        "type": "attempt",
        "schema_version": 1,
        "sequence_index": sequence_index,
        "memory_mode": "on",
        "org_id": "org-1",
        "served_model": {"model": "local-llm-proxy/qwen3.6-35b-bench", "upstream_model": "qwen3.6-35b"},
        "verdict": verdict,
        "termination_reason": "green",
        "attempts_to_green": 1,
        "progress": progress,
        "work_input_tokens": 1000,
        "work_output_tokens": 2000,
        "work_total_tokens": 3000,
        "injected_block_est_tokens": 500,
        "injected_count": 1,
        "injected_block_chars": 4096,
        "consumer_injected_count": 1,
        "extraction_state": "invoked_completed",
        "extraction_candidate_count": 3,
        "terminal_outcome": True,
        "terminal_reason": "all_green",
        "session_fp": session_fp,
        "session_id": session_id,
    }


def test_write_run_manifest_writes_once_and_second_write_raises(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    first = _manifest()

    write_run_manifest(path, first)
    original = path.read_text(encoding="utf-8")

    second = replace(_manifest(), run_id="run-differs")

    with pytest.raises(FileExistsError):
        write_run_manifest(path, second)

    # First content unchanged.
    assert path.read_text(encoding="utf-8") == original
    assert "run-abc" in original
    assert "run-differs" not in path.read_text(encoding="utf-8")


def test_load_run_manifest_round_trips_all_fields(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    original = _manifest()
    write_run_manifest(path, original)

    loaded = load_run_manifest(path)

    assert loaded.to_dict() == original.to_dict()
    assert loaded.run_id == "run-abc"
    assert loaded.created_at == "2026-08-05T00:00:00Z"
    assert loaded.served_model == "qwen3.6-35b-bench"
    assert loaded.requested_model == "local-llm-proxy/qwen3.6-35b-bench"
    assert loaded.memory_mode == "on"
    assert loaded.org_id == "org-1"
    assert loaded.source_commit == "deadbeef1234"
    assert loaded.worker_image_fingerprint == {"sha256": "abcd1234"}
    assert loaded.seed == 42
    assert loaded.template_hash == "tpl-1"
    assert loaded.roster_fingerprint == "rost-1"


def test_status_stream_append_read_and_reopen_keeps_prior_lines(tmp_path) -> None:
    path = tmp_path / "manifest.status.jsonl"
    stream = StatusStream(path)

    rec1 = _status_record(sequence_index=0, session_fp="fp-0", session_id="s-0", progress=_progress(full_green=True, resolved=3, total_tokens=3000))
    rec2 = _status_record(sequence_index=1, session_fp="fp-1", session_id="s-1", progress=_progress(full_green=False, resolved=1, total_tokens=1000))

    stream.append(rec1)
    stream.append(rec2)

    all_records = stream.records()
    assert len(all_records) == 2
    assert all_records[0]["sequence_index"] == 0
    assert all_records[1]["sequence_index"] == 1

    # Reopen a fresh handle and append; prior lines must survive.
    reopened = StatusStream(path)
    rec3 = _status_record(sequence_index=2, session_fp="fp-2", session_id="s-2", progress=_progress(full_green=True, resolved=5, total_tokens=5000))
    reopened.append(rec3)

    all_records = reopened.records()
    assert len(all_records) == 3
    assert [r["sequence_index"] for r in all_records] == [0, 1, 2]


def test_status_stream_skips_unparseable_lines_keeps_valid_ones(tmp_path) -> None:
    path = tmp_path / "manifest.status.jsonl"
    stream = StatusStream(path)

    rec = _status_record(sequence_index=0, session_fp="fp-0", session_id="s-0", progress=_progress(full_green=True, resolved=3, total_tokens=3000))
    stream.append(rec)

    # Simulate a partial/corrupt trailing line left by a mid-write crash
    # (newline-terminated so the following append lands on its own line).
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"type": "attempt", "sequence_index": 1, "progress": {"full_green": t\n')  # invalid JSON

    stream.append(_status_record(sequence_index=2, session_fp="fp-2", session_id="s-2", progress=_progress(full_green=True, resolved=5, total_tokens=5000)))

    all_records = stream.records()
    # The garbage line is skipped; the valid records before and after remain.
    assert len(all_records) == 2
    assert [r["sequence_index"] for r in all_records] == [0, 2]


def test_default_paths_derive_sibling_names(tmp_path) -> None:
    manifest_path = str(tmp_path / "runs" / "cumulative" / "manifest.json")

    assert default_run_manifest_path(manifest_path) == str(
        tmp_path / "runs" / "cumulative" / "manifest.run-manifest.json"
    )
    assert default_status_stream_path(manifest_path) == str(
        tmp_path / "runs" / "cumulative" / "manifest.status.jsonl"
    )


def test_build_scorecard_reads_only_manifest_and_stream(tmp_path) -> None:
    run_dir = tmp_path / "runs" / "cumulative"
    mutable_manifest_path = run_dir / "manifest.json"  # deliberately absent
    run_manifest_path = run_dir / "manifest.run-manifest.json"

    # Two cells, two attempts each; terminal attempt carries non-None progress.
    stream = StatusStream(run_dir / "manifest.status.jsonl")
    stream.append(_status_record(sequence_index=0, session_fp="fp-0", session_id="s-0", progress=None, verdict="pending"))
    stream.append(_status_record(sequence_index=0, session_fp="fp-0", session_id="s-0", progress=_progress(full_green=True, resolved=3, total_tokens=3000)))
    stream.append(_status_record(sequence_index=1, session_fp="fp-1", session_id="s-1", progress=None, verdict="pending"))
    stream.append(_status_record(sequence_index=1, session_fp="fp-1", session_id="s-1", progress=_progress(full_green=False, resolved=1, total_tokens=1000)))

    manifest = _manifest()
    write_run_manifest(run_manifest_path, manifest)

    scorecard = build_scorecard(mutable_manifest_path)

    assert scorecard["schema_version"] == RUN_ARTIFACTS_SCHEMA_VERSION
    assert scorecard["manifest"]["run_id"] == "run-abc"
    assert scorecard["manifest"]["org_id"] == "org-1"
    assert scorecard["manifest"]["memory_mode"] == "on"
    assert scorecard["stream_records"] == 4
    assert scorecard["scored_sessions"] == 2

    convergence = scorecard["convergence"]
    assert convergence["sessions_completed"] == 2
    assert isinstance(convergence["trend_hash"], str)
    assert len(convergence["trend_hash"]) == 8
    int(convergence["trend_hash"], 16)  # stable 8-hex string

    # The mutable manifest path was never created / touched.
    assert not mutable_manifest_path.exists()