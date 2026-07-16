from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import pytest

from wevibe_bench.recall_gold import (
    CATEGORIES,
    NO_INJECTION_CATEGORIES,
    GoldError,
    ResolveError,
    load_checkpoint,
    load_corpus,
    load_gold,
    resolve,
    resolve_from_files,
    validate_gold_against_corpus,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = REPO_ROOT / "recall" / "corpus" / "go-concurrency-v1.json"
GOLD_PATH = REPO_ROOT / "recall" / "gold" / "go-concurrency-v1.gold.jsonl"


def _load_shipped_fixture() -> tuple:
    corpus = load_corpus(CORPUS_PATH)
    gold = load_gold(GOLD_PATH)
    return corpus, gold


def _required_slugs(gold: list) -> list[str]:
    return sorted({slug for case in gold for slug in case.expected_slugs})


def _fake_slug_to_hash(slugs: list[str], *, prefix: str) -> dict[str, str]:
    return {slug: f"{prefix}-{index:02d}" for index, slug in enumerate(sorted(slugs), start=1)}


def _write_checkpoint(
    path: Path,
    slug_to_hash: dict[str, str],
    *,
    org_id: str = "org-test",
    topic: str = "go-concurrency",
    updated_at: str = "2026-07-16T04:34:00Z",
) -> None:
    committed = [
        {"idx": idx, "id": slug, "submission_hash": cid}
        for idx, (slug, cid) in enumerate(sorted(slug_to_hash.items()), start=1)
    ]
    payload = {
        "org_id": org_id,
        "topic": topic,
        "total": len(committed),
        "updated_at": updated_at,
        "committed": committed,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_shipped_fixture_schema_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    corpus = load_corpus(CORPUS_PATH)
    gold = load_gold(GOLD_PATH)
    validate_gold_against_corpus(gold, corpus)


def test_category_and_count_coverage() -> None:
    _, gold = _load_shipped_fixture()

    counts: dict[str, int] = {category: 0 for category in CATEGORIES}
    for case in gold:
        counts[case.category] += 1

    assert set(counts.keys()) == set(CATEGORIES)
    assert all(counts[category] > 0 for category in CATEGORIES)
    assert counts["single_hit"] == 12
    assert counts["near_tie"] == 2
    assert counts["cross_stack_negative"] == 5
    assert counts["thin_prompt"] == 2
    assert counts["no_match"] == 2
    assert len(gold) >= 20


def test_stable_slug_and_caseid_uniqueness() -> None:
    corpus, gold = _load_shipped_fixture()

    assert len(corpus.slugs) == 12

    case_ids = [case.case_id for case in gold]
    assert len(case_ids) == len(set(case_ids))

    required = {slug for case in gold for slug in case.expected_slugs}
    assert required == corpus.slugs

    for case in gold:
        for slug in case.expected_slugs:
            assert slug in corpus.slugs


def test_no_injection_category_invariants() -> None:
    _, gold = _load_shipped_fixture()

    for case in gold:
        if case.category in NO_INJECTION_CATEGORIES:
            assert list(case.expected_slugs) == []
            assert case.expect_injection is False
        else:
            assert list(case.expected_slugs) != []
            assert case.expect_injection is True


def test_resolver_deterministic(tmp_path: Path) -> None:
    corpus, gold = _load_shipped_fixture()
    required = _required_slugs(gold)
    assert set(required) == corpus.slugs

    checkpoint_path = tmp_path / "checkpoint.det.json"
    _write_checkpoint(checkpoint_path, _fake_slug_to_hash(required, prefix="cid-det"))
    checkpoint_map = load_checkpoint(checkpoint_path)

    resolved_a = resolve(
        gold,
        corpus,
        checkpoint_map,
        run_id="run-deterministic",
        checkpoint_path=checkpoint_path,
    )
    resolved_b = resolve(
        gold,
        corpus,
        checkpoint_map,
        run_id="run-deterministic",
        checkpoint_path=checkpoint_path,
    )

    serialized_a = json.dumps(resolved_a, sort_keys=True)
    serialized_b = json.dumps(resolved_b, sort_keys=True)
    assert serialized_a == serialized_b
    assert list(resolved_a["cases"].keys()) == sorted(resolved_a["cases"].keys())


def test_resolver_fail_loud_missing_slug(tmp_path: Path) -> None:
    corpus, gold = _load_shipped_fixture()
    required = _required_slugs(gold)
    assert set(required) == corpus.slugs

    missing_slug = required[0]
    partial_mapping = _fake_slug_to_hash([slug for slug in required if slug != missing_slug], prefix="cid-miss")

    checkpoint_path = tmp_path / "checkpoint.missing.json"
    _write_checkpoint(checkpoint_path, partial_mapping)
    checkpoint_map = load_checkpoint(checkpoint_path)

    with pytest.raises(ResolveError) as excinfo:
        resolve(
            gold,
            corpus,
            checkpoint_map,
            run_id="run-missing",
            checkpoint_path=checkpoint_path,
        )
    msg = str(excinfo.value)
    assert "unresolved" in msg
    assert missing_slug in msg


def test_resolver_fail_loud_duplicate_checkpoint_id(tmp_path: Path) -> None:
    duplicate_slug = "gc_goroutine_leak_unbuffered_send"
    checkpoint_path = tmp_path / "checkpoint.duplicate.json"
    checkpoint_payload = {
        "org_id": "org-test",
        "topic": "go-concurrency",
        "total": 2,
        "committed": [
            {"idx": 1, "id": duplicate_slug, "submission_hash": "cid-1"},
            {"idx": 2, "id": duplicate_slug, "submission_hash": "cid-2"},
        ],
    }
    checkpoint_path.write_text(json.dumps(checkpoint_payload, indent=2), encoding="utf-8")

    with pytest.raises(ResolveError) as excinfo:
        load_checkpoint(checkpoint_path)
    assert duplicate_slug in str(excinfo.value)


def test_validate_fail_loud_unknown_gold_slug(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.json"
    corpus_payload = {
        "topic": "tiny-topic",
        "memories": [
            {
                "id": "known_slug",
                "text": "tiny memory",
                "keywords": ["tiny"],
                "stack_hint": "go",
            }
        ],
    }
    corpus_path.write_text(json.dumps(corpus_payload, indent=2), encoding="utf-8")

    unknown_slug = "unknown_slug"
    gold_path = tmp_path / "gold.jsonl"
    gold_case = {
        "case_id": "case-unknown",
        "category": "single_hit",
        "query": "should fail validation",
        "expected_slugs": [unknown_slug],
        "expect_injection": True,
        "session": {},
    }
    gold_path.write_text(json.dumps(gold_case) + "\n", encoding="utf-8")

    corpus = load_corpus(corpus_path)
    gold = load_gold(gold_path)
    with pytest.raises(GoldError) as excinfo:
        validate_gold_against_corpus(gold, corpus)
    msg = str(excinfo.value)
    assert "unknown slug" in msg
    assert unknown_slug in msg


def test_reseed_cid_churn_tolerance(tmp_path: Path) -> None:
    corpus, gold = _load_shipped_fixture()
    required = _required_slugs(gold)
    assert set(required) == corpus.slugs

    slug_to_hash_a = _fake_slug_to_hash(required, prefix="seed-a")
    slug_to_hash_b = _fake_slug_to_hash(required, prefix="seed-b")

    checkpoint_a = tmp_path / "checkpoint.seed-a.json"
    checkpoint_b = tmp_path / "checkpoint.seed-b.json"
    _write_checkpoint(checkpoint_a, slug_to_hash_a, org_id="org-a")
    _write_checkpoint(checkpoint_b, slug_to_hash_b, org_id="org-b")

    map_a = load_checkpoint(checkpoint_a)
    map_b = load_checkpoint(checkpoint_b)

    resolved_a = resolve(gold, corpus, map_a, run_id="run-seed-a", checkpoint_path=checkpoint_a)
    resolved_b = resolve(gold, corpus, map_b, run_id="run-seed-b", checkpoint_path=checkpoint_b)

    for case in gold:
        expected_a = [map_a[slug] for slug in case.expected_slugs] if case.expect_injection else []
        expected_b = [map_b[slug] for slug in case.expected_slugs] if case.expect_injection else []
        assert resolved_a["cases"][case.case_id]["resolved_cids"] == expected_a
        assert resolved_b["cases"][case.case_id]["resolved_cids"] == expected_b

    assert any(
        resolved_a["cases"][case.case_id]["resolved_cids"]
        != resolved_b["cases"][case.case_id]["resolved_cids"]
        for case in gold
        if case.expect_injection
    )


def test_no_match_and_cross_stack_resolve_empty(tmp_path: Path) -> None:
    corpus, gold = _load_shipped_fixture()
    required = _required_slugs(gold)
    assert set(required) == corpus.slugs

    checkpoint_path = tmp_path / "checkpoint.full.json"
    _write_checkpoint(checkpoint_path, _fake_slug_to_hash(required, prefix="cid-full"))
    checkpoint_map = load_checkpoint(checkpoint_path)

    resolved = resolve(
        gold,
        corpus,
        checkpoint_map,
        run_id="run-empty-categories",
        checkpoint_path=checkpoint_path,
    )

    negative_cases = [case for case in gold if case.category in NO_INJECTION_CATEGORIES]
    assert negative_cases

    for case in negative_cases:
        assert resolved["cases"][case.case_id]["resolved_cids"] == []


def test_gold_physically_isolated_from_worker() -> None:
    recall_dir = (REPO_ROOT / "recall").resolve()
    corpus_path = CORPUS_PATH.resolve()
    gold_path = GOLD_PATH.resolve()
    worker_context = (REPO_ROOT / "docker" / "worker").resolve()

    assert recall_dir.is_dir()
    assert corpus_path.is_file()
    assert gold_path.is_file()

    for path in (recall_dir, corpus_path, gold_path):
        assert worker_context not in (path, *path.parents)
        assert "docker/worker" not in path.as_posix()

        lowered_parts = {part.lower() for part in path.parts}
        assert "scaffold" not in lowered_parts
        assert "worktree" not in lowered_parts
        assert "work" not in lowered_parts


def test_resolve_logs_content_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    corpus_payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    gold_payload = [json.loads(line) for line in GOLD_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]

    memory_snippet = corpus_payload["memories"][0]["text"][:40]
    query_snippet = gold_payload[0]["query"][:40]

    corpus, gold = _load_shipped_fixture()
    required = _required_slugs(gold)
    slug_to_hash = _fake_slug_to_hash(required, prefix="cid-log")
    sentinel_hash = "FAKE_SUBMISSION_HASH_SENTINEL_1234567890"
    slug_to_hash[required[0]] = sentinel_hash

    checkpoint_path = tmp_path / "checkpoint.logs.json"
    _write_checkpoint(checkpoint_path, slug_to_hash)
    out_path = tmp_path / "runs" / "resolved.json"

    logger = logging.getLogger("wevibe_bench.recall_gold")
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        resolve_from_files(
            GOLD_PATH,
            CORPUS_PATH,
            checkpoint_path,
            run_id="run-log-content-free",
            out_path=out_path,
        )
    finally:
        logger.handlers.clear()
        logger.handlers.extend(previous_handlers)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    logs = stream.getvalue()
    assert "op=recall.gold.resolve_from_files" in logs
    assert memory_snippet not in logs
    assert query_snippet not in logs
    assert sentinel_hash not in logs


def test_shipped_fixture_secret_safe() -> None:
    corpus_raw = CORPUS_PATH.read_text(encoding="utf-8")
    gold_raw = GOLD_PATH.read_text(encoding="utf-8")
    combined = f"{corpus_raw}\n{gold_raw}"
    combined_lower = combined.lower()

    forbidden_substrings = [
        "epoch_sk",
        "kfrag",
        "cfrag",
        "dek",
        "private_key",
        "api_key",
        "begin",
        "/users/",
        "jerrysmith",
        "opencode",
        "walter",
        "claude",
        "opus",
        "glm-",
    ]
    for needle in forbidden_substrings:
        assert needle not in combined_lower

    assert re.search(r"\b[a-fA-F0-9]{32,}\b", combined) is None
    assert re.search(r"\b(?=[A-Za-z0-9+/]{32,}={0,2}\b)(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{32,}={0,2}\b", combined) is None
