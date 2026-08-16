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
  //
  // `kind` DOES default to local, and that asymmetry is deliberate: local is
  // what every cell before cloud routing existed was, so it is the state of the
  // world rather than a guess, and the failure mode of getting it wrong is
  // one-directional — a cloud cell mislabelled local is refused by the roster
  // lookup, while a local cell mislabelled cloud would be billed.
  sel: { model: "", arm: "", org: "", kind: "local" },
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

/**
 * PRESET FROM THE LEDGER. The model row's [+ baseline] and a profile's [+ run]
 * both land here.
 *
 * IT PREFILLS AND ARMS — IT DOES NOT LAUNCH. A benchmark cell costs ~3 hours
 * and starting one while another is live abandons that cell, so the second
 * click is the cheapest insurance on the board (see the header). Wiring the
 * ledger buttons straight to `startRun` would delete exactly that protection
 * for the two paths most likely to be clicked by reflex.
 *
 * The org is deliberately NOT guessed for an ON cell: the server refuses an ON
 * cell with no org, and inventing one would either target the wrong corpus or
 * produce a restatement the operator cannot check.
 */
export function presetRun({ model, arm, kind = "local" }) {
  ui.sel.model = model ?? "";
  ui.sel.arm = arm ?? "";
  // THE SUBSTRATE TRAVELS WITH THE PRESET. It is part of the token's
  // fingerprint on the server, so a preset that dropped it would arm a local
  // cell for a model the operator picked on the cloud branch — and the refusal
  // would arrive as "not served by the proxy roster", which names the wrong
  // cause entirely.
  ui.sel.kind = kind === "cloud" ? "cloud" : "local";
  // A preset is a NEW set of parameters, so any token minted for the previous
  // ones must die with it.
  disarm();
  ui.refusal = null;
}

/**
 * THE LIFECYCLE, PUBLISHED FOR THE STARTUP FEED.
 *
 * `ui` is module-private on purpose, but its state WAS the operator's blind
 * spot: an armed-and-unconfirmed run and a refused preview both lived here and
 * were painted by exactly one surface (`renderRunControl`), which was reachable
 * only through the profile inspector — a dialog that opened only when a profile
 * existed. When it was not on screen the failure existed and was invisible. The
 * inspector is gone; the run control is raised on its own (overlay.js) the
 * moment there is something to confirm or a refusal to read.
 *
 * A READ-ONLY SNAPSHOT, NOT THE OBJECT. Handing out `ui` would let any consumer
 * mutate run-start state from outside the panel that owns it; the copy makes
 * this a report, which is all the feed is entitled to.
 */
export function runLifecycleState() {
  return {
    armed: ui.armed,
    pending: ui.pending,
    refusal: ui.refusal ? { ...ui.refusal } : null,
    restatement: ui.restatement,
    startedAt: ui.startedAt,
    starting: ui.startedAt !== null,
    model: ui.sel.model || null,
    arm: ui.sel.arm || null,
    kind: ui.sel.kind,
    org: ui.sel.arm === "on" ? ui.sel.org.trim() || null : null,
  };
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
    // DECLARED, NEVER SNIFFED. The server refuses an unknown substrate by name
    // rather than inferring one from the model id's shape — see validateStart.
    kind: ui.sel.kind,
    org: ui.sel.arm === "on" ? ui.sel.org.trim() || undefined : undefined,
    // NO `context` KEY. The server treats an absent context as "use the
    // registry default" (server.mjs:314 gates on `context !== null`, and
    // :650 only sets WEVIBE_BENCH_WORKER_NUM_CTX when one was supplied), which
    // is exactly the pinned ceiling every bench alias already carries.
  };
}

// ── RENDER ───────────────────────────────────────────────────────────────────

