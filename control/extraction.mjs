// ─────────────────────────────────────────────────────────────────────────────
// EXTRACTION — trigger the pipeline and track its stages
//
// The stage machine is read from `BACKGAMMON_SXE_STAGE` lines, a structured
// emitter added to scripts/backgammon_sxe.py for exactly this purpose.
//
// ── WHY A STRUCTURED EMITTER RATHER THAN SCRAPING THE PROSE LOG ──────────────
//
// The extraction script already had a `stage` local tracking the real pipeline
// state, but it was only ever PRINTED on the error path (`ERROR stage=<x>`).
// Everything else was prose: "extract end path=extract dur_ms=… n_memories=…".
//
// A UI built by pattern-matching prose breaks silently the moment a message is
// reworded — and it breaks by showing a STALE stage, which is worse than
// showing none. So the script now emits one machine-readable line per
// transition with a stable schema, and this module consumes only that. The
// prose lines are untouched; the change is purely additive.
//
// ── THE FIFTH STATE ──────────────────────────────────────────────────────────
//
// `gated` is neither success nor failure. WO-DBVOL-1 made the substrate check
// fail-closed because SQLite corruption is PARTIAL — the corrupt DB answered
// `count(*)` with 492 while `PRAGMA quick_check` reported a malformed image.
// An unverified substrate did not fail extraction; it silently produced FEWER
// memories and the ON arm published a number that looked real and was simply
// too low.
//
// So a gated stage must read as the instrument DELIBERATELY REFUSING. The
// emitter marks gated stages terminal so the error path cannot overwrite them
// with `failed`, and this module preserves that distinction all the way to the
// board.
// ─────────────────────────────────────────────────────────────────────────────

import { spawn } from "node:child_process";
import { emptyExtraction, EXTRACT_STAGES } from "./contract.mjs";

const STAGE_LINE = "BACKGAMMON_SXE_STAGE ";
const RESULT_LINE = "BACKGAMMON_SXE_RESULT_JSON ";

/**
 * Parse the structured stage/result lines out of a chunk of process output.
 * Tolerant by design: an unrecognised line is ignored, a malformed JSON payload
 * is skipped, and a partial trailing line is left for the next chunk.
 */
export function parseStageLines(text) {
  const stages = [];
  let result = null;
  for (const line of String(text ?? "").split("\n")) {
    const t = line.trim();
    if (t.startsWith(STAGE_LINE)) {
      try {
        stages.push(JSON.parse(t.slice(STAGE_LINE.length)));
      } catch {
        /* half-flushed line — expected, skipped */
      }
    } else if (t.startsWith(RESULT_LINE)) {
      try {
        result = JSON.parse(t.slice(RESULT_LINE.length));
      } catch {
        /* incomplete result line */
      }
    }
  }
  return { stages, result };
}

/**
 * Fold stage records into the declared stage list.
 *
 * The DECLARED list is the spine: every stage is present from the start, in
 * order, so the UI can render what is coming rather than only what has
 * happened. Records only change a stage's state — they never add or reorder
 * one, so an unknown stage id from a future script revision is ignored here
 * and surfaced by the drift test rather than silently appearing in the UI.
 */
export function foldStages(records) {
  const view = emptyExtraction();
  const byId = new Map(view.stages.map((s) => [s.id, s]));

  for (const r of records) {
    const s = byId.get(String(r?.stage ?? ""));
    if (!s) continue;
    const state = String(r?.state ?? "");
    if (!["pending", "running", "complete", "failed", "gated"].includes(state)) continue;

    // A terminal state is never overwritten by a later non-terminal one.
    // Without this a `gated` substrate could be reset to `running` by an
    // out-of-order line and the refusal would vanish from the UI.
    if (s.state === "gated" || s.state === "failed") continue;

    if (state === "running") s.started_at = Number.isFinite(r.at) ? r.at * 1000 : Date.now();
    if (state !== "running" && s.started_at) {
      s.elapsed_s = Math.max(0, Math.round((Number(r.at) * 1000 - s.started_at) / 1000));
    }
    s.state = state;
    // `count` is copied even when 0 — a measured zero is a real result.
    if (Number.isFinite(r.count)) s.count = r.count;
    if (typeof r.detail === "string" && r.detail) s.detail = r.detail;
  }

  return view;
}

/**
 * Live extraction tracker. Owns at most ONE extraction process — the pipeline
 * mutates the corpus, and two concurrent extractions against one org would
 * interleave submissions unpredictably.
 */
