from __future__ import annotations

from pathlib import Path

from wevibe_bench.adapters.backgammon import _scan_funnel_snapshot


def _write_snapshot(worktree: Path, contents: str) -> None:
    snapshot_path = worktree / ".wevibe" / "state" / "funnel-snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(contents, encoding="utf-8")


def test_scan_funnel_snapshot_reads_per_session_counters(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_snapshot(
        worktree,
        (
            '{"s1": {"episode_opened": 1, "episode_armed": 1, "recall_fired": 2, '
            '"gate_shown": 1, "gate_decided": 1, "serve_sent": 1, '
            '"confirmed_on_chain": 1, "gate_decision_ms": 12}, '
            '"s2": {"episode_opened": 1, "episode_armed": 0, "recall_fired": 0, '
            '"gate_shown": 0, "gate_decided": 0, "serve_sent": 0, '
            '"confirmed_on_chain": 0, "gate_decision_ms": null}}'
        ),
    )

    snapshot = _scan_funnel_snapshot(worktree)

    assert snapshot is not None
    assert set(snapshot.keys()) == {"s1", "s2"}
    assert snapshot["s1"]["recall_fired"] == 2
    assert snapshot["s1"]["gate_decision_ms"] == 12
    assert snapshot["s2"]["recall_fired"] == 0
    assert snapshot["s2"]["gate_decision_ms"] is None


def test_scan_funnel_snapshot_returns_none_when_file_is_absent(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    assert _scan_funnel_snapshot(worktree) is None


def test_scan_funnel_snapshot_returns_empty_when_file_has_no_sessions(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_snapshot(worktree, "{}")

    assert _scan_funnel_snapshot(worktree) == {}


def test_scan_funnel_snapshot_tolerates_corrupt_json(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_snapshot(worktree, "{not-valid-json")

    assert _scan_funnel_snapshot(worktree) is None


def test_scan_funnel_snapshot_tolerates_non_object_root(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_snapshot(worktree, "[1, 2, 3]")

    assert _scan_funnel_snapshot(worktree) is None


def test_scan_funnel_snapshot_skips_non_dict_counters(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _write_snapshot(worktree, '{"s1": {"episode_opened": 1}, "s2": "garbage"}')

    snapshot = _scan_funnel_snapshot(worktree)

    assert snapshot is not None
    assert set(snapshot.keys()) == {"s1"}