// ─────────────────────────────────────────────────────────────────────────────
// BASELINES — the floor, derived once, stored, and exported from here
//
// THE ONE OWNER. Every "does model X have a floor, and which cell is it" answer
// on this machine comes from this file. It was previously computed inside
// models-ledger.mjs and existed nowhere else: unstored, recomputed on every
// poll, and reachable only by importing the ledger module. A floor is not a
// detail of one panel — it is the reference every Δ on the board is measured
// against, and the gate on [+ baseline] and [+ profile] — so it gets an owner,
// a file on disk, and an endpoint.
//
// ── THE RULE THIS FILE ENFORCES ─────────────────────────────────────────────
//
// ONE FLOOR PER MODEL, and exactly one. A model with a valid floor cannot start
// another baseline (re-baselining is a declared act — archive the run — not a
// button). A model WITHOUT one can always start a baseline: a floor is measured
// against nothing, so no other model's profile, floor or run has any bearing on
// it. That second half was broken for as long as the subject rule was applied to
// OFF cells; see models-ledger.mjs.
//
// ── VOID IS NOT COMPLETE ────────────────────────────────────────────────────
//
// A void-instrument cell ran to completion and produced numbers that measure the
// harness rather than the model (RUNBOOK 5.10). It is treated as NO baseline —
// [+ baseline] re-enables and the reason is stated — because it looks like
// success from every angle except the one that counts.
//
// ── STORED, AND WHY THAT IS NOT A CACHE ─────────────────────────────────────
//
// `<runsRoot>/baselines.json` is written whenever the derived index CHANGES. It
// is a published record, never an input: every read re-derives from the run
// directories, and the file is the export other readers consume — the dashboard
// mounts runs/ read-only, and an operator can `cat` it. Nothing here ever reads
// it back to answer a question, so a stale or hand-edited file cannot change a
// gate. If the write fails the index is still correct and still served; the
// failure is reported in `stored`, not thrown, because losing the export must
// never cost the answer.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { join } from "node:path";

/**
 * Is this run directory ARCHIVED?
 *
 * The RUNBOOK's archive convention is `mv runs/cumulative runs/cumulative.<why>-<date>`,
 * so any suffix after the base name marks a run the operator deliberately set
 * aside — a dead cell, a void instrument, a superseded campaign.
 *
 * ARCHIVED RUNS MUST NEVER SUPPLY A BASELINE. They were retired precisely
 * because their measurements are not to be built on, and the archive reason is
 * frequently in the directory name itself ("void-truncation", "harness-error").
 * Counting them silently resurrects a floor the operator already rejected, and
 * because the numbers look ordinary nothing downstream can detect it.
 */
export function isArchivedRun(name) {
  return String(name ?? "").includes(".");
}

/** Mirrors the dashboard's own contract. A cell is the unit of measurement. */
const OFF_ARM = "off";

async function readJsonOrNull(path) {
  try {
    return JSON.parse(await fs.readFile(path, "utf8"));
  } catch {
    return null;
  }
}

async function readJsonlOrEmpty(path) {
  try {
    const text = await fs.readFile(path, "utf8");
    const out = [];
    for (const line of text.split("\n")) {
      const t = line.trim();
      if (!t) continue;
      try {
        out.push(JSON.parse(t));
      } catch {
        // A partially-written final line is normal while a cell is running.
        // Skipping it is correct; failing the whole read would blank the board
        // every time the harness is mid-write.
      }
    }
    return out;
  } catch {
    return [];
  }
}

const int = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
const str = (v) => (typeof v === "string" && v.trim() ? v.trim() : null);

/**
 * Every OFF cell on disk, folded per model.
 *
 * Reads the SCHEDULE for identity and the status stream for measurement, which
 * is the same split the dashboard's stack-ledger uses: a cell that was
 * scheduled but never produced a status record is still a real cell, and
 * dropping it would make a crashed run look like it never happened.
 */
