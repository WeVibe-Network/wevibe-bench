// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: extraction-inventory
//
// THE CROSS-SESSION INVENTORY. This is what makes the extraction queue unified:
// one read over every extraction this machine has ever run, rather than the
// single in-flight job the control plane happens to be holding in memory.
//
// ── WHY A DATABASE AND NOT THE CONTROL PLANE'S LIVE VIEW ─────────────────────
//
// `/api/extraction` returns exactly ONE extraction: whatever `ExtractionTracker`
// currently holds. It is memory-resident, single-slot, and dies with the
// process. That is correct for "what is running now" and useless for "what has
// this machine extracted", which is the question the unified queue exists to
// answer. A restart of the control plane erased the entire history.
//
// So the harness now persists every job/stage/candidate to
// `data/extract/extraction-telemetry.db`
// and this module reads it. The live tracker is still authoritative for the
// RUNNING job — see the merge note in panels/extraction.js — because a row is
// only written at a stage transition and the tracker is fresher between them.
//
// ── READ-ONLY, AND ENFORCED BY THREE INDEPENDENT LAYERS ──────────────────────
//   1. `readOnly: true` on the connection
//   2. the bench repo is mounted `:ro` in the container (docker-compose.yml:29)
//   3. the dashboard server serves GET only (server.mjs:320)
// Any one of these alone would do; all three are already true, so this module
// adds no new privilege of any kind.
//
// ── WAL WOULD BREAK THIS. IT IS NOT A STYLE PREFERENCE ───────────────────────
// SQLite in WAL mode CREATES a `-shm` sidecar when opening a database, even for
// a pure read, and a `:ro` mount refuses that — the open fails outright with
// "unable to open database file". The writer therefore pins `journal_mode=DELETE`
// and asserts it. Measured before either side was written:
//     WAL    + :ro → unable to open database file      FATAL
//     DELETE + :ro → reads fine, committed rows only, even mid-write
// If this source ever reports `unable to open`, suspect the journal mode first.
//
// ── PLAINTEXT IS PRESENT IN THIS DB, AND IS NOT SENT TO THE BOARD ────────────
// The telemetry DB deliberately stores memory plaintext so near-dup scores are
// interpretable (operator-ratified; see the writer's header for why that is
// sound for a local bench instrument and nowhere else).
//
// This module is where that data would leave the machine, so the boundary is
// drawn HERE, at the seam, and for a reason that has nothing to do with
// cryptography: the board is a STREAMING SURFACE. Everything it renders is
// public forever, and the dashboard is explicitly documented as exposable on
// the LAN. Shipping memory bodies into a page meant for a stream is a
// presentation decision, not a confidentiality one — and it would also make the
// /api/board payload unbounded.
//
// So the queue carries sizes, counts, scores and decisions. The dedup view
// carries the near-dup SCORE and both FINGERPRINTS — enough to tune the
// threshold, which is the stated goal — and names the exact `sqlite3` query
// that shows the bodies to an operator sitting at the machine. The data is
// fully available; it is simply not broadcast.
// ─────────────────────────────────────────────────────────────────────────────

import { DatabaseSync } from "node:sqlite";
import { join } from "node:path";
import { statOrNull } from "./_runtime.mjs";
import { int, num, str } from "../contract.mjs";

export const id = "extraction-inventory";
export const fields = ["extraction_queue", "dedup"];
export function describe() {
  return "extraction telemetry DB — every extraction on this machine, per-memory dedup + keyword detail";
}

/** How many jobs the queue renders. Oldest are trimmed, not summarised away. */
export const QUEUE_LIMIT = 40;
/** How many flagged candidates the dedup view carries. */
export const DEDUP_LIMIT = 60;

/**
 * Must match `NEAR_DUP_COSINE_THRESHOLD` in wevibe-mcp/src/extraction.ts:77.
 *
 * DECLARED, NOT DERIVED. The board cannot see the MCP's constant, so this is a
 * copy — and a copy that drifts is worse than no copy at all. It is rendered
 * with its source named so a reader can check it, and the whole point of the
 * dedup view is to show the score DISTRIBUTION against this line: if the
 * flagged/kept split looks wrong, the threshold is the knob.
 */
export const NEAR_DUP_THRESHOLD = 0.93;
export const NEAR_DUP_THRESHOLD_SOURCE = "wevibe-mcp/src/extraction.ts:77";

