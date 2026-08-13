// ─────────────────────────────────────────────────────────────────────────────
// PANEL: EXTRACTION — unified queue, process, and dedup decision view
//
// Replaces the per-profile extraction column that lived inside the LIVE RUN
// panel (live.js). That column could only ever show ONE extraction — whatever
// the control plane held in memory — and it was wedged into a 380px third
// column of a panel about something else. It also vanished from view exactly
// when a run was interesting, which is the operator's ask 4.
//
// ── THREE VIEWS, ONE POPOUT ─────────────────────────────────────────────────
//   PROCESS  the 10-stage machine for the CURRENT extraction
//   QUEUE    every extraction this machine has run — the unified view
//   DEDUP    near-duplicate decisions + the score distribution
//
// ── NOT SCOPED TO THE LOADED PROFILE. THIS IS THE POINT ─────────────────────
// A profile is a READ filter over one stack. Extraction is a WRITE into one
// shared corpus, and the sessions feeding it come from bench cells and ad-hoc
// editor work alike. Scoping this queue to `board.profile` would hide rows that
// are mutating the very corpus the loaded profile reads from — the operator
// would be watching a filtered view of a global side effect. Nothing in this
// file reads `board.profile`, deliberately.
//
// ── UNIFIED VIEW, SERIAL EXECUTION ──────────────────────────────────────────
// The design draws four concurrent extractions. The control plane permits ONE,
// on purpose: two concurrent extractions against one org interleave submissions
// into the shared corpus (control/extraction.mjs:106). That is a corpus
// integrity property, not a limitation to route around, so this panel presents
// a unified VIEW over serial EXECUTION and never implies parallelism.
//
// ── `complete`, NEVER `done` ────────────────────────────────────────────────
// A prior drift bug keyed CSS on `done` while the emitter writes `complete`;
// every finished stage rendered blank and the counter read 0/10. Stage keys
// come from STAGE_STATES and are asserted by control.test.mjs.
//
// ── TWO SOURCES, AND THE SEAM BETWEEN THEM IS EXPLICIT ──────────────────────
// `board.extraction`       — the control plane's LIVE tracker. Freshest for the
//                            running job; memory-resident; dies with the process.
// `board.extraction_queue` — the telemetry DB. Every job ever; only updated at
//                            a stage transition.
// The running job is therefore read from the LIVE source and history from the
// DB. They are never merged into one number, because a disagreement between
// them is real information (it means the control plane restarted).
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, dur, tok, clip } from "../board.js";
import { renderPopout, isPopoutOpen } from "./popout.js";

/** Stage vocabulary — must match control/contract.mjs STAGE_STATES exactly. */
export const STAGE_STATES = ["pending", "running", "complete", "failed", "gated"];
const STAGE_GLYPH = { pending: "·", running: "›", complete: "✓", failed: "!", gated: "⊘" };

/** The declared pipeline. Mirrors control/contract.mjs EXTRACT_STAGES. */
export const EXTRACT_STAGES = [
  "init", "substrate", "identity", "preflight", "orchestrator",
  "org_resolve", "extract", "submit", "approve", "prove_delivery",
];

export const POPOUT_ID = "extraction";
const VIEWS = ["process", "queue", "dedup"];
let view = "queue";

export function setExtractView(v) {
  if (VIEWS.includes(v)) view = v;
}
export function extractView() {
  return view;
}

export function renderExtraction(board) {
  const live = board.extraction ?? null;
  const q = board.extraction_queue ?? null;
  const d = board.dedup ?? null;

  const body =
    view === "process" ? processView(live)
    : view === "dedup" ? dedupView(d)
    : queueView(q, live);

  return renderPopout({
    id: POPOUT_ID,
    brand: "EXTRACTION",
    // The dock bar states what is happening WITHOUT being expanded.
    status: dockStatus(live, q),
    title: "MEMORY EXTRACTION",
    note: scopeNote(q),
    tag: "READ-ONLY — REPORTS, NEVER STARTS",
    width: 1180,
    dockIndex: 1, // the TUI dock owns slot 0
    tabs: [
      { id: "process", label: "PROCESS", active: view === "process", count: null },
      { id: "queue", label: "QUEUE", active: view === "queue", count: q?.jobs?.length ?? null },
      { id: "dedup", label: "DEDUP", active: view === "dedup", count: d?.flagged?.length ?? null },
    ],
    body,
    foot:
      "one shared corpus · extraction is not scoped to the loaded profile · " +
      "the queue is a VIEW; execution stays serial",
  });
}

/** Is the popout currently expanded? Used by the board to skip work. */
export function isExtractionOpen() {
  return isPopoutOpen(POPOUT_ID);
}

// ── DOCK STATUS ─────────────────────────────────────────────────────────────
// Rule 1 of the popout shell: always say what it is doing.

