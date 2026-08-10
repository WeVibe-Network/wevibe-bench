"""Bench-side mirror of the vendor ``bench-fixture-adapter`` report parser.

The WeVibe opencode plugin's ``bench-fixture-adapter.ts`` is the authoritative
parser of a bench-fixture command's machine-readable report: it walks
``ctx.output`` and extracts failing test ids. This module mirrors that parse
semantics in pure Python so the bench test suite can verify reporter output →
failing ids WITHOUT touching the vendored TypeScript.

Reporter format v1 (mirrored exactly):
  - the first non-empty line, whitespace-trimmed, must equal the magic header
    ``WEVIBE-BENCH-REPORT v1``;
  - each subsequent non-empty line is one JSONL record:
        {"test":"<stable-id>","status":"fail"|"pass"}
  - blank lines, malformed JSON, records with a missing/empty ``test``, and
    records whose ``status`` is neither ``"fail"`` nor ``"pass"`` are ignored
    (robust to noise).

Failing ids are returned in file order, deduplicated first-wins, mirroring
``extractIds(ctx, "fail")``. If the header is absent (or no header can be
established as the first non-empty line) the report is not considered a valid
bench report and ``[]`` is returned.
"""

from __future__ import annotations

import json

BENCH_REPORT_HEADER = "WEVIBE-BENCH-REPORT v1"


def parse_failing_ids(report: str) -> list[str]:
    """Return failing test ids in file order, deduped first-wins.

    Mirrors the vendor adapter: requires ``BENCH_REPORT_HEADER`` as the first
    non-empty line; ignores blank/malformed/non-bench records; returns ``[]``
    when the header is absent.
    """
    if not has_report_header(report):
        return []

    seen: set[str] = set()
    ids: list[str] = []
    for raw_line in report.split("\n"):
        record = _parse_record(raw_line)
        if record is None or record["status"] != "fail":
            continue
        test_id = record["test"]
        if test_id in seen:
            continue
        seen.add(test_id)
        ids.append(test_id)
    return ids


def has_report_header(output: str) -> bool:
    """True when the first non-empty output line equals the magic header."""
    for raw_line in output.split("\n"):
        if raw_line.strip() == "":
            continue
        return raw_line.strip() == BENCH_REPORT_HEADER
    return False


def _parse_record(line: str) -> dict[str, str] | None:
    """Parse one JSONL line into a ``{test, status}`` record, else None.

    Mirrors ``parseRecord`` in the vendor adapter: the header, blank lines,
    malformed JSON, missing/empty ``test``, and ``status`` values other than
    ``"fail"``/``"pass"`` all yield None.
    """
    trimmed = line.strip()
    if trimmed == "":
        return None
    if trimmed == BENCH_REPORT_HEADER:
        return None
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    test = parsed.get("test")
    if not isinstance(test, str) or test == "":
        return None
    status = parsed.get("status")
    if status not in ("fail", "pass"):
        return None
    return {"test": test, "status": status}