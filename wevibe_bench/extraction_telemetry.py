"""Extraction telemetry sink — the tunability instrument for the memory system.

WHY THIS EXISTS
===============
The MCP computes a great deal of per-memory detail on the extraction path —
near-duplicate cosine scores, classified vs suggested keywords, stack hints,
the mc1 write envelope — and the bench threw ALL of it away before anything
could read it. Two four-field whitelists were responsible:

    wevibe_bench/lifecycle/m2_proof.py  _memory_from_candidate
    scripts/backgammon_sxe.py           _normalize_memory

Both kept only text/keywords/stack_hint/memory_type. So the single most useful
question an operator can ask about the memory system — "what is the actual
distribution of near-dup scores, and is 0.93 the right threshold?" — was
unanswerable from the bench, not because the number was never computed but
because it was discarded one function call after being produced.

This module is the durable sink for that data. The goal, in the operator's
words: "measure now and make ALL data available to understand the inner
components of the memory system so that it can be seen and tuned."

⚠ PLAINTEXT IS STORED HERE, DELIBERATELY ⚠
==========================================
This sink stores memory PLAINTEXT, verbatim. That is an explicit,
operator-ratified (Walter, 2026-08-12) override of the RUNBOOK §9 hash-only
logging rule, and it is scoped to THIS FILE'S DATABASE ONLY.

It is sound HERE and nowhere else, for reasons that are properties of the
bench rather than preferences:

  - This is a local, single-operator benchmark instrument on the operator's own
    machine, measuring the operator's own synthetic corpus. There is no third
    party whose knowledge is at stake.
  - The confidentiality guarantee WeVibe actually makes is narrow and is about
    the HUB: "the hub cannot decrypt stored ciphertext" (WHITEPAPER §4.5). It
    has never been a claim that no component anywhere may hold plaintext — the
    moderator decrypts and reviews plaintext locally by design
    (DECISIONS D-2.4), and the smart-leader coordinator reads candidate text by
    design ("This is by design and is not a leak", RUNBOOK §9).
  - Nothing here is on an authority path. This DB is never read by the hub, the
    chain, the recall ranker or any decision that affects an organization's
    corpus. It is a measurement side-channel, and deleting the entire file
    changes no outcome anywhere.

DO NOT COPY THIS PATTERN INTO A PRODUCTION PATH. R-37 stands everywhere else:
fingerprints and sizes only, never plaintext, never keys, never ciphertext.
A hub-side, server-side or MCP-side logger that stored plaintext would be a
canon violation. This one is a bench instrument and says so in its own file
header, its schema comment, and the report that introduced it.

⚠ journal_mode=DELETE IS LOAD-BEARING — DO NOT "OPTIMISE" IT TO WAL ⚠
====================================================================
The dashboard container mounts the bench read-only (`:ro` in
dashboard/docker-compose.yml). SQLite in WAL mode must CREATE a `-shm` sidecar
file when opening a database, even to read it, and the kernel refuses that on a
read-only mount. Measured directly, before this module was written:

    WAL    + :ro mount  →  "unable to open database file"   FATAL
    DELETE + :ro mount  →  reads fine
    DELETE + :ro mount, writer holding an open write txn  →  reads committed
                                                              rows only, correct

WAL is the usual right answer for a concurrent reader and is the change someone
will eventually be tempted to make. Here it silently breaks the panel on the
DEPLOYED artifact while passing every host-side test, which is the worst
available failure mode. The mode is asserted on every open.

WRITES ARE FAIL-OPEN, ALWAYS
============================
Telemetry must never be able to fail an extraction. A benchmark cell costs
hours; losing one because a measurement side-channel could not write would be
an absurd trade. Every public function swallows its own errors and reports them
through `last_error` — swallowed for CONTROL FLOW, never silent: the reason is
retained and printed by the caller's progress line.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# One file, appended across every extraction on this machine. The unified queue
# is a read over this table, which is what makes it cross-session by
# construction rather than by scanning run directories.
DEFAULT_DB_NAME = "extraction-telemetry.db"


def _default_db_path() -> Path:
    """`wevibe-bench/data/extract/extraction-telemetry.db`.

    `data/` is the established telemetry/retention layer (data/README.md) and is
    explicitly NEVER a competing source of truth: `runs/` stays authoritative
    for the manifest and status stream (RC-5). This DB is measurement only.
    """
    override = os.environ.get("WEVIBE_BENCH_EXTRACT_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "data" / "extract" / DEFAULT_DB_NAME


# ── SCHEMA ───────────────────────────────────────────────────────────────────
#
# Three tables, one row per real thing:
#
#   extraction_jobs      one row per extraction invocation
#   extraction_memories  one row per memory candidate — THE TUNING TABLE
#   extraction_stages    one row per stage transition — the timing table
#
# Every column that can be absent is NULLABLE, and NULL means NOT OBSERVED.
# A measured zero is stored as 0 and is a real result. These are different
# facts and the schema keeps them different, matching the board's contract
# rule 1 (three kinds of nothing).

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_jobs (
    job_rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      REAL    NOT NULL,
    finished_at     REAL,
    run_label       TEXT    NOT NULL,
    source_mode     TEXT    NOT NULL,
    org_id          TEXT,
    session_id      TEXT,
    session_db_path TEXT,
    producer_model  TEXT,
    extract_model   TEXT,
    provider        TEXT,
    num_ctx         INTEGER,
    status          TEXT,
    delivery        TEXT,
    n_candidates    INTEGER,
    n_committed     INTEGER,
    extract_dur_ms  INTEGER,
    extract_path    TEXT,
    logfile         TEXT,
    error           TEXT,
    schema_version  INTEGER NOT NULL DEFAULT 1
);

-- THE TUNING TABLE. One row per memory candidate produced by extraction.
--
-- `text` holds PLAINTEXT verbatim — see the module header. This is the whole
-- point of the table: a near-dup score of 0.94 is uninterpretable without the
-- two texts that produced it, and "is 0.93 the right threshold" cannot be
-- answered from fingerprints.
CREATE TABLE IF NOT EXISTS extraction_memories (
    memory_rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_rowid           INTEGER NOT NULL REFERENCES extraction_jobs(job_rowid),
    idx                 INTEGER NOT NULL,
    text                TEXT,
    text_size           INTEGER,
    memory_fp           TEXT,
    memory_type         TEXT,
    keywords_json       TEXT,
    keyword_count       INTEGER,
    classified_json     TEXT,
    suggestions_json    TEXT,
    stack_hint_json     TEXT,
    -- NEAR-DUP: the reason this table exists. Flagged, NEVER dropped.
    near_dup_source     TEXT,
    near_dup_matched    TEXT,
    near_dup_score      REAL,
    near_dup_decision   TEXT,
    extraction_hash     TEXT,
    submission_hash     TEXT,
    approve_status      TEXT,
    delivered           INTEGER,
    delivery_mode       TEXT,
    raw_candidate_json  TEXT
);

CREATE TABLE IF NOT EXISTS extraction_stages (
    stage_rowid  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_rowid    INTEGER NOT NULL REFERENCES extraction_jobs(job_rowid),
    at           REAL    NOT NULL,
    stage        TEXT    NOT NULL,
    state        TEXT    NOT NULL,
    count        INTEGER,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS idx_mem_job   ON extraction_memories(job_rowid);
CREATE INDEX IF NOT EXISTS idx_stage_job ON extraction_stages(job_rowid);
CREATE INDEX IF NOT EXISTS idx_jobs_run  ON extraction_jobs(run_label, source_mode);
CREATE INDEX IF NOT EXISTS idx_mem_ndup  ON extraction_memories(near_dup_score);
"""


def _json(value: Any) -> str | None:
    """Serialise, or None. Never raises — telemetry cannot fail a run."""
    if value is None:
        return None
    try:
        return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
    except Exception:
        return None


class ExtractionTelemetry:
    """Fail-open writer for one extraction invocation.

    Every method swallows its own exceptions and records the reason in
    `last_error`. A telemetry failure degrades to missing rows — never to a
    failed extraction.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.job_rowid: int | None = None
        self.last_error: str | None = None
        self.enabled = True
        self._conn: sqlite3.Connection | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0)
            # DELETE, NOT WAL. See the module header — WAL breaks the
            # dashboard's read-only mount. Asserted, not assumed: sqlite
            # returns the mode it actually adopted.
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            adopted = str(mode[0]).lower() if mode else "?"
            if adopted != "delete":
                self.last_error = (
                    f"refusing telemetry: journal_mode is {adopted!r}, not 'delete' — "
                    "WAL cannot be read from the dashboard's read-only mount"
                )
                self.enabled = False
                conn.close()
                return None
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            return conn
        except Exception as exc:  # fail-open, never silent
            self.last_error = f"telemetry connect failed: {exc}"
            self.enabled = False
            return None

    def start_job(self, **fields: Any) -> int | None:
        """Open the job row. Returns its rowid, or None when telemetry is off."""
        if not self.enabled:
            return None
        conn = self._connect()
        if conn is None:
            return None
        cols = {
            "started_at": time.time(),
            "schema_version": SCHEMA_VERSION,
            **{k: v for k, v in fields.items() if v is not None},
        }
        try:
            names = ",".join(cols)
            marks = ",".join("?" * len(cols))
            cur = conn.execute(
                f"INSERT INTO extraction_jobs ({names}) VALUES ({marks})",
                tuple(cols.values()),
            )
            conn.commit()
            self.job_rowid = int(cur.lastrowid)
            return self.job_rowid
        except Exception as exc:
            self.last_error = f"telemetry start_job failed: {exc}"
            return None

    def record_stage(
        self,
        stage: str,
        state: str,
        *,
        at: float | None = None,
        count: int | None = None,
        detail: str | None = None,
    ) -> None:
        """One stage transition. Mirrors the BACKGAMMON_SXE_STAGE emitter."""
        if not self.enabled or self.job_rowid is None:
            return
        conn = self._connect()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT INTO extraction_stages (job_rowid, at, stage, state, count, detail) "
                "VALUES (?,?,?,?,?,?)",
                (self.job_rowid, at if at is not None else time.time(), stage, state, count, detail),
            )
            conn.commit()
        except Exception as exc:
            self.last_error = f"telemetry record_stage failed: {exc}"

    def record_memories(self, memories: list[dict[str, Any]]) -> int:
        """Write every candidate, with everything carried on it.

        Returns the number of rows written. `near_dup` is read from the
        candidate as the MCP produced it; `decision` is derived the same way the
        MCP logs it (`extraction.ts:940`): flagged when a near-dup was attached,
        kept otherwise. FLAGGED-NEVER-DROPPED is the invariant — this function
        stores a decision for review and never removes a memory.
        """
        if not self.enabled or self.job_rowid is None:
            return 0
        conn = self._connect()
        if conn is None:
            return 0

        written = 0
        for idx, memory in enumerate(memories, start=1):
            try:
                text = memory.get("text")
                text_str = text if isinstance(text, str) else None
                near = memory.get("near_dup")
                near = near if isinstance(near, dict) else {}
                keywords = memory.get("keywords")
                keywords_list = keywords if isinstance(keywords, list) else None
                raw_keywords = memory.get("keywords_raw")
                classified = None
                suggestions = None
                if isinstance(raw_keywords, dict):
                    classified = raw_keywords.get("classified")
                    suggestions = raw_keywords.get("suggestions")

                conn.execute(
                    "INSERT INTO extraction_memories ("
                    "job_rowid, idx, text, text_size, memory_fp, memory_type,"
                    "keywords_json, keyword_count, classified_json, suggestions_json,"
                    "stack_hint_json, near_dup_source, near_dup_matched, near_dup_score,"
                    "near_dup_decision, extraction_hash, raw_candidate_json"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.job_rowid,
                        idx,
                        text_str,
                        len(text_str) if text_str is not None else None,
                        memory.get("memory_fp"),
                        memory.get("memory_type"),
                        _json(keywords_list),
                        len(keywords_list) if keywords_list is not None else None,
                        _json(classified),
                        _json(suggestions),
                        _json(memory.get("stack_hint")),
                        near.get("source"),
                        near.get("matched"),
                        near.get("score"),
                        # Derived exactly as the MCP logs it. A memory with no
                        # near_dup is 'kept' — a REAL measured decision, not an
                        # absence, so it is stored rather than left NULL.
                        "flagged" if near else "kept",
                        memory.get("extraction_hash"),
                        _json(memory.get("raw_candidate")),
                    ),
                )
                written += 1
            except Exception as exc:
                self.last_error = f"telemetry record_memories failed at idx={idx}: {exc}"
                continue
        try:
            conn.commit()
        except Exception as exc:
            self.last_error = f"telemetry record_memories commit failed: {exc}"
        return written

    def update_memory_commit(
        self,
        idx: int,
        *,
        submission_hash: str | None = None,
        approve_status: str | None = None,
        memory_fp: str | None = None,
    ) -> None:
        """Stamp submit/approve outcome onto an already-written candidate row."""
        if not self.enabled or self.job_rowid is None:
            return
        conn = self._connect()
        if conn is None:
            return
        sets: dict[str, Any] = {}
        if submission_hash is not None:
            sets["submission_hash"] = submission_hash
        if approve_status is not None:
            sets["approve_status"] = approve_status
        if memory_fp is not None:
            sets["memory_fp"] = memory_fp
        if not sets:
            return
        try:
            assignment = ",".join(f"{k}=?" for k in sets)
            conn.execute(
                f"UPDATE extraction_memories SET {assignment} WHERE job_rowid=? AND idx=?",
                (*sets.values(), self.job_rowid, idx),
            )
            conn.commit()
        except Exception as exc:
            self.last_error = f"telemetry update_memory_commit failed: {exc}"

    def update_memory_delivery(self, idx: int, *, delivered: bool, delivery_mode: str | None) -> None:
        """Stamp the per-memory delivery probe outcome."""
        if not self.enabled or self.job_rowid is None:
            return
        conn = self._connect()
        if conn is None:
            return
        try:
            conn.execute(
                "UPDATE extraction_memories SET delivered=?, delivery_mode=? "
                "WHERE job_rowid=? AND idx=?",
                (1 if delivered else 0, delivery_mode, self.job_rowid, idx),
            )
            conn.commit()
        except Exception as exc:
            self.last_error = f"telemetry update_memory_delivery failed: {exc}"

    def finish_job(self, **fields: Any) -> None:
        """Close the job row. Called on BOTH the success and error paths."""
        if not self.enabled or self.job_rowid is None:
            return
        conn = self._connect()
        if conn is None:
            return
        cols = {"finished_at": time.time(), **{k: v for k, v in fields.items() if v is not None}}
        try:
            assignment = ",".join(f"{k}=?" for k in cols)
            conn.execute(
                f"UPDATE extraction_jobs SET {assignment} WHERE job_rowid=?",
                (*cols.values(), self.job_rowid),
            )
            conn.commit()
        except Exception as exc:
            self.last_error = f"telemetry finish_job failed: {exc}"

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                self.last_error = f"telemetry close failed: {exc}"
            self._conn = None
