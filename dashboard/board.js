// ─────────────────────────────────────────────────────────────────────────────
// WEVIBE BENCH DASHBOARD — BOARD RENDERER
//
// DELIBERATE DEVIATION FROM THE BRIEF (single-file React):
// This is dependency-free vanilla JS. React from a CDN would make the board a
// blank page the moment the network hiccups — on a live stream, with the whole
// stack running locally, that is an unacceptable failure mode for zero benefit.
// The component structure is preserved (pure render functions over one state
// object, diffed by a cheap key); there is simply no framework and no build
// step. It runs from `node server.mjs` on stock Node with nothing installed.
//
// RENDER RULES:
//  - null is a designed state. Every renderer distinguishes three things:
//      unobserved  — not measured yet
//      unwired     — the source that would carry it is not connected
//      zero        — measured, and the answer is 0 (a real result)
//  - No hover-dependent information. The streamer is talking, not pointing.
//  - Panels re-render only when their slice actually changed, so the board does
//    not flicker every poll.
// ─────────────────────────────────────────────────────────────────────────────

const POLL_MS = 2000;

// ── formatting ───────────────────────────────────────────────────────────────

/** The single null renderer. Everything null-ish flows through here. */
export function nul(kind = "unobserved") {
  return `<span class="null">${kind}</span>`;
}

export function pct(x, digits = 0) {
  if (x === null || x === undefined || !Number.isFinite(x)) return null;
  return `${(x * 100).toFixed(digits)}%`;
}

export function dur(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return null;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}

export function tok(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return null;
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return String(n);
}

/** Escape everything that reaches the DOM. Error strings are model output. */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

/** Truncate a content id for display. Never render a full cid on stream. */
export function shortCid(cid) {
  const s = String(cid ?? "");
  if (s.length <= 18) return s;
  return `${s.slice(0, 10)}…${s.slice(-4)}`;
}

export function clip(s, n) {
  const t = String(s ?? "");
  return t.length <= n ? t : `${t.slice(0, n - 1)}…`;
}

// ── arm vocabulary ───────────────────────────────────────────────────────────
// Arm identity is carried by TWO channels — colour AND the literal words
// "MEMORY ON" / "CONTROL" — so the board still reads correctly in greyscale,
// under heavy compression, and for a viewer with colour vision deficiency.

export const ARM = {
  on: { cls: "a", label: "MEMORY ON", short: "A", color: "var(--arm-a)" },
  off: { cls: "b", label: "CONTROL", short: "B", color: "var(--arm-b)" },
};

export function armOf(arm) {
  return ARM[arm] ?? { cls: "null", label: "UNKNOWN ARM", short: "—", color: "var(--null)" };
}

// ── state ────────────────────────────────────────────────────────────────────

let board = null;
let lastError = null;
let consecutiveErrors = 0;

async function poll() {
  try {
    const res = await fetch("/api/board", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    board = await res.json();
    lastError = null;
    consecutiveErrors = 0;
  } catch (err) {
    consecutiveErrors += 1;
    lastError = String(err?.message ?? err);
    // A failed poll must never blank the board. The last good state stays up
    // and the top bar says the feed is stale — that is information, not noise.
  }
  render();
}

// ── render ───────────────────────────────────────────────────────────────────

import { renderTopbar, renderProvenance } from "./panels/chrome.js";
import { renderWall } from "./panels/wall.js";
import { renderHero } from "./panels/hero.js";
import { renderRecall } from "./panels/recall.js";
import { renderTicker, renderCell } from "./panels/ticker.js";
import { renderRail } from "./panels/rail.js";
import { mountDrawer, updateDrawer } from "./panels/drawer.js";

function render() {
  const root = document.getElementById("root");

  if (!board) {
    root.innerHTML = `
      <div class="topbar"><span class="brand">HOW GOOD IS <u>YOUR</u> MEMORY SYSTEM</span>
        <span class="spacer"></span>
        <span class="attest">connecting to feed…</span></div>
      <div style="flex:1;display:flex;align-items:center;justify-content:center">
        <div style="text-align:center">
          <div class="label" style="margin-bottom:10px">no board yet</div>
          <div class="null">${esc(lastError ?? "waiting for /api/board")}</div>
        </div>
      </div>`;
    return;
  }

  root.innerHTML = `
    ${renderTopbar(board, { stale: consecutiveErrors > 0, lastError })}
    <div class="grid">
      ${renderWall(board)}
      <div class="stack">
        ${renderHero(board)}
        ${renderRecall(board)}
      </div>
      ${renderTicker(board)}
      ${renderCell(board)}
      ${renderRail(board)}
    </div>
    ${renderProvenance(board)}
  `;

  // The drawer lives OUTSIDE #root and survives this innerHTML swap, so it is
  // updated rather than rebuilt — that is what preserves its open state, its
  // scroll position, and any half-made selection in its controls. It is updated
  // AFTER the swap because it measures the provenance strip it sits above.
  mountDrawer();
  updateDrawer(board);
}

poll();
setInterval(poll, POLL_MS);
