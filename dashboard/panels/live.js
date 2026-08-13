// ─────────────────────────────────────────────────────────────────────────────
// PANEL: LIVE RUN — absorbs the old ticker and the drawer's event/stage tabs
//
// Two columns: the PHASE SPINE and the EVENT FEED.
//
// ── EXTRACTION MOVED OUT (WO-BOARD-EXTRACT-1) ───────────────────────────────
// This panel used to carry a third column showing extraction stages. It is gone
// — not hidden, not behind a flag — and lives in panels/extraction.js now.
//
// Three reasons, each a defect the column had:
//   · It could only show ONE extraction: whatever the control plane held in
//     memory. Extractions across other cells and sessions were unreachable.
//   · It was scoped to the LIVE RUN panel, so it vanished exactly when a run
//     was holding the board — which is when the operator asked to see it.
//   · A profile is a read filter; extraction is a write into a shared corpus.
//     Rendering it inside a run-scoped panel implied a scoping that is false.
// The queue is now a popout that survives a run holding the board.
//
// ── THE SPINE IS 3 PHASES, NOT 6 ────────────────────────────────────────────
// A cell is BUILD (`initial`) → GRADE (`verdict-pass-1`) → GRADE
// (`verdict-pass-2`). The six work orders are SUB-TICKS INSIDE PHASE 1 and are
// rendered only there. Showing "6 phases" (an earlier misreading) makes a cell
// in phase 2 look 1/6 done when it is 2/3 done.
//
// ── FEED BEHAVIOURS ARE PRESERVED VERBATIM ──────────────────────────────────
// These were built and verified against a live 45s capture and are NOT
// re-derived here — they are carried over intact, and every one of them exists
// because its absence was a real observed defect:
//   · oldest-first, constant 34px row height
//   · per-kind filter chips with live counts, all ON by default
//   · render cap 400, trimmed from the TOP with scroll compensation
//   · sticky-bottom with BOTTOM_EPS=24 tolerance (exact equality drops the
//     operator out of follow mode on fractional scroll heights)
//   · "N new ↓" pill when detached — announces without stealing the viewport
//   · append past a seq watermark, never innerHTML rebuild (a rebuild resets
//     scrollTop every poll and makes "new" undetectable)
//   · one-shot flash on background + inset left rule ONLY — never height,
//     margin or transform, which would reflow the list under the eye
//   · prefers-reduced-motion → static rule, no animation
//
// PROVISIONAL COUNTERS. A running cell's totals are marked provisional and
// suffixed ›. Presenting a mid-flight total as final is the same lie as
// presenting a partial delta as a result.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, tok, dur } from "../board.js";

export const EVENT_KINDS = ["tool", "file", "thinking", "error", "lifecycle"];
const KIND_MARK = { tool: "$", file: "~", thinking: "·", error: "!", lifecycle: "◦" };

export const EVENT_RENDER_CAP = 400;
export const BOTTOM_EPS = 24;

/** Filter state lives here, client-side, and survives the board's re-render. */
const filters = { tool: true, file: true, thinking: true, error: true, lifecycle: true };
export function toggleKind(k) {
  if (k in filters) filters[k] = !filters[k];
}

export function renderLive(board) {
  const r = board.run ?? {};
  const running = r.state === "running";

  return `
    <section class="panel live">
      <div class="live-spine">
        <div class="phead">
          <span class="ttl">LIVE RUN</span>
          <span class="sub">${cellLabel(r)}</span>
        </div>
        ${spine(r, board)}
        ${provisional(r, running)}
      </div>
      <div class="live-feed">
        ${feedHead(board)}
        <div class="evbox" id="sc-events" data-preserve="1"></div>
      </div>
    </section>`;
}

function cellLabel(r) {
  if (!r.arm && !r.cell_label) return nul("no run observed");
  const seq = r.cell_label ? esc(r.cell_label) : "cell";
  const arm = r.arm ? esc(r.arm.toUpperCase()) : nul("arm unobserved");
  return `${seq} · ${arm}`;
}

// ── THE 3-PHASE SPINE ───────────────────────────────────────────────────────

const PHASES = [
  { n: 1, name: "BUILD", label: "initial" },
  { n: 2, name: "GRADE", label: "verdict-pass-1" },
  { n: 3, name: "GRADE", label: "verdict-pass-2" },
];

/**
 * Map the harness phase string onto 1..3. The harness emits `initial`,
 * `initial-chunk-N`, `feedback-1`, `feedback-2`, `verdict-pass-N`.
 */
