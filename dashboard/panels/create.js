// ─────────────────────────────────────────────────────────────────────────────
// PANEL: [+ PROFILE] — one chooser, then a three-step sequence on either branch
//
// ── WHAT THIS REPLACED ──────────────────────────────────────────────────────
//
// Two dialogs and a scattering of per-row buttons:
//
//   [+ baseline] on a model row → a one-card "are you sure?" (runstart.js)
//   [+ profile]  on a model row → a single tall form asking both frozen facts
//                                 at once (profile.js)
//
// Both are gone. They asked the operator to have already decided which of two
// different acts they wanted BEFORE the surface explained the difference, and
// they put up to a dozen controls on a card whose primary job is to be read.
//
// ── ONE ENTRY, THEN THE FORK ────────────────────────────────────────────────
//
// The card carries a single [+ PROFILE]. Its first frame asks which act is
// meant, and the disabled card STATES ITS REQUIREMENT ON ITSELF: on a cold
// install "create benchmark profile" is dead because there is no baseline to
// measure against, and saying so on the card is the only way an operator learns
// the ordering the bench actually enforces. A greyed control with no sentence on
// it teaches nothing and reads as broken.
//
// ── NOTHING COMMITS BEFORE THE LAST CARD ────────────────────────────────────
//
// Forward advances, back retreats, escape closes, and every frame before the
// third is pure selection held in this module. That is not politeness: the
// baseline branch ends in a cell that costs hours and — on the cloud branch —
// money, and the profile branch ends in a file that is frozen forever and can
// never be edited. Both are worth three deliberate clicks.
//
// AND THE LAST CARD IS NOT THE LAST CONFIRMATION ON THE BASELINE BRANCH.
// CONTINUE arms the run through the server's preview, which mints a token and
// returns its own restatement; the run control then shows THE SERVER'S WORDS and
// takes the final click. So the words the operator reads before a cell starts
// are the words the server will act on, never this panel's summary of them.
//
// ── THE MEMORY ROSTER IS STILL DECLARED, NOT ENFORCED ───────────────────────
//
// Frame PROFILE·2 asks whose memories the profile may read, and the answer is
// frozen to disk and applied to nothing. No recall request carries a producer
// allowlist — `producer_model_id` is written to the payload and read back, and
// no consumer filters on it. The debt vocabulary is IMPORTED from profile.js and
// rendered on that frame rather than re-worded here, because two phrasings of
// "declared, not enforced" are two claims and the operator cannot know they mean
// the same thing.
// ─────────────────────────────────────────────────────────────────────────────

import { esc } from "../board.js";
import { debt, debtBlock, transferBlock } from "./profile.js";

/**
 * THE WHOLE FLOW'S STATE, in one object.
 *
 * Module-local and never on the board payload: it describes what the OPERATOR is
 * part-way through choosing, which no poll can know and every poll would erase.
 */
const ui = {
  open: false,
  // chooser → b1 → b2 → b3   |   chooser → p1 → p2 → p3
  step: "chooser",
  branch: null,

  // ── baseline branch ──
  kind: null,        // "local" | "cloud"
  model: null,       // the id from `startable`
  query: "",
  provider: "all",

  // ── profile branch ──
  baseline: null,    // the baseline id the profile is measured against
  memory: new Set(), // producer models whose memories it may read

  // The freeze POST's own state. `pending` and `refusal` are never conflated:
  // "working" and "refused" are different facts and a surface that shows one for
  // the other sends the operator to fix the wrong thing.
  pending: false,
  refusal: null,
};

export function openCreate() {
  ui.open = true;
  ui.step = "chooser";
  ui.branch = null;
  ui.kind = null;
  ui.model = null;
  ui.query = "";
  ui.provider = "all";
  ui.baseline = null;
  ui.memory = new Set();
  ui.pending = false;
  ui.refusal = null;
}

export function closeCreate() {
  ui.open = false;
  ui.pending = false;
  ui.refusal = null;
}

export function isCreateOpen() {
  return ui.open;
}

