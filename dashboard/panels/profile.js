// ─────────────────────────────────────────────────────────────────────────────
// PANEL: MEMORY PROFILE — the inspector, and the ONLY place a run starts
//
// ── WHAT CHANGED AND WHY (WO-BOARD-PROFILE-1) ───────────────────────────────
//
// Two operator-reported defects are fixed here.
//
// 1. "There is no means to click on the profile to understand how it is
//    configured after it is configured." True: the frozen profile rendered as a
//    flat card with no affordance, and the only interactive control on the
//    surface was the CREATE button, which vanished once a profile existed. The
//    profile is now openable — the ledger's PROFILE chip is a button — and the
//    inspector carries the configuration, the baseline, the historical overlay,
//    the run history, and run start.
//
// 2. "I created a memory profile and nothing happened. I thought it would start
//    a new session." Investigated: NO auto-start was ever coded. The old
//    create handler mutated a browser-local object and returned; it posted
//    nothing and spawned nothing, and the object was overwritten by the next
//    2s poll because no source produced it. So there was no auto-start feature
//    to remove — there was a missing start feature, and a silent surface.
//
//    Both halves are answered: creation now persists through the control plane,
//    and the inspector states CREATION SIDE EFFECTS — NONE in words, so the
//    surface never again leaves the operator guessing what a click did.
//
// ── IMMUTABLE, AND THE CONTROL IS ABSENT RATHER THAN DISABLED ───────────────
// One profile per ON stack, frozen at creation. Changing the allowlist means
// starting a NEW stack. There is no pencil, no "manage", and no greyed-out
// button implying a later unlock — a disabled control is a promise that it will
// one day be enabled. Frozen means the affordance does not exist.
//
// Why it must be frozen: every run in the stack is measured under one policy.
// A profile edited at run 4 would silently make runs 1–3 incomparable to 4–N,
// and the curve would be a line through two different experiments.
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
// So this profile is RECORDED AGAINST THE STACK and NOT APPLIED TO RETRIEVAL.
// Every ON run in the stack recalls the WHOLE corpus regardless of what is
// ticked here. Persisting the allowlist to disk did NOT change this — storing a
// policy is not enforcing one, which is precisely why the badge stays.
//
// A board that implied model-filtered recall while recall is unfiltered would
// make every ON result unattributable — the operator would attribute a result
// to a curated subset that was never curated. That is the exact failure this
// benchmark exists to detect, so the badge is doubled (the WORDS plus a double
// rule, legible in greyscale) and appears EVERYWHERE the profile appears.
//
// The badge disappears the day the backend filter lands. It is the visible
// debt, not decoration.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, dur } from "../board.js";

export const DEBT_BADGE = "⚠ DECLARED — NOT YET ENFORCED";

/**
 * Client-side selection state for the creation modal.
 *
 * TWO SEPARATE FIELDS, because they are two separate facts (see the TWO AXES
 * header). `subject` is single-valued: the OFF→ON pair is one model, and a
 * multi-select here would let an operator declare a measurement that cannot
 * exist. `selection` is the memory roster and is many-valued: that is the
 * experiment variable.
 */
let subject = "";
let selection = new Set();
let ack = false;
let open = false;

/**
 * THE LAST REFUSAL, SHOWN TO THE OPERATOR.
 *
 * The control plane refuses with a `code` and a written `reason` for every
 * rejected freeze. Those were previously sent to `console.error` and NOWHERE
 * else, which made a working button in front of a refusing server visually
 * IDENTICAL to a dead button — the operator clicked FREEZE PROFILE, the server
 * answered 409 with a precise explanation, and the screen did not change.
 *
 * That is the defect this state exists to close. A refusal the operator cannot
 * see is a swallowed error no matter where it was logged.
 *
 * `pending` is separate from `error`: an in-flight POST must disable the button
 * so a second click cannot race the first, and the two states are never
 * conflated — "working" and "refused" are different facts.
 */
let refusal = null;
let pending = false;

