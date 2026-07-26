from __future__ import annotations

from pathlib import Path

from wevibe_bench.adapters.backgammon import RecallFunnelScan, _scan_recall_funnel


def _write_plugin_log(worktree: Path, contents: str) -> None:
    log_path = worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(contents, encoding="utf-8")


def test_scan_recall_funnel_counts_mixed_fire_triggers(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "recall_fired trigger=user_message sid=s1\n"
        "recall_fired trigger=tool_failure sid=s1\n"
        "recall_fired trigger=tool_failure sid=s2\n",
    )

    scan = _scan_recall_funnel(worktree)

    assert scan is not None
    assert scan.recall_fired_total == 3
    assert scan.recall_fired_user_message == 1
    assert scan.recall_fired_tool_failure == 2


def test_scan_recall_funnel_counts_returned_and_no_keywords(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "recall_returned status=200 count=3 reason_code=none dur_ms=45 error=none\n"
        "recall_returned status=cache count=2 reason_code=cache_hit dur_ms=3 error=none\n"
        "recall_returned status=200 count=0 reason_code=no_keywords dur_ms=12 error=none\n",
    )

    scan = _scan_recall_funnel(worktree)

    assert scan is not None
    assert scan.recall_returned_total == 3
    assert scan.recall_returned_count_sum == 5
    assert scan.no_keywords_count == 1


def test_scan_recall_funnel_counts_serve_attempts_and_both_failure_shapes(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[serve] upsert cid=cid-a sid=s1\n"
        "[serve] upsert cid=cid-b sid=s1\n"
        "[serve] receipt failed status=503 reason=timeout cid_fp=deadbeef\n"
        "[serve] receipt failed reason=network_disconnect cid_fp=cafebabe\n",
    )

    scan = _scan_recall_funnel(worktree)

    assert scan is not None
    assert scan.served_attempted == 2
    assert scan.served_failed == 2
    assert scan.served_confirmed == 0


def test_scan_recall_funnel_counts_clean_serve_confirmation(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[serve] upsert cid=cid-a sid=s1\n"
        "[serve] upsert cid=cid-b sid=s1\n",
    )

    scan = _scan_recall_funnel(worktree)

    assert scan is not None
    assert scan.served_attempted == 2
    assert scan.served_failed == 0
    assert scan.served_confirmed == 2


def test_scan_recall_funnel_counts_injected_only_not_restored(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[inject] injected count=2 block_chars=1200 block_tokens=300 top_k=5 sid=s1 newly_served=2 injected_once=2 budget_remaining=0 cadence=once\n"
        "[inject] restored count=5 block_chars=3000 sid=s1 cadence=once compaction_restores=1\n",
    )

    scan = _scan_recall_funnel(worktree)

    assert scan is not None
    assert scan.injected_count == 2


def test_scan_recall_funnel_parses_prefixed_physical_lines_same_as_bare(tmp_path: Path) -> None:
    bare_worktree = tmp_path / "bare"
    bare_worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        bare_worktree,
        "recall_fired trigger=user_message sid=s1\n"
        "recall_returned status=200 count=1 reason_code=none dur_ms=9 error=none\n"
        "[inject] injected count=1 block_chars=600 block_tokens=150 top_k=3 sid=s1 newly_served=1 injected_once=1 budget_remaining=0 cadence=once\n"
        "[serve] upsert cid=cid-1 sid=s1\n",
    )

    prefixed_worktree = tmp_path / "prefixed"
    prefixed_worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        prefixed_worktree,
        "2026-07-26T12:00:00Z [INFO] trace=1a2b3c4d recall_fired trigger=user_message sid=s1\n"
        "2026-07-26T12:00:01Z [INFO] trace=1a2b3c4d recall_returned status=200 count=1 reason_code=none dur_ms=9 error=none\n"
        "2026-07-26T12:00:02Z [INFO] trace=1a2b3c4d [inject] injected count=1 block_chars=600 block_tokens=150 top_k=3 sid=s1 newly_served=1 injected_once=1 budget_remaining=0 cadence=once\n"
        "2026-07-26T12:00:03Z [INFO] trace=1a2b3c4d [serve] upsert cid=cid-1 sid=s1\n",
    )

    expected = RecallFunnelScan(
        recall_fired_total=1,
        recall_fired_user_message=1,
        recall_fired_tool_failure=0,
        recall_returned_total=1,
        recall_returned_count_sum=1,
        no_keywords_count=0,
        injected_count=1,
        served_attempted=1,
        served_failed=0,
        served_confirmed=1,
    )

    assert _scan_recall_funnel(bare_worktree) == expected
    assert _scan_recall_funnel(prefixed_worktree) == expected


