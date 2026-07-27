"""Build a distilled corpus by running SWEContextBench transcripts through /v1/extract.

This script implements the EXTRACT half of corpus construction:
SWEContextBench past-experience transcript -> hosted distillation (/v1/extract)
-> corpus JSON consumable by scripts/seed_corpus.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seed_corpus import _load_identity, _required_env
from wevibe_bench.lifecycle.lconfig import (
    DEFAULT_CONTRIB_KEYSTORE_PATH,
    DEFAULT_LEADER_KEYSTORE_PATH,
    LifecycleConfig,
)
from wevibe_bench.lifecycle.logging_util import run_logger
from wevibe_bench.lifecycle.m2_proof import M2Proof
from wevibe_bench.lifecycle.mcp_process import McpInstance, McpProcessManager
from wevibe_bench.lifecycle.orchestrator import LifecycleOrchestrator


DEFAULT_TRANSCRIPT_DIR = "~/Desktop/benchmark/SWEContextBench/cases/SWEContextBench Lite Past Experience"
DEFAULT_MODEL = "kimi/kimi-k3"
DEFAULT_OUT = "~/Desktop/benchmark/datasets/swecontextbench/distilled_corpus.json"
DEFAULT_TOPIC = "swecontextbench-distilled"
DEFAULT_MAX_TRANSCRIPT_CHARS = 120_000
DEFAULT_OPENCODE_AUTH = "~/.local/share/opencode/auth.json"
DEFAULT_EXTRACT_ATTEMPTS = 3
EXTRACT_RETRY_SLEEP_SECONDS = 2.0
_INSTANCE_ID_RE = re.compile(r"instance_id:\s*([A-Za-z0-9_.-]+)")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _emit(logger: Any, message: str) -> None:
    logger.info(message)
    print(message)


def _sha256_first8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = " …[truncated]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _json_line(path: Path, line_no: int, raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid JSONL object at {path}:{line_no}: expected object")
    return payload


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text" and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                    continue
                if isinstance(item.get("content"), str):
                    chunks.append(item["content"])
                    continue
                chunks.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
                continue
            chunks.append(str(item))
        return "\n".join(chunks)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return str(content["text"])
        if isinstance(content.get("content"), str):
            return str(content["content"])
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    if content is None:
        return ""
    return str(content)


def _assistant_turn_lines(entry: dict[str, Any]) -> list[str]:
    message = entry.get("message")
    if not isinstance(message, dict):
        return []

    content = message.get("content")
    blocks: list[Any]
    if isinstance(content, list):
        blocks = content
    elif content is None:
        blocks = []
    else:
        blocks = [content]

    lines: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            text = block.strip()
            if text:
                lines.append(f"ASSISTANT: {text}")
            continue

        if not isinstance(block, dict):
            continue

        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                lines.append(f"ASSISTANT: {text.strip()}")
            continue

        if block_type == "tool_use":
            tool_name = block.get("name")
            if not isinstance(tool_name, str) or not tool_name.strip():
                tool_name = "unknown"

            raw_input = block.get("input")
            if isinstance(raw_input, dict):
                if isinstance(raw_input.get("command"), str):
                    tool_text = raw_input["command"]
                elif isinstance(raw_input.get("args"), list):
                    tool_text = " ".join(str(part) for part in raw_input["args"])
                else:
                    tool_text = json.dumps(raw_input, ensure_ascii=False, sort_keys=True)
            elif isinstance(raw_input, str):
                tool_text = raw_input
            else:
                tool_text = json.dumps(raw_input, ensure_ascii=False, sort_keys=True)

            lines.append(f"TOOL({tool_name}): {_truncate(_one_line(tool_text), 600)}")

    return lines


def _user_tool_result_lines(entry: dict[str, Any]) -> list[str]:
    message = entry.get("message")
    if not isinstance(message, dict):
        return []

    content = message.get("content")
    if not isinstance(content, list):
        return []

    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        body = _content_text(block.get("content"))
        cleaned = _one_line(body)
        if cleaned:
            lines.append(f"RESULT: {_truncate(cleaned, 800)}")
    return lines


def _extract_summary(entries: list[dict[str, Any]]) -> str | None:
    for entry in entries:
        if entry.get("type") != "summary":
            continue
        summary = entry.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return None


def _extract_first_user_content(entries: list[dict[str, Any]]) -> str:
    for entry in entries:
        if entry.get("type") != "user":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        text = _content_text(message.get("content")).strip()
        if text:
            return text
    raise RuntimeError("transcript missing first user content")


def _extract_repo_slug(first_user_content: str) -> str | None:
    match = re.search(r"\brepo:\s*([^\s]+)", first_user_content)
    if not match:
        return None
    repo = match.group(1).strip()
    return repo or None


def _cap_with_header(header: str, body_lines: list[str], max_chars: int) -> str:
    if max_chars <= 0:
        raise RuntimeError("--max-transcript-chars must be > 0")

    header_clean = header.strip()
    if not header_clean:
        raise RuntimeError("transcript header unexpectedly empty")

    if len(header_clean) >= max_chars:
        return _truncate(header_clean, max_chars)

    out = header_clean
    for line in body_lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        candidate = f"\n{line_clean}"
        if len(out) + len(candidate) <= max_chars:
            out += candidate
            continue

        remaining = max_chars - len(out)
        if remaining > 0:
            out += _truncate(candidate, remaining)
        break

    return out


def _load_transcript_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            entries.append(_json_line(path, line_no, stripped))
    if not entries:
        raise RuntimeError(f"transcript is empty: {path}")
    return entries


def build_compact_transcript(path: Path, max_chars: int) -> dict[str, Any]:
    entries = _load_transcript_entries(path)
    summary = _extract_summary(entries)
    first_user = _extract_first_user_content(entries)
    repo = _extract_repo_slug(first_user)

    header_lines: list[str] = []
    if summary:
        header_lines.append(f"SUMMARY: {summary}")
    header_lines.append("USER_CONTEXT:")
    header_lines.append(first_user)
    header = "\n".join(header_lines)

    body_lines: list[str] = []
    for entry in entries:
        kind = entry.get("type")
        if kind == "assistant":
            body_lines.extend(_assistant_turn_lines(entry))
            continue
        if kind == "user":
            body_lines.extend(_user_tool_result_lines(entry))
            continue
        if kind in {"summary", "file-history-snapshot"}:
            continue

    compact = _cap_with_header(header, body_lines, max_chars)
    return {
        "transcript": compact,
        "summary": summary,
        "first_user": first_user,
        "repo": repo,
        "chars": len(compact),
    }


def build_compact_transcript_mini(path: Path, max_chars: int) -> dict[str, Any]:
    if max_chars <= 0:
        raise RuntimeError("--max-transcript-chars must be > 0")

    entries = _load_transcript_entries(path)
    summary: str | None = None
    first_user: str | None = None

    sections: list[str] = []
    assistant_sections: list[str] = []
    for entry in entries:
        if entry.get("type") == "meta":
            continue

        role_raw = entry.get("role")
        if not isinstance(role_raw, str) or not role_raw.strip():
            raise RuntimeError(f"mini transcript entry missing role in {path}")
        role = role_raw.strip().lower()

        turn = entry.get("turn")
        turn_label = str(turn) if isinstance(turn, int) else "?"

        content_raw = entry.get("content")
        content = content_raw if isinstance(content_raw, str) else _content_text(content_raw)

        section = f"\n=== {role.upper()} (turn {turn_label}) ===\n{content}"
        sections.append(section)
        if role == "assistant":
            assistant_sections.append(section)
        if role == "user" and first_user is None:
            first_user = content.strip()

    if not sections:
        raise RuntimeError(f"mini transcript has no message entries: {path}")

    full_compact = "".join(sections).lstrip("\n")
    if len(full_compact) <= max_chars:
        compact = full_compact
    else:
        assistant_compact = "".join(assistant_sections).lstrip("\n")
        if assistant_compact:
            compact = assistant_compact if len(assistant_compact) <= max_chars else _truncate(assistant_compact, max_chars)
        else:
            compact = _truncate(full_compact, max_chars)

    first_user_text = first_user or ""
    repo = _extract_repo_slug(first_user_text) if first_user_text else None
    return {
        "transcript": compact,
        "summary": summary,
        "first_user": first_user_text,
        "repo": repo,
        "chars": len(compact),
    }


def _load_experience_ids(path: Path) -> list[str]:
    if not path.is_file():
        raise RuntimeError(f"--experiences file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise RuntimeError(f"--experiences file is empty: {path}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"--experiences file is invalid JSON: {path}") from exc

    if not isinstance(payload, list):
        raise RuntimeError("--experiences must decode to a JSON array")

    ids: list[str] = []
    for idx, item in enumerate(payload, start=1):
        if isinstance(item, str):
            candidate = item.strip()
        elif isinstance(item, dict):
            value = item.get("id")
            candidate = value.strip() if isinstance(value, str) else ""
        else:
            raise RuntimeError(f"--experiences item #{idx} must be string or object with 'id'")

        if not candidate:
            raise RuntimeError(f"--experiences item #{idx} has empty id")
        ids.append(candidate)

    return ids


def _parse_inline_ids(raw: str) -> list[str]:
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    return ids


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _index_transcript_files(transcript_dir: Path) -> dict[str, list[Path]]:
    if not transcript_dir.is_dir():
        raise RuntimeError(f"--transcript-dir not found: {transcript_dir}")

    index: dict[str, list[Path]] = {}
    for path in sorted(transcript_dir.rglob("*.jsonl")):
        found_ids: set[str] = set()
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                if "instance_id:" not in raw_line:
                    continue
                for match in _INSTANCE_ID_RE.finditer(raw_line):
                    candidate = match.group(1).strip()
                    if candidate:
                        found_ids.add(candidate)

        for instance_id in found_ids:
            index.setdefault(instance_id, []).append(path)

    return index


def _index_transcript_files_mini(transcript_dir: Path) -> dict[str, Path]:
    if not transcript_dir.is_dir():
        raise RuntimeError(f"--transcript-dir not found: {transcript_dir}")

    index: dict[str, Path] = {}
    for path in sorted(transcript_dir.rglob("*.jsonl")):
        instance_id = path.stem.strip()
        if not instance_id:
            continue
        existing = index.get(instance_id)
        if existing is not None:
            raise RuntimeError(
                f"multiple mini transcript files found for id={instance_id!r}: {existing}, {path}"
            )
        index[instance_id] = path

    return index


def _find_transcript_file(index: dict[str, list[Path]], experience_id: str) -> Path:
    matches = index.get(experience_id, [])
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(f"no transcript file found for id={experience_id!r}")
    joined = ", ".join(str(path) for path in matches)
    raise RuntimeError(f"multiple transcript files found for id={experience_id!r}: {joined}")


def _find_transcript_file_mini(index: dict[str, Path], experience_id: str) -> Path:
    match = index.get(experience_id)
    if match is not None:
        return match
    raise RuntimeError(f"no transcript file found for id={experience_id!r}")


def _load_openrouter_key(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"OpenCode auth file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise RuntimeError(f"OpenCode auth file is empty: {path}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenCode auth file is invalid JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"OpenCode auth file must decode to object: {path}")

    openrouter = payload.get("openrouter")
    if not isinstance(openrouter, dict):
        raise RuntimeError("OpenCode auth file missing object field 'openrouter'")

    key = openrouter.get("key")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("OpenCode auth file missing non-empty openrouter.key")

    return key.strip()


def _is_empty_off_task_extract_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "no usable memory candidate" in message
        or "off_task_output" in message
        or "emptyreason" in message
    )


def _fallback_keyword(repo: str | None, experience_id: str) -> str | None:
    repo_name = ""
    if isinstance(repo, str) and repo.strip():
        repo_name = repo.strip().split("/")[-1]
    if repo_name:
        token = re.sub(r"[^a-z0-9]+", " ", repo_name.lower()).strip().split(" ")
        if token and token[0]:
            return token[0]

    prefix = experience_id.split("__", 1)[0].strip().lower()
    prefix = re.sub(r"[^a-z0-9]+", " ", prefix).strip()
    if prefix:
        return prefix.split(" ")[0]
    return None


def _coerce_keywords(raw_keywords: Any, *, repo: str | None, experience_id: str) -> list[str]:
    cleaned: list[str] = []

    if isinstance(raw_keywords, list):
        for keyword in raw_keywords:
            if not isinstance(keyword, str):
                continue
            candidate = keyword.strip()
            if candidate:
                cleaned.append(candidate)
    elif isinstance(raw_keywords, str):
        candidate = raw_keywords.strip()
        if candidate:
            cleaned.append(candidate)

    deduped = _dedupe_preserve_order(cleaned)
    if deduped:
        return deduped

    fallback = _fallback_keyword(repo, experience_id)
    if fallback:
        return [fallback]

    raise RuntimeError(f"unable to derive non-empty keywords for id={experience_id!r}")


def _coerce_stack_hint(raw_stack_hint: Any) -> str | None:
    if raw_stack_hint is None:
        return None
    if isinstance(raw_stack_hint, str):
        return raw_stack_hint.strip() or None
    if isinstance(raw_stack_hint, list):
        parts = [item.strip() for item in raw_stack_hint if isinstance(item, str) and item.strip()]
        return ", ".join(parts) if parts else None
    return None


def _guess_stack(repo: str | None, experience_id: str) -> str:
    if repo and repo.strip():
        low = repo.lower()
        if any(tok in low for tok in ("flask", "django", "pandas", "numpy", "pytest", "seaborn")):
            return "python"
        if any(tok in low for tok in ("react", "next", "node", "express", "webpack")):
            return "javascript"
        if any(tok in low for tok in ("rust", "cargo", "tokio")):
            return "rust"
        if any(tok in low for tok in ("go", "golang", "gin")):
            return "go"
        return repo

    fallback = experience_id.split("__", 1)[0].strip()
    return fallback or experience_id


def _memory_from_payload(
    *,
    experience_id: str,
    payload: dict[str, Any],
    repo: str | None,
) -> dict[str, Any]:
    text_raw = payload.get("text")
    text = text_raw.strip() if isinstance(text_raw, str) else ""
    if not text:
        raise RuntimeError(f"extract returned empty text for id={experience_id!r}")

    keywords = _coerce_keywords(payload.get("keywords"), repo=repo, experience_id=experience_id)
    stack_hint = _coerce_stack_hint(payload.get("stack_hint"))

    return {
        "id": experience_id,
        "text": text,
        "keywords": keywords,
        "stack_hint": stack_hint,
    }


def _validate_memory_row(row: Any, source: Path) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise RuntimeError(f"invalid memory row in {source}: expected object")

    memory_id = row.get("id")
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise RuntimeError(f"invalid memory row in {source}: missing non-empty id")

    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"invalid memory row in {source} id={memory_id!r}: missing non-empty text")

    keywords = row.get("keywords")
    if not isinstance(keywords, list):
        raise RuntimeError(f"invalid memory row in {source} id={memory_id!r}: keywords must be list")
    cleaned_keywords = [kw.strip() for kw in keywords if isinstance(kw, str) and kw.strip()]
    if not cleaned_keywords:
        raise RuntimeError(f"invalid memory row in {source} id={memory_id!r}: keywords cannot be empty")

    stack_hint = row.get("stack_hint")
    if stack_hint is not None and not isinstance(stack_hint, str):
        raise RuntimeError(f"invalid memory row in {source} id={memory_id!r}: stack_hint must be str or null")

    return {
        "id": memory_id.strip(),
        "text": text.strip(),
        "keywords": _dedupe_preserve_order(cleaned_keywords),
        "stack_hint": stack_hint.strip() if isinstance(stack_hint, str) and stack_hint.strip() else None,
    }


def _load_memory_rows(path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise RuntimeError(f"existing file is empty: {path}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"existing file has invalid JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"existing file must decode to object: {path}")

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise RuntimeError(f"existing file missing non-empty topic: {path}")

    raw_memories = payload.get("memories")
    if not isinstance(raw_memories, list):
        raise RuntimeError(f"existing file missing array 'memories': {path}")

    memories: dict[str, dict[str, Any]] = {}
    for item in raw_memories:
        row = _validate_memory_row(item, path)
        memory_id = row["id"]
        if memory_id in memories and memories[memory_id] != row:
            raise RuntimeError(f"duplicate conflicting memory id={memory_id!r} in {path}")
        memories[memory_id] = row

    return topic.strip(), memories


def _ordered_rows(memories_by_id: dict[str, dict[str, Any]], requested_ids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for memory_id in requested_ids:
        row = memories_by_id.get(memory_id)
        if row is None:
            continue
        rows.append(row)
        seen.add(memory_id)

    for memory_id in sorted(memories_by_id):
        if memory_id in seen:
            continue
        rows.append(memories_by_id[memory_id])
    return rows


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(path)


def _write_progress(
    *,
    out_path: Path,
    checkpoint_path: Path,
    topic: str,
    memories_by_id: dict[str, dict[str, Any]],
    requested_ids: list[str],
) -> None:
    rows = _ordered_rows(memories_by_id, requested_ids)
    output_payload = {
        "topic": topic,
        "memories": rows,
    }
    _atomic_write_json(out_path, output_payload)

    checkpoint_payload = {
        "topic": topic,
        "done_ids": [row["id"] for row in rows],
        "memories": rows,
        "updated_at": _utc_now_iso(),
    }
    _atomic_write_json(checkpoint_path, checkpoint_payload)


def _load_resume_memories(
    *,
    resume: bool,
    topic: str,
    out_path: Path,
    checkpoint_path: Path,
) -> dict[str, dict[str, Any]]:
    if not resume:
        return {}

    merged: dict[str, dict[str, Any]] = {}
    for source in (checkpoint_path, out_path):
        if not source.is_file():
            continue

        source_topic, source_rows = _load_memory_rows(source)
        if source_topic != topic:
            raise RuntimeError(
                f"resume topic mismatch in {source}: file topic={source_topic!r}, requested topic={topic!r}"
            )

        for memory_id, row in source_rows.items():
            existing = merged.get(memory_id)
            if existing is not None and existing != row:
                raise RuntimeError(f"resume conflict for id={memory_id!r} between output/checkpoint")
            merged[memory_id] = row

    return merged


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build distilled corpus JSON from SWEContextBench transcripts via /v1/extract.",
    )
    parser.add_argument(
        "--experiences",
        type=Path,
        default=None,
        help="JSON file with ids (array of strings or array of {id: ...}).",
    )
    parser.add_argument(
        "--experiences-inline",
        type=str,
        default="",
        help="Comma-separated experience ids (id1,id2,...).",
    )
    parser.add_argument(
        "--transcript-dir",
        type=Path,
        default=Path(DEFAULT_TRANSCRIPT_DIR).expanduser(),
        help="Directory containing SWEContextBench past-experience JSONL transcripts.",
    )
    parser.add_argument(
        "--transcript-format",
        choices=("claude", "mini"),
        default="claude",
        help="Transcript JSONL schema: 'claude' (legacy Claude-Code) or 'mini' (WeVibe seed-producer).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help="Hosted distiller model slug passed through to /v1/extract.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_OUT).expanduser(),
        help="Output corpus JSON path.",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=DEFAULT_TOPIC,
        help="Corpus topic.",
    )
    parser.add_argument(
        "--max-transcript-chars",
        type=int,
        default=DEFAULT_MAX_TRANSCRIPT_CHARS,
        help="Hard cap for compact transcript characters sent to extract.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from <out>.checkpoint.json and/or existing --out contents.",
    )
    parser.add_argument(
        "--keep-clone",
        action="store_true",
        help="Keep leader/contributor clone MCP processes alive on exit.",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    ids: list[str] = []
    if args.experiences is not None:
        ids.extend(_load_experience_ids(args.experiences.expanduser()))
    if args.experiences_inline.strip():
        ids.extend(_parse_inline_ids(args.experiences_inline))
    ids = _dedupe_preserve_order(ids)
    if not ids:
        raise RuntimeError("provide at least one experience id via --experiences and/or --experiences-inline")

    transcript_dir = args.transcript_dir.expanduser()
    out_path = args.out.expanduser()
    checkpoint_path = Path(str(out_path) + ".checkpoint.json")

    leader = _load_identity("WEVIBE_BENCH_LEADER_SEED_HEX")
    contributor = _load_identity("WEVIBE_BENCH_CONTRIB_SEED_HEX")
    leader_wallet = _required_env("WEVIBE_BENCH_LEADER_WALLET")

    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    logger = run_logger("build-distilled-corpus", cfg.runs_dir)
    logfile = getattr(logger, "logfile_path", "")

    wevibe_root = os.environ.get("WEVIBE_BENCH_WEVIBE_ROOT", str(Path(__file__).resolve().parents[2]))
    leader_keystore = os.environ.get("WEVIBE_BENCH_LEADER_KEYSTORE", DEFAULT_LEADER_KEYSTORE_PATH)
    contributor_keystore = os.environ.get("WEVIBE_BENCH_CONTRIB_KEYSTORE", DEFAULT_CONTRIB_KEYSTORE_PATH)

    api_key = _load_openrouter_key(Path(DEFAULT_OPENCODE_AUTH).expanduser())
    api_key_fp = _sha256_first8(api_key)

    _emit(
        logger,
        (
            "[distill] start "
            f"ids={len(ids)} transcript_dir={transcript_dir} out={out_path} checkpoint={checkpoint_path} "
            f"topic={args.topic} model={args.model} max_transcript_chars={args.max_transcript_chars} "
            f"resume={args.resume} keep_clone={args.keep_clone} logfile={logfile}"
        ),
    )
    _emit(
        logger,
        (
            "[distill] api_key provider=openrouter "
            f"present={bool(api_key)} sha256_first8={api_key_fp}"
        ),
    )

    transcript_index: dict[str, list[Path]] | None = None
    transcript_index_mini: dict[str, Path] | None = None
    if args.transcript_format == "mini":
        transcript_index_mini = _index_transcript_files_mini(transcript_dir)
    else:
        transcript_index = _index_transcript_files(transcript_dir)
    memories_by_id = _load_resume_memories(
        resume=args.resume,
        topic=args.topic,
        out_path=out_path,
        checkpoint_path=checkpoint_path,
    )

    if args.resume:
        _emit(
            logger,
            (
                "[distill] resume "
                f"loaded={len(memories_by_id)} from={checkpoint_path if checkpoint_path.is_file() else '<none>'} "
                f"out_exists={out_path.is_file()}"
            ),
        )

    procman = McpProcessManager(wevibe_root=wevibe_root, cfg=cfg, logger=logger)
    orchestrator = LifecycleOrchestrator(
        cfg=cfg,
        wevibe_root=wevibe_root,
        leader=leader,
        contributor=contributor,
        leader_keystore=leader_keystore,
        contributor_keystore=contributor_keystore,
        leader_wallet=leader_wallet,
        logger=logger,
        procman=procman,
    )
    proof = M2Proof(
        cfg=cfg,
        orchestrator=orchestrator,
        leader=leader,
        contributor=contributor,
        logger=logger,
        direct_memory=None,
    )

    leader_instance: McpInstance | None = None
    contributor_instance: McpInstance | None = None
    try:
        leader_instance, contributor_instance = orchestrator.bring_up(build=False)

        total = len(ids)
        failed_ids: list[str] = []
        for idx, experience_id in enumerate(ids, start=1):
            if args.resume and experience_id in memories_by_id:
                _emit(
                    logger,
                    f"[distill] {idx}/{total} id={experience_id} already distilled in checkpoint -> skipping",
                )
                continue

            try:
                if args.transcript_format == "mini":
                    if transcript_index_mini is None:
                        raise RuntimeError("internal error: mini transcript index not initialized")
                    transcript_file = _find_transcript_file_mini(transcript_index_mini, experience_id)
                    compact = build_compact_transcript_mini(transcript_file, args.max_transcript_chars)
                else:
                    if transcript_index is None:
                        raise RuntimeError("internal error: transcript index not initialized")
                    transcript_file = _find_transcript_file(transcript_index, experience_id)
                    compact = build_compact_transcript(transcript_file, args.max_transcript_chars)
                compact_text = str(compact["transcript"])
                transcript_chars = len(compact_text)
                repo = compact.get("repo") if isinstance(compact.get("repo"), str) else None

                _emit(
                    logger,
                    (
                        f"[distill] {idx}/{total} id={experience_id} "
                        f"transcript_file={transcript_file} transcript_chars={transcript_chars} "
                        f"model={args.model} -> extracting"
                    ),
                )

                extract_payload: dict[str, Any] | None = None
                latency_ms = 0
                for attempt in range(1, DEFAULT_EXTRACT_ATTEMPTS + 1):
                    t0 = time.perf_counter()
                    try:
                        extract_payload = proof.produce_memory(
                            transcript=compact_text,
                            model=args.model,
                            api_key=api_key,
                            project_context={
                                "title": experience_id,
                                "directory": repo or experience_id,
                                "stack": [_guess_stack(repo, experience_id)],
                            },
                            org_id="",
                            provider="openrouter",
                        )
                        latency_ms = int((time.perf_counter() - t0) * 1000)
                        break
                    except Exception as exc:
                        should_retry = (
                            attempt < DEFAULT_EXTRACT_ATTEMPTS
                            and _is_empty_off_task_extract_failure(exc)
                        )
                        if not should_retry:
                            raise
                        warn_msg = (
                            f"[distill] {idx}/{total} id={experience_id} "
                            f"attempt={attempt}/{DEFAULT_EXTRACT_ATTEMPTS} empty/off-task, retrying"
                        )
                        logger.warning(warn_msg)
                        print(warn_msg)
                        time.sleep(EXTRACT_RETRY_SLEEP_SECONDS)

                if extract_payload is None:
                    raise RuntimeError("extract payload missing after retries")

                memory_row = _memory_from_payload(
                    experience_id=experience_id,
                    payload=extract_payload,
                    repo=repo,
                )
                memories_by_id[experience_id] = memory_row

                _write_progress(
                    out_path=out_path,
                    checkpoint_path=checkpoint_path,
                    topic=args.topic,
                    memories_by_id=memories_by_id,
                    requested_ids=ids,
                )

                stack_hint = memory_row.get("stack_hint")
                stack_hint_text = stack_hint if isinstance(stack_hint, str) else "<none>"
                _emit(
                    logger,
                    (
                        f"[distill] {idx}/{total} id={experience_id} "
                        f"latency_ms={latency_ms} text_size={len(memory_row['text'])} "
                        f"keywords={len(memory_row['keywords'])} stack_hint={stack_hint_text} DONE"
                    ),
                )
            except Exception as exc:
                error_msg = f"[distill] {idx}/{total} id={experience_id} FAILED after retries: {exc}"
                logger.exception(error_msg)
                print(error_msg)
                failed_ids.append(experience_id)
                continue

        _write_progress(
            out_path=out_path,
            checkpoint_path=checkpoint_path,
            topic=args.topic,
            memories_by_id=memories_by_id,
            requested_ids=ids,
        )
        final_rows = _ordered_rows(memories_by_id, ids)
        _emit(
            logger,
            (
                "[distill] complete "
                f"memories={len(final_rows)} out={out_path} checkpoint={checkpoint_path}"
            ),
        )
        _emit(
            logger,
            f"[distill] failed_ids={failed_ids} ({len(failed_ids)}/{total})",
        )

        if args.keep_clone and leader_instance is not None and contributor_instance is not None:
            _emit(
                logger,
                (
                    "[distill] KEEP_CLONE "
                    f"leader_pid={leader_instance.pid} leader_port={leader_instance.port} "
                    f"contrib_pid={contributor_instance.pid} contrib_port={contributor_instance.port}"
                ),
            )

        return 1 if failed_ids else 0
    except Exception as exc:
        logger.exception("[distill] failed err=%s", exc)
        print(f"[distill] ERROR {exc}")
        return 1
    finally:
        if not args.keep_clone:
            if contributor_instance is not None:
                procman.stop(contributor_instance)
            if leader_instance is not None:
                procman.stop(leader_instance)


if __name__ == "__main__":
    raise SystemExit(main())
