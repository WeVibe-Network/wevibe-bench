// ─────────────────────────────────────────────────────────────────────────────
// PANEL: RUN LEDGER — one row per model, profiles nested inside
//
// THE SHAPE. Model name leftmost, [+baseline] [+profile] rightmost. Clicking a
// model row expands an accordion of that model's profiles, each carrying the
// measurement columns and its own [+run]. Clicking a PROFILE row opens the
// frozen policy underneath it.
//
// ── THE CONSOLIDATION (this panel is now the only home) ─────────────────────
//
// A frozen profile used to be readable on TWO other surfaces: a board-level
// MEMORY PROFILE card and a full-screen inspector modal it opened. Both are
// gone, and their content is the drawer at the foot of this file.
//
// They were not merely duplicates, they were WRONG. Both read `board.profile` —
// a single global "the profile", from the era of one profile per machine — so
// with four profiles on disk they showed exactly one and gave the operator no
// way to tell which. The ledger already holds every profile, keyed to the model
// it measures, which is the shape the data actually has. The card also carried
// its own frozen timestamp, subject, roster and transfer edge: four restatements
// of facts that appear in this accordion, free to disagree with it.
//
// What was NOT duplicated — the full memory roster with per-model corpus counts,
// the transfer prose, the debt block and the run history — is preserved here in
// full. The consolidation removes surfaces, not facts.
//
// THE THREE RULES THIS SURFACE EXPRESSES, none of them decided here:
//   1. a run cannot start until the model's baseline is complete and non-void
//   2. a profile cannot be created until that same baseline exists
//   3. runs are SERIAL — one cell in flight blocks EVERY button on EVERY model
//
// EVERY GATE IS COMPUTED SERVER-SIDE (`control/models-ledger.mjs`) and arrives
// as `{allowed, reason}`. This panel renders that verdict and never re-derives
// it. A button whose enabled state disagreed with the refusal the server would
// actually apply is worse than no button: it teaches the operator that the UI
// lies, and the lesson generalises to every other control on the board.
//
// A DISABLED BUTTON ALWAYS STATES WHY. `reason` is rendered beside the row, not
// buried in a tooltip nobody on a stream can hover. The most common reason —
// "no baseline yet" — is the entire point of the layout, so hiding it would
// hide the workflow.
//
// THE FOOTER IS THE HARD RULE MADE VISIBLE. Efficiency and correctness sit in
// two boxes, SIDE BY SIDE, at the SAME type size. There is no third box
// combining them, no arrow, no score. A single "improvement" number would be
// the most natural thing to put here and would silently let a faster-and-worse
// cell read as a win.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, tok, dur } from "../board.js";
// The profile VOCABULARY is imported, never re-worded. The transfer edge, the
// debt block and the UNFILTERED RECALL badge must read identically here and in
// the creation modal — two phrasings of "declared, not enforced" would be two
// claims, and the operator has no way to know they mean the same thing.
import { transferBlock, debtBlock, unfilteredBadge } from "./profile.js";

/**
 * The accordion is CLAMPED, NEVER PRE-ALLOCATED. Space for ten profile rows is
 * reserved only if ten exist; one profile gets one row's worth of height. A
 * fixed ten-row well would leave nine rows of dead space under the common case
 * and read as "nine profiles failed to load".
 */
const PROFILE_CLAMP = 10;

/**
 * WHICH MODEL ROW IS OPEN. Module-local, matching the pattern the inspector
 * uses (panels/profile.js) — it is view state, not measurement, so it never
 * belongs on the board payload where a poll would overwrite it.
 *
 * ONE ROW AT A TIME. Opening a second row closes the first: the accordion can
 * hold ten profile rows, and two open at once pushes the two-axis footer — the
 * board's hard rule made visible — off the bottom of the screen.
 */
let expandedModel = null;

/**
 * WHICH PROFILE'S FROZEN POLICY IS OPEN.
 *
 * Also one at a time, and for a stronger reason than the model rows: the drawer
 * carries the full memory roster, and two open drawers would push the row an
 * operator is comparing against off the screen.
 *
 * A profile id is globally unique, so this is NOT scoped to the open model —
 * closing a model row leaves the id set but unreachable, and it is reconciled on
 * the next draw rather than tracked in two places.
 */
