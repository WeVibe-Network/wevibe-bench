from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


ABSENT = "absent"


@dataclass
class PollEvidence:
    verdict: str
    db_age_s: int | None = None
    events_age_s: int | None = None
    budget_age_s: int | None = None
    outstanding_count: int | None = None
    proxylog_age_s: int | None = None
    turns: int | None = None
    tools: int | None = None
    cost_usd: float | None = None
    reasoning_tokens: int | None = None
    last_text: str = ""
    notes: list[str] = field(default_factory=list)


def _age_s_from_epoch_ms(epoch_ms: int | None, now_s: float) -> int | None:
    if epoch_ms is None:
        return None
    return max(0, int(now_s - (epoch_ms / 1000.0)))


def _age_s_from_mtime(path: Path, now_s: float) -> int | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return max(0, int(now_s - mtime))


def _sqlite_ro_connect(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path), safe="/:")
    return sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)


def _truncate_text(raw: str, max_len: int = 120) -> str:
    single_line = " ".join(raw.replace("\r", " ").replace("\n", " ").split())
    if len(single_line) <= max_len:
        return single_line
    return f"{single_line[: max_len - 3]}..."


def _parse_iso_age_s(iso: str | None, now_s: float) -> int | None:
    if not iso:
        return None
    value = iso.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int(now_s - dt.timestamp()))


def _read_db_evidence(db_path: Path, now_s: float) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "db_age_s": None,
        "turns": None,
        "tools": None,
        "cost_usd": None,
        "reasoning_tokens": None,
        "last_text": "",
        "notes": [],
    }

    if not db_path.exists():
        payload["notes"].append("db_absent")
        return payload

    db_age = _age_s_from_mtime(db_path, now_s)
    if db_age is not None:
        payload["db_age_s"] = db_age

    try:
        with _sqlite_ro_connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT MAX(time_updated) AS max_time_updated FROM part").fetchone()
            max_updated = row["max_time_updated"] if row is not None else None
            if max_updated is None:
                payload["notes"].append("db_no_parts_used_db_mtime")
            else:
                payload["db_age_s"] = _age_s_from_epoch_ms(int(max_updated), now_s)

            turns_row = conn.execute(
                "SELECT COUNT(*) AS c FROM part WHERE json_extract(data, '$.type')='step-finish'"
            ).fetchone()
            tools_row = conn.execute(
                "SELECT COUNT(*) AS c FROM part WHERE json_extract(data, '$.type')='tool'"
            ).fetchone()
            payload["turns"] = int(turns_row["c"]) if turns_row is not None else 0
            payload["tools"] = int(tools_row["c"]) if tools_row is not None else 0

            text_row = conn.execute(
                "SELECT data FROM part WHERE json_extract(data, '$.type')='text' "
                "ORDER BY time_created DESC LIMIT 1"
            ).fetchone()
            if text_row is not None and text_row["data"]:
                try:
                    text_payload = json.loads(str(text_row["data"]))
                    text_value = str(text_payload.get("text", ""))
                except (json.JSONDecodeError, TypeError, ValueError):
                    text_value = ""
                    payload["notes"].append("db_last_text_parse_error")
                payload["last_text"] = _truncate_text(text_value)

            session_row = conn.execute(
                "SELECT MAX(cost) AS cost_usd, MAX(tokens_reasoning) AS reasoning_tokens FROM session"
            ).fetchone()
            if session_row is not None:
                if session_row["cost_usd"] is not None:
                    payload["cost_usd"] = float(session_row["cost_usd"])
                if session_row["reasoning_tokens"] is not None:
                    payload["reasoning_tokens"] = int(session_row["reasoning_tokens"])
    except sqlite3.Error as exc:
        payload["notes"].append(f"db_read_error:{exc.__class__.__name__}")

    return payload


