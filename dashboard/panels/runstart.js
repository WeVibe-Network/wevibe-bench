// ─────────────────────────────────────────────────────────────────────────────
// PANEL: RUN START — the only surface that can begin a benchmark cell
//
// ── PROVENANCE: PORTED, NOT REWRITTEN ───────────────────────────────────────
//
// The arm→confirm protocol here is carried over from the previous drawer
// implementation, which was fully dead code: `panels/drawer.js` was served by
// server.mjs but imported by nothing, its CSS had been dropped, and it was
// therefore the ONLY run-start UI in the tree while being unreachable from the
// board. That file is deleted; this is where its logic now lives.
//
// Every behaviour below exists because its absence was a real defect, so none
// of it was re-derived:
//
//   · THE TOKEN IS MINTED BY THE SERVER, NEVER THE BROWSER. A client-generated
//     confirmation confirms nothing the server can trust. PREVIEW returns both
//     the token and the restatement, and the token is echoed back verbatim.
//   · THE RESTATEMENT IS THE SERVER'S WORDS. The words the operator reads are
//     the words the server will act on; a client-composed summary can drift
//     from the payload it claims to describe.
//   · ANY PARAMETER CHANGE DISARMS. The token fingerprints the parameters, so
//     silently reusing it would start a run the operator never agreed to.
//   · ON NEEDS AN ORG, OFF FORBIDS ONE. Enforced in the form so the operator
//     learns the rule before the refusal rather than after committing.
//   · REFUSALS RENDER VERBATIM WITH THEIR CODE. Every refusal reason in the
//     control plane was written for a human reading a stream; paraphrasing
//     strips exactly the detail needed to fix the cause.
//   · RESUME IS REFUSED IN THE UI, WITH ITS REASON. The harness has no mid-cell
//     checkpoint. A button that always 501s implies a capability that does not
//     exist.
//
// ── THE FOUR STATES (design 5b) ─────────────────────────────────────────────
//
//   IDLE       single press arms the confirm step
//   BLOCKED    disabled WITH THE REASON ON THE CONTROL, never a tooltip — a
//              control disabled without saying why is indistinguishable from a
//              broken one, and nobody on a stream can hover
//   LIVE       a cell is already running: "ARE YOU SURE?" naming what is lost
//   STARTING   an elapsed counter runs so the wait is visibly bounded, not hung
//
// ── THE CONFIRMATION IS NOT DECORATIVE ──────────────────────────────────────
// Starting a run while a cell is live ABANDONS that cell. The campaign is
// strictly serial — one resident local model, one slot — so there is no queue
// to fall back on, and a partial cell is never graded and never enters the
// curve. A benchmark cell costs hours, so the second click is the cheapest
// insurance on the board.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, clip, dur } from "../board.js";

/**
 * Local UI state. Never derived from the board payload — it describes what the
 * OPERATOR is doing, which no poll can know.
 */
const ui = {
  // `arm` has NO default on purpose. It decides whether an org is required (ON)
  // or forbidden (OFF), so guessing it would either mint a run against the
  // wrong arm or produce a restatement reading "UNKNOWN ARM".
  sel: { model: "", context: "", arm: "", org: "" },
  armed: false,
  token: null,
  restatement: null,
  pending: false,
  refusal: null,
  // Set when START fires, cleared once run state reports a live cell. Drives
  // the STARTING counter.
  startedAt: null,
};

export function runSel() {
  return ui.sel;
}

export function setRunSel(key, value) {
  if (!(key in ui.sel)) return;
  ui.sel[key] = value;
  // Any parameter change invalidates a pending confirmation.
  disarm();
}

export function disarm() {
  ui.armed = false;
  ui.token = null;
  ui.restatement = null;
}

export function isArmed() {
  return ui.armed;
}

export function isPending() {
  return ui.pending;
}

export function clearRefusal() {
  ui.refusal = null;
}

/**
 * ARM. Asks the server to validate the parameters and mint a token.
 *
 * The server runs the SAME validation it will run at start, minus the serial
 * gate — so preview can never green-light a run that start would refuse, which
 * would move the refusal to after the operator has committed.
 */
