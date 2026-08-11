// ─────────────────────────────────────────────────────────────────────────────
// CHROME — top bar + provenance strip
//
// NON-NEGOTIABLES LIVE HERE:
//  - `bench-mock/self-declared` renders permanently as PLAIN LABEL TEXT.
//    Never a badge. Never a tier. It is provenance, not a credential.
//  - The policy anchor status is shown verbatim. A run on an unverified anchor
//    is not a valid run, so a viewer must be able to see which it is.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, dur, nul, armOf } from "../board.js";

export function renderTopbar(board, { stale, lastError }) {
  const r = board.run ?? {};
  const p = board.provenance ?? {};
  const arm = armOf(r.arm);

  // The feed being stale is information. Say it plainly rather than freezing a
  // number that looks live.
  const feed = stale
    ? `<span class="attest" style="color:var(--fail)">FEED STALE — ${esc(lastError ?? "poll failed")}</span>`
    : "";

  // A cell that has stopped is not a cell that is running. Never imply motion.
  const state = r.state === "complete"
    ? `<span class="attest">CELL COMPLETE${r.terminal_status ? ` · ${esc(r.terminal_status)}` : ""}</span>`
    : r.elapsed_s !== null && r.elapsed_s !== undefined
      ? `<span class="attest">RUNNING ${esc(dur(r.elapsed_s) ?? "")}</span>`
      : `<span class="attest">${nul("no run observed")}</span>`;

  return `
  <div class="topbar">
    <span class="brand">WEVIBE LIVE BENCHMARK</span>
    <span class="ident">${r.cell_label ? esc(r.cell_label) : nul("no cell")}</span>
    <span class="armtag" style="color:${arm.color}">${arm.label}</span>
    <span class="ident">${r.model ? esc(r.model) : nul("model unobserved")}</span>
    <span class="spacer"></span>
    ${feed}
    <span class="attest">${esc(p.attestation ?? "bench-mock/self-declared")}</span>
    ${state}
  </div>`;
}

export function renderProvenance(board) {
  const p = board.provenance ?? {};
  const anchor = p.policy_anchor_status;

  // anchor_verified is the only good state; anything else is shown in the fail
  // colour because it means the run should not be trusted.
  const anchorHtml =
    anchor === null || anchor === undefined
      ? nul("anchor unobserved")
      : `<span style="color:${anchor === "anchor_verified" ? "var(--pass)" : "var(--fail)"}">${esc(anchor)}</span>`;

  const bit = (label, v) => `${label} ${v === null || v === undefined ? nul("—") : esc(String(v))}`;

  return `
  <div class="prov">
    <span>${bit("policy", p.policy_version)} · ${anchorHtml}</span>
    <span>${bit("worker", p.worker_image_fp)}</span>
    <span>${bit("org", board.run?.org_id)}</span>
    <span>${bit("leader", p.leader_fp)}</span>
    <span>${bit("seed", p.seed)}</span>
    <span>corpus ${esc(p.corpus ?? "benchmark")}</span>
    <span>contract v${esc(board.contract_version ?? "?")}</span>
    <span>${renderSourceHealth(board)}</span>
  </div>`;
}

/**
 * Source health. An unwired source is NOT an error — it is a panel that will
 * stay null, and saying which one prevents a viewer reading an empty panel as
 * a broken board.
 */
function renderSourceHealth(board) {
  const s = board.sources ?? [];
  if (!s.length) return nul("no sources");
  const ok = s.filter((x) => x.ok).length;
  const down = s.filter((x) => !x.ok).map((x) => x.id);
  return `sources ${ok}/${s.length}${down.length ? ` · unwired: ${esc(down.join(", "))}` : ""}`;
}
