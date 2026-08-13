// ─────────────────────────────────────────────────────────────────────────────
// PANEL: TUI POPOUT
//
// The operator's complaint, verbatim: "the TUI was added to the space, the
// space was not made for the TUI."
//
// ── THE BOX IS DERIVED FROM THE GRID, NEVER THE OTHER WAY ROUND ─────────────
// The terminal is a HARD 40×130 character grid with fixed monospace metrics.
// At 8.4px advance and 17px leading that is exactly 1092 × 680 of content.
// Those numbers are computed below FROM the grid constants, so a change to
// TUI_ROWS/TUI_COLS moves the box rather than clipping the frame. Nothing
// squeezes the grid into whatever space was left over — that is the entire
// defect being fixed.
//
// ── STRICTLY READ-ONLY ──────────────────────────────────────────────────────
// The mirror never writes to the PTY. There is no input, no focusable field,
// no caret, and nothing that suggests typing into it — a `pre`, not a
// `textarea`. The header says READ-ONLY MIRROR — NO INPUT in words.
//
// ── FIDELITY ────────────────────────────────────────────────────────────────
// white-space: pre · ligatures off · one character cell = one grid cell ·
// colour preserved from the run-length frame. Serialised frames arrive as rows
// of styled runs (control/tui.mjs Screen.serialise) and are rebuilt span by
// span, so colour survives the trip.
//
// ── NON-LIVE STATES ARE NOT ERRORS ──────────────────────────────────────────
//   starting  first paint takes ~10s. NORMAL, not a hang. The elapsed counter
//             runs so the wait is visibly bounded.
//   failed    could not attach — verbatim reason.
//   silent    attached, no output. The grid is UNCHANGED and NOTHING is drawn
//             moving: a silent terminal must look silent.
//   exited    the last frame is held, dimmed, and labelled as the final frame.
//
// ── DETACH & CLOSE IS DESTRUCTIVE AND SAYS SO ───────────────────────────────
// Always present, confirmed, and phrased plainly: it kills the PTY, the mirror
// stops, and there is no reattach. The benchmark cell keeps running — the
// operator just will not see its terminal again. Saying "close" without saying
// "for good" would be the lie.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, dur } from "../board.js";

/** Must match control/tui.mjs. Asserted by a drift guard. */
export const TUI_ROWS = 40;
export const TUI_COLS = 130;
/** Measured monospace metrics at 13px JetBrains Mono. */
export const CH_W = 8.4;
export const CH_H = 17;

export const GRID_W = Math.round(TUI_COLS * CH_W); // 1092
export const GRID_H = TUI_ROWS * CH_H; // 680

let expanded = false;
let confirming = false;

export function toggleTui() {
  expanded = !expanded;
  confirming = false;
}
export function askDetach() {
  confirming = true;
}
export function cancelDetach() {
  confirming = false;
}
export function isTuiExpanded() {
  return expanded;
}
export function isDetachConfirming() {
  return confirming;
}

export function renderTui(board) {
  const t = board.tui ?? null;
  const status = t?.status ?? null;

  return `
    <div class="tui-dock ${expanded ? "open" : "min"}">
      ${expanded ? expandedView(t, status) : minimizedBar(t, status)}
    </div>
    ${confirming ? confirmModal() : ""}`;
}

// ── MINIMIZED — THE DEFAULT ─────────────────────────────────────────────────
// 34px tall, docked bottom-right. It always says what the mirror is doing, so
// the operator never has to expand it to find out whether it is alive.

function minimizedBar(t, status) {
  return `
    <div class="tui-bar">
      <span class="tui-brand">▌TUI</span>
      <span class="tui-stat">${esc(`${TUI_COLS}×${TUI_ROWS}`)} · ${statusWord(t, status)}</span>
      <span class="spacer"></span>
      <button class="btn sm" data-tui-toggle="1">EXPAND ⌃</button>
      <button class="btn sm destroy" data-tui-detach="1">DETACH &amp; CLOSE</button>
    </div>`;
}

function statusWord(t, status) {
  if (!t) return `<span class="null">not attached</span>`;
  if (status === "live") {
    const age = t.last_data_at ? Math.round((Date.now() - t.last_data_at) / 1000) : null;
    return `<span class="bright">live</span>${age !== null ? ` · ${esc(String(age))}s` : ""}`;
  }
  if (status === "starting") return `<span class="muted">attaching…</span>`;
  if (status === "failed") return `<span class="danger">failed</span>`;
  if (status === "silent") return `<span class="muted">silent</span>`;
  if (status === "exited") return `<span class="muted">exited</span>`;
  return nul("status unobserved");
}

// ── EXPANDED — SIZED TO THE GRID ────────────────────────────────────────────

