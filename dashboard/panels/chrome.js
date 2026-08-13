// ─────────────────────────────────────────────────────────────────────────────
// CHROME — top bar + provenance strip, restyled to v2
//
// NON-NEGOTIABLES LIVE HERE:
//  - BRAND TEXT IS A HARD REQUIREMENT: HOW GOOD IS <u>YOUR</u> MEMORY SYSTEM.
//  - `bench-mock/self-declared` renders permanently as PLAIN LABEL TEXT.
//    Never a badge. Never a tier. It is provenance, not a credential.
//  - The policy anchor status is shown verbatim. A run on an unverified anchor
//    is not a valid run, so a viewer must be able to see which it is.
//  - NOWRAP IS LOAD-BEARING: these are single-line identity chips. Only the
//    long identifiers (stack id, model) may ellipsize.
//  - A cell that has stopped must never imply motion (constraint 7).
// ─────────────────────────────────────────────────────────────────────────────

import { esc, dur, nul, controlReachability } from "../board.js";

export function renderTopbar(board, { stale, lastError }) {
  const r = board.run ?? {};
  const p = board.provenance ?? {};
  const s = board.stack ?? {};

  // The feed being stale is information. Say it plainly rather than freezing a
  // number that looks live.
  const feed = stale
    ? `<span class="chip danger">FEED STALE — ${esc(lastError ?? "poll failed")}</span>`
    : "";

  // A stopped cell is not a running cell. Never imply motion in a stopped cell.
  //
  // STALLED IS ITS OWN BRANCH, AND IT IS LOUD. A wedged cell used to fall
  // through to "no run observed" — the most dangerous possible rendering, since
  // a run burning hours in an unbounded nudge loop read as nothing happening at
  // all. Recovery is unbounded by design (WO-NUDGE-INF-1) and hang detection was
  // delegated to a poller that did not exist; this chip is that poller's output
  // and it sits in the most-read spot on the board.
  //
  // IT CLAIMS A STALL, NOT A FAILURE. The run may still recover — the harness
  // is nudging. What it asserts is only what was measured: nothing has been
  // written for this long.
  const state =
    r.state === "complete"
      ? `<span class="tag">CELL COMPLETE${r.terminal_status ? ` · ${esc(r.terminal_status)}` : ""}</span>`
      : r.state === "stalled"
        ? `<span class="chip danger">CELL STALLED${
            r.log_silent_s === null || r.log_silent_s === undefined
              ? ""
              : ` — SILENT ${esc(dur(r.log_silent_s))}`
          }</span>`
        : r.state === "running"
          ? `<span class="tag on">RUNNING${r.elapsed_s !== null && r.elapsed_s !== undefined ? ` ${esc(dur(r.elapsed_s))}` : ""}</span>`
          : `<span class="chip dimchip">${nul("no run observed")}</span>`;

  const stackId = s.id ? String(s.id).split("|")[0] : null;

  // WRITES → CONTROL PLANE is a CLAIM, and it is false when the browser cannot
  // reach the control plane. Leaving it up while every write silently fails is
  // the same defect class as a button that looks alive and is not.
  const reach = controlReachability(board);

  return `
  <div class="topbar">
    <span class="brand">HOW GOOD IS <u>YOUR</u> MEMORY SYSTEM</span>
    <span class="vr"></span>
    <span class="chip shrink">stack <span class="bright">${stackId ? esc(stackId) : nul("no stack")}</span></span>
    <span class="chip dimchip shrink">${r.model ? esc(r.model) : nul("model unobserved")}</span>
    <span class="spacer"></span>
    ${feed}
    <span class="chip">${esc(p.attestation ?? "bench-mock/self-declared")}</span>
    ${state}
    ${reach.ok
      ? `<span class="tag">READ-ONLY · WRITES → CONTROL PLANE</span>`
      : `<span class="tag bad">READ-ONLY · CONTROLS UNAVAILABLE HERE</span>`}
  </div>
  ${reach.ok ? "" : reachBanner(reach)}`;
}

/**
 * Why the controls are dead, stated ONCE at the top of the board rather than
 * per-button. The operator learns it before clicking, not after a failure.
 *
 * The remedy is included because this condition is fixable by the operator in
 * one command — and a diagnosis with no remedy leaves them exactly as stuck.
 */
function reachBanner(reach) {
  return `
  <div class="reach-warn" role="alert">
    <span class="rw-head">CONTROL PLANE NOT REACHABLE FROM THIS BROWSER</span>
    <span class="rw-body">${esc(reach.reason)}</span>
    ${reach.fix ? `<span class="rw-fix">${esc(reach.fix)}</span>` : ""}
    <span class="rw-note">${esc("The board itself is fully live — everything you can see is real and current. Only the controls that write are unavailable.")}</span>
  </div>`;
}

export function renderProvenance(board) {
  const p = board.provenance ?? {};
  const anchor = p.policy_anchor_status;

  // anchor_verified is the only good state; anything else means the run should
  // not be trusted, and it is shown in the one off-hue rather than quietly.
  const anchorHtml =
    anchor === null || anchor === undefined
      ? nul("anchor unobserved")
      : `<span class="${anchor === "anchor_verified" ? "bright" : "danger"}">${esc(anchor)}</span>`;

  const bit = (label, v) =>
    `${label} ${v === null || v === undefined ? nul("—") : esc(String(v))}`;

  return `
  <div class="prov">
    <span>${bit("policy", p.policy_version)} · ${anchorHtml}</span>
    <span>${bit("worker", p.worker_image_fp)}</span>
    <span>${bit("org", board.run?.org_id)}</span>
    <span>${bit("leader", p.leader_fp)}</span>
    <span>${bit("seed", p.seed)}</span>
    <span>clock: harness wall, not agent-reported</span>
    <span class="spacer"></span>
    <span>serial by contract — one session, one cell, HTTP 409 on overlap</span>
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
