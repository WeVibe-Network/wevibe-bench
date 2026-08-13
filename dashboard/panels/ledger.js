// ─────────────────────────────────────────────────────────────────────────────
// PANEL: RUN LEDGER — one row per model, profiles nested inside
//
// THE SHAPE. Model name leftmost, [+baseline] [+profile] rightmost. Clicking a
// model row expands an accordion of that model's profiles, each carrying the
// measurement columns and its own [+run].
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

export function toggleModelRow(id) {
  expandedModel = expandedModel === id ? null : id;
}

export function expandedModelId() {
  return expandedModel;
}

export function renderLedger(board) {
  const s = board.stack ?? {};
  const ledger = board.models_ledger ?? null;

  // A row whose model left the roster must not stay open invisibly — the state
  // is reconciled against what is actually being drawn.
  if (expandedModel && ledger && !(ledger.models ?? []).some((m) => m.id === expandedModel)) {
    expandedModel = null;
  }
  const expanded = expandedModel;

  return `
    <section class="ledger">
      <div class="ledger-head">
        <span class="ttl">RUN LEDGER</span>
        <span class="sub">every model in this bench · baseline first · profiles nested</span>
        <span class="spacer"></span>
        ${serialChip(ledger)}
      </div>
      ${ledger ? models(ledger, expanded) : unwired()}
      ${footer(s)}
    </section>`;
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

function models(ledger, expanded) {
  const rows = ledger.models ?? [];
  if (!rows.length) {
    const why = ledger.unwired_reason ?? "no bench-eligible model is served by the proxy roster";
    return `
      <div class="ledger-empty">
        <div class="bright">${esc("No bench-eligible models.")}</div>
        <div class="note">${esc(why)}</div>
      </div>`;
  }
  return `${rows.map((m) => modelRow(m, expanded === m.id)).join("")}${orphans(ledger)}`;
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
function modelRow(m, open) {
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
      ${open ? accordion(m) : ""}
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

function accordion(m) {
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

  return `
    <div class="lacc">
      <div class="pcols">
        <span>PROFILE</span><span>PHASES</span><span>TURNS</span><span>TOKENS</span>
        <span>TIME</span><span>GATES</span><span>CORPUS</span><span>VERDICT</span>
        <span>Δ VS BASELINE</span><span></span>
      </div>
      <div class="pscroll" style="--clamp:${clamp}">
        ${profiles.map((p) => profileRow(p, m)).join("")}
      </div>
      ${over ? `<div class="note">${esc(`${profiles.length} profiles — scrolling; ten fit at a time`)}</div>` : ""}
    </div>`;
}

/**
 * ONE PROFILE. The measurement columns are the CELL's, not the profile's — a
 * profile with no run has no numbers, and every column says `unobserved`
 * rather than 0. A zero here would assert a measured result of nothing.
 */
function profileRow(p, m) {
  const runs = p.runs ?? [];
  const latest = runs.length ? runs[runs.length - 1] : null;
  const cell = p.latest_cell ?? null;
  // The server states WHY there is no cell. That sentence is shown once under
  // the row instead of letting eight `unobserved` columns imply a measurement
  // is merely pending.
  const why = !cell && p.latest_cell_unavailable ? p.latest_cell_unavailable : null;

  return `
    <div class="prow">
      <span class="pname" title="${esc(p.id)}">${esc(p.id)}${transferTag(p)}</span>
      <span>${cell ? esc(phases(cell)) : nul("no run")}</span>
      <span>${num(cell?.turns, (v) => String(v))}</span>
      <span>${num(cell?.tokens, (v) => tok(v))}</span>
      <span>${num(cell?.wall_seconds, (v) => dur(v))}</span>
      <span>${gatesCell(cell)}</span>
      <span>${corpusCell(cell)}</span>
      <span class="${cell?.verdict === "FAIL" ? "danger" : ""}">${cell?.verdict ? esc(cell.verdict) : nul("—")}</span>
      <span class="delta">${profileDelta(cell, m)}</span>
      <span class="pact">${gateBtn("data-run-profile", p.id, "+ run", p.can_run)}</span>
    </div>
    ${p.can_run?.allowed === false && p.can_run?.reason ? `<div class="pwhy"><span class="null">${esc(p.can_run.reason)}</span></div>` : ""}
    ${why ? `<div class="pwhy"><span class="null">${esc(why)}</span></div>` : ""}
    ${latest && !cell ? `<div class="pwhy"><span class="null">${esc(`launched ${new Date(latest.started_at).toISOString().slice(0, 16).replace("T", " ")} — no measurement recorded yet`)}</span></div>` : ""}`;
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

// ── THE PINNED FLOOR + THE TWO-AXIS FOOTER ──────────────────────────────────

function footer(s) {
  const base = s.baseline;
  if (!base) {
    return `
      <div class="ledger-foot">
        <div class="pin">
          <span class="kick">PINNED — THE FLOOR</span>
          <span class="null">no OFF cell in this stack</span>
          <span class="note">Every Δ on this board is measured against one OFF cell. Without it nothing here can be compared.</span>
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
      </div>
      <div class="axes">
        ${effBox(base, latest, s)}
        ${corrBox(base, latest, s)}
      </div>
    </div>`;
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