let expandedProfile = null;

export function toggleModelRow(id) {
  expandedModel = expandedModel === id ? null : id;
  // Collapsing the model takes its drawer with it. Leaving the id set would
  // re-open a drawer the operator closed the row on, minutes later.
  expandedProfile = null;
}

export function toggleProfileRow(id) {
  expandedProfile = expandedProfile === id ? null : id;
}

export function expandedModelId() {
  return expandedModel;
}

export function expandedProfileId() {
  return expandedProfile;
}

export function renderLedger(board) {
  const s = board.stack ?? {};
  const ledger = board.models_ledger ?? null;

  // A row whose model left the roster must not stay open invisibly — the state
  // is reconciled against what is actually being drawn.
  if (expandedModel && ledger && !(ledger.models ?? []).some((m) => m.id === expandedModel)) {
    expandedModel = null;
    expandedProfile = null;
  }
  const expanded = expandedModel;

  return `
    <section class="ledger">
      <div class="ledger-head">
        <span class="ttl">RUN LEDGER</span>
        <span class="sub">every model in this bench · baseline first · profiles nested</span>
        <span class="spacer"></span>
        ${floorsChip(ledger)}
        ${serialChip(ledger)}
      </div>
      ${ledger ? models(board, ledger, expanded) : unwired()}
      ${footer(s, ledger)}
    </section>`;
}

/**
 * HOW MANY MODELS HAVE A FLOOR — read from the stored baseline index.
 *
 * `models_ledger.baselines` is the control plane's single baseline export
 * (control/baselines.mjs, also served at /api/baselines and written to
 * runs/baselines.json). Every gate in the rows below resolves from that same
 * object, so this count cannot disagree with the buttons underneath it.
 *
 * It answers the question the row-by-row view makes slow: a bench of four
 * models with one floor is one measurement and three models that cannot be
 * benchmarked yet, and that ratio is the campaign's actual progress.
 */
function floorsChip(ledger) {
  const idx = ledger?.baselines ?? null;
  if (!idx?.models) return "";
  const all = Object.values(idx.models);
  if (!all.length) return "";
  const floored = all.filter((b) => b?.scorable).length;
  return `<span class="tag${floored === all.length ? " on" : ""}" title="${esc(idx.note ?? "")}">FLOORS ${floored}/${all.length}</span>`;
}

/**
 * THE SERIAL RULE, STATED ONCE AT THE TOP.
 *
 * It is a property of the BENCH, not of any model, and a reader scanning rows
 * should not have to infer it from every row carrying the same refusal.
 */
function serialChip(ledger) {
  if (!ledger) return "";
  if (!ledger.run_in_flight) {
    return `<span class="tag">IDLE — NO CELL IN FLIGHT</span>`;
  }
  return `<span class="tag bad" title="${esc(ledger.serial_note ?? "")}">CELL IN FLIGHT — ALL LAUNCHES BLOCKED</span>`;
}

function unwired() {
  return `
    <div class="ledger-empty">
      <div class="bright">${esc("The model ledger is unavailable.")}</div>
      <div class="note">${esc(
        "GET /api/models-ledger did not answer. Without it the launch gates cannot be evaluated, and this panel will not draw buttons whose enabled state it cannot verify.",
      )}</div>
    </div>`;
}

function models(board, ledger, expanded) {
  const rows = ledger.models ?? [];
  if (!rows.length) {
    const why = ledger.unwired_reason ?? "no bench-eligible model is served by the proxy roster";
    return `
      <div class="ledger-empty">
        <div class="bright">${esc("No bench-eligible models.")}</div>
        <div class="note">${esc(why)}</div>
      </div>`;
  }
  return `${rows.map((m) => modelRow(board, m, expanded === m.id)).join("")}${orphans(ledger)}`;
}

/**
 * PROFILES WHOSE SUBJECT MODEL LEFT THE ROSTER.
 *
 * The server keeps these deliberately (they still exist on disk) and it would
 * defeat the point to compute them and then not draw them: a profile that
 * vanishes the moment its model stops being served looks like data loss, and an
 * operator hunting for it has no surface that admits it still exists.
 *
 * They carry no buttons. Nothing can be run under them until their model is
 * back on the roster, and that is stated rather than implied by absence.
 */
