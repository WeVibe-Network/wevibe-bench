// ─────────────────────────────────────────────────────────────────────────────
// HONESTY RAIL — permanent, bottom.
//
// This is the panel that buys credibility for everything above it. It answers
// the questions an engineer is already forming, before they have to ask.
//
// SIX FIELDS:
//   coverage        % of episodes reaching an observable conclusion, carrying
//                   the literal statement that uncovered episodes count as
//                   NEITHER positive nor negative.
//   unresolved      running count, never hidden.
//   guard           detections by type. the safety pipeline being live IS a
//                   result, so zero detections after a real scan is reported as
//                   a measured zero, not as absence.
//   recall overhead p50/p95. NEVER synthesized — an unmeasured latency seam is
//                   VOID-INSTRUMENT, so unmeasured stays visibly unmeasured.
//   cost            TWO different costs, never summed:
//                     pre-trigger — turns burned before the gated trigger fired
//                     recovered   — turns that happened and were excluded from
//                                   scoring (guard + finalize kills)
//   serves          delivery ONLY. labelled as such. this box is deliberately
//                   the quietest of the six so it can never read as a win
//                   metric.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, pct } from "../board.js";

export function renderRail(board) {
  const h = board.honesty ?? {};

  return `
  <div class="rail">
    ${coverage(h)}
    ${unresolved(h)}
    ${guard(h)}
    ${latency(h)}
    ${cost(h)}
    ${serves(h)}
  </div>`;
}

function box(label, value, note, opts = {}) {
  return `
  <div class="railbox" style="${opts.quiet ? "opacity:.82" : ""}">
    <div class="label">${label}</div>
    <div class="railval">${value}</div>
    <div class="railnote">${note}</div>
  </div>`;
}

function coverage(h) {
  const c = h.coverage ?? {};
  const val =
    c.total === null || c.total === undefined || c.total === 0
      ? nul(c.total === 0 ? "0 episodes" : "unobserved")
      : `${c.concluded ?? 0}/${c.total} <span class="label">${pct((c.concluded ?? 0) / c.total) ?? ""}</span>`;

  return box("coverage", val, esc(c.note ?? "uncovered episodes count as neither positive nor negative"));
}

function unresolved(h) {
  const v = h.unresolved;
  return box(
    "unresolved",
    v === null || v === undefined ? nul("unobserved") : String(v),
    "episodes still red at close — counted, never hidden",
  );
}

function guard(h) {
  const g = h.guard_detections ?? {};
  const keys = Object.keys(g);
  const total = keys.reduce((a, k) => a + (g[k] ?? 0), 0);

  // A measured zero is a RESULT (the pipeline ran and found nothing). It is not
  // the same as never having scanned, and the note distinguishes them.
  const val = !keys.length ? nul("no scan observed") : String(total);
  const note = keys.length
    ? esc(keys.map((k) => `${k} ${g[k]}`).join(" · "))
    : "the safety pipeline being live is itself a result";

  return box("guard detections", val, note);
}

function latency(h) {
  const l = h.recall_latency_ms ?? {};
  const val =
    l.p50 === null || l.p50 === undefined
      ? nul("unmeasured")
      : `${l.p50}<span class="label"> / ${l.p95}ms</span>`;

  return box(
    "recall overhead",
    val,
    l.n ? `p50 / p95 over ${l.n} calls` : "never synthesized — an unmeasured seam stays unmeasured",
  );
}

/**
 * The two costs, side by side and explicitly NOT summed. They are different
 * facts: one is the price of the gated trigger, the other is the price of
 * transport recovery.
 */
function cost(h) {
  const w = h.wasted_turns;
  const rec = h.recovered_turns;

  const part = (v, label) =>
    v === null || v === undefined
      ? `<span class="null">—</span> <span class="label">${label}</span>`
      : `${v} <span class="label">${label}</span>`;

  return box(
    "turns burned",
    `${part(w, "pre-trigger")} &nbsp; ${part(rec, "recovered")}`,
    "pre-trigger = cost of the second-failure gate · recovered = happened, excluded from scoring",
  );
}

function serves(h) {
  const s = h.serves ?? {};
  const val =
    s.sent === null || s.sent === undefined
      ? nul("unobserved")
      : `${s.sent}<span class="label"> sent</span>`;

  const bits = [];
  if (s.confirmed_on_chain !== null && s.confirmed_on_chain !== undefined) bits.push(`${s.confirmed_on_chain} on-chain`);
  if (s.rejected !== null && s.rejected !== undefined) bits.push(`${s.rejected} rejected`);

  return box(
    "serves · delivery only",
    val,
    `${bits.length ? `${esc(bits.join(" · "))} — ` : ""}<strong>delivery, not outcome</strong>`,
    { quiet: true },
  );
}
