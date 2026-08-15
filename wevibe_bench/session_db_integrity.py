"""Integrity verification for an exported opencode session database.

WO-DBVOL-1 (2026-08-11). The session DB is the SOLE substrate for extraction:
the extraction stage reads memories out of it, and the ON arm's entire result
is derived from what that read returns. Before this module the only check was
``session_db_path.is_file()``.

That gap is the most dangerous failure mode in the harness, because SQLite
corruption is PARTIAL. On the preserved DB of the 2026-08-11 cell:

    PRAGMA integrity_check  -> "database disk image is malformed"
    SELECT count(*) FROM part -> 492      (answers fine)

A damaged B-tree page raises only when a query happens to touch it. So a
corrupt DB does not fail extraction — it silently returns FEWER memories, and
the ON arm reports a real-looking number that is simply too low. A benchmark
others plug into must never do that: no number is recoverable, a wrong number
is not.

The check therefore runs BEFORE extraction and is fail-closed. It is read-only
and opens the DB immutably, so verification can never itself mutate or further
damage the artifact under inspection.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


# The tables extraction actually depends on. `part` is where the 2026-08-11
# corruption landed and is the table holding message content.
_REQUIRED_TABLES = ("session", "message", "part")


class SessionDbCorrupt(RuntimeError):
    """Raised when a session DB cannot be trusted as an extraction substrate."""


@dataclass(frozen=True)
class IntegrityReport:
    """Outcome of verifying one session DB."""

    path: str
    ok: bool
    detail: str
    part_rows: int | None = None
    message_rows: int | None = None

    def summary(self) -> str:
        return (
            f"session_db_integrity path={self.path} ok={str(self.ok).lower()} "
            f"messages={self.message_rows} parts={self.part_rows} detail={self.detail}"
        )


def _immutable_uri(path: Path) -> str:
    # immutable=1 implies read-only AND tells SQLite the file will not change,
    # so verification never writes, never creates a WAL, and never recovers a
    # hot journal on the artifact it is inspecting.
    return f"file:{path}?immutable=1&mode=ro"


def verify_session_db(path: str | Path) -> IntegrityReport:
    """Verify ``path`` is a readable, structurally sound extraction substrate.

    Returns an :class:`IntegrityReport`; never raises for a corrupt DB (the
    caller decides). Only a missing file is a hard input error.
    """
    db_path = Path(path).expanduser().resolve()
    if not db_path.is_file():
        return IntegrityReport(
            path=str(db_path), ok=False, detail="missing: no such file"
        )

    try:
        conn = sqlite3.connect(_immutable_uri(db_path), uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return IntegrityReport(
            path=str(db_path), ok=False, detail=f"unopenable: {exc}"
        )

    try:
        # quick_check catches the malformed-page class (the observed defect)
        # without integrity_check's full-index cost on a multi-MB DB.
        try:
            rows = conn.execute("PRAGMA quick_check;").fetchall()
        except sqlite3.DatabaseError as exc:
            return IntegrityReport(
                path=str(db_path), ok=False, detail=f"quick_check raised: {exc}"
            )
        verdict = str(rows[0][0]).strip().lower() if rows else ""
        if verdict != "ok":
            first = "; ".join(str(r[0]) for r in rows[:3])
            return IntegrityReport(
                path=str(db_path), ok=False, detail=f"malformed: {first}"
            )

        # quick_check can pass while a required table is absent (a truncated or
        # never-initialised DB), which would read downstream as "zero memories".
        present = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        missing = [t for t in _REQUIRED_TABLES if t not in present]
        if missing:
            return IntegrityReport(
                path=str(db_path),
                ok=False,
                detail=f"missing tables: {','.join(missing)}",
            )

        # Full table scans force SQLite to actually visit every page of the
        # tables extraction reads. quick_check alone does not guarantee each
        # row is retrievable; this is what turns a latent partial corruption
        # into a loud failure BEFORE it becomes an under-count.
        counts: dict[str, int] = {}
        for table in ("message", "part"):
            try:
                counts[table] = int(
                    conn.execute(f"SELECT count(*) FROM {table};").fetchone()[0]
                )
            except sqlite3.DatabaseError as exc:
                return IntegrityReport(
                    path=str(db_path),
                    ok=False,
                    detail=f"unreadable table {table}: {exc}",
                )
        try:
            conn.execute("SELECT id, message_id, data FROM part;").fetchall()
        except sqlite3.DatabaseError as exc:
            return IntegrityReport(
                path=str(db_path),
                ok=False,
                detail=f"part scan failed: {exc}",
                part_rows=counts.get("part"),
                message_rows=counts.get("message"),
            )

        return IntegrityReport(
            path=str(db_path),
            ok=True,
            detail="ok",
            part_rows=counts.get("part"),
            message_rows=counts.get("message"),
        )
    finally:
        conn.close()


def require_sound_session_db(path: str | Path) -> IntegrityReport:
    """Verify ``path`` and raise :class:`SessionDbCorrupt` when untrustworthy.

    Fail-closed gate for the extraction entrypoint. A corrupt substrate must
    VOID the cell, never silently under-extract.
    """
    report = verify_session_db(path)
    if not report.ok:
        raise SessionDbCorrupt(
            f"session DB failed integrity verification and cannot be used as an "
            f"extraction substrate ({report.detail}); path={report.path}. "
            "The cell is VOID-INSTRUMENT: extracting from this DB would silently "
            "under-report memories."
        )
    return report
