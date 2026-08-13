// ─────────────────────────────────────────────────────────────────────────────
// PANEL: HOLD / REVIEW
//
// Contract: wevibe-meta/workspace/reports/1786523828-WO-HOLD-UI-2-dashboard-consumer-contract.md
//
// The flow: a run ends → the harness BLOCKS → writes <run_dir>/hold-ui.json →
// the operator opens the artifact, tests it, comes back, releases. Release
// creates a file at `release.path`; the harness polls for it every 2.0s.
//
// SIX STATES, each a designed answer:
//
//   no hold file          render NOTHING. Not an error, not an empty box, not
//                         a spinner. Absence is a specified state.
//   ui_healthy: true      url + release control + held-for-N from started_at
//   ui_healthy: false     NO LINK IS SHOWN. Handing over a URL for an artifact
//                         that never came up wastes the operator's time twice —
//                         they click it, get nothing, and have to work out
//                         whether the link or the artifact is broken. Show
//                         boot_detail + server_log instead. RELEASE STILL WORKS.
//   lan_reachable: true   the artifact ignored the loopback requirement and is
//                         reachable from every device on the network. Named,
//                         with the address, and it must NOT look like a normal
//                         hold — it is a FINDING, not a success.
//   vanished mid-render   hold-ui.json is unlinked ON RELEASE. Disappearance
//                         means RELEASED. It is never an error state.
//   feature off           WEVIBE_BENCH_HOLD_UI unset. Phrased as a choice not
//                         yet made, so it never reads as broken.
//
// CANON (§5.4). Release is NEVER blocked by a dead UI or a bad bind. A board
// that can withhold the operator's ability to continue their own run would be
// a party withholding control over knowledge — precisely what the Four Exit
// Guarantees forbid. The release control is present in every held state.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, dur } from "../board.js";

export function renderHold(board) {
  const h = board.hold;

  // NO HOLD → NOTHING. Rendering an empty panel here would train the operator
  // to ignore a region that matters enormously when it does appear.
  if (!h) return "";

  if (h.feature_off) {
    return band("off", "HOLD FOR REVIEW — OPT-IN, CURRENTLY OFF", [
      `<div class="hold-line">Hold-for-review is opt-in and is currently off.</div>`,
      `<div class="note">Set <span class="bright">WEVIBE_BENCH_HOLD_UI=1</span> before the run to stop the harness at session end so you can test the artifact before the cell closes.</div>`,
    ]);
  }

  if (h.released) {
    // The file vanished between poll and render. That is the success path.
    return band("released", "RELEASED — THE RUN CONTINUES", [
      `<div class="hold-line">Released. The run continues.</div>`,
      `<div class="note">hold-ui.json is unlinked on release. Its disappearance means released — never an error.</div>`,
    ]);
  }

  const lan = h.bind?.lan_reachable === true;
  const healthy = h.ui_healthy === true;

  const kind = lan ? "lan" : healthy ? "ok" : "dead";
  const title = lan
    ? "⚠ LAN EXPOSED — NOT A SUCCESSFUL HOLD"
    : healthy
      ? "HELD — UI HEALTHY"
      : "HELD — UI DEAD";

  const parts = [held(h)];

  if (lan) {
    parts.push(`
      <div class="hold-line">The artifact ignored the loopback requirement. It is bound to <span class="danger bright">${esc(h.bind?.lan_address ?? "an unknown LAN address")}</span> and is reachable from every device on your network.</div>`);
    // The URL is SHOWN but never promoted — this hold is a finding.
    parts.push(urlBox(h, true));
  } else if (healthy) {
    parts.push(urlBox(h, false));
  } else {
    parts.push(`
      <div class="hold-line">The artifact never came up. There is nothing to test.</div>`);
    parts.push(diag(h));
  }

  parts.push(releaseRow(h, kind));
  return band(kind, title, parts);
}

function band(kind, title, parts) {
  return `
    <section class="hold ${kind}">
      <div class="phead">
        <span class="ttl ${kind === "dead" || kind === "lan" ? "danger" : ""}">${esc(title)}</span>
        ${kind === "ok" ? `<span class="sub">ui_healthy: true</span>` : ""}
        ${kind === "dead" ? `<span class="sub">ui_healthy: false · no link is shown</span>` : ""}
        ${kind === "lan" ? `<span class="sub">bind.lan_reachable: true</span>` : ""}
      </div>
      ${parts.join("")}
    </section>`;
}

function held(h) {
  const s = h.held_seconds;
  const since = h.started_at ? new Date(h.started_at).toLocaleTimeString("en-GB", { hour12: false }) : null;
  if (s === null || s === undefined) {
    return `<div class="hold-line">This run is blocked for review. ${nul("hold duration unobserved")}</div>`;
  }
  return `<div class="hold-line">This run is blocked for review. Held <span class="bright">${esc(dur(s))}</span>${since ? ` — since ${esc(since)}` : ""}.</div>`;
}

function urlBox(h, muted) {
  const url = h.url;
  if (!url) return `<div class="null">${esc("no url in hold-ui.json")}</div>`;
  return `
    <div class="hold-url ${muted ? "muted-box" : ""}">
      <span class="muted">$</span>
      ${muted
        ? `<span>${esc(url)}</span>`
        : `<a href="${esc(url)}" target="_blank" rel="noreferrer noopener">${esc(url)}</a>`}
      <span class="spacer"></span>
      <span class="note ${muted ? "danger" : ""}">${esc(muted ? "shown, not promoted — this hold is a finding, not a success" : "loopback only · copy or open")}</span>
    </div>`;
}

/** Verbatim boot failure. Model/tool output — escaped, never interpreted. */
function diag(h) {
  const boot = h.boot_detail;
  const log = h.server_log;
  return `
    <div class="hold-diag">
      <span class="kick">boot_detail</span>
      <span>${boot ? esc(boot) : nul("not captured")}</span>
      <span class="kick">server_log</span>
      <span>${log ? esc(Array.isArray(log) ? log.join("\n") : log) : nul("not captured")}</span>
    </div>`;
}

/**
 * RELEASE IS ALWAYS AVAILABLE. See the Canon note in the header — this control
 * is never disabled, never hidden, and never gated on the artifact's health.
 */
function releaseRow(h, kind) {
  const path = h.release?.path ?? null;
  return `
    <div class="hold-actions">
      <button class="btn ${kind === "ok" ? "primary" : ""}" data-hold-release="1" ${path ? "" : "disabled"}>
        ${esc(kind === "ok" ? "RELEASE — CONTINUE THE RUN" : kind === "dead" ? "RELEASE ANYWAY" : "RELEASE")}
      </button>
      <span class="note">${
        path
          ? esc(kind === "dead"
              ? "release is never blocked by a dead UI"
              : kind === "lan"
                ? "record the bind violation against this cell"
                : "writes release.path · harness polls every 2s")
          : esc("no release.path in hold-ui.json — the harness did not publish one, so this board cannot release the run")
      }</span>
    </div>`;
}
