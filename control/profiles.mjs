// ─────────────────────────────────────────────────────────────────────────────
// MEMORY PROFILES — durable storage, frozen at creation
//
// ── A PROFILE FREEZES TWO FACTS, NOT ONE (WO-BOARD-PROFILE-2) ────────────────
//
// This module first shipped with a single flat `models[]` allowlist, which
// silently conflated the two axes of the experiment. The operator corrected it:
//
//   SUBJECT MODEL   the OFF→ON pair. This IS the measurement. OFF and ON are
//                   ALWAYS the same model — pair an ON cell on model B against
//                   an OFF floor on model A and the Δ measures A-vs-B capability
//                   rather than memory lift, which is not the claim.
//
//   MEMORY ROSTER   the producer models whose memories may be injected into the
//                   ON sequence. THIS is the experiment variable. The corpus is
//                   model-agnostic by rule (RUNBOOK RC-7: "it accumulates
//                   knowledge regardless of which model produced an entry, which
//                   model consumes it... no rule may be written that does").
//
// The two are ORTHOGONAL. Transfer direction — memories moving from a stronger
// model down to a weaker one, or a weaker one up to a stronger — is a property
// of the SUBJECT↔ROSTER edge and has nothing to do with OFF↔ON. Same-model
// (roster == [subject]) is the base measurement being run today; it is the
// degenerate case of the same shape, not a different one.
//
// DIRECTION IS INFERRED, NEVER DECLARED. There is no direction field to set and
// no picker in the UI — see `transferOf()` below. An operator who could label
// their own experiment "weaker → greater" could label it wrongly, and the label
// would then outlive the run in a frozen file.
//
// ── WHY THIS EXISTS ──────────────────────────────────────────────────────────
//
// Before this module the profile was a BROWSER-LOCAL OBJECT. `createDeclaredProfile()`
// in dashboard/board.js mutated `board.profile` in page memory, and the next 2s
// poll overwrote it because no source populated that field. The operator created
// a profile, saw the modal close, and had nothing: no session, no record, and
// nothing left after a refresh.
//
// That is the defect this module closes. A profile is now a file on disk owned
// by the control plane — the only tier permitted to write — and the dashboard
// READS it like any other source.
//
// ── FROZEN MEANS THERE IS NO WRITE PATH, NOT A DISABLED BUTTON ───────────────
//
// This module deliberately exposes `create` and `read` and NOTHING ELSE. There
// is no update, no patch, no merge. The reason is measurement integrity, not
// tidiness: every run in a stack is measured under one recall policy, so a
// profile edited at run 4 would silently make runs 1-3 incomparable to 4-N and
// the transfer curve would be a line drawn through two different experiments.
//
// An `update()` that no caller uses is still a loaded gun. It is absent.
//
// ── ATTRIBUTION IS RECORDED FROM WHAT THIS SERVICE OBSERVED ──────────────────
//
// A run is attached to a profile ONLY when this service launched it while that
// profile was active. Runs launched at the CLI are real and are NOT attributable
// to any profile — they are reported as unattributed rather than being silently
// swept into the newest profile, which would inflate its history with cells that
// were never run under its allowlist.
//
// ── THE MEMORY ROSTER IS DECLARED, NOT ENFORCED ──────────────────────────────
//
// `enforced` is hardcoded false and is NOT a placeholder awaiting wiring. The
// roster is meant to be applied at the PLUGIN, on the recall path. Today no
// recall request carries a producer-model allowlist: `producer_model_id` is
// written to the Qdrant payload and read back, but no consumer filters on it.
// Storing an allowlist does not create one. The field says so in the data so the
// UI cannot quietly imply otherwise, and it flips only when that filter lands.
//
// The consequence is specific and must stay legible: until the filter ships,
// every ON cell recalls the WHOLE corpus, so a declared cross-model roster does
// not actually produce a cross-model experiment. `transferOf()` reports the
// DECLARED edge; `enforced:false` says it was not applied. Both are true and
// neither may be dropped.
//
// The SUBJECT is a different matter — it is enforced, at run start, by
// `validateStart()` in server.mjs. A cell whose model is not the profile's
// subject is refused outright, because that is the one thing this service can
// actually hold to.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";

