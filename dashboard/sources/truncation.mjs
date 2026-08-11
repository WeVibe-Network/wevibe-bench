// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: truncation
//
// Per-turn transport anomalies, written live during the cell:
//
//   <runs_root>/<run_dir>/sessions/<run_label>/truncation-evidence.jsonl
//
// Verified record shape on disk:
//   { attempt_id, run_label, phase, terminal, reason, ts_start_epoch_ms,
//     ts_end_epoch_ms, session_id, finish_reason, output_tokens_received, ... }
//
// terminal ∈ { transport_error, truncated_no_signal, ... }
// reason   ∈ { stream_finalize_timeout, stream-incomplete, ... }
//
// WHY THIS IS ON THE BOARD: a truncated turn is a VOID-INSTRUMENT class, never
// scored as a capability failure. Showing the anomaly count is what stops a
// viewer from reading a transport failure as "the model is bad" — and it is the
// panel that proves the instrument is being watched rather than flattered.
// ─────────────────────────────────────────────────────────────────────────────

import { join } from "node:path";
import { str } from "../contract.mjs";
import { readTail, parseJsonl, listDir, statOrNull } from "./_runtime.mjs";

export const id = "truncation";
export const fields = ["honesty.transport"];
export function describe() {
  return "per-turn transport anomalies (truncations, finalize timeouts) — VOID-INSTRUMENT class";
}

async function findEvidence(runsRoot) {
  const found = [];
  for (const runDir of await listDir(runsRoot)) {
    if (!runDir.isDirectory()) continue;
    const sessionsDir = join(runsRoot, runDir.name, "sessions");
    for (const sess of await listDir(sessionsDir)) {
      if (!sess.isDirectory()) continue;
      const p = join(sessionsDir, sess.name, "truncation-evidence.jsonl");
      const st = await statOrNull(p);
      if (st?.isFile()) found.push({ path: p, mtime: st.mtimeMs, size: st.size });
    }
  }
  return found.sort((a, b) => b.mtime - a.mtime);
}

export async function read(ctx) {
  const files = await findEvidence(ctx.runsRoot);
  if (!files.length) {
    return { ok: false, reason: "no truncation evidence file yet" };
  }

  let truncations = 0;
  let finalizeTimeouts = 0;
  const byPhase = {};
  let lastAt = null;

  for (const f of files) {
    for (const rec of parseJsonl(await readTail(f.path))) {
      const terminal = str(rec.terminal);
      const reason = str(rec.reason);
      if (terminal === "truncated_no_signal") truncations += 1;
      if (reason === "stream_finalize_timeout") finalizeTimeouts += 1;
      const phase = str(rec.phase);
      if (phase) byPhase[phase] = (byPhase[phase] ?? 0) + 1;
      const end = Number(rec.ts_end_epoch_ms);
      if (Number.isFinite(end) && (lastAt === null || end > lastAt)) lastAt = end;
    }
  }

  return {
    ok: true,
    provenance: { path: files[0].path, mtime: files[0].mtime, bytes: files[0].size },
    patch: {
      honesty: {
        transport: {
          truncations,
          finalize_timeouts: finalizeTimeouts,
          by_phase: byPhase,
          last_at: lastAt,
        },
      },
    },
  };
}
