// ─────────────────────────────────────────────────────────────────────────────
// PANEL: BASELINES — every completed OFF measurement, profiles nested inside
//
// ── WHAT REPLACED WHAT, AND WHY ─────────────────────────────────────────────
//
// This was the RUN LEDGER: one row per bench-eligible MODEL, with that model's
// floor as a chip on the row and its profiles in an accordion. It is now one row
// per BASELINE, with the profiles measured against that baseline nested inside
// it and their runs nested inside those.
//
// THE OLD ROOT WAS THE MODEL UNIVERSE, WHICH IS NOT WHAT THE CARD IS FOR. A
// bench serving six aliases drew six rows, five of them carrying NO BASELINE and
// a disabled button — five statements of intent above the one measurement that
// exists. The card that answers "what has this bench measured" was mostly
// occupied by what it has not.
//
// THE NEW NESTING IS THE ARGUMENT. A profile has no meaning apart from the
// baseline it is measured against: its entire content is a Δ, and a Δ is a
// subtraction from one specific floor. Rendering profiles as top-level objects
// (which is what a model-rooted card does, one level down) invites the one
// mistake this board exists to prevent — comparing a profile's result against a
// floor that is not the one it was measured against. Under this shape that
// comparison is not merely discouraged, it is unspellable: the floor is the row
// the profile is physically inside.
//
// A MODEL WITH NO FLOOR HAS NO ROW. That is the point, not an omission. Starting
// a baseline is the first branch of the [+ PROFILE] modal, which is where an
// absence belongs — a card of measurements should not be padded with placeholder
// rows for measurements nobody has taken.
//
// ── THE RULES THIS SURFACE EXPRESSES, none of them decided here ─────────────
//   1. a profile cannot be created until its baseline is complete and non-void
//   2. a run cannot start until that same baseline is still valid NOW
//   3. runs are SERIAL — one cell in flight blocks EVERY launch on EVERY row
//
// EVERY GATE IS COMPUTED SERVER-SIDE (`control/models-ledger.mjs`) and arrives
// as `{allowed, reason}`. This panel renders that verdict and never re-derives
// it. A button whose enabled state disagreed with the refusal the server would
// actually apply is worse than no button: it teaches the operator that the UI
// lies, and the lesson generalises to every other control on the board.
//
// A DISABLED CONTROL ALWAYS STATES WHY, beside the row rather than in a tooltip
// nobody on a stream can hover.
//
// ── THE TWO-AXIS FOOTER STAYS ───────────────────────────────────────────────
//
// Efficiency and correctness sit in two boxes, SIDE BY SIDE, at the SAME type
// size. There is no third box combining them, no arrow, no score. A single
// "improvement" number would be the most natural thing to put here and would
// silently let a faster-and-worse cell read as a win. The design specimen for
// this card does not show it — it shows one card out of a board — and it is kept
// because it is a hard rule of the board, not a feature of the old shape.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, tok, dur } from "../board.js";
// The profile VOCABULARY is imported, never re-worded. The transfer edge, the
// debt block and the UNFILTERED RECALL badge must read identically here and in
// the creation flow — two phrasings of "declared, not enforced" would be two
// claims, and the operator has no way to know they mean the same thing.
import { transferBlock, debtBlock, unfilteredBadge } from "./profile.js";

/**
 * WHICH BASELINE ROW IS OPEN. Module-local: it is view state, not measurement,
 * so it never belongs on the board payload where a poll would overwrite it.
 *
 * ONE AT A TIME. Opening a second row closes the first — a row can hold several
 * profiles, each of which can hold several runs, and two open at once pushes the
 * two-axis footer off the bottom of the screen.
 */
let expandedBaseline = null;

/**
 * WHICH PROFILE IS OPEN, inside the open baseline.
 *
 * Also one at a time, and for a stronger reason: the expansion carries the full
 * frozen policy AND the run history, so two open would push the row an operator
 * is comparing against off the screen.
 *
 * A profile id is globally unique, so this is NOT scoped to the open baseline —
 * closing a baseline row leaves the id unreachable, and it is reconciled on the
 * next draw rather than tracked in two places.
 */
let expandedProfile = null;

export function toggleBaselineRow(id) {
  expandedBaseline = expandedBaseline === id ? null : id;
  // Collapsing the baseline takes its open profile with it. Leaving the id set
  // would re-open a drawer the operator closed the row on, minutes later.
  expandedProfile = null;
}

export function toggleProfileRow(id) {
  expandedProfile = expandedProfile === id ? null : id;
}

export function expandedBaselineId() {
  return expandedBaseline;
}

export function expandedProfileId() {
  return expandedProfile;
}