/** Where profiles live, under the runs root so they survive a service restart. */
export function profilesDir(runsRoot) {
  return join(runsRoot, "profiles");
}

/**
 * A short, stable, human-quotable id — `prof-8d1e`, matching the design.
 *
 * Derived from the frozen content (models + creation instant) rather than a
 * counter: a counter needs a shared mutable cursor, and two creations racing on
 * one would collide on an id that is supposed to be permanent.
 */
function profileId(subject, memoryModels, createdAt) {
  const h = createHash("sha256")
    .update(JSON.stringify({ subject, memory: [...memoryModels].sort(), createdAt }))
    .digest("hex");
  return `prof-${h.slice(0, 4)}`;
}

/**
 * INFER the transfer edge from the two frozen facts. Never stored, never
 * declared — derived on every read from `subject_model` vs `memory_models`.
 *
 * Derived rather than frozen because the RANK basis does not exist at creation
 * time. Saying which of two models is "greater" requires a measurement, and the
 * only honest one this bench produces is each model's own OFF baseline on the
 * same task under the same deterministic oracle. Those baselines accumulate
 * AFTER the profile is frozen, so a direction written at freeze time would be a
 * guess that hardens into a record.
 *
 * So `kind` — which is pure identity comparison and therefore knowable
 * immediately — is always answered. `direction` is answered only for `self`,
 * where it is `same` by construction and needs no rank at all. Every cross-model
 * edge reports `unranked` and names what would rank it.
 *
 * This is the same discipline as `gate_universe: obs` and `enforced: false`:
 * report what is provable, label what is not, invent nothing. A control plane
 * that ranked two models by fiat would be computing a verdict, which rule 3 of
 * the control contract forbids outright.
 */
export function transferOf(profile) {
  const subject = typeof profile?.subject_model === "string" ? profile.subject_model : null;
  const roster = Array.isArray(profile?.memory_models) ? profile.memory_models : [];

  if (!subject || !roster.length) {
    return {
      kind: null,
      direction: null,
      self: false,
      foreign: [],
      note: "transfer edge unobservable — the profile is missing a subject or a memory roster",
    };
  }

  const foreign = roster.filter((m) => m !== subject);
  const includesSelf = roster.includes(subject);

  // SELF — the base measurement. Same model to same model, no rank required:
  // the producer and the consumer are the same identity, so "greater" and
  // "weaker" do not apply rather than being unknown.
  if (!foreign.length) {
    return {
      kind: "self",
      direction: "same",
      self: true,
      foreign: [],
      note:
        "same model to same model — the base measurement. The subject recalls only memories it " +
        "authored itself, so no capability gradient is crossed and no ranking is needed.",
    };
  }

  return {
    kind: includesSelf ? "mixed" : "cross",
    // NOT a gap to be filled in by the operator. See the header.
    direction: "unranked",
    self: includesSelf,
    foreign,
    note:
      `memories from ${foreign.length} model${foreign.length === 1 ? "" : "s"} other than the subject ` +
      `(${foreign.join(", ")})${includesSelf ? ", plus the subject's own" : ""}. Whether that is a ` +
      "transfer up or down is UNRANKED: ranking two models requires a measured floor for each on " +
      "this task, and the bench ranks nothing by declaration. The edge is recorded; the gradient " +
      "is not claimed.",
  };
}

/**
 * Write atomically: full file to a temp name, then rename.
 *
 * A partial write of a profile is worse than no profile — the board would read
 * truncated JSON, fail to parse, and report "no profile" for a stack that has
 * one, which is a lie about frozen state. rename(2) is atomic within a
 * filesystem, so a reader sees either the old file or the whole new one.
 */
