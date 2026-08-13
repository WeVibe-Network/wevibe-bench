// ─────────────────────────────────────────────────────────────────────────────
// HARNESS EVENTS — grading progress, tailed from the run log
//
// WHY THIS EXISTS (WO-GRADE-VIS-1)
//
// The event feed shows what the AGENT does, via the worker's `opencode serve`
// SSE stream. But grading is HARNESS work, and while it runs the agent is idle
// by design — so the feed correctly goes quiet and the board becomes
// indistinguishable from a wedged one.
//
// Measured 2026-08-12: an attempt-3 gate ran 1918s (~32 min) against a 45s/113s
// baseline. For that entire window the gate log did not exist (the harness
// buffered gate output until exit) and the event feed was silent. The operator
// had no way to tell "grading" from "stalled" without an agent inspecting
// process stacks by hand. That is the gap this module closes.
//
// It reads the harness's OWN published PROGRESS lines — the same channel the
// dashboard's run-log source already parses — and republishes the grading ones
// as feed rows. It does not invent a second telemetry channel, and it never
// talks to the worker.
//
// READ-ONLY: tails a log file. Never writes, never spawns, never signals.
// ─────────────────────────────────────────────────────────────────────────────

import { GATE_STALL_THRESHOLD_S } from "./contract.mjs";
import { newestLog, readTail } from "./runstate.mjs";

/** Parse `k=v` pairs out of a PROGRESS line. */
function parseKV(line) {
  const out = {};
  const re = /(\w+)=([^\s]+)/g;
  let m;
  while ((m = re.exec(line))) out[m[1]] = m[2];
  return out;
}

/**
 * Extract grading events from run-log text, in order.
 *
 * DEDUPE: every PROGRESS line is emitted TWICE by the harness (once through the
 * structured logger, once bare) — verified on disk, and the reason the
 * dashboard's run-log source keys its deltas by phase. Here the whole line
 * (minus its timestamp) is the key, so each real event yields exactly one row
 * instead of a visibly doubled feed.
 *
 * Returns rows in the BoardEvent shape used by EventRing, with `kind:"harness"`.
 */
export function parseGateEvents(text) {
  const rows = [];
  const seen = new Set();

  for (const raw of String(text ?? "").split("\n")) {
    if (!raw.includes("step=gate-")) continue;
    const kv = parseKV(raw);
    const step = kv.step;
    if (!step || !step.startsWith("gate-")) continue;

    // Identity excludes the leading timestamp so the duplicate pair collapses.
    const idx = raw.indexOf("PROGRESS");
    const key = idx >= 0 ? raw.slice(idx).trim() : raw.trim();
    if (seen.has(key)) continue;
    seen.add(key);

    rows.push(rowFor(step, kv));
  }
  return rows.filter(Boolean);
}

function rowFor(step, kv) {
  const base = {
    id: null,
    kind: "harness",
    type: `harness:${step}`,
    at: null,
    session_id: null,
    tool: null,
    file: null,
    name: null,
    detail: null,
    text: null,
    truncated: false,
    phase: kv.phase ?? null,
  };

  switch (step) {
    case "gate-attempt-start":
      base.name = "grading";
      base.detail = `attempt ${kv.attempt ?? "?"} — grading started`;
      return base;

    case "gate-phase-start":
      base.name = `gate:${kv.phase ?? "?"}`;
      base.detail = `${kv.phase ?? "?"} running`;
      return base;

    case "gate-phase-end": {
      // A failing gate phase is a normal, expected measurement outcome — the
      // whole point of the benchmark is that gates fail. It must NEVER be
      // rendered as an error, or a working run looks broken.
      base.name = `gate:${kv.phase ?? "?"}`;
      const problems = kv.problems && kv.problems !== "unknown" ? ` · ${kv.problems} problems` : "";
      base.detail = `${kv.phase ?? "?"} ${kv.status ?? "done"}${problems}`;
      return base;
    }

    case "gate-timeout":
      // The one genuine error in this family: the gate was KILLED and the
      // attempt was never graded.
      base.kind = "error";
      base.name = "gate timeout";
      base.detail = `gate killed after ${kv.wall_s ?? "?"}s (limit ${kv.limit_s ?? "?"}s) — attempt not graded`;
      base.text = base.detail;
      return base;

    default:
      return null;
  }
}