export function createStep() {
  return ui.step;
}

/** What the freeze POST needs. Read by board.js, which owns every fetch. */
export function createSelection() {
  return {
    branch: ui.branch,
    kind: ui.kind,
    model: ui.model,
    baseline: ui.baseline,
    memory: [...ui.memory],
  };
}

export function setCreatePending(v) {
  ui.pending = Boolean(v);
  if (ui.pending) ui.refusal = null;
}

export function setCreateRefusal(code, reason) {
  ui.refusal = { code: code ?? null, reason: reason ?? null };
  ui.pending = false;
}

// ── NAVIGATION ──────────────────────────────────────────────────────────────
//
// THE ORDER IS FIXED AND SHORT. Two sequences of three, and `back` on the first
// step of either returns to the chooser rather than closing — an operator who
// picked the wrong branch must be able to correct it without losing the dialog.

const NEXT = { chooser: null, b1: "b2", b2: "b3", b3: null, p1: "p2", p2: "p3", p3: null };
const BACK = { b1: "chooser", b2: "b1", b3: "b2", p1: "chooser", p2: "p1", p3: "p2" };

export function chooseBranch(branch) {
  ui.branch = branch;
  ui.step = branch === "baseline" ? "b1" : "p1";
  ui.refusal = null;
}

export function createForward() {
  const next = NEXT[ui.step];
  if (next) ui.step = next;
}

export function createBack() {
  const prev = BACK[ui.step];
  if (!prev) return;
  ui.step = prev;
  // Returning to the chooser abandons the branch, so the branch's own choices
  // go with it. Keeping them would silently pre-fill a sequence the operator
  // has just backed out of.
  if (prev === "chooser") {
    ui.branch = null;
    ui.kind = null;
    ui.model = null;
    ui.baseline = null;
    ui.memory = new Set();
  }
  ui.refusal = null;
}

export function setCreateKind(kind) {
  ui.kind = kind;
  // The model list is filtered by substrate, so a model chosen on one substrate
  // is meaningless on the other and must not survive the change.
  ui.model = null;
  ui.provider = "all";
}

export function setCreateModel(id) {
  ui.model = ui.model === id ? null : id;
}

export function setCreateQuery(q) {
  ui.query = String(q ?? "");
}

export function setCreateProvider(p) {
  ui.provider = String(p ?? "all");
}

export function setCreateBaseline(id) {
  ui.baseline = ui.baseline === id ? null : id;
}

export function toggleCreateMemory(id) {
  if (ui.memory.has(id)) ui.memory.delete(id);
  else ui.memory.add(id);
}

// ── RENDER ──────────────────────────────────────────────────────────────────

export function renderCreate(board) {
  if (!ui.open) return "";
  const ledger = board.models_ledger ?? null;

  return `
    <div class="modal-scrim" data-create-scrim="1">
      <div class="modal cmodal" role="dialog" aria-modal="true" aria-label="Add a baseline or a profile">
        ${frame(board, ledger)}
      </div>
    </div>`;
}

function frame(board, ledger) {
  switch (ui.step) {
    case "chooser": return chooser(ledger);
    case "b1": return baselineKind(ledger);
    case "b2": return baselineModel(ledger);
    case "b3": return baselineConfirm(ledger);
    case "p1": return profileBaseline(ledger);
    case "p2": return profileMemory(board, ledger);
    case "p3": return profileConfirm(board, ledger);
    default: return chooser(ledger);
  }
}

/**
 * THE FRAME SHELL — step label, branch, title, body, note, and the two controls.
 *
 * Every frame is drawn through here so the sequence cannot develop a different
 * geometry, a different back-affordance or a different place for its CTA
 * depending on which branch an operator took.
 */
