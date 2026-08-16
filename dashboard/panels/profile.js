// ─────────────────────────────────────────────────────────────────────────────
// THE PROFILE VOCABULARY — the words every surface about a profile must reuse
//
// ── WHAT THIS FILE IS, AFTER THE BASELINES REWRITE ──────────────────────────
//
// ONE THING: the shared wording. The debt badge, the debt block, the transfer
// edge and the UNFILTERED RECALL mark, exported so that the creation flow
// (panels/create.js) and the frozen-policy drawer (panels/ledger.js) render the
// SAME sentences rather than each writing their own.
//
// That is not tidiness. Two phrasings of "declared, not enforced" are TWO
// CLAIMS, and an operator reading one on the modal and the other on the card has
// no way to know they mean the same thing — so they will reasonably assume they
// do not, and go looking for the difference.
//
// ── WHAT LEFT, AND WHERE IT WENT ────────────────────────────────────────────
//
// The CREATION MODAL used to live here: one tall form asking both frozen facts
// at once, opened by a [+ profile] button on a model row. It is now frames
// PROFILE·1–3 of panels/create.js, which asks them in the order they matter and
// asks the baseline question FIRST — because a profile is measured against one
// specific floor, and the old form let that be decided implicitly by whichever
// row the operator happened to click.
//
// Before that, two more surfaces left: a board-level MEMORY PROFILE card and the
// full-screen inspector it opened. Both read `board.profile` — a single global
// "the profile" from the era of one profile per machine — so with four profiles
// on disk they showed one and gave no way to tell which. Reading a frozen
// profile happens in exactly one place now: inside the row it belongs to.
//
// ── §5.4-CANON HARD: DECLARED — NOT YET ENFORCED ────────────────────────────
// This is the load-bearing part of this file.
//
// Model provenance EXISTS: pending_submissions.producer_model_id
// (wevibe-server/db/schema.sql:184) → Qdrant payload (retrieval.go:421) → read
// back (retrieval.go:679). But grep for `producer_model` across the plugin and
// the MCP returns NOTHING: no recall request carries an allowlist. The filter
// is not implemented in any consumer.
//
// So a profile is RECORDED AGAINST THE STACK and NOT APPLIED TO RETRIEVAL. Every
// ON run recalls the WHOLE corpus regardless of what is ticked. Persisting the
// allowlist to disk did NOT change this — storing a policy is not enforcing one,
// which is precisely why the badge stays.
//
// A board that implied model-filtered recall while recall is unfiltered would
// make every ON result unattributable — the operator would attribute a result to
// a curated subset that was never curated. That is the exact failure this
// benchmark exists to detect, so the badge is doubled (the WORDS plus a double
// rule, legible in greyscale) and appears EVERYWHERE the profile appears.
//
// The badge disappears the day the backend filter lands. It is the visible debt,
// not decoration.
// ─────────────────────────────────────────────────────────────────────────────

import { esc } from "../board.js";

export const DEBT_BADGE = "⚠ DECLARED — NOT YET ENFORCED";

/** The doubled badge. Words + double rule, so greyscale still carries it. */
export function debt() {
  return `<div class="debt">${esc(DEBT_BADGE)}</div>`;
}

/** The per-row badge that rides EVERY ON run in the card. */
export function unfilteredBadge() {
  return `<span class="debt sm">UNFILTERED RECALL</span>`;
}

/**
 * THE DEBT, IN FULL SENTENCES. The creation flow's roster frame and the frozen
 * drawer both draw this one.
 */
export function debtBlock() {
  return `
    <div class="pd-debt">
      <span class="debt-kick">${esc(DEBT_BADGE)}</span>
      <span class="debt-body">Recall cannot filter by producing model today. Every ON run under this profile retrieves from the whole corpus, including every model in the roster that is unchecked. Freezing the allowlist to disk records the policy; it does not apply it.</span>
    </div>`;
}

/**
 * THE TRANSFER EDGE — subject ← memory roster.
 *
 * INFERRED, NEVER DECLARED. There is no direction control anywhere on any
 * surface and there must never be one: "greater → weaker" is a claim about two
 * models' relative capability, and this board does not rank models. Whether the
 * edge crosses a capability gradient is read off the identities the operator
 * chose; whether that gradient runs UP or DOWN is reported as unranked, because
 * ranking would need a measured floor for each model on this task and the bench
 * refuses to assert one it has not observed.
 *
 * Same-model is not a special case bolted on. It is `self` — the degenerate
 * edge, and the base measurement being run today.
 *
 * EXPORTED, AND THE ONLY DEFINITION. The card's drawer draws the FROZEN edge
 * with this function; the creation flow draws the PRE-FREEZE preview with it. A
 * second copy could word "direction UNRANKED" differently and turn one refusal
 * to rank models into two claims that look unrelated.
 */
export function transferBlock(t) {
  if (!t) {
    return `
      <div class="xfer unset">
        <span class="xfer-kind">TRANSFER — UNSET</span>
        <span class="note">${esc("choose a subject model and at least one memory producer; the edge is inferred from those two and is never declared")}</span>
      </div>`;
  }

  const word =
    t.kind === "self"
      ? "SAME MODEL → SAME MODEL"
      : t.kind === "mixed"
        ? "MIXED — OWN + FOREIGN MEMORIES"
        : "CROSS-MODEL";

  const dirWord = t.direction === "same" ? "no gradient crossed" : "direction UNRANKED";

  // The `self` case is the only one that can be stated completely, so it is the
  // only one drawn as settled. Everything else carries the unranked mark — not
  // as a warning, but because an unlabelled cross-model edge would read as
  // though the board knew which way it ran.
  return `
    <div class="xfer ${esc(t.kind)}">
      <span class="xfer-kind">TRANSFER — ${esc(word)}</span>
      <span class="xfer-dir ${t.direction === "same" ? "settled" : "unranked"}">${esc(dirWord)}</span>
      <span class="note">${esc(
        t.kind === "self"
          ? "the subject recalls only memories it authored itself — the base measurement. Producer and consumer are one identity, so up/down does not apply rather than being unknown."
          : `memories from ${t.foreign.length} model${t.foreign.length === 1 ? "" : "s"} other than the subject: ${t.foreign.join(", ")}${t.self ? ", plus the subject's own" : ""}. Whether this is a transfer up or down is not claimed — ranking two models requires a measured floor for each on this task, and none is asserted by declaration.`,
      )}</span>
    </div>`;
}
