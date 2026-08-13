// ─────────────────────────────────────────────────────────────────────────────
// DOM PATCH — replace the wholesale innerHTML swap with an in-place morph
//
// ── THE DEFECT THIS EXISTS TO FIX ───────────────────────────────────────────
//
// `render()` ran `root.innerHTML = ...` every 2 seconds. That destroys and
// rebuilds EVERY node on the board, which costs three things the operator was
// losing constantly:
//
//   1. SCROLL POSITION. Not the window's — the browser restores that — but
//      every scrollable pane INSIDE the board. A destroyed element has no
//      scrollTop to restore, so any list the operator had scrolled snapped
//      back to the top twice a second. That is the "keeps jumping me around"
//      complaint, and it is why the page could not be navigated while a run
//      was live.
//   2. FOCUS AND CARET. A half-typed org id in the run form was wiped mid-word
//      because the input element it lived in no longer existed.
//   3. SELECTION. Text the operator was highlighting to copy — a cid, an error
//      string — was deselected before it could be copied.
//
// ── WHY MORPH AND NOT "RENDER LESS OFTEN" ───────────────────────────────────
//
// Slowing the poll trades one defect for another: the board's whole job is to
// show a live run. The problem was never the frequency, it was that a re-render
// was indistinguishable from a rebuild. Morphing makes an unchanged panel a
// no-op, so a 2s cadence costs nothing and the data stays live.
//
// ── THE CONTRACT ────────────────────────────────────────────────────────────
//
// Panels still emit HTML STRINGS. Nothing about how a panel is written changes
// — this module is purely the application step. That matters: the panels are
// the reviewed surface, and a rewrite into imperative DOM calls would have
// re-opened every one of them.
//
// Reconciliation is BY POSITION, with tag+key identity. The board's structure
// is static (the same panels in the same order every frame), so positional
// matching is correct and cheap. Where a list's rows genuinely reorder, the
// row carries `data-k` and is matched by key instead — see keyedChildren().
//
// `data-preserve` marks a subtree this module MUST NOT touch. The event feed
// owns its own children (append-only, scroll-compensated, seq-watermarked in
// live.js paintFeed) and a morph would fight it. The TUI screen is likewise
// painted from a run-length frame. Both are opted out by attribute rather than
// by a hardcoded id list here, so a future pane can opt out without editing
// this file.
// ─────────────────────────────────────────────────────────────────────────────

/** Attribute marking a subtree whose children are managed elsewhere. */
export const PRESERVE_ATTR = "data-preserve";

/** Attribute carrying a stable row identity for keyed reconciliation. */
export const KEY_ATTR = "data-k";

/**
 * Patch `container`'s children to match `html`.
 *
 * The parse happens in a detached <template>, so nothing partially-built is
 * ever attached to the live document — no flash of half-rendered board, and no
 * layout work on intermediate states.
 */
export function patch(container, html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  patchChildren(container, tpl.content);
}

/**
 * Reconcile one level of children, then recurse.
 *
 * Keyed and positional matching are chosen per-parent, not globally: a parent
 * whose children all carry `data-k` is reordered by key, everything else is
 * matched by index. Mixing the two within one parent is treated as positional,
 * because a partially-keyed list has no coherent identity to match on.
 */
function patchChildren(oldParent, newParent) {
  const newNodes = [...newParent.childNodes];
  const oldNodes = [...oldParent.childNodes];

  if (isKeyed(newNodes) && isKeyed(oldNodes)) {
    patchKeyed(oldParent, oldNodes, newNodes);
    return;
  }

  const n = Math.max(oldNodes.length, newNodes.length);
  for (let i = 0; i < n; i += 1) {
    const oldNode = oldNodes[i];
    const newNode = newNodes[i];

    if (!newNode) {
      // Surplus old node. Remove it.
      oldNode?.remove();
      continue;
    }
    if (!oldNode) {
      oldParent.appendChild(newNode.cloneNode(true));
      continue;
    }
    patchNode(oldParent, oldNode, newNode);
  }
}