function orphans(ledger) {
  const list = ledger.orphaned_profiles ?? [];
  if (!list.length) return "";
  return `
    <div class="lorph">
      <div class="note">${esc(
        `${list.length} profile${list.length === 1 ? "" : "s"} on disk whose subject model is not currently bench-eligible — `
        + "kept, but nothing can run under them until that model is served again",
      )}</div>
      ${list.map((p) => `<div class="lorph-row"><span class="pname">${esc(p.id)}</span><span class="muted">${esc(p.subject_model ?? "unknown model")}</span></div>`).join("")}
    </div>`;
}

/**
 * ONE MODEL. The row is the control surface; the accordion is the detail.
 *
 * The whole row is the expand affordance, so the click target is the size of
 * the row rather than a caret an operator has to aim at. The buttons stop
 * propagation implicitly by being checked first in board.js's delegated
 * handler — a [+baseline] click must never also toggle the accordion.
 */
function modelRow(board, m, open) {
  const b = m.baseline ?? {};
  const n = (m.profiles ?? []).length;

  return `
    <div class="lrow-wrap${open ? " open" : ""}">
      <div class="lrow" data-model-expand="${esc(m.id)}" role="button" tabindex="0" aria-expanded="${open ? "true" : "false"}">
        <span class="lcaret">${open ? "▾" : "▸"}</span>
        <span class="lname" title="${esc(m.upstream_model ?? m.id)}">${esc(m.id)}</span>
        ${baselineChip(b)}
        <span class="lprof">${n} profile${n === 1 ? "" : "s"}</span>
        <span class="spacer"></span>
        ${gateBtn("data-run-baseline", m.id, "+ baseline", m.can_baseline)}
        ${gateBtn("data-new-profile", m.id, "+ profile", m.can_profile)}
      </div>
      ${blockedNote(m)}
      ${open ? accordion(board, m) : ""}
    </div>`;
}

/**
 * THE BASELINE STATE, IN WORDS.
 *
 * Four distinct states, never collapsed: a valid floor, a VOID floor (numbers
 * exist but measure the harness), a floor being measured right now, and none at
 * all. Void is the one that matters most — it looks like success from every
 * angle except the one that counts.
 */
function baselineChip(b) {
  if (b.scorable) {
    const when = b.measured_before ? String(b.measured_before).slice(0, 10) : null;
    const shared = (b.shared_by ?? 0) > 1 ? ` · shared by ${b.shared_by} profiles` : "";
    return `<span class="tag on" title="${esc(`floor: ${b.run_dir ?? "?"} seq ${b.sequence_index ?? "?"}`)}">BASELINE${when ? ` ${esc(when)}` : ""}${esc(shared)}</span>`;
  }
  if (b.voided) return `<span class="tag bad">BASELINE VOID</span>`;
  if (b.pending) return `<span class="tag">BASELINE RUNNING</span>`;
  return `<span class="tag">NO BASELINE</span>`;
}

/**
 * A gated button renders as a button ONLY when it is allowed. When it is not,
 * it renders as a disabled control carrying `title`, and the reason is ALSO
 * printed in full below the row — a disabled control with no visible
 * explanation is the single most common way a UI wastes an operator's time.
 */
function gateBtn(attr, model, label, gate) {
  const allowed = gate?.allowed === true;
  if (!allowed) {
    return `<button class="btn sm" disabled title="${esc(gate?.reason ?? "not available")}">${esc(label)}</button>`;
  }
  return `<button class="btn sm primary" ${attr}="${esc(model)}">${esc(label)}</button>`;
}

/**
 * The refusal, printed. Only ONE line even when both buttons are blocked for
 * the same cause — repeating "a cell is in flight" twice per row, on four rows,
 * is noise that trains the operator to stop reading.
 */