function shell({ step, branch, title, body, note, back = "‹ back", cta, ctaAttr, ctaOk = true, final = false }) {
  return `
    <div class="cframe${final ? " final" : ""}">
      <div class="chead">
        <span class="cstep">${esc(step)}</span>
        <span class="cbranch">${esc(branch)}</span>
        <span class="spacer"></span>
        <button class="cclose" data-create-cancel="1" aria-label="close">esc</button>
      </div>
      <span class="ctitle">${esc(title)}</span>
      <div class="cbody">${body}</div>
      ${note ? `<span class="cnote">${esc(note)}</span>` : ""}
      ${refusalBlock()}
      <div class="cfoot">
        ${back ? `<button class="cback" data-create-back="1">${esc(back)}</button>` : `<span></span>`}
        <span class="spacer"></span>
        ${cta
          ? `<button class="cta${final ? " final" : ""}" ${ctaAttr} ${ctaOk && !ui.pending ? "" : "disabled"}>${esc(ui.pending ? "…" : cta)}</button>`
          : ""}
      </div>
    </div>`;
}

/**
 * THE REFUSAL, IN THE OPERATOR'S FACE, directly above the control that caused
 * it — the answer appears where the question was asked.
 *
 * Both halves are shown and they are different things: `code` is the machine
 * reason, greppable and the thing to quote in a report; `reason` is the server's
 * own prose, verbatim and never paraphrased here. A paraphrase would be a SECOND
 * definition of why the server refused, free to drift from the first.
 */
function refusalBlock() {
  if (!ui.refusal) return "";
  const transport = ui.refusal.code === "transport_failed";
  return `
    <div class="freeze-refusal" role="alert">
      <span class="fr-head">${esc(transport ? "COULD NOT REACH THE CONTROL PLANE" : "THE CONTROL PLANE REFUSED THIS")}</span>
      <span class="fr-code">${esc(ui.refusal.code ?? "no code given")}</span>
      <span class="fr-reason">${esc(ui.refusal.reason ?? "no reason given")}</span>
      <span class="fr-note">${esc(transport
        ? "Nothing was written. The request never arrived — check that the control plane is running, then try again."
        : "Nothing was written. Change a choice above and try again — the store is unchanged.")}</span>
    </div>`;
}

/**
 * One selectable line. `kind` drives the whole visual grammar:
 *   on    picked
 *   off   pickable, not picked
 *   dead  refused, and the refusal is ON the line as its meta text
 *   ghost a non-control (a search box, a statement of fact)
 */
function line({ glyph, text, meta, kind = "off", attr = "" }) {
  return `
    <div class="cline ${esc(kind)}" ${attr}>
      <span class="cglyph">${esc(glyph)}</span>
      <span class="ctext">
        <span class="ct">${esc(text)}</span>
        ${meta ? `<span class="cm">${esc(meta)}</span>` : ""}
      </span>
    </div>`;
}

// ── MODAL · 0 — the chooser ─────────────────────────────────────────────────

function chooser(ledger) {
  const rows = ledger?.baseline_rows ?? [];
  const usable = rows.filter((b) => b.scorable);
  const canProfile = usable.length > 0;

  const body = `
    ${line({
      glyph: "▸",
      text: "Start new baseline",
      meta: "always available — a floor is measured against nothing",
      kind: ui.branch === "baseline" ? "on" : "off",
      attr: `data-create-branch="baseline"`,
    })}
    ${canProfile
      ? line({
          glyph: "▸",
          text: "Create benchmark profile",
          meta: `${usable.length} baseline${usable.length === 1 ? "" : "s"} to measure against`,
          kind: ui.branch === "profile" ? "on" : "off",
          attr: `data-create-branch="profile"`,
        })
      : line({
          glyph: "✕",
          text: "Create benchmark profile",
          // THE REQUIREMENT IS ON THE CARD. A disabled control that does not say
          // what it is waiting for is indistinguishable from a broken one, and
          // this particular requirement IS the workflow: nothing can be profiled
          // until something has been measured.
          meta: rows.length
            ? "disabled — no baseline is complete and valid yet"
            : "disabled — 0 baselines exist",
          kind: "dead",
        })}`;

  return shell({
    step: "MODAL · 0",
    branch: "chooser",
    title: "What are you adding?",
    body,
    note: "A baseline is one OFF cell: the floor. A profile is an allowlist frozen against one baseline. Neither starts the other.",
    back: null,
    cta: "→",
    ctaAttr: `data-create-next="1"`,
    ctaOk: Boolean(ui.branch),
  });
}

