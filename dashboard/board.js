// ─────────────────────────────────────────────────────────────────────────────
// WEVIBE BENCH BOARD v2 — RENDERER
//
// Dependency-free vanilla JS. React from a CDN would make the board a blank
// page the moment the network hiccups — on a live stream, with the whole stack
// running locally, that is an unacceptable failure mode for zero benefit.
//
// THE QUESTION THE BOARD ANSWERS: does a growing memory corpus make the SAME
// local model finish the SAME build in fewer turns, fewer tokens and less time
// — and at what corpus size does that stop being true?
//
// RENDER RULES:
//  - null is a designed state. Three kinds of nothing stay visually distinct:
//      unobserved  — not measured yet
//      unwired     — the source that would carry it is not connected
//      zero        — measured, and the answer is 0 (a real result)
//  - CORRECTNESS and EFFICIENCY are two axes, never blended. Nothing anywhere
//    combines them into one number.
//  - A stopped cell must never imply motion.
//  - No hover-dependent information.
// ─────────────────────────────────────────────────────────────────────────────

// The board is PUSHED over SSE (see connect()). There is no poll interval.

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
// "MEMORY ON" / "CONTROL" — so the board reads correctly in greyscale and for
// a viewer with colour vision deficiency.

export const ARM = {
  on: { cls: "a", label: "MEMORY ON", short: "A", color: "var(--fg)" },
  off: { cls: "b", label: "CONTROL", short: "B", color: "var(--dim)" },
};

export function armOf(arm) {
  return ARM[arm] ?? { cls: "null", label: "UNKNOWN ARM", short: "—", color: "var(--dim)" };
}

// ── state ────────────────────────────────────────────────────────────────────

let board = null;
let lastError = null;
let consecutiveErrors = 0;

// ── THE LIVE STREAM ──────────────────────────────────────────────────────────
//
// The board was refetching /api/board every 2 seconds — 240KB per poll, of
// which 82% was 400 event rows that had not changed. It is now PUSHED: the
// server assembles once for all clients and sends a board frame only when the
// board actually changed, plus event rows newer than this client's cursor.
//
// SSE rather than a raw WebSocket, deliberately — see the rationale block in
// server.mjs. The property that matters here: EventSource reconnects by itself
// with backoff, so a control-plane restart mid-run does not leave a dark board
// and there is no hand-rolled retry loop to get wrong.
//
// THE LAST GOOD BOARD ALWAYS STAYS UP. A dropped stream marks the feed stale in
// the top bar; it never blanks the screen. Staleness is information.

/** High-water mark of events this client has rendered. Resumes a reconnect. */
let eventCursor = 0;
/** The accumulated event window, mirrored from the pushed deltas. */
let eventRows = [];
const EVENT_WINDOW_CAP = 400;

let stream = null;

/**
 * Reconnect the stream.
 *
 * Used when the client's SUBSCRIPTION changes — today that is only the TUI
 * popout opening or closing, which changes whether the server should spend
 * 12.4KB per tick sending terminal frames. The cursor is preserved, so no
 * event is replayed or skipped across the reconnect.
 */
export function resubscribe() {
  if (stream) {
    stream.close();
    stream = null;
  }
  connect();
}

