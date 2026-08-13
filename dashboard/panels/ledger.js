// ─────────────────────────────────────────────────────────────────────────────
// PANEL: RUN LEDGER
//
// Every cell in the stack, newest first, with the pinned baseline welded to the
// bottom as THE FLOOR.
//
// THE FOOTER IS THE HARD RULE MADE VISIBLE. Efficiency and correctness sit in
// two boxes, SIDE BY SIDE, at the SAME type size, separated by a rule. There is
// no third box combining them, no arrow, no score, no ranking. Reading order
// does not imply precedence — they are adjacent, not stacked.
//
// This matters because it is the exact place the rule is easiest to break: a
// single "improvement" number would be the most natural thing to put here and
// would silently let a faster-and-worse cell read as a win. The layout makes
// that impossible rather than merely discouraged.
//
// THE BASELINE IS PINNED, NOT SORTED. It sits below every scrolling row on its
// own bright top border, on --bg while the rows sit on --bg-2, so it reads as
// bedrock rather than as the last row of a list. It is labelled n=1 BY DESIGN
// at the line — it is a single observation, not a distribution, and saying so
// in a tooltip would be saying it nowhere.
//
// UNFILTERED RECALL rides EVERY ON row. Per the ruling: caveat under the
// allowlist, badge on every ON run. It is the visible debt, and it disappears
// the day the recall filter ships.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, tok, dur } from "../board.js";

export function renderLedger(board) {
  const s = board.stack ?? {};
  const rows = [...(s.all ?? [])].reverse(); // newest first
  const p = board.profile ?? {};

  return `
    <section class="ledger">
      <div class="ledger-head">
        <span class="ttl">RUN LEDGER</span>
        <span class="sub">every cell in this stack · newest first · one profile, frozen at stack creation</span>
        <span class="spacer"></span>
        ${profileChip(p)}
      </div>
      <div class="ledger-cols">
        <span>SEQ</span><span>ARM</span><span>MODEL</span><span>PHASES</span>
        <span>TURNS</span><span>TOKENS</span><span>TIME</span><span>GATES</span>
        <span>CORPUS</span><span>VERDICT</span><span>Δ VS BASELINE</span>
      </div>
      ${rows.length
        ? rows.map((r) => row(r, s)).join("")
        : `<div class="ledger-empty">${esc("no cell has been scheduled in this stack")}</div>`}
      ${footer(s)}
    </section>`;
}

function profileChip(p) {
  if (!p.exists) {
    return `<button class="btn sm" data-profile-open="1">CREATE MEMORY PROFILE</button>`;
  }
  // CLICKABLE. This is the operator's entry point into the inspector: before
  // this, a configured profile had no affordance anywhere on the board and
  // there was no way to see how it was configured after configuring it.
  //
  // The badge is doubled — the words AND the double rule — so it survives a
  // greyscale screenshot and a viewer with colour vision deficiency.
  // The chip states the EDGE, not a count. "3 MODELS" said nothing about which
  // experiment this is — the same count covers same-model and cross-model, and
  // those are the two things the operator most needs to tell apart at a glance.
  const t = p.transfer ?? null;
  const kind =
    t?.kind === "self" ? "SAME-MODEL" : t?.kind === "cross" ? "CROSS-MODEL" : t?.kind === "mixed" ? "MIXED" : "UNSET";
  const n = (p.memory_models ?? []).length;
  return `<button class="tag on chip-btn" data-profile-inspect="1">PROFILE: ${esc(kind)} · ${n} PRODUCER${n === 1 ? "" : "S"} — DECLARED, NOT ENFORCED →</button>`;
}

