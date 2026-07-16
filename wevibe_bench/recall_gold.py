"""Gold fixture validation + seed-checkpoint slug→CID resolution."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias

CATEGORIES = ("single_hit", "near_tie", "cross_stack_negative", "thin_prompt", "no_match")
NO_INJECTION_CATEGORIES = ("cross_stack_negative", "no_match")

_RUN_SCOPED_NOTE = "RUN-SCOPED resolved CIDs — NOT committed stable labels; regenerate every seed"
_FALLBACK_ISO = "1970-01-01T00:00:00Z"

PathLike: TypeAlias = str | os.PathLike[str]
ResolvedMapping: TypeAlias = dict[str, Any]

_LOG = logging.getLogger("wevibe_bench.recall_gold")


class GoldError(Exception):
    pass


class ResolveError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Corpus:
    topic: str
    slugs: set[str]


@dataclass(frozen=True, slots=True)
class GoldCase:
    case_id: str
    category: str
    query: str
    expected_slugs: tuple[str, ...]
    expect_injection: bool
    session: dict[str, Any]
    notes: Any | None = None


def _as_path(path: PathLike) -> Path:
    return Path(path).expanduser()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json_object(path: Path, *, kind: str, exc_type: type[Exception]) -> dict[str, Any]:
    if not path.is_file():
        raise exc_type(f"{kind} file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise exc_type(f"{kind} file is empty: {path}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise exc_type(f"{kind} file is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise exc_type(f"{kind} JSON must decode to an object: {path}")
    return payload


def _required_slug_set(gold: list[GoldCase]) -> set[str]:
    return {slug for case in gold for slug in case.expected_slugs}


def _category_counts(gold: list[GoldCase]) -> dict[str, int]:
    counts = {category: 0 for category in CATEGORIES}
    for case in gold:
        counts[case.category] += 1
    return counts


def _checkpoint_payload(path: PathLike) -> dict[str, Any]:
    return _load_json_object(_as_path(path), kind="checkpoint", exc_type=ResolveError)


def _checkpoint_org_and_timestamp(path: PathLike) -> tuple[str, str]:
    payload = _checkpoint_payload(path)
    org_id = payload.get("org_id", "")
    if org_id is None:
        org = ""
    elif isinstance(org_id, str):
        org = org_id.strip()
    else:
        raise ResolveError(f"checkpoint org_id must be a string when present: {_as_path(path)}")

    updated_at = payload.get("updated_at")
    if isinstance(updated_at, str) and updated_at.strip():
        return org, updated_at.strip()
    created_at = payload.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        return org, created_at.strip()
    return org, _FALLBACK_ISO


def load_corpus(path: PathLike) -> Corpus:
    corpus_path = _as_path(path)
    payload = _load_json_object(corpus_path, kind="corpus", exc_type=GoldError)

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise GoldError(f"corpus missing non-empty string 'topic': {corpus_path}")

    raw_memories = payload.get("memories")
    if not isinstance(raw_memories, list) or not raw_memories:
        raise GoldError(f"corpus must contain a non-empty 'memories' array: {corpus_path}")

    slugs: set[str] = set()
    for index, memory in enumerate(raw_memories, start=1):
        if not isinstance(memory, dict):
            raise GoldError(f"corpus memory #{index} must be an object: {corpus_path}")
        raw_slug = memory.get("id")
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            raise GoldError(f"corpus memory #{index} missing non-empty 'id': {corpus_path}")
        slug = raw_slug.strip()
        if slug in slugs:
            raise GoldError(f"corpus has duplicate memory id={slug!r}: {corpus_path}")
        slugs.add(slug)

    return Corpus(topic=topic.strip(), slugs=slugs)


def load_gold(path: PathLike) -> list[GoldCase]:
    gold_path = _as_path(path)
    if not gold_path.is_file():
        raise GoldError(f"gold file not found: {gold_path}")

    lines = gold_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise GoldError(f"gold file is empty: {gold_path}")

    cases: list[GoldCase] = []
    seen_case_ids: set[str] = set()

    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            raise GoldError(f"gold line {line_no} is blank: {gold_path}")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoldError(f"gold line {line_no} is invalid JSON: {gold_path}") from exc
        if not isinstance(payload, dict):
            raise GoldError(f"gold line {line_no} must decode to an object: {gold_path}")

        case_id = payload.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise GoldError(f"gold line {line_no} missing non-empty 'case_id': {gold_path}")
        case_id = case_id.strip()
        if case_id in seen_case_ids:
            raise GoldError(f"gold duplicate case_id={case_id!r}: {gold_path}")

        category = payload.get("category")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise GoldError(
                f"gold case {case_id!r} has invalid category={category!r}; expected one of {CATEGORIES}"
            )

        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise GoldError(f"gold case {case_id!r} missing non-empty 'query'")

        raw_expected_slugs = payload.get("expected_slugs")
        if not isinstance(raw_expected_slugs, list):
            raise GoldError(f"gold case {case_id!r} requires array field 'expected_slugs'")

        cleaned_expected_slugs: list[str] = []
        seen_slugs: set[str] = set()
        for slug_index, raw_slug in enumerate(raw_expected_slugs, start=1):
            if not isinstance(raw_slug, str) or not raw_slug.strip():
                raise GoldError(
                    f"gold case {case_id!r} has empty/non-string expected_slugs[{slug_index}]"
                )
            slug = raw_slug.strip()
            if slug in seen_slugs:
                raise GoldError(f"gold case {case_id!r} duplicates expected slug {slug!r}")
            seen_slugs.add(slug)
            cleaned_expected_slugs.append(slug)

        expect_injection = payload.get("expect_injection")
        if not isinstance(expect_injection, bool):
            raise GoldError(f"gold case {case_id!r} requires boolean 'expect_injection'")

        session = payload.get("session")
        if not isinstance(session, dict):
            raise GoldError(f"gold case {case_id!r} requires object field 'session'")

        if category in NO_INJECTION_CATEGORIES:
            if cleaned_expected_slugs:
                raise GoldError(
                    f"gold case {case_id!r} category={category!r} must have expected_slugs=[]"
                )
            if expect_injection:
                raise GoldError(
                    f"gold case {case_id!r} category={category!r} must set expect_injection=false"
                )
        else:
            if not cleaned_expected_slugs:
                raise GoldError(
                    f"gold case {case_id!r} category={category!r} must have non-empty expected_slugs"
                )
            if not expect_injection:
                raise GoldError(
                    f"gold case {case_id!r} category={category!r} must set expect_injection=true"
                )

        seen_case_ids.add(case_id)
        cases.append(
            GoldCase(
                case_id=case_id,
                category=category,
                query=query.strip(),
                expected_slugs=tuple(cleaned_expected_slugs),
                expect_injection=expect_injection,
                session=session,
                notes=payload.get("notes"),
            )
        )

    return cases


def validate_gold_against_corpus(gold: list[GoldCase], corpus: Corpus) -> None:
    for case in gold:
        for slug in case.expected_slugs:
            if slug not in corpus.slugs:
                raise GoldError(f"gold case {case.case_id!r} references unknown slug {slug!r}")


def load_checkpoint(path: PathLike) -> dict[str, str]:
    checkpoint_path = _as_path(path)
    payload = _checkpoint_payload(checkpoint_path)
    raw_committed = payload.get("committed")
    if not isinstance(raw_committed, list):
        raise ResolveError(f"checkpoint missing array field 'committed': {checkpoint_path}")

    mapping: dict[str, str] = {}
    for idx, entry in enumerate(raw_committed, start=1):
        if not isinstance(entry, dict):
            raise ResolveError(f"checkpoint committed[{idx}] must be an object: {checkpoint_path}")

        raw_slug = entry.get("id")
        raw_cid = entry.get("submission_hash")
        if not isinstance(raw_slug, str) or not raw_slug.strip() or not isinstance(raw_cid, str) or not raw_cid.strip():
            raise ResolveError(
                f"checkpoint committed[{idx}] requires non-empty 'id' and 'submission_hash': {checkpoint_path}"
            )

        slug = raw_slug.strip()
        cid = raw_cid.strip()
        if slug in mapping:
            raise ResolveError(f"checkpoint duplicates committed id={slug!r}: {checkpoint_path}")
        mapping[slug] = cid

    return mapping


def resolve(
    gold: list[GoldCase],
    corpus: Corpus,
    checkpoint_map: dict[str, str],
    *,
    run_id: str,
    checkpoint_path: PathLike | None = None,
) -> ResolvedMapping:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ResolveError("run_id must be a non-empty string")

    validate_gold_against_corpus(gold, corpus)
    required_slugs = sorted(_required_slug_set(gold))
    unresolved = sorted(slug for slug in required_slugs if slug not in checkpoint_map)
    if unresolved:
        raise ResolveError(f"unresolved slug(s): {', '.join(unresolved)}")

    checkpoint_path_str = str(_as_path(checkpoint_path)) if checkpoint_path is not None else None
    if checkpoint_path is None:
        org = ""
        resolved_at = _utc_now_iso()
    else:
        org, resolved_at = _checkpoint_org_and_timestamp(checkpoint_path)

    cases: dict[str, dict[str, Any]] = {}
    for case in sorted(gold, key=lambda item: item.case_id):
        expected = list(case.expected_slugs)
        resolved_cids = [] if not case.expect_injection else [checkpoint_map[slug] for slug in expected]
        cases[case.case_id] = {
            "category": case.category,
            "expected_slugs": expected,
            "resolved_cids": resolved_cids,
            "expect_injection": case.expect_injection,
        }

    return {
        "run_id": run_id.strip(),
        "resolved_at": resolved_at,
        "checkpoint_path": checkpoint_path_str,
        "org": org,
        "note": _RUN_SCOPED_NOTE,
        "cases": cases,
    }


def write_resolved_mapping(mapping: ResolvedMapping, out_path: PathLike) -> None:
    target = _as_path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(mapping, indent=2, sort_keys=True)
    tmp_path = target.parent / f".{target.name}.tmp-{os.getpid()}"
    tmp_path.write_text(f"{payload}\n", encoding="utf-8")
    os.replace(tmp_path, target)


def resolve_from_files(
    gold_path: PathLike,
    corpus_path: PathLike,
    checkpoint_path: PathLike,
    *,
    run_id: str,
    out_path: PathLike,
) -> ResolvedMapping:
    n_cases = 0
    counts = {category: 0 for category in CATEGORIES}
    n_required_slugs = 0
    n_resolved = 0
    n_unresolved = 0
    status = "failed"

    try:
        corpus = load_corpus(corpus_path)
        gold = load_gold(gold_path)
        validate_gold_against_corpus(gold, corpus)
        checkpoint_map = load_checkpoint(checkpoint_path)

        n_cases = len(gold)
        counts = _category_counts(gold)

        required_slugs = sorted(_required_slug_set(gold))
        unresolved_slugs = sorted(slug for slug in required_slugs if slug not in checkpoint_map)

        n_required_slugs = len(required_slugs)
        n_unresolved = len(unresolved_slugs)
        n_resolved = n_required_slugs - n_unresolved

        if unresolved_slugs:
            raise ResolveError(f"unresolved slug(s): {', '.join(unresolved_slugs)}")

        mapping = resolve(
            gold,
            corpus,
            checkpoint_map,
            run_id=run_id,
            checkpoint_path=checkpoint_path,
        )
        write_resolved_mapping(mapping, out_path)
        status = "ok"
        return mapping
    finally:
        _LOG.info(
            (
                "op=recall.gold.resolve_from_files status=%s n_cases=%s single_hit=%s near_tie=%s "
                "cross_stack_negative=%s thin_prompt=%s no_match=%s "
                "n_required_slugs=%s n_resolved=%s n_unresolved=%s"
            ),
            status,
            n_cases,
            counts["single_hit"],
            counts["near_tie"],
            counts["cross_stack_negative"],
            counts["thin_prompt"],
            counts["no_match"],
            n_required_slugs,
            n_resolved,
            n_unresolved,
        )