// ── BASELINE · 1 — local or cloud ───────────────────────────────────────────

function baselineKind(ledger) {
  const cloud = ledger?.cloud ?? null;
  const local = (ledger?.startable ?? []).filter((m) => m.kind === "local");
  const cloudModels = (ledger?.startable ?? []).filter((m) => m.kind === "cloud");

  const body = `
    ${line({
      glyph: ui.kind === "local" ? "●" : "○",
      text: "Local baseline",
      meta: `${local.length} bench alias${local.length === 1 ? "" : "es"} behind the relay proxy · unbilled`,
      kind: ui.kind === "local" ? "on" : "off",
      attr: `data-create-kind="local"`,
    })}
    ${cloudReady(cloud)
      ? line({
          glyph: ui.kind === "cloud" ? "●" : "○",
          text: "Cloud API baseline",
          meta: `${(cloud?.providers ?? []).length} providers · ${cloudModels.length} models · BILLED, ceiling $${Number(cloud?.spend_ceiling_usd ?? 0).toFixed(2)} per cell`,
          kind: ui.kind === "cloud" ? "on" : "off",
          attr: `data-create-kind="cloud"`,
        })
      : line({
          glyph: "✕",
          text: "Cloud API baseline",
          meta: cloud?.can_start_reason ?? "the control plane reports no cloud capability",
          kind: "dead",
        })}`;

  return shell({
    step: "BASELINE · 1",
    branch: "start new baseline",
    title: "Local or cloud?",
    body,
    note: "Cloud routes the cell straight at the vendor and is billed against a per-cell ceiling; local runs on the resident model and is not. Nothing else about the measurement changes.",
    cta: "→",
    ctaAttr: `data-create-next="1"`,
    ctaOk: Boolean(ui.kind),
  });
}

function cloudReady(cloud) {
  return Boolean(cloud) && cloud.can_start === true;
}

// ── BASELINE · 2 — the model under test ─────────────────────────────────────

function baselineModel(ledger) {
  const all = (ledger?.startable ?? []).filter((m) => m.kind === ui.kind);
  const providers = [...new Set(all.map((m) => m.provider).filter(Boolean))];

  const q = ui.query.trim().toLowerCase();
  const shown = all.filter((m) => {
    if (ui.provider !== "all" && m.provider !== ui.provider) return false;
    if (!q) return true;
    return `${m.id} ${m.label ?? ""}`.toLowerCase().includes(q);
  });

  const body = `
    <div class="cfilters">
      <input class="csearch" data-create-query="1" value="${esc(ui.query)}" placeholder="search models…" aria-label="search models">
      ${providers.length > 1
        ? `<select class="cprov" data-create-provider="1" aria-label="filter by provider">
             <option value="all"${ui.provider === "all" ? " selected" : ""}>provider — all</option>
             ${providers.map((p) => `<option value="${esc(p)}"${ui.provider === p ? " selected" : ""}>${esc(p)}</option>`).join("")}
           </select>`
        : ""}
      <span class="cshown">${esc(`${shown.length} shown`)}</span>
    </div>
    <div class="clist">
      ${shown.length
        ? shown.map(modelLine).join("")
        : `<div class="null">${esc(all.length ? "no model matches this filter" : "no model is available on this substrate")}</div>`}
    </div>
    ${ui.kind === "cloud" ? keyLine(ledger?.cloud ?? null) : ""}`;

  return shell({
    step: "BASELINE · 2",
    branch: "start new baseline",
    title: "Pick the model under test",
    body,
    note: "One model per baseline. A model that already has a valid floor cannot start another — re-baselining is a declared act (archive the run), not a button.",
    cta: "→",
    ctaAttr: `data-create-next="1"`,
    ctaOk: Boolean(ui.model),
  });
}