export class ExtractionTracker {
  constructor() {
    this.records = [];
    this.proc = null;
    this.state = "idle";
    this.model = null;
    this.started_at = null;
    this.finished_at = null;
    this.status = null;
    this.reason = null;
    this.n_memories = null;
    this.exit_code = null;
    this._buf = "";
    this._completed = false;
  }

  get running() {
    return this.proc !== null;
  }

  /** The full view the board renders. Always a complete stage list. */
  view() {
    const v = foldStages(this.records);
    v.state = this.state;
    v.model = this.model;
    v.started_at = this.started_at;
    v.finished_at = this.finished_at;
    v.status = this.status;
    v.reason = this.reason;
    v.n_memories = this.n_memories;
    v.exit_code = this.exit_code;
    v.elapsed_s = this.started_at
      ? Math.round(((this.finished_at ?? Date.now()) - this.started_at) / 1000)
      : null;
    return v;
  }

  _consume(chunk) {
    this._buf += chunk;
    const nl = this._buf.lastIndexOf("\n");
    if (nl === -1) return;
    const complete = this._buf.slice(0, nl);
    this._buf = this._buf.slice(nl + 1);

    const { stages, result } = parseStageLines(complete);
    for (const s of stages) this.records.push(s);
    if (result) {
      this.status = typeof result.status === "string" ? result.status : null;
      this.n_memories = Number.isFinite(result.n_memories) ? result.n_memories : null;
      if (typeof result.error === "string" && result.error) this.reason = result.error;
    }
  }

  /**
   * Launch extraction. Returns { ok } or a refusal — never throws.
   *
   * The command is built from an ARGUMENT ARRAY and spawned WITHOUT a shell.
   * Operator-supplied values (model, org) therefore cannot inject a command:
   * they are argv entries, not shell words. This is the single most important
   * property of this function.
   */
  start({ python, script, cwd, runLabel, sourceMode, orgId, model, env, onComplete }) {
    if (this.running) {
      return { ok: false, code: "run_in_flight", reason: "an extraction is already running" };
    }

    const args = [
      script,
      "--run-label",
      String(runLabel),
      "--source-mode",
      String(sourceMode),
      "--session-model",
      String(model),
    ];
    if (orgId) args.push("--org-id", String(orgId));

    this.records = [];
    this.state = "running";
    this.model = model;
    this.started_at = Date.now();
    this.finished_at = null;
    this.status = null;
    this.reason = null;
    this.n_memories = null;
    this.exit_code = null;
    this._buf = "";
    this._completed = false;

    let child;
    try {
      child = spawn(python, args, {
        cwd,
        env: { ...process.env, ...(env ?? {}) },
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (err) {
      this.state = "failed";
      this.reason = `launcher failed: ${String(err?.message ?? err)}`;
      this.finished_at = Date.now();
      return { ok: false, code: "launcher_failed", reason: this.reason };
    }

    this.proc = child;
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (c) => this._consume(c));
    // stderr is consumed too: the stage emitter writes to stdout, but a
    // traceback lands on stderr and is the only explanation a failed run has.
    child.stderr.on("data", (c) => {
      const t = String(c).trim();
      if (t) this.reason = t.slice(-400);
    });

    child.on("error", (err) => {
      this.proc = null;
      this.state = "failed";
      this.reason = `process error: ${String(err?.message ?? err)}`;
      this.finished_at = Date.now();
    });

    child.on("close", (code) => {
      this.proc = null;
      this.exit_code = code;
      this.finished_at = Date.now();
      // Exit code alone does not decide the verdict: a `gated` substrate exits
      // non-zero and that is the gate working correctly, not a crash. The
      // stage records carry the distinction.
      const gated = this.records.some((r) => r.state === "gated");
      this.state = gated ? "gated" : code === 0 ? "complete" : "failed";
      if (this.state === "complete" && this.status === "ok" && typeof onComplete === "function") {
        Promise.resolve(onComplete()).catch((err) => {
          this.reason = `extraction completed but source-cell stamp failed: ${String(err?.message ?? err)}`;
        });
      }
      if (this.state === "failed" && !this.reason) {
        this.reason = `extraction exited ${code} without a stated reason`;
      }
    });

    return { ok: true };
  }

}

/** Declared stage ids, for the drift test against the Python emitter. */
export const DECLARED_STAGE_IDS = EXTRACT_STAGES.map((s) => s.id);