function blockedNote(m) {
  const reasons = [m.can_baseline?.reason, m.can_profile?.reason].filter(Boolean);
  if (!reasons.length) return "";
  const unique = [...new Set(reasons)];
  return `<div class="lwhy">${unique.map((r) => `<span class="null">${esc(r)}</span>`).join("")}</div>`;
}

// ── THE ACCORDION ───────────────────────────────────────────────────────────

function accordion(board, m) {
  const profiles = m.profiles ?? [];
  if (!profiles.length) {
    return `
      <div class="lacc">
        <div class="null">${esc(
          m.baseline?.scorable
            ? "no profile yet — + profile freezes one against this model's baseline"
            : "no profile yet, and none can be created until this model has a valid baseline",
        )}</div>
      </div>`;
  }

  // CLAMPED, NOT PRE-ALLOCATED: the well grows with the content up to ten rows
  // and only then scrolls.
  const clamp = Math.min(profiles.length, PROFILE_CLAMP);
  const over = profiles.length > PROFILE_CLAMP;

  // ── THE TABLE IS ONLY DRAWN WHEN THERE IS SOMETHING TO PUT IN IT ─────────
  //
  // The eight measurement columns (PHASES…VERDICT, Δ) are the CELL's. Today no
  // cell can be attributed to a profile at all — a profile's run record carries
  // a log name and the launcher records no run_dir, so `latest_cell` is null for
  // every row and the server says so in `latest_cell_unavailable`.
  //
  // Drawn anyway, that is ten columns squeezed into an accordion where eight of
  // them read `unobserved` / `never ran` / `unknown` — text wider than the 52px
  // and 62px tracks holding it, so the values overflowed into each other and the
  // row became unreadable. Worse, it repeated the same "no cell attributed"
  // sentence under EVERY row.
  //
  // So: the full table appears when at least one profile here has a measured
  // cell, and until then the row states what is actually known — how many cells
  // were launched under the profile and when the last one was — with the reason
  // no measurement is attached stated ONCE, under the well.
  const measured = profiles.some((p) => p.latest_cell);
  const unjoined = profiles.find((p) => !p.latest_cell && p.latest_cell_unavailable);

  return `
    <div class="lacc">
      <div class="pcols${measured ? "" : " lean"}">
        ${measured
          ? `<span>PROFILE</span><span>PHASES</span><span>TURNS</span><span>TOKENS</span>
             <span>TIME</span><span>GATES</span><span>CORPUS</span><span>VERDICT</span>
             <span>Δ VS BASELINE</span><span></span>`
          : `<span>PROFILE</span><span>CELLS LAUNCHED</span><span>LAST LAUNCH</span><span></span>`}
      </div>
      <div class="pscroll" style="--clamp:${clamp}">
        ${profiles.map((p) => profileRow(p, m, measured)).join("")}
      </div>
      ${over ? `<div class="note">${esc(`${profiles.length} profiles — scrolling; ten fit at a time`)}</div>` : ""}
      ${measured || !unjoined
        ? ""
        : `<div class="note">${esc(`no measurement column is shown because none can be filled: ${unjoined.latest_cell_unavailable}`)}</div>`}
      ${drawerFor(board, profiles)}
    </div>`;
}

/**
 * THE OPEN DRAWER SITS BELOW THE WELL, NOT INSIDE IT.
 *
 * `.pscroll` is height-clamped and scrolls; a drawer nested in it would open
 * into a ten-row-tall box and immediately scroll its own contents away. Below
 * the well the policy gets the full width of the accordion and the measurement
 * rows above it stay where the operator left them.
 */
function drawerFor(board, profiles) {
  const p = profiles.find((q) => q.id === expandedProfile);
  return p ? profileDrawer(board, p) : "";
}

/**
 * ONE PROFILE, in whichever of the two shapes the accordion resolved.
 *
 * MEASURED: the full cell table. Every column is the CELL's, not the profile's,
 * and an absent value says `unobserved` rather than 0 — a zero would assert a
 * measured result of nothing.
 *
 * LEAN: what is known without a cell — how many cells were launched under this
 * profile and when the last one went out. No `unobserved` columns, because a
 * column that can never be filled is not a pending measurement, it is a column
 * that should not be drawn.
 *
 * THE ROW IS ALSO THE WAY IN TO THE FROZEN POLICY. It replaced a chip labelled
 * OPEN INSPECTOR: the whole row is the target now, for the same reason the model
 * row above it is, and the caret says so before the click rather than after.
 */