export function phaseIndex(phase) {
  const p = String(phase ?? "").toLowerCase();
  if (!p) return null;
  if (p.startsWith("initial")) return 1;
  if (p.includes("1")) return 2;
  if (p.includes("2")) return 3;
  return null;
}

/** Chunk number out of `initial-chunk-4`. Meaningful in phase 1 ONLY. */
export function chunkOf(phase) {
  const m = String(phase ?? "").match(/chunk-(\d+)/i);
  return m ? Number(m[1]) : null;
}

function spine(r, board) {
  const active = phaseIndex(r.phase);
  const stopped = r.state === "complete" || r.state === "aborted";

  if (active === null && !stopped) {
    return `<div class="spine"><div class="null">${esc("no phase observed — nothing has reported yet")}</div></div>`;
  }

  return `<div class="spine">${PHASES.map((p) => {
    const state =
      active === null ? "pending" : p.n < active ? "done" : p.n === active ? (stopped ? "done" : "running") : "pending";
    return phaseRow(p, state, r, board);
  }).join("")}</div>`;
}

function phaseRow(p, state, r, board) {
  const title = p.n === 1 ? `${p.n} — ${p.name}` : `${p.n} — ${p.name} · ${p.label}`;
  const word = state === "running" ? "RUNNING" : state === "done" ? "DONE" : "PENDING";

  // Chunks are internal to phase 1 and are drawn ONLY while it is the phase.
  const ticks =
    p.n === 1 && state !== "pending" ? chunkTicks(r, board) : "";

  return `
    <div class="ph ${state}">
      <div class="ph-top"><span>${esc(title)}</span><span class="ph-state">${word}</span></div>
      ${ticks}
    </div>`;
}

function chunkTicks(r, board) {
  const total = r.chunk?.total ?? 6;
  const cur = r.chunk?.current ?? chunkOf(r.phase);
  if (!cur) {
    return `<div class="ph-note">${esc(`${total} work orders — none reported yet`)}</div>`;
  }
  const ticks = Array.from({ length: total }, (_, i) => {
    const n = i + 1;
    const cls = n < cur ? "done" : n === cur ? "now" : "todo";
    return `<span class="tick ${cls}"></span>`;
  }).join("");
  return `
    <div class="ticks">${ticks}</div>
    <div class="ph-note">${esc(`work order ${cur} of ${total} — chunks are internal to phase 1`)}</div>`;
}

function provisional(r, running) {
  const t = r.tokens ?? {};
  const total = t.input !== null && t.output !== null ? t.input + t.output : null;
  const mark = running ? " <span class='muted'>›</span>" : "";

  return `
    <div class="prov-tot">
      <span class="kick">${running ? "PROVISIONAL — RUNNING TOTALS, NOT FINAL" : "CELL TOTALS"}</span>
      <div class="big">
        <span>${r.turns === null || r.turns === undefined ? nul("turns unobserved") : `${esc(String(r.turns))} turns${mark}`}</span>
        <span>${total === null ? nul("tokens unobserved") : `${esc(tok(total))}${mark}`}</span>
        <span>${r.elapsed_s === null || r.elapsed_s === undefined ? nul("time unobserved") : `${esc(dur(r.elapsed_s))}${mark}`}</span>
      </div>
    </div>`;
}

// ── EVENT FEED ──────────────────────────────────────────────────────────────

function feedHead(board) {
  const ev = board.events ?? null;
  const counts = ev?.counts ?? {};
  const chips = EVENT_KINDS.map((k) => {
    const n = counts[k] ?? 0;
    return `<button class="chip ${filters[k] ? "on" : "off"} ${k === "error" && n > 0 ? "err" : ""}" data-kind="${k}">${KIND_MARK[k]} ${k} ${n}</button>`;
  }).join("");

  return `
    <div class="feed-head">
      <span class="kick">EVENT FEED</span>
      ${chips}
      <span class="spacer"></span>
      <span class="note">${esc(feedNote(ev))}</span>
      <button class="pill" id="evjump" style="display:none"></button>
    </div>`;
}

/**
 * `capped` and `windowed` are DIFFERENT facts and are never collapsed:
 * windowed means "more exist, ask for them"; capped means events were DROPPED
 * and are gone. Only the second is data loss.
 */
function feedNote(ev) {
  if (!ev) return "oldest first · cap 400 · sticky bottom";
  const bits = [];
  if (ev.capped) bits.push(`ring full — oldest dropped (${ev.total} seen)`);
  else if (ev.returned < ev.retained) bits.push(`showing ${ev.returned} of ${ev.retained}`);
  // A high unmapped count is CORRECT, not a defect: message.part.delta is ~99%
  // of traffic (one frame per token) and is deliberately dropped.
  if (ev.unmapped) bits.push(`${ev.unmapped} unmapped`);
  bits.push("oldest first · cap 400");
  return bits.join(" · ");
}