export function renderLedger(board) {
  const s = board.stack ?? {};
  const ledger = board.models_ledger ?? null;
  const rows = ledger?.baseline_rows ?? null;

  // A row whose baseline left the index must not stay open invisibly — the
  // state is reconciled against what is actually being drawn.
  if (expandedBaseline && rows && !rows.some((b) => b.id === expandedBaseline)) {
    expandedBaseline = null;
    expandedProfile = null;
  }

  return `
    <section class="ledger bl">
      ${head(ledger)}
      ${ledger ? body(board, ledger, rows) : unwired()}
      ${nesting()}
      ${footer(s, ledger)}
    </section>`;
}

/**
 * THE HEAD — what the card is, how much of it there is, and the one control.
 *
 * ONE BUTTON, NOT ONE PER ROW. [+ baseline] and [+ profile] used to sit on every
 * model row, which put up to twelve controls on a card whose job is to be read.
 * Both acts now start from a single [+ PROFILE] that asks which one is meant —
 * see panels/create.js. The count beside it ("5 complete · 1 running") is what
 * the per-row view made slow to answer.
 */
function head(ledger) {
  const c = ledger?.counts ?? null;
  return `
    <div class="ledger-head">
      <span class="ttl">BASELINES</span>
      <span class="sub">every completed OFF measurement · local and cloud · a profile can only exist under one of these</span>
      ${c ? `<span class="blcount">${esc(countWords(c))}</span>` : ""}
      ${serialChip(ledger)}
      <button class="btn sm primary blnew" data-create-open="1">+ PROFILE</button>
    </div>`;
}

/**
 * VOID IS COUNTED SEPARATELY OR NOT AT ALL — never folded into "complete".
 *
 * A void-instrument baseline ran to completion and produced numbers that measure
 * the harness. Counting it as complete would inflate the bench's apparent
 * progress with a floor nothing may be measured against; omitting it silently
 * would make a baseline the operator remembers running disappear from the tally.
 */
function countWords(c) {
  const bits = [`${c.complete} complete`];
  if (c.running) bits.push(`${c.running} running`);
  if (c.void) bits.push(`${c.void} void`);
  return bits.join(" · ");
}

/**
 * THE SERIAL RULE, STATED ONCE AT THE TOP.
 *
 * It is a property of the BENCH, not of any baseline, and a reader scanning rows
 * should not have to infer it from every row carrying the same refusal.
 */
function serialChip(ledger) {
  if (!ledger) return "";
  if (!ledger.run_in_flight) return `<span class="tag">IDLE — NO CELL IN FLIGHT</span>`;
  return `<span class="tag bad" title="${esc(ledger.serial_note ?? "")}">CELL IN FLIGHT — ALL LAUNCHES BLOCKED</span>`;
}

function unwired() {
  return `
    <div class="ledger-empty">
      <div class="bright">${esc("The baseline index is unavailable.")}</div>
      <div class="note">${esc(
        "GET /api/models-ledger did not answer. Without it the launch gates cannot be evaluated, and this panel will not draw controls whose enabled state it cannot verify.",
      )}</div>
    </div>`;
}

function body(board, ledger, rows) {
  if (!rows || !rows.length) return empty(ledger);
  return `
    ${cols()}
    ${rows.map((b) => baselineRow(board, ledger, b)).join("")}
    ${orphans(ledger)}`;
}

/**
 * NO BASELINES AT ALL — the cold-install state, and the one that must not read
 * as a failure.
 *
 * It states the next action rather than the absence, because on a fresh bench
 * the absence is correct and the operator's question is "what do I do", not
 * "what went wrong".
 */
function empty(ledger) {
  const startable = (ledger.startable ?? []).filter((m) => m.can_baseline?.allowed);
  return `
    <div class="ledger-empty">
      <div class="bright">${esc("No baseline has been measured yet.")}</div>
      <div class="note">${esc(
        "A baseline is one OFF cell — the floor every Δ on this board is subtracted from. Nothing can be "
        + "profiled, run or compared until one exists.",
      )}</div>
      <div class="note">${
        startable.length
          ? esc(`${startable.length} model${startable.length === 1 ? " is" : "s are"} ready to baseline — + PROFILE, then “start new baseline”.`)
          : esc("No model can start one right now; open + PROFILE to see what each is waiting on.")
      }</div>
    </div>`;
}

/** SEVEN COLUMNS, shared by the header and every baseline row so they cannot drift. */
function cols() {
  return `
    <div class="blcols">
      <span></span><span>BASELINE</span><span>KIND</span><span>MODEL · PROVIDER</span>
      <span>TURNS</span><span>GATES</span><span>PROFILES</span>
    </div>`;
}

