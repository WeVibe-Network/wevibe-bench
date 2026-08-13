// ─────────────────────────────────────────────────────────────────────────────
// PANEL: TRANSFER CURVE
//
// THE HEADLINE. It replaces the old hero, and it answers the only question the
// board exists to answer: does memory make this model cheaper — and where does
// it stop?
//
// FIVE STATES, each a designed answer rather than a degraded version of the
// last (see STACK_STATES in contract.mjs). The state is DECIDED UPSTREAM by
// sources/stack-ledger.mjs and consumed here; this panel never re-derives it,
// because two definitions of "is this a regression" is exactly the drift the
// board exists to expose.
//
// THE LINE RULE. A line is drawn ONLY at n≥2 ON runs. At n=1 the point is drawn
// alone: a segment from the baseline to a single ON run draws a TREND, and one
// run cannot support a trend. This is the difference between plotting data and
// implying a finding.
//
// CORRECTNESS RIDES EVERY POINT. Each point carries its gate ratio ABOVE it and
// its corpus size BELOW it. That is what makes a faster-and-worse cell legible
// as faster-and-worse: the turn count falls while the gate ratio also falls,
// both visible at the same point, neither folded into the other. A hollow point
// with a danger stroke marks a cell that costs more than the floor.
//
// NO TREND LINE THROUGH THE BASELINE. n=1 by design; the floor is a dashed
// reference line labelled at the line itself, never a series.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, tok, dur } from "../board.js";

/** Geometry, lifted from the design source's own renderer. */
const PAD_X0 = 92;
const PAD_X1 = 26;
const Y_TOP = 58;
const Y_BOT_INSET = 58;

const METRICS = {
  turns: { key: "turns", label: "TURNS — LOWER IS BETTER", fmt: (v) => String(v), unit: "" },
  tokens: { key: "tokens", label: "TOKENS — LOWER IS BETTER", fmt: (v) => tok(v) ?? "—", unit: "" },
  time: { key: "wall_seconds", label: "WALL TIME — LOWER IS BETTER", fmt: (v) => dur(v) ?? "—", unit: "" },
};

/** Which metric the curve is showing. Client-side only — never refetches. */
let metric = "turns";
export function setCurveMetric(m) {
  if (METRICS[m]) metric = m;
}
export function curveMetric() {
  return metric;
}

export function renderCurve(board) {
  const s = board.stack ?? {};
  const state = s.state ?? "no_baseline";
  const M = METRICS[metric];

  return `
    <section class="panel curve">
      <div class="phead">
        <span class="ttl">TRANSFER CURVE</span>
        <span class="sub">does memory make this model cheaper — and where does it stop?</span>
        <span class="seg" data-seg="metric">
          ${segBtn("turns", "TURNS")}
          ${segBtn("tokens", "TOKENS")}
          ${segBtn("time", "TIME")}
        </span>
      </div>
      ${body(s, state, M)}
      ${footer(s, state, M)}
    </section>`;
}

function segBtn(id, label) {
  return `<button class="${metric === id ? "on" : ""}" data-metric="${id}">${label}</button>`;
}

function body(s, state, M) {
  if (state === "no_baseline") {
    return frame(
      "no baseline · no axis · nothing measured",
      "There is no OFF run in this campaign. The stack cannot start.",
      "A curve with no floor would be a claim with no evidence.",
    );
  }

  if (state === "baseline_void") {
    // A floor exists but the harness itself refuses to score it. Drawing a
    // curve against it would measure every ON run against a transport failure.
    const b = s.baseline ?? {};
    return frame(
      "baseline is void-instrument · no valid floor",
      `The OFF cell ended ${esc(b.terminal_reason ?? "with a truncation signal")} — an instrument failure, not a capability result.`,
      "RUNBOOK 5.10: it is not scored, so no delta on this board would be valid. Re-run the baseline.",
      true,
    );
  }

  const pts = plottable(s);
  return svg(s, pts, M, state);
}

function plottable(s) {
  return (s.runs ?? []).filter((r) => !r.void_instrument && val(r, metric) !== null);
}

function val(row, m) {
  const v = m === "turns" ? row.turns : m === "tokens" ? row.tokens : row.wall_seconds;
  return Number.isFinite(v) ? v : null;
}

function frame(headline, line, note, bad = false) {
  return `
    <div class="curve-frame ${bad ? "bad" : ""}">
      <div class="curve-frame-head">${esc(headline)}</div>
      <div class="curve-frame-line">${esc(line)}</div>
      <div class="curve-frame-note">${esc(note)}</div>
    </div>`;
}

