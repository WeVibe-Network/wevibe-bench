// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: hub-db  [OPT-IN — DISABLED BY DEFAULT]
//
// Reads the hub's recall observability tables. This is the ONLY source that can
// populate the recall-moment candidate list with real relevance and standing.
//
// Tables used (wevibe-server/db/schema.sql):
//   query_log              — one row per recall query: candidate_count,
//                            returned_count, relevance_floor, surface_budget
//   query_candidate_scores — per candidate: vector_score, standing_bps,
//                            keyword_overlap, combined_score, matched_keywords,
//                            rank_position, disposition
//   serve_events           — delivery (NOT a win). status, tx_hash.
//   outcome_events         — resolution tri-state: worked/didnt_work/unobserved
//   memory_standing        — DERIVED projection, never authoritative
//
// ── CONNECTS OVER TCP, NEVER `docker exec` ───────────────────────────────────
// An earlier revision shelled out to `docker exec wevibe-postgres psql`. That
// cannot work from inside a container without mounting the docker socket —
// which would hand this read-only dashboard root-equivalent control of the
// host's docker daemon. A TCP connection to postgres is both containerisable
// and dramatically less privileged, so the socket is never mounted.
//
// DISABLED BY DEFAULT because a user running this out of the box must not need
// a database, or any part of the WeVibe stack, for the board to come up.
//
// PRIVACY BOUNDARY — enforced here, not by convention:
//   - query_log.query_text is v1-always-NULL by design and is NEVER selected.
//   - memory PLAINTEXT is never read from this source. Only content hashes,
//     scores and dispositions cross into the board.
//   - CIDs are truncated for display upstream in the UI.
// Everything rendered is public forever; this module is written to that rule.
//
// READ-ONLY BY CONSTRUCTION: every statement is a SELECT, and the session is
// opened with `default_transaction_read_only=on` so the server itself rejects
// a write even if one were ever introduced here.
// ─────────────────────────────────────────────────────────────────────────────

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { int, num, str } from "../contract.mjs";

const exec = promisify(execFile);

export const id = "hub-db";
export const fields = ["recall_moment.candidates", "hub", "honesty.serves"];
export function describe() {
  return "hub recall tables over TCP — candidate relevance/standing, serves, outcomes (opt-in, disabled by default)";
}

/**
 * Run one read-only SQL statement via psql over TCP.
 * Returns rows as arrays of strings. \x01 is the field separator because it
 * cannot appear in any of the columns we select.
 */
async function q(ctx, sql) {
  const cfg = ctx.config?.hubDb ?? {};
  const host = cfg.host ?? "wevibe-postgres";
  const port = String(cfg.port ?? 5432);
  const user = cfg.user ?? "wevibe";
  const db = cfg.database ?? "wevibe_hub";
  const password = cfg.password ?? process.env.WEVIBE_HUB_DB_PASSWORD ?? "";

  const { stdout } = await exec(
    "psql",
    [
      "-h", host,
      "-p", port,
      "-U", user,
      "-d", db,
      "-At",
      "-F", "\u0001",
      "-v", "ON_ERROR_STOP=1",
      "-c", "SET default_transaction_read_only = on;",
      "-c", sql,
    ],
    {
      timeout: 1500,
      maxBuffer: 2 * 1024 * 1024,
      env: {
        ...process.env,
        PGPASSWORD: password,
        PGCONNECT_TIMEOUT: "2",
        // Never let psql try to open an interactive prompt in a container.
        PSQL_PAGER: "cat",
      },
    },
  );

  return stdout
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && l !== "SET")
    .map((l) => l.split("\u0001"));
}

export async function read(ctx) {
  if (!ctx.config?.hubDb?.enabled) {
    return { ok: false, reason: "disabled by default — enable hubDb in dashboard.config.json" };
  }

  let counts;
  try {
    counts = await q(
      ctx,
      `SELECT
         (SELECT count(*) FROM query_log),
         (SELECT count(*) FROM serve_events),
         (SELECT count(*) FROM outcome_events),
         (SELECT count(*) FROM memory_standing)`,
    );
  } catch (err) {
    const msg = String(err?.stderr || err?.message || err).trim().slice(0, 160);
    return { ok: false, reason: `hub db unreachable: ${msg}` };
  }

  const [queries, serves, outcomes, standing] = (counts[0] ?? []).map((v) => int(v) ?? 0);

  if (!queries) {
    return {
      ok: false,
      reason: "hub reachable but no recall queries recorded yet (expected until an ON cell runs)",
    };
  }

  // Most recent query and its candidates. NOTE: query_text is never selected.
  const [latest] = await q(
    ctx,
    `SELECT query_id, relevance_floor, surface_budget, candidate_count, returned_count,
            extract(epoch from created_at)*1000
     FROM query_log ORDER BY created_at DESC LIMIT 1`,
  );

  let candidates = [];
  if (latest) {
    const rows = await q(
      ctx,
      `SELECT memory_cid, vector_score, standing_bps, keyword_overlap, combined_score,
              array_to_string(matched_keywords, ','), rank_position, disposition
       FROM query_candidate_scores
       WHERE query_id = '${String(latest[0]).replace(/'/g, "''")}'
       ORDER BY rank_position ASC LIMIT 12`,
    );
    candidates = rows.map((r) => ({
      cid: str(r[0]),
      relevance: num(r[1]),
      standing_bps: int(r[2]),
      keyword_overlap: num(r[3]),
      combined_score: num(r[4]),
      matched_keywords: str(r[5]) ? str(r[5]).split(",") : [],
      rank_position: int(r[6]),
      disposition: str(r[7]),
    }));
  }

  // Outcome tri-state tally. `unobserved` is a first-class third state.
  const outcomeRows = outcomes
    ? await q(ctx, `SELECT resolution, count(*) FROM outcome_events GROUP BY resolution`)
    : [];
  const outcomeTally = Object.fromEntries(outcomeRows.map((r) => [str(r[0]), int(r[1]) ?? 0]));

  const serveRows = serves
    ? await q(ctx, `SELECT status, count(*) FROM serve_events GROUP BY status`)
    : [];
  const serveTally = Object.fromEntries(serveRows.map((r) => [str(r[0]), int(r[1]) ?? 0]));

  return {
    ok: true,
    provenance: { path: "hub-db://query_log", mtime: Date.now(), bytes: null },
    patch: {
      hub: {
        queries,
        serves,
        outcomes,
        standing_rows: standing,
        outcome_tally: outcomeTally,
        latest_query: latest
          ? {
              relevance_floor: num(latest[1]),
              surface_budget: int(latest[2]),
              candidate_count: int(latest[3]),
              returned_count: int(latest[4]),
              at: int(latest[5]),
            }
          : null,
      },
      recall_moment: candidates.length ? { candidates } : null,
      honesty: {
        serves: {
          sent: serves || null,
          confirmed_on_chain: serveTally.submitted ?? null,
          rejected: serveTally.failed ?? null,
        },
      },
    },
  };
}