function profileRow(p, m, measured) {
  const runs = p.runs ?? [];
  const latest = runs.length ? runs[runs.length - 1] : null;
  const cell = p.latest_cell ?? null;
  const open = expandedProfile === p.id;

  const head = `<span class="pname" title="${esc(p.id)}"><span class="pcaret">${open ? "▾" : "▸"}</span>${esc(p.id)}${transferTag(p)}</span>`;
  const act = `<span class="pact">${gateBtn("data-run-profile", p.id, "+ run", p.can_run)}</span>`;
  const attrs = `data-profile-expand="${esc(p.id)}" role="button" tabindex="0" aria-expanded="${open ? "true" : "false"}"`;

  const body = measured
    ? `${head}
      <span>${cell ? esc(phases(cell)) : nul("no run")}</span>
      <span>${num(cell?.turns, (v) => String(v))}</span>
      <span>${num(cell?.tokens, (v) => tok(v))}</span>
      <span>${num(cell?.wall_seconds, (v) => dur(v))}</span>
      <span>${gatesCell(cell)}</span>
      <span>${corpusCell(cell)}</span>
      <span class="${cell?.verdict === "FAIL" ? "danger" : ""}">${cell?.verdict ? esc(cell.verdict) : nul("—")}</span>
      <span class="delta">${profileDelta(cell, m)}</span>
      ${act}`
    : `${head}
      <span>${runs.length ? esc(`${runs.length} cell${runs.length === 1 ? "" : "s"}`) : nul("none")}</span>
      <span>${latest ? esc(launchedAt(latest)) : nul("never launched")}</span>
      ${act}`;

  return `
    <div class="prow${open ? " open" : ""}${measured ? "" : " lean"}" ${attrs}>${body}</div>
    ${p.can_run?.allowed === false && p.can_run?.reason ? `<div class="pwhy"><span class="null">${esc(p.can_run.reason)}</span></div>` : ""}`;
}