// The feed is painted OUT OF BAND, after the board's innerHTML swap, because it
// is append-only and stateful. See paintFeed() below.

let renderedSeq = -1;
let renderedSig = null;
let unread = 0;

function sigOf(ev) {
  return JSON.stringify([EVENT_KINDS.filter((k) => filters[k]), ev?.connected ?? null, ev?.reason ?? null]);
}

export function paintFeed(board) {
  const box = document.getElementById("sc-events");
  if (!box) return;
  const ev = board.events ?? null;
  const sig = sigOf(ev);

  if (!ev) {
    box.innerHTML = padNote("control plane not enabled — the event feed is opt-in and currently off.");
    renderedSeq = -1; renderedSig = sig;
    return;
  }
  if (!ev.connected) {
    // Counts on screen are frozen at the last event and may be stale — say so.
    box.innerHTML = padNote(`event feed disconnected — ${ev.reason ?? "no reason given"}. Counts above are frozen at the last event and may be stale.`, true);
    renderedSeq = -1; renderedSig = sig;
    return;
  }

  const rows = (ev.events ?? []).filter((e) => filters[e.kind] !== false);
  if (!rows.length) {
    box.innerHTML = padNote(
      ev.retained
        ? "every retained event is hidden by the active filters."
        : "connected, no events yet — nothing has happened in the session.",
    );
    renderedSeq = -1; renderedSig = sig;
    return;
  }

  // Measure BEFORE touching the DOM — scrollHeight changes on append.
  const atBottom = isAtBottom(box);
  const wrapped = rows[0].seq > renderedSeq + 1 && renderedSeq !== -1;
  const stale = sig !== renderedSig || wrapped || box.querySelector(".null");

  if (stale) {
    box.innerHTML = rows.map((e) => evRow(e, false)).join("");
    renderedSeq = rows[rows.length - 1].seq ?? -1;
    renderedSig = sig;
    box.scrollTop = box.scrollHeight;
    return;
  }

  const fresh = rows.filter((e) => (e.seq ?? -1) > renderedSeq);
  if (!fresh.length) return;

  const prevTop = box.scrollTop;
  box.insertAdjacentHTML("beforeend", fresh.map((e) => evRow(e, true)).join(""));
  renderedSeq = fresh[fresh.length - 1].seq ?? renderedSeq;

  // Trim from the TOP to the cap, compensating scroll by the exact height
  // removed — otherwise the list jumps every time the cap is hit.
  const over = box.children.length - EVENT_RENDER_CAP;
  let trimmed = 0;
  if (over > 0) {
    for (let i = 0; i < over; i += 1) {
      const first = box.firstElementChild;
      if (!first) break;
      trimmed += first.getBoundingClientRect().height;
      first.remove();
    }
  }

  if (atBottom) box.scrollTop = box.scrollHeight;
  else box.scrollTop = prevTop - trimmed;

  markUnread(fresh.length, atBottom);
}

function isAtBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight <= BOTTOM_EPS;
}

function markUnread(n, atBottom) {
  unread = atBottom ? 0 : unread + n;
  const pill = document.getElementById("evjump");
  if (!pill) return;
  if (unread > 0) {
    pill.textContent = `${unread} new ↓`;
    pill.style.display = "";
  } else {
    pill.style.display = "none";
  }
}

export function jumpToLive() {
  const box = document.getElementById("sc-events");
  if (!box) return;
  unread = 0;
  box.scrollTop = box.scrollHeight;
  const pill = document.getElementById("evjump");
  if (pill) pill.style.display = "none";
}

function evRow(e, isNew) {
  const t = e.at ? new Date(e.at).toLocaleTimeString("en-GB", { hour12: false }) : "";
  return `
    <div class="evrow ${esc(e.kind)}${isNew ? " fresh" : ""}" data-seq="${esc(String(e.seq ?? ""))}">
      <span class="evt">${esc(t)}</span>
      <span class="evmark">${KIND_MARK[e.kind] ?? "·"}</span>
      <span class="evname">${esc(e.name ?? "")}</span>
      <span class="evdetail ${e.kind === "file" ? "evpath" : ""}">${esc(e.detail ?? "")}</span>
    </div>`;
}

function padNote(text, bad = false) {
  return `<div class="null pad ${bad ? "danger" : ""}">${esc(text)}</div>`;
}
