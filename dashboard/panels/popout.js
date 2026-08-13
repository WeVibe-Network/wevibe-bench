// ─────────────────────────────────────────────────────────────────────────────
// POPOUT — the reusable dock/window shell
//
// ── WHY THIS EXISTS RATHER THAN A TOASTER ───────────────────────────────────
//
// The design (§9.4) called for extraction toasts, and the operator killed them
// outright: NO TOASTERS. The reasoning holds up and is worth keeping written
// down, because "just add a toast" is the thing someone will propose again:
//
//   · A toast is a TIMED surface. Extraction runs for minutes; a notification
//     that auto-dismisses at 6s cannot report a process that outlives it, so it
//     degrades into an alert that something happened somewhere.
//   · A toast STACKS OVER the board. The board's entire argument is a fixed
//     layout the eye learns; something that covers a different panel each time
//     it appears is the opposite of that.
//   · Extraction is not an event, it is a QUEUE with state. It needs a surface
//     you can open, read, scroll and leave open.
//
// The TUI popout already solved exactly this shape — a long-running thing that
// must be watchable without stealing the board — and it was built and verified
// against a live run. So this generalises THAT, rather than deriving a second
// pattern that would drift from it.
//
// ── WHAT IS GENERALISED (all four are load-bearing, from panels/tui.js) ─────
//
//   1. MINIMIZED IS THE DEFAULT, and the 34px bar ALWAYS STATES WHAT IT IS
//      DOING. The operator must never have to expand a popout to find out
//      whether the thing inside it is alive. A dock bar that says only its own
//      name is a decoration.
//   2. THE WINDOW IS SIZED FROM ITS CONTENT, never squeezed into leftover
//      space. The operator's complaint, verbatim: "the TUI was added to the
//      space, the space was not made for the TUI."
//   3. READ-ONLY FRAMING. A popout mirrors state; it is not a control panel
//      that happens to float.
//   4. A DESTRUCTIVE ACTION IS CONFIRMED AND SAYS WHAT IS LOST — in words, not
//      as "are you sure?" alone.
//
// ── WHAT IS DELIBERATELY *NOT* GENERALISED ──────────────────────────────────
//
// tui.js KEEPS its own rendering. It is not refactored onto this shell.
//
// That is a deliberate call, not laziness. The TUI popout carries a hard 40×130
// character grid whose pixel geometry is DERIVED from the grid constants
// (GRID_W/GRID_H at tui.js:50-51), a run-length colour frame rebuilt span by
// span, and a `data-preserve` subtree that the DOM morpher must not touch.
// Forcing that through a generic shell would mean either the shell grows
// terminal-specific knobs — at which point it is not general — or the grid gets
// squeezed into the shell's idea of a box, which is the ORIGINAL DEFECT the TUI
// panel exists to fix.
//
// Two popouts sharing a vocabulary and a stylesheet is the win. Two popouts
// sharing an implementation, where one of them has a pixel-exact terminal in
// it, is a regression waiting for the next person who edits the shell.
// The shared CSS (`.pop-*`) is where the consistency actually lives.
// ─────────────────────────────────────────────────────────────────────────────

import { esc } from "../board.js";

/**
 * Registry of popout open/closed state, keyed by popout id.
 *
 * Module-level and NOT derived from the board payload: it describes what the
 * OPERATOR has open, which no poll can know and no server may overwrite. The
 * board re-renders every 2s and this must survive that untouched.
 */
const open = new Map();

export function isPopoutOpen(id) {
  return open.get(id) === true;
}

export function togglePopout(id) {
  open.set(id, !isPopoutOpen(id));
}

/**
 * Render a popout.
 *
 * @param {object} cfg
 * @param {string} cfg.id          stable id; drives the data-pop-* hooks
 * @param {string} cfg.brand       short name on the dock bar, e.g. "EXTRACTION"
 * @param {string} cfg.status      WHAT IT IS DOING — required, see rule 1
 * @param {string} cfg.title       window header title
 * @param {string} cfg.note        header sub-note (counts, scope)
 * @param {string} cfg.tag         framing tag, e.g. "READ-ONLY — REPORTS ONLY"
 * @param {number} cfg.width       content width in px — the window is sized
 *                                 FROM this, never from leftover space
 * @param {string} cfg.body        the expanded content (HTML string)
 * @param {string} cfg.foot        one line stating what the surface is
 * @param {Array}  cfg.tabs        [{id,label,active,count}] optional view switch
 * @param {number} cfg.dockIndex   stacking slot, so two docks never overlap
 */
export function renderPopout(cfg) {
  const id = cfg.id;
  const expanded = isPopoutOpen(id);
  // Docks stack upward from the bottom-right. Computed rather than hardcoded so
  // adding a third popout cannot silently bury the second one.
  const bottom = 14 + (cfg.dockIndex ?? 0) * 42;

  return `
    <div class="pop-dock ${expanded ? "open" : "min"}" style="bottom:${bottom}px" data-pop="${esc(id)}">
      ${expanded ? popWindow(cfg) : popBar(cfg)}
    </div>`;
}

// ── MINIMIZED — THE DEFAULT ─────────────────────────────────────────────────
// 34px. It always says what the thing is doing. `status` is not optional: a
// dock bar showing only a name forces the operator to expand it to learn
// whether anything is happening, which is the defect this shape exists to
// avoid.

function popBar(cfg) {
  return `
    <div class="pop-bar">
      <span class="pop-brand">▌${esc(cfg.brand)}</span>
      <span class="pop-stat">${cfg.status ?? ""}</span>
      <span class="spacer"></span>
      <button class="btn sm" data-pop-toggle="${esc(cfg.id)}">EXPAND ⌃</button>
    </div>`;
}

// ── EXPANDED — SIZED FROM ITS CONTENT ───────────────────────────────────────

function popWindow(cfg) {
  const width = cfg.width ?? 1180;
  return `
    <div class="pop-win" style="width:${width}px">
      <div class="pop-head">
        <span class="pop-brand">▌${esc(cfg.title ?? cfg.brand)}</span>
        ${cfg.note ? `<span class="note">${esc(cfg.note)}</span>` : ""}
        ${cfg.tag ? `<span class="tag">${esc(cfg.tag)}</span>` : ""}
        <span class="spacer"></span>
        <button class="btn sm" data-pop-toggle="${esc(cfg.id)}">MINIMIZE ⌄</button>
      </div>
      ${cfg.tabs?.length ? tabs(cfg) : ""}
      <div class="pop-body">${cfg.body ?? ""}</div>
      ${cfg.foot ? `<div class="pop-foot">${esc(cfg.foot)}</div>` : ""}
    </div>`;
}

function tabs(cfg) {
  return `
    <div class="pop-tabs">
      ${cfg.tabs
        .map(
          (t) =>
            `<button class="pop-tab ${t.active ? "on" : ""}" data-pop-view="${esc(cfg.id)}:${esc(t.id)}">${esc(t.label)}${
              // A count of 0 is rendered — it is a measured result. Only an
              // UNOBSERVED count is omitted, and the two are different facts.
              t.count === null || t.count === undefined ? "" : ` <span class="pop-n">${esc(String(t.count))}</span>`
            }</button>`,
        )
        .join("")}
    </div>`;
}