function connect() {
  // A reconnect resumes FROM THE CURSOR, so the server replays only what was
  // missed rather than the whole ring. `tui=1` opts into full terminal frames;
  // without it the server sends the mirror's STATUS only, which is all the
  // minimized dock bar renders.
  const wantsTui = isTuiExpanded() ? "&tui=1" : "";
  stream = new EventSource(`/api/stream?since=${eventCursor}${wantsTui}`);

  stream.addEventListener("board", (msg) => {
    try {
      const next = JSON.parse(msg.data);
      // Event rows live on the client and are merged in below — the server
      // deliberately strips them from the board frame so an unchanged ring is
      // never re-sent. Re-attach the local window before rendering.
      board = next;
      board.events = board.events ? { ...board.events, events: eventRows } : null;
      lastError = null;
      consecutiveErrors = 0;
      render();
    } catch (err) {
      console.error("board frame failed to parse:", err);
    }
  });

  // ── PATCH: ONLY THE SECTIONS THAT CHANGED ──────────────────────────────
  // The server digests each top-level key independently and sends only the
  // ones that moved. A ticking `run.elapsed_s` therefore costs ~486 bytes
  // instead of re-sending the 12.4KB TUI screen sitting beside it.
  //
  // MERGE IS BY KEY AND WHOLESALE WITHIN A KEY. Each section is published by
  // exactly one source and is internally consistent, so a deep merge would
  // risk splicing two assemblies together — a half-old `stack` is a lie in a
  // way a whole-old one is not. `null` means the section is GONE and is
  // assigned as null, never skipped: a panel must not keep rendering state the
  // server no longer has.
  stream.addEventListener("patch", (msg) => {
    if (!board) return; // no baseline to patch onto; the board frame comes first
    try {
      const patch = JSON.parse(msg.data);
      for (const [k, v] of Object.entries(patch)) {
        // DOTTED KEYS ARE SPLIT SECTIONS. `control` is 6.5KB of mostly-static
        // capabilities and roster wrapped around a clock that ticks every 2s,
        // so the server digests its children separately and sends only the one
        // that moved (server.mjs granularSignatures). `parent.__rest` carries
        // whatever was not split out, so no field can vanish from the wire.
        if (k.includes(".")) {
          const [parent, child] = k.split(".");
          if (!board[parent] || typeof board[parent] !== "object") board[parent] = {};
          if (child === "__rest") {
            board[parent] = { ...board[parent], ...(v ?? {}) };
          } else {
            board[parent][child] = v;
          }
          continue;
        }
        if (k === "events") {
          // The ring's METADATA may change (counts, connected, grading) while
          // the ROWS are owned by the client's own accumulated window.
          board.events = v ? { ...v, events: eventRows } : null;
          continue;
        }
        if (k === "tui_rows") {
          // ROW SPLICE. The server sends only the terminal rows that changed,
          // addressed by index, because re-sending the whole 36KB screen at
          // 250ms cost 2.8MB per 20s — an unacceptable price for low latency.
          //
          // A splice with no frame to splice into is DISCARDED, not applied to
          // an empty grid: a partial screen rendered as if it were whole is a
          // lie about what the terminal shows. The server sends a full frame
          // whenever it has no diff base, so the next tick recovers.
          if (!board.tui?.frame) continue;
          const frame = board.tui.frame.slice();
          for (const [i, row] of v.rows ?? []) frame[i] = row;
          board.tui = { ...board.tui, ...(v.meta ?? {}), frame };
          continue;
        }
        if (k === "tui") {
          // A WITHHELD FRAME MUST NOT ERASE THE ONE ON SCREEN. The server drops
          // the 12.4KB terminal frame for a client that has not subscribed, and
          // assigning that null over a good frame would blank a live mirror.
          board.tui = v?.frame_withheld && board.tui?.frame ? { ...v, frame: board.tui.frame } : v;
          continue;
        }
        board[k] = v;
      }
      lastError = null;
      consecutiveErrors = 0;
      render();
    } catch (err) {
      console.error("patch frame failed to parse:", err);
    }
  });

  stream.addEventListener("events", (msg) => {
    try {
      const { events: fresh = [], cursor } = JSON.parse(msg.data);
      if (!fresh.length) return;
      eventRows = [...eventRows, ...fresh];
      if (eventRows.length > EVENT_WINDOW_CAP) {
        eventRows = eventRows.slice(eventRows.length - EVENT_WINDOW_CAP);
      }
      eventCursor = fresh[fresh.length - 1].seq ?? eventCursor;
      if (typeof cursor === "number" && cursor > eventCursor) eventCursor = cursor;
      if (board) {
        board.events = board.events ? { ...board.events, events: eventRows } : null;
        // The feed paints itself out of band (append-only, scroll-compensated),
        // so a pure event frame does not need a whole-board render.
        try { paintFeed(board); } catch (err) { console.error("feed paint failed:", err); }
      }
    } catch (err) {
      console.error("events frame failed to parse:", err);
    }
  });

  stream.addEventListener("error", (msg) => {
    // Two different things arrive here: a server-sent `error` frame carrying a
    // reason, and EventSource's own transport error which carries none. They
    // are diagnosed differently and must not be conflated.
    if (msg?.data) {
      try {
        lastError = JSON.parse(msg.data).reason ?? "server reported an error";
      } catch {
        lastError = "server reported an error";
      }
    } else {
      lastError = "stream disconnected — reconnecting";
    }
    consecutiveErrors += 1;
    // EventSource reconnects on its own. The board keeps its last good state
    // and the top bar says the feed is stale.
    render();
  });

  stream.addEventListener("open", () => {
    consecutiveErrors = 0;
    lastError = null;
  });
}