// ── THE BASELINE CONFIRM MODAL IS GONE, AND WAS NOT REPLACED IN KIND ────────
//
// A one-card "are you sure you want to start a benchmark with <model>?" used to
// live here, raised by [+ baseline] on a model row. Its job — make the operator
// commit deliberately to a multi-hour cell rather than starting one by reflex —
// is now frame BASELINE·3 of panels/create.js, at the end of a three-step
// sequence that also establishes WHICH model and WHICH substrate.
//
// It is deleted rather than kept beside the new flow because two dialogs that
// both mean "confirm this baseline" would eventually diverge on what they warn
// about, and the operator would learn to dismiss whichever one they saw more
// often. The arm→confirm protocol below is untouched: CONTINUE on that frame
// calls armRun() exactly as CONTINUE here did.

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
  // profile exists the ON model is stated, not selected.
  //
  // THE GATE MIRRORS THE SERVER. validateStart enforces subject ownership ONLY
  // on `arm === "on"` — an OFF cell is a baseline and the server always allows
  // it on any eligible model. Pinning an OFF cell overwrote a legally-armable
  // floor and disarmed it for nothing, so the pin fires ONLY when the cell is
  // ON.
  const subject = p.exists ? (p.subject_model ?? null) : null;
  if (ui.sel.arm === "on" && subject && ui.sel.model !== subject) {
    const from = ui.sel.model;
    ui.sel.model = subject;
    // A pin that changed invalidates any armed token — it fingerprints the model.
    disarm();
    // THE PIN IS A REAL MUTATION — overwrite plus disarm — and `disarm()` is
    // purely local, so without a record the event is silent and undiagnosable.
    // Log it fire-and-forget to runs/profile-pin.log, where it survives wipes;
    // logging must NEVER break the render/arm flow. The arm is reported as-is,
    // not hardcoded: if the gate above ever regressed to arm-blind, the log
    // would still show arm=off.
    try {
      fetch(`${c.base_url}/api/profiles/pin-log`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ event: "profile_pin", from, to: subject, arm: ui.sel.arm }),
      }).catch(() => {});
    } catch {
      // Best-effort by design: a failed log never blocks a render or an arm.
    }
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
  // A CLOUD CELL DOES NOT NEED THE PROXY ROSTER. `eligible` is the local relay's
  // model list, and gating a cloud launch on it would make the entire cloud
  // branch dead whenever the local proxy is down — which is precisely the
  // situation in which an operator would reach for a cloud baseline.
  const rosterOk = ui.sel.kind === "cloud" || eligible.length > 0;
  const complete = armChosen && orgOk && Boolean(ui.sel.model) && rosterOk;

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

      ${substrateLine()}
      ${form(eligible, roster, live, subject)}
      ${ui.armed && ui.restatement ? `<div class="restate">${esc(ui.restatement)}</div>` : ""}
      ${live && ui.armed ? abandonWarning(run) : ""}
      ${refusal()}
      ${button(complete, live, blockedNoBaseline, starting, baselineWrongModel)}
      ${why(run, armChosen, orgOk, eligible, blockedNoBaseline, live, { baselineWrongModel, baselineModel, subject })}
      ${resumeNote(caps)}
    </div>`;
}

/**
 * THE SUBSTRATE, ON THE CONTROL THAT STARTS THE CELL.
 *
 * A cloud cell is billed and a local one is not, and that is the single largest
 * difference between two runs that are otherwise identical in every field on
 * this form. The server states it in its own restatement too; this states it
 * BEFORE the arm, so the operator is not relying on reading the restatement
 * carefully at the moment they have already decided to click.
 */
function substrateLine() {
  if (ui.sel.kind !== "cloud") return "";
  return `<div class="rcloud">${esc(
    "CLOUD CELL — routed to the vendor and BILLED. The key is resolved by the control plane; this browser never holds it.",
  )}</div>`;
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
  //
  // A CLOUD MODEL IS ALWAYS PINNED, whether or not a profile froze it. It is not
  // in the proxy roster, so the dropdown below cannot contain it — offering that
  // dropdown would silently discard the operator's choice and arm a local cell
  // for whatever they picked instead.
  const cloudPinned = ui.sel.kind === "cloud" && ui.sel.model;
  const pin = subject ?? (cloudPinned ? ui.sel.model : null);
  const modelRow = pin
    ? `<div class="rrow">
         <label>MODEL</label>
         <span class="rpin">${esc(pin)}<span class="rpin-why">${esc(
           subject
             ? "subject frozen by the profile — both arms are this model"
             : "chosen on the cloud branch — not a proxy alias, so it is stated rather than selected",
         )}</span></span>
       </div>`
    : `<div class="rrow">
         <label>MODEL</label>
         <select class="rsel" data-run-sel="model">
           <option value="">choose…</option>
           ${modelOpts}
         </select>
       </div>`;

  // NO CONTEXT PICKER. Every bench alias is pinned at its own 256k-class
  // ceiling in the proxy's models.yaml and the worker registry, so the picker
  // offered three choices of which one was correct and two silently shortened
  // the window the cell was supposed to measure at. Omitting `context` from the
  // payload is a first-class case in the server: it skips the
  // WEVIBE_BENCH_WORKER_NUM_CTX override entirely and the registry default —
  // the pinned ceiling — applies (server.mjs:650). The field is gone rather
  // than defaulted-and-hidden: a control that can only be set to one correct
  // value is not a choice, it is a trap.

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
  // Only a LOCAL cell needs the proxy roster to have something in it. Saying
  // this about a cloud cell would name a cause that has no bearing on it and
  // send the operator to restart a proxy the run does not use.
  if (!eligible.length && ui.sel.kind !== "cloud") {
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