function expandedView(t, status) {
  return `
    <div class="tui-win" style="width:${GRID_W + 28}px">
      <div class="tui-head">
        <span class="tui-brand">▌TUI MIRROR</span>
        <span class="note">${esc(`${TUI_COLS} cols × ${TUI_ROWS} rows`)}${t?.session_id ? esc(` · pty ${String(t.session_id).slice(0, 8)}`) : ""}</span>
        <span class="tag">READ-ONLY MIRROR — NO INPUT</span>
        <span class="spacer"></span>
        <button class="btn sm" data-tui-toggle="1">MINIMIZE ⌄</button>
        <button class="btn sm destroy" data-tui-detach="1">DETACH &amp; CLOSE</button>
      </div>
      <div class="tui-screen" style="width:${GRID_W}px;height:${GRID_H}px">
        ${screen(t, status)}
      </div>
      <div class="tui-foot">${esc("one character cell = one grid cell · white-space: pre · ligatures off · colour fidelity preserved · nothing here accepts a keystroke")}</div>
    </div>`;
}

function screen(t, status) {
  if (!t) {
    return state("NOT ATTACHED", "The TUI mirror is not running.", "The control plane is not enabled, so no capture can be started.");
  }

  if (status === "failed") {
    return state("FAILED", "Could not attach to the session.", t.reason ?? "no reason given", true);
  }

  if (status === "starting") {
    const waited = t.started_at ? Math.round((Date.now() - t.started_at) / 1000) : null;
    return state(
      "STARTING",
      `Attaching to the PTY. First paint takes about 10 seconds.${waited !== null ? ` Waited ${waited}s.` : ""}`,
      t.reason ?? "This is normal, not a hang. The elapsed counter runs so the wait is visibly bounded.",
    );
  }

  if (!t.frame) {
    return state("NO FRAME", "Attached, but nothing has been painted yet.", t.reason ?? "the client has produced no output");
  }

  // SILENT and EXITED still render the LAST FRAME — the grid is unchanged and
  // nothing animates. A silent terminal must look silent.
  const dim = status === "exited" || status === "silent";
  return `
    ${status === "silent" ? banner(`Attached. ${t.reason ?? "no output"} — the mirror is live and the grid is unchanged.`) : ""}
    ${status === "exited" ? banner(`${t.reason ?? "session ended"} — this is the final frame.`) : ""}
    <pre class="tui-pre ${dim ? "dim" : ""}" aria-readonly="true">${frame(t.frame)}</pre>`;
}

function banner(text) {
  return `<div class="tui-banner">${esc(text)}</div>`;
}

/** Rebuild the run-length frame. Colour is carried per run, never guessed. */
function frame(rows) {
  return rows
    .map((runs) =>
      runs
        .map((r) => {
          const style = [
            r.fg ? `color:${cssColor(r.fg)}` : "",
            r.bg ? `background:${cssColor(r.bg)}` : "",
            r.bold ? "font-weight:700" : "",
          ]
            .filter(Boolean)
            .join(";");
          const text = esc(r.t);
          return style ? `<span style="${style}">${text}</span>` : text;
        })
        .join(""),
    )
    .join("\n");
}

/**
 * SGR index → colour. Terminal output is model-authored, so the value is
 * mapped through a fixed table and never interpolated into CSS raw.
 */
const ANSI = [
  "#02100a", "#ff6b6b", "#2fe07a", "#b8ffcf", "#54f59a", "#66ffa6", "#aaffc8", "#e2ffec",
  "#1d7a44", "#ff6b6b", "#6dff9e", "#b8ffcf", "#54f59a", "#66ffa6", "#aaffc8", "#e2ffec",
];
function cssColor(v) {
  const n = Number(v);
  return Number.isInteger(n) && n >= 0 && n < ANSI.length ? ANSI[n] : "inherit";
}

function state(label, line, reason, bad = false) {
  return `
    <div class="tui-state">
      <span class="ttl ${bad ? "danger" : ""}">${esc(label)}</span>
      <span class="tui-line">${esc(line)}</span>
      <span class="note">${esc(reason)}</span>
    </div>`;
}

// ── DETACH CONFIRM ──────────────────────────────────────────────────────────

function confirmModal() {
  return `
    <div class="modal-scrim" data-tui-cancel="1">
      <div class="modal tui-confirm" role="dialog" aria-modal="true">
        <span class="ttl danger">ARE YOU SURE? (Y/N)</span>
        <span class="modal-title">Detach and close this TUI session for good?</span>
        <span class="note body">This kills the PTY. The session ends, the mirror stops, and there is no reattach — the benchmark cell keeps running, but you will not see its terminal again.</span>
        <div class="confirm-actions">
          <button class="btn destroy solid" data-tui-detach-yes="1">YES — CLOSE THE SESSION</button>
          <button class="btn" data-tui-cancel="1">NO — KEEP IT OPEN</button>
        </div>
      </div>
    </div>`;
}
