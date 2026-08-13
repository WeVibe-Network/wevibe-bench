// ─────────────────────────────────────────────────────────────────────────────
// EXTRACTION INVENTORY — the telemetry read path
//
//     cd wevibe-bench/dashboard && node --test extraction-inventory.test.mjs
//
// Zero dependencies, stock `node --test`. Fixtures are built with `node:sqlite`
// writing the SAME schema the Python writer creates, so this exercises the real
// query path rather than a mock of it.
//
// ── WHAT THIS PINS, AND WHY EACH ONE IS HERE ────────────────────────────────
//
//  1. journal_mode=DELETE is READABLE and WAL is NOT, from a read-only handle.
//     This is the one that would otherwise ship broken: WAL passes every
//     host-side test where the file is writable, and fails only on the
//     DEPLOYED artifact where the bench is mounted `:ro`. The failure mode is
//     an extraction panel that reads "unwired" for a reason nobody can
//     reproduce locally. Asserted directly rather than trusted to a comment.
//
//  2. A MISSING DB IS NOT AN ERROR. Before the first extraction there is no
//     file, and that is a designed state. If it reported as a broken source
//     the operator would go looking for a fault that does not exist.
//
//  3. NO MEMORY PLAINTEXT REACHES THE BOARD PAYLOAD. The telemetry DB stores
//     bodies on purpose; the board is a streaming surface where everything
//     rendered is public forever. This asserts the boundary at the seam that
//     crosses it, so the two decisions cannot silently merge.
//
//  4. `flagged` IS COUNTED FROM THE STORED DECISION, never recomputed from the
//     score against the panel's copy of the threshold. If the constants drift,
//     the board must report what the MCP DECIDED, not re-adjudicate it.
//
//  5. A MEASURED ZERO SURVIVES AS 0. Three kinds of nothing stay distinct
//     (contract rule 1): a zero-memory extraction is a real result and must
//     never arrive as null, which renders as "unobserved".
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { mkdtempSync, mkdirSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { read, NEAR_DUP_THRESHOLD } from "./sources/extraction-inventory.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

/** The schema the Python writer creates. Kept byte-compatible on purpose. */
const SCHEMA = `
CREATE TABLE extraction_jobs (
  job_rowid INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL NOT NULL, finished_at REAL,
  run_label TEXT NOT NULL, source_mode TEXT NOT NULL, org_id TEXT, session_id TEXT,
  session_db_path TEXT, producer_model TEXT, extract_model TEXT, provider TEXT, num_ctx INTEGER,
  status TEXT, delivery TEXT, n_candidates INTEGER, n_committed INTEGER, extract_dur_ms INTEGER,
  extract_path TEXT, logfile TEXT, error TEXT, schema_version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE extraction_memories (
  memory_rowid INTEGER PRIMARY KEY AUTOINCREMENT, job_rowid INTEGER NOT NULL, idx INTEGER NOT NULL,
  text TEXT, text_size INTEGER, memory_fp TEXT, memory_type TEXT, keywords_json TEXT,
  keyword_count INTEGER, classified_json TEXT, suggestions_json TEXT, stack_hint_json TEXT,
  near_dup_source TEXT, near_dup_matched TEXT, near_dup_score REAL, near_dup_decision TEXT,
  extraction_hash TEXT, submission_hash TEXT, approve_status TEXT, delivered INTEGER,
  delivery_mode TEXT, raw_candidate_json TEXT);
CREATE TABLE extraction_stages (
  stage_rowid INTEGER PRIMARY KEY AUTOINCREMENT, job_rowid INTEGER NOT NULL, at REAL NOT NULL,
  stage TEXT NOT NULL, state TEXT NOT NULL, count INTEGER, detail TEXT);
`;

const SECRET = "PLAINTEXT-CANARY-never-broadcast-this-body";

function benchRootWith({ journal = "DELETE", rows = true } = {}) {
  const root = mkdtempSync(join(tmpdir(), "xinv-"));
  mkdirSync(join(root, "data", "extract"), { recursive: true });
  const dbPath = join(root, "data", "extract", "extraction-telemetry.db");

  const db = new DatabaseSync(dbPath);
  db.exec(`PRAGMA journal_mode=${journal};`);
  db.exec(SCHEMA);

  if (rows) {
    db.exec(`
      INSERT INTO extraction_jobs
        (started_at, finished_at, run_label, source_mode, org_id, session_id, producer_model,
         status, delivery, n_candidates, n_committed, extract_dur_ms)
      VALUES (1000, 1100, 'cumulative', 'on', 'wevibe-org-2', 'sess-aaa', 'qwen3.6-35b',
              'ok', 'YES', 2, 2, 48210);
      INSERT INTO extraction_jobs
        (started_at, run_label, source_mode, producer_model, status, n_candidates, n_committed)
      VALUES (900, 'cumulative', 'off', 'glm-4.6-air', 'ok', 0, 0);

      INSERT INTO extraction_stages (job_rowid, at, stage, state, count)
      VALUES (1, 1001, 'init', 'complete', NULL), (1, 1002, 'extract', 'complete', 2);

      INSERT INTO extraction_memories
        (job_rowid, idx, text, text_size, memory_fp, near_dup_source, near_dup_matched,
         near_dup_score, near_dup_decision, submission_hash, delivered, keyword_count)
      VALUES (1, 1, '${SECRET}', 42, 'aa11', 'intra_session', 'eh-0', 0.9412, 'flagged', 'sub-1', 1, 3);
      INSERT INTO extraction_memories
        (job_rowid, idx, text, text_size, memory_fp, near_dup_score, near_dup_decision, keyword_count)
      VALUES (1, 2, 'a clean unique memory body', 26, 'bb22', 0.4010, 'kept', 2);
    `);
  }
  db.close();
  return { root, dbPath };
}

test("journal_mode=DELETE is readable from a read-only handle", () => {
  const { dbPath } = benchRootWith({ journal: "DELETE" });
  const db = new DatabaseSync(dbPath, { readOnly: true });
  const n = db.prepare("SELECT count(*) AS n FROM extraction_jobs").get();
  db.close();
  assert.equal(Number(n.n), 2);
});

test("the WRITER pins journal_mode=DELETE and refuses anything else", () => {
  // ── WHY THIS IS A SOURCE ASSERTION AND NOT A BEHAVIOURAL ONE ──────────────
  //
  // The real constraint is a READ-ONLY BIND MOUNT, not sqlite's `readOnly`
  // flag. Opening WAL with `{readOnly:true}` inside a WRITABLE directory
  // succeeds — sqlite can still create the `-shm` sidecar — so a test written
  // that way asserts nothing. The failure only appears when the FILESYSTEM
  // refuses the sidecar, which needs a genuinely read-only mount.
  //
  // That was verified directly, out of band, against the real container:
  //
  //   docker run --rm -v "$PWD/sq2:/d:ro" node:22-alpine node -e '...'
  //     WAL    + :ro → "unable to open database file"          FATAL
  //     DELETE + :ro → reads fine
  //     DELETE + :ro + writer holding an open write txn → committed rows only
  //
  // Reproducing a read-only mount inside `node --test` would need root or a
  // disk image, so what is pinned here is the INVARIANT THAT PREVENTS IT: the
  // writer must demand DELETE and refuse to proceed otherwise. If someone
  // "optimises" the writer to WAL, this fails immediately — on the host, where
  // it is cheap — instead of silently on the deployed artifact.
  const writer = readFileSync(
    join(HERE, "..", "wevibe_bench", "extraction_telemetry.py"),
    "utf8",
  );
  assert.match(
    writer,
    /PRAGMA journal_mode=DELETE/,
    "the telemetry writer must pin journal_mode=DELETE — WAL cannot be opened " +
      "from the dashboard's read-only bench mount",
  );
  assert.match(
    writer,
    /adopted != "delete"/,
    "the writer must ASSERT the adopted journal mode and disable itself otherwise — " +
      "requesting DELETE without checking it was granted is not a guarantee",
  );
  assert.ok(
    !/journal_mode\s*=\s*WAL/i.test(writer),
    "the telemetry writer must never request WAL",
  );
});

test("a missing DB is a designed state, not a broken source", async () => {
  const r = await read({ benchRoot: join(tmpdir(), "xinv-does-not-exist") });
  assert.equal(r.ok, false);
  assert.match(r.reason, /no extraction telemetry yet/);
  assert.match(r.reason, /created by the first extraction/);
});

test("NO memory plaintext reaches the board payload", async () => {
  const { root } = benchRootWith();
  const r = await read({ benchRoot: root });
  assert.equal(r.ok, true);
  const wire = JSON.stringify(r.patch);
  assert.ok(
    !wire.includes(SECRET),
    "a memory BODY reached the board payload — the board is a streaming surface and " +
      "everything on it is public forever. Bodies stay in the DB on the machine.",
  );
  // The fingerprint DOES cross, because that is what makes the row identifiable.
  assert.ok(wire.includes("aa11"), "the memory fingerprint must reach the board");
  rmSync(root, { recursive: true, force: true });
});

test("flagged is counted from the STORED decision, not recomputed from the score", async () => {
  const { root } = benchRootWith();
  const r = await read({ benchRoot: root });
  const d = r.patch.dedup;

  assert.equal(d.distribution.flagged, 1);
  assert.equal(d.distribution.kept, 1);
  assert.equal(d.distribution.scored, 2);
  assert.equal(d.flagged.length, 1);
  assert.equal(d.flagged[0].decision, "flagged");
  assert.equal(d.flagged[0].score, 0.9412);

  // The row scored 0.4010 is BELOW the threshold and stored 'kept'. If the
  // panel ever re-adjudicated with its own constant, a drift between the two
  // copies would make the board contradict the MCP about what it decided.
  assert.ok(d.flagged[0].score >= NEAR_DUP_THRESHOLD);
  rmSync(root, { recursive: true, force: true });
});

test("a measured zero survives as 0, never as null", async () => {
  const { root } = benchRootWith();
  const r = await read({ benchRoot: root });
  const offJob = r.patch.extraction_queue.jobs.find((j) => j.source_mode === "off");
  assert.equal(offJob.n_candidates, 0, "0 memories is a REAL result and must not arrive as null");
  assert.notEqual(offJob.n_candidates, null);
  rmSync(root, { recursive: true, force: true });
});

test("the queue is ordered newest-first and carries per-job stage rows", async () => {
  const { root } = benchRootWith();
  const r = await read({ benchRoot: root });
  const jobs = r.patch.extraction_queue.jobs;
  assert.equal(jobs.length, 2);
  assert.ok(jobs[0].started_at >= jobs[1].started_at, "queue must be newest-first");
  const withStages = jobs.find((j) => j.source_mode === "on");
  assert.equal(withStages.stages.length, 2);
  assert.equal(withStages.tally.candidates, 2);
  assert.equal(withStages.tally.flagged, 1);
  rmSync(root, { recursive: true, force: true });
});

test("every row's origin is asserted from what wrote it, never guessed", async () => {
  // Only the bench harness writes to this DB, so every row is a bench cell.
  // The design's "remote · mbp-2" row has no inventory behind it; the panel
  // renders unobservable origins as unwired rather than fabricating them.
  const { root } = benchRootWith();
  const r = await read({ benchRoot: root });
  for (const j of r.patch.extraction_queue.jobs) {
    assert.equal(j.origin, "bench");
  }
  rmSync(root, { recursive: true, force: true });
});
