"""Tests for the session-DB integrity gate (WO-DBVOL-1).

The gate exists because SQLite corruption is PARTIAL: the 2026-08-11 session DB
answered ``SELECT count(*) FROM part`` with 492 while ``PRAGMA quick_check``
reported "database disk image is malformed". An unverified substrate therefore
does not fail extraction — it silently returns FEWER memories and the ON arm
publishes a plausible number that is simply too low.

These tests build genuinely corrupt SQLite files (real byte damage to the page
store, not mocks) so the gate is proven against the actual failure mode.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wevibe_bench.session_db_integrity import (
    SessionDbCorrupt,
    require_sound_session_db,
    verify_session_db,
)


def _make_session_db(path: Path, *, messages: int = 20, parts_per: int = 6) -> None:
    """Build a structurally realistic opencode-shaped session DB."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, data TEXT);")
        conn.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);"
        )
        conn.execute(
            "CREATE TABLE part ("
            "id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT);"
        )
        conn.execute("INSERT INTO session VALUES ('ses_1', '{}');")
        for m in range(messages):
            mid = f"msg_{m:04d}"
            conn.execute(
                "INSERT INTO message VALUES (?, 'ses_1', ?);", (mid, "{}" * 40)
            )
            for p in range(parts_per):
                conn.execute(
                    "INSERT INTO part VALUES (?, ?, 'ses_1', 0, 0, ?);",
                    (f"prt_{m:04d}_{p:02d}", mid, "x" * 900),
                )
        conn.commit()
    finally:
        conn.close()


def _corrupt_pages(path: Path, *, start_page: int = 6, pages: int = 5) -> None:
    """Overwrite interior B-tree pages with garbage — real, not simulated."""
    page_size = 4096
    with open(path, "r+b") as fh:
        fh.seek(start_page * page_size)
        fh.write(b"\xde\xad\xbe\xef" * (page_size * pages // 4))


def test_sound_db_passes(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _make_session_db(db)

    report = verify_session_db(db)

    assert report.ok is True
    assert report.detail == "ok"
    assert report.message_rows == 20
    assert report.part_rows == 120


def test_corrupt_db_is_rejected(tmp_path: Path) -> None:
    """The real defect: byte-damaged pages must be caught, not counted."""
    db = tmp_path / "opencode.db"
    _make_session_db(db, messages=60, parts_per=8)
    _corrupt_pages(db)

    report = verify_session_db(db)

    assert report.ok is False, (
        "a corrupt DB must never be accepted as an extraction substrate"
    )


def test_corrupt_db_raises_at_the_extraction_gate(tmp_path: Path) -> None:
    """require_sound_session_db is fail-closed — it voids, never degrades."""
    db = tmp_path / "opencode.db"
    _make_session_db(db, messages=60, parts_per=8)
    _corrupt_pages(db)

    with pytest.raises(SessionDbCorrupt) as excinfo:
        require_sound_session_db(db)

    assert "under-report" in str(excinfo.value), (
        "the error must name the real hazard: silent under-extraction"
    )


def test_missing_db_is_rejected(tmp_path: Path) -> None:
    report = verify_session_db(tmp_path / "nope.db")
    assert report.ok is False
    assert "missing" in report.detail


def test_truncated_db_with_no_tables_is_rejected(tmp_path: Path) -> None:
    """An empty-but-valid DB would otherwise read as 'zero memories extracted'."""
    db = tmp_path / "opencode.db"
    sqlite3.connect(str(db)).close()

    report = verify_session_db(db)

    assert report.ok is False
    assert "missing tables" in report.detail


def test_non_database_file_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    db.write_bytes(b"this is not a sqlite database" * 100)

    report = verify_session_db(db)

    assert report.ok is False


def test_verification_never_mutates_the_artifact(tmp_path: Path) -> None:
    """Opened immutable+ro: verification must not write, WAL, or recover.

    A check that mutates the artifact it inspects destroys the evidence needed
    to diagnose the failure it just found.
    """
    db = tmp_path / "opencode.db"
    _make_session_db(db)
    before = db.read_bytes()

    verify_session_db(db)

    assert db.read_bytes() == before, "integrity check must be read-only"
    assert not (tmp_path / "opencode.db-wal").exists()
    assert not (tmp_path / "opencode.db-shm").exists()