function row(r, s) {
  const live = r.state === "running";
  const base = s.baseline;
  const isBaseline = base && r.run_dir === base.run_dir && r.sequence_index === base.sequence_index;
  // The floor is rendered pinned below; it must not also appear as a row.
  if (isBaseline) return "";

  const g = r.gates ?? {};
  const c = r.corpus ?? {};

  return `
    <div class="ledger-row ${live ? "live" : ""} ${r.void_instrument ? "void" : ""}">
      <span>${String(r.seq).padStart(2, "0")}</span>
      <span class="arm">${esc((r.arm ?? "—").toUpperCase())}</span>
      <span class="model" title="${esc(r.model ?? "")}">${r.model ? esc(r.model) : nul("unobserved")}</span>
      <span class="${live ? "bright" : ""}">${phases(r)}</span>
      <span>${cell(r.turns, (v) => String(v), live)}</span>
      <span>${cell(r.tokens, (v) => tok(v), live)}</span>
      <span>${cell(r.wall_seconds, (v) => dur(v), live)}</span>
      <span>${gates(g, r)}</span>
      <span>${corpus(c)}</span>
      <span class="${verdictCls(r)}">${verdict(r)}</span>
      <span class="delta">${delta(r, s)}</span>
    </div>`;
}

function phases(r) {
  const p = r.phases ?? {};
  if (r.state === "not_started") return `<span class="null">not started</span>`;
  const base = `${p.done ?? 0} / ${p.total ?? 3}`;
  // Chunks are INTERNAL TO PHASE 1 and are only meaningful while phase 1 runs.
  if (r.state === "running" && (p.done ?? 0) <= 1 && r.chunk?.current) {
    return `${base} · wo ${r.chunk.current}/${r.chunk.total ?? 6}`;
  }
  return base;
}

/** A running cell's numbers are PROVISIONAL and marked ›, never presented final. */
function cell(v, fmt, live) {
  if (v === null || v === undefined) return nul("unobserved");
  return `${esc(fmt(v))}${live ? " <span class='muted'>›</span>" : ""}`;
}

function gates(g, r) {
  if (r.state === "not_started") return nul("never ran");
  if (g.failed === null || g.failed === undefined) return nul("not graded");
  // The denominator is the OBSERVED universe, never a fabricated suite size,
  // so it is labelled `obs` at the number rather than silently implying 114.
  if (!g.total) return `${g.failed} failed`;
  return `${g.total - g.failed}/${g.total}<span class="muted"> obs</span>`;
}

function corpus(c) {
  if (c.at_recall === null || c.at_recall === undefined) {
    // A hole in the chain is not a zero. Saying "0" here would claim an empty
    // corpus, which is a measurement we do not have.
    return nul("unknown");
  }
  return c.at_recall.toLocaleString();
}

function verdict(r) {
  if (r.void_instrument) return "VOID";
  if (r.state === "running") return "RUNNING";
  if (r.state === "not_started") return nul("—");
  return r.verdict ? esc(r.verdict) : nul("unobserved");
}

function verdictCls(r) {
  if (r.void_instrument) return "muted";
  if (r.verdict === "FAIL") return "danger";
  return r.state === "running" ? "bright" : "";
}

/**
 * Δ IS WITHHELD UNTIL THE CELL CLOSES. A partial cell's turn count is a running
 * total; comparing it to a completed baseline would show a fake improvement
 * that shrinks as the cell finishes.
 */
function delta(r, s) {
  const base = s.baseline;
  if (r.state === "running") return `<span class="null">Δ withheld until the cell closes</span>`;
  if (r.state === "not_started") return "";
  if (r.void_instrument) return `<span class="muted">void instrument — not a capability result</span>`;
  if (!base) return `<span class="null">no baseline</span>`;
  if (!s.baseline_scorable) return `<span class="null">baseline is void — no valid Δ</span>`;
  if (r.turns === null || base.turns === null) return nul("unobserved");

  const dTurns = r.turns - base.turns;
  const dTok = r.tokens !== null && base.tokens !== null ? r.tokens - base.tokens : null;
  const dSec = r.wall_seconds !== null && base.wall_seconds !== null ? r.wall_seconds - base.wall_seconds : null;
  const worse = dTurns > 0;

  const parts = [`${sign(dTurns)}${Math.abs(dTurns)} turns`];
  if (dTok !== null) parts.push(`${sign(dTok)}${tok(Math.abs(dTok))}`);
  if (dSec !== null) parts.push(`${sign(dSec)}${dur(Math.abs(dSec))}`);

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
