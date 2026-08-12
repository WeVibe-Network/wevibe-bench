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
// SELECTION RULE: the run directory whose status stream was written most
// recently, falling back to the most recent manifest for a run that has not
// yet appended an attempt record. Recency is the only signal on disk that
// tracks the operator's actual intent — the harness writes to the live run and
// nothing else, and an abandoned run's files stop changing the moment it is
// abandoned. Directory NAME is deliberately not used: `cumulative.aborted-*`
// is a convention, not a guarantee, and a rule that depends on someone
// remembering to rename a directory fails silently the one time they don't.
//
// A source that legitimately spans runs must say so explicitly; nothing does
// today. Cross-run aggregation is what this function exists to prevent.
export async function activeRun(runsRoot) {
  let best = null;
  for (const ent of await listDir(runsRoot)) {
    if (!ent.isDirectory()) continue;
    const dir = join(runsRoot, ent.name);
    const status = await statOrNull(join(dir, "manifest.status.jsonl"));
    const manifest = await statOrNull(join(dir, "manifest.json"));
    if (!status?.isFile() && !manifest?.isFile()) continue;

    // A run that has appended an attempt record always outranks one that has
    // only a manifest, regardless of clock: the manifest is written once at run
    // start, so a fresh manifest beside a live status stream would otherwise let
    // a just-created run steal the screen from the one actually producing data.
    const rank = status?.isFile() ? 1 : 0;
    const mtime = Math.max(status?.mtimeMs ?? 0, manifest?.mtimeMs ?? 0);
    if (!best || rank > best.rank || (rank === best.rank && mtime > best.mtime)) {
      best = {
        name: ent.name,
        dir,
        rank,
        mtime,
        statusPath: status?.isFile() ? join(dir, "manifest.status.jsonl") : null,
        statusStat: status ?? null,
        manifestPath: manifest?.isFile() ? join(dir, "manifest.json") : null,
        manifestStat: manifest ?? null,
      };
    }
  }
  return best;
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
