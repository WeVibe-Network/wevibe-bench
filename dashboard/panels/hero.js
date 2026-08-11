// ─────────────────────────────────────────────────────────────────────────────
// HERO — the arm delta. The ONLY causal surface on the board.
//
// HARD RULE: below min_cells_per_arm this panel renders COLLECTING with the
// real cell counts, and no delta. A number here at n=1 is exactly what a
// skeptical engineer kills you with, and the empty state is a statement of
// rigour rather than a gap — so it is designed to look deliberate, because it
// will be on screen for most of the night.
//
// NO CONFIDENCE INTERVAL, EVER, over gate counts. 68 gates from one cell are
// not 68 independent samples — they cluster within cell. A binomial CI over
// them would be a lie dressed as rigour. The standing note says so permanently,
// not as a footnote that scrolls away.
//
// Cell counts are rendered at equal visual weight to the rate, because with
// n=3 the denominator is the more honest number.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, pct, nul } from "../board.js";

export function renderHero(board) {
  const d = board.arm_delta ?? {};
  const body = d.sufficient && d.delta !== null ? sufficient(d) : collecting(d);

  return `
  <div class="panel">
    <div class="phead">
      <span class="label">Arm delta — gate resolution</span>
      <span class="label">${d.sufficient ? "n sufficient" : `n &lt; ${d.min_cells_per_arm ?? 3} per arm`}</span>
    </div>
    <div class="pbody">
      ${body}
      <div class="note">${esc(d.note ?? "")}</div>
    </div>
  </div>`;
}

function sufficient(d) {
  const delta = d.delta * 100;
  const sign = delta > 0 ? "+" : "";
  return `
  <div class="hero">
    <div>
      <div class="heronum">${sign}${delta.toFixed(1)}</div>
      <div class="label" style="margin-top:4px">points · resolution rate</div>
    </div>
    ${armCol("a", "ARM A · MEMORY ON", d.a)}
    ${armCol("b", "ARM B · CONTROL", d.b)}
  </div>
  <div style="margin-top:10px;font-size:var(--fs-body);color:var(--type);line-height:1.5">
    ${esc(d.statement ?? "")}
  </div>`;
}

/**
 * The empty state. It carries the real counts and the threshold, so a viewer
 * learns what is being waited for rather than seeing a spinner.
 */
function collecting(d) {
  const need = d.min_cells_per_arm ?? 3;
  return `
  <div class="hero">
    <div>
      <div class="collect">COLLECTING</div>
      <div class="label" style="margin-top:6px">${need} cells per arm required<br>before any delta is shown</div>
    </div>
    ${armCol("a", "ARM A · MEMORY ON", d.a, need)}
    ${armCol("b", "ARM B · CONTROL", d.b, need)}
  </div>`;
}

function armCol(cls, label, arm, need) {
  const a = arm ?? {};
  const cells = a.cells ?? 0;

  const rate =
    a.resolution_rate === null || a.resolution_rate === undefined
      ? nul("unobserved")
      : `<span class="${cls}">${pct(a.resolution_rate)}</span>`;

  const gates =
    a.gates_resolved === null || a.gates_total === null
      ? ""
      : `<span class="label">${a.gates_resolved}/${a.gates_total} gates</span>`;

  const median =
    a.median_turns_to_green === null || a.median_turns_to_green === undefined
      ? nul("unobserved")
      : `${a.median_turns_to_green}`;

  // Excluded cells are stated, never hidden. A cell dropped from the delta is
  // still a cell that ran and burned real tokens; silently omitting it would be
  // its own kind of dishonesty, and the reason is what makes the exclusion
  // auditable against RUNBOOK rule 5.10 rather than something to take on trust.
  const ex = a.excluded ?? {};
  const excluded =
    (ex.total ?? 0) > 0
      ? `<div style="font-size:var(--fs-label);margin-top:4px">
           <span class="null">${ex.total} excluded</span>
           <span class="label">${excludedReasons(ex)}</span>
         </div>`
      : "";

  return `
  <div class="armcol">
    <div class="label" style="color:var(--arm-${cls})">${label}</div>
    <div class="armrate">${rate}</div>
    <div style="font-size:var(--fs-label);margin-top:2px">
      <span class="${cls}">${cells}</span>${need ? `<span class="null"> / ${need}</span>` : ""} cells ${gates}
    </div>
    ${excluded}
    <div style="font-size:var(--fs-label);margin-top:6px">
      <span class="label">median turns to green</span> ${median}
    </div>
  </div>`;
}

function excludedReasons(ex) {
  const parts = [];
  if ((ex.void_instrument ?? 0) > 0) parts.push(`${ex.void_instrument} void-instrument`);
  if ((ex.resolution_unmeasurable ?? 0) > 0) {
    parts.push(`${ex.resolution_unmeasurable} single-attempt`);
  }
  return esc(parts.join(" · "));
}