export async function collectOffCells(runsRoot) {
  const cells = [];
  let entries = [];
  try {
    entries = await fs.readdir(runsRoot, { withFileTypes: true });
  } catch {
    return cells;
  }

  for (const ent of entries) {
    if (!ent.isDirectory()) continue;
    // Archived runs are excluded entirely — see isArchivedRun().
    if (isArchivedRun(ent.name)) continue;
    const dir = join(runsRoot, ent.name);
    const manifest = await readJsonOrNull(join(dir, "manifest.json"));
    if (!manifest) continue;

    const schedule = Array.isArray(manifest.schedule) ? manifest.schedule : [];
    if (!schedule.length) continue;

    const status = await readJsonlOrEmpty(join(dir, "manifest.status.jsonl"));

    // Fold the status stream by sequence_index — one cell may have many
    // attempt records, and the LAST one carries the cell's outcome.
    const folded = new Map();
    for (const r of status) {
      if (r.type !== undefined && r.type !== "attempt") continue;
      const seq = int(r.sequence_index) ?? 0;
      const p = r.progress ?? {};
      const prev = folded.get(seq) ?? {};
      folded.set(seq, {
        verdict: str(r.verdict) ?? prev.verdict ?? null,
        turns: int(p.turns) ?? prev.turns ?? null,
        tokens: int(p.total_tokens) ?? int(p.tokens) ?? prev.tokens ?? null,
        wall_seconds: int(p.wall_seconds) ?? prev.wall_seconds ?? null,
        terminal_reason: str(r.terminal_reason) ?? prev.terminal_reason ?? null,
        // Summed across attempts, not overwritten: a truncation in ANY attempt
        // taints the cell, and the last attempt reporting none does not undo it.
        truncated_turns: (prev.truncated_turns ?? 0) + (int(p.truncated_turns) ?? 0),
        length_truncations: (prev.length_truncations ?? 0) + (int(p.length_truncations) ?? 0),
        // A cell is only GREEN if it actually passed. Anything else leaves the
        // truncation signals decisive.
        full_green: str(r.verdict) === "PASS" || prev.full_green === true,
      });
    }

    for (let i = 0; i < schedule.length; i += 1) {
      const slot = schedule[i] ?? {};

      // THE FIELD IS `memory_mode`. The harness writes `memory_mode: "off"`,
      // not `mode` or `arm` — verified against a real manifest. Reading the
      // wrong key does not throw, it silently yields ZERO OFF cells, which
      // presents as "no baseline has ever been run" and would re-enable
      // [+baseline] on a model that already has a valid floor.
      const arm = str(slot.memory_mode) ?? str(slot.mode) ?? str(slot.arm);
      if (arm !== OFF_ARM) continue;

      const seq = int(slot.sequence_index) ?? i;
      const meas = folded.get(seq) ?? null;

      // THE MODEL IS PROVIDER-PREFIXED in `model` ("local-llm-proxy/qwen…")
      // but bare in `provider_pin` ("qwen…"), and the roster's ids are bare.
      // `provider_pin` is preferred so the join with bench_models actually
      // matches; the prefix is stripped as a fallback rather than assuming
      // every manifest carries a pin.
      const rawModel = str(slot.provider_pin) ?? str(slot.model) ?? str(manifest.model);
      const model = rawModel ? rawModel.split("/").pop() : null;

      // ── VOID INSTRUMENT — RUNBOOK 5.10, mirrored from stack-ledger.mjs:420 ──
      //
      // A non-green terminal attempt carrying a provider-side truncation signal
      // is an INSTRUMENT failure, never a capability result. The rule is copied
      // from the dashboard's own reader deliberately: two different definitions
      // of "void" would let a cell be the floor here and be excluded there.
      //
      // `harness_error` is included because the harness itself declared the run
      // unusable. `attempt_ceiling_reached` is NOT void — a model that fails
      // every attempt is a real capability result, and calling it an instrument
      // fault would silently discard the bench's most important finding.
      const voidInstrument = Boolean(
        meas
        && !meas.full_green
        && (meas.terminal_reason === "transport_incomplete"
          || meas.terminal_reason === "harness_error"
          || (meas.length_truncations ?? 0) > 0
          || (meas.truncated_turns ?? 0) > 0),
      );

      cells.push({
        run_dir: ent.name,
        sequence_index: seq,
        model,
        arm,
        state: meas ? "complete" : "not_started",
        void_instrument: voidInstrument,
        verdict: meas?.verdict ?? null,
        turns: meas?.turns ?? null,
        tokens: meas?.tokens ?? null,
        wall_seconds: meas?.wall_seconds ?? null,
        terminal_reason: meas?.terminal_reason ?? null,
        created_at: str(manifest.created_at),
      });
    }
  }

  return cells;
}

/**
 * The baseline for ONE model: the newest OFF cell that is complete and not void.
 *
 * Returns the gate AND its reason. A refusal without a reason is what makes an
 * operator click a dead button repeatedly, so every false here carries the
 * sentence explaining it.
 */
