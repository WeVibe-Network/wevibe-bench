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

import { listProfiles, activeProfile } from "./profiles.mjs";
// THE FLOOR HAS ONE OWNER. This file used to derive it inline; every gate below
// now reads the same index that /api/baselines serves and baselines.json
// records, so a button here and a floor quoted anywhere else cannot disagree.
import { readBaselines, collectCells } from "./baselines.mjs";

const str = (v) => (typeof v === "string" && v.trim() ? v.trim() : null);
const int = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);

/**
 * Assemble GET /api/models-ledger.
 *
 * `run_in_flight` is passed in rather than re-derived: the run state has ONE
 * owner (runstate.mjs), and a second derivation here could disagree with the
 * refusal /api/run/start actually applies.
 */
export async function readModelsLedger({ runsRoot, benchModels, runInFlight, blockedReason, cloud = null }) {
  const { profiles } = await listProfiles(runsRoot);

  const eligible = (benchModels ?? []).filter((m) => m?.bench_eligible);

  // ONE DERIVATION, FOR EVERY ROW AND FOR THE WIRE. The same index is attached
  // to the payload below, so the gate a button carries and the floor a reader
  // quotes are literally the same object rather than two agreeing computations.
  const baselines = await readBaselines({ runsRoot, models: eligible });

  // EVERY CELL ON DISK, BOTH ARMS — the other half of the join below. Read once
  // here rather than per profile: a bench with six profiles would otherwise walk
  // every run directory six times per poll to answer the same question.
  const allCells = await collectCells(runsRoot);

  // ── TWO FACTS ABOUT THE BENCH, NOT ABOUT ANY ROW ────────────────────────
  //
  // Both were computed inside the per-model loop, which was harmless while one
  // shape consumed them and became a trap the moment a second did: the baseline
  // rows and the startable list need the identical answers, and re-deriving the
  // serial rule beside a copy of it is exactly how one surface ends up offering
  // a launch the other has already refused. Hoisted, there is one of each.
  //
  // THE ACTIVE PROFILE IS THE NEWEST ONE WITH A LIVE CAMPAIGN. The launcher
  // attributes every cell to it (profiles.mjs `activeProfile`, which excludes
  // profiles whose referenced campaign is archived or nonexistent) and it does
  // NOT accept a profile id.
  const active = await activeProfile(runsRoot);
  const activeProfileId = active?.id ?? null;

  // SERIAL FIRST, AND BENCH-WIDE. One cell in flight blocks every launch button
  // on every model — not just that model's. This is the rule most easily broken
  // by a per-row UI, because each row looks independent.
  const serialNote = runInFlight
    ? (blockedReason ?? "a cell is already in flight — bench runs are serial, never parallel")
    : null;

  const models = eligible.map((m) => {
    const id = str(m.id);
    const baseline = baselines.models[id] ?? {
      exists: false,
      scorable: false,
      candidates: 0,
      reason: `no floor was resolved for ${id}`,
    };
    const mine = profiles.filter((p) => p.subject_model === id);
    // Hoisted above — see the block before this loop. Bound to local names so
    // the gate expressions below read exactly as they did when each row
    // computed its own.
    const activeId = activeProfileId;
    const serialBlock = serialNote;

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
      profiles: mine.map((p) => {
        // ── THE JOIN, PERFORMED ──────────────────────────────────────────
        //
        // Every run this profile recorded, resolved against the cell it
        // produced. See joinRuns(): a run launched since the join key shipped
        // resolves to a real measurement; one launched before it says so in a
        // sentence, per run, rather than the whole panel disclaiming itself.
        const runs = joinRuns(p, allCells, baseline);
        const measured = runs.filter((r) => r.cell);
        const latest = measured.length ? measured[measured.length - 1] : null;
        const unjoined = runs.find((r) => !r.cell && r.cell_unavailable);

        return {
        id: p.id,
        created_at: p.created_at,
        subject_model: p.subject_model,
        memory_models: Array.isArray(p.memory_models) ? p.memory_models : [],
        transfer: p.transfer ?? null,
        enforced: p.enforced === true,
        runs,
        // THE CELL THIS PROFILE'S NEWEST MEASURED RUN PRODUCED.
        //
        // This was hardcoded null with a stated reason, and the reason was
        // true: a run record carried a log name and nothing else, so no cell
        // could be attributed to a profile. The launcher now records the
        // campaign directory and the schedule index at spawn time, which is the
        // only moment either is knowable, and the columns fill from the same
        // manifests every other measurement on the board is read from.
        //
        // STILL NULL WHEN IT IS NULL. A profile whose only runs predate the
        // join key, or whose recorded cell is not on disk, reports null and
        // carries the reason — the field never becomes a guess just because it
        // is now fillable in the common case.
        latest_cell: latest?.cell ?? null,
        latest_cell_unavailable: latest ? null : (unjoined?.cell_unavailable ?? null),
        // THE BEST RESULT UNDER THIS PROFILE — the design's "BEST −14 TURNS".
        // Fewest turns against the floor, across completed non-void runs only.
        // A running cell has no total and a void one measures the harness;
        // either would win this comparison by being incomplete.
        best: bestDelta(measured, baseline),
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
        };
      }),
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
    // ── THE CARD'S OWN SHAPE: BASELINES AT THE ROOT ──────────────────────
    //
    // The same facts as `models` above, rooted the other way up, because the
    // two answer different questions and the surface asks the second one.
    //
    // `models` is the MODEL UNIVERSE with a floor hanging off each row, which
    // is what a gate needs: "may this model start a baseline" has to be
    // answerable for a model that has never run anything. Rendered directly, it
    // puts a row on screen for every model the bench could theoretically
    // measure, most of them empty, and buries the one real measurement among
    // five statements of intent.
    //
    // `baseline_rows` is the MEASUREMENTS, with the profiles that rest on each
    // one nested inside it. A profile has no meaning apart from the baseline it
    // is measured against — that is the argument the nesting makes — and a
    // baseline that does not exist yet has no row, because the card lists what
    // was measured rather than what could be.
    baseline_rows: baselineRows({
      baselines,
      profiles,
      allCells,
      activeId: activeProfileId,
      serialBlock: serialNote,
    }),
    counts: baselines.counts ?? { complete: 0, running: 0, void: 0 },
    // ── WHAT A NEW BASELINE COULD BE STARTED ON ──────────────────────────
    //
    // Every model on both substrates, each carrying its own resolved gate. This
    // is what the [+ PROFILE] modal's baseline branch renders, and it is
    // computed HERE for the reason the header of this file gives: a picker that
    // offers a model the launch would refuse teaches the operator that the UI
    // lies, and the lesson generalises to every other control on the board.
    startable: startableModels({ eligible, baselines, cloud, serialBlock: serialNote }),
    cloud: cloud
      ? {
          // The catalogue and the key REPORT — never the key. See cloud.mjs.
          router: cloud.router,
          providers: cloud.providers,
          models: cloud.models,
          key: cloud.key,
          spend_ceiling_usd: cloud.spend_ceiling_usd,
          spend_note: cloud.spend_note,
          can_start: cloud.can_start,
          can_start_reason: cloud.can_start_reason,
        }
      : null,
    // Profiles whose subject model is no longer bench-eligible still exist on
    // disk and are NOT silently dropped — they would otherwise vanish with no
    // explanation the moment a model left the roster.
    //
    // A CLOUD PROFILE IS NOT AN ORPHAN. `eligible` is the LOCAL proxy roster, so
    // testing membership against it alone declared every cloud profile orphaned
    // the moment cloud baselines existed — a profile whose subject is running
    // perfectly well, filed under "nothing can run under this until its model is
    // served again". A profile is an orphan when nothing on EITHER substrate
    // claims its subject.
    orphaned_profiles: profiles
      .filter((p) => !eligible.some((m) => str(m.id) === p.subject_model))
      .filter((p) => !(cloud?.models ?? []).some((m) => m.key === p.subject_model))
      .map((p) => ({ id: p.id, subject_model: p.subject_model, created_at: p.created_at })),
  };
}

/**
 * ONE ROW PER MEASURED FLOOR, with its profiles and their runs nested inside.
 *
 * EVERY GATE IS RESOLVED HERE AND THE CARD RENDERS THE VERDICT. That is the
 * whole thesis of this file, and it matters more on this shape than on the last
 * one: the nesting means a single row can carry a dozen controls, and a card
 * that re-derived even one of them would eventually disagree with the server
 * about a run that costs hours.
 */
function baselineRows({ baselines, profiles, allCells, activeId, serialBlock }) {
  const rows = Array.isArray(baselines?.list) ? baselines.list : [];

  return rows.map((b) => {
    const mine = profiles.filter((p) => p.subject_model === b.model);

    // A PROFILE NEEDS A CLOSED, VALID FLOOR — the same rule the model rows
    // apply, re-stated against this row's own baseline. A running baseline has
    // no total to compare against, and a void one has numbers that measure the
    // harness; both refuse, and they refuse differently because the operator's
    // next move differs (wait, versus archive and re-run).
    const canProfile = {
      allowed: !serialBlock && b.scorable === true,
      reason: serialBlock ?? (b.scorable ? null : b.reason),
    };

    return {
      ...b,
      // UPPERCASED FOR THE COLUMN, resolved from the manifest rather than from
      // the id's shape. The design's KIND column is two words and this is the
      // one place that decides which.
      kind_label: b.kind === "cloud" ? "CLOUD" : "LOCAL",
      profiles: mine.map((p) => {
        // THE SAME JOIN THE MODEL ROWS USE, against THIS ROW'S floor. `b` is
        // the baseline the profile is nested under, so a Δ computed here is a Δ
        // against the cell the operator can see one line above it — which is
        // the entire reason the nesting is the shape it is.
        const runs = joinRuns(p, allCells, b);
        return {
        id: p.id,
        created_at: p.created_at,
        subject_model: p.subject_model,
        memory_models: Array.isArray(p.memory_models) ? p.memory_models : [],
        // The design's "3 source models" — the count is what the row shows and
        // the list is what the drawer shows, so both travel rather than the
        // board counting an array it also renders.
        source_count: Array.isArray(p.memory_models) ? p.memory_models.length : 0,
        transfer: p.transfer ?? null,
        enforced: p.enforced === true,
        is_active: p.id === activeId,
        can_run: {
          allowed: !serialBlock && b.scorable === true && p.id === activeId,
          reason: serialBlock
            ?? (!b.scorable
              ? b.reason
              : p.id === activeId
                ? null
                : `runs are attributed to the newest profile (${activeId}), and the launcher accepts `
                  + `no profile id — starting a cell from this row would record it under ${activeId}, `
                  + `not ${p.id}`),
        },
        run_count: runs.length,
        // NEWEST FIRST, matching the design's note that "runs sort newest first
        // inside a profile". The stored order is append-only and therefore
        // oldest-first; reversing here rather than in the browser keeps the
        // `seq` ordinals — which are positions in the stored order — correct.
        runs: [...runs].reverse(),
        best: bestDelta(runs.filter((r) => r.cell), b),
        };
      }),
      profile_count: mine.length,
      can_profile: canProfile,
    };
  });
}

/**
 * EVERY MODEL A BASELINE COULD BE STARTED ON, both substrates, each gated.
 *
 * LOCAL AND CLOUD ARE ONE LIST WITH A `kind` FIELD, not two lists. The modal
 * asks "local or cloud?" and then filters — so a single list with the substrate
 * on each row is the shape the picker actually consumes, and it means the two
 * branches cannot drift into applying different rules to the same question.
 *
 * THE THREE REFUSALS, in the order they bind:
 *   serial      a cell is in flight; nothing may start anywhere on the bench
 *   floor       this model already has a valid one — re-baselining is a declared
 *               act (archive the run), not a button
 *   key         cloud only, and it is checked HERE rather than at the vendor so
 *               the picker refuses before a campaign directory is built
 */
function startableModels({ eligible, baselines, cloud, serialBlock }) {
  const out = [];

  for (const m of eligible) {
    const id = str(m.id);
    if (!id) continue;
    const b = baselines.models[id] ?? null;
    out.push({
      id,
      kind: "local",
      provider: "local-llm-proxy",
      label: id,
      resident: m.resident === true,
      context: int(m.declared_context),
      has_baseline: b?.scorable === true,
      can_baseline: {
        allowed: !serialBlock && b?.scorable !== true,
        reason: serialBlock
          ?? (b?.scorable
            ? `${id} already has a valid baseline (${b.id ?? "floor"}); re-baselining is a declared act, not a button`
            : null),
      },
    });
  }

  for (const m of cloud?.models ?? []) {
    // The floor for a cloud model is keyed by its `{provider}/{model}` key, and
    // the list export is derived from the CELLS rather than the roster, which is
    // the only reason a cloud floor is findable at all — the local proxy roster
    // has never heard of it.
    const row = (baselines.list ?? []).find((b) => b.model === m.key) ?? null;
    const keyed = cloud?.key?.present === true;
    out.push({
      id: m.key,
      kind: "cloud",
      provider: m.provider,
      label: m.name,
      slug: m.slug,
      resident: null,
      context: m.context,
      has_baseline: row?.scorable === true,
      can_baseline: {
        allowed: !serialBlock && row?.scorable !== true && keyed,
        reason: serialBlock
          ?? (row?.scorable
            ? `${m.key} already has a valid baseline (${row.id}); re-baselining is a declared act, not a button`
            : keyed
              ? null
              : (cloud?.can_start_reason ?? "no cloud API key resolves, so a cloud cell cannot authenticate")),
      },
    });
  }

  return out;
}

// ── THE RUN → CELL JOIN ─────────────────────────────────────────────────────
//
// ── WHAT WAS BROKEN, AND FOR HOW LONG ───────────────────────────────────────
//
// Every measurement column on every profile row on every board has been null
// since the ledger shipped, and the panel disclaimed itself in a sentence
// repeated under each row: "runs are not joined to measured cells". That was an
// accurate description of the data and a false description of the problem. The
// join key was never unobtainable — it was never written down. `/api/run/start`
// already resolved which campaign directory the cell would write to (it has to,
// to pass `--manifest`), and the manifest already recorded which slot was
// current. Both were discarded the instant the process spawned, and no reader
// afterwards could recover them: a directory holding four cells cannot say which
// of them produced a given log file.
//
// The launcher now records `run_dir` and `sequence_index` on the run entry. This
// function spends them.
//
// ── THE KEY IS VERIFIED, NOT TRUSTED ────────────────────────────────────────
//
// `sequence_index` is the manifest's `current_index` READ A MOMENT BEFORE the
// spawn — a claim about what the harness was about to do, not a receipt for
// what it did. So the cell found at that address is checked: its arm must match
// the arm that was launched. A mismatch means the harness scheduled something
// other than what was predicted, and the run reports NO cell with that stated,
// rather than adopting a neighbouring cell's measurements. Silently attributing
// an OFF cell's numbers to an ON run would invert the sign of every Δ computed
// from it, and nothing downstream could detect it.
//
// ── A MISSING KEY IS A DIFFERENT ANSWER FROM A MISSING CELL ─────────────────
//
// Three distinct nulls, never collapsed, because they need three different
// things done about them:
//   no key      the run predates the join and never can be attributed — history
//   no cell     the key points at a directory or slot that is not on disk
//   wrong arm   the key resolved, and to the wrong thing
// A single "unavailable" would tell an operator to go looking in all three cases
// when only the middle one is worth investigating.

const PHASE_TOTAL = 3;

function joinRuns(profile, allCells, baseline) {
  const entries = Array.isArray(profile?.runs) ? profile.runs : [];

  return entries.map((r, i) => {
    const runDir = str(r?.run_dir);
    const seq = int(r?.sequence_index);
    const arm = str(r?.arm);

    const base = {
      // The ordinal the design's run rows carry ("run 06"). It is the position
      // in this profile's own append-only history, so it is stable: a run's
      // number never changes when a later one is added.
      seq: i + 1,
      started_at: r?.started_at ?? null,
      log_name: r?.log_name ?? null,
      arm,
      model: r?.model ?? null,
      org: r?.org ?? null,
      kind: r?.kind ?? null,
      run_dir: runDir,
      sequence_index: seq,
    };

    if (!runDir || seq === null) {
      return {
        ...base,
        cell: null,
        cell_unavailable:
          "this run was launched before the control plane recorded which campaign directory and "
          + "schedule slot a cell was about to write to, so it cannot be attributed to a "
          + "measurement. It is real and it is kept; nothing can be computed from it.",
        delta: null,
      };
    }

    const cell = allCells.find((c) => c.run_dir === runDir && c.sequence_index === seq) ?? null;
    if (!cell) {
      return {
        ...base,
        cell: null,
        cell_unavailable:
          `no cell is on disk at ${runDir} slot ${seq}, where this run was recorded as writing. `
          + "The campaign directory may have been archived or removed.",
        delta: null,
      };
    }
    if (arm && cell.arm && cell.arm !== arm) {
      return {
        ...base,
        cell: null,
        cell_unavailable:
          `the cell at ${runDir} slot ${seq} is a ${cell.arm.toUpperCase()} cell and this run was `
          + `launched as ${arm.toUpperCase()}. The schedule slot predicted at launch is not the one `
          + "the harness ran, so no measurement is attributed — adopting this cell's numbers would "
          + "compute a Δ with the wrong sign.",
        delta: null,
      };
    }

    return {
      ...base,
      cell: {
        run_dir: cell.run_dir,
        sequence_index: cell.sequence_index,
        state: cell.state,
        phases: cell.phases ?? { done: 0, total: PHASE_TOTAL },
        turns: cell.turns,
        tokens: cell.tokens,
        wall_seconds: cell.wall_seconds,
        gates: cell.gates,
        verdict: cell.verdict,
        void_instrument: cell.void_instrument === true,
        terminal_reason: cell.terminal_reason,
      },
      cell_unavailable: null,
      delta: deltaOf(cell, baseline),
    };
  });
}

/**
 * Δ AGAINST THIS PROFILE'S OWN FLOOR.
 *
 * `baseline` is the subject model's, passed in rather than looked up, because a
 * Δ against another model's floor is a capability comparison wearing a memory-
 * lift label — the single most expensive mistake this board can make look
 * ordinary.
 *
 * FOUR REFUSALS BEFORE A NUMBER, each stating which fact is missing:
 * a running cell has no total; a void cell measured the instrument; an unscored
 * floor cannot be subtracted from; and an unobserved turn count is not a zero.
 */
function deltaOf(cell, baseline) {
  if (!cell) return null;
  if (cell.state !== "complete") {
    return { computable: false, reason: "withheld until the cell closes", turns: null, tokens: null };
  }
  if (cell.void_instrument) {
    return {
      computable: false,
      reason: "void instrument — these numbers measure the harness, not the model",
      turns: null,
      tokens: null,
    };
  }
  if (!baseline?.scorable) {
    return { computable: false, reason: "no valid floor to measure against", turns: null, tokens: null };
  }
  if (cell.turns === null || baseline.turns === null || baseline.turns === undefined) {
    return { computable: false, reason: "turns unobserved on one side", turns: null, tokens: null };
  }

  const turns = cell.turns - baseline.turns;
  const tokens =
    cell.tokens !== null && baseline.tokens !== null && baseline.tokens !== undefined
      ? cell.tokens - baseline.tokens
      : null;

  return {
    computable: true,
    reason: null,
    turns,
    tokens,
    // NAMED, NOT INFERRED FROM THE SIGN. Fewer turns is better and a negative Δ
    // is therefore an improvement — which is the opposite of the convention a
    // reader brings to a number with a minus in front of it. The board renders
    // this word; it does not re-derive the polarity.
    better: turns < 0,
  };
}

/**
 * THE BEST RUN UNDER A PROFILE — fewest turns against the floor.
 *
 * EFFICIENCY ONLY, AND SAID SO. This is a turns comparison and nothing else: it
 * does not know whether the run that took fewest turns also passed fewer gates.
 * The board's hard rule is that the two axes are never combined into one number,
 * so this one is LABELLED with its axis wherever it is drawn rather than being
 * presented as "best" without qualification.
 */
function bestDelta(runs, baseline) {
  const scored = runs.filter((r) => r.delta?.computable === true);
  if (!scored.length) return null;
  const best = scored.reduce((a, b) => (b.delta.turns < a.delta.turns ? b : a));
  return {
    run_seq: best.seq,
    turns: best.delta.turns,
    tokens: best.delta.tokens,
    better: best.delta.better,
    axis: "efficiency",
    note: "fewest turns against this model's floor. Turns only — it says nothing about gates.",
  };
}

export const MODELS_LEDGER_CONTRACT_VERSION = 1;