/**
 * Current grading status derived from the ordered event rows.
 *
 * `active` is true when a phase has STARTED and not yet ENDED. That pairing is
 * what makes an in-phase hang visible: phase markers alone would show the last
 * phase that began and look identical whether it finished or not.
 *
 * Elapsed is measured from the gate log's MTIME, never from a parsed timestamp
 * — the harness writes naive local timestamps and `Date.parse` resolves them
 * against the READER's timezone, which produced a constant phantom 7.1h silence
 * in a UTC container (see contract.mjs STALL_THRESHOLD_S). mtime is an absolute
 * epoch and needs no agreement between writer and reader.
 */
export function gradingStatus(rows, { logMtimeMs = null, now = Date.now() } = {}) {
  let phase = null;
  let active = false;
  let attempt = null;
  let timedOut = false;

  // ── PER-PHASE RESULTS (added 2026-08-13) ────────────────────────────────
  //
  // THE GAP THIS FILLS: the harness publishes a real, quantified result for
  // every grading phase as it completes —
  //   step=gate-phase-end phase=conformance status=fail problems=3
  //   step=gate-phase-end phase=backend     status=fail problems=2
  // — but the only consumer of gate data on the board was `wall.gates`, which
  // is built from manifest.status.jsonl and is written ONLY AT ATTEMPT END.
  //
  // The consequence the operator hit: grading ran for minutes, three phases
  // reported real failures, and the GATE WALL sat at zero gates showing "the
  // grader has not run yet" — which was false, and unresponsive-looking,
  // precisely when there was something to watch.
  //
  // These are NOT gate identities and must never be rendered as squares: a
  // phase problem count is a different measurement from a named failing gate,
  // and conflating them would invent gates that do not exist. They are carried
  // as their own list so the panel can state what the grader has found SO FAR,
  // clearly labelled as in-flight and provisional.
  const phases = [];

  for (const r of rows) {
    if (r.type === "harness:gate-attempt-start") {
      attempt = r.detail?.match(/attempt (\S+)/)?.[1] ?? attempt;
      phase = null;
      active = true;
      // A new attempt re-grades from scratch; last attempt's phase results are
      // not this attempt's.
      phases.length = 0;
    } else if (r.type === "harness:gate-phase-start") {
      phase = r.phase;
      active = true;
      if (r.phase && !phases.some((p) => p.phase === r.phase)) {
        phases.push({ phase: r.phase, status: null, problems: null, running: true });
      }
    } else if (r.type === "harness:gate-phase-end") {
      phase = r.phase;
      active = false;
      // `detail` is composed as "<phase> <status> · <n> problems" by rowFor().
      const st = r.detail?.match(/^\S+\s+(\S+)/)?.[1] ?? null;
      const probs = r.detail?.match(/·\s*(\d+)\s+problems/)?.[1] ?? null;
      const existing = phases.find((p) => p.phase === r.phase);
      const done = {
        phase: r.phase,
        status: st,
        problems: probs === null ? null : Number(probs),
        running: false,
      };
      if (existing) Object.assign(existing, done);
      else phases.push(done);
    } else if (r.type === "harness:gate-timeout") {
      timedOut = true;
      active = false;
      const open = phases.find((p) => p.running);
      if (open) {
        open.running = false;
        open.status = "timeout";
      }
    }
  }

  const elapsed =
    active && logMtimeMs ? Math.max(0, Math.round((now - logMtimeMs) / 1000)) : null;

  return {
    grading: active,
    phase,
    attempt,
    timed_out: timedOut,
    // Seconds since the gate log was last written while a phase is open.
    silent_s: elapsed,
    stall_threshold_s: GATE_STALL_THRESHOLD_S,
    // The operator-facing verdict. Deliberately a derived boolean rather than a
    // colour: the panel decides presentation, this decides truth.
    stalled: elapsed !== null && elapsed >= GATE_STALL_THRESHOLD_S,
    // Per-phase progress. PROVISIONAL BY CONSTRUCTION — the authoritative gate
    // list still lands in manifest.status.jsonl at attempt end.
    phases,
  };
}

/**
 * Read grading events + status for the active run.
 *
 * Tail-bounded like every other reader here: a multi-hour log costs the same as
 * a fresh one.
 */
export async function readGateActivity(runsRoot) {
  const log = await newestLog(runsRoot);
  if (!log) return { rows: [], status: null, log: null };

  const text = await readTail(log.path);
  const rows = parseGateEvents(text);
  const status = gradingStatus(rows, { logMtimeMs: log.mtime });
  return { rows, status, log };
}
