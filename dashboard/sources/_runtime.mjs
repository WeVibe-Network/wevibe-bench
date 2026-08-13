// ─────────────────────────────────────────────────────────────────────────────
// SOURCE MODULE RUNTIME
//
// Every source module exports:  { id, describe(), fields, async read(ctx) }
//   read(ctx) -> { ok, patch, provenance, reason? }
//
// GUARANTEE: a source module cannot take the board down. Each read is wrapped
// in a timeout + try/catch. A module that throws, hangs, or finds no file is
// reported as `unwired` with a reason, and its fields simply stay null. That
// null is a designed UI state, not an error path.
//
// Reads are READ-ONLY and tail-bounded. Nothing here opens a file for write,
// and no module may read an unbounded log into memory.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { createReadStream } from "node:fs";
import { join } from "node:path";

/** Never read more than this from the tail of any log. */
export const TAIL_BYTES = 256 * 1024;

/** A single module read may not exceed this. */
export const READ_TIMEOUT_MS = 2000;

export async function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}: timed out after ${ms}ms`)), ms);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** Stat without throwing. Returns null when absent. */
export async function statOrNull(path) {
  try {
    return await fs.stat(path);
  } catch {
    return null;
  }
}

/** Read the last `bytes` of a file as utf8. Returns "" when absent. */
export async function readTail(path, bytes = TAIL_BYTES) {
  const st = await statOrNull(path);
  if (!st || !st.isFile()) return "";
  const start = Math.max(0, st.size - bytes);
  return await new Promise((resolve) => {
    let buf = "";
    const s = createReadStream(path, { start, encoding: "utf8" });
    s.on("data", (c) => {
      buf += c;
    });
    s.on("end", () => resolve(buf));
    s.on("error", () => resolve(""));
  });
}

/** Read a whole small file (configs, manifests). Returns null when absent. */
export async function readTextCapped(path, cap = 4 * 1024 * 1024) {
  const st = await statOrNull(path);
  if (!st || !st.isFile() || st.size > cap) return null;
  try {
    return await fs.readFile(path, "utf8");
  } catch {
    return null;
  }
}

export async function readJson(path) {
  const text = await readTextCapped(path);
  if (text === null) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** Parse JSON-lines tolerantly: a half-written last line is normal and skipped. */
export function parseJsonl(text) {
  const out = [];
  for (const line of String(text ?? "").split("\n")) {
    const t = line.trim();
    if (!t) continue;
    try {
      const v = JSON.parse(t);
      if (v && typeof v === "object") out.push(v);
    } catch {
      // a truncated head (tail read) or a half-flushed tail line. expected.
    }
  }
  return out;
}

export async function listDir(path) {
  try {
    return await fs.readdir(path, { withFileTypes: true });
  } catch {
    return [];
  }
}

// ── THE ACTIVE RUN ───────────────────────────────────────────────────────────
//
// ONE run is on screen at a time, and every source must agree on WHICH.
//
// This exists because the board previously had no such concept: each source
// globbed `<runs_root>/*/` independently and folded EVERY run directory it
// found into one picture. On a machine with abandoned runs on disk that
// produced a wall showing the UNION of gates across runs — measured live:
// 40 gates from the active run rendered red, plus 8 gates that existed only in
// two abandoned runs rendered "unobserved", on the SAME strip, as if they
// belonged to one cell. A viewer diffing that strip against the active run's
// log finds gates that are not in it.
//
// That is a correctness defect, not a cosmetic one. The wall's stated contract
// (wall.js) is that it is checkable line-by-line against the raw failed_gates
// list of the run being watched, and the arm delta counted abandoned runs'
// cells toward exclusions on an arm the operator is currently measuring.
//
// SELECTION RULE: the run whose MANIFEST DECLARES THE NEWEST created_at.
//
// ── THE DEFECT THIS REPLACES (measured, 2026-08-13) ─────────────────────────
//
// The rule used to be "the run with a status stream wins; break ties on
// mtime". It is exactly backwards for the real topology, and it made the board
// show TWO DIFFERENT CELLS AT ONCE for the first half-hour of every run:
//
//   · `manifest.status.jsonl` is appended AT ATTEMPT END — ~30 minutes in
//     (status-stream.mjs:18-20 states this cadence).
//   · So a freshly launched run has a manifest and NO status file → rank 0.
//   · Any abandoned run on disk has a status file → rank 1, and rank was
//     checked BEFORE mtime.
//   · Therefore the corpse outranked the live cell, unconditionally, until
//     the live cell's first attempt closed.
//
// Observed on the running board: `run-log` resolved the live cell
// (`off-cell-20260813T051334.log`, written seconds earlier) while
// `status-stream` — which feeds the GATE WALL and the arm delta — resolved
// `cumulative.void-truncation-orphan-contention-20260812T0253`, stale by 21
// hours. The wall's 23 gates belonged to a run the operator had abandoned.
//
// ── WHY created_at AND NOT mtime, AND NOT THE LAUNCH LOG ────────────────────
//
// mtime is wrong because it measures when a file was last TOUCHED, which an
// archive/rename or a stray read-modify can move. `created_at` is written once
// by the harness at run start and is the run's own statement of when it began.
//
// The launch log was the obvious alternative and is REJECTED on evidence: the
// log records an absolute path (`dst=…/runs/<dir>/sessions/…`), but runs are
// RENAMED WHEN ARCHIVED and the log keeps the original name. Measured: all
// three `off-cell-*.log` files on disk point at `runs/cumulative`, because each
// was the live `cumulative` when it ran. Joining through the log would resolve
// three different runs to one directory.
//
// Directory NAME remains deliberately unused: `cumulative.aborted-*` is a
// convention, not a guarantee, and a rule that depends on someone remembering
// to rename a directory fails silently the one time they don't.
//
// FALLBACK, IN ORDER. A manifest with no parseable `created_at` sorts below
// every run that declares one — it is never treated as epoch 0 in a way that
// could let an unparseable manifest win. When NEITHER run declares a start,
// the older rule is still the right one and is kept: a run carrying real
// attempt records outranks a bare directory, so a just-created empty manifest
// cannot steal the screen from a run that is mid-flight. mtime breaks the
// final tie. Both orderings are pinned by tests in arm-delta-validity.test.mjs.
//
// A source that legitimately spans runs must say so explicitly; only
// stack-ledger.mjs does, and it says so in its header. Cross-run aggregation is
// what this function exists to prevent.
export async function activeRun(runsRoot) {
  let best = null;
  for (const ent of await listDir(runsRoot)) {
    if (!ent.isDirectory()) continue;
    const dir = join(runsRoot, ent.name);
    const status = await statOrNull(join(dir, "manifest.status.jsonl"));
    const manifest = await statOrNull(join(dir, "manifest.json"));
    if (!status?.isFile() && !manifest?.isFile()) continue;

    // The run's own declared start. null when absent or unparseable — such a
    // run is outranked by any run that declares one, rather than defaulting to
    // a number that could win.
    const declared = manifest?.isFile()
      ? (Date.parse(String((await readJson(join(dir, "manifest.json")))?.created_at ?? "")) || null)
      : null;
    const mtime = Math.max(status?.mtimeMs ?? 0, manifest?.mtimeMs ?? 0);

    const candidate = {
      name: ent.name,
      dir,
      declared,
      // Carries real attempt records. Only consulted when NEITHER run declares
      // a start — never as the primary key, which is the defect above.
      rank: status?.isFile() ? 1 : 0,
      mtime,
      statusPath: status?.isFile() ? join(dir, "manifest.status.jsonl") : null,
      statusStat: status ?? null,
      manifestPath: manifest?.isFile() ? join(dir, "manifest.json") : null,
      manifestStat: manifest ?? null,
    };

    if (!best || newerRun(candidate, best)) best = candidate;
  }
  return best;
}

/**
 * Is `a` the more current run than `b`?
 *
 * Declared start first (the run's own statement of when it began), then — only
 * when neither declares — presence of attempt data, then mtime.
 */