function dockStatus(live, q) {
  if (live && live.state === "running") {
    const done = (live.stages ?? []).filter((s) => s.state === "complete").length;
    const total = (live.stages ?? []).length || EXTRACT_STAGES.length;
    return `<span class="bright">extracting</span> · ${esc(String(done))}/${esc(String(total))}`;
  }
  if (live && live.state === "gated") return `<span class="danger">gated</span> · refused`;
  if (live && live.state === "failed") return `<span class="danger">failed</span>`;
  if (q?.total_jobs) {
    const failed = q.failed_jobs ?? 0;
    return `idle · ${esc(String(q.total_jobs))} run${q.total_jobs === 1 ? "" : "s"}${failed ? ` · <span class="danger">${esc(String(failed))} failed</span>` : ""}`;
  }
  return `<span class="null">nothing extracted yet</span>`;
}

function scopeNote(q) {
  if (!q) return "every session on this machine · not scoped to the loaded profile";
  return `${q.total_jobs ?? 0} extraction${q.total_jobs === 1 ? "" : "s"} on this machine · not scoped to the loaded profile · corpus writes are global`;
}

// ── VIEW 1: PROCESS ─────────────────────────────────────────────────────────
// The 10-stage machine for the CURRENT extraction, from the live tracker.

function processView(x) {
  if (!x) {
    return pad(
      "control plane not enabled — the live stage machine is opt-in and currently off. " +
        "Past extractions are still readable in QUEUE, which reads the telemetry DB on disk.",
    );
  }

  const stages = Array.isArray(x.stages) ? x.stages : [];
  if (!stages.length) return pad("extraction has not started — no stage has reported.");

  const complete = stages.filter((s) => s.state === "complete").length;
  const gated = stages.filter((s) => s.state === "gated").length;
  const failed = stages.filter((s) => s.state === "failed").length;

  const head = [`${complete}/${stages.length} complete`];
  if (gated) head.push(`${gated} gated`);
  if (failed) head.push(`${failed} failed`);

  return `
    <div class="xproc">
      <div class="phead">
        <span class="kick">CURRENT EXTRACTION</span>
        <span class="sub ${gated || failed ? "danger" : ""}">${esc(head.join(" · "))}</span>
        <span class="spacer"></span>
        <span class="note">${esc(procMeta(x))}</span>
      </div>
      <div class="stages">${stages.map(stageRow).join("")}</div>
      ${
        gated
          ? `<div class="note danger">${esc(
              "GATED is the instrument deliberately refusing — not a crash and not a pass. " +
                "A gated stage stops the ones after it; completed stages before it remain valid. " +
                "The substrate gate exists because SQLite corruption is PARTIAL: an unverified DB " +
                "does not fail extraction, it silently returns FEWER memories.",
            )}</div>`
          : ""
      }
      ${x.reason ? `<div class="note danger">${esc(clip(x.reason, 400))}</div>` : ""}
    </div>`;
}

function procMeta(x) {
  const bits = [];
  if (x.model) bits.push(clip(x.model, 34));
  if (x.elapsed_s !== null && x.elapsed_s !== undefined) bits.push(dur(x.elapsed_s));
  if (x.n_memories !== null && x.n_memories !== undefined) bits.push(`${x.n_memories} memories`);
  return bits.length ? bits.join(" · ") : "no metadata reported";
}

function stageRow(s) {
  const state = STAGE_STATES.includes(s.state) ? s.state : "pending";
  return `
    <div class="stage ${state}">
      <span class="sg">${STAGE_GLYPH[state]}</span>
      <span class="sn">${esc(s.title ?? s.id ?? "—")}</span>
      ${
        // A measured 0 is rendered as 0 — it is a real result, not absence.
        s.count === null || s.count === undefined
          ? ""
          : `<span class="scount">${esc(String(s.count))}</span>`
      }
      <span class="ss">${esc(state.toUpperCase())}</span>
    </div>`;
}

// ── VIEW 2: QUEUE — the unified inventory ───────────────────────────────────

function queueView(q, live) {
  if (!q) {
    return pad(
      "no extraction telemetry on disk yet. The DB is created by the first extraction " +
        "(wevibe-bench/data/extract/extraction-telemetry.db) — this is the state before " +
        "anything has been extracted, not a failure.",
    );
  }
  if (!q.jobs?.length) return pad("telemetry DB exists but carries no extraction rows.");

  const rows = q.jobs.map((j) => queueRow(j, live)).join("");

  return `
    <div class="xq">
      <div class="xq-cols">
        <span>SOURCE SESSION</span><span>ORIGIN</span><span>PRODUCER MODEL</span>
        <span>STAGE 1–10</span><span>WRITTEN</span><span>CORPUS</span><span>STATE</span>
      </div>
      ${rows}
      ${q.returned < (q.total_jobs ?? 0)
        ? `<div class="note">${esc(`showing the ${q.returned} most recent of ${q.total_jobs} — older rows are trimmed from the view, not deleted from the DB`)}</div>`
        : ""}
      ${unwiredOrigins()}
    </div>`;
}