/**
 * PROFILES WHOSE SUBJECT MODEL IS ON NEITHER SUBSTRATE.
 *
 * The server keeps these deliberately (they still exist on disk) and it would
 * defeat the point to compute them and then not draw them: a profile that
 * vanishes the moment its model stops being served looks like data loss, and an
 * operator hunting for it has no surface that admits it still exists.
 *
 * They carry no controls. Nothing can be run under them until their model is
 * available again, and that is stated rather than implied by absence.
 */
function orphans(ledger) {
  const list = ledger.orphaned_profiles ?? [];
  if (!list.length) return "";
  return `
    <div class="lorph">
      <div class="note">${esc(
        `${list.length} profile${list.length === 1 ? "" : "s"} on disk whose subject model is served by neither the proxy roster nor the cloud catalogue — `
        + "kept, but nothing can run under them until that model is available again",
      )}</div>
      ${list.map((p) => `<div class="lorph-row"><span class="pname">${esc(p.id)}</span><span class="muted">${esc(p.subject_model ?? "unknown model")}</span></div>`).join("")}
    </div>`;
}

// ── ONE BASELINE ────────────────────────────────────────────────────────────

/**
 * The whole row is the expand affordance, so the click target is the size of the
 * row rather than a caret an operator has to aim at.
 *
 * A ROW WITH NO PROFILES STILL EXPANDS. It opens onto the statement that it has
 * none and what that costs — which is exactly the row an operator most needs an
 * explanation on, and the one a "nothing to expand" row would refuse to give.
 */
function baselineRow(board, ledger, b) {
  const open = expandedBaseline === b.id;
  const n = b.profile_count ?? 0;

  return `
    <div class="blwrap${open ? " open" : ""}${b.state === "running" ? " running" : ""}">
      <div class="blrow" data-baseline-expand="${esc(b.id)}" role="button" tabindex="0" aria-expanded="${open ? "true" : "false"}">
        <span class="blcaret">${open ? "▾" : "▸"}</span>
        <span class="blid" title="${esc(`${b.run_dir ?? "?"} seq ${b.sequence_index ?? "?"}`)}">${esc(b.id)} · ${esc(shortModel(b.model))}</span>
        <span class="blkind ${esc(b.kind ?? "local")}">${esc(b.kind_label ?? "LOCAL")}</span>
        <span class="blmodel" title="${esc(b.model_slug ?? b.model ?? "")}">${esc(b.model ?? "unknown model")}${b.provider ? esc(` · ${b.provider}`) : ""}</span>
        <span>${b.turns === null || b.turns === undefined ? nul("— pending") : esc(String(b.turns))}</span>
        <span>${gatesCell(b.gates)}</span>
        <span class="blstate ${esc(b.state)}">${esc(stateWord(b, n))}</span>
      </div>
      ${voidNote(b)}
      ${open ? drawer(board, ledger, b) : ""}
    </div>`;
}

/**
 * The right-hand column: what this row IS, in the vocabulary of profiles.
 *
 * A RUNNING BASELINE SAYS SO AND SAYS NOTHING ELSE. Its profile count is not
 * zero — it is not yet a question, because a baseline with no total cannot carry
 * profiles at all. Printing "NO PROFILES" on it would state a fact about a
 * decision nobody has been allowed to make yet.
 */
function stateWord(b, n) {
  // ELAPSED, NOT "AGO". This column is 128px and "RUNNING · 22m ago" clips to
  // "RUNNING · 22m ag…", which reads as a truncated word rather than a duration.
  // The design's own form is "RUNNING · 22m": the cell is running NOW, so the
  // number is how long it has been going, and "ago" is the wrong preposition for
  // it anyway — it belongs on the run rows, where the event is in the past.
  if (b.state === "running") return `RUNNING${b.campaign_started_at ? ` · ${elapsed(b.campaign_started_at)}` : ""}`;
  if (b.state === "void") return "VOID — NOT A FLOOR";
  if (!n) return "NO PROFILES";
  return `${n} PROFILE${n === 1 ? "" : "S"}`;
}

/**
 * THE VOID EXPLANATION, printed on the row rather than behind the expansion.
 *
 * Void is the state that matters most and looks like success from every angle
 * except the one that counts: the cell ran, it produced turns and gates, and
 * every one of those numbers measures the harness. An operator who does not
 * read this row's reason will read its numbers.
 */
function voidNote(b) {
  if (b.state !== "void" || !b.reason) return "";
  return `<div class="blwhy"><span class="null">${esc(b.reason)}</span></div>`;
}