// ── render ───────────────────────────────────────────────────────────────────

import { renderTopbar, renderProvenance } from "./panels/chrome.js";
import { renderCurve, setCurveMetric } from "./panels/curve.js";
import { renderWall } from "./panels/wall.js";
import { renderLedger, toggleModelRow } from "./panels/ledger.js";
import { renderLive, paintFeed, toggleKind, jumpToLive } from "./panels/live.js";
import { renderHold } from "./panels/hold.js";
import { renderRail } from "./panels/rail.js";
import { renderRecall } from "./panels/recall.js";
import {
  renderProfile,
  openProfileModal,
  closeProfileModal,
  toggleModel,
  setSubject,
  toggleAck,
  profileSelection,
  profileSubject,
  openInspector,
  closeInspector,
  isInspectorOpen,
  setRefusal,
  setPending,
} from "./panels/profile.js";
import {
  setRunSel,
  presetRun,
  armRun,
  startRun,
  disarm as disarmRun,
} from "./panels/runstart.js";
import {
  renderTui, toggleTui, askDetach, cancelDetach,
  isDetachConfirming, isTuiExpanded,
} from "./panels/tui.js";
import { renderExtraction, setExtractView } from "./panels/extraction.js";
import { togglePopout } from "./panels/popout.js";
import { renderOverlay } from "./overlay.js";
import { patch } from "./dom.js";

function render() {
  const root = document.getElementById("root");

  if (!board) {
    patch(root, `
      <div class="topbar"><span class="brand">HOW GOOD IS <u>YOUR</u> MEMORY SYSTEM</span>
        <span class="spacer"></span>
        <span class="chip dimchip">connecting to feed…</span></div>
      <div style="display:flex;align-items:center;justify-content:center;padding:80px 0">
        <div style="text-align:center">
          <div class="kick" style="margin-bottom:10px">no board yet</div>
          <div class="null">${esc(lastError ?? "waiting for /api/board")}</div>
        </div>
      </div>`);
    return;
  }

  // PANEL ORDER IS THE ARGUMENT THE BOARD MAKES:
  //   hold first  — a blocked run outranks everything, in the operator's face
  //   curve|wall  — the two axes, adjacent and equal, 50/50
  //   ledger      — every cell, floor pinned, both axes restated
  //   live        — the running cell's pulse
  //   recall      — proof retrieval fires, demoted
  //   rail        — the honesty that buys credibility for all of the above
  //   provenance  — what a skeptic checks first
  //
  // PATCHED, NOT REPLACED. This was `root.innerHTML = ...`, which rebuilt every
  // node on the board twice a second and destroyed scroll position, focus, the
  // caret and any live text selection along with them — the board could not be
  // read or navigated while a run was in flight. `patch()` morphs the existing
  // tree in place, so an unchanged panel is untouched and only real changes
  // reach the DOM. See dom.js.
  patch(root, `
    <div class="shell">
      ${renderTopbar(board, { stale: consecutiveErrors > 0, lastError })}
      ${renderHold(board)}
      <div class="axes-row">
        ${renderCurve(board)}
        ${renderWall(board)}
      </div>
      ${renderLedger(board)}
      ${renderLive(board)}
      ${renderProfile(board)}
      ${renderRecall(board)}
      ${renderRail(board)}
      ${renderProvenance(board)}
      ${renderTui(board)}
      ${renderExtraction(board)}
    </div>
  `);

  // AFTER the swap: the feed is append-only and stateful, so it paints into the
  // fresh container rather than being rebuilt by the string above.
  // OVERLAY (modals) lives outside #root and survives the swap, so an open
  // dialog is not torn down by the poll.
  //
  // EACH IS WRAPPED: the board is the thing that must never go dark. A throw
  // in any one of these costs that surface, never the board — and the failure
  // is printed, never swallowed.
  try { paintFeed(board); } catch (err) { console.error("feed paint failed:", err); }
  try { renderOverlay(board); } catch (err) { console.error("overlay failed:", err); }
}

