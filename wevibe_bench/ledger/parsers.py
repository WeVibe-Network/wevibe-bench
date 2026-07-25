"""Defensive parsers for GSTV run-ledger source artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Iterable

_OPS_LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+(?P<level>INFO|WARN|ERROR)\s+op=(?P<op>[^\s]+)(?P<rest>.*)$"
)
_KV_RE = re.compile(r"(?P<key>[A-Za-z0-9_.-]+)=(?P<value>[^\s]+)")
_LEADER_RE = re.compile(
    r"^(?P<ts>\S+)\s+\[(?P<level>[A-Z]+)\]\s+trace=(?P<trace>[^\s]+)\s+(?P<rest>.*)$"
)
_JSON_OBJECT_AT_END_RE = re.compile(r"(\{.*\})\s*$")
_DECISION_WORD_RE = re.compile(r"\b(approve|deny|commit|verify)\b", flags=re.IGNORECASE)
_SERVE_RECEIPT_FAILURE_RE = re.compile(
    r"\[serve\]\s+receipt\s+failed\s+status=(?P<status>\d+)\s+reason=[^\s]+\s+cid_fp=(?P<cid_fp>[0-9a-fA-F]{8})"
)
_HTTP_SERVE_RE = re.compile(r"\bop=http\.request\b.*\burl=/v1/serves\b.*\bstatus=(?P<status>\d+)\b")
_INJECT_COUNT_RE = re.compile(r"\[inject\].*\bcount=(?P<count>\d+)\b")
_INJECT_BLOCK_CHARS_RE = re.compile(r"\[inject\].*\bblock_chars=(?P<block_chars>\d+)\b")


@dataclass(frozen=True)
class OpRecord:
    ts: str
    level: str
    op: str
    fields: dict[str, str]


@dataclass(frozen=True)
class OpsLogParse:
    records: list[OpRecord] = field(default_factory=list)
    skipped_lines: int = 0


@dataclass(frozen=True)
class SpoolParse:
    envelopes: list[dict] = field(default_factory=list)
    truncated_final: bool = False


@dataclass(frozen=True)
class LeaderParse:
    decisions: list[dict] = field(default_factory=list)
    skipped_lines: int = 0


@dataclass(frozen=True)
class ServeInjectParse:
    serves: int = 0
    serve_receipt_failures: list[dict[str, str | int]] = field(default_factory=list)
    injections: int = 0
    block_chars_total: int = 0


def _parse_bool(text: str) -> bool:
    return text.lower() == "true"


def parse_ops_log(path: Path) -> OpsLogParse:
    if not path.exists():
        return OpsLogParse(records=[], skipped_lines=0)

    records: list[OpRecord] = []
    skipped = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _OPS_LINE_RE.match(line)
        if not match:
            skipped += 1
            continue
        fields = {m.group("key"): m.group("value") for m in _KV_RE.finditer(match.group("rest"))}
        records.append(
            OpRecord(
                ts=match.group("ts"),
                level=match.group("level"),
                op=match.group("op"),
                fields=fields,
            )
        )
    return OpsLogParse(records=records, skipped_lines=skipped)


def parse_spool_jsonl(path: Path) -> SpoolParse:
    if not path.exists():
        return SpoolParse(envelopes=[], truncated_final=False)

    content = path.read_text(encoding="utf-8")
    if content == "":
        return SpoolParse(envelopes=[], truncated_final=False)

    envelopes: list[dict] = []
    truncated_final = False
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        raw = line.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            if idx == len(lines) - 1:
                truncated_final = True
            continue
        if isinstance(parsed, dict):
            envelopes.append(parsed)

    if not content.endswith(("\n", "\r")) and lines:
        final_line = lines[-1].strip()
        if final_line:
            try:
                json.loads(final_line)
            except json.JSONDecodeError:
                truncated_final = True

    return SpoolParse(envelopes=envelopes, truncated_final=truncated_final)


def extraction_integrity_records(ops_log_parse: OpsLogParse) -> list[dict]:
    out: list[dict] = []
    for record in ops_log_parse.records:
        if record.op != "extraction.integrity":
            continue
        fields = dict(record.fields)
        converted: dict[str, object] = {
            "ts": record.ts,
            "level": record.level,
            "op": record.op,
            "phase": fields.get("phase"),
            "job_id": fields.get("job_id"),
            "session_fp": fields.get("session_fp"),
            "outcome": fields.get("outcome"),
            "empty_reason": fields.get("empty_reason"),
            "episode_metadata": fields.get("episode_metadata"),
        }
        for int_key in (
            "resolved_problem_count",
            "unresolved_problem_count",
            "coincidental_count",
            "emitted_memory_count",
        ):
            value = fields.get(int_key)
            converted[int_key] = int(value) if value is not None and value.isdigit() else None
        invariant = fields.get("invariant_violation")
        converted["invariant_violation"] = _parse_bool(invariant) if invariant is not None else None
        out.append(converted)
    return out


def parse_leader_signer_log(path: Path) -> LeaderParse:
    if not path.exists():
        return LeaderParse(decisions=[], skipped_lines=0)

    decisions: list[dict] = []
    skipped = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _LEADER_RE.match(line)
        if not match:
            skipped += 1
            continue

        rest = match.group("rest")
        meta: dict = {}
        meta_match = _JSON_OBJECT_AT_END_RE.search(rest)
        if meta_match:
            json_blob = meta_match.group(1)
            try:
                loaded = json.loads(json_blob)
                if isinstance(loaded, dict):
                    meta = loaded
                rest = rest[: meta_match.start()].strip()
            except json.JSONDecodeError:
                pass

        decision_word = _DECISION_WORD_RE.search(rest)
        if not decision_word:
            continue
        decisions.append(
            {
                "ts": match.group("ts"),
                "level": match.group("level"),
                "trace": match.group("trace"),
                "decision": decision_word.group(1).lower(),
                "message": rest,
                "meta": meta,
            }
        )
    return LeaderParse(decisions=decisions, skipped_lines=skipped)


def parse_serve_inject_lines(lines: Iterable[str]) -> ServeInjectParse:
    serves = 0
    failures: list[dict[str, str | int]] = []
    injections = 0
    block_chars_total = 0

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        failure_match = _SERVE_RECEIPT_FAILURE_RE.search(line)
        if failure_match:
            failures.append(
                {
                    "status": int(failure_match.group("status")),
                    "cid_fp": failure_match.group("cid_fp").lower(),
                }
            )

        if _HTTP_SERVE_RE.search(line):
            serves += 1

        inject_count_match = _INJECT_COUNT_RE.search(line)
        if inject_count_match:
            injections += int(inject_count_match.group("count"))

        block_chars_match = _INJECT_BLOCK_CHARS_RE.search(line)
        if block_chars_match:
            block_chars_total += int(block_chars_match.group("block_chars"))

    return ServeInjectParse(
        serves=serves,
        serve_receipt_failures=failures,
        injections=injections,
        block_chars_total=block_chars_total,
    )


def read_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded
