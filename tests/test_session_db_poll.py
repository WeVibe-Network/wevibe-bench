from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import session_db_poll as poll


def _touch_with_age(path: Path, *, now_s: float, age_s: int, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    ts = now_s - age_s
    path.touch()
    path.chmod(0o644)
    # keep deterministic freshness
    import os

    os.utime(path, (ts, ts))


def _build_db(
    db_path: Path,
    *,
    part_time_updated_ms: int | None,
    now_s: float,
    db_age_s: int,
    turns: int = 0,
    tools: int = 0,
    last_text: str = "",
    cost: float = 0.0,
    reasoning_tokens: int = 0,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE part (data TEXT NOT NULL, time_updated INTEGER, time_created INTEGER)"
        )
        conn.execute(
            "CREATE TABLE session (cost REAL, tokens_reasoning INTEGER, model TEXT)"
        )
        for i in range(turns):
            conn.execute(
                "INSERT INTO part (data, time_updated, time_created) VALUES (?, ?, ?)",
                (json.dumps({"type": "step-finish"}), part_time_updated_ms, part_time_updated_ms or 0 + i),
            )
        for i in range(tools):
            conn.execute(
                "INSERT INTO part (data, time_updated, time_created) VALUES (?, ?, ?)",
                (json.dumps({"type": "tool"}), part_time_updated_ms, part_time_updated_ms or 0 + turns + i),
            )
        if last_text:
            conn.execute(
                "INSERT INTO part (data, time_updated, time_created) VALUES (?, ?, ?)",
                (
                    json.dumps({"type": "text", "text": last_text}),
                    part_time_updated_ms,
                    (part_time_updated_ms or 0) + turns + tools + 1,
                ),
            )
        conn.execute(
            "INSERT INTO session (cost, tokens_reasoning, model) VALUES (?, ?, ?)",
            (cost, reasoning_tokens, "openai/gpt-5.2"),
        )
        conn.commit()
    finally:
        conn.close()

    import os

    ts = now_s - db_age_s
    os.utime(db_path, (ts, ts))


def _write_budget(
    path: Path,
    *,
    updated_at_epoch_s: float,
    outstanding: dict[str, object],
    accrued_derived_usd: float = 0.0,
    accrued_actual_usd: float = 0.0,
    committed_unproven_usd: float = 0.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "outstanding": outstanding,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated_at_epoch_s)),
        "accrued_derived_usd": accrued_derived_usd,
        "accrued_actual_usd": accrued_actual_usd,
        "committed_unproven_usd": committed_unproven_usd,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_smoke3_stale_logs_with_outstanding_is_alive(tmp_path: Path) -> None:
    now = 1_722_000_000.0
    run_dir = tmp_path / "run"
    db = run_dir / "session-db" / "opencode.db"
    events = run_dir / "worktree.events.jsonl"
    budget = tmp_path / "proxy" / "run-budget.json"
    proxy_log = tmp_path / "proxy" / "run.log"

    _build_db(
        db,
        part_time_updated_ms=int((now - 1200) * 1000),
        now_s=now,
        db_age_s=1200,
        turns=2,
        tools=0,
        last_text="all quiet but still reasoning",
        cost=1.25,
        reasoning_tokens=29108,
    )
    _touch_with_age(events, now_s=now, age_s=1300, content="{}\n")
    _write_budget(
        budget,
        updated_at_epoch_s=now - 1300,
        outstanding={"trace-1": {"ub_usd": 1.0}},
        accrued_derived_usd=1.2,
        accrued_actual_usd=1.1,
        committed_unproven_usd=0.1,
    )
    _touch_with_age(proxy_log, now_s=now, age_s=1300, content="proxy\n")

    evidence = poll.assess_activity(run_dir, proxy_budget=budget, proxy_log=proxy_log, window_s=900, now_s=now)
    line = poll.format_evidence_line(evidence)
    assert evidence.verdict == "ALIVE"
    assert "VERDICT=ALIVE" in line
    assert "outstanding=1" in line