/** GATES: a real ratio when the suite total was recorded, and never a fake one. */
function gatesCell(g) {
  if (!g) return nul("not graded");
  if (!g.total) return nul("no suite total");
  const passed = g.passed ?? (g.total - (g.failed ?? 0));
  return `<span class="${g.failed ? "danger" : ""}">${esc(`${passed}/${g.total}`)}</span>`;
}

/**
 * A model id, shortened for the identity column only.
 *
 * The FULL id is one column to the right and the full slug is on the title, so
 * nothing is lost — this is the design's `base-8d1e · qwen3c-30b`, where the
 * second half is a hint for scanning rather than the authoritative name.
 *
 * THIRTEEN CHARACTERS, measured rather than guessed: the column is 210px, the
 * face is 12.5px mono (~7.5px/char ≈ 27 characters), and `base-XXXX · ` spends
 * twelve of them. Truncating HERE rather than leaving it to the CSS ellipsis is
 * what keeps the cut at a whole character on every row instead of mid-glyph at
 * a width that shifts with the id.
 */
function shortModel(id) {
  const s = String(id ?? "");
  const bare = s.includes("/") ? s.split("/").pop() : s;
  return bare.length <= 13 ? bare : `${bare.slice(0, 12)}…`;
}

// ── THE PROFILES UNDER A BASELINE ───────────────────────────────────────────

function drawer(board, ledger, b) {
  const profiles = b.profiles ?? [];

  if (!profiles.length) {
    return `
      <div class="blacc">
        <div class="null">${esc(
          b.can_profile?.allowed
            ? "no profile rests on this baseline yet — + PROFILE freezes one against it"
            : (b.can_profile?.reason ?? "no profile yet, and none can be created against this baseline"),
        )}</div>
      </div>`;
  }

  return `
    <div class="blacc">
      <div class="pcols">
        <span></span><span>PROFILE</span><span>MEMORY SOURCES</span><span>RUNS</span><span>BEST</span><span></span>
      </div>
      ${profiles.map((p) => profileRow(board, b, p)).join("")}
    </div>`;
}

/**
 * ONE PROFILE. The row is the way in to its policy AND its runs.
 *
 * BOTH LIVE IN ONE EXPANSION, deliberately. They are the two halves of the same
 * question — what was frozen, and what happened under it — and splitting them
 * across two affordances is what let the old board show a policy without the
 * runs measured under it and vice versa.
 */
function profileRow(board, b, p) {
  const open = expandedProfile === p.id;
  const n = p.run_count ?? 0;

  return `
    <div class="prow-wrap${open ? " open" : ""}">
      <div class="prow" data-profile-expand="${esc(p.id)}" role="button" tabindex="0" aria-expanded="${open ? "true" : "false"}">
        <span class="pcaret">${open ? "▾" : "▸"}</span>
        <span class="pname" title="${esc(p.id)}">${esc(p.id)} — ${esc(`${p.source_count} source model${p.source_count === 1 ? "" : "s"}`)}${transferTag(p)}</span>
        <span class="psrc" title="${esc((p.memory_models ?? []).join(", "))}">${esc((p.memory_models ?? []).join(", ") || "—")}</span>
        <span>${n ? esc(`${n} run${n === 1 ? "" : "s"}`) : nul("no runs")}</span>
        <span class="pbest">${bestCell(p)}</span>
        <span class="pact">${runBtn(b, p)}</span>
      </div>
      ${p.can_run?.allowed === false && p.can_run?.reason ? `<div class="pwhy"><span class="null">${esc(p.can_run.reason)}</span></div>` : ""}
      ${open ? profileBody(board, b, p) : ""}
    </div>`;
}

/**
 * THE BEST RESULT, WITH ITS AXIS ON IT.
 *
 * "BEST −14 TURNS", never "BEST". The board's hard rule is that efficiency and
 * correctness are never blended, and an unqualified superlative is the shortest
 * possible way to break it — a run that finished in fewer turns and failed more
 * gates would be "the best" with nothing on screen to contradict it.
 */
function bestCell(p) {
  const bst = p.best ?? null;
  if (!bst) return nul("no measured run");
  return `<span class="${bst.better ? "bright" : "danger"}" title="${esc(bst.note ?? "")}">${esc(
    `BEST ${sign(bst.turns)}${Math.abs(bst.turns)} TURNS`,
  )}</span>`;
}

function transferTag(p) {
  const k = p.transfer?.kind;
  if (!k) return "";
  const word = k === "self" ? "SAME" : k === "cross" ? "CROSS" : "MIXED";
  // DECLARED, NOT ENFORCED stays visible: no recall path filters by producing
  // model, so the roster states a policy nothing applies.
  return ` <span class="muted">${esc(word)}${p.enforced ? "" : " · declared"}</span>`;
}

