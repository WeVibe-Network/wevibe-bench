// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: plugin-log
//
// The agent plugin's own error/info log, exported host-side alongside the
// funnel snapshot:
//
//   <bench_root>/data/cells/<unix_ts>-<run_label>/plugin-errors.log
//
// This is the ONLY place recall latency is measured. Verified line shapes
// (wevibe-plugin.ts):
//
//   recall_returned status=ok count=3 reason_code=... dur_ms=340 error=...
//   recall_fired trigger=repeat_failure sid=ses_...
//
// RECALL OVERHEAD is a hard measured seam, not a budget with an escape hatch —
// a run launched with an unmeasured latency seam is VOID-INSTRUMENT. So p50/p95
// stay NULL when unmeasured; we never synthesize a plausible number.
//
// PRIVACY: this module extracts counters, status codes and durations only. It
// never forwards log prose to the board, because a plugin log line can contain
// an error string from the agent's own workspace and everything rendered here
// is public forever.
// ─────────────────────────────────────────────────────────────────────────────

import { join } from "node:path";
import { int, str, percentiles } from "../contract.mjs";
import { readTail, listDir, statOrNull } from "./_runtime.mjs";

export const id = "plugin-log";
export const fields = ["honesty.recall_latency_ms", "honesty.guard_detections", "recall_stats"];
export function describe() {
  return "plugin log — recall latency (p50/p95), fire/return counts, guard detections";
}

export async function read(ctx) {
  const cellsDir = join(ctx.benchRoot, "data", "cells");
  const logs = [];
  for (const ent of await listDir(cellsDir)) {
    if (!ent.isDirectory()) continue;
    const p = join(cellsDir, ent.name, "plugin-errors.log");
    const st = await statOrNull(p);
    if (st?.isFile()) logs.push({ path: p, mtime: st.mtimeMs, size: st.size });
  }

  if (!logs.length) {
    return {
      ok: false,
      reason: "no plugin log yet — ON cells only; recall latency stays unmeasured (never synthesized)",
    };
  }

  logs.sort((a, b) => b.mtime - a.mtime);

  const durations = [];
  const guard = {};
  let fired = 0;
  let returned = 0;
  let returnedCountSum = 0;
  const reasons = {};

  for (const l of logs) {
    for (const line of (await readTail(l.path)).split("\n")) {
      if (line.includes("recall_fired")) fired += 1;

      if (line.includes("recall_returned")) {
        returned += 1;
        const dur = int(/dur_ms=(\d+)/.exec(line)?.[1]);
        if (dur !== null) durations.push(dur);
        const count = int(/count=(\d+)/.exec(line)?.[1]);
        if (count !== null) returnedCountSum += count;
        const reason = str(/reason_code=([^\s]+)/.exec(line)?.[1]);
        if (reason && reason !== "null") reasons[reason] = (reasons[reason] ?? 0) + 1;
      }

      // guard detections are named rule hits; count by type, never echo content
      const det = /guard[_ ]detection[= ]([A-Za-z0-9_.-]+)/.exec(line);
      if (det) guard[det[1]] = (guard[det[1]] ?? 0) + 1;
    }
  }

  if (!fired && !returned && !durations.length) {
    return { ok: false, reason: "plugin log present but no recall activity recorded" };
  }

  return {
    ok: true,
    provenance: { path: logs[0].path, mtime: logs[0].mtime, bytes: logs[0].size },
    patch: {
      recall_stats: {
        fired,
        returned,
        returned_count_sum: returnedCountSum,
        reason_codes: reasons,
      },
      honesty: {
        recall_latency_ms: percentiles(durations),
        guard_detections: guard,
      },
    },
  };
}
