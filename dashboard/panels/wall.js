// ─────────────────────────────────────────────────────────────────────────────
// THE WALL — the persistent signature element
//
// Every conformance gate as a fixed cell, one strip per arm, identical geometry
// so the same gate sits at the same x-position in both rows and the arms can be
// diffed by eye.
//
// THREE STATES, AND THE THIRD ONE MATTERS:
//   red        — failing in the latest attempt
//   green      — was red in an earlier attempt of this cell and is now absent
//   unobserved — never seen failing in this arm at all
//
// A gate absent from attempt 1 was never failing, so it is NOT evidence that
// anything was fixed and must never be counted as green. That is why
// `unobserved` is hatched rather than coloured — it is a third thing, not a
// weaker pass.
//
// SLOTS NEVER REFLOW. Gate order is fixed for the life of the run so erosion
// reads as change-over-time rather than as relayout. The grid is also the one
// surface a skeptic can check line-by-line against the raw failed_gates list.
//
// NO MOTION HERE DURING A TAKEOVER. A gate flipping green next to a serve that
// just fired would assert causation the data cannot support. Separate surfaces.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, clip } from "../board.js";

export function renderWall(board) {
  const wall = board.wall ?? { gates: [], totals: {} };
  const gates = wall.gates ?? [];

  if (!gates.length) {
    return panel(`
      <div style="display:flex;align-items:center;justify-content:center;height:100%">
        <div style="text-align:center;max-width:420px">
          <div class="label" style="margin-bottom:8px">no gates observed yet</div>
          <div class="null" style="line-height:1.6">
            gates are recorded when the first attempt's oracle run completes.
            the wall fills at attempt end, not continuously.
          </div>
        </div>
      </div>`, gates.length);
  }

  return panel(`
    ${armStrip(board, "a", "on")}
    ${armStrip(board, "b", "off")}
    ${legend()}
    ${changedLine(gates)}
  `, gates.length);
}

function panel(inner, count) {
  return `
  <div class="panel" style="grid-row:1/3">
    <div class="phead">
      <span class="label">The Wall — ${count || "no"} conformance gates</span>
      <span class="label">checkable against raw log</span>
    </div>
    <div class="pbody">${inner}</div>
  </div>`;
}

function armStrip(board, side, armKey) {
  const gates = board.wall.gates ?? [];
  const totals = board.wall.totals?.[side] ?? { red: 0, green: 0, unobserved: 0 };
  const meta = armKey === "on"
    ? { label: "ARM A · MEMORY ON", color: "var(--arm-a)" }
    : { label: "ARM B · CONTROL", color: "var(--arm-b)" };

  // An arm with nothing observed is a real, expected state — say why in words
  // rather than showing an ambiguous empty strip.
  const allUnobserved = totals.green === 0 && totals.red === 0;
  const summary = allUnobserved
    ? nul(armKey === "on" ? "no memory-on cell yet" : "no control cell yet")
    : `<span class="pass">${totals.green} green</span> · <span class="fail">${totals.red} red</span>` +
      (totals.unobserved ? ` · <span class="null">${totals.unobserved} unobserved</span>` : "");

  const cells = gates
    .map((g) => `<div class="cellbox ${g[side]}" title="${esc(g.id)}"></div>`)
    .join("");

  return `
  <div class="wallrow">
    <div class="wallhead">
      <span class="label" style="color:${meta.color}">${meta.label}</span>
      <span style="font-size:var(--fs-label)">${summary}</span>
    </div>
    <div class="strip">${cells}</div>
  </div>`;
}

function legend() {
  return `
  <div style="display:flex;gap:16px;font-size:var(--fs-label);margin-top:4px">
    <span><span class="cellbox red" style="display:inline-block;width:14px;height:12px;vertical-align:-1px"></span> failing</span>
    <span><span class="cellbox green" style="display:inline-block;width:14px;height:12px;vertical-align:-1px"></span> resolved</span>
    <span><span class="cellbox unobserved" style="display:inline-block;width:14px;height:12px;vertical-align:-1px"></span> never observed failing</span>
  </div>`;
}

/**
 * Named gates that flipped. The grid alone cannot say WHAT changed, and a gate
 * id is the thing a viewer can verify — so the names print in full-size text
 * rather than hiding in a tooltip nobody on a stream can hover.
 */
function changedLine(gates) {
  const flipped = gates
    .filter((g) => g.a_flipped_at_attempt !== null || g.b_flipped_at_attempt !== null)
    .slice(0, 3);

  if (!flipped.length) {
    return `<div class="changed"><span class="label">last changed</span> ${nul("nothing yet")}</div>`;
  }

  const items = flipped
    .map((g) => {
      const who = g.a_flipped_at_attempt !== null ? "a" : "b";
      return `<span class="${who}">${esc(g.id)}</span> ${esc(g.req ?? "")} ${esc(clip(g.title ?? "", 46))}`;
    })
    .join(" &nbsp;·&nbsp; ");

  return `<div class="changed"><span class="label">most recent flips</span> ${items}</div>`;
}