// ── interaction ──────────────────────────────────────────────────────────────
// ONE DELEGATED LISTENER, bound to `document` and never to a rendered node.
// All interactive elements carry data-* hooks instead of bound handlers.
//
// This is still required after the move to morphing. patch() reuses nodes where
// it can, but it REPLACES any node whose tag changed and removes any node that
// left the tree — a handler bound directly to one of those would be silently
// lost. Delegation is invariant to how the tree is updated.

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-metric],[data-kind],#evjump,[data-tui-toggle],[data-tui-detach],[data-tui-detach-yes],[data-tui-cancel],[data-hold-release],[data-profile-open],[data-profile-cancel],[data-profile-ack],[data-profile-create],[data-model],[data-subject],[data-profile-inspect],[data-inspect-close],[data-inspect-scrim],[data-run-arm],[data-run-confirm],[data-model-expand],[data-run-baseline],[data-new-profile],[data-run-profile],[data-pop-toggle],[data-pop-view]");
  if (!t) return;

  if (t.dataset.metric) { setCurveMetric(t.dataset.metric); render(); return; }
  if (t.dataset.kind) { toggleKind(t.dataset.kind); render(); return; }
  if (t.id === "evjump") { jumpToLive(); return; }
  // TOGGLING THE TUI CHANGES THE SUBSCRIPTION, not just the view. The terminal
  // frame is 12.4KB per tick and is withheld by the server unless this client
  // asked for it, so opening the popout must re-open the stream with `tui=1`.
  // The cursor survives the reconnect, so no event is replayed or skipped.
  if (t.dataset.tuiToggle !== undefined && t.hasAttribute("data-tui-toggle")) { toggleTui(); resubscribe(); render(); return; }
  if (t.hasAttribute("data-tui-detach")) { askDetach(); render(); return; }
  if (t.hasAttribute("data-tui-detach-yes")) { void detachTui(); return; }
  if (t.hasAttribute("data-tui-cancel")) { cancelDetach(); render(); return; }
  if (t.hasAttribute("data-hold-release")) { void releaseHold(); return; }
  if (t.hasAttribute("data-profile-open")) { openProfileModal(); render(); return; }
  if (t.hasAttribute("data-profile-cancel")) { closeProfileModal(); render(); return; }
  if (t.hasAttribute("data-profile-ack")) { toggleAck(); render(); return; }
  // Subject is checked BEFORE model: a row in the subject picker carries only
  // `data-subject`, but keeping the order explicit stops a future row that
  // carries both from silently toggling the wrong axis.
  if (t.dataset.subject) { setSubject(t.dataset.subject); render(); return; }
  if (t.dataset.model) { toggleModel(t.dataset.model); render(); return; }
  if (t.hasAttribute("data-profile-create")) { void freezeProfile(); return; }
  if (t.hasAttribute("data-profile-inspect")) { openInspector(); render(); return; }
  // The scrim closes the inspector, but ONLY when the scrim itself was clicked
  // — a click that bubbled up from inside the dialog must not dismiss it.
  if (t.hasAttribute("data-inspect-scrim") && e.target === t) { closeInspector(); render(); return; }
  if (t.hasAttribute("data-inspect-close")) { closeInspector(); render(); return; }
  if (t.hasAttribute("data-run-arm")) { void doArmRun(); return; }
  if (t.hasAttribute("data-run-confirm")) { void doStartRun(); return; }
  // ── THE MODEL LEDGER ──────────────────────────────────────────────────────
  // Expansion is checked FIRST, but the three buttons live inside the row and
  // would otherwise match the row's own closest() hit. They each stopPropagation
  // by returning here — the button attributes are tested before the row's.
  if (t.dataset.runBaseline) {
    // A baseline is BY DEFINITION the OFF arm. Prefills and arms; the operator
    // still confirms, because this starts a multi-hour cell.
    presetRun({ model: t.dataset.runBaseline, arm: "off" });
    render();
    return;
  }
  if (t.dataset.newProfile) { openProfileModal([], t.dataset.newProfile); render(); return; }
  if (t.dataset.runProfile) {
    // A run under a profile is the ON arm. The org is NOT guessed — the server
    // requires one and the operator picks it in the run panel.
    presetRun({ model: t.dataset.runProfile, arm: "on" });
    render();
    return;
  }
  if (t.dataset.modelExpand) { toggleModelRow(t.dataset.modelExpand); render(); return; }
  // POPOUTS. The view switch is checked BEFORE the toggle: a tab click must
  // change the view, never collapse the window out from under the operator.
  if (t.dataset.popView) {
    const [, v] = t.dataset.popView.split(":");
    setExtractView(v);
    render();
    return;
  }
  if (t.dataset.popToggle) { togglePopout(t.dataset.popToggle); render(); return; }
});