/** Record a refusal. `code` is the machine reason, `reason` the prose one. */
export function setRefusal(code, reason) {
  refusal = { code: code ?? null, reason: reason ?? null, at: Date.now() };
  pending = false;
}
export function clearRefusal() {
  refusal = null;
}
export function setPending(v) {
  pending = Boolean(v);
  if (pending) refusal = null;
}
export function isPending() {
  return pending;
}
export function currentRefusal() {
  return refusal;
}

/** Inspector visibility. Separate from the creation modal — different jobs. */
let inspector = false;

export function openProfileModal(preselect = []) {
  subject = "";
  selection = new Set(preselect);
  ack = false;
  open = true;
  refusal = null;
  pending = false;
}
export function closeProfileModal() {
  open = false;
  ack = false;
  refusal = null;
  pending = false;
}
export function isProfileModalOpen() {
  return open;
}

/**
 * Choosing the subject does NOT auto-tick it in the memory roster.
 *
 * Auto-ticking would make same-model the silent default and hide the choice
 * being made — and a profile that qualifies only FOREIGN memories (subject
 * unticked) is a legitimate experiment: the subject recalls nothing it wrote.
 * The operator ticks the subject's own memories deliberately or not at all.
 */
export function setSubject(id) {
  subject = subject === id ? "" : id;
  // The refusal described the PREVIOUS choices. Changing an input makes it
  // stale, and a stale refusal next to new choices reads as a fresh rejection.
  refusal = null;
}
export function toggleModel(id) {
  if (selection.has(id)) selection.delete(id);
  else selection.add(id);
  refusal = null;
}
export function toggleAck() {
  ack = !ack;
  refusal = null;
}
export function profileSubject() {
  return subject;
}
export function profileSelection() {
  return [...selection];
}

/**
 * MIRROR of `transferOf()` in control/profiles.mjs. The two services share no
 * import (separate processes, separate trust levels — the same reason
 * roster.mjs mirrors the Python context registry rather than importing it).
 *
 * Blast radius is deliberately tiny: this drives the PRE-FREEZE preview only.
 * Once frozen, `board.profile.transfer` comes from the service and the
 * inspector renders that, so any drift between the two shows up immediately as
 * the preview disagreeing with the inspector for the same pair of choices.
 */
function inferTransfer(subjectId, roster) {
  if (!subjectId || !roster.length) return null;
  const foreign = roster.filter((m) => m !== subjectId);
  const self = roster.includes(subjectId);
  if (!foreign.length) return { kind: "self", direction: "same", self: true, foreign: [] };
  return { kind: self ? "mixed" : "cross", direction: "unranked", self, foreign };
}

export function openInspector() {
  inspector = true;
}
export function closeInspector() {
  inspector = false;
}
export function isInspectorOpen() {
  return inspector;
}

// ── THE FROZEN PROFILE, AS SHOWN ON THE BOARD ───────────────────────────────
//
// The board-level card is a SUMMARY that opens the inspector. It keeps the debt
// badge, because the badge must ride every surface the profile appears on — an
// operator who only ever sees the summary must still see the debt.

export function renderProfile(board) {
  const p = board.profile ?? {};
  if (!p.exists) return "";

  return `
    <section class="panel profile-card">
      <div class="phead">
        <span class="kick">MEMORY PROFILE — READ-ONLY FOREVER</span>
        <span class="sub">${p.created_at ? esc(`frozen ${new Date(p.created_at).toLocaleString("en-GB")}`) : nul("creation time unobserved")}</span>
        <span class="spacer"></span>
        <button class="btn sm" data-profile-inspect="1">OPEN INSPECTOR →</button>
      </div>
      <div class="profile-subject">
        <span class="skick">SUBJECT — OFF→ON</span>
        <span class="sval">${p.subject_model ? esc(p.subject_model) : nul("no subject frozen")}</span>
      </div>
      <div class="profile-models">
        <span class="skick">MEMORY ROSTER</span>
        ${(p.memory_models ?? []).length
          ? p.memory_models.map((m) => `<span>✓ ${esc(typeof m === "string" ? m : m.id)}</span>`).join("")
          : `<span class="null">${esc("no producer models in profile")}</span>`}
      </div>
      ${transferBlock(p.transfer, { compact: true })}
      ${debt()}
      <div class="note">No edit affordance exists on this surface. There is no pencil, no “manage”, no disabled button implying a later unlock. Frozen means the control is absent, not greyed.</div>
    </section>`;
}