/**
 * ONE MODEL. A model that cannot be baselined is drawn dead WITH ITS REASON,
 * never hidden.
 *
 * Hiding it would answer the operator's actual question — "where is the model I
 * wanted" — with silence, and the most common reason (it already has a floor) is
 * the one they most need to see, because it means the thing they wanted is
 * already done.
 */
function modelLine(m) {
  const ok = m.can_baseline?.allowed === true;
  const on = ui.model === m.id;
  const meta = ok
    ? [m.provider, m.resident === true ? "resident" : m.resident === false ? "not resident — loads on first request" : null]
        .filter(Boolean)
        .join(" · ")
    : (m.can_baseline?.reason ?? "not available");

  return line({
    glyph: ok ? (on ? "●" : "○") : "✕",
    text: m.id,
    meta,
    kind: ok ? (on ? "on" : "off") : "dead",
    attr: ok ? `data-create-model="${esc(m.id)}"` : "",
  });
}

/**
 * THE KEY, REPORTED — never requested.
 *
 * There is no field here and there must never be one. The credential is resolved
 * server-side from the same places the harness reads (the environment, then
 * `config/cloud.env`), and what reaches this browser is presence, source and an
 * eight-character fingerprint. A key typed into this modal would live in page
 * memory, in a POST body and in the browser's autofill store, to configure a
 * file that already sits on the same disk as the service that reads it.
 */
function keyLine(cloud) {
  const k = cloud?.key ?? null;
  if (!k) return "";
  return `
    <div class="ckey ${k.present ? "on" : "bad"}">
      <span class="ckey-head">${esc(k.present ? "API KEY RESOLVES" : "NO API KEY")}</span>
      <span class="ckey-body">${esc(
        k.present
          ? `read from ${k.source === "environment" ? "the control plane's environment" : k.source_detail ?? "the key file"} · fingerprint ${k.fingerprint}`
          : (k.reason ?? "no key could be resolved"),
      )}</span>
      <span class="ckey-note">${esc(
        "Resolved by the control plane and never sent to this browser. There is no field here to type one into.",
      )}</span>
    </div>`;
}

// ── BASELINE · 3 — confirm ──────────────────────────────────────────────────

function baselineConfirm(ledger) {
  const m = (ledger?.startable ?? []).find((x) => x.id === ui.model) ?? null;
  const cloud = ledger?.cloud ?? null;
  const isCloud = ui.kind === "cloud";

  const body = `
    ${line({ glyph: "✓", text: isCloud ? `cloud · ${m?.provider ?? "vendor"}` : "local · relay proxy", meta: isCloud ? (m?.slug ?? "") : "lm studio", kind: "on" })}
    ${line({ glyph: "✓", text: ui.model ?? "no model", meta: m?.label ?? "", kind: "on" })}
    ${line({ glyph: "✓", text: "CONTROL cell — memory off", meta: "this IS the floor; it is measured against nothing", kind: "on" })}
    ${isCloud
      ? line({
          glyph: "$",
          text: `billed · ceiling $${Number(cloud?.spend_ceiling_usd ?? 0).toFixed(2)} for this cell`,
          meta: cloud?.spend_note ?? "",
          kind: "off",
        })
      : ""}
    ${line({ glyph: "·", text: "no profile is created here", meta: "profiles are the other branch", kind: "ghost" })}`;

  return shell({
    step: "BASELINE · 3",
    branch: "start new baseline",
    title: "Confirm",
    body,
    // WHAT CONTINUE ACTUALLY DOES, stated exactly. It does not launch: it asks
    // the server to validate these parameters and mint a token, and the server's
    // own restatement then takes the final click. An operator told "this starts
    // a run" who then sees another dialog learns the surface is imprecise.
    note: "Continue arms the cell and shows the control plane's own restatement for a final confirmation — it does not launch yet. A pending row appears on the card behind this modal.",
    cta: "CONTINUE →",
    ctaAttr: `data-create-baseline-continue="1"`,
    ctaOk: Boolean(ui.model),
    final: true,
  });
}

