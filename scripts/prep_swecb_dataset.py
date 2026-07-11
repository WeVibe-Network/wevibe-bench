"""Prepare SWEContextBench Lite held-out/corpus/edge-map artifacts for local ablation runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_LITE_DIR = Path("~/Desktop/benchmark/SWEContextBench/cases/SWEContextBench Lite").expanduser()
DEFAULT_EXPERIENCE_DIR = Path(
    "~/Desktop/benchmark/SWEContextBench/cases/SWEContextBench Lite Past Experience"
).expanduser()
DEFAULT_OUTPUT_DIR = Path("~/Desktop/benchmark/datasets/swecontextbench").expanduser()
DEFAULT_HF_LOCAL_DIR = DEFAULT_OUTPUT_DIR / "hf"
DEFAULT_REL_PARQUET = DEFAULT_HF_LOCAL_DIR / "data" / "SWEContextBench_Relationship.parquet"

TOPIC = "swecontextbench-experience"

# Hub rejects memory plaintext over ~2000 UTF-8 bytes; cap with margin.
MAX_MEMORY_TEXT_BYTES = 1800


def _cap_utf8_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore").rstrip()
KEYWORD_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
INSTANCE_RE = re.compile(r"instance_id:\s*(\S+)")
REPO_RE = re.compile(r"repo:\s*([^\s]+/[^\s]+)")
PROBLEM_LABEL_RE = re.compile(r"problem_statement:\s*", re.IGNORECASE)
NEXT_TOP_LEVEL_KEY_RE = re.compile(r"\n\s{2}[A-Za-z_][A-Za-z0-9_]*\s*:")

SHORTLIST_REPO_ORDER = [
    "pallets/flask",
    "psf/requests",
    "pytest-dev/pytest",
    "pylint-dev/pylint",
    "django/django",
]
SHORTLIST_MAX = 10

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


@dataclass
class TranscriptRecord:
    file_name: str
    summary: str
    instance_id: str | None
    repo: str | None
    problem_statement: str | None
    parse_error: str | None
    summary_fallback_used: bool


def log(message: str) -> None:
    print(f"[prep_swecb_dataset] {message}")


def sha8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def set_fingerprint(values: set[str] | list[str]) -> str:
    if not values:
        return "00000000"
    ordered = sorted(values)
    return sha8("\n".join(ordered))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lite-dir", type=Path, default=DEFAULT_LITE_DIR)
    parser.add_argument("--experience-dir", type=Path, default=DEFAULT_EXPERIENCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def ensure_relationship_parquet(output_dir: Path) -> Path:
    local_dir = output_dir / "hf"
    parquet_path = local_dir / "data" / "SWEContextBench_Relationship.parquet"
    if parquet_path.is_file():
        log(
            "relationship parquet ready "
            f"path={parquet_path} size_bytes={parquet_path.stat().st_size} fp={sha8(str(parquet_path))}"
        )
        return parquet_path

    local_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "hf",
        "download",
        "jiayuanz3/SWEContextBench",
        "data/SWEContextBench_Relationship.parquet",
        "--repo-type",
        "dataset",
        "--local-dir",
        str(local_dir),
    ]
    log("downloading relationship parquet via hf cli")
    log(f"hf_cmd={' '.join(cmd)}")
    try:
        completed = subprocess.run(cmd, check=False, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("hf CLI not found in PATH; install huggingface_hub[cli]") from exc

    if completed.stdout.strip():
        log(f"hf_stdout={completed.stdout.strip()}")
    if completed.stderr.strip():
        log(f"hf_stderr={completed.stderr.strip()}")
    if completed.returncode != 0:
        raise RuntimeError(f"hf download failed with exit code {completed.returncode}")
    if not parquet_path.is_file():
        raise RuntimeError(f"expected parquet missing after download: {parquet_path}")

    log(
        "relationship parquet downloaded "
        f"path={parquet_path} size_bytes={parquet_path.stat().st_size} fp={sha8(str(parquet_path))}"
    )
    return parquet_path


def load_heldout_lite(lite_dir: Path) -> list[dict[str, Any]]:
    lite_files = sorted(lite_dir.glob("*.json"))
    rows: list[dict[str, Any]] = []
    for path in lite_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.append(
            {
                "instance_id": payload["instance_id"],
                "repo": payload["repo"],
                "base_commit": payload["base_commit"],
                "problem_statement": payload["problem_statement"],
                "FAIL_TO_PASS": payload["FAIL_TO_PASS"],
                "PASS_TO_PASS": payload["PASS_TO_PASS"],
                "test_patch": payload["test_patch"],
                "version": payload["version"],
                "environment_setup_commit": payload["environment_setup_commit"],
            }
        )

    ids = [row["instance_id"] for row in rows]
    log(
        "loaded heldout lite "
        f"count={len(rows)} dir={lite_dir} ids_fp={set_fingerprint(ids)}"
    )
    return rows


def _decode_json_objects(text: str) -> tuple[list[Any], str | None]:
    decoder = json.JSONDecoder()
    records: list[Any] = []
    pos = 0
    text_len = len(text)

    while pos < text_len:
        while pos < text_len and text[pos].isspace():
            pos += 1
        if pos >= text_len:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError as exc:
            return records, f"json_decode_error pos={exc.pos} msg={exc.msg}"
        records.append(obj)
        pos = end

    return records, None


def _extract_problem_statement(user_content: str) -> str | None:
    marker = PROBLEM_LABEL_RE.search(user_content)
    if not marker:
        return None
    rest = user_content[marker.end() :]
    next_key = NEXT_TOP_LEVEL_KEY_RE.search(rest)
    if next_key:
        statement = rest[: next_key.start()]
    else:
        statement = rest
    statement = statement.strip()
    if not statement:
        return None
    if len(statement) > 1500:
        statement = statement[:1500].rstrip()
    return statement


def _first_user_content(objects: list[Any]) -> str | None:
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "user":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return None


def _first_summary(objects: list[Any]) -> str | None:
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "summary":
            continue
        summary = obj.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return None


def _derive_summary(problem_statement: str, fallback_id: str) -> str:
    first_line = ""
    for line in problem_statement.splitlines():
        candidate = re.sub(r"\s+", " ", line).strip()
        if candidate:
            first_line = candidate
            break

    if not first_line:
        first_line = f"experience {fallback_id}"

    if len(first_line) > 75:
        first_line = f"{first_line[:72].rstrip()}..."
    return first_line


def _sanitize_keyword(token: str) -> str | None:
    lowered = token.strip().lower().replace("-", "_")
    lowered = re.sub(r"[^a-z0-9_]", "", lowered)
    if KEYWORD_RE.fullmatch(lowered):
        return lowered
    return None


def _keywords_from_summary(repo_short: str, summary: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()

    repo_kw = _sanitize_keyword(repo_short)
    if repo_kw:
        keywords.append(repo_kw)
        seen.add(repo_kw)

    for token in re.split(r"[^A-Za-z0-9_]+", summary.lower()):
        if len(token) < 2 or token in STOPWORDS:
            continue
        keyword = _sanitize_keyword(token)
        if not keyword or keyword in seen:
            continue
        keywords.append(keyword)
        seen.add(keyword)
        if len(keywords) >= 20:
            break

    if keywords:
        return keywords

    fallback = _sanitize_keyword(repo_short) or "python"
    return [fallback]


def parse_transcript(path: Path) -> TranscriptRecord:
    text = path.read_text(encoding="utf-8")
    objects, parse_error = _decode_json_objects(text)

    summary = _first_summary(objects)
    summary_fallback_used = False
    user_content = _first_user_content(objects)

    # Fallback parsing from raw text for malformed shards: keep this lightweight and bounded.
    probe_text = text.replace("\\n", "\n")
    source_for_regex = user_content if user_content else probe_text

    instance_match = INSTANCE_RE.search(source_for_regex)
    repo_match = REPO_RE.search(source_for_regex)
    problem_statement = _extract_problem_statement(source_for_regex)

    instance_id = instance_match.group(1).strip() if instance_match else None
    repo = repo_match.group(1).strip() if repo_match else None

    if summary is None and problem_statement and instance_id:
        summary = _derive_summary(problem_statement, fallback_id=instance_id)
        summary_fallback_used = True

    if summary is None:
        summary = ""
        summary_fallback_used = True

    return TranscriptRecord(
        file_name=path.name,
        summary=summary.strip(),
        instance_id=instance_id,
        repo=repo,
        problem_statement=problem_statement,
        parse_error=parse_error,
        summary_fallback_used=summary_fallback_used,
    )


def build_experience_corpus(experience_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    transcript_files = sorted(experience_dir.glob("*.jsonl"))

    parsed_count = 0
    skipped: list[str] = []
    parse_errors: dict[str, str] = {}
    summary_fallbacks = 0

    memories: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    dupes: list[str] = []

    for path in transcript_files:
        record = parse_transcript(path)

        if record.parse_error:
            parse_errors[path.name] = record.parse_error

        if not record.instance_id or not record.problem_statement:
            skipped.append(path.name)
            continue

        parsed_count += 1

        if record.summary_fallback_used:
            summary_fallbacks += 1

        if not record.summary:
            summary_fallbacks += 1
            record.summary = _derive_summary(record.problem_statement, fallback_id=record.instance_id)

        if record.instance_id in seen_ids:
            dupes.append(record.instance_id)
            continue
        seen_ids.add(record.instance_id)

        repo_short = (
            (record.repo.split("/", 1)[1] if record.repo and "/" in record.repo else record.instance_id.split("__", 1)[0])
            .strip()
            .lower()
        )
        keywords = _keywords_from_summary(repo_short=repo_short, summary=record.summary)
        text = f"{record.summary}\n\n{record.problem_statement.strip()}".strip()
        # Hub enforces a ~2000 UTF-8 BYTE limit on the decrypted memory plaintext
        # (unicode-heavy sympy/matplotlib statements blow past it even under 2000 chars).
        # Cap on a char boundary with generous margin so seeding never rejects.
        text = _cap_utf8_bytes(text, MAX_MEMORY_TEXT_BYTES)

        memories.append(
            {
                "id": record.instance_id,
                "text": text,
                "keywords": keywords,
                "stack_hint": f"{repo_short},python",
            }
        )

    corpus = {"topic": TOPIC, "memories": memories}
    stats = {
        "transcript_files": len(transcript_files),
        "transcripts_parsed": parsed_count,
        "skipped": skipped,
        "parse_errors": parse_errors,
        "summary_fallbacks": summary_fallbacks,
        "dupes": dupes,
        "memories_after_dedupe": len(memories),
        "memory_ids": {m["id"] for m in memories},
    }
    return corpus, stats


def validate_corpus_schema(corpus: dict[str, Any]) -> None:
    memories = corpus.get("memories")
    if not isinstance(memories, list) or not memories:
        raise RuntimeError("experience_corpus.json must have non-empty memories list")

    for index, memory in enumerate(memories, start=1):
        if not isinstance(memory, dict):
            raise RuntimeError(f"memory #{index} must be an object")

        memory_id = memory.get("id")
        text = memory.get("text")
        keywords = memory.get("keywords")
        stack_hint = memory.get("stack_hint")

        if not isinstance(memory_id, str) or not memory_id.strip():
            raise RuntimeError(f"memory #{index} has invalid id")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"memory #{index} id={memory_id!r} has invalid text")
        if not isinstance(keywords, list) or not keywords:
            raise RuntimeError(f"memory #{index} id={memory_id!r} has invalid keywords")
        if not all(isinstance(item, str) and item.strip() for item in keywords):
            raise RuntimeError(f"memory #{index} id={memory_id!r} has non-string keyword")
        if not isinstance(stack_hint, str):
            raise RuntimeError(f"memory #{index} id={memory_id!r} has invalid stack_hint")


def build_edge_map(
    parquet_path: Path,
    heldout_instances: list[dict[str, Any]],
    seeded_experience_ids: set[str],
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    heldout_by_id = {row["instance_id"]: row for row in heldout_instances}
    heldout_ids = set(heldout_by_id)

    df = pd.read_parquet(parquet_path)
    required_columns = {
        "related_instance_id",
        "related_pr_url",
        "related_issue_url",
        "experience_instance_id",
        "experience_pr_url",
        "experience_issue_url",
    }
    missing = sorted(required_columns - set(df.columns))
    if missing:
        raise RuntimeError(f"relationship parquet missing required columns: {missing}")

    filtered = df[
        df["related_instance_id"].isin(heldout_ids)
        & df["experience_instance_id"].isin(seeded_experience_ids)
    ]

    edge_map: dict[str, list[str]] = {}
    for related_id, group in filtered.groupby("related_instance_id"):
        experiences = [str(exp) for exp in group["experience_instance_id"].tolist()]
        deduped = sorted(dict.fromkeys(experiences))
        edge_map[str(related_id)] = deduped

    n_edges = sum(len(v) for v in edge_map.values())
    n_related = len(edge_map)
    repo_distribution = Counter(heldout_by_id[instance_id]["repo"] for instance_id in edge_map)
    summary = {
        "n_related_with_seeded_edge": n_related,
        "n_edges": n_edges,
        "repo_distribution": dict(sorted(repo_distribution.items())),
    }
    return edge_map, summary


def shortlist_instances_with_edges(
    heldout_instances: list[dict[str, Any]],
    edge_map: dict[str, list[str]],
) -> list[tuple[str, str, str, list[str]]]:
    heldout_by_id = {row["instance_id"]: row for row in heldout_instances}
    rank = {repo: idx for idx, repo in enumerate(SHORTLIST_REPO_ORDER)}

    candidates = [instance_id for instance_id, edges in edge_map.items() if edges]
    candidates.sort(
        key=lambda instance_id: (
            rank.get(heldout_by_id[instance_id]["repo"], len(rank) + 100),
            heldout_by_id[instance_id]["repo"],
            instance_id,
        )
    )

    shortlist: list[tuple[str, str, str, list[str]]] = []
    for instance_id in candidates[:SHORTLIST_MAX]:
        heldout = heldout_by_id[instance_id]
        shortlist.append(
            (
                instance_id,
                heldout["repo"],
                str(heldout.get("version", "")),
                edge_map[instance_id],
            )
        )
    return shortlist


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n", encoding="utf-8")


def main() -> int:
    args = parse_args()

    lite_dir = args.lite_dir.expanduser()
    experience_dir = args.experience_dir.expanduser()
    output_dir = args.output_dir.expanduser()

    heldout_output = output_dir / "heldout_lite.json"
    corpus_output = output_dir / "experience_corpus.json"
    edge_map_output = output_dir / "edge_map.json"
    edge_map_summary_output = output_dir / "edge_map_summary.json"

    heldout_instances = load_heldout_lite(lite_dir)
    write_json(heldout_output, heldout_instances)

    corpus, corpus_stats = build_experience_corpus(experience_dir)
    validate_corpus_schema(corpus)
    write_json(corpus_output, corpus)

    parquet_path = ensure_relationship_parquet(output_dir)
    edge_map, edge_summary = build_edge_map(
        parquet_path=parquet_path,
        heldout_instances=heldout_instances,
        seeded_experience_ids=corpus_stats["memory_ids"],
    )
    write_json(edge_map_output, edge_map)
    write_json(edge_map_summary_output, edge_summary)

    shortlist = shortlist_instances_with_edges(heldout_instances, edge_map)

    skipped = corpus_stats["skipped"]
    parse_errors = corpus_stats["parse_errors"]

    log(
        "counts "
        f"lite_instances_loaded={len(heldout_instances)} "
        f"transcripts_parsed={corpus_stats['transcripts_parsed']} "
        f"skipped={len(skipped)}"
    )
    if skipped:
        for file_name in skipped:
            reason = parse_errors.get(file_name, "missing required fields")
            log(f"skipped_transcript file={file_name} reason={reason}")

    if parse_errors:
        for file_name, reason in sorted(parse_errors.items()):
            log(f"transcript_parse_error file={file_name} detail={reason}")

    log(
        "counts "
        f"corpus_memories_after_dedupe={corpus_stats['memories_after_dedupe']} "
        f"dupes={len(corpus_stats['dupes'])} "
        f"summary_fallbacks={corpus_stats['summary_fallbacks']}"
    )
    log(
        "counts "
        f"edges_kept={edge_summary['n_edges']} "
        f"related_with_seeded_edge={edge_summary['n_related_with_seeded_edge']} "
        f"edge_fp={set_fingerprint(set(edge_map))}"
    )

    log("smoke_shortlist_begin")
    for instance_id, repo, version, experiences in shortlist:
        log(f"{instance_id}, {repo}, {version}, {experiences}")
    log("smoke_shortlist_end")

    log(
        "artifacts "
        f"heldout={heldout_output} corpus={corpus_output} edge_map={edge_map_output} "
        f"edge_summary={edge_map_summary_output}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
