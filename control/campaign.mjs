// ─────────────────────────────────────────────────────────────────────────────
// CAMPAIGN LAYOUT — which manifest a cell writes to
//
// Split out of server.mjs so it can be TESTED: importing server.mjs binds a
// port, so anything only reachable from there is only reachable by running the
// service. This is pure path resolution over what is already on disk, and it
// decides where hours of measurement land — exactly the kind of rule that must
// be asserted rather than read.
// ─────────────────────────────────────────────────────────────────────────────

import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

async function readJsonOrNull(path) {
  try {
    return JSON.parse(await readFile(path, "utf8"));
  } catch {
    return null;
  }
}

// ── ONE CAMPAIGN DIRECTORY PER MODEL ────────────────────────────────────────
//
// THE PROBLEM THIS SOLVES. The harness defaults every run to
// `runs/cumulative/manifest.json`, and a manifest FREEZES its roster hash. The
// sequencer re-checks that hash on EVERY launch (not only on resume) and refuses
// on drift. So a baseline for a second model, launched with no --manifest, died
// at startup with `roster hash drift detected` — a button that looked live and
// could never work. Enabling [+ baseline] for every un-floored model (which is
// the rule: one floor per model) is only true if each model has somewhere to put
// its campaign.
//
// THE LEGACY DIRECTORY KEEPS ITS OWNER. `runs/cumulative` belongs to whichever
// model's manifest is already sitting in it, and that model keeps launching
// there — the live campaign, its baseline and its status stream are untouched by
// this change. Every OTHER model gets `runs/cumulative-<model>`.
//
// This is not a new concept on the board: the dashboard's stack key is
// (org, task, roster_hash, seed), so a second model was ALREADY a separate stack
// — it simply had nowhere to live on disk. RC-5 (one run directory, one
// manifest, one status stream) is preserved rather than bent.
//
// DOTS ARE STRIPPED FROM THE DIRECTORY NAME, and that is load-bearing rather
// than cosmetic: `isArchivedRun()` treats ANY dot in a run directory name as the
// archive convention (`runs/cumulative.<why>-<date>`), so a directory named for
// `qwen3.6-35b-a3b-bench` would be read as archived and its baseline would
// silently vanish from the floor index.
export function campaignDirName(model) {
  return `cumulative-${String(model).replace(/\./g, "-")}`;
}

/**
 * Which manifest should a cell on `model` write to?
 *
 * Returns null when the default (`runs/cumulative`) is correct, so the launcher
 * passes no --manifest and the invocation stays byte-identical to the RUNBOOK's
 * for the model that already owns it.
 */
export async function manifestArgFor(model, runsRoot) {
  const legacyPath = join(runsRoot, "cumulative", "manifest.json");
  const mine = join(runsRoot, campaignDirName(model), "manifest.json");

  // ABSENT and UNREADABLE ARE DIFFERENT ANSWERS, and collapsing them is the
  // dangerous case. Absent = no campaign has claimed the default directory, so
  // this model names its own and the layout stays uniform. Unreadable = a
  // campaign IS there and cannot be parsed; routing around it would present as
  // the whole run history having vanished, so the default is used and the
  // harness fails loudly on the file that is actually broken.
  if (!existsSync(legacyPath)) return mine;

  const legacy = await readJsonOrNull(legacyPath);
  if (!legacy) return null;

  const owner = legacyManifestModel(legacy);
  if (!owner || owner === model) return null;
  return mine;
}

/**
 * The model a manifest froze. `roster[].model` is provider-prefixed
 * ("local-llm-proxy/qwen…") while `schedule[].provider_pin` is bare, which is
 * the same split collectOffCells reads; the pin is preferred and the prefix is
 * stripped as a fallback rather than assuming either field is present.
 */
function legacyManifestModel(m) {
  const sched = Array.isArray(m?.schedule) ? m.schedule : [];
  for (const s of sched) {
    const pin = typeof s?.provider_pin === "string" ? s.provider_pin.trim() : "";
    if (pin) return pin;
  }
  const roster = Array.isArray(m?.roster) ? m.roster : [];
  for (const r of roster) {
    const id = typeof r?.model === "string" ? r.model.trim() : "";
    if (id) return id.split("/").pop();
  }
  return null;
}

