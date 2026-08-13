// ─────────────────────────────────────────────────────────────────────────────
// OVERLAY — dialogs that live OUTSIDE #root
//
// `render()` replaces the whole of #root.innerHTML every 2s. That is correct
// for a read-only board and fatal for an interactive dialog: a wholesale swap
// destroys select focus, a half-typed org id, and a scroll position. So modals
// mount here, as a sibling of #root, and survive the poll.
//
// ONE AT A TIME, AND THE ORDER IS DELIBERATE. The creation modal outranks the
// inspector: you cannot inspect a profile you are still defining, and stacking
// two scrims would leave the operator unsure which dialog a click belongs to.
// ─────────────────────────────────────────────────────────────────────────────

import {
  renderProfileModal,
  isProfileModalOpen,
  closeProfileModal,
  renderInspector,
  isInspectorOpen,
  closeInspector,
} from "./panels/profile.js";
import { renderRunControl, renderBaselineModal, isBaselineModalOpen, closeBaselineModal, runLifecycleState } from "./panels/runstart.js";
import { patch } from "./dom.js";

export function renderOverlay(board) {
  let root = document.getElementById("overlay-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "overlay-root";
    document.body.appendChild(root);
  }

  // THE BASELINE CONFIRM OUTRANKS EVERYTHING. It is the only dialog raised by a
  // direct operator action that starts a multi-hour cell, so it must never be
  // hidden behind a panel that happens to be open.
  if (isBaselineModalOpen()) {
    patch(root, renderBaselineModal());
    return;
  }

  // PATCHED, NOT REPLACED. Mounting outside #root saved the dialog from the
  // board's swap, but this function then did the same thing to it: `innerHTML`
  // every 2s rebuilt the dialog, so a scrolled model list snapped back to the
  // top and a half-typed org id lost its caret. Surviving one wholesale swap
  // only to be destroyed by another is not survival. See dom.js.
  if (isProfileModalOpen()) {
    patch(root, renderProfileModal(board));
    return;
  }

  if (isInspectorOpen()) {
    // The run control is COMPOSED IN rather than imported by the inspector.
    // The inspector describes a frozen policy; the run control changes the
    // world. Keeping the write surface out of the read panel's imports means
    // the panel cannot grow a way to start a run by accident.
    patch(root, renderInspector(board, renderRunControl(board)));
    return;
  }

  // ── THE RUN CONTROL'S OWN HOME ────────────────────────────────────────────
  //
  // THIS IS THE FIX FOR THE DEAD END. Until now `renderRunControl` had exactly
  // ONE call site — the branch above — so the CONFIRM button, the server's
  // restatement and every refusal were reachable only through the profile
  // inspector, which only opens when a profile exists.
  //
  // The consequence on the [+ baseline] path: CONTINUE armed the run, the
  // server minted a token and returned its restatement, and the surface that
  // renders them was never on screen. The run sat ARMED forever, the operator
  // saw nothing, and the benchmark never started. A refused preview was worse —
  // the reason went to `ui.refusal`, which nothing painted.
  //
  // So the run control gets a home that does NOT depend on a profile existing.
  // It is raised only once there is something to say — armed, in flight, or
  // refused — because an unconditional dialog would sit over the board
  // permanently and make it unreadable.
  //
  // THE ARM→CONFIRM PROTOCOL IS UNCHANGED. This renders the SAME control, whose
  // token is still server-minted, whose restatement is still the server's own
  // words, and which still disarms on any parameter change. Nothing here starts
  // a run; it only makes the second click reachable.
  const lc = runLifecycleState();
  if (lc.armed || lc.pending || lc.refusal || lc.starting) {
    patch(root, `
      <div class="modal-scrim" data-run-scrim="1">
        <div class="modal rmodal" role="dialog" aria-modal="true">
          ${renderRunControl(board)}
          <div class="rmodal-foot">
            <button class="btn" data-run-dismiss="1">DISMISS</button>
            <span class="note">${"Dismissing disarms the run — nothing is started, and no cell in flight is affected."}</span>
          </div>
        </div>
      </div>`);
    return;
  }

  patch(root, "");
}

export function closeOverlays() {
  closeProfileModal();
  closeInspector();
  closeBaselineModal();
}