export async function armRun(base) {
  ui.pending = true;
  ui.refusal = null;
  try {
    const res = await fetch(`${base}/api/run/preview`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload()),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || data?.ok === false) {
      ui.refusal = { code: data?.code ?? `HTTP ${res.status}`, reason: data?.reason ?? "preview refused" };
      disarm();
    } else {
      ui.armed = true;
      ui.token = data?.token ?? null;
      ui.restatement = data?.restatement ?? null;
    }
  } catch (err) {
    ui.refusal = { code: "unreachable", reason: String(err?.message ?? err) };
    disarm();
  } finally {
    ui.pending = false;
  }
}

/**
 * CONFIRM. Sends the SAME parameters that were previewed, carrying the token.
 * Any divergence is rejected by the server rather than quietly starting a
 * different run.
 */
export async function startRun(base) {
  if (!ui.token) {
    disarm();
    return;
  }
  ui.pending = true;
  try {
    const res = await fetch(`${base}/api/run/start`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...payload(), confirm: ui.token }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || data?.ok === false) {
      ui.refusal = { code: data?.code ?? `HTTP ${res.status}`, reason: data?.reason ?? "refused" };
      ui.startedAt = null;
    } else {
      // The run is live. The TUI mirror is opened by the caller — attaching is
      // a separate, explicit act rather than a side effect buried in here.
      ui.startedAt = Date.now();
      // Attribution is reported by the server: it says which profile the cell
      // was recorded against, or why it could not be. A failure to attribute
      // must be visible, not silent — the run still started.
      if (data?.attribution && data.attribution.recorded === false && data.attribution.reason) {
        ui.refusal = { code: "unattributed", reason: data.attribution.reason };
      }
    }
  } catch (err) {
    ui.refusal = { code: "unreachable", reason: String(err?.message ?? err) };
    ui.startedAt = null;
  } finally {
    ui.pending = false;
    disarm();
  }
}

function payload() {
  return {
    model: ui.sel.model,
    arm: ui.sel.arm || undefined,
    org: ui.sel.arm === "on" ? ui.sel.org.trim() || undefined : undefined,
    context: Number(ui.sel.context) || undefined,
  };
}

// ── RENDER ───────────────────────────────────────────────────────────────────

export function renderRunControl(board) {
  const c = board.control ?? null;

  if (!c) {
    return `
      <div class="runctl">
        <span class="kick">START NEW RUN</span>
        <div class="note">Control plane not enabled — the board is read-only by construction and cannot start a run itself. Start the control service and reload.</div>
      </div>`;
  }

  const run = c.run ?? {};
  const roster = c.roster ?? {};
  const caps = c.capabilities ?? {};
  const s = board.stack ?? {};
  const p = board.profile ?? {};

  const eligible = (roster.models ?? []).filter((m) => m.eligible !== false && m.bench_eligible !== false);

  // THE SUBJECT IS PINNED, NOT PICKED.
  //
  // A frozen profile names the model whose OFF→ON pair is the measurement, and
  // /api/run/start refuses any other. Leaving a free dropdown here would offer
  // eight choices of which seven produce a refusal — and worse, it is what let
  // the subject be decided by a later click rather than by the profile. When a
  // profile exists the model is stated, not selected.
  const subject = p.exists ? (p.subject_model ?? null) : null;
  if (subject && ui.sel.model !== subject) {
    ui.sel.model = subject;
    // A pin that changed invalidates any armed token — it fingerprints the model.
    disarm();
  }

  // THE BASELINE GATE. An ON cell measured against no floor, or against a floor
  // the harness refuses to score, produces a delta that cannot be defended.
  const baselineOk = Boolean(s.baseline) && s.baseline_scorable !== false;

  // AND THE FLOOR MUST BELONG TO THE SUBJECT. A scorable OFF cell run on a
  // DIFFERENT model is not a floor for this one: the Δ would be the gap between
  // two models plus whatever the memories did, with no way to separate them.
  // Checked here because `baseline` is chosen by recency within the stack and
  // is model-blind on its own.
  const baselineModel = s.baseline?.model ?? null;
  const baselineWrongModel =
    Boolean(subject) && Boolean(baselineModel) && baselineModel !== subject;

  const wantsOn = ui.sel.arm === "on";
  const blockedNoBaseline = wantsOn && (!baselineOk || baselineWrongModel);

  const live = run.can_start === false;
  const armChosen = ui.sel.arm === "on" || ui.sel.arm === "off";
  const orgOk = !wantsOn || Boolean(ui.sel.org.trim());
  const complete = armChosen && orgOk && Boolean(ui.sel.model) && eligible.length > 0;

  // STARTING: the request has fired and run state has not yet caught up.
  const starting = ui.startedAt !== null && run.state !== "running";
  if (ui.startedAt !== null && run.state === "running") ui.startedAt = null;

  return `
    <div class="runctl">
      <div class="runctl-head">
        <span class="kick">START NEW RUN</span>
        <span class="spacer"></span>
        <span class="note">${esc(runStateWord(run))}</span>
      </div>

      ${form(eligible, roster, live, subject)}
      ${ui.armed && ui.restatement ? `<div class="restate">${esc(ui.restatement)}</div>` : ""}
      ${live && ui.armed ? abandonWarning(run) : ""}
      ${refusal()}
      ${button(complete, live, blockedNoBaseline, starting, baselineWrongModel)}
      ${why(run, armChosen, orgOk, eligible, blockedNoBaseline, live, { baselineWrongModel, baselineModel, subject })}
      ${resumeNote(caps)}
    </div>`;
}

