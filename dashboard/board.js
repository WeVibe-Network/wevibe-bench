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
import { renderLedger, toggleBaselineRow, toggleProfileRow } from "./panels/ledger.js";
import { renderLive, paintFeed, toggleKind, jumpToLive } from "./panels/live.js";
import { renderHold } from "./panels/hold.js";
import { renderRail } from "./panels/rail.js";
import { renderRecall } from "./panels/recall.js";
import {
  openCreate,
  closeCreate,
  isCreateOpen,
  createStep,
  createSelection,
  chooseBranch,
  createForward,
  createBack,
  setCreateKind,
  setCreateModel,
  setCreateQuery,
  setCreateProvider,
  setCreateBaseline,
  toggleCreateMemory,
  setCreatePending,
  setCreateRefusal,
} from "./panels/create.js";
import {
  setRunSel,
  presetRun,
  armRun,
  startRun,
  disarm as disarmRun,
  runLifecycleState,
  clearRefusal,
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
  //   ledger      — every model, every profile, every cell; floor pinned, both
  //                 axes restated. The frozen policy opens INSIDE it — the
  //                 MEMORY PROFILE card that used to sit below `live` is gone,
  //                 along with the inspector it opened: both drew the single
  //                 global `board.profile`, so with several profiles on disk
  //                 they showed one and could not say which.
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
      ${renderRecall(board)}
      ${renderRail(board)}
      ${renderProvenance(board)}
      ${renderTui(board, runLifecycleState())}
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
//
// BOUND BY THE BOOT GUARD, NOT AT MODULE SCOPE. These three listeners used to
// run on import, which broke this module's OWN documented library/entry-point
// split (see the guard at the foot of the file): board.js exports esc/nul/clip/
// tok/dur, so every panel imports it — and any test that imports a panel
// executed `document.addEventListener` under Node and threw
// `ReferenceError: document is not defined` before a single assertion ran.
// The condition was latent only because no test had yet imported a panel
// module. Binding here keeps the behaviour identical in a browser and makes the
// module importable everywhere else, which is what the guard already claimed.
function bindInteraction() {
  document.addEventListener("click", onClick);
  // Run-form inputs. `change` covers the selects; `input` covers typing the org.
  document.addEventListener("change", onRunSel);
  document.addEventListener("input", onRunSel);
  document.addEventListener("keydown", onKeydown);
}

function onClick(e) {
  const t = e.target.closest("[data-metric],[data-kind],#evjump,[data-tui-toggle],[data-tui-detach],[data-tui-detach-yes],[data-tui-cancel],[data-hold-release],[data-create-open],[data-create-cancel],[data-create-scrim],[data-create-branch],[data-create-next],[data-create-back],[data-create-kind],[data-create-model],[data-create-baseline],[data-create-memory],[data-create-baseline-continue],[data-create-accept],[data-run-arm],[data-run-confirm],[data-baseline-expand],[data-profile-expand],[data-run-dismiss],[data-run-scrim],[data-run-profile],[data-pop-toggle],[data-pop-view]");
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

  // ── THE [+ PROFILE] FLOW ──────────────────────────────────────────────────
  //
  // One entry, then a chooser and two three-step sequences (panels/create.js).
  // Every step below is pure selection held in that module — only the two
  // handlers at the end of each branch reach the network.
  if (t.hasAttribute("data-create-open")) { openCreate(); render(); return; }
  // The scrim closes the flow, but ONLY when the scrim ITSELF was clicked — a
  // click that lands inside the dialog also bubbles through it.
  if (
    t.hasAttribute("data-create-cancel")
    || (t.hasAttribute("data-create-scrim") && e.target === t)
  ) { closeCreate(); render(); return; }
  if (t.dataset.createBranch) { chooseBranch(t.dataset.createBranch); render(); return; }
  if (t.hasAttribute("data-create-next")) { createForward(); render(); return; }
  if (t.hasAttribute("data-create-back")) { createBack(); render(); return; }
  if (t.dataset.createKind) { setCreateKind(t.dataset.createKind); render(); return; }
  if (t.dataset.createModel) { setCreateModel(t.dataset.createModel); render(); return; }
  if (t.dataset.createBaseline) { setCreateBaseline(t.dataset.createBaseline); render(); return; }
  if (t.dataset.createMemory) { toggleCreateMemory(t.dataset.createMemory); render(); return; }
  if (t.hasAttribute("data-create-baseline-continue")) {
    // THE END OF THE BASELINE BRANCH — and it still does not launch. This arms
    // the cell through the server's preview, which mints a token and returns
    // its own restatement; the run control then takes the final click. The
    // operator therefore confirms against THE SERVER'S words, not this flow's
    // summary of them.
    const sel = createSelection();
    presetRun({ model: sel.model, arm: "off", kind: sel.kind });
    closeCreate();
    void doArmRun();
    return;
  }
  if (t.hasAttribute("data-create-accept")) { void freezeProfile(); return; }

  if (t.hasAttribute("data-run-arm")) { void doArmRun(); return; }
  if (t.hasAttribute("data-run-confirm")) { void doStartRun(); return; }
  // DISMISSING THE RUN CONTROL DISARMS IT. Leaving a token armed behind a
  // closed surface is exactly the invisible state this work exists to remove —
  // the operator would have a live token they can no longer see or confirm.
  // The scrim only dismisses when the scrim ITSELF was clicked; a click that
  // bubbled out of the form must never throw away a half-filled run.
  if (t.hasAttribute("data-run-dismiss") || (t.hasAttribute("data-run-scrim") && e.target === t)) {
    disarmRun();
    clearRefusal();
    render();
    return;
  }
  if (t.dataset.runProfile) {
    // A run under a profile is the ON arm. The org is NOT guessed — the server
    // requires one and the operator picks it in the run panel.
    //
    // THE SUBSTRATE COMES FROM THE BASELINE THIS PROFILE SITS UNDER, because a
    // profile's subject is that baseline's model and an ON cell must run on the
    // same substrate the floor was measured on. Reading it off the row rather
    // than defaulting to local is what makes [+ run] work on a cloud profile.
    presetRun({
      model: t.dataset.runModel,
      arm: "on",
      kind: t.dataset.runKind,
    });
    render();
    return;
  }
  // ── THE BASELINES CARD ────────────────────────────────────────────────────
  // Expansion is checked AFTER the buttons that live inside the rows — [+ run]
  // must start a run, never open a drawer. Returning here is what keeps a
  // button click from also toggling the row it sits in.
  if (t.dataset.baselineExpand) { toggleBaselineRow(t.dataset.baselineExpand); render(); return; }
  if (t.dataset.profileExpand) { toggleProfileRow(t.dataset.profileExpand); render(); return; }
  // POPOUTS. The view switch is checked BEFORE the toggle: a tab click must
  // change the view, never collapse the window out from under the operator.
  if (t.dataset.popView) {
    const [, v] = t.dataset.popView.split(":");
    setExtractView(v);
    render();
    return;
  }
  if (t.dataset.popToggle) { togglePopout(t.dataset.popToggle); render(); return; }
}

function onRunSel(e) {
  const s = e.target.closest("[data-run-sel]");
  if (s) {
    setRunSel(s.dataset.runSel, s.value);
    render();
    return;
  }
  // THE CREATE FLOW'S TWO FILTERS. They are re-rendered on every keystroke, so
  // the caret would be lost if the input were rebuilt — patch() morphs the
  // existing node in place, which is what makes typing here survive the redraw.
  if (e.target.closest("[data-create-query]")) { setCreateQuery(e.target.value); render(); return; }
  if (e.target.closest("[data-create-provider]")) { setCreateProvider(e.target.value); render(); }
}

function onKeydown(e) {
  if (e.key !== "Escape") return;
  // The create flow is checked FIRST — it is rendered on top, so Escape must
  // dismiss what the operator is actually looking at.
  //
  // ESCAPE CLOSES THE WHOLE FLOW, IT DOES NOT STEP BACK. Back is a control on
  // the frame with a word on it; escape is the universal "I am done with this
  // dialog". Overloading escape as back would make a three-step sequence take
  // three escapes to leave, which is not what any operator means by it.
  if (isCreateOpen()) { closeCreate(); render(); return; }
  // Escape disarms the run control for the same reason DISMISS does: an armed
  // token behind a dismissed surface is an invisible state.
  const lc = runLifecycleState();
  if (lc.armed || lc.refusal) { disarmRun(); clearRefusal(); render(); return; }
  if (isDetachConfirming()) { cancelDetach(); render(); }
}

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
 * a session, or attach a TUI. The modal and the ledger's drawer both say so in
 * words,
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
    setCreateRefusal(reach.code, reach.fix ? `${reach.reason}\n\n${reach.fix}` : reach.reason);
    console.error(`profile creation unavailable — ${reach.code}: ${reach.reason}`);
    render();
    return;
  }

  // THE SUBJECT IS THE CHOSEN BASELINE'S MODEL — read off the baseline, never
  // picked separately.
  //
  // It used to be its own question in the old modal, which meant an operator
  // could freeze a profile whose subject had no floor, or whose floor belonged
  // to a different model. Under this flow that is unspellable: the profile is
  // measured against a baseline the operator selected, and the subject IS that
  // baseline's model by construction.
  const sel = createSelection();
  const row = (board?.models_ledger?.baseline_rows ?? []).find((b) => b.id === sel.baseline) ?? null;
  if (!row) {
    setCreateRefusal(
      "baseline_missing",
      `baseline ${sel.baseline ?? "(none)"} is no longer in the index — it may have been archived while this dialog was open. Go back and choose again.`,
    );
    render();
    return;
  }

  const base = board.control.base_url;
  setCreatePending(true);
  render();
  try {
    const res = await fetch(`${base}/api/profiles/create`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        // TWO axes, sent separately. The subject is the measurement; the roster
        // is the variable. The server refuses either being absent, with a
        // distinct code for each.
        subject_model: row.model,
        memory_models: sel.memory,
        stack_id: board?.stack?.id ?? null,
      }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || data?.ok === false) {
      // Never swallow: the reason reaches the operator ON SCREEN, not just in
      // the console. The server's prose is passed through verbatim.
      setCreateRefusal(
        data?.code ?? `http_${res.status}`,
        data?.reason ?? `the control plane answered HTTP ${res.status} without a stated reason`,
      );
      console.error(`profile creation refused: ${data?.code ?? res.status} — ${data?.reason ?? ""}`);
      render();
      return;
    }
    setCreatePending(false);
    closeCreate();
    // The board is PUSHED, so the freeze lands on screen when the server's next
    // assembly observes the new file on disk — within one tick. Nothing is
    // written into `board` here: a local write would be a second source of
    // truth for a value that is now durable, and the two could disagree.
  } catch (err) {
    // The request never reached the server. That is a DIFFERENT diagnosis from
    // a refusal and is labelled as such, so "the control plane is down" is
    // never mistaken for "the control plane said no".
    setCreateRefusal("transport_failed", String(err?.message ?? err));
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
  bindInteraction();
  render();
  connect();
}