/**
 * A gated control renders as a live button ONLY when it is allowed. When it is
 * not, it renders disabled carrying `title`, and the reason is ALSO printed in
 * full below the row — a disabled control with no visible explanation is the
 * single most common way a UI wastes an operator's time.
 */
function gateBtn(attr, id, label, gate) {
  if (gate?.allowed !== true) {
    return `<button class="btn sm" disabled title="${esc(gate?.reason ?? "not available")}">${esc(label)}</button>`;
  }
  return `<button class="btn sm primary" ${attr}="${esc(id)}">${esc(label)}</button>`;
}

/**
 * [+ run] — and it carries THE MODEL AND THE SUBSTRATE, not just the profile id.
 *
 * The previous version passed the PROFILE ID as the run's model and got away
 * with it only because the run control overwrote the field with the profile's
 * frozen subject a moment later. That accident does not survive cloud: the
 * substrate has to travel too (it decides whether the cell is billed and how the
 * server resolves the identity), and there is no second surface to correct it
 * from. Both facts are read off the row they belong to and stated on the button.
 */
function runBtn(b, p) {
  const gate = p.can_run;
  if (gate?.allowed !== true) {
    return `<button class="btn sm" disabled title="${esc(gate?.reason ?? "not available")}">+ run</button>`;
  }
  return `<button class="btn sm primary"
    data-run-profile="${esc(p.id)}"
    data-run-model="${esc(p.subject_model ?? b.model ?? "")}"
    data-run-kind="${esc(b.kind ?? "local")}">+ run</button>`;
}

// ── INSIDE A PROFILE: THE FROZEN POLICY, THEN THE RUNS ──────────────────────

function profileBody(board, b, p) {
  return `
    <div class="pbody">
      ${profileDrawer(board, p)}
      ${runs(p, b)}
    </div>`;
}

/**
 * THE RUNS UNDER THIS PROFILE, newest first.
 *
 * Every column here is the CELL's, not the profile's. A run with no cell
 * attributed states WHICH of the three reasons applies (see joinRuns in
 * control/models-ledger.mjs) rather than rendering `unobserved` across five
 * columns, because "not measured" and "cannot be attributed" send an operator
 * to two different places.
 */
function runs(p, b) {
  const list = p.runs ?? [];
  if (!list.length) {
    return `
      <div class="rsec">
        <span class="kick">RUNS UNDER THIS PROFILE — 0</span>
        <div class="pd-empty">${esc(
          "no cell has been launched under this profile from this board. Cells started at the CLI are real but "
          + "unattributed — they are not swept in here, because they cannot be shown to have run under this allowlist.",
        )}</div>
      </div>`;
  }

  return `
    <div class="rsec">
      <div class="pd-secline">
        <span class="kick">RUNS UNDER THIS PROFILE — ${list.length}</span>
        <span class="spacer"></span>
        <span class="note">${esc("newest first · cells this control plane launched while this profile was active")}</span>
      </div>
      <div class="rcols">
        <span></span><span>RUN</span><span>WHEN</span><span>DETAIL</span><span>TURNS</span><span>Δ VS BASELINE</span>
      </div>
      ${list.map((r) => runRow(r, b)).join("")}
    </div>`;
}

function runRow(r, b) {
  const c = r.cell ?? null;
  return `
    <div class="rrow">
      <span></span>
      <span class="rseq">${esc(`run ${String(r.seq).padStart(2, "0")}`)}</span>
      <span>${r.started_at ? esc(since(r.started_at)) : nul("time unobserved")}</span>
      <span class="rdetail" title="${esc(r.cell_unavailable ?? "")}">${detail(r, c)}</span>
      <span>${c && c.turns !== null ? esc(String(c.turns)) : nul("—")}</span>
      <span class="delta">${deltaCell(r, b)}</span>
    </div>`;
}

/** The design's "3 of 3 · 90/114 gates", or the reason there is no cell. */
function detail(r, c) {
  if (!c) return `<span class="null">${esc(r.cell_unavailable ?? "no cell attributed")}</span>`;
  const bits = [];
  const ph = c.phases ?? {};
  bits.push(c.state === "complete" ? `${ph.done ?? 0} of ${ph.total ?? 3}` : `phase ${ph.done ?? 0} of ${ph.total ?? 3} · running`);
  if (c.gates?.total) bits.push(`${c.gates.passed ?? c.gates.total - (c.gates.failed ?? 0)}/${c.gates.total} gates`);
  if (c.void_instrument) bits.push("VOID INSTRUMENT");
  if (c.verdict) bits.push(c.verdict);
  return esc(bits.join(" · "));
}