def test_scan_recall_funnel_returns_none_when_log_is_missing(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    assert _scan_recall_funnel(worktree) is None


def test_scan_recall_funnel_returns_zero_scan_when_log_has_no_funnel_lines(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "[plugin] startup complete\n"
        "[plugin] heartbeat ok\n",
    )

    assert _scan_recall_funnel(worktree) == RecallFunnelScan()


def test_scan_recall_funnel_has_one_to_one_fire_return_totals(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "2026-07-26T12:05:00Z [INFO] trace=feedbabe recall_fired trigger=user_message sid=s1\n"
        "2026-07-26T12:05:01Z [INFO] trace=feedbabe recall_returned status=200 count=2 reason_code=none dur_ms=10 error=none\n"
        "2026-07-26T12:05:02Z [INFO] trace=feedbabe recall_fired trigger=tool_failure sid=s1\n"
        "2026-07-26T12:05:03Z [INFO] trace=feedbabe recall_returned status=cache count=1 reason_code=cache_hit dur_ms=2 error=none\n",
    )

    scan = _scan_recall_funnel(worktree)

    assert scan is not None
    assert scan.recall_fired_total == scan.recall_returned_total
    assert scan.recall_fired_total == 2


def test_scan_recall_funnel_realistic_combined_log_sample_output(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_plugin_log(
        worktree,
        "2026-07-26T12:10:00Z [INFO] trace=aa11bb22 recall_fired trigger=user_message sid=s1\n"
        "2026-07-26T12:10:01Z [INFO] trace=aa11bb22 recall_returned status=200 count=2 reason_code=none dur_ms=14 error=none\n"
        "2026-07-26T12:10:02Z [INFO] trace=cc33dd44 recall_fired trigger=tool_failure sid=s1\n"
        "2026-07-26T12:10:03Z [INFO] trace=cc33dd44 recall_returned status=200 count=0 reason_code=no_keywords dur_ms=7 error=none\n"
        "2026-07-26T12:10:04Z [INFO] trace=cc33dd44 [inject] injected count=3 block_chars=1800 block_tokens=450 top_k=5 sid=s1 newly_served=3 injected_once=3 budget_remaining=120 cadence=once\n"
        "2026-07-26T12:10:05Z [INFO] trace=cc33dd44 [inject] restored count=9 block_chars=5400 sid=s1 cadence=once compaction_restores=1\n"
        "2026-07-26T12:10:06Z [INFO] trace=ee55ff66 [serve] upsert cid=cid-101 sid=s1\n"
        "2026-07-26T12:10:07Z [INFO] trace=ee55ff66 [serve] upsert cid=cid-102 sid=s1\n"
        "2026-07-26T12:10:08Z [WARN] trace=ee55ff66 [serve] receipt failed reason=timeout cid_fp=1234abcd\n",
    )

    parsed = _scan_recall_funnel(worktree)
    print(parsed)

    expected = RecallFunnelScan(
        recall_fired_total=2,
        recall_fired_user_message=1,
        recall_fired_tool_failure=1,
        recall_returned_total=2,
        recall_returned_count_sum=2,
        no_keywords_count=1,
        injected_count=3,
        served_attempted=2,
        served_failed=1,
        served_confirmed=1,
    )
    assert parsed == expected