/** The doubled badge. Words + double rule, so greyscale still carries it. */
export function debt() {
  return `<div class="debt">${esc(DEBT_BADGE)}</div>`;
}

/**
 * THE TRANSFER EDGE — subject ← memory roster.
 *
 * INFERRED, NEVER DECLARED. There is no direction control anywhere on this
 * surface and there must never be one: "greater → weaker" is a claim about two
 * models' relative capability, and this board does not rank models. Whether the
 * edge crosses a capability gradient is read off the identities the operator
 * chose; whether that gradient runs UP or DOWN is reported as unranked, because
 * ranking would need a measured floor for each model on this task and the bench
 * refuses to assert one it has not observed.
 *
 * Same-model is not a special case bolted on. It is `self` — the degenerate
 * edge, and the base measurement being run today.
 */
function transferBlock(t, { compact = false } = {}) {
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
      ${compact
        ? ""
        : `<span class="note">${esc(
            t.kind === "self"
              ? "the subject recalls only memories it authored itself — the base measurement. Producer and consumer are one identity, so up/down does not apply rather than being unknown."
              : `memories from ${t.foreign.length} model${t.foreign.length === 1 ? "" : "s"} other than the subject: ${t.foreign.join(", ")}${t.self ? ", plus the subject's own" : ""}. Whether this is a transfer up or down is not claimed — ranking two models requires a measured floor for each on this task, and none is asserted by declaration.`,
          )}</span>`}
    </div>`;
}

/** The per-row badge that rides EVERY ON run in the ledger. */
export function unfilteredBadge() {
  return `<span class="debt sm">UNFILTERED RECALL</span>`;
}

// ── THE INSPECTOR ───────────────────────────────────────────────────────────
//
// Design 5a. Two columns: the frozen policy on the left, the evidence produced
// under it on the right, with run start welded to the bottom of the evidence
// column — you start the next run underneath the record of every previous one.

export function renderInspector(board, runCtl) {
  if (!inspector) return "";

  const p = board.profile ?? {};
  if (!p.exists) return "";

  return `
    <div class="modal-scrim" data-inspect-scrim="1">
      <div class="modal inspector" role="dialog" aria-modal="true" aria-label="Memory profile inspector">
        <div class="insp-head">
          <span class="kick">MEMORY PROFILE — ${esc(p.id ?? "unidentified")}</span>
          <span class="sub">${inspectorSub(p, board)}</span>
          <span class="spacer"></span>
          <span class="debt sm">UNFILTERED RECALL</span>
          <button class="btn sm" data-inspect-close="1">ESC</button>
        </div>

        <div class="insp-body">
          <div class="insp-left">
            ${subjectBlock(board, p)}
            ${qualifiedModels(board, p)}
            ${transferBlock(p.transfer)}
            ${debtBlock()}
            ${baselineBlock(board)}
            <div class="insp-spacer"></div>
            ${sideEffectsBlock()}
          </div>

          <div class="insp-right">
            ${overlayBlock(board)}
            ${historyBlock(board, p)}
            <div class="insp-spacer"></div>
            ${runCtl}
          </div>
        </div>
      </div>
    </div>`;
}

function inspectorSub(p, board) {
  const bits = [];
  if (p.created_at) bits.push(`frozen ${new Date(p.created_at).toLocaleString("en-GB")}`);
  if (p.stack_id) bits.push(`stack ${p.stack_id}`);
  bits.push("read-only forever");
  return esc(bits.join(" · "));
}

/**
 * THE SUBJECT MODEL — the OFF→ON pair.
 *
 * Stated FIRST and on its own, above the roster, because it is the measurement
 * and the roster is only the variable applied to it. It also carries the one
 * enforcement fact that runs the other way from the debt badge below: unlike
 * the roster, the subject IS enforced — run start refuses a cell on any other
 * model — so the operator can rely on the OFF→ON pair never drifting even
 * though the roster is inert.
 */
function subjectBlock(board, p) {
  const s = board.stack ?? {};
  const baselineModel = s.baseline?.model ?? null;

  // A baseline belonging to a DIFFERENT model than the frozen subject makes
  // every Δ on this board a model-vs-model comparison. It should be impossible
  // now that start enforces the subject, but a stack predating this profile can
  // carry one — so it is checked and named rather than assumed away.
  const mismatch =
    p.subject_model && baselineModel && baselineModel !== p.subject_model ? baselineModel : null;

  return `
    <div class="insp-sec">
      <span class="kick">SUBJECT MODEL — THE MEASUREMENT</span>
      <div class="insp-subject">
        <span class="sval">${p.subject_model ? esc(p.subject_model) : nul("no subject frozen")}</span>
        <span class="note">${esc("OFF and ON are both this model. Run start refuses a cell on any other — the subject is enforced, unlike the roster below.")}</span>
      </div>
      ${mismatch
        ? `<div class="insp-mismatch">${esc(`BASELINE MODEL MISMATCH — the floor on this board was run on ${mismatch}, not ${p.subject_model}. A Δ against it compares two models' capabilities, not memory lift. Re-baseline on the subject before reading any delta here.`)}</div>`
        : ""}
    </div>`;
}

/**
 * MODELS QUALIFIED FOR INJECTED MEMORIES — the memory roster.
 *
 * The roster is shown in FULL, with the profile's picks checked and the rest
 * left visible and unchecked. Listing only the chosen three would hide the six
 * that are excluded, and the excluded set is exactly what the debt block below
 * is talking about — every one of them still reaches recall today.
 *
 * The subject is marked where it appears, ticked or not: "the subject's own
 * memories are/are not in scope" is the single fact that separates the base
 * measurement from a cross-model one, and it must be readable at a glance.
 */
function qualifiedModels(board, p) {
  const roster = board.control?.roster ?? null;
  const all = Array.isArray(roster?.models) ? roster.models : [];
  const picked = new Set((p.memory_models ?? []).map((m) => (typeof m === "string" ? m : m?.id)));

  // The roster can be unreachable while the profile is perfectly readable. In
  // that case the picks are still known — they are frozen in the profile — so
  // they are rendered from the profile itself rather than showing nothing.
  const subj = p.subject_model ?? null;
  const rows = all.length
    ? all.map((m) => {
        const mid = typeof m === "string" ? m : m.id;
        return modelRow(mid, picked.has(mid), m, mid === subj);
      })
    : [...picked].map((mid) => modelRow(mid, true, null, mid === subj));

  const head = all.length
    ? `MODELS QUALIFIED FOR INJECTED MEMORIES — ${picked.size} OF ${all.length}`
    : `MODELS QUALIFIED FOR INJECTED MEMORIES — ${picked.size}`;

  return `
    <div class="insp-sec">
      <span class="kick">${esc(head)}</span>
      <div class="insp-models">${rows.join("")}</div>
      ${all.length ? "" : `<span class="note">${esc("roster unreachable — showing the allowlist frozen in the profile, which is authoritative regardless")}</span>`}
    </div>`;
}

function modelRow(mid, on, m, isSubject = false) {
  // A model with zero memories is marked. Without this the operator sees a
  // qualified model contribute nothing and concludes the filter is broken —
  // the count is the whole point of the row.
  const n = m && typeof m === "object" && Number.isFinite(m.memories) ? m.memories : null;
  const note =
    n === null
      ? "memory count unobserved"
      : n === 0
        ? "0 memories — contributes nothing yet"
        : `${n.toLocaleString()} memories in corpus`;

  return `
    <div class="mrow ${on ? "on" : "off"}${isSubject ? " subj" : ""}">
      <span class="mcheck">${on ? "✓" : ""}</span>
      <span class="mid">${esc(mid)}</span>
      ${isSubject ? `<span class="subjtag">SUBJECT</span>` : ""}
      <span class="mnote ${n === 0 ? "muted" : ""}">${esc(note)}</span>
    </div>`;
}

function debtBlock() {
  return `
    <div class="insp-debt">
      <span class="debt-kick">${esc(DEBT_BADGE)}</span>
      <span class="debt-body">Recall cannot filter by producing model today. Every ON run under this profile retrieves from the whole corpus, including every model above that is unchecked. Freezing the allowlist to disk records the policy; it does not apply it.</span>
    </div>`;
}

/**
 * BASELINE — the gate on starting an ON run.
 *
 * Three distinct answers, never collapsed: a scorable floor, a floor the
 * harness itself refuses to score (void-instrument), and no floor at all. The
 * middle case is the one that matters — a void baseline looks like a baseline
 * until you ask whether it can be measured against.
 */
function baselineBlock(board) {
  const s = board.stack ?? {};
  const b = s.baseline ?? null;

  if (!b) {
    return `
      <div class="insp-sec">
        <span class="kick">BASELINE</span>
        <div class="insp-baseline bad">
          <span class="ttl danger">✕ NONE</span>
          <span class="note">${esc("no OFF cell exists in this stack")}</span>
        </div>
        <span class="note">A stack with no complete baseline cannot start an ON run — there would be no floor to measure against.</span>
      </div>`;
  }

  if (s.baseline_scorable === false) {
    return `
      <div class="insp-sec">
        <span class="kick">BASELINE</span>
        <div class="insp-baseline bad">
          <span class="ttl danger">✕ VOID-INSTRUMENT</span>
          <span class="note">${esc(b.terminal_reason ?? "the harness refuses to score this cell")}</span>
        </div>
        <span class="note">RUNBOOK 5.10: an instrument failure, not a capability result. It is not scored, so no delta against it would be valid. Re-run the baseline.</span>
      </div>`;
  }

  const g = b.gates ?? {};
  const gateTxt =
    g.failed === null || g.failed === undefined
      ? "gates not measured"
      : `${(g.total ?? 0) - g.failed}/${g.total ?? "?"} obs`;

  return `
    <div class="insp-sec">
      <span class="kick">BASELINE</span>
      <div class="insp-baseline">
        <span class="ttl">✓ COMPLETE</span>
        <span class="note">${esc(`OFF cell · ${b.turns ?? "?"} turns · ${gateTxt} · n=${s.baseline_n ?? 1}`)}</span>
      </div>
      <span class="note">n=1 BY DESIGN — a single observation, not a distribution. This stack can start an ON run.</span>
    </div>`;
}

/**
 * CREATION SIDE EFFECTS — NONE.
 *
 * This block is the direct answer to the operator's report. It is load-bearing
 * documentation on the surface itself: it states that creating a profile writes
 * an allowlist and does nothing else, so the next operator does not sit waiting
 * for a session that was never going to open.
 */
function sideEffectsBlock() {
  return `
    <div class="insp-sec insp-sfx">
      <span class="kick">CREATION SIDE EFFECTS — NONE</span>
      <span class="note">Creating a profile freezes an allowlist and writes nothing else. It does not arm a cell, open a session, or attach a TUI. Runs start from the control below, and only from there.</span>
    </div>`;
}

/**
 * THE HISTORICAL OVERLAY.
 *
 * Prior profiles are drawn as their own listed series and are NEVER joined to
 * the active one. They were measured under a DIFFERENT allowlist, so a line
 * across them would draw a trend spanning two different experiments — the same
 * reason the curve refuses a line at n=1.
 */
function overlayBlock(board) {
  const all = board.profiles ?? null;
  const prior = Array.isArray(all?.prior) ? all.prior : [];
  const active = all?.active ?? null;

  return `
    <div class="insp-sec">
      <div class="insp-secline">
        <span class="kick">TRANSFER CURVE — HISTORICAL OVERLAY</span>
        <span class="spacer"></span>
        <span class="note">${esc(prior.length ? `${prior.length} prior profile${prior.length === 1 ? "" : "s"}` : "no prior profile")}</span>
      </div>
      <div class="insp-overlay">
        <span class="ov-row"><span class="ov-dot solid"></span>${esc(`${active?.id ?? "this profile"} — ${(active?.runs ?? []).length} cell${(active?.runs ?? []).length === 1 ? "" : "s"} attributed`)}</span>
        ${prior.length
          ? prior
              .map(
                (q) =>
                  `<span class="ov-row"><span class="ov-dot hollow"></span>${esc(`${q.id} — ${(q.runs ?? []).length} cells, different allowlist, drawn hollow and never joined by a line`)}</span>`,
              )
              .join("")
          : `<span class="note">${esc("nothing to overlay — this is the first profile on this machine")}</span>`}
      </div>
    </div>`;
}

/**
 * RUNS UNDER THIS PROFILE.
 *
 * Attribution is what this service OBSERVED at launch. A cell launched from the
 * CLI is real and is deliberately absent, because it cannot be shown to have
 * run under this allowlist. That absence is STATED — an empty list with no
 * explanation reads as "no runs happened", which would be false.
 */
function historyBlock(board, p) {
  const runs = Array.isArray(p.runs) ? [...p.runs].reverse() : [];

  return `
    <div class="insp-sec">
      <div class="insp-secline">
        <span class="kick">RUNS UNDER THIS PROFILE — ${runs.length}</span>
        <span class="spacer"></span>
        <span class="note">${esc("cells this control plane launched while this profile was active")}</span>
      </div>
      <div class="insp-cols"><span>ARM</span><span>MODEL</span><span>STARTED</span><span>LOG</span></div>
      ${runs.length
        ? runs.map(historyRow).join("")
        : `<div class="insp-empty">${esc("no cell has been launched under this profile from this board. Cells started at the CLI are real but unattributed — they are not swept in here, because they cannot be shown to have run under this allowlist.")}</div>`}
    </div>`;
}

function historyRow(r) {
  const when = r.started_at ? new Date(r.started_at).toLocaleString("en-GB") : null;
  return `
    <div class="insp-row">
      <span class="arm">${esc(String(r.arm ?? "—").toUpperCase())}</span>
      <span class="model">${r.model ? esc(r.model) : nul("unobserved")}</span>
      <span>${when ? esc(when) : nul("unobserved")}</span>
      <span class="log">${r.log_name ? esc(r.log_name) : nul("unobserved")}</span>
    </div>`;
}

// ── THE CREATION MODAL ──────────────────────────────────────────────────────

/**
 * THE CREATION MODAL — two questions, asked separately.
 *
 * The first version of this modal asked ONE question ("which models' memories
 * may this stack recall?") and silently left the subject to whatever the
 * operator later picked in the run-start dropdown. That made the measurement
 * an accident of a later click, and made transfer direction unrecorded: the
 * board could not say whether a run was same-model or cross-model, because it
 * never asked which model was being measured.
 *
 * So the modal now asks both, in the order they matter:
 *
 *   1. WHAT IS BEING MEASURED — the subject. One model. Both arms.
 *   2. WHAT IS BEING VARIED   — the memory roster. Any models.
 *
 * and shows the edge those two imply, live, before the freeze. The operator
 * sees "SAME MODEL → SAME MODEL" or "CROSS-MODEL" as a CONSEQUENCE of the two
 * choices — never as a third control they set themselves.
 */
export function renderProfileModal(board) {
  if (!open) return "";

  const roster = board.control?.roster ?? null;
  const proxyOk = roster?.proxy_ok !== false;
  const models = Array.isArray(roster?.models) ? roster.models : [];

  // The subject must be a model the bench can actually run. The memory roster
  // must NOT be filtered the same way: a memory's producer is whatever model
  // authored it, which may be a model this machine no longer serves — an
  // interactive alias, or one retired from the bench roster. Restricting the
  // roster to bench-eligible models would make historical corpora unqualifiable.
  const subjectPool = models.filter((m) => m.bench_eligible !== false);
  const t = inferTransfer(subject, [...selection]);

  return `
    <div class="modal-scrim" data-profile-scrim="1">
      <div class="modal profile-modal" role="dialog" aria-modal="true" aria-label="Memory profile">
        <div class="modal-head">
          <span class="kick">NEW ON STACK · FIRST RUN ONLY</span>
          <span class="modal-title">What is being measured, and whose memories does it get?</span>
          <span class="note">${rosterNote(roster, proxyOk, models)}</span>
        </div>

        <div class="modal-body">
          <div class="pick-sec">
            <span class="kick">1 · SUBJECT MODEL — THE MEASUREMENT</span>
            <span class="note">${esc("The model whose OFF→ON pair is the experiment. OFF is the floor, ON is measured against it, and both arms are always this one model — an ON cell scored against a floor from a different model measures the two models against each other, not the memories. The sequence is unbounded: ON cells run 1..N until the curve degrades.")}</span>
            <div class="pick-rows">
              ${subjectPool.length
                ? subjectPool.map(subjectPickRow).join("")
                : `<div class="null">${esc(proxyOk ? "no bench-eligible model in the roster — an interactive slot cannot carry a defensible measurement" : "roster unavailable — the proxy is unreachable")}</div>`}
            </div>
          </div>

          <div class="pick-sec">
            <span class="kick">2 · MEMORY ROSTER — THE EXPERIMENT VARIABLE</span>
            <span class="note">${esc("Which models' memories may be injected into this stack's ON cells. Tick only the subject for the base same-model measurement; tick others to declare a cross-model transfer. The subject is not ticked for you — a stack that recalls only foreign memories is a real experiment and must be selectable.")}</span>
            <div class="pick-rows">
              ${models.length
                ? models.map((m) => pickRow(m, subject)).join("")
                : `<div class="null">${esc(proxyOk ? "the roster is empty — the proxy returned no models" : "roster unavailable — the proxy is unreachable")}</div>`}
            </div>
          </div>

          <div class="pick-sec">
            <span class="kick">3 · INFERRED — NOT A CHOICE</span>
            ${transferBlock(t)}
          </div>
        </div>

        ${debt()}
        <div class="note debt-body">
          The recall path cannot filter by producing model today. <span class="bright">producer_model_id</span> is written to the payload and read back, but no recall request carries an allowlist. This profile is <span class="bright">recorded against the stack</span> and <span class="bright">not applied to retrieval</span>. Until the backend filter lands, every ON run in this stack recalls the whole corpus.
        </div>

        <div class="note">Creating this profile freezes the subject and the memory roster and does nothing else — it does not arm a cell, open a session, or attach a TUI. Start a run from the inspector afterwards.</div>

        <div class="modal-foot">
          ${refusalBlock()}
          <label class="ackrow">
            <span class="ackbox ${ack ? "on" : ""}" data-profile-ack="1">${ack ? "✓" : ""}</span>
            <span>I understand this profile is <span class="bright">frozen at creation and can never be edited</span>. Changing <span class="bright">either</span> the subject or the memory roster means starting a new stack — they are the two halves of one experiment. Every run measured here was measured under exactly this policy.</span>
          </label>
          <div class="modal-actions">
            <button class="btn primary" data-profile-create="1" ${ack && subject && selection.size && !pending ? "" : "disabled"}>${pending ? "FREEZING…" : "FREEZE PROFILE"}</button>
            <button class="btn" data-profile-cancel="1">CANCEL</button>
            <span class="spacer"></span>
            <span class="note">${esc(freezeWhy())}</span>
          </div>
        </div>
      </div>
    </div>`;
}

function rosterNote(roster, proxyOk, models) {
  if (!roster) {
    return "Roster unavailable — the control plane is not enabled, so no model list can be read.";
  }
  if (!proxyOk) {
    // Names shown anywhere may be from a stale cache — say so, don't hide it.
    return "proxy_ok: false — the proxy is unreachable. Model names shown here are from the last successful response and may be wrong.";
  }
  return `Roster read from the proxy — /api/roster, ${models.length} model${models.length === 1 ? "" : "s"}, proxy_ok: true. oMLX is the provider; the proxy is the surface this board reads.`;
}

/**
 * THE REFUSAL, IN THE OPERATOR'S FACE.
 *
 * Rendered inside the modal, directly above the button that caused it, so the
 * answer appears where the question was asked. Both halves are shown and they
 * are different things:
 *
 *   code    the machine reason — greppable, and the thing to quote in a report
 *   reason  the server's own prose, verbatim and never paraphrased here
 *
 * A paraphrase would be a SECOND definition of why the server refused, free to
 * drift from the first. The server owns the wording; this panel owns only the
 * fact that the operator sees it.
 *
 * `transport` marks a refusal that never reached the server at all (the fetch
 * itself threw). That is a materially different diagnosis — the control plane
 * being down is not the same as the control plane saying no — so it is labelled
 * distinctly rather than rendered as a generic failure.
 */
function refusalBlock() {
  if (!refusal) return "";
  const transport = refusal.code === "transport_failed";
  return `
    <div class="freeze-refusal" role="alert">
      <span class="fr-head">${esc(transport ? "COULD NOT REACH THE CONTROL PLANE" : "THE CONTROL PLANE REFUSED THIS PROFILE")}</span>
      <span class="fr-code">${esc(refusal.code ?? "no code given")}</span>
      <span class="fr-reason">${esc(refusal.reason ?? "no reason given")}</span>
      <span class="fr-note">${esc(transport
        ? "Nothing was written. The request never arrived, so no profile was created — check that the control plane is running, then try again."
        : "Nothing was written. Change a choice above and freeze again — the profile store is unchanged.")}</span>
    </div>`;
}

/**
 * Why FREEZE is disabled, on the control itself.
 *
 * Two required facts means two distinct ways to be incomplete, and "disabled"
 * without saying which one is missing is indistinguishable from broken — the
 * same rule the run-start button follows.
 */
function freezeWhy() {
  if (pending) return "posting to the control plane — waiting for the answer";
  if (!subject && !selection.size) return "choose a subject model and at least one memory producer";
  if (!subject) return "no subject model — nothing is being measured";
  if (!selection.size) return "no memory producer ticked — this stack could recall nothing";
  if (!ack) return "acknowledge the freeze to continue";
  return "freezing writes the subject and the roster · it starts nothing";
}

/** SUBJECT picker — single-select, so the row is a radio, not a checkbox. */
function subjectPickRow(m) {
  const id = typeof m === "string" ? m : m.id;
  const on = subject === id;
  const resident = typeof m === "object" ? m.resident : null;
  // Residency is a fact about right now, not a gate: the proxy makes the model
  // resident on first request. It is shown because a non-resident subject means
  // the first cell pays a load, and `null` means the runtime was unreachable —
  // which is not the same as "not loaded" and must not read as it.
  const note =
    resident === true ? "resident" : resident === false ? "not resident — loads on first request" : "residency unobserved";

  return `
    <div class="mrow radio ${on ? "on" : ""}" data-subject="${esc(id)}">
      <span class="mcheck">${on ? "●" : "○"}</span>
      <span class="mid">${esc(id)}</span>
      <span class="mnote">${esc(note)}</span>
    </div>`;
}

/** MEMORY ROSTER picker — multi-select. */
function pickRow(m, subjectId) {
  const id = typeof m === "string" ? m : m.id;
  const on = selection.has(id);
  const isSubject = Boolean(subjectId) && id === subjectId;
  const n = typeof m === "object" && Number.isFinite(m.memories) ? m.memories : null;
  const note =
    n === null
      ? "memory count unobserved"
      : n === 0
        ? "0 memories — contributes nothing yet"
        : `${n.toLocaleString()} memories in corpus`;

  return `
    <div class="mrow ${on ? "on" : ""}${isSubject ? " subj" : ""}" data-model="${esc(id)}">
      <span class="mcheck">${on ? "✓" : ""}</span>
      <span class="mid">${esc(id)}</span>
      ${isSubject ? `<span class="subjtag">SUBJECT</span>` : ""}
      <span class="mnote ${n === 0 ? "muted" : ""}">${esc(note)}</span>
    </div>`;
}
