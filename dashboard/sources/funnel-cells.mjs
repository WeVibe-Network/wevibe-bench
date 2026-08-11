// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: funnel-cells
//
// The recall funnel counters the agent plugin writes per session, exported
// host-side before container teardown into:
//
//   <bench_root>/data/cells/<unix_ts>-<run_label>/funnel-snapshot.json
//
// Shape: { "<sessionId>": FunnelCounters, ... } — counts and ms ONLY. The
// plugin is explicit that this file never carries secrets or plaintext memory
// content, which is why it is safe to render on a public stream.
//
// FunnelCounters fields (verified against plugins/funnel-counters.ts):
//   episode_opened, episode_armed, recall_fired, gate_shown, gate_decided,
//   serve_sent, serve_rejected, confirmed_on_chain, gate_decision_ms,
//   predicate_mode, distinct_failure_keys
//
// ARM ASYMMETRY (by construction, not a bug): on an OFF cell the harness
// deletes the worktree `.wevibe` directory, so this file never exists. Absence
// here on a control cell is the CORRECT state and the UI says so in words.
//
// WHAT THIS PROVES AND WHAT IT DOESN'T:
//   serve_sent            — delivery. NOT a win.
//   episode_armed         — a second failure under one key. the trigger beat.
//   gate_decision_ms      — how long the approval gate took. in bench mode it
//                           auto-approves, so a tiny value here is EXPECTED and
//                           must never be presented as a human decision.
// ─────────────────────────────────────────────────────────────────────────────

import { join } from "node:path";
import { int, str } from "../contract.mjs";
import { readJson, listDir, statOrNull } from "./_runtime.mjs";

export const id = "funnel-cells";
export const fields = ["honesty.serves", "honesty.coverage", "honesty.wasted_turns", "funnel"];
export function describe() {
  return "plugin funnel counters per cell (ON cells only; absent on control by construction)";
}

const NUMERIC = [
  "episode_opened",
  "episode_armed",
  "recall_fired",
  "gate_shown",
  "gate_decided",
  "serve_sent",
  "serve_rejected",
  "confirmed_on_chain",
  "distinct_failure_keys",
];

export async function read(ctx) {
  const cellsDir = join(ctx.benchRoot, "data", "cells");
  const entries = [];
  for (const ent of await listDir(cellsDir)) {
    if (!ent.isDirectory()) continue;
    const p = join(cellsDir, ent.name, "funnel-snapshot.json");
    const st = await statOrNull(p);
    if (st?.isFile()) entries.push({ name: ent.name, path: p, mtime: st.mtimeMs, size: st.size });
  }

  if (!entries.length) {
    return {
      ok: false,
      reason: "no funnel snapshot yet — written by ON cells only (control cells have no plugin state by construction)",
    };
  }

  entries.sort((a, b) => b.mtime - a.mtime);

  const totals = Object.fromEntries(NUMERIC.map((k) => [k, 0]));
  const gateMs = [];
  let predicateMode = null;
  let sessions = 0;

  for (const e of entries) {
    const snap = await readJson(e.path);
    if (!snap || typeof snap !== "object") continue;
    for (const counters of Object.values(snap)) {
      if (!counters || typeof counters !== "object") continue;
      sessions += 1;
      for (const k of NUMERIC) totals[k] += int(counters[k]) ?? 0;
      const ms = int(counters.gate_decision_ms);
      if (ms !== null) gateMs.push(ms);
      predicateMode = str(counters.predicate_mode) ?? predicateMode;
    }
  }

  if (!sessions) {
    return { ok: false, reason: "funnel snapshot present but carries no sessions" };
  }

  // Coverage: episodes that reached an observable conclusion. An episode that
  // armed but whose gate never decided has NOT concluded — it counts as
  // neither positive nor negative, which is the whole point of the panel.
  const concluded = totals.gate_decided;
  const total = totals.episode_opened;

  return {
    ok: true,
    provenance: { path: entries[0].path, mtime: entries[0].mtime, bytes: entries[0].size },
    patch: {
      funnel: {
        ...totals,
        predicate_mode: predicateMode,
        gate_decision_ms_samples: gateMs,
        sessions,
      },
      honesty: {
        serves: {
          sent: totals.serve_sent,
          rejected: totals.serve_rejected,
          confirmed_on_chain: totals.confirmed_on_chain,
        },
        coverage: { concluded, total },
        // Wasted turns = the honest cost of the gated trigger. Episodes that
        // opened but never armed burned turns before any recall could fire.
        wasted_turns: Math.max(0, totals.episode_opened - totals.episode_armed),
      },
    },
  };
}
