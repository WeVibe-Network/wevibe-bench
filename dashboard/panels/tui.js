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

import { esc, nul } from "../board.js";
import { renderStartupFeed, startupFeed } from "./startup.js";

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

export function renderTui(board, lifecycle = null) {
  const t = board.tui ?? null;
  const status = t?.status ?? null;

  return `
    <div class="tui-dock ${expanded ? "open" : "min"}">
      ${expanded ? expandedView(board, t, status, lifecycle) : minimizedBar(board, t, status, lifecycle)}
    </div>
    ${confirming ? confirmModal() : ""}`;
}

/**
 * THE YIELD RULE — "once the TUI renders, the startup feed disappears".
 *
 * DERIVED FROM THE REAL SIGNAL, NEVER A TIMER. The feed is displaced by the
 * arrival of an actual painted frame, so it cannot vanish while the mirror is
 * still empty — which is precisely when it is the only thing with anything to
 * say.
 *
 * `frame_withheld` does NOT count. The server drops the frame for a client
 * whose popout is minimized, so treating "no frame" as "no terminal" there
 * would hide the feed behind a frame that was never sent.
 *
 * THE FEED COMES BACK WHEN THE TERMINAL STOPS BEING LIVE. On `failed` and
 * `exited` the mirror is showing a dead or final frame, and that is exactly the
 * moment an operator needs the background-process list again — a mirror that
 * died mid-run is a question the last frame cannot answer. `silent` is treated
 * as still-live: a silent terminal is a real, legible state of a healthy run,
 * and displacing it would be claiming a failure that has not happened.
 */
function terminalHasPainted(t, status) {
  return Boolean(t?.frame) && (status === "live" || status === "silent");
}

// ── MINIMIZED — THE DEFAULT ─────────────────────────────────────────────────
// 34px tall, docked bottom-right. It always says what the mirror is doing, so
// the operator never has to expand it to find out whether it is alive.

function minimizedBar(board, t, status, lifecycle) {
  // THE BLOCKING COUNT RIDES THE MINIMIZED BAR. The dock is minimized by
  // default, so a failure visible only in the expanded view is a failure the
  // operator has to go looking for — which is how the original one was missed.
  const feed = terminalHasPainted(t, status) ? null : startupFeed(board, lifecycle);
  const blocking = feed?.blocking?.length ?? 0;
  return `
    <div class="tui-bar">
      <span class="tui-brand">▌TUI</span>
      <span class="tui-stat">${esc(`${TUI_COLS}×${TUI_ROWS}`)} · ${statusWord(t, status)}</span>
      ${blocking
        ? `<span class="tui-block">${esc(`${blocking} BLOCKING START`)}</span>`
        : feed
          ? `<span class="tui-ready">${esc("startup: ready")}</span>`
          : ""}
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

function expandedView(board, t, status, lifecycle) {
  const painted = terminalHasPainted(t, status);
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
        ${painted ? screen(t, status) : renderStartupFeed(board, lifecycle)}
      </div>
      <div class="tui-foot">${esc(
        painted
          ? "one character cell = one grid cell · white-space: pre · ligatures off · colour fidelity preserved · nothing here accepts a keystroke"
          : "no live terminal frame yet — this space reports the background processes a benchmark start depends on, and yields to the terminal the moment it paints",
      )}</div>
    </div>`;
}

/**
 * THE PAINTED TERMINAL, AND ONLY THAT.
 *
 * ── DEAD BRANCHES PURGED ────────────────────────────────────────────────────
 * This function previously carried five non-frame placeholder states — NOT
 * ATTACHED / FAILED / STARTING / ATTACHING VIEW / NO FRAME. Every one of them
 * is now UNREACHABLE: `expandedView` only calls this when `terminalHasPainted`
 * is true, which requires a real `t.frame` and a live-or-silent status, and
 * routes every other case to the startup feed — which reports the same facts
 * with more of them, in one ranked list, alongside the other nine processes.
 *
 * They are DELETED rather than left behind a condition that can no longer fire.
 * Two surfaces describing the same state is how they drift apart, and a dead
 * branch is indistinguishable from a live one when someone reads it later.
 *
 * SILENT STILL RENDERS THE LAST FRAME, dimmed and labelled — the grid is
 * unchanged and nothing animates, because a silent terminal must look silent.
 */
function screen(t, status) {
  const dim = status === "silent";
  return `
    ${status === "silent" ? banner(`Attached. ${t.reason ?? "no output"} — the mirror is live and the grid is unchanged.`) : ""}
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

// `state()` lived here and rendered the five placeholder screens. It was the
// only caller-facing use of `.tui-state`/`.tui-line`, and every one of its call
// sites is gone — the startup feed answers those states now. Deleted with them.

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