/** Launch time, to the minute. Seconds are noise on a cell that runs for hours. */
function launchedAt(r) {
  if (!r.started_at) return "time unobserved";
  const d = new Date(r.started_at);
  if (Number.isNaN(d.valueOf())) return "time unobserved";
  return d.toLocaleString("en-GB", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function transferTag(p) {
  const k = p.transfer?.kind;
  if (!k) return "";
  const word = k === "self" ? "SAME" : k === "cross" ? "CROSS" : "MIXED";
  // DECLARED, NOT ENFORCED stays visible: no recall path filters by producing
  // model, so the roster states a policy nothing applies.
  return ` <span class="muted">${esc(word)}${p.enforced ? "" : " · declared"}</span>`;
}

function num(v, fmt) {
  if (v === null || v === undefined) return nul("unobserved");
  return esc(fmt(v));
}

function phases(c) {
  if (c.state === "not_started") return "not started";
  return `${c.phases?.done ?? 0} / ${c.phases?.total ?? 3}`;
}

function gatesCell(c) {
  if (!c) return nul("never ran");
  const g = c.gates ?? {};
  if (g.failed === null || g.failed === undefined) return nul("not graded");
  if (!g.total) return `${g.failed} failed`;
  return `${g.total - g.failed}/${g.total}`;
}

function corpusCell(c) {
  const at = c?.corpus?.at_recall;
  // A hole in the chain is not a zero. "0" would claim an empty corpus, which
  // is a measurement we do not have.
  if (at === null || at === undefined) return nul("unknown");
  return at.toLocaleString();
}

/**
 * Δ against THIS MODEL's floor — the baseline the accordion sits under, which
 * is why it is passed in rather than looked up. A Δ against another model's
 * floor would be a capability comparison wearing a memory-lift label.
 */
function profileDelta(c, m) {
  const b = m.baseline ?? {};
  if (!c) return "";
  if (c.state === "running") return `<span class="null">Δ withheld until the cell closes</span>`;
  if (c.void_instrument) return `<span class="muted">void instrument — not a capability result</span>`;
  if (!b.scorable) return `<span class="null">no valid floor</span>`;
  if (c.turns === null || b.turns === null || b.turns === undefined) return nul("unobserved");

  const dTurns = c.turns - b.turns;
  const worse = dTurns > 0;
  const parts = [`${sign(dTurns)}${Math.abs(dTurns)} turns`];
  if (c.tokens !== null && b.tokens !== null && b.tokens !== undefined) {
    parts.push(`${sign(c.tokens - b.tokens)}${tok(Math.abs(c.tokens - b.tokens))}`);
  }
  return `<span class="${worse ? "danger" : "bright"}">${esc(parts.join(" · "))}${worse ? " — worse than baseline" : ""}</span>`;
}

function sign(d) {
  return d > 0 ? "+" : d < 0 ? "−" : "±";
}

// ── THE FROZEN POLICY, IN PLACE ─────────────────────────────────────────────
//
// This is what the MEMORY PROFILE card and its inspector modal used to show,
// rendered under the row it belongs to instead of in a separate surface that had
// to guess which profile the operator meant.
//
// IT READS THE ROW'S OWN PROFILE — `p`, handed down from the accordion — and
// never `board.profile`. That single global field is what made the old surfaces
// wrong: with four profiles on disk it named one, and the operator could not
// tell whether the policy on screen was the one whose row they had clicked.
//
// NOTHING HERE IS AN EDIT CONTROL. A frozen profile has no pencil, no "manage"
// and no greyed-out button implying a later unlock — a disabled control is a
// promise that it will one day be enabled. Frozen means the affordance is
// absent. Changing an allowlist means freezing a new profile, which is what
// [+ profile] on the model row above does.

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
          ${historySec(p)}
        </div>
      </div>

      <div class="note">${esc(
        "Freezing a profile writes the subject and the memory roster and does nothing else — it does not arm a cell, "
        + "open a session, or attach a TUI. Runs start from + run on the row above, and only from there.",
      )}</div>
    </div>`;
}

/**
 * ACTIVE vs SUPERSEDED, stated rather than left to the refusal.
 *
 * The launcher attributes every cell to the NEWEST profile and accepts no
 * profile id (control/models-ledger.mjs), which is why `+ run` is dead on every
 * older row. The row already carries that refusal; this says the same fact the
 * positive way, because "superseded" is also the answer to "why does the curve
 * not join these two series" — they were measured under different allowlists.
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

/**
 * RUNS UNDER THIS PROFILE.
 *
 * Attribution is what the control plane OBSERVED at launch. A cell started from
 * the CLI is real and is deliberately absent, because it cannot be shown to have
 * run under this allowlist — and that absence is STATED, since an empty list
 * with no explanation reads as "no runs happened", which would be false.
 */
function historySec(p) {
  const runs = Array.isArray(p.runs) ? [...p.runs].reverse() : [];

  return `
    <div class="pd-sec">
      <div class="pd-secline">
        <span class="kick">RUNS UNDER THIS PROFILE — ${runs.length}</span>
        <span class="spacer"></span>
        <span class="note">${esc("cells this control plane launched while this profile was active")}</span>
      </div>
      <div class="pd-cols"><span>ARM</span><span>MODEL</span><span>STARTED</span><span>LOG</span></div>
      ${runs.length
        ? runs.map(historyRow).join("")
        : `<div class="pd-empty">${esc(
            "no cell has been launched under this profile from this board. Cells started at the CLI are real but unattributed — "
            + "they are not swept in here, because they cannot be shown to have run under this allowlist.",
          )}</div>`}
    </div>`;
}

function historyRow(r) {
  const when = r.started_at ? new Date(r.started_at).toLocaleString("en-GB") : null;
  return `
    <div class="pd-row">
      <span class="arm">${esc(String(r.arm ?? "—").toUpperCase())}</span>
      <span class="model">${r.model ? esc(r.model) : nul("unobserved")}</span>
      <span>${when ? esc(when) : nul("unobserved")}</span>
      <span class="log">${r.log_name ? esc(r.log_name) : nul("unobserved")}</span>
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
