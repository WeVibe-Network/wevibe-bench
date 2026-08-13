// ─────────────────────────────────────────────────────────────────────────────
// GRADED TEXT — what the model was ACTUALLY told, verbatim.
//
// WHY THIS EXISTS (WO-FEEDBACK-1)
//
// The grader produces per-gate results; the harness renders those into prose
// and hands it to the model as if a user had typed it. Until now that prose
// existed in exactly one place — a sidecar file inside the cell's session dir —
// and no surface read it. The operator could see that a message was sent
// (`step=user-event-sidecar chars=1287 text_fp=c02b9470`) but never WHAT was
// sent, so the one thing that actually steers the benchmarked model was the one
// thing nobody could read.
//
// VERBATIM OR NOTHING. This module does not summarise, re-wrap, re-render or
// "clean up" the text. It carries the exact bytes the model received, because
// the entire point is to judge whether those bytes read like a person wrote
// them. A surface that prettified them would be answering a different question.
//
// READ-ONLY: reads append-only JSONL. Never writes, never spawns, never signals.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { join } from "node:path";

import { resolveRunDir } from "./wall.mjs";

/** The contract version the board can assert against. */
export const FEEDBACK_CONTRACT_VERSION = 1;

/**
 * The three voices the harness speaks in.
 *
 * A task prompt and a failure report are not the same kind of message and must
 * not render identically — an operator scanning the feed needs to see at a
 * glance which is which.
 */
export const FEEDBACK_KINDS = /** @type {const} */ (["chunk", "pass_verdict", "feedback"]);

/** Messages are large (a chunk prompt ran 33KB); bound the read like every other. */
const DEFAULT_BYTES = 2 * 1024 * 1024;

async function statOrNull(path) {
  try {
    return await fs.stat(path);
  } catch {
    return null;
  }
}

async function listDir(path) {
  try {
    return await fs.readdir(path, { withFileTypes: true });
  } catch {
    return [];
  }
}

/**
 * Read one sidecar file into message records.
 *
 * A truncated leading line is dropped (it is a fragment of a record whose whole
 * content is unknown) and an unparseable line is skipped rather than aborting —
 * a run that died mid-write must still yield every intact message before it.
 */
export async function readSidecar(path, { bytes = DEFAULT_BYTES } = {}) {
  const st = await statOrNull(path);
  if (!st?.isFile()) return null;

  let text;
  const fh = await fs.open(path, "r").catch(() => null);
  if (!fh) return null;
  try {
    const start = Math.max(0, st.size - bytes);
    const len = st.size - start;
    const buf = Buffer.alloc(len);
    await fh.read(buf, 0, len, start);
    text = buf.toString("utf8");
    if (start > 0) text = text.slice(text.indexOf("\n") + 1);
  } catch {
    return null;
  } finally {
    await fh.close().catch(() => {});
  }

  const out = [];
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t.startsWith("{")) continue;
    let record;
    try {
      record = JSON.parse(t);
    } catch {
      continue;
    }
    if (typeof record?.text !== "string") continue;
    out.push(record);
  }
  return out;
}

/**
 * Normalise a sidecar record into the served shape.
 *
 * `kind` defaults to "feedback" ONLY when absent, and the default is reported
 * via `kind_inferred` rather than silently asserted — records written before
 * the field existed are real data and must not be relabelled as if the writer
 * had stated a kind it never stated.
 */
export function normalizeMessage(record, index) {
  const kind = typeof record.kind === "string" && record.kind ? record.kind : null;
  const text = String(record.text ?? "");
  return {
    seq: index,
    kind: kind ?? "feedback",
    kind_inferred: kind === null,
    attempt: Number.isFinite(Number(record.attempt)) ? Number(record.attempt) : null,
    at: Number.isFinite(Number(record.timestamp)) ? Number(record.timestamp) : null,
    chars: Number.isFinite(Number(record.chars)) ? Number(record.chars) : text.length,
    text_fp: typeof record.text_fp === "string" ? record.text_fp : null,
    // VERBATIM. Never trimmed, re-wrapped, or escaped beyond JSON transport.
    text,
  };
}

/**
 * Every cell session dir under a run, newest first.
 *
 * A campaign has one sidecar per cell; the newest is the one an operator
 * watching a live run means by "the feedback".
 */
