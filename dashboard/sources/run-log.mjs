// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: run-log
//
// The LIVE PULSE. The status stream is authoritative but only lands at attempt
// end (~30 min); this parses the runner's PROGRESS lines from the launch log,
// which land every few minutes. Without this the board looks frozen for half an
// hour at a time, and a frozen board on a stream reads as broken.
//
// Real lines observed on disk (post WO-NUDGE-INF-1, backgammon.py:2905-2913):
//   PROGRESS ... step=serve-drive-end phase=initial-chunk-5
//            turns=38 guard_aborted_turns=0 finalize_timeout_turns=1
//            recovery_nudges=2 session_turns=131 input=0 output=28080 ...
//   PROGRESS ... step=chunk-compaction chunk=5 action=backstop ...
//   PROGRESS ... step=transport-recovery phase=initial-chunk-6
//            terminal=transport_error action=nudge ...
//   PROGRESS ... step=memory-mode mode=off pure=true
//
// ── THE TURN-COUNT DISTINCTION (WO-NUDGE-INF-1) ──────────────────────────────
// `turns=` is SCORING turns: raw turns minus guard-killed minus finalize-killed
//   (backgammon.py: scoring_turns = d_turns - d_guard_aborted - d_finalize_timeouts)
// `session_turns=` is the RAW count, inflated by every recovered turn.
//
// The board displays SCORING turns. Showing session_turns would inflate the
// measurement by exactly the recovered turns the harness deliberately excludes —
// the defect class WO-NUDGE-INF-1 fixed. The raw count is carried separately as
// an anomaly figure, never as the measurement.
//
// ── WHY recovery_nudges IS ON THE BOARD ──────────────────────────────────────
// Nudges are now UNBOUNDED. The report's own named, accepted failure mode: a
// permanently wedged relay no longer self-terminates, and hang detection "rests
// entirely on the operator watching the status stream." A climbing nudge count
// with a phase that never advances IS the wedge signature, and nothing else on
// the board would reveal it.
//
// Tail-bounded: only the last TAIL_BYTES are parsed, so a multi-hour log costs
// the same as a fresh one.
// ─────────────────────────────────────────────────────────────────────────────

import { join } from "node:path";
import { int, str } from "../contract.mjs";
import { readTail, listDir, statOrNull } from "./_runtime.mjs";

export const id = "run-log";
export const fields = ["run.phase", "run.chunk", "run.turns", "run.state", "run.elapsed_s"];
export function describe() {
  return "runner PROGRESS lines — the live pulse between attempt records";
}

const KV = /(\w+)=([^\s]+)/g;

function parseKV(line) {
  const out = {};
  let m;
  KV.lastIndex = 0;
  while ((m = KV.exec(line))) out[m[1]] = m[2];
  return out;
}

async function newestLog(runsRoot) {
  let best = null;
  for (const ent of await listDir(runsRoot)) {
    if (!ent.isFile() || !ent.name.endsWith(".log")) continue;
    if (!/^(off|on)-cell-|^cell-/.test(ent.name)) continue;
    const p = join(runsRoot, ent.name);
    const st = await statOrNull(p);
    if (st?.isFile() && (!best || st.mtimeMs > best.mtime)) {
      best = { path: p, mtime: st.mtimeMs, size: st.size, name: ent.name };
    }
  }
  return best;
}

/** "initial-chunk-5" -> 5 ; "feedback-2" -> null (no longer a build chunk) */
function chunkOf(phase) {
  const m = /^initial-chunk-(\d+)$/.exec(phase ?? "");
  return m ? Number(m[1]) : null;
}