async function writeAtomic(path, text) {
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`;
  await fs.writeFile(tmp, text, "utf8");
  try {
    await fs.rename(tmp, path);
  } catch (err) {
    await fs.unlink(tmp).catch(() => {});
    throw err;
  }
}

async function readJsonOrNull(path) {
  try {
    return JSON.parse(await fs.readFile(path, "utf8"));
  } catch {
    return null;
  }
}

/**
 * Every profile on disk, NEWEST FIRST.
 *
 * A file that fails to parse is REPORTED, never skipped silently. A profile that
 * exists but cannot be read is a different fact from a profile that does not
 * exist, and collapsing the two would hide a corrupt freeze record.
 */
export async function listProfiles(runsRoot) {
  const dir = profilesDir(runsRoot);
  let entries;
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch {
    // The directory not existing is the normal pre-first-profile state, not an
    // error: it means zero profiles, which is a real and expected answer.
    return { profiles: [], unreadable: [] };
  }

  const profiles = [];
  const unreadable = [];
  for (const ent of entries) {
    if (!ent.isFile() || !ent.name.endsWith(".json")) continue;
    const p = await readJsonOrNull(join(dir, ent.name));
    // The transfer edge is attached HERE, on every read, and is deliberately
    // never written to disk. Deriving it on read means it can be sharpened as
    // the rank basis becomes available without rewriting a frozen file — and a
    // frozen file must never be rewritten.
    if (p && typeof p.id === "string") profiles.push({ ...p, transfer: transferOf(p) });
    else unreadable.push(ent.name);
  }

  profiles.sort((a, b) => String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")));
  return { profiles, unreadable };
}

/**
 * The ACTIVE profile is the newest one.
 *
 * Deliberately derived rather than stored as a pointer. A stored "active" marker
 * is mutable state that can disagree with the files it points at — and the one
 * question it would answer (which profile does a new run belong to?) already has
 * an unambiguous answer, because a new stack means a new profile.
 */
export async function activeProfile(runsRoot) {
  const { profiles } = await listProfiles(runsRoot);
  return profiles.length ? profiles[0] : null;
}

/**
 * Freeze a new profile.
 *
 * TWO REQUIRED FACTS, refused separately so the operator learns WHICH is missing:
 *
 *   `subjectModel`  the OFF→ON pair. Without it there is no measurement at all —
 *                   a memory roster with nothing to measure describes an
 *                   experiment that cannot be run.
 *   `memoryModels`  the producer allowlist. An EMPTY roster states a policy that
 *                   can never admit a memory, and since nothing is enforced today
 *                   it would read as "no filter" while claiming to be one. That
 *                   ambiguity is exactly what the debt badge exists to prevent.
 *
 * The subject is NOT auto-added to the roster. A profile that qualifies only
 * foreign memories is a legitimate — and interesting — experiment: the subject
 * recalls nothing it wrote itself. Silently inserting the subject would convert
 * a pure cross-model run into a mixed one behind the operator's back.
 */
export async function createProfile(runsRoot, { subjectModel, memoryModels, stackId = null, note = null }) {
  const idOf = (m) => String(typeof m === "string" ? m : m?.id ?? "").trim();

  const subject = idOf(subjectModel);
  const list = Array.isArray(memoryModels)
    ? [...new Set(memoryModels.map(idOf).filter(Boolean))]
    : [];

  if (!subject) {
    return {
      ok: false,
      code: "profile_no_subject",
      reason:
        "a profile must name the subject model — the model whose OFF→ON pair is the measurement. " +
        "OFF and ON are always the same model; without a subject there is nothing being measured, " +
        "only a memory roster with no experiment attached to it",
    };
  }

  if (!list.length) {
    return {
      ok: false,
      code: "profile_empty",
      reason:
        "a profile must qualify at least one producer model for injected memories — an empty " +
        "roster states a policy that admits nothing, and with enforcement absent it would read " +
        "as no filter at all",
    };
  }

  const createdAt = new Date().toISOString();
  const id = profileId(subject, list, createdAt);
  const dir = profilesDir(runsRoot);
  const path = join(dir, `${id}.json`);

  const existing = await readJsonOrNull(path);
  if (existing) {
    // Frozen means frozen. Creating over an existing id would rewrite a record
    // that runs have already been measured against.
    return {
      ok: false,
      code: "profile_exists",
      reason: `profile ${id} already exists and is frozen — a profile is never rewritten`,
    };
  }

  const profile = {
    id,
    created_at: createdAt,
    // THE MEASUREMENT. OFF and ON are both this model, always.
    subject_model: subject,
    // THE EXPERIMENT VARIABLE. Producer models eligible for injection.
    memory_models: list,
    stack_id: stackId,
    // NOT a placeholder. See the header: no recall path filters by producer
    // model, so storing the roster does not apply it.
    enforced: false,
    // Runs this service launched while this profile was active. Append-only.
    runs: [],
    note,
  };

  await fs.mkdir(dir, { recursive: true });
  await writeAtomic(path, `${JSON.stringify(profile, null, 2)}\n`);
  // The transfer edge rides the response so the caller never has to re-derive
  // it — one derivation, in one place, for every consumer.
  return { ok: true, profile: { ...profile, transfer: transferOf(profile) } };
}

/**
 * Record that a cell was launched under a profile.
 *
 * APPEND-ONLY, and it records only what was observed at launch: the log the
 * runner writes, the arm, the model, and when. It never records a verdict — the
 * scoring artifacts own that, and a second copy would be free to drift from them.
 *
 * Best-effort BY DESIGN: a failure here must never abort a run that has already
 * been spawned. Losing the attribution of one cell is recoverable; killing a
 * launched benchmark to preserve bookkeeping is not. The failure is returned so
 * the caller can report it rather than swallow it.
 */
export async function attachRun(runsRoot, profileId_, entry) {
  const path = join(profilesDir(runsRoot), `${String(profileId_)}.json`);
  const profile = await readJsonOrNull(path);
  if (!profile) {
    return { ok: false, code: "profile_missing", reason: `profile ${profileId_} not found on disk` };
  }
  if (!Array.isArray(profile.runs)) profile.runs = [];
  profile.runs.push({
    log_name: entry?.log_name ?? null,
    arm: entry?.arm ?? null,
    model: entry?.model ?? null,
    org: entry?.org ?? null,
    context: entry?.context ?? null,
    pid: entry?.pid ?? null,
    started_at: entry?.started_at ?? Date.now(),
  });
  await writeAtomic(path, `${JSON.stringify(profile, null, 2)}\n`);
  return { ok: true };
}

/**
 * The read view the board consumes.
 *
 * `attributable` is stated explicitly rather than left for the UI to infer: it
 * is false when profiles exist but this service launched none of the runs (the
 * CLI-launch case), and the inspector must then say the history is incomplete
 * instead of drawing an empty list that looks like "no runs happened".
 */
export async function readProfiles(runsRoot) {
  const { profiles, unreadable } = await listProfiles(runsRoot);
  const active = profiles.length ? profiles[0] : null;
  return {
    active,
    prior: profiles.slice(1),
    count: profiles.length,
    unreadable,
    enforced: false,
    enforcement_note:
      "recall cannot filter by producing model today — producer_model_id is written to the " +
      "payload and read back, but no recall request carries an allowlist. Every ON run under " +
      "any profile retrieves from the whole corpus.",
    // Stated separately because the two halves of a profile have DIFFERENT
    // enforcement status, and collapsing them into one badge would understate
    // the subject rule and overstate the roster.
    subject_enforced: true,
    subject_enforcement_note:
      "the subject model IS enforced: /api/run/start refuses any cell whose model is not the " +
      "active profile's subject, so the OFF→ON pair cannot drift mid-stack.",
  };
}