/** Every element child carries a key, and there is at least one. */
function isKeyed(nodes) {
  const els = nodes.filter((n) => n.nodeType === Node.ELEMENT_NODE);
  return els.length > 0 && els.length === nodes.length && els.every((e) => e.hasAttribute(KEY_ATTR));
}

/**
 * Keyed reconciliation. Existing rows are MOVED rather than rebuilt, so a row
 * that merely changed position keeps its scroll, focus and selection.
 */
function patchKeyed(parent, oldNodes, newNodes) {
  const byKey = new Map();
  for (const o of oldNodes) byKey.set(o.getAttribute(KEY_ATTR), o);

  const seen = new Set();
  let cursor = null;

  for (const n of newNodes) {
    const key = n.getAttribute(KEY_ATTR);
    seen.add(key);
    let target = byKey.get(key);

    if (target) {
      patchElement(target, n);
    } else {
      target = n.cloneNode(true);
    }

    // Insert after the cursor, which walks the reconciled prefix. This is a
    // no-op DOM call when the node is already in place.
    const next = cursor ? cursor.nextSibling : parent.firstChild;
    if (next !== target) parent.insertBefore(target, next);
    cursor = target;
  }

  for (const o of oldNodes) {
    if (!seen.has(o.getAttribute(KEY_ATTR))) o.remove();
  }
}

/** Patch a single node in place, or replace it if it cannot be reconciled. */
function patchNode(parent, oldNode, newNode) {
  // Different node kind, or a different tag: not the same thing. Replace.
  if (oldNode.nodeType !== newNode.nodeType || oldNode.nodeName !== newNode.nodeName) {
    parent.replaceChild(newNode.cloneNode(true), oldNode);
    return;
  }

  if (oldNode.nodeType === Node.TEXT_NODE || oldNode.nodeType === Node.COMMENT_NODE) {
    // Assigning an identical string still invalidates layout in some engines,
    // and would collapse a live text selection. Compare first.
    if (oldNode.nodeValue !== newNode.nodeValue) oldNode.nodeValue = newNode.nodeValue;
    return;
  }

  if (oldNode.nodeType === Node.ELEMENT_NODE) patchElement(oldNode, newNode);
}

function patchElement(oldEl, newEl) {
  patchAttrs(oldEl, newEl);

  // The subtree is owned by another painter. Attributes are still synced above
  // (so a class change on the container lands) but the children are its own.
  if (oldEl.hasAttribute(PRESERVE_ATTR)) return;

  patchFormState(oldEl, newEl);
  patchChildren(oldEl, newEl);
}

function patchAttrs(oldEl, newEl) {
  for (const { name, value } of [...newEl.attributes]) {
    if (oldEl.getAttribute(name) !== value) oldEl.setAttribute(name, value);
  }
  for (const { name } of [...oldEl.attributes]) {
    if (!newEl.hasAttribute(name)) oldEl.removeAttribute(name);
  }
}

/**
 * Form state lives in PROPERTIES, not attributes — setting `value=` on an
 * element the user has typed into does nothing to what they see. So it is
 * synced explicitly, and ONLY when that element is not the one being typed in.
 *
 * THE FOCUSED ELEMENT IS NEVER OVERWRITTEN. Rewriting the value of a focused
 * input moves the caret to the end mid-word, which is exactly the defect that
 * made the org field unusable while the board polled. The server is not the
 * authority on a field the operator is still editing.
 */
function patchFormState(oldEl, newEl) {
  const tag = oldEl.nodeName;
  if (tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") return;
  if (document.activeElement === oldEl) return;

  if (tag === "SELECT") {
    const want = newEl.querySelector("option[selected]")?.getAttribute("value") ?? newEl.value;
    if (want !== null && oldEl.value !== want) oldEl.value = want;
    return;
  }
  if (tag === "INPUT" && (oldEl.type === "checkbox" || oldEl.type === "radio")) {
    const want = newEl.hasAttribute("checked");
    if (oldEl.checked !== want) oldEl.checked = want;
    return;
  }
  const want = newEl.getAttribute("value");
  if (want !== null && oldEl.value !== want) oldEl.value = want;
}
