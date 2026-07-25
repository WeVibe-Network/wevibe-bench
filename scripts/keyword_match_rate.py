"""Compute per-run keyword match-rate from existing bench artifacts.

Inputs are existing offline artifact files (telemetry JSON, recall-smoke log, or
plugin log) from a bench run directory. Output is a JSON report with per-memory
rows plus aggregate match-rate metrics:
  - served_n, matched_n, match_rate, vector_only_serve_count
  - matched_keyword_instances
  - unmatched_query_keywords (when query keywords + matched keyword data exist)

This script is stdlib-only, offline, and never calls Qdrant or any live service.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys


_INJECT_CID_RE = re.compile(r"([0-9a-fA-F]+)\(score=([0-9]+(?:\.[0-9]+)?)")


@dataclass
class ServedMemory:
    cid: str | None
    matched_keywords: list[str]
    keyword_score: float | None = None
    vector_score: float | None = None
    combined_score: float | None = None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_telemetry_list(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("telemetry payload must be a JSON object or list")

    for key in ("precision_dilution", "memories", "injected"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return candidate

    for value in payload.values():
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict) and (
                "matched_keywords" in first
                or "keyword_score" in first
                or "vector_score" in first
                or "combined_score" in first
                or "cid" in first
            ):
                return value

    raise ValueError("telemetry JSON does not contain a parseable memory list")


def parse_telemetry_json(path: Path) -> list[ServedMemory]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = _extract_telemetry_list(payload)

    out: list[ServedMemory] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_matched = item.get("matched_keywords")
        if isinstance(raw_matched, list):
            matched = [str(token) for token in raw_matched if isinstance(token, str)]
        else:
            matched = []
        out.append(
            ServedMemory(
                cid=str(item.get("cid")) if item.get("cid") is not None else None,
                matched_keywords=matched,
                keyword_score=_to_float(item.get("keyword_score")),
                vector_score=_to_float(item.get("vector_score")),
                combined_score=_to_float(item.get("combined_score")),
            )
        )
    return out


def _flatten_keyword_channel(value: object) -> list[str]:
    out: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            out.append(node)
            return
        if isinstance(node, dict):
            for child in node.values():
                _walk(child)
            return
        if isinstance(node, (list, tuple, set)):
            for child in node:
                _walk(child)

    _walk(value)
    return out


def parse_recall_smoke_log(path: Path) -> tuple[list[ServedMemory], list[str]]:
    memories: list[ServedMemory] = []
    query_keywords: list[str] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if "memory[" in line and " cid=" in line and " keyword=" in line:
            cid_match = re.search(r"\bcid=([^\s]+)", line)
            keyword_match = re.search(r"\bkeyword=([^\s]+)", line)
            vector_match = re.search(r"\bvector=([^\s]+)", line)
            combined_match = re.search(r"\bcombined=([^\s]+)", line)
            memories.append(
                ServedMemory(
                    cid=cid_match.group(1) if cid_match else None,
                    matched_keywords=[],
                    keyword_score=_to_float(keyword_match.group(1)) if keyword_match else None,
                    vector_score=_to_float(vector_match.group(1)) if vector_match else None,
                    combined_score=_to_float(combined_match.group(1)) if combined_match else None,
                )
            )
            continue

        if "keyword_channel=" in line:
            _, _, raw_channel = line.partition("keyword_channel=")
            text = raw_channel.strip()
            if not text:
                continue
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                continue
            query_keywords.extend(_flatten_keyword_channel(parsed))

    return memories, query_keywords


def parse_plugin_log(path: Path) -> list[ServedMemory]:
    memories: list[ServedMemory] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if "[inject]" not in line:
            continue
        for cid, score in _INJECT_CID_RE.findall(line):
            memories.append(
                ServedMemory(
                    cid=cid,
                    matched_keywords=[],
                    combined_score=_to_float(score),
                )
            )
    return memories


def compute_report(
    memories: list[ServedMemory],
    query_keywords: list[str] | None = None,
    source: dict[str, str] | None = None,
) -> dict:
    per_memory: list[dict[str, object]] = []
    matched_n = 0
    vector_only_serve_count = 0
    matched_keyword_instances = 0

    any_keyword_data = any(
        bool(memory.matched_keywords) or (memory.keyword_score is not None)
        for memory in memories
    )
    any_matched_keywords_present = any(bool(memory.matched_keywords) for memory in memories)

    all_matched_keywords: set[str] = set()

    for memory in memories:
        matched_keywords = list(memory.matched_keywords)
        matched_count = len(matched_keywords)
        keyword_score = memory.keyword_score
        vector_score = memory.vector_score
        combined_score = memory.combined_score

        is_matched = matched_count > 0 or ((keyword_score or 0.0) > 0)
        if is_matched:
            matched_n += 1

        vector_signal = (vector_score or 0.0) > 0 or (combined_score or 0.0) > 0
        vector_only = (matched_count == 0 and (keyword_score is None or keyword_score == 0)) and vector_signal
        if vector_only:
            vector_only_serve_count += 1

        matched_keyword_instances += matched_count
        all_matched_keywords.update(matched_keywords)

        per_memory.append(
            {
                "cid": memory.cid,
                "matched_keywords": matched_keywords,
                "matched_count": matched_count,
                "keyword_score": keyword_score,
                "vector_score": vector_score,
                "combined_score": combined_score,
                "vector_only": vector_only,
            }
        )

    served_n = len(memories)
    match_rate = (matched_n / served_n) if served_n else None

    unmatched_query_keywords: list[str] | None
    if query_keywords is not None and any_matched_keywords_present:
        unmatched_query_keywords = []
        seen_query: set[str] = set()
        for keyword in query_keywords:
            token = str(keyword)
            if token in seen_query:
                continue
            seen_query.add(token)
            if token not in all_matched_keywords:
                unmatched_query_keywords.append(token)
    else:
        unmatched_query_keywords = None

    if source is None:
        source = {
            "artifact": "<unknown>",
            "kind": "unknown",
            "data_completeness": "full" if any_keyword_data else "served_only",
        }
    elif "data_completeness" not in source:
        source = dict(source)
        source["data_completeness"] = "full" if any_keyword_data else "served_only"

    return {
        "source": source,
        "aggregate": {
            "served_n": served_n,
            "matched_n": matched_n,
            "match_rate": match_rate,
            "vector_only_serve_count": vector_only_serve_count,
            "matched_keyword_instances": matched_keyword_instances,
        },
        "unmatched_query_keywords": unmatched_query_keywords,
        "memories": per_memory,
    }


def _collect_telemetry_candidates(directory: Path) -> list[Path]:
    direct = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".json")
    recursive = sorted(path for path in directory.rglob("*.json") if path.is_file())
    ordered = direct + [path for path in recursive if path not in direct]

    parseable: list[Path] = []
    for path in ordered:
        try:
            parse_telemetry_json(path)
        except Exception:  # noqa: BLE001 - probe parser intentionally tolerant
            continue
        parseable.append(path)
    return parseable


def _collect_recall_smoke_candidate(directory: Path) -> Path | None:
    direct = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".log")
    recursive = sorted(path for path in directory.rglob("*.log") if path.is_file())
    ordered = direct + [path for path in recursive if path not in direct]
    for path in ordered:
        memories, _query_keywords = parse_recall_smoke_log(path)
        if memories:
            return path
    return None


def _collect_plugin_candidate(directory: Path) -> Path | None:
    direct = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".log")
    recursive = sorted(path for path in directory.rglob("*.log") if path.is_file())
    ordered = direct + [path for path in recursive if path not in direct]
    for path in ordered:
        memories = parse_plugin_log(path)
        if memories:
            return path
    return None


def build_report_for_input(target: Path) -> dict:
    if target.is_file():
        try:
            memories = parse_telemetry_json(target)
        except Exception:  # noqa: BLE001 - detection path
            memories = None
        if memories is not None:
            return compute_report(
                memories,
                source={
                    "artifact": str(target),
                    "kind": "telemetry_json",
                    "data_completeness": "full",
                },
            )

        recall_memories, query_keywords = parse_recall_smoke_log(target)
        if recall_memories:
            return compute_report(
                recall_memories,
                query_keywords=query_keywords,
                source={
                    "artifact": str(target),
                    "kind": "recall_smoke_log",
                    "data_completeness": "full",
                },
            )

        plugin_memories = parse_plugin_log(target)
        if plugin_memories:
            return compute_report(
                plugin_memories,
                source={
                    "artifact": str(target),
                    "kind": "plugin_log",
                    "data_completeness": "served_only",
                },
            )

        raise ValueError(f"no parseable artifact found at file: {target}")

    if not target.is_dir():
        raise ValueError(f"path does not exist or is not a file/directory: {target}")

    telemetry_paths = _collect_telemetry_candidates(target)
    if telemetry_paths:
        aggregated: list[ServedMemory] = []
        for path in telemetry_paths:
            aggregated.extend(parse_telemetry_json(path))
        return compute_report(
            aggregated,
            source={
                "artifact": ",".join(str(path) for path in telemetry_paths),
                "kind": "telemetry_json",
                "data_completeness": "full",
            },
        )

    recall_path = _collect_recall_smoke_candidate(target)
    if recall_path is not None:
        recall_memories, query_keywords = parse_recall_smoke_log(recall_path)
        return compute_report(
            recall_memories,
            query_keywords=query_keywords,
            source={
                "artifact": str(recall_path),
                "kind": "recall_smoke_log",
                "data_completeness": "full",
            },
        )

    plugin_path = _collect_plugin_candidate(target)
    if plugin_path is not None:
        plugin_memories = parse_plugin_log(plugin_path)
        return compute_report(
            plugin_memories,
            source={
                "artifact": str(plugin_path),
                "kind": "plugin_log",
                "data_completeness": "served_only",
            },
        )

    raise ValueError(
        "no parseable artifact found (expected telemetry JSON, recall-smoke log, or plugin log)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compute keyword match-rate from bench artifacts")
    parser.add_argument("run_dir_or_artifact", help="Run directory or artifact file path")
    parser.add_argument("--json", dest="json_out", help="Optional output path for report JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.run_dir_or_artifact).expanduser().resolve()

    try:
        report = build_report_for_input(target)
    except ValueError as exc:
        print(f"keyword_match_rate: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.json_out:
        out_path = Path(args.json_out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
