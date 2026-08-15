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

import { listProfiles } from "./profiles.mjs";
// THE FLOOR HAS ONE OWNER. This file used to derive it inline; every gate below
// now reads the same index that /api/baselines serves and baselines.json
// records, so a button here and a floor quoted anywhere else cannot disagree.
import { readBaselines } from "./baselines.mjs";

const str = (v) => (typeof v === "string" && v.trim() ? v.trim() : null);
const int = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);

/**
 * Assemble GET /api/models-ledger.
 *
 * `run_in_flight` is passed in rather than re-derived: the run state has ONE
 * owner (runstate.mjs), and a second derivation here could disagree with the
 * refusal /api/run/start actually applies.
 */
export async function readModelsLedger({ runsRoot, benchModels, runInFlight, blockedReason }) {
  const { profiles } = await listProfiles(runsRoot);

  const eligible = (benchModels ?? []).filter((m) => m?.bench_eligible);

  // ONE DERIVATION, FOR EVERY ROW AND FOR THE WIRE. The same index is attached
  // to the payload below, so the gate a button carries and the floor a reader
  // quotes are literally the same object rather than two agreeing computations.
  const baselines = await readBaselines({ runsRoot, models: eligible });

  const models = eligible.map((m) => {
    const id = str(m.id);
    const baseline = baselines.models[id] ?? {
      exists: false,
      scorable: false,
      candidates: 0,
      reason: `no floor was resolved for ${id}`,
    };
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

    // THE SUBJECT RULE, RESOLVED HERE TOO — AND IT BINDS ON CELLS ONLY.
    //
    // /api/run/start refuses an ON cell on any model that is not the ACTIVE
    // profile's frozen subject (server.mjs `model_not_subject`). This mirror
    // exists so no button's enabled state can disagree with the refusal the
    // server would apply.
    //
    // IT ONCE BOUND [+ baseline] TOO, AND THAT WAS WRONG IN BOTH PLACES. A
    // baseline is measured against nothing — it IS the floor, and this file's
    // own rule is ONE FLOOR PER MODEL. Freezing the first profile therefore
    // disabled [+ baseline] on every other bench model permanently: the bench
    // could never acquire a second model's floor, and because [+ profile] gates
    // on that floor, no second model could ever be benchmarked at all. The whole
    // bench was locked to whichever model was profiled first.
    //
    // The ON gate needs no variable of its own here: a profile row only exists
    // under its own subject model, and `can_run` below already refuses every
    // profile that is not the active one — the same set the server's
    // `model_not_subject` refusal covers.
    //
    // A BASELINE IS GATED BY TWO THINGS AND NOTHING ELSE: no cell may be in
    // flight (runs are serial), and this model must not already have a valid
    // floor (one per model, re-baselining is a declared act rather than a
    // button). No other model's profile, and no other model's floor, has any
    // bearing on it.
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
    // THE FLOOR INDEX, ATTACHED WHOLE. Also served on its own at
    // /api/baselines and recorded to runs/baselines.json — this copy rides the
    // ledger so a board that already fetches the ledger needs no second call to
    // reference the floors, and cannot end up holding two different vintages of
    // the same answer.
    baselines,
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