export function baselineFor(model, offCells) {
  const mine = offCells.filter((c) => c.model === model);
  const scorable = mine.filter((c) => c.state === "complete" && !c.void_instrument);
  const voids = mine.filter((c) => c.state === "complete" && c.void_instrument);
  const running = mine.filter((c) => c.state !== "complete");

  if (scorable.length) {
    // Newest wins: a re-baseline is a sanctioned, declared act, and the most
    // recent valid floor is the one subsequent cells are measured against.
    const b = scorable[scorable.length - 1];
    return {
      exists: true,
      scorable: true,
      run_dir: b.run_dir,
      sequence_index: b.sequence_index,
      measured_before: b.created_at,
      turns: b.turns,
      tokens: b.tokens,
      wall_seconds: b.wall_seconds,
      verdict: b.verdict,
      // A second valid OFF cell is not an error, but it IS a fact the operator
      // should see: only one of them is the floor.
      candidates: scorable.length,
      reason: null,
    };
  }

  if (voids.length) {
    return {
      exists: false,
      scorable: false,
      voided: true,
      candidates: 0,
      reason:
        `the last OFF cell for ${model} is void-instrument (${voids[voids.length - 1].terminal_reason ?? "instrument fault"}) — ` +
        "it produced numbers, but they measure the harness rather than the model, so every Δ " +
        "computed against them would be invalid. Run a new baseline.",
    };
  }

  if (running.length) {
    return {
      exists: false,
      scorable: false,
      pending: true,
      candidates: 0,
      reason: `an OFF cell for ${model} is scheduled or in flight but has not produced a measurement yet`,
    };
  }

  return {
    exists: false,
    scorable: false,
    candidates: 0,
    reason: `no OFF cell has ever been run for ${model} — the floor every Δ is measured against does not exist yet`,
  };
}

/** The stored export. One file, rewritten only when the answer changes. */
export const BASELINES_FILE = "baselines.json";
export const BASELINES_CONTRACT_VERSION = 1;

/**
 * THE INDEX — every bench model's floor, resolved together.
 *
 * `models` is the roster to resolve. A model with no OFF cell still gets an
 * entry carrying `exists: false` and the reason, because "this model has no
 * floor yet" is an answer the UI needs to render a gate; omitting it would make
 * an unmeasured model indistinguishable from one that is not on the bench.
 */
export async function buildBaselineIndex({ runsRoot, models }) {
  const offCells = await collectOffCells(runsRoot);
  const ids = (models ?? []).map((m) => (typeof m === "string" ? m : str(m?.id))).filter(Boolean);

  const out = {};
  for (const id of ids) out[id] = baselineFor(id, offCells);

  return {
    contract_version: BASELINES_CONTRACT_VERSION,
    generated_at: new Date().toISOString(),
    runs_root: runsRoot,
    // Counted, not just listed: "how many OFF cells exist at all" is the first
    // thing to check when a floor is missing that the operator believes ran.
    off_cells_seen: offCells.length,
    models: out,
    note:
      "one floor per model. A model with no scorable floor may always start a baseline; a model "
      + "with one may not — re-baselining is a declared act (archive the run), not a button. "
      + "Void-instrument cells are not floors.",
  };
}

/**
 * Derive, publish, return.
 *
 * The write is best-effort and its outcome is REPORTED rather than thrown: the
 * index is the product, and a read-only or full disk must not turn a correct
 * answer into an error. Unchanged content is not rewritten, so a 2s poll does
 * not churn the file or its mtime.
 */
export async function readBaselines({ runsRoot, models }) {
  const index = await buildBaselineIndex({ runsRoot, models });
  const stored = await publishBaselines(runsRoot, index);
  return { ok: true, ...index, stored };
}

async function publishBaselines(runsRoot, index) {
  const path = join(runsRoot, BASELINES_FILE);
  // `generated_at` changes on every derivation, so it is excluded from the
  // comparison — including it would rewrite the file every poll and make the
  // mtime meaningless as a "when did the floor last change" signal.
  const { generated_at: _ignored, ...stable } = index;
  const body = `${JSON.stringify(index, null, 2)}\n`;

  try {
    const prev = await fs.readFile(path, "utf8");
    const parsed = JSON.parse(prev);
    const { generated_at: _prevGen, ...prevStable } = parsed;
    if (JSON.stringify(prevStable) === JSON.stringify(stable)) {
      return { path, written: false, reason: "unchanged since the last derivation" };
    }
  } catch {
    // No readable previous file — fall through and write one.
  }

  try {
    await fs.writeFile(path, body, "utf8");
    return { path, written: true, reason: null };
  } catch (err) {
    return { path, written: false, reason: `could not write the baseline export: ${String(err?.message ?? err)}` };
  }
}