async function sessionDirs(runPath) {
  const sessions = join(runPath, "sessions");
  const rows = [];
  for (const ent of await listDir(sessions)) {
    if (!ent.isDirectory()) continue;
    const dir = join(sessions, ent.name);
    const sidecar = join(dir, "worktree.user-events.jsonl");
    const st = await statOrNull(sidecar);
    if (!st?.isFile()) continue;
    rows.push({ cell: ent.name, path: sidecar, mtime: st.mtimeMs });
  }
  rows.sort((a, b) => b.mtime - a.mtime);
  return rows;
}

/**
 * Assemble GET /api/feedback.
 *
 * NEVER 500, NEVER FABRICATE. A run with no sidecar yet is the normal state
 * before the first prompt is sent; it returns ok:true with an empty list and
 * `unwired:["user-events"]` plus a reason, so "nothing sent yet" stays
 * distinguishable from "this surface is not wired up".
 */
export async function readFeedback({ runsRoot, runDir, cell = null, limit = 50, includeText = true }) {
  const target = resolveRunDir(runsRoot, runDir);
  if (!target) {
    return {
      ok: false,
      code: "bad_run_dir",
      reason: `run_dir must be a single directory name under the runs root; got ${JSON.stringify(String(runDir ?? ""))}`,
    };
  }

  const dirs = await sessionDirs(target.path);
  if (dirs.length === 0) {
    return {
      ok: true,
      contract_version: FEEDBACK_CONTRACT_VERSION,
      run_dir: target.name,
      cell: null,
      cells: [],
      messages: [],
      counts: { chunk: 0, pass_verdict: 0, feedback: 0 },
      unwired: ["user-events"],
      unwired_reasons: {
        "user-events":
          `no worktree.user-events.jsonl under runs/${target.name}/sessions — the sidecar is created ` +
          "when the first prompt is sent to the model, so this is the normal state before a cell starts",
      },
    };
  }

  const chosen = cell ? dirs.find((d) => d.cell === cell) : dirs[0];
  if (!chosen) {
    return {
      ok: false,
      code: "no_such_cell",
      reason: `no cell ${JSON.stringify(String(cell))} with a sidecar under runs/${target.name}/sessions`,
    };
  }

  const records = (await readSidecar(chosen.path)) ?? [];
  const all = records.map(normalizeMessage);

  const counts = { chunk: 0, pass_verdict: 0, feedback: 0 };
  for (const m of all) {
    if (counts[m.kind] === undefined) counts[m.kind] = 0;
    counts[m.kind] += 1;
  }

  // Newest last — this is a transcript, not a ticker, matching the event feed's
  // oldest-first ordering so the two can be read together.
  const bounded = Number.isFinite(limit) && limit > 0 ? all.slice(-limit) : all;
  const messages = includeText ? bounded : bounded.map(({ text, ...rest }) => rest);

  return {
    ok: true,
    contract_version: FEEDBACK_CONTRACT_VERSION,
    run_dir: target.name,
    cell: chosen.cell,
    cells: dirs.map((d) => d.cell),
    total: all.length,
    returned: messages.length,
    // Present only when the caller asked to omit bodies, so a client can tell
    // "no text in this response" from "no text was sent".
    text_included: includeText,
    messages,
    counts,
    unwired: [],
    unwired_reasons: {},
  };
}

/**
 * The same messages as feed rows, for interleaving into /api/events.
 *
 * Shaped like EventRing's BoardEvent so the feed does not need a second
 * renderer. `kind:"user"` is deliberate: on the board these ARE user turns —
 * that is exactly the fiction under test, and labelling them "harness" in the
 * feed would quietly answer the question the operator is trying to judge.
 */
export function feedbackRows(messages, { textCap = 4000 } = {}) {
  return messages.map((m) => {
    const truncated = m.text.length > textCap;
    return {
      id: `user-event:${m.seq}`,
      kind: "user",
      type: `user:${m.kind}`,
      at: m.at,
      session_id: null,
      tool: null,
      file: null,
      name:
        m.kind === "chunk"
          ? `task chunk (attempt ${m.attempt ?? "?"})`
          : m.kind === "pass_verdict"
            ? `verdict: passing (attempt ${m.attempt ?? "?"})`
            : `verdict: still failing (attempt ${m.attempt ?? "?"})`,
      detail: `${m.chars} chars · fp ${m.text_fp ?? "none"}`,
      // VERBATIM up to the cap. Capped because a 33KB chunk prompt would swamp
      // the feed — and `truncated` says so rather than letting the tail vanish.
      text: truncated ? m.text.slice(0, textCap) : m.text,
      truncated,
      phase: null,
    };
  });
}
