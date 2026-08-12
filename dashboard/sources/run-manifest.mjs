// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: run-manifest
//
// Reads <runs_root>/<run_dir>/manifest.json — written at run start. Carries the
// provenance a skeptical engineer checks FIRST:
//
//   - policy version + anchor verification status (a run on an unverified
//     anchor is not a valid run — RUNBOOK §6)
//   - the recall-mode lever, which is what makes the approval gate auto-approve
//   - org, model, seed, roster
//
// GATE MODE IS DERIVED, NEVER HARDCODED. It reads the L4_WEVIBE_RECALL_MODE
// lever the harness recorded. `test` mode auto-approves recalled memories;
// prod/unset blocks on a human popup no headless run can answer. If the lever
// is absent we report null and the UI says so — we never assume auto-approve.
// ─────────────────────────────────────────────────────────────────────────────

import { str, int } from "../contract.mjs";
import { readJson, activeRun } from "./_runtime.mjs";

export const id = "run-manifest";
export const fields = ["provenance", "run.org_id", "run.model"];
export function describe() {
  return "run manifest — policy anchor, levers, org, model identity (RC-5)";
}

export async function read(ctx) {
  // Scoped to the active run, so provenance describes the run whose gates are
  // on the wall. Picking the newest manifest INDEPENDENTLY of the stream could
  // pair one run's policy anchor with another run's measurements.
  const run = await activeRun(ctx.runsRoot);
  if (!run?.manifestPath) return { ok: false, reason: "no manifest.json under runs root" };

  const m = await readJson(run.manifestPath);
  if (!m) return { ok: false, reason: "manifest.json unreadable or malformed" };

  const levers = m.run_context?.levers ?? {};
  const recallMode = str(levers.L4_WEVIBE_RECALL_MODE?.value);
  const edge = m.run_context?.edge_policy ?? {};

  const schedule = Array.isArray(m.schedule) ? m.schedule : [];
  const roster = Array.isArray(m.roster) ? m.roster : [];
  const model =
    str(schedule[0]?.provider_pin) ??
    str(roster[0]?.provider_pin) ??
    str(roster[0]?.model);

  return {
    ok: true,
    provenance: {
      path: run.manifestPath,
      mtime: run.manifestStat?.mtimeMs ?? null,
      bytes: run.manifestStat?.size ?? null,
      run: run.name,
    },
    patch: {
      run: {
        org_id: str(m.org_id),
        model,
        started_at: Date.parse(str(m.created_at) ?? "") || null,
      },
      provenance: {
        // test-mode auto-approves the gate; anything else blocks on a human.
        gate_mode: recallMode === null ? null : recallMode === "test" ? "auto-approve" : "human",
        gate_mode_source: recallMode ? `L4_WEVIBE_RECALL_MODE=${recallMode}` : null,
        policy_version: str(edge.version),
        policy_anchor_status: str(edge.anchor_status),
        seed: int(m.seed),
      },
    },
  };
}