function runStateWord(run) {
  const bits = [run.state ?? "unknown"];
  if (run.log_name) bits.push(clip(run.log_name, 30));
  return bits.join(" · ");
}

function form(eligible, roster, live, subject) {
  const modelOpts = eligible.length
    ? eligible.map((m) => opt(m.id, `${m.id}${m.resident ? " · resident" : ""}`, ui.sel.model)).join("")
    : `<option value="">no eligible model</option>`;

  // Pinned, with the reason on the row. A frozen subject is not a disabled
  // control awaiting unlock — it is a fact about this stack, so it reads as a
  // statement rather than as a select the operator cannot use.
  const modelRow = subject
    ? `<div class="rrow">
         <label>MODEL</label>
         <span class="rpin">${esc(subject)}<span class="rpin-why">${esc("subject frozen by the profile — both arms are this model")}</span></span>
       </div>`
    : `<div class="rrow">
         <label>MODEL</label>
         <select class="rsel" data-run-sel="model">
           <option value="">choose…</option>
           ${modelOpts}
         </select>
       </div>`;

  const ctxOpts = (roster.context_choices ?? [65536, 131072, 262144])
    .map((n) => opt(String(n), `${Math.round(n / 1024)}K`, ui.sel.context))
    .join("");

  // The inputs stay ENABLED while a cell is live. The operator must be able to
  // review and prepare the next run's parameters mid-flight; it is the ACTION
  // that is gated by the confirmation, not the form.
  return `
    <div class="runctl-form">
      <div class="rrow">
        <label>ARM</label>
        <select class="rsel" data-run-sel="arm">
          <option value="">choose…</option>
          ${opt("off", "CONTROL (memory off)", ui.sel.arm)}
          ${opt("on", "MEMORY ON", ui.sel.arm)}
        </select>
      </div>
      ${modelRow}
      <div class="rrow">
        <label>CONTEXT</label>
        <select class="rsel" data-run-sel="context">
          <option value="">registry default</option>
          ${ctxOpts}
        </select>
      </div>
      ${ui.sel.arm === "on"
        ? `<div class="rrow">
             <label>ORG</label>
             <input class="rsel" data-run-sel="org" value="${esc(ui.sel.org)}" placeholder="org id">
           </div>`
        : ""}
    </div>`;
}

/**
 * The abandonment warning. Named explicitly because "are you sure" without
 * saying WHAT is lost is not informed consent — the operator needs to know the
 * live cell is kept, marked ABANDONED, never graded, and never enters the curve.
 */