def _read_budget_evidence(budget_path: Path | None, now_s: float) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "budget_age_s": None,
        "outstanding_count": None,
        "notes": [],
    }
    if budget_path is None or not budget_path.exists():
        payload["notes"].append("budget_absent")
        return payload

    payload["budget_age_s"] = _age_s_from_mtime(budget_path, now_s)
    try:
        raw = json.loads(budget_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        payload["notes"].append(f"budget_unreadable_treated_absent:{exc.__class__.__name__}")
        payload["budget_age_s"] = None
        return payload

    updated_at_age = _parse_iso_age_s(raw.get("updated_at"), now_s)
    if updated_at_age is not None:
        payload["budget_age_s"] = updated_at_age

    outstanding = raw.get("outstanding")
    if isinstance(outstanding, dict):
        payload["outstanding_count"] = len(outstanding)
    else:
        payload["notes"].append("budget_outstanding_missing_or_invalid")
        payload["outstanding_count"] = None

    if "accrued_derived_usd" in raw:
        payload["notes"].append(f"accrued_derived_usd={raw.get('accrued_derived_usd')}")
    if "accrued_actual_usd" in raw:
        payload["notes"].append(f"accrued_actual_usd={raw.get('accrued_actual_usd')}")
    if "committed_unproven_usd" in raw:
        payload["notes"].append(f"committed_unproven_usd={raw.get('committed_unproven_usd')}")
    return payload


def assess_activity(
    run_dir: Path,
    proxy_budget: Path | None = None,
    proxy_log: Path | None = None,
    *,
    window_s: int = 900,
    now_s: float | None = None,
) -> PollEvidence:
    now = time.time() if now_s is None else now_s
    db_path = run_dir / "session-db" / "opencode.db"
    events_path = run_dir / "worktree.events.jsonl"

    db_data = _read_db_evidence(db_path, now)
    budget_data = _read_budget_evidence(proxy_budget, now)
    events_age_s = _age_s_from_mtime(events_path, now)
    proxylog_age_s = _age_s_from_mtime(proxy_log, now) if proxy_log else None

    notes = list(db_data["notes"]) + list(budget_data["notes"])
    if events_age_s is None:
        notes.append("events_absent")
    if proxy_log is None or proxylog_age_s is None:
        notes.append("proxylog_absent")

    outstanding_count = budget_data["outstanding_count"]
    outstanding_non_empty = outstanding_count is not None and outstanding_count > 0
    db_absent = not db_path.exists()

    observed_ages = [
        age
        for age in [db_data["db_age_s"], events_age_s, budget_data["budget_age_s"], proxylog_age_s]
        if age is not None
    ]
    fresh_observed = any(age <= window_s for age in observed_ages)

    if db_absent:
        verdict = "UNKNOWN"
    elif outstanding_non_empty or fresh_observed:
        verdict = "ALIVE"
    elif observed_ages and all(age > window_s for age in observed_ages):
        verdict = "DEAD"
    else:
        verdict = "UNKNOWN"
        notes.append("insufficient_evidence_no_kill")

    return PollEvidence(
        verdict=verdict,
        db_age_s=db_data["db_age_s"],
        events_age_s=events_age_s,
        budget_age_s=budget_data["budget_age_s"],
        outstanding_count=outstanding_count,
        proxylog_age_s=proxylog_age_s,
        turns=db_data["turns"],
        tools=db_data["tools"],
        cost_usd=db_data["cost_usd"],
        reasoning_tokens=db_data["reasoning_tokens"],
        last_text=db_data["last_text"],
        notes=notes,
    )


def format_evidence_line(evidence: PollEvidence) -> str:
    db_age = evidence.db_age_s if evidence.db_age_s is not None else ABSENT
    events_age = evidence.events_age_s if evidence.events_age_s is not None else ABSENT
    budget_age = evidence.budget_age_s if evidence.budget_age_s is not None else ABSENT
    outstanding = evidence.outstanding_count if evidence.outstanding_count is not None else ABSENT
    proxylog_age = evidence.proxylog_age_s if evidence.proxylog_age_s is not None else ABSENT
    turns = evidence.turns if evidence.turns is not None else ABSENT
    tools = evidence.tools if evidence.tools is not None else ABSENT
    cost_usd = f"{evidence.cost_usd:.6f}" if evidence.cost_usd is not None else ABSENT
    reasoning_tokens = evidence.reasoning_tokens if evidence.reasoning_tokens is not None else ABSENT
    last_text = _truncate_text(evidence.last_text)
    notes = ",".join(evidence.notes) if evidence.notes else "ok"

    return (
        f"VERDICT={evidence.verdict} db_age_s={db_age} events_age_s={events_age} "
        f"budget_age_s={budget_age} outstanding={outstanding} proxylog_age_s={proxylog_age} "
        f"turns={turns} tools={tools} cost_usd={cost_usd} reasoning_tokens={reasoning_tokens} "
        f"last_text=\"{last_text}\" note={notes}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Session activity poller verdict.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--proxy-budget", type=Path, default=None)
    parser.add_argument("--proxy-log", type=Path, default=None)
    parser.add_argument("--window-s", type=int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    evidence = assess_activity(
        run_dir=args.run_dir,
        proxy_budget=args.proxy_budget,
        proxy_log=args.proxy_log,
        window_s=int(args.window_s),
    )
    print(format_evidence_line(evidence), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