function newerRun(a, b) {
  if (a.declared !== null && b.declared !== null) {
    return a.declared !== b.declared ? a.declared > b.declared : a.mtime > b.mtime;
  }
  if (a.declared !== null) return true; // a declares, b does not
  if (b.declared !== null) return false; // b declares, a does not
  if (a.rank !== b.rank) return a.rank > b.rank; // data beats a bare directory
  return a.mtime > b.mtime;
}

/**
 * Run one module with full isolation. Never throws.
 */
export async function runSource(mod, ctx) {
  const started = Date.now();
  try {
    const res = await withTimeout(
      Promise.resolve(mod.read(ctx)),
      READ_TIMEOUT_MS,
      mod.id,
    );
    const ok = Boolean(res && res.ok);
    return {
      id: mod.id,
      ok,
      fields: mod.fields ?? [],
      reason: ok ? null : (res?.reason ?? "no data"),
      provenance: res?.provenance ?? null,
      patch: ok ? (res.patch ?? {}) : {},
      ms: Date.now() - started,
    };
  } catch (err) {
    return {
      id: mod.id,
      ok: false,
      fields: mod.fields ?? [],
      reason: String(err?.message ?? err).slice(0, 200),
      provenance: null,
      patch: {},
      ms: Date.now() - started,
    };
  }
}

/**
 * Deep-merge a module patch into the board.
 *
 * MERGE RULE: null never overwrites a non-null value. Modules are additive —
 * a module that has no opinion about a field leaves it exactly as it was, so
 * enabling a new source can only ever ADD information to the board.
 * Arrays replace wholesale (they are owned by exactly one module).
 */
export function mergePatch(target, patch) {
  if (!patch || typeof patch !== "object") return target;
  for (const [k, v] of Object.entries(patch)) {
    if (v === null || v === undefined) continue;
    if (Array.isArray(v)) {
      target[k] = v;
    } else if (typeof v === "object" && typeof target[k] === "object" && target[k] !== null && !Array.isArray(target[k])) {
      mergePatch(target[k], v);
    } else {
      target[k] = v;
    }
  }
  return target;
}