// ── PROFILE · 1 — which baseline ────────────────────────────────────────────

function profileBaseline(ledger) {
  const rows = ledger?.baseline_rows ?? [];

  const body = rows.length
    ? rows.map(baselineLine).join("")
    : `<div class="null">${esc("no baseline exists — start one on the other branch first")}</div>`;

  return shell({
    step: "PROFILE · 1",
    branch: "create benchmark profile",
    title: "Measure against which baseline?",
    body,
    note: "Only closed, non-void baselines are selectable — a running one has no total to compare against, and a void one produced numbers that measure the harness.",
    cta: "→",
    ctaAttr: `data-create-next="1"`,
    ctaOk: Boolean(ui.baseline),
  });
}

function baselineLine(b) {
  const ok = b.scorable === true;
  const on = ui.baseline === b.id;
  const meta = ok
    ? `${b.model} · ${b.turns ?? "?"} turns${b.gates?.total ? ` · ${b.gates.passed ?? b.gates.total - (b.gates.failed ?? 0)}/${b.gates.total} gates` : ""}`
    : b.state === "running"
      ? "still running — no total yet"
      : (b.reason ?? "not a valid floor");

  return line({
    glyph: ok ? (on ? "●" : "○") : "✕",
    text: `${b.id} · ${b.model}`,
    meta,
    kind: ok ? (on ? "on" : "off") : "dead",
    attr: ok ? `data-create-baseline="${esc(b.id)}"` : "",
  });
}

// ── PROFILE · 2 — the memory roster ─────────────────────────────────────────

/**
 * THE EXPERIMENT VARIABLE, and the one frame that carries the debt.
 *
 * THE LIST SPANS BOTH SUBSTRATES AND IS NOT FILTERED TO BENCH-ELIGIBLE MODELS.
 * A memory's producer is whatever model authored it, which may be a model this
 * machine no longer serves — an interactive alias, or one retired from the
 * bench. Restricting the roster the way the subject picker is restricted would
 * make a historical corpus unqualifiable.
 *
 * THE SUBJECT IS NOT TICKED FOR THE OPERATOR. A profile that qualifies only
 * FOREIGN memories is a legitimate and interesting experiment — the subject
 * recalls nothing it wrote itself — and pre-ticking would convert it into a
 * mixed one behind their back.
 */
function profileMemory(board, ledger) {
  const producers = memoryProducers(board, ledger);
  const q = ui.query.trim().toLowerCase();
  const shown = q ? producers.filter((p) => p.id.toLowerCase().includes(q)) : producers;

  const body = `
    <div class="cfilters">
      <input class="csearch" data-create-query="1" value="${esc(ui.query)}" placeholder="search models…" aria-label="search models">
      <span class="cshown">${esc(`${ui.memory.size} ticked · ${shown.length} shown`)}</span>
    </div>
    <div class="clist">
      ${shown.length
        ? shown.map(memoryLine).join("")
        : `<div class="null">${esc(producers.length ? "no model matches this filter" : "no producer model could be listed — the roster is unreachable")}</div>`}
    </div>
    ${debt()}
    <div class="cdebt">${esc(
      "Recall cannot filter by producing model today. Every ON run under this profile retrieves from the whole corpus, "
      + "including every model left unticked. Freezing the allowlist records the policy; it does not apply it.",
    )}</div>`;

  return shell({
    step: "PROFILE · 2",
    branch: "create benchmark profile",
    title: "Whose memories may this profile read?",
    body,
    note: "Multi-select, spanning local and cloud. At least one is required — an empty roster states a policy that admits nothing, which with enforcement absent would read as no filter at all.",
    cta: "→",
    ctaAttr: `data-create-next="1"`,
    ctaOk: ui.memory.size > 0,
  });
}

/**
 * EVERY MODEL THAT COULD HAVE AUTHORED A MEMORY — the proxy roster plus the
 * cloud catalogue, de-duplicated.
 *
 * Two sources, one list, and neither is treated as authoritative over the other:
 * a corpus on this machine may contain entries from both.
 */