function abandonWarning(run) {
  return `
    <div class="abandon">
      <span class="ttl danger">ARE YOU SURE?</span>
      <span class="note">A cell is ${esc(run.state ?? "in flight")}${run.log_name ? esc(` (${clip(run.log_name, 34)})`) : ""}. Starting a new run abandons it — the partial cell is kept and marked ABANDONED, is never graded, and never enters the curve. Runs are strictly serial; there is no queue.</span>
    </div>`;
}

function button(complete, live, blockedNoBaseline, starting, baselineWrongModel = false) {
  if (starting) {
    const waited = ui.startedAt ? Math.round((Date.now() - ui.startedAt) / 1000) : 0;
    return `
      <div class="starting">
        <span class="ttl">ARMING CELL</span>
        <span class="dots"><i></i><i></i><i></i></span>
        <span class="note">${esc(`${waited}s · waiting for the runner to write its log`)}</span>
      </div>`;
  }

  if (blockedNoBaseline) {
    // Disabled WITH THE REASON ON THE CONTROL — never a tooltip. The two ways
    // to lack a usable floor say different things, because they need different
    // fixes: one wants a baseline run, the other wants it run on this subject.
    return baselineWrongModel
      ? `<button class="runbtn blocked" disabled>START NEW RUN — BASELINE IS ANOTHER MODEL</button>`
      : `<button class="runbtn blocked" disabled>START NEW RUN — NO SCORABLE OFF CELL</button>`;
  }

  if (!complete) {
    return `<button class="runbtn blocked" disabled>START NEW RUN</button>`;
  }

  if (ui.armed) {
    return `<button class="runbtn armed" data-run-confirm="1" ${ui.pending ? "disabled" : ""}>${ui.pending ? "…" : live ? "YES — ABANDON IT AND START" : "CONFIRM — START RUN"}</button>`;
  }

  return `<button class="runbtn" data-run-arm="1" ${ui.pending ? "disabled" : ""}>${ui.pending ? "…" : "START NEW RUN →"}</button>`;
}

function why(run, armChosen, orgOk, eligible, blockedNoBaseline, live, subj = {}) {
  // The wrong-model case is named SEPARATELY from the missing-baseline case.
  // Collapsing them would tell an operator who HAS a complete baseline that
  // they have none, and send them to re-run a cell that already exists.
  if (blockedNoBaseline && subj.baselineWrongModel) {
    return `<div class="rwhy bad">${esc(
      `the floor on this board was run on ${subj.baselineModel}, but this profile's subject is ${subj.subject}. ` +
        "An ON cell scored against another model's floor yields a delta between two models plus whatever " +
        "the memories did, with no way to separate them. Run an OFF cell on the subject first.",
    )}</div>`;
  }
  if (blockedNoBaseline) {
    return `<div class="rwhy bad">${esc("an ON cell needs a scorable OFF baseline — without a floor there is nothing to measure the delta against")}</div>`;
  }
  if (!eligible.length) {
    return `<div class="rwhy bad">${esc("no bench-eligible model in the roster — an interactive slot contends with live daily-driver use and produces a measurement that cannot be defended")}</div>`;
  }
  if (!armChosen) {
    return `<div class="rwhy">${esc("choose an arm — it decides whether the cell extracts into an org")}</div>`;
  }
  if (!orgOk) {
    return `<div class="rwhy">${esc("an ON cell needs an org; extraction has no target without one")}</div>`;
  }
  if (!ui.sel.model) {
    return `<div class="rwhy">${esc("choose a subject model")}</div>`;
  }
  if (live && !ui.armed) {
    return `<div class="rwhy bad">${esc(run.blocked_reason ?? "a cell is already in flight")}</div>`;
  }
  return "";
}

function refusal() {
  if (!ui.refusal) return "";
  // Verbatim, with its code. The server's reason is the useful artifact.
  return `<div class="rwhy bad">REFUSED — ${esc(ui.refusal.code ?? "error")}<br>${esc(ui.refusal.reason ?? "")}</div>`;
}

function resumeNote(caps) {
  if (caps.resume?.supported !== false) return "";
  return `<div class="rwhy">${esc(`RESUME unavailable — ${caps.resume.reason ?? ""} Use ${caps.resume.alternative ?? "archive_and_restart"}.`)}</div>`;
}

function opt(value, label, selected) {
  return `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`;
}
