import hashlib
import json
import os

import pytest

from wevibe_bench.cumulative.catalog import (
    CatalogRecord,
    PrivateCatalog,
    SafeDecisionLedger,
    reconcile,
)
from wevibe_bench.cumulative.decision import VERIFY


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(
    *,
    submission_hash: str,
    committed_id: str | None,
    text: str,
    keywords: list[str],
    org_id: str,
    sequence_index: int,
    committing_identity: str,
) -> CatalogRecord:
    return CatalogRecord(
        submission_hash=submission_hash,
        committed_id=committed_id,
        keywords=keywords,
        comparison_text=text,
        org_id=org_id,
        sequence_index=sequence_index,
        committing_identity=committing_identity,
        content_hash=_sha256_text(text),
        committed_at="2026-07-23T12:00:00Z",
    )


def test_private_catalog_creates_0600_and_load_round_trip(tmp_path) -> None:
    catalog_path = tmp_path / "private.catalog.jsonl"
    catalog = PrivateCatalog(str(catalog_path))

    first = _record(
        submission_hash="sub-0001",
        committed_id="cid-0001",
        text="alpha synthetic memory",
        keywords=["alpha", "memory"],
        org_id="org-catalog",
        sequence_index=0,
        committing_identity="leader-a",
    )
    second = _record(
        submission_hash="sub-0002",
        committed_id="cid-0002",
        text="beta synthetic memory",
        keywords=["beta", "memory"],
        org_id="org-catalog",
        sequence_index=1,
        committing_identity="leader-a",
    )

    catalog.append(first)
    assert catalog_path.exists()
    assert oct(os.stat(catalog_path).st_mode & 0o777) == "0o600"

    catalog.append(second)
    assert catalog.load() == [first, second]


def test_find_duplicates_matches_hash_submission_and_keyword_overlap(tmp_path) -> None:
    catalog = PrivateCatalog(str(tmp_path / "private.catalog.jsonl"))

    exact_text = "exact-content-match"
    by_hash = _record(
        submission_hash="sub-hash-only",
        committed_id="cid-hash",
        text=exact_text,
        keywords=["irrelevant"],
        org_id="org-catalog",
        sequence_index=0,
        committing_identity="leader-a",
    )
    by_submission = _record(
        submission_hash="sub-shared",
        committed_id="cid-submission",
        text="different-content",
        keywords=["none"],
        org_id="org-catalog",
        sequence_index=1,
        committing_identity="leader-a",
    )
    by_keywords = _record(
        submission_hash="sub-keyword",
        committed_id="cid-keyword",
        text="keyword-only-match",
        keywords=["alpha", "beta"],
        org_id="org-catalog",
        sequence_index=2,
        committing_identity="leader-a",
    )
    for record in (by_hash, by_submission, by_keywords):
        catalog.append(record)

    candidate = {
        "submission_hash": "sub-shared",
        "text": exact_text,
        "keywords": ["alpha", "beta", "gamma"],
    }

    matches = catalog.find_duplicates(candidate)
    matched_refs = {record.submission_hash for record in matches}

    assert matched_refs == {
        by_hash.submission_hash,
        by_submission.submission_hash,
        by_keywords.submission_hash,
    }


def test_reconcile_reports_bidirectional_gaps_and_identity_mismatch() -> None:
    orphan = _record(
        submission_hash="sub-orphan",
        committed_id="cid-orphan",
        text="orphan-memory",
        keywords=["orphan"],
        org_id="org-catalog",
        sequence_index=0,
        committing_identity="catalog-identity",
    )
    mismatch = _record(
        submission_hash="sub-match",
        committed_id="cid-match",
        text="matched-memory",
        keywords=["match"],
        org_id="org-catalog",
        sequence_index=1,
        committing_identity="catalog-identity",
    )

    authoritative_inventory = [
        {
            "committed_id": "cid-match",
            "content_hash": mismatch.content_hash,
            "committing_identity": "chain-identity",
        },
        {
            "committed_id": "cid-chain-only",
            "content_hash": "hash-chain-only",
            "committing_identity": "chain-only-identity",
        },
    ]

    report = reconcile([orphan, mismatch], authoritative_inventory)

    assert report["in_catalog_not_chain"] == [
        {
            "submission_hash": "sub-orphan",
            "committed_id": "cid-orphan",
            "content_hash": orphan.content_hash,
            "org_id": "org-catalog",
            "sequence_index": 0,
            "committing_identity": "catalog-identity",
            "keyword_count": 1,
            "committed_at": "2026-07-23T12:00:00Z",
            "status": "orphan",
        }
    ]

    assert report["in_chain_not_catalog"] == [
        {
            "committed_id": "cid-chain-only",
            "content_hash": "hash-chain-only",
            "committing_identity": "chain-only-identity",
            "status": "content_unavailable",
        }
    ]

    assert report["identity_mismatch"] == [
        {
            "committed_id": "cid-match",
            "content_hash": mismatch.content_hash,
            "catalog_committing_identity": "catalog-identity",
            "authoritative_committing_identity": "chain-identity",
        }
    ]


def test_safe_decision_ledger_rejects_plaintext_keys_and_writes_hashes_only(tmp_path) -> None:
    ledger_path = tmp_path / "safe-ledger.jsonl"
    forbidden_plaintext = "PLAINTEXT_SHOULD_NEVER_LAND"

    with pytest.raises(ValueError, match="forbidden plaintext key"):
        SafeDecisionLedger.append_entry(
            str(ledger_path),
            {
                "sequence_index": 0,
                "submission_hash": "sub-plaintext",
                "verdict": VERIFY,
                "reason": "should fail",
                "keyword_count": 1,
                "content_hash": _sha256_text("does-not-matter"),
                "committing_identity": "leader-a",
                "comparison_text": forbidden_plaintext,
            },
        )

    SafeDecisionLedger.append_entry(
        str(ledger_path),
        {
            "sequence_index": 1,
            "submission_hash": "sub-hash-only",
            "committed_id": "cid-hash-only",
            "cid": "cid-hash-only",
            "verdict": VERIFY,
            "reason": "hashes-only-entry",
            "keyword_count": 2,
            "content_hash": _sha256_text("hash-only"),
            "duplicate_refs": ["dup-a", "dup-b"],
            "committing_identity": "leader-a",
            "counts": {"duplicate_refs": 2},
        },
    )

    ledger_bytes = ledger_path.read_bytes()
    assert forbidden_plaintext.encode("utf-8") not in ledger_bytes
    assert b"comparison_text" not in ledger_bytes
    assert b'"text"' not in ledger_bytes
    assert b"plaintext" not in ledger_bytes

    parsed_lines = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert len(parsed_lines) == 1