export async function read(ctx) {
  const log = await newestLog(ctx.runsRoot);
  if (!log) return { ok: false, reason: "no cell launch log under runs root" };

  const text = await readTail(log.path);
  const lines = text.split("\n").filter((l) => l.includes("PROGRESS"));
  if (!lines.length) {
    return { ok: false, reason: "log present but carries no PROGRESS lines yet" };
  }

  let phase = null;
  let chunk = null;
  let sessionTurns = null; // raw, inflated by recovered turns — never the measurement
  let mode = null;
  let recoveries = 0;
  let terminal = null;

  // ── DEDUPE (measured, not assumed) ──────────────────────────────────────
  // Every PROGRESS line is emitted TWICE: once through the structured logger
  // (`run_cumulative.progress ...`) and once bare. Verified on disk — each
  // `step=serve-drive-end` payload appears exactly 2×. Naive accumulation
  // therefore doubles every per-phase delta (observed: 316 scoring turns
  // against 163 raw session turns, which is impossible by construction since
  // scoring turns are a SUBSET of raw turns).
  //
  // Deltas are keyed by phase so each phase contributes exactly once. Keying by
  // phase (not by whole-line identity) is also robust to the two emissions
  // being formatted differently.
  const phaseDeltas = new Map();

  for (const line of lines) {
    const kv = parseKV(line);
    const step = str(kv.step);

    // STEP-SCOPED PARSING. A blanket key match is wrong: `mode=` also appears on
    // backend lines as `mode=real` (the recall backend), and `phase=` appears as
    // `phase=entry` / `phase=owned_org_resolved` during org bootstrap. Only the
    // steps named below define these fields.
    switch (step) {
      case "memory-mode":
        mode = str(kv.mode); // "on" | "off" — the arm
        break;
      case "serve-drive-end": {
        const ph = str(kv.phase) ?? "(unnamed)";
        phase = ph;
        const c = chunkOf(kv.phase);
        if (c !== null) chunk = c;
        // Last write wins per phase — identical across the duplicate emissions.
        phaseDeltas.set(ph, {
          turns: int(kv.turns) ?? 0,
          guard: int(kv.guard_aborted_turns) ?? 0,
          // absent on logs written before WO-NUDGE-INF-1 — stays 0, never null
          finalize: int(kv.finalize_timeout_turns) ?? 0,
          nudges: int(kv.recovery_nudges) ?? 0,
        });
        // session_turns is CUMULATIVE for the cell, not a delta — take the last.
        sessionTurns = int(kv.session_turns) ?? sessionTurns;
        break;
      }
      case "serve-drive-start":
      case "transport-recovery": {
        const c = chunkOf(kv.phase);
        if (kv.phase) phase = str(kv.phase);
        if (c !== null) chunk = c;
        if (step === "transport-recovery") recoveries += 1;
        break;
      }
      case "chunk-compaction":
        chunk = int(kv.chunk) ?? chunk;
        break;
      default:
        break;
    }
  }

  let scoringTurns = null;
  let guardAborted = 0;
  let finalizeTurns = 0;
  let nudges = 0;
  for (const d of phaseDeltas.values()) {
    scoringTurns = (scoringTurns ?? 0) + d.turns;
    guardAborted += d.guard;
    finalizeTurns += d.finalize;
    nudges += d.nudges;
  }
  // transport-recovery lines are duplicated too.
  recoveries = Math.round(recoveries / 2);

  // Terminal state: the runner writes a bare JSON status object as its last
  // line when the cell stops. `awaiting_extract` means the cell finished and
  // extraction is the next (separate) invocation.
  for (const raw of text.split("\n").slice(-6)) {
    const t = raw.trim();
    if (!t.startsWith("{") || !t.endsWith("}")) continue;
    try {
      const o = JSON.parse(t);
      if (typeof o?.status === "string") terminal = o.status;
      if (typeof o?.memory_mode === "string") mode = o.memory_mode;
    } catch {
      /* not a status object */
    }
  }

  // ── SILENCE, MEASURED WITHOUT PARSING A TIMESTAMP ───────────────────────
  // "silent" is a real state: the runner publishes progress continuously, so a
  // long silence is information. With nudges now unbounded, silence + a
  // climbing nudge count is the wedged-relay signature.
  //
  // It is derived from the log file's MTIME, not from the timestamp text in the
  // last line. The harness writes NAIVE local timestamps ("2026-08-11 15:26:00"
  // with no offset), and `Date.parse` resolves those against the READER's
  // timezone. The dashboard container runs UTC while the harness writes host
  // local time, so parsing produced a CONSTANT phantom silence equal to the
  // UTC offset — measured at 25560s (7.1h) on a log written seconds earlier,
  // which pinned the panel's `EXCEEDS 900s` alarm on for every run and made a
  // genuine stall indistinguishable from the bug.
  //
  // mtime is an absolute epoch from the filesystem, so it carries no timezone
  // ambiguity and needs no agreement between writer and reader. It measures
  // exactly the thing the panel claims: how long since the runner last wrote.
  const silentFor = Math.max(0, Math.round((Date.now() - log.mtime) / 1000));

  return {
    ok: true,
    provenance: { path: log.path, mtime: log.mtime, bytes: log.size },
    patch: {
      run: {
        phase,
        chunk: { current: chunk, total: 6 },
        turns: scoringTurns, // SCORING turns. never session_turns.
        session_turns: sessionTurns, // raw, carried for the anomaly rail only
        arm: mode,
        state: terminal ? "complete" : "running",
        terminal_status: terminal,
        log_silent_s: silentFor,
      },
      honesty: {
        transport: {
          // Post WO-NUDGE-INF-1 these are RECOVERED, not fatal. A finalize
          // timeout used to kill a run at budget; now it is nudged
          // indefinitely and excluded from scoring. The label must not read
          // as alarm when it is the system working as designed.
          guard_aborts: guardAborted,
          finalize_timeout_turns: finalizeTurns,
          recovery_nudges: nudges,
          recoveries,
        },
        // The honest cost of recovery: turns that really happened, burned real
        // tokens, and are correctly excluded from the measurement.
        recovered_turns: guardAborted + finalizeTurns,
      },
    },
  };
}