function dbPath(ctx) {
  return join(ctx.benchRoot, "data", "extract", "extraction-telemetry.db");
}

/**
 * Open read-only. Returns null when the file does not exist — which is the
 * NORMAL state before the first extraction, not an error, and must not be
 * reported as a broken source.
 */
function openDb(path) {
  return new DatabaseSync(path, { readOnly: true });
}

export async function read(ctx) {
  const path = dbPath(ctx);
  const st = await statOrNull(path);
  if (!st?.isFile()) {
    return {
      ok: false,
      reason:
        "no extraction telemetry yet — the DB is created by the first extraction " +
        `(expected at ${path}). Nothing has been extracted on this machine.`,
    };
  }

  let db;
  try {
    db = openDb(path);
  } catch (err) {
    // Named explicitly: this is the WAL symptom, and the fix is the writer's
    // journal mode, not this module.
    return {
      ok: false,
      reason:
        `extraction telemetry unreadable: ${String(err?.message ?? err)}. ` +
        "If this says 'unable to open database file' on a file that exists, the " +
        "writer is in WAL mode — WAL cannot be opened from a read-only mount.",
    };
  }

  try {
    const jobs = db
      .prepare(
        `SELECT job_rowid, started_at, finished_at, run_label, source_mode, org_id,
                session_id, producer_model, extract_model, status, delivery,
                n_candidates, n_committed, extract_dur_ms, error
         FROM extraction_jobs
         ORDER BY started_at DESC
         LIMIT ?`,
      )
      .all(QUEUE_LIMIT);

    // Per-job stage progress. The queue's "STAGE 1-10" pips are a COUNT of
    // stages that reached a terminal state, plus whichever is running — derived
    // here in SQL rather than in the renderer so the panel cannot invent
    // progress the data does not support.
    const stageRows = db
      .prepare(
        `SELECT job_rowid, stage, state, at, count, detail
         FROM extraction_stages
         ORDER BY stage_rowid ASC`,
      )
      .all();

    const stagesByJob = new Map();
    for (const r of stageRows) {
      const key = int(r.job_rowid);
      if (!stagesByJob.has(key)) stagesByJob.set(key, []);
      stagesByJob.get(key).push({
        stage: str(r.stage),
        state: str(r.state),
        at: num(r.at),
        count: int(r.count),
        detail: str(r.detail),
      });
    }

    // Per-job candidate tallies. `flagged` is counted from the stored decision
    // rather than recomputed from the score, so the board reports what the MCP
    // DECIDED and can never disagree with it — even if the threshold constant
    // above drifts.
    const tallyRows = db
      .prepare(
        `SELECT job_rowid,
                count(*)                                                   AS n,
                sum(CASE WHEN near_dup_decision = 'flagged' THEN 1 ELSE 0 END) AS flagged,
                sum(CASE WHEN submission_hash IS NOT NULL   THEN 1 ELSE 0 END) AS submitted,
                sum(CASE WHEN delivered = 1                 THEN 1 ELSE 0 END) AS delivered,
                sum(COALESCE(text_size, 0))                                AS bytes
         FROM extraction_memories
         GROUP BY job_rowid`,
      )
      .all();

    const tallyByJob = new Map();
    for (const r of tallyRows) {
      tallyByJob.set(int(r.job_rowid), {
        candidates: int(r.n),
        flagged: int(r.flagged),
        submitted: int(r.submitted),
        delivered: int(r.delivered),
        bytes: int(r.bytes),
      });
    }

    const queue = jobs.map((j) => {
      const rowid = int(j.job_rowid);
      const stages = stagesByJob.get(rowid) ?? [];
      const tally = tallyByJob.get(rowid) ?? null;
      return {
        id: rowid,
        started_at: num(j.started_at) !== null ? Math.round(num(j.started_at) * 1000) : null,
        finished_at: num(j.finished_at) !== null ? Math.round(num(j.finished_at) * 1000) : null,
        run_label: str(j.run_label),
        source_mode: str(j.source_mode),
        org_id: str(j.org_id),
        session_id: str(j.session_id),
        producer_model: str(j.producer_model),
        extract_model: str(j.extract_model),
        status: str(j.status),
        delivery: str(j.delivery),
        // A measured 0 is a real result and is preserved as 0, never nulled.
        n_candidates: int(j.n_candidates),
        n_committed: int(j.n_committed),
        extract_dur_ms: int(j.extract_dur_ms),
        error: str(j.error),
        stages,
        tally,
        // ORIGIN is asserted from what produced the row, never guessed. Every
        // row in this DB was written by the bench harness, so every row is a
        // bench cell. The design's "remote · mbp-2" row has no inventory behind
        // it and is rendered `unwired` by the panel rather than fabricated.
        origin: "bench",
      };
    });

    // ── THE DEDUP DECISION VIEW ──────────────────────────────────────────
    // Flagged candidates, newest first. FLAGGED-NEVER-DROPPED is the invariant
    // being surfaced: every row here was KEPT and submitted. The view presents
    // a decision for review; it has no mechanism to discard anything, and the
    // panel says so in words.
    const flagged = db
      .prepare(
        `SELECT m.memory_rowid, m.job_rowid, m.idx, m.text_size, m.memory_fp,
                m.near_dup_source, m.near_dup_matched, m.near_dup_score,
                m.near_dup_decision, m.extraction_hash, m.submission_hash,
                m.keyword_count, j.run_label, j.source_mode, j.producer_model,
                j.started_at
         FROM extraction_memories m
         JOIN extraction_jobs j ON j.job_rowid = m.job_rowid
         WHERE m.near_dup_decision = 'flagged'
         ORDER BY m.memory_rowid DESC
         LIMIT ?`,
      )
      .all(DEDUP_LIMIT);

    // Score distribution across EVERY scored candidate, flagged or not. This is
    // the actual tuning instrument: it answers "is 0.93 in the right place"
    // rather than only showing what the current threshold already caught.
    const dist = db
      .prepare(
        `SELECT count(*)                                                    AS scored,
                sum(CASE WHEN near_dup_decision = 'flagged' THEN 1 ELSE 0 END) AS flagged,
                sum(CASE WHEN near_dup_decision = 'kept'    THEN 1 ELSE 0 END) AS kept,
                min(near_dup_score)                                         AS min_score,
                max(near_dup_score)                                         AS max_score,
                avg(near_dup_score)                                         AS avg_score
         FROM extraction_memories`,
      )
      .get();

    const totals = db
      .prepare(
        `SELECT count(*)                                                   AS jobs,
                sum(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)             AS ok,
                sum(CASE WHEN status = 'error' THEN 1 ELSE 0 END)          AS failed
         FROM extraction_jobs`,
      )
      .get();

    return {
      ok: true,
      provenance: { path, mtime: st.mtimeMs, bytes: st.size },
      patch: {
        extraction_queue: {
          jobs: queue,
          returned: queue.length,
          total_jobs: int(totals?.jobs),
          ok_jobs: int(totals?.ok),
          failed_jobs: int(totals?.failed),
          limit: QUEUE_LIMIT,
          db_path: path,
        },
        dedup: {
          threshold: NEAR_DUP_THRESHOLD,
          threshold_source: NEAR_DUP_THRESHOLD_SOURCE,
          flagged: flagged.map((f) => ({
            id: int(f.memory_rowid),
            job_id: int(f.job_rowid),
            idx: int(f.idx),
            run_label: str(f.run_label),
            source_mode: str(f.source_mode),
            producer_model: str(f.producer_model),
            at: num(f.started_at) !== null ? Math.round(num(f.started_at) * 1000) : null,
            text_size: int(f.text_size),
            keyword_count: int(f.keyword_count),
            // FINGERPRINTS, not bodies. See the header: this is a streaming
            // surface, and the bodies stay on the machine.
            memory_fp: str(f.memory_fp),
            extraction_hash: str(f.extraction_hash),
            matched: str(f.near_dup_matched),
            score: num(f.near_dup_score),
            source: str(f.near_dup_source),
            decision: str(f.near_dup_decision),
            submission_hash: str(f.submission_hash),
          })),
          distribution: {
            scored: int(dist?.scored),
            flagged: int(dist?.flagged),
            kept: int(dist?.kept),
            min_score: num(dist?.min_score),
            max_score: num(dist?.max_score),
            avg_score: num(dist?.avg_score),
          },
          db_path: path,
        },
      },
    };
  } catch (err) {
    return { ok: false, reason: `extraction telemetry query failed: ${String(err?.message ?? err)}` };
  } finally {
    try {
      db.close();
    } catch {
      // The read is done; a close failure cannot affect the board and there is
      // nothing to recover. Not swallowed silently — there is simply no state
      // left to report on.
    }
  }
}