def test_true_dead_all_stale_and_outstanding_empty(tmp_path: Path) -> None:
    now = 1_722_000_000.0
    run_dir = tmp_path / "run"
    db = run_dir / "session-db" / "opencode.db"
    events = run_dir / "worktree.events.jsonl"
    budget = tmp_path / "proxy" / "run-budget.json"
    proxy_log = tmp_path / "proxy" / "run.log"

    _build_db(db, part_time_updated_ms=int((now - 1200) * 1000), now_s=now, db_age_s=1200, turns=1, tools=1)
    _touch_with_age(events, now_s=now, age_s=1200, content="{}\n")
    _write_budget(budget, updated_at_epoch_s=now - 1200, outstanding={})
    _touch_with_age(proxy_log, now_s=now, age_s=1200, content="proxy\n")

    evidence = poll.assess_activity(run_dir, proxy_budget=budget, proxy_log=proxy_log, window_s=900, now_s=now)
    line = poll.format_evidence_line(evidence)
    assert evidence.verdict == "DEAD"
    assert "VERDICT=DEAD" in line


def test_fresh_session_db_activity_is_alive(tmp_path: Path) -> None:
    now = 1_722_000_000.0
    run_dir = tmp_path / "run"
    db = run_dir / "session-db" / "opencode.db"
    events = run_dir / "worktree.events.jsonl"

    _build_db(db, part_time_updated_ms=int((now - 5) * 1000), now_s=now, db_age_s=1000, turns=3, tools=2)
    _touch_with_age(events, now_s=now, age_s=1000, content="{}\n")

    evidence = poll.assess_activity(run_dir, proxy_budget=None, proxy_log=None, window_s=900, now_s=now)
    assert evidence.verdict == "ALIVE"


def test_missing_session_db_is_unknown(tmp_path: Path) -> None:
    now = 1_722_000_000.0
    run_dir = tmp_path / "run"
    events = run_dir / "worktree.events.jsonl"
    _touch_with_age(events, now_s=now, age_s=10, content="{}\n")

    evidence = poll.assess_activity(run_dir, proxy_budget=None, proxy_log=None, window_s=900, now_s=now)
    line = poll.format_evidence_line(evidence)
    assert evidence.verdict == "UNKNOWN"
    assert "VERDICT=UNKNOWN" in line


def test_budget_absent_and_stale_everything_else_is_dead(tmp_path: Path) -> None:
    now = 1_722_000_000.0
    run_dir = tmp_path / "run"
    db = run_dir / "session-db" / "opencode.db"
    events = run_dir / "worktree.events.jsonl"

    _build_db(db, part_time_updated_ms=int((now - 1200) * 1000), now_s=now, db_age_s=1200)
    _touch_with_age(events, now_s=now, age_s=1200, content="{}\n")

    evidence = poll.assess_activity(run_dir, proxy_budget=None, proxy_log=None, window_s=900, now_s=now)
    assert evidence.verdict == "DEAD"


def test_fresh_budget_updated_at_is_alive(tmp_path: Path) -> None:
    now = 1_722_000_000.0
    run_dir = tmp_path / "run"
    db = run_dir / "session-db" / "opencode.db"
    events = run_dir / "worktree.events.jsonl"
    budget = tmp_path / "proxy" / "run-budget.json"

    _build_db(db, part_time_updated_ms=int((now - 1200) * 1000), now_s=now, db_age_s=1200)
    _touch_with_age(events, now_s=now, age_s=1200, content="{}\n")
    _write_budget(budget, updated_at_epoch_s=now - 5, outstanding={})

    evidence = poll.assess_activity(run_dir, proxy_budget=budget, proxy_log=None, window_s=900, now_s=now)
    assert evidence.verdict == "ALIVE"


def test_corrupt_budget_treated_absent_not_crash(tmp_path: Path) -> None:
    now = 1_722_000_000.0
    run_dir = tmp_path / "run"
    db = run_dir / "session-db" / "opencode.db"
    events = run_dir / "worktree.events.jsonl"
    budget = tmp_path / "proxy" / "run-budget.json"

    _build_db(db, part_time_updated_ms=int((now - 1200) * 1000), now_s=now, db_age_s=1200)
    _touch_with_age(events, now_s=now, age_s=1200, content="{}\n")
    budget.parent.mkdir(parents=True, exist_ok=True)
    budget.write_text("{not-json", encoding="utf-8")

    evidence = poll.assess_activity(run_dir, proxy_budget=budget, proxy_log=None, window_s=900, now_s=now)
    line = poll.format_evidence_line(evidence)
    assert evidence.verdict == "DEAD"
    assert "budget_unreadable_treated_absent" in line