/**
 * Δ AGAINST THE BASELINE THIS ROW IS PHYSICALLY INSIDE.
 *
 * Computed on the server against that same floor and rendered here — the panel
 * does not subtract anything. `better` arrives as a word rather than being read
 * off the sign, because fewer turns is an improvement and a leading minus reads
 * as a loss to everyone who has ever seen a financial figure.
 */
function deltaCell(r, b) {
  const d = r.delta ?? null;
  if (!d) return r.cell ? nul("—") : "";
  if (!d.computable) return `<span class="null">${esc(`Δ ${d.reason}`)}</span>`;
  const parts = [`${sign(d.turns)}${Math.abs(d.turns)} turns`];
  if (d.tokens !== null && d.tokens !== undefined) parts.push(`${sign(d.tokens)}${tok(Math.abs(d.tokens))}`);
  return `<span class="${d.better ? "bright" : "danger"}">${esc(parts.join(" · "))}${d.better ? "" : " — worse than baseline"}</span>`;
}

function sign(d) {
  return d > 0 ? "+" : d < 0 ? "−" : "±";
}

/**
 * ELAPSED, IN WORDS, to a resolution that means something.
 *
 * Seconds are noise on a cell that runs for hours, and an exact timestamp is
 * what an operator has to do arithmetic on. "11m ago" is the design's own form
 * and it is the one that answers the question being asked of this column.
 */