// Run-form inputs. `change` covers the selects; `input` covers typing the org.
document.addEventListener("change", onRunSel);
document.addEventListener("input", onRunSel);
function onRunSel(e) {
  const s = e.target.closest("[data-run-sel]");
  if (!s) return;
  setRunSel(s.dataset.runSel, s.value);
  render();
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (isInspectorOpen()) { closeInspector(); render(); return; }
  if (isDetachConfirming()) { cancelDetach(); render(); }
});

/** The board never posts. The browser posts DIRECTLY to the control plane. */

/**
 * CAN THIS BROWSER REACH THE CONTROL PLANE AT ALL?
 *
 * `base_url` is a loopback address, and loopback resolves to whichever machine
 * dereferences it. Browsing the board from the host, that is the host and every
 * control POST works. Browsing it from another device on the LAN — the
 * documented remote-viewing case — `127.0.0.1:7718` is THAT DEVICE, so the
 * request dies before it leaves the laptop and surfaces as a transport error
 * with no obvious cause.
 *
 * The control plane deliberately cannot be published on the LAN to fix this: it
 * binds 127.0.0.1 with no --host flag as a stated safety property
 * (control/server.mjs:23-25), because it spawns processes. The read-only board
 * may be exposed; the thing that can change the world may not.
 *
 * So the honest answer is to say so. This returns the reason a write is
 * impossible, or null when it is possible — one derivation, consumed by every
 * control path, so no button can disagree with another about whether it works.
 */
export function controlReachability(b) {
  const base = b?.control?.base_url ?? null;
  if (!base) {
    return {
      ok: false,
      code: "control_plane_unwired",
      reason: "the board does not know where the control plane is — it is not running, or the dashboard was started without it.",
      fix: null,
    };
  }
  // Only the browser knows its own origin, which is why the comparison happens
  // here and not in the source module that published the URL.
  const remote =
    b?.control?.base_url_is_loopback === true &&
    !["localhost", "127.0.0.1", "[::1]", "::1"].includes(location.hostname);
  if (remote) {
    return {
      ok: false,
      code: "control_plane_not_reachable_from_here",
      reason:
        `this board is open at ${location.hostname}, but the control plane is published as ${base}. ` +
        "That address means THIS device, not the bench host, so the request would never leave your machine. " +
        "The control plane binds loopback only, on purpose — it starts runs and spawns processes, so it is never exposed on a network.",
      fix:
        `ssh -L 7717:127.0.0.1:7717 -L 7718:127.0.0.1:7718 <user>@${location.hostname}\n` +
        "then open http://127.0.0.1:7717 in this browser. Both ports tunnel to the bench host's loopback, so the controls work and nothing is exposed on the network.",
    };
  }
  return { ok: true, code: null, reason: null, fix: null };
}

/** The board never posts. The browser posts DIRECTLY to the control plane. */
async function releaseHold() {
  const reach = controlReachability(board);
  if (!reach.ok) {
    console.error(`hold release unavailable — ${reach.code}: ${reach.reason}`);
    return;
  }
  const base = board.control.base_url;
  try {
    const res = await fetch(`${base}/api/hold/release`, { method: "POST" });
    if (!res.ok) console.error(`hold release refused: HTTP ${res.status}`);
    // The next poll observes the file vanish, which IS the success signal.
  } catch (err) {
    console.error("hold release failed:", err);
  }
}

async function detachTui() {
  const reach = controlReachability(board);
  cancelDetach();
  if (!reach.ok) {
    console.error(`tui detach unavailable — ${reach.code}: ${reach.reason}`);
    render();
    return;
  }
  const base = board.control.base_url;
  try {
    const res = await fetch(`${base}/api/tui/detach`, { method: "POST" });
    if (!res.ok) console.error(`tui detach refused: HTTP ${res.status}`);
  } catch (err) {
    console.error("tui detach failed:", err);
  }
  render();
}

