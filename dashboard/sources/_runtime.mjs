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