/**
 * A queue row.
 *
 * The RUNNING job is identified by the live tracker, not by the DB: a row is
 * written at stage transitions, so between them the DB says "running" for a
 * job that may already have finished. Where the two disagree the live source
 * wins for state, and that seam is deliberate.
 */
function queueRow(j, live) {
  const running = live?.state === "running" && live?.model && j.status === null;
  const state = rowState(j, running);

  // Pips count stages that reached a terminal state. Derived from the stage
  // rows themselves — never from a percentage, which would let the bar imply
  // progress the data does not carry.
  const terminal = new Set(
    (j.stages ?? []).filter((s) => ["complete", "failed", "gated"].includes(s.state)).map((s) => s.stage),
  );
  const gatedStage = (j.stages ?? []).find((s) => s.state === "gated");
  const failedStage = (j.stages ?? []).find((s) => s.state === "failed");

  const pips = EXTRACT_STAGES.map((id) => {
    if (gatedStage?.stage === id) return `<span class="pip gated"></span>`;
    if (failedStage?.stage === id) return `<span class="pip failed"></span>`;
    if (terminal.has(id)) return `<span class="pip done"></span>`;
    if (running && !terminal.has(id)) return `<span class="pip todo"></span>`;
    return `<span class="pip todo"></span>`;
  }).join("");

  const stageLabel = gatedStage
    ? `${gatedStage.stage} — gated`
    : failedStage
      ? `${failedStage.stage} — failed`
      : `${terminal.size} of ${EXTRACT_STAGES.length}`;

  const wrote =
    j.tally?.candidates === null || j.tally?.candidates === undefined
      ? nul("unobserved")
      : j.tally.candidates === 0
        ? `<span class="muted">0 memories</span>`
        : `${esc(String(j.tally.candidates))} memories${j.tally.flagged ? ` · <span class="danger">${esc(String(j.tally.flagged))} flagged</span>` : ""}`;

  return `
    <div class="xq-row ${running ? "live" : ""}" data-k="xq-${esc(String(j.id))}">
      <span class="xq-sess">${j.session_id ? esc(clip(j.session_id, 18)) : `${esc(j.run_label ?? "—")}/${esc(j.source_mode ?? "—")}`}</span>
      <span class="xq-origin">${esc(j.origin ?? "—")}${j.source_mode ? esc(` · ${j.source_mode}`) : ""}</span>
      <span class="xq-model">${j.producer_model ? esc(clip(j.producer_model, 26)) : nul("unobserved")}</span>
      <span class="xq-stage"><span class="pips">${pips}</span><span class="xq-slabel">${esc(stageLabel)}</span></span>
      <span class="xq-wrote">${wrote}</span>
      <span class="xq-corpus">${j.org_id ? esc(clip(j.org_id, 14)) : nul("none")}</span>
      <span class="xq-state ${state.cls}">${esc(state.word)}</span>
    </div>`;
}

function rowState(j, running) {
  if (running) return { word: "EXTRACTING", cls: "run" };
  if ((j.stages ?? []).some((s) => s.state === "gated")) return { word: "GATED", cls: "gated" };
  if (j.status === "error") return { word: "FAILED", cls: "gated" };
  if (j.status === "ok" && j.tally?.candidates === 0) return { word: "ZERO", cls: "zero" };
  if (j.status === "ok") return { word: "WRITTEN", cls: "ok" };
  if (j.status === null || j.status === undefined) return { word: "PENDING", cls: "pending" };
  return { word: String(j.status).toUpperCase(), cls: "pending" };
}

/**
 * ORIGINS THIS MACHINE CANNOT OBSERVE.
 *
 * The design draws a "remote · mbp-2" row. No remote inventory exists, and
 * fabricating one would put a row on the board that corresponds to nothing.
 * Editor sessions are likewise not enumerated: the harness writes a telemetry
 * row, an ad-hoc editor extraction through the MCP does not. Both are named as
 * unwired WITH the reason, which is the honest rendering.
 */
function unwiredOrigins() {
  return `
    <div class="xq-unwired">
      <span class="label">ORIGINS NOT INVENTORIED</span>
      <span class="railnote">${esc(
        "remote hosts and ad-hoc editor extractions do not appear here. Only the bench harness " +
          "writes telemetry rows, so a memory extracted through the MCP outside the harness is " +
          "real, is in the corpus, and is invisible to this queue. It is listed as unwired rather " +
          "than fabricated — a row here always corresponds to an extraction that happened.",
      )}</span>
    </div>`;
}