function memoryProducers(board, ledger) {
  const out = new Map();
  for (const m of board.control?.roster?.models ?? []) {
    const id = typeof m === "string" ? m : m?.id;
    if (!id) continue;
    out.set(id, {
      id,
      where: "local",
      memories: typeof m === "object" && Number.isFinite(m.memories) ? m.memories : null,
      retired: typeof m === "object" ? (m.retired_reason ?? null) : null,
    });
  }
  for (const m of ledger?.cloud?.models ?? []) {
    if (!m?.key || out.has(m.key)) continue;
    out.set(m.key, { id: m.key, where: m.provider ?? "cloud", memories: null, retired: null });
  }
  return [...out.values()];
}

function memoryLine(p) {
  const on = ui.memory.has(p.id);
  const meta = [
    p.where,
    p.memories === null
      ? "memory count unobserved"
      : p.memories === 0
        ? "0 memories — contributes nothing yet"
        : `${p.memories.toLocaleString()} memories in corpus`,
    p.retired ? "retired from the bench" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return line({
    glyph: on ? "✓" : "○",
    text: p.id,
    meta,
    kind: on ? "on" : "off",
    attr: `data-create-memory="${esc(p.id)}"`,
  });
}

// ── PROFILE · 3 — confirm ───────────────────────────────────────────────────

function profileConfirm(board, ledger) {
  const b = (ledger?.baseline_rows ?? []).find((x) => x.id === ui.baseline) ?? null;
  const subject = b?.model ?? null;
  const roster = [...ui.memory];
  const producers = memoryProducers(board, ledger);

  // THE TRANSFER EDGE IS SHOWN AS A CONSEQUENCE, NEVER AS A CONTROL. It is
  // inferred from the subject and the roster the operator already chose — there
  // is no direction picker anywhere on this surface and there must never be one,
  // because an operator who could label their own experiment "weaker → greater"
  // could label it wrongly, and the label would outlive the run in a frozen file.
  const t = inferTransfer(subject, roster);

  const body = `
    ${line({ glyph: "✓", text: `against ${ui.baseline ?? "—"}`, meta: b ? `${b.model} · ${b.turns ?? "?"} turns` : "", kind: "on" })}
    ${line({ glyph: "✓", text: `subject — ${subject ?? "—"}`, meta: "OFF and ON are both this model; run start refuses any other", kind: "on" })}
    ${line({ glyph: "✓", text: `${roster.length} source model${roster.length === 1 ? "" : "s"}`, meta: `of ${producers.length} · ${roster.join(", ")}`, kind: "on" })}
    ${line({ glyph: "!", text: "the allowlist freezes on accept", meta: "a profile is never rewritten — changing it means freezing a new one", kind: "off" })}
    ${line({ glyph: "·", text: "no run starts here", meta: "runs start from + run on the profile row", kind: "ghost" })}
    ${transferBlock(t)}
    ${debtBlock()}`;

  return shell({
    step: "PROFILE · 3",
    branch: "create benchmark profile",
    title: "Confirm",
    body,
    note: "Accept writes the profile under its baseline and does nothing else — it does not arm a cell, open a session, or attach a TUI.",
    cta: "ACCEPT",
    ctaAttr: `data-create-accept="1"`,
    ctaOk: Boolean(subject) && roster.length > 0,
    final: true,
  });
}

/**
 * MIRROR of `transferOf()` in control/profiles.mjs, for the PRE-FREEZE preview
 * only. Once frozen, the edge comes from the service and the card's drawer
 * renders THAT — so any drift shows up immediately as the preview disagreeing
 * with the drawer for the same pair of choices.
 */
function inferTransfer(subjectId, roster) {
  if (!subjectId || !roster.length) return null;
  const foreign = roster.filter((m) => m !== subjectId);
  const self = roster.includes(subjectId);
  if (!foreign.length) return { kind: "self", direction: "same", self: true, foreign: [] };
  return { kind: self ? "mixed" : "cross", direction: "unranked", self, foreign };
}