function svg(s, pts, M, state) {
  const W = 860;
  const H = 300;
  const base = val(s.baseline ?? {}, metric);
  const x0 = PAD_X0;
  const x1 = W - PAD_X1;
  const yBot = H - Y_BOT_INSET;

  const vals = pts.map((p) => val(p, metric)).concat(base !== null ? [base] : []);
  if (!vals.length) {
    return frame("nothing plottable yet", "The floor exists but no ON run has produced a measurement.", "Arm an ON run to start the curve.");
  }

  let lo = Math.min(...vals);
  let hi = Math.max(...vals);
  const pad = (hi - lo) * 0.35 || 6;
  lo -= pad;
  hi += pad;

  const Y = (v) => yBot - ((v - lo) / (hi - lo)) * (yBot - Y_TOP);
  const X = (i) => (pts.length <= 1 ? (x0 + x1) / 2 : x0 + (i * (x1 - x0)) / (pts.length - 1));

  const bits = [];

  bits.push(`<text x="2" y="14" class="c-axis">${esc(M.label)}</text>`);
  if (base !== null) {
    bits.push(
      `<text x="2" y="32" class="c-legend">─ ─ OFF baseline · n=1 · ${esc(M.fmt(base))}</text>`,
    );
  }
  bits.push(`<line x1="${x0 - 18}" y1="${yBot + 10}" x2="${x1}" y2="${yBot + 10}" class="c-ax"/>`);

  // THE FLOOR. Dashed, labelled AT THE LINE — n=1 is stated where it is read,
  // not in a footnote a viewer has to hunt for.
  if (base !== null) {
    const by = Y(base);
    bits.push(`<line x1="${x0 - 18}" y1="${by}" x2="${x1}" y2="${by}" class="c-base"/>`);
    bits.push(`<text x="2" y="${by + 4}" class="c-base-lbl">OFF ${esc(M.fmt(base))}</text>`);
  }

  // THE LINE — n≥2 only. See the header.
  if (pts.length >= 2) {
    const d = pts
      .map((p, i) => `${i ? "L" : "M"}${X(i)} ${Y(val(p, metric))}`)
      .join(" ");
    bits.push(`<path d="${d}" class="c-path"/>`);
  }

  pts.forEach((p, i) => {
    const cx = X(i);
    const cy = Y(val(p, metric));
    // A cell that costs MORE than the floor is hollow with a danger stroke —
    // drawn at full weight, same size, same line. The finding is not hidden.
    const worse = base !== null && val(p, metric) >= base;
    const g = p.gates ?? {};
    const gateTxt =
      g.failed === null || g.failed === undefined
        ? "gates not measured"
        : g.total
          ? `${g.total - g.failed}/${g.total} obs`
          : `${g.failed} failed`;
    // Gate ratio worsening is its OWN signal, independent of cost.
    const gateBad = g.failed !== null && g.failed !== undefined && g.failed > 0;
    const corpus =
      p.corpus?.at_recall === null || p.corpus?.at_recall === undefined
        ? "corpus unknown"
        : `${p.corpus.at_recall.toLocaleString()} mem`;
    const delta =
      base === null ? "" : deltaLabel(val(p, metric) - base, metric);

    bits.push(
      `<circle cx="${cx}" cy="${cy}" r="5.5" class="c-pt ${worse ? "worse" : ""}"/>`,
      `<text x="${cx}" y="${cy - 30}" text-anchor="middle" class="c-val">${esc(M.fmt(val(p, metric)))}</text>`,
      `<text x="${cx}" y="${cy - 15}" text-anchor="middle" class="c-delta ${worse ? "bad" : ""}">${esc(delta)}</text>`,
      `<text x="${cx}" y="${yBot + 28}" text-anchor="middle" class="c-gate ${gateBad ? "bad" : ""}">${esc(gateTxt)}</text>`,
      `<text x="${cx}" y="${yBot + 44}" text-anchor="middle" class="c-corpus">${esc(corpus)}</text>`,
    );
  });

  if (state === "regression") {
    bits.push(
      `<text x="${x1}" y="14" text-anchor="end" class="c-annot">CROSSES THE FLOOR AT RUN ${pts.length}</text>`,
    );
  }
  if (state === "n1_on") {
    bits.push(
      `<text x="${x1}" y="14" text-anchor="end" class="c-note">n=1 — NO LINE DRAWN</text>`,
    );
  }

  return `<div class="curve-svg"><svg viewBox="0 0 ${W} ${H}" width="100%">${bits.join("")}</svg></div>`;
}

function deltaLabel(d, m) {
  if (!Number.isFinite(d)) return "";
  const sign = d > 0 ? "+" : "−";
  const a = Math.abs(d);
  if (m === "tokens") return `${sign}${tok(a) ?? a}`;
  if (m === "time") return `${sign}${dur(a) ?? a}`;
  return `${sign}${a}`;
}

function footer(s, state, M) {
  const base = val(s.baseline ?? {}, metric);
  const pts = plottable(s);
  const newest = pts.length ? pts[pts.length - 1] : null;

  let verdict;
  if (state === "no_baseline") verdict = "arm an OFF run — it is the only cell that can be first";
  else if (state === "baseline_void") verdict = "no valid floor — re-run the baseline before any ON run";
  else if (state === "baseline_only") verdict = "floor established · no ON run yet";
  else if (state === "n1_on") verdict = "one ON run — a single delta, stated as a single delta";
  else if (state === "regression" && newest && base !== null) {
    const c = newest.corpus?.at_recall;
    verdict = `transfer stopped — ${deltaLabel(val(newest, metric) - base, metric)} at ${c === null || c === undefined ? "an unknown corpus size" : `${c.toLocaleString()} memories`}`;
  } else if (newest && base !== null) {
    const c = newest.corpus?.at_recall;
    verdict = `transfer holding — ${deltaLabel(val(newest, metric) - base, metric)} at ${c === null || c === undefined ? "an unknown corpus size" : `${c.toLocaleString()} memories`}`;
  } else verdict = "";

  const bad = state === "regression" || state === "baseline_void";

  return `
    <div class="curve-foot">
      <span>─── OFF baseline, n=1${base !== null ? `, ${esc(M.fmt(base))}` : ""}</span>
      <span class="bright">● ON run · corpus below · gates above</span>
      <span class="${bad ? "danger" : "bright"}">${esc(verdict)}</span>
    </div>`;
}