// ── VIEW 3: DEDUP — the decision view ───────────────────────────────────────

function dedupView(d) {
  if (!d) {
    return pad(
      "no extraction telemetry on disk yet — near-duplicate decisions are recorded by the " +
        "harness at extraction time.",
    );
  }

  const dist = d.distribution ?? {};
  const scored = dist.scored ?? 0;

  const head = `
    <div class="xd-head">
      <div class="xd-stat">
        <span class="label">THRESHOLD</span>
        <span class="railval">${esc(String(d.threshold))}</span>
        <span class="railnote">${esc(`declared in ${d.threshold_source}`)}</span>
      </div>
      <div class="xd-stat">
        <span class="label">SCORED CANDIDATES</span>
        <span class="railval">${scored === 0 ? nul("none yet") : esc(String(scored))}</span>
        <span class="railnote">${esc("every candidate compared, flagged or not")}</span>
      </div>
      <div class="xd-stat">
        <span class="label">FLAGGED / KEPT</span>
        <span class="railval">${esc(String(dist.flagged ?? 0))} / ${esc(String(dist.kept ?? 0))}</span>
        <span class="railnote">${esc("flagged memories were KEPT and submitted")}</span>
      </div>
      <div class="xd-stat">
        <span class="label">SCORE RANGE</span>
        <span class="railval">${
          dist.min_score === null || dist.min_score === undefined
            ? nul("unobserved")
            : `${esc(dist.min_score.toFixed(4))} – ${esc((dist.max_score ?? dist.min_score).toFixed(4))}`
        }</span>
        <span class="railnote">${esc("observed cosine similarity across all scored candidates")}</span>
      </div>
    </div>`;

  // THE INVARIANT, STATED. This view presents a decision for review; it has no
  // mechanism to discard a memory and must never imply that it does.
  const invariant = `
    <div class="xd-invariant">
      <span class="label">FLAGGED IS NOT DROPPED</span>
      <span class="railnote">${esc(
        "A near-duplicate is FLAGGED and kept — every row below was submitted to the corpus. " +
          "Nothing on this path deletes a memory, and this view has no control that could. " +
          "It exists so the threshold can be judged against the observed score distribution " +
          "rather than by assertion.",
      )}</span>
    </div>`;

  if (!d.flagged?.length) {
    return `${head}${invariant}${pad(
      scored
        ? `no candidate has scored at or above ${d.threshold} yet — every one of the ${scored} scored candidates was kept. That is a real measurement, not an empty panel.`
        : "no candidates scored yet — run an extraction to populate the distribution.",
    )}`;
  }

  const rows = d.flagged
    .map(
      (f) => `
      <div class="xd-row" data-k="xd-${esc(String(f.id))}">
        <span class="xd-score ${f.score >= 0.97 ? "hot" : ""}">${f.score === null ? nul("—") : esc(f.score.toFixed(4))}</span>
        <span class="xd-src">${esc(f.source ?? "—")}</span>
        <span class="xd-fp">${esc(f.memory_fp ?? f.extraction_hash ?? "—")}</span>
        <span class="xd-match">${esc(clip(f.matched ?? "—", 22))}</span>
        <span class="xd-model">${f.producer_model ? esc(clip(f.producer_model, 22)) : nul("unobserved")}</span>
        <span class="xd-size">${f.text_size === null ? nul("—") : esc(`${f.text_size} B`)}</span>
        <span class="xd-decision ${esc(f.decision ?? "")}">${esc((f.decision ?? "—").toUpperCase())}</span>
      </div>`,
    )
    .join("");

  return `
    ${head}
    ${invariant}
    <div class="xd">
      <div class="xd-cols">
        <span>SCORE</span><span>SOURCE</span><span>MEMORY FP</span><span>MATCHED</span>
        <span>PRODUCER</span><span>SIZE</span><span>DECISION</span>
      </div>
      ${rows}
    </div>
    <div class="xd-bodies">
      <span class="label">READING THE MEMORY BODIES</span>
      <span class="railnote">${esc(
        "Fingerprints and scores are shown here because this board is a streaming surface and " +
          "everything on it is public forever. The full plaintext of every candidate IS stored, " +
          "on this machine, in the telemetry DB. To compare two flagged memories directly:",
      )}</span>
      <code class="xd-q">${esc(
        `sqlite3 ${d.db_path ?? "wevibe-bench/data/extract/extraction-telemetry.db"} "SELECT idx, near_dup_score, text FROM extraction_memories WHERE near_dup_decision='flagged' ORDER BY near_dup_score DESC;"`,
      )}</code>
    </div>`;
}

function pad(text) {
  return `<div class="null pad">${esc(text)}</div>`;
}
