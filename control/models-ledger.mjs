// ─────────────────────────────────────────────────────────────────────────────
// THE MODEL LEDGER — one row per bench-eligible model, profiles nested inside
//
// Served whole by GET /api/models-ledger. The board renders it and decides
// nothing: every gate below is computed HERE, once, so the button an operator
// sees and the rule the server enforces cannot disagree. A disabled button that
// the server would have accepted is a lie; an enabled button the server refuses
// is worse.
//
// ── THE THREE RULES THIS SURFACE EXISTS TO EXPRESS ──────────────────────────
//
//  1. A run cannot start until the model's baseline is COMPLETE and NON-VOID.
//  2. A profile cannot be created until that same baseline exists.
//  3. Runs are SERIAL, never parallel — one cell in flight across the whole
//     bench, not one per model.
//
// ── BASELINE OWNERSHIP: PER MODEL (operator ruling, 2026-08-13) ─────────────
//
// One OFF cell per model, shared by every profile with that model as subject.
// The alternative — a baseline per profile — is strictly more correct (floor
// and ON cells then share identical conditions) but costs a ~3h OFF cell per
// profile, and it makes [+profile] gate on a baseline belonging to a different
// profile, which is incoherent. The cost of the chosen rule is real and is
// NAMED ON THE SURFACE rather than hidden: `baseline.shared_by` says how many
// profiles rest on this one floor, and `baseline.measured_before` carries its
// timestamp so an operator can see the floor predates the profile.
//
// ── VOID IS NOT COMPLETE ────────────────────────────────────────────────────
//
// A void-instrument baseline is treated as NO BASELINE: [+baseline] re-enables
// and the reason is stated. A void cell ran to completion and produced numbers,
// which is exactly why this must be explicit — those numbers are an instrument
// artifact, and a Δ measured against them is invalid in a way nothing
// downstream can detect.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { join } from "node:path";

import { listProfiles } from "./profiles.mjs";

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

/**
 * Assemble GET /api/models-ledger.
 *
 * `run_in_flight` is passed in rather than re-derived: the run state has ONE
 * owner (runstate.mjs), and a second derivation here could disagree with the
 * refusal /api/run/start actually applies.
 */
export async function readModelsLedger({ runsRoot, benchModels, runInFlight, blockedReason }) {
  const offCells = await collectOffCells(runsRoot);
  const { profiles } = await listProfiles(runsRoot);

  const eligible = (benchModels ?? []).filter((m) => m?.bench_eligible);

  const models = eligible.map((m) => {
    const id = str(m.id);
    const baseline = baselineFor(id, offCells);
    const mine = profiles.filter((p) => p.subject_model === id);
    // THE ACTIVE PROFILE IS THE NEWEST ONE, GLOBALLY. The launcher attributes
    // every cell to it (server.mjs:709 via activeProfile, profiles.mjs:243-246,
    // sorted newest-first at profiles.mjs:231). It does NOT accept a profile id.
    const activeId = profiles.length ? profiles[0].id : null;

    // ── THE THREE GATES, RESOLVED ONCE, HERE ─────────────────────────────
    //
    // SERIAL FIRST. One cell in flight across the whole bench blocks every
    // launch button on every model — not just that model's. This is the rule
    // most easily broken by a per-row UI, because each row looks independent.
    const serialBlock = runInFlight
      ? (blockedReason ?? "a cell is already in flight — bench runs are serial, never parallel")
      : null;

    const canBaseline = {
      allowed: !runInFlight && !baseline.scorable,
      reason: serialBlock
        ?? (baseline.scorable
          ? `${id} already has a valid baseline; re-baselining is a declared act, not a button`
          : null),
    };

    const canProfile = {
      allowed: !runInFlight && baseline.scorable,
      reason: serialBlock ?? (baseline.scorable ? null : baseline.reason),
    };

    return {
      id,
      upstream_model: str(m.upstream_model),
      resident: m.resident === true,
      declared_context: int(m.declared_context),
      max_context: int(m.max_context),
      baseline: {
        ...baseline,
        // The named cost of per-model baselines: how many profiles rest on
        // this one floor, and when it was measured relative to them.
        shared_by: mine.length,
      },
      profiles: mine.map((p) => ({
        id: p.id,
        created_at: p.created_at,
        subject_model: p.subject_model,
        memory_models: Array.isArray(p.memory_models) ? p.memory_models : [],
        transfer: p.transfer ?? null,
        enforced: p.enforced === true,
        runs: Array.isArray(p.runs) ? p.runs : [],
        // NO PER-PROFILE MEASUREMENT EXISTS, AND THIS SAYS SO RATHER THAN
        // IMPLYING ONE IS PENDING.
        //
        // A profile's run entry records `log_name` only (profiles.mjs:354-362),
        // and runstate reports `run_dir: null` (runstate.mjs:177). There is no
        // key joining a launched run to the manifest cell that measured it, so
        // the cell columns CANNOT be filled for a profile.
        //
        // Emitting null with a stated reason is the honest form. Leaving the
        // field absent would render as "unobserved", which asserts the run
        // happened and was not measured — a different, false claim.
        latest_cell: null,
        latest_cell_unavailable:
          "runs are not joined to measured cells — a profile's run record carries only a log name, "
          + "and the launcher records no run_dir, so no cell can be attributed to this profile yet",
        // A run needs the SAME gate as the profile did, re-checked now: a
        // baseline can be invalidated between creating a profile and running
        // under it, and the button must reflect the present, not the past.
        //
        // PLUS A THIRD CONDITION THE UI CANNOT SEE. The launcher attributes the
        // cell to whatever profile is ACTIVE (the newest), and takes no profile
        // id. So `+ run` on any older row would start a real, multi-hour cell
        // and file it under a DIFFERENT profile. The gate is closed here rather
        // than drawing a button whose stated effect is not the one it has.
        can_run: {
          allowed: !runInFlight && baseline.scorable && p.id === activeId,
          reason: serialBlock
            ?? (!baseline.scorable
              ? baseline.reason
              : p.id === activeId
                ? null
                : `runs are attributed to the newest profile (${activeId}), and the launcher accepts no profile id — `
                  + `starting a cell from this row would record it under ${activeId}, not ${p.id}`),
        },
        // Stated so the row can mark itself rather than the reader inferring it
        // from the refusal sentence.
        is_active: p.id === activeId,
      })),
      can_baseline: canBaseline,
      can_profile: canProfile,
    };
  });

  return {
    ok: true,
    contract_version: MODELS_LEDGER_CONTRACT_VERSION,
    // Stated once at the top as well as per-model: the serial rule is a
    // property of the BENCH, and a reader scanning rows should not have to
    // infer it from every row carrying the same reason.
    run_in_flight: Boolean(runInFlight),
    run_blocked_reason: runInFlight ? (blockedReason ?? "a cell is already in flight") : null,
    serial_note:
      "one cell runs at a time across the whole bench. The local model is a single resident slot, " +
      "so a second concurrent cell would contend for it and corrupt the timing evidence of both.",
    models,
    // Profiles whose subject model is no longer bench-eligible still exist on
    // disk and are NOT silently dropped — they would otherwise vanish with no
    // explanation the moment a model left the roster.
    orphaned_profiles: profiles
      .filter((p) => !eligible.some((m) => str(m.id) === p.subject_model))
      .map((p) => ({ id: p.id, subject_model: p.subject_model, created_at: p.created_at })),
  };
}

export const MODELS_LEDGER_CONTRACT_VERSION = 1;
