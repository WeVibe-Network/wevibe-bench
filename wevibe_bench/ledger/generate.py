"""GSTV run-ledger generator (logs-only, honest-absence)."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator

from .model import (
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
from .parsers import OpRecord, extraction_integrity_records, parse_leader_signer_log, parse_ops_log, parse_serve_inject_lines, parse_spool_jsonl, read_json_file

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TRACE_RE = re.compile(r"\btrace=(?P<trace>[^\s]+)")
_CID_FP_RE = re.compile(r"\bcid_fp=(?P<cid_fp>[0-9a-fA-F]{8})\b")

_SUMMARY_FIELDS = [
    "trace",
    "run_id",
    "goals",
    "episodes_open",
    "episodes_closed",
    "coincidental",
    "receipts_predicate",
    "receipts_negative",
    "unattributed_vector_only",
    "signal_key_mode",
    "status",
]

_UTILIZATION_ABSENT_GAP = "utilization_proxy inputs absent (side inputs not provided)"


class _OpsWrap:
    def __init__(self, records: list[OpRecord]) -> None:
        self.records = records


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_int_or_none(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def _aggregate_signal_key_mode(modes: list[str]) -> str:
    observed = {mode for mode in modes if mode in {"parsed", "raw"}}
    if not observed:
        return "absent"
    return "mixed" if len(observed) == 2 else next(iter(observed))


def _coincidental_true(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_ops_records(ops_dir: str | None, gaps: set[str]) -> list[OpRecord]:
    if not ops_dir:
        gaps.add("ops input absent")
        return []
    root = Path(ops_dir)
    if not root.exists() or not root.is_dir():
        gaps.add("ops input absent")
        return []
    paths = sorted(root.glob("*.log"))
    if not paths:
        gaps.add("ops input absent")
        return []
    out: list[OpRecord] = []
    for path in paths:
        out.extend(parse_ops_log(path).records)
    return out


def _load_serve_lines(paths: list[str] | None, gaps: set[str]) -> list[str]:
    if not paths:
        gaps.add("serve/inject inputs absent")
        return []
    lines: list[str] = []
    seen = 0
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            continue
        seen += 1
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    if seen == 0:
        gaps.add("serve/inject inputs absent")
    return lines


def _load_leader_decisions(paths: list[str] | None, gaps: set[str]) -> dict[str, str]:
    if not paths:
        gaps.add("leader input absent")
        return {}
    out: dict[str, str] = {}
    seen = 0
    for raw in paths:
        path = Path(raw)
        if path.exists():
            seen += 1
        for row in parse_leader_signer_log(path).decisions:
            trace = row.get("trace")
            decision = row.get("decision")
            if isinstance(trace, str) and isinstance(decision, str):
                out[trace] = decision
    if seen == 0:
        gaps.add("leader input absent")
    return out


def _serve_trace_indices(lines: list[str]) -> tuple[dict[str, set[str]], set[str]]:
    by_trace: dict[str, set[str]] = defaultdict(set)
    inject_cids: set[str] = set()
    for line in lines:
        trace_match = _TRACE_RE.search(line)
        cid_match = _CID_FP_RE.search(line)
        if "[inject]" in line and cid_match:
            inject_cids.add(cid_match.group("cid_fp").lower())
        if trace_match and cid_match:
            by_trace[trace_match.group("trace")].add(cid_match.group("cid_fp").lower())
    return by_trace, inject_cids


def _catalog_ops(repo_root: Path) -> list[str]:
    data = json.loads((repo_root / "ledger" / "gstv-ops-catalog.json").read_text(encoding="utf-8"))
    ops = data.get("ops")
    if not isinstance(ops, list):
        return []
    out: list[str] = []
    for row in ops:
        if isinstance(row, dict) and isinstance(row.get("op"), str):
            out.append(row["op"])
    return out


def _extract_corr_by_trace(records: list[OpRecord]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for record in records:
        if record.op != "extraction.integrity":
            continue
        trace = record.fields.get("trace")
        if not trace:
            continue
        candidate_hash = record.fields.get("candidate_hash") or record.fields.get("candidate_fp") or record.fields.get("candidate")
        committed_cid = record.fields.get("committed_cid") or record.fields.get("cid_fp") or record.fields.get("memory_cid")
        corr: dict[str, str] = {}
        if candidate_hash:
            corr["candidate_hash"] = candidate_hash
        if committed_cid:
            corr["committed_cid"] = committed_cid.lower()
        if corr:
            out[trace] = corr
    return out


def _jaccard(left: str, right: str) -> float:
    left_tokens = set(_TOKEN_RE.findall(left.lower()))
    right_tokens = set(_TOKEN_RE.findall(right.lower()))
    if not left_tokens and not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    return 0.0 if not union else round(len(left_tokens & right_tokens) / len(union), 4)


def _summary_line(values: dict[str, str | int]) -> str:
    parts = [_now_iso(), "INFO", "op=gstv.run_summary"]
    for key in _SUMMARY_FIELDS:
        parts.append(f"{key}={values[key]}")
    return " ".join(parts)


def generate_run_ledger(
    run_id: str,
    *,
    ops_dir: str | None = None,
    spool_path: str | None = None,
    leader_log_paths: list[str] | None = None,
    serve_inject_log_paths: list[str] | None = None,
    scorecard_paths: list[str] | None = None,
    memory_implement_texts: dict[str, str] | None = None,
    attempt_diff_texts: dict[str, str] | None = None,
    out_dir: str | None = None,
) -> dict:
    gaps: set[str] = set()
    records = _load_ops_records(ops_dir, gaps)

    if spool_path is None:
        gaps.add("spool input absent")
    else:
        spool = parse_spool_jsonl(Path(spool_path))
        if not Path(spool_path).exists():
            gaps.add("spool input absent")
        if spool.truncated_final:
            gaps.add("spool input truncated final line")

    if scorecard_paths:
        for raw in scorecard_paths:
            read_json_file(Path(raw))

    leader_decisions = _load_leader_decisions(leader_log_paths, gaps)
    serve_lines = _load_serve_lines(serve_inject_log_paths, gaps)
    serve_parse = parse_serve_inject_lines(serve_lines)
    serve_by_trace, injected_cids = _serve_trace_indices(serve_lines)

    observed_ops = {row.op for row in records}
    catalog_ops = _catalog_ops(_repo_root())
    coverage = OpsCoverage(
        present=sorted(observed_ops),
        absent=sorted(set(catalog_ops) - observed_ops),
    )

    trace_to_goal: dict[str, str] = {}
    goal_rows: dict[str, dict[str, object]] = {}

    def goal_state(goal_id: str) -> dict[str, object]:
        row = goal_rows.get(goal_id)
        if row is None:
            row = {
                "seal_fp": None,
                "closed": False,
                "attempts_to_green": None,
                "sessions": 0,
                "links": 0,
                "gaps": 0,
                "red_boundaries": None,
                "predicate_fps": [],
                "negative_fps": [],
                "unlock_fp": None,
                "signal_modes": [],
            }
            goal_rows[goal_id] = row
        return row

    episode_open: list[OpRecord] = []
    episode_close: list[OpRecord] = []
    for record in records:
        fields = record.fields
        goal_id = fields.get("goal_id")
        trace = fields.get("trace")
        if goal_id:
            goal_state(goal_id)
            if trace:
                trace_to_goal.setdefault(trace, goal_id)

        if record.op == "episode.open":
            episode_open.append(record)
            mode = fields.get("signal_key_mode")
            mapped_goal = trace_to_goal.get(trace or "") if trace else None
            if mapped_goal and mode:
                goal_state(mapped_goal)["signal_modes"].append(mode)
            continue
        if record.op == "episode.close":
            episode_close.append(record)
            continue
        if not goal_id:
            continue

        state = goal_state(goal_id)
        if record.op == "gstv.seal":
            state["seal_fp"] = fields.get("seal_fp")
        elif record.op == "gstv.goal.close":
            state["closed"] = True
            state["attempts_to_green"] = _to_int_or_none(fields.get("attempts_to_green"))
            state["sessions"] = _to_int_or_none(fields.get("sessions")) or 0
            state["links"] = _to_int_or_none(fields.get("links")) or 0
            state["gaps"] = _to_int_or_none(fields.get("gaps")) or 0
            state["red_boundaries"] = _to_int_or_none(fields.get("red_boundaries"))
        elif record.op == "gstv.extraction.unlock":
            state["unlock_fp"] = fields.get("unlock_fp")
        elif record.op == "predicate.receipt" and fields.get("receipt_fp"):
            state["predicate_fps"].append(fields["receipt_fp"])
        elif record.op == "negative.receipt" and fields.get("receipt_fp"):
            state["negative_fps"].append(fields["receipt_fp"])

    goals = [
        GoalEntry(
            goal_id=goal_id,
            seal_fp=row["seal_fp"],
            closed=bool(row["closed"]),
            attempts_to_green=row["attempts_to_green"],
            sessions=int(row["sessions"]),
            links=int(row["links"]),
            gaps=int(row["gaps"]),
            red_boundaries=row["red_boundaries"],
            receipts=GoalReceipts(
                predicate_fps=sorted(set(row["predicate_fps"])),
                negative_fps=sorted(set(row["negative_fps"])),
            ),
            unlock_fp=row["unlock_fp"],
            signal_key_mode=_aggregate_signal_key_mode(row["signal_modes"]),
        )
        for goal_id, row in sorted(goal_rows.items())
    ]

    extraction_rows = extraction_integrity_records(_OpsWrap(records))
    if not extraction_rows:
        gaps.add("extraction.integrity input absent")
    extraction = Extraction(
        resolved=sum(int(r["resolved_problem_count"]) for r in extraction_rows if r["resolved_problem_count"] is not None),
        emitted=sum(int(r["emitted_memory_count"]) for r in extraction_rows if r["emitted_memory_count"] is not None),
        empty_reason=next((v for v in [r.get("empty_reason") for r in extraction_rows][::-1] if isinstance(v, str) and v), None),
        invariant_violation=any(r.get("invariant_violation") is True for r in extraction_rows),
    )

    corr_by_trace = _extract_corr_by_trace(records)
    problems: list[ProblemEntry] = []
    missing_problem_corr = False
    for record in episode_close:
        fields = record.fields
        trace = fields.get("trace")
        corr = corr_by_trace.get(trace or "") if trace else None
        candidate_hash = corr.get("candidate_hash") if corr else None
        committed_cid = corr.get("committed_cid") if corr else None
        if committed_cid is None and trace and trace in serve_by_trace and len(serve_by_trace[trace]) == 1:
            committed_cid = next(iter(serve_by_trace[trace]))
        if candidate_hash is None or committed_cid is None:
            missing_problem_corr = True
        problems.append(
            ProblemEntry(
                signal_key=fields.get("signal_key") or "missing/signal",
                episode_id=fields.get("episode_id"),
                attempt_diff_fp=None if fields.get("attempt_diff_fp") == "-" else fields.get("attempt_diff_fp"),
                candidate_hash=candidate_hash,
                leader_decision=leader_decisions.get(trace or "") if trace else None,
                committed_cid=committed_cid,
                injected_memory_overlap=(
                    None
                    if committed_cid is None or not injected_cids
                    else committed_cid in injected_cids
                ),
            )
        )
    if problems and missing_problem_corr:
        gaps.add("problem candidate/commit correlation absent for one or more episode.close traces")

    recalls = sum(1 for row in records if "recall" in row.op)
    if recalls == 0:
        gaps.add("recall ops absent")

    gate_events = len(extraction_rows)
    if gate_events == 0:
        gaps.add("cadence gate events absent (no extraction.integrity records)")

    cadence = Cadence(
        recalls=recalls,
        gate_events=gate_events,
        injections=serve_parse.injections,
        serves=serve_parse.serves,
        unattributed_vector_only=len(serve_parse.serve_receipt_failures),
        basis=(
            "recalls:op contains 'recall'; "
            "gate_events:op=extraction.integrity; "
            "serves:op=http.request url=/v1/serves; "
            "injections:[inject] count=...; "
            "unattributed_vector_only:[serve] receipt failed status=..."
        ),
    )

    utilization_pairs: list[UtilizationPair] = []
    if memory_implement_texts is None or attempt_diff_texts is None:
        gaps.add(_UTILIZATION_ABSENT_GAP)
    else:
        seen: set[tuple[str, str]] = set()
        for row in problems:
            if not row.committed_cid or not row.attempt_diff_fp:
                continue
            memory = memory_implement_texts.get(row.committed_cid)
            diff = attempt_diff_texts.get(row.attempt_diff_fp)
            if memory is None or diff is None:
                continue
            key = (row.committed_cid, row.attempt_diff_fp)
            if key in seen:
                continue
            seen.add(key)
            utilization_pairs.append(UtilizationPair(memory_fp=key[0], attempt_diff_fp=key[1], similarity=_jaccard(memory, diff)))
        utilization_pairs.sort(key=lambda p: (p.memory_fp, p.attempt_diff_fp))

    run_signal_mode = _aggregate_signal_key_mode([r.fields.get("signal_key_mode", "") for r in episode_open])
    ledger = RunLedger(
        run_id=run_id,
        generated_at=_now_iso(),
        signal_key_mode=run_signal_mode,
        goals=goals,
        problems=problems,
        cadence=cadence,
        extraction=extraction,
        utilization_proxy=UtilizationProxy(pairs=utilization_pairs),
        integrity=Integrity(gaps_disclosed=sorted(gaps), ops_coverage=coverage),
    ).to_dict()

    schema = json.loads((_repo_root() / "ledger" / "gstv-run-v1.schema.json").read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(ledger), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = ".".join(str(p) for p in first.absolute_path)
        raise ValueError(f"gstv-run-v1 validation failed at '{path}': {first.message}")

    summary = {
        "trace": f"ledger-{run_id}",
        "run_id": run_id,
        "goals": len(goals),
        "episodes_open": len(episode_open),
        "episodes_closed": len(episode_close),
        "coincidental": sum(1 for row in episode_close if _coincidental_true(row.fields.get("coincidental_flip"))),
        "receipts_predicate": sum(1 for row in records if row.op == "predicate.receipt"),
        "receipts_negative": sum(1 for row in records if row.op == "negative.receipt"),
        "unattributed_vector_only": len(serve_parse.serve_receipt_failures),
        "signal_key_mode": run_signal_mode,
        "status": "ok",
    }
    if out_dir:
        root = Path(out_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"gstv-run-{run_id}.json").write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (root / f"gstv-run-{run_id}.run_summary.log").write_text(_summary_line(summary) + "\n", encoding="utf-8")
    return ledger


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GSTV run ledger from logs")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ops-dir")
    parser.add_argument("--spool")
    parser.add_argument("--leader-log", action="append", dest="leader_logs")
    parser.add_argument("--serve-inject-log", action="append", dest="serve_inject_logs")
    parser.add_argument("--scorecard", action="append", dest="scorecards")
    parser.add_argument("--out-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    generate_run_ledger(
        args.run_id,
        ops_dir=args.ops_dir,
        spool_path=args.spool,
        leader_log_paths=args.leader_logs,
        serve_inject_log_paths=args.serve_inject_logs,
        scorecard_paths=args.scorecards,
        out_dir=args.out_dir,
    )
    out = Path(args.out_dir)
    print(str(out / f"gstv-run-{args.run_id}.json"))
    print(str(out / f"gstv-run-{args.run_id}.run_summary.log"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