function since(when) {
  const s = secondsSince(when);
  if (s === null) return "time unobserved";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d}d ago`;
}

/** The same duration, for something still happening. No "ago". */
function elapsed(when) {
  const s = secondsSince(when);
  if (s === null) return "elapsed unobserved";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function secondsSince(when) {
  const t = typeof when === "number" ? when : Date.parse(String(when));
  if (!Number.isFinite(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}

// ── THE FROZEN POLICY, IN PLACE ─────────────────────────────────────────────
//
// IT READS THE ROW'S OWN PROFILE — `p`, handed down — and never `board.profile`.
// That single global field is what made the deleted MEMORY PROFILE card and its
// inspector modal wrong: with four profiles on disk they named one, and the
// operator could not tell whether the policy on screen was the one whose row
// they had clicked.
//
// NOTHING HERE IS AN EDIT CONTROL. A frozen profile has no pencil, no "manage"
// and no greyed-out button implying a later unlock — a disabled control is a
// promise that it will one day be enabled. Frozen means the affordance is
// absent. Changing an allowlist means freezing a new profile.

function profileDrawer(board, p) {
  const frozen = p.created_at ? new Date(p.created_at).toLocaleString("en-GB") : null;

  return `
    <div class="pdrawer">
      <div class="pd-head">
        <span class="kick">FROZEN POLICY — READ-ONLY FOREVER</span>
        <span class="note">${frozen ? esc(`frozen ${frozen}`) : nul("creation time unobserved")}</span>
        <span class="spacer"></span>
        ${activeTag(p)}
        ${unfilteredBadge()}
      </div>

      <div class="pd-grid">
        <div class="pd-col">
          ${subjectSec(p)}
          ${rosterSec(board, p)}
        </div>
        <div class="pd-col">
          ${transferBlock(p.transfer)}
          ${debtBlock()}
        </div>
      </div>
    </div>`;
}

/**
 * ACTIVE vs SUPERSEDED, stated rather than left to the refusal.
 *
 * The launcher attributes every cell to the NEWEST profile and accepts no
 * profile id, which is why `+ run` is dead on every older row. The row already
 * carries that refusal; this says the same fact the positive way, because
 * "superseded" is also the answer to "why does the curve not join these two
 * series" — they were measured under different allowlists.
 */
function activeTag(p) {
  return p.is_active
    ? `<span class="tag on">ACTIVE — CELLS ATTRIBUTE HERE</span>`
    : `<span class="tag" title="${esc("a newer profile is active; the launcher attributes every cell to it")}">SUPERSEDED</span>`;
}

/**
 * THE SUBJECT — and the one enforcement fact that runs the OTHER way from the
 * debt badge beside it. The roster is inert; the subject is not. Run start
 * refuses a cell on any other model, so the OFF→ON pair cannot drift even while
 * the allowlist does nothing.
 */
function subjectSec(p) {
  return `
    <div class="pd-sec">
      <span class="kick">SUBJECT — THE MEASUREMENT</span>
      <div class="pd-subject">
        <span class="sval">${p.subject_model ? esc(p.subject_model) : nul("no subject frozen")}</span>
        <span class="note">${esc(
          "OFF and ON are both this model. Run start refuses a cell on any other — the subject is enforced, unlike the roster below.",
        )}</span>
      </div>
    </div>`;
}

/**
 * THE MEMORY ROSTER, IN FULL — picked models checked, the rest left visible and
 * unchecked.
 *
 * Listing only the picks would hide the models that are excluded, and the
 * excluded set is exactly what the debt block is about: every one of them still
 * reaches recall today.
 */
function rosterSec(board, p) {
  const roster = board.control?.roster ?? null;
  const all = Array.isArray(roster?.models) ? roster.models : [];
  const picked = new Set((p.memory_models ?? []).map((m) => (typeof m === "string" ? m : m?.id)));
  const subj = p.subject_model ?? null;

  // The roster can be unreachable while the profile is perfectly readable — the
  // picks are frozen IN the profile, so they are drawn from it rather than
  // showing nothing.
  const rows = all.length
    ? all.map((m) => {
        const mid = typeof m === "string" ? m : m.id;
        return rosterRow(mid, picked.has(mid), m, mid === subj);
      })
    : [...picked].map((mid) => rosterRow(mid, true, null, mid === subj));

  return `
    <div class="pd-sec">
      <span class="kick">${esc(
        all.length
          ? `MEMORY ROSTER — ${picked.size} OF ${all.length} QUALIFIED`
          : `MEMORY ROSTER — ${picked.size} QUALIFIED`,
      )}</span>
      <div class="pd-models">${rows.join("")}</div>
      ${all.length
        ? ""
        : `<span class="note">${esc("roster unreachable — showing the allowlist frozen in the profile, which is authoritative regardless")}</span>`}
    </div>`;
}

/**
 * One roster row. A model with ZERO memories is marked: without it an operator
 * sees a qualified model contribute nothing and concludes the filter is broken.
 *
 * A RETIRED ALIAS STILL APPEARS HERE, and that is deliberate. This is the
 * PRODUCER roster, not the bench roster: a memory's producer is whichever model
 * authored it, which may be a model the bench no longer runs. Dropping retired
 * aliases would make a historical corpus unqualifiable. It is marked instead, so
 * nobody reads its presence as the bench still offering to run it.
 */
function rosterRow(mid, on, m, isSubject) {
  const n = m && typeof m === "object" && Number.isFinite(m.memories) ? m.memories : null;
  const retired = m && typeof m === "object" ? (m.retired_reason ?? null) : null;
  const note =
    n === null
      ? "memory count unobserved"
      : n === 0
        ? "0 memories — contributes nothing yet"
        : `${n.toLocaleString()} memories in corpus`;

  return `
    <div class="mrow ${on ? "on" : "off"}${isSubject ? " subj" : ""}">
      <span class="mcheck">${on ? "✓" : ""}</span>
      <span class="mid">${esc(mid)}${retired ? ` <span class="muted" title="${esc(retired)}">· retired from the bench</span>` : ""}</span>
      ${isSubject ? `<span class="subjtag">SUBJECT</span>` : ""}
      <span class="mnote ${n === 0 ? "muted" : ""}">${esc(note)}</span>
    </div>`;
}

// ── THE NESTING, ARGUED IN WORDS ────────────────────────────────────────────

/**
 * The design's two footer sentences, kept verbatim in substance.
 *
 * They are on the card rather than in a doc because the nesting is a CLAIM about
 * what these objects are, and a reader who does not know the claim reads the
 * indentation as a filing convention they are free to ignore.
 */
function nesting() {
  return `
    <div class="blnest">
      <span>${esc(
        "Runs sort newest first inside a profile. A running baseline holds its row with the pending state and cannot carry profiles until it closes.",
      )}</span>
      <span>${esc(
        "Nesting is the argument: a profile has no meaning apart from the baseline it is measured against, so it is never a top-level object.",
      )}</span>
    </div>`;
}

// ── THE PINNED FLOOR + THE TWO-AXIS FOOTER ──────────────────────────────────

function footer(s, ledger) {
  const base = s.baseline;
  if (!base) {
    return `
      <div class="ledger-foot">
        <div class="pin">
          <span class="kick">PINNED — THE FLOOR</span>
          <span class="null">no OFF cell in this stack</span>
          <span class="note">Every Δ on this board is measured against one OFF cell. Without it nothing here can be compared.</span>
          ${storedNote(ledger)}
        </div>
      </div>`;
  }

  const on = (s.runs ?? []).filter((r) => !r.void_instrument && r.state === "complete");
  const latest = on.length ? on[on.length - 1] : null;

  return `
    <div class="ledger-foot">
      <div class="pin">
        <span class="kick">PINNED — THE FLOOR</span>
        <span class="bright">${String(base.seq).padStart(2, "0")} · OFF · ${esc(base.model ?? "unknown model")}</span>
        <span class="note">n=1 by design. Not a distribution. Every Δ on this board is measured against this one cell.${
          s.baseline_scorable ? "" : " <span class='danger'>This cell is void-instrument — no Δ computed from it is valid.</span>"
        }</span>
        ${storedNote(ledger)}
      </div>
      <div class="axes">
        ${effBox(base, latest, s)}
        ${corrBox(base, latest, s)}
      </div>
    </div>`;
}

/**
 * WHERE THE FLOORS ARE RECORDED.
 *
 * THE PINNED CELL ABOVE IS THIS STACK'S FLOOR — one campaign, one OFF cell, and
 * the two Δ boxes beside it are computed from that stack's own runs. It is NOT
 * the same question as "which models have a floor", which is bench-wide and
 * lives in the stored index. Naming the file keeps the two straight and tells a
 * skeptic where to look without opening the control plane.
 */
function storedNote(ledger) {
  const st = ledger?.baselines?.stored ?? null;
  if (!st?.path) return "";
  return `<span class="note">Per-model floors are recorded at <span class="bright">${esc(st.path)}</span> and served at /api/baselines — one derivation, read by every gate above.</span>`;
}

function effBox(base, latest, s) {
  if (!latest || !s.baseline_scorable) {
    return `
      <div class="axis">
        <span class="kick">EFFICIENCY — OFF vs LATEST ON</span>
        <div class="null">${esc(!s.baseline_scorable ? "no valid floor to measure against" : "no completed ON cell yet")}</div>
        <span class="note">turns · tokens · wall time</span>
      </div>`;
  }
  const dT = latest.turns - base.turns;
  const dK = latest.tokens !== null && base.tokens !== null ? latest.tokens - base.tokens : null;
  const dS = latest.wall_seconds !== null && base.wall_seconds !== null ? latest.wall_seconds - base.wall_seconds : null;
  const w = (d) => (d > 0 ? "danger" : "bright");

  return `
    <div class="axis">
      <span class="kick">EFFICIENCY — OFF vs LATEST ON (run ${String(latest.seq).padStart(2, "0")})</span>
      <div class="big">
        <span class="${w(dT)}">${sign(dT)}${Math.abs(dT)} turns</span>
        ${dK === null ? `<span class="null">tokens unobserved</span>` : `<span class="${w(dK)}">${sign(dK)}${tok(Math.abs(dK))} tokens</span>`}
        ${dS === null ? `<span class="null">time unobserved</span>` : `<span class="${w(dS)}">${sign(dS)}${dur(Math.abs(dS))}</span>`}
      </div>
      <span class="note">${esc(`${base.turns} → ${latest.turns} turns`)}${
        dK === null ? "" : esc(` · ${tok(base.tokens)} → ${tok(latest.tokens)}`)
      }${dS === null ? "" : esc(` · ${dur(base.wall_seconds)} → ${dur(latest.wall_seconds)}`)}</span>
    </div>`;
}

function corrBox(base, latest, s) {
  if (!latest || !s.baseline_scorable) {
    return `
      <div class="axis">
        <span class="kick">CORRECTNESS — OFF vs LATEST ON</span>
        <div class="null">${esc(!s.baseline_scorable ? "no valid floor to measure against" : "no completed ON cell yet")}</div>
        <span class="note">gates passed / observed. Never multiplied into efficiency.</span>
      </div>`;
  }

  const bg = base.gates ?? {};
  const lg = latest.gates ?? {};
  const total = lg.total ?? bg.total ?? null;
  if (bg.failed === null || lg.failed === null || !total) {
    return `
      <div class="axis">
        <span class="kick">CORRECTNESS — OFF vs LATEST ON (run ${String(latest.seq).padStart(2, "0")})</span>
        <div class="null">gates not measured on both cells</div>
        <span class="note">Stated side by side, same size, never combined into a score.</span>
      </div>`;
  }

  const bPass = total - bg.failed;
  const lPass = total - lg.failed;
  const d = lPass - bPass;
  const bothFail = base.verdict === "FAIL" && latest.verdict === "FAIL";

  return `
    <div class="axis">
      <span class="kick">CORRECTNESS — OFF vs LATEST ON (run ${String(latest.seq).padStart(2, "0")})</span>
      <div class="big">
        <span class="${d < 0 ? "danger" : "bright"}">${sign(d)}${Math.abs(d)} gate${Math.abs(d) === 1 ? "" : "s"}</span>
        <span class="mid">${bPass}/${total} → ${lPass}/${total}<span class="muted"> obs</span></span>
        ${bothFail ? `<span class="tag bad">BOTH FAIL</span>` : ""}
      </div>
      <span class="note">Stated side by side, same size, never combined into a score.</span>
    </div>`;
}