/**
 * FREEZE A PROFILE.
 *
 * This POSTs to the control plane and the profile lands on disk. It previously
 * only mutated `board.profile` in page memory, which the next 2s poll
 * overwrote — the operator created a profile and nothing survived, which is the
 * defect being fixed.
 *
 * IT STARTS NOTHING. Freezing writes an allowlist; it does not arm a cell, open
 * a session, or attach a TUI. The modal and the inspector both say so in words,
 * because the previous silence is what made "nothing happened" ambiguous
 * between "it worked and did nothing visible" and "it failed".
 *
 * EVERY FAILURE PATH REACHES THE OPERATOR. Refusals were previously written to
 * `console.error` and nowhere else, so a working button in front of a refusing
 * server looked exactly like a dead button. A stale control plane serving an
 * older wire contract refused every valid payload and the screen never changed.
 * The reason is now rendered in the modal; the console line is kept for the
 * devtools trail, but it is no longer the ONLY place the truth appears.
 */
async function freezeProfile() {
  const reach = controlReachability(board);
  if (!reach.ok) {
    // The refusal the operator sees carries the FIX when one exists. A reason
    // without a remedy leaves them exactly as stuck as silence did.
    setRefusal(reach.code, reach.fix ? `${reach.reason}\n\n${reach.fix}` : reach.reason);
    console.error(`profile creation unavailable — ${reach.code}: ${reach.reason}`);
    render();
    return;
  }
  const base = board.control.base_url;
  setPending(true);
  render();
  try {
    const res = await fetch(`${base}/api/profiles/create`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        // TWO axes, sent separately. The subject is the measurement; the roster
        // is the variable. The server refuses either being absent, with a
        // distinct code for each.
        subject_model: profileSubject(),
        memory_models: profileSelection(),
        stack_id: board?.stack?.id ?? null,
      }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || data?.ok === false) {
      // Never swallow: the reason reaches the operator ON SCREEN, not just in
      // the console. The server's prose is passed through verbatim.
      setRefusal(
        data?.code ?? `http_${res.status}`,
        data?.reason ?? `the control plane answered HTTP ${res.status} without a stated reason`,
      );
      console.error(`profile creation refused: ${data?.code ?? res.status} — ${data?.reason ?? ""}`);
      render();
      return;
    }
    setPending(false);
    closeProfileModal();
    // The board is PUSHED, so the freeze lands on screen when the server's next
    // assembly observes the new file on disk — within one tick. Nothing is
    // written into `board` here: a local write would be a second source of
    // truth for a value that is now durable, and the two could disagree.
  } catch (err) {
    // The request never reached the server. That is a DIFFERENT diagnosis from
    // a refusal and is labelled as such, so "the control plane is down" is
    // never mistaken for "the control plane said no".
    setRefusal("transport_failed", String(err?.message ?? err));
    console.error("profile creation failed:", err);
    render();
  }
}

// Run start is the loudest control on the board, so a silent `return` here was
// the worst instance of the original defect: the operator arms a run, nothing
// happens, and no reason is given anywhere. The reason now reaches the console
// AND the topbar banner explains the LAN case before the click.
async function doArmRun() {
  const reach = controlReachability(board);
  if (!reach.ok) {
    console.error(`run arm unavailable — ${reach.code}: ${reach.reason}`);
    render();
    return;
  }
  render();
  await armRun(board.control.base_url);
  render();
}

async function doStartRun() {
  const reach = controlReachability(board);
  if (!reach.ok) {
    console.error(`run start unavailable — ${reach.code}: ${reach.reason}`);
    render();
    return;
  }
  await startRun(board.control.base_url);
  // ATTACH THE TUI. The operator asked for the terminal to come up with the
  // run. The mirror is on-demand and the control plane's poll IS its keepalive,
  // so expanding it is what starts the capture. It remains a second, strictly
  // read-only attach client that never writes to the pty.
  if (!isTuiExpanded()) { toggleTui(); resubscribe(); }
  render();
}

// FIRST PAINT, THEN THE STREAM. render() draws the "connecting to feed…" state
// immediately so the board is never a blank page while the socket opens.
//
// GUARDED BECAUSE THIS MODULE IS BOTH THE ENTRY POINT AND A LIBRARY. board.js
// exports esc/nul/clip/tok/dur, so every panel imports it — and every panel
// TEST therefore executes this file's module scope under Node, where there is
// no DOM and no EventSource. Booting unconditionally made importing a pure
// string builder throw `ReferenceError: EventSource is not defined`.
//
// The guard is a capability check, not an environment sniff: it asks whether
// the two things the boot actually needs are present. In a browser both are;
// under `node --test` neither is, and the module is then exactly what the tests
// treat it as — a library.
if (typeof document !== "undefined" && typeof EventSource !== "undefined") {
  render();
  connect();
}
