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
import { renderRunControl } from "./panels/runstart.js";
import { patch } from "./dom.js";

export function renderOverlay(board) {
  let root = document.getElementById("overlay-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "overlay-root";
    document.body.appendChild(root);
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

  patch(root, "");
}

export function closeOverlays() {
  closeProfileModal();
  closeInspector();
}
