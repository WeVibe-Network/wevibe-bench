// ─────────────────────────────────────────────────────────────────────────────
// EPISODE TICKER + LIVE CELL
//
// TICKER: rows keyed by failureKey, newest first.
//   `red-again` is the pre-trigger state and visibly TENSES UP — this is the
//   beat before the drop. The tension is STATIC (double rule + inset + armed
//   marker), never a pulse: this board is up for hours and a blinking row
//   becomes wallpaper within minutes.
//
//   The empty state carries its REASON. On a control cell there are no episodes
//   by construction, and saying so is the difference between a board that looks
//   broken and one that looks deliberate.
//
// CELL: the only fast-moving panel — it is what proves the board is alive when
//   everything else is static for 30 minutes at a time.
//
//   TURN COUNT (WO-NUDGE-INF-1): displays SCORING turns. `session_turns` is the
//   raw count inflated by recovered turns and is shown separately as an anomaly
//   figure, never as the measurement.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, dur, tok, clip, armOf } from "../board.js";

const STATE_STYLE = {
  red: { color: "var(--fail)", text: "RED" },
  "red-again": { color: "var(--fail)", text: "RED AGAIN" },
  "recall-fired": { color: "var(--arm-a)", text: "RECALL FIRED" },
  injected: { color: "var(--arm-a)", text: "INJECTED" },
  green: { color: "var(--pass)", text: "GREEN" },
  abandoned: { color: "var(--null)", text: "ABANDONED" },
};

export function renderTicker(board) {
  const eps = board.episodes ?? [];
  const isControl = board.run?.arm === "off";

  const body = eps.length
    ? eps.slice(0, 9).map(row).join("")
    : `
      <div style="display:flex;align-items:center;justify-content:center;height:100%">
        <div style="text-align:center;max-width:400px">
          <div class="label" style="margin-bottom:8px">
            ${isControl ? "control arm — none by construction" : "no episodes observed yet"}
          </div>
          <div class="null" style="font-size:var(--fs-label);line-height:1.6">
            ${isControl
              ? "the harness removes plugin state on a control cell, so no episode can be recorded. this is the measurement working, not a gap."
              : "an episode opens when a signal first fails. it arms on the second failure of the same key."}
          </div>
        </div>
      </div>`;

  return `
  <div class="panel">
    <div class="phead">
      <span class="label">Episode ticker</span>
      <span class="label">newest first</span>
    </div>
    <div class="pbody" style="overflow:auto">${body}</div>
  </div>`;
}

function row(e) {
  const s = STATE_STYLE[e.state] ?? { color: "var(--null)", text: String(e.state ?? "—").toUpperCase() };
  const arm = armOf(e.arm);
  const tense = e.state === "red-again";

  return `
  <div class="trow ${tense ? "tense" : ""}">
    <span class="ident">${esc(e.failure_key ?? "—")}</span>
    <span class="${arm.cls}" style="font-size:var(--fs-label);font-weight:700">${arm.short}</span>
    <span>
      <span class="state" style="color:${s.color}">${s.text}${tense ? " ▸ ARMED" : ""}</span>
      ${e.error ? `<div class="null" style="font-size:12px;margin-top:2px">${esc(clip(e.error, 58))}</div>` : ""}
    </span>
    <span style="font-size:var(--fs-label)">${e.turns ?? "—"}t</span>
    <span style="font-size:var(--fs-label)">${dur(e.elapsed_s) ?? nul("—")}</span>
  </div>`;
}

export function renderCell(board) {
  const r = board.run ?? {};

  // Scoring vs raw turns. Showing the raw count as "turns" would inflate the
  // measurement by exactly the turns the harness deliberately excludes.
  const excluded =
    r.session_turns !== null && r.session_turns !== undefined &&
    r.turns !== null && r.turns !== undefined
      ? r.session_turns - r.turns
      : null;

  const items = [
    ["chunk", r.chunk?.current ? `${r.chunk.current}/${r.chunk.total ?? "?"}` : null],
    ["attempt", r.attempt?.current ? `${r.attempt.current}/${r.attempt.max ?? 3}` : null],
    ["scoring turns", r.turns],
    ["tokens in", tok(r.tokens?.input)],
    ["tokens out", tok(r.tokens?.output)],
    ["injected block", tok(r.tokens?.injected_block)],
  ];

  const cells = items
    .map(
      ([label, v]) => `
      <div>
        <div class="label">${label}</div>
        <div style="font-size:22px;font-weight:600;color:var(--type)">
          ${v === null || v === undefined ? nul("—") : esc(String(v))}
        </div>
      </div>`,
    )
    .join("");

  // Silence is information — with nudges now unbounded, a long silence beside a
  // climbing nudge count is the wedged-relay signature.
  const silent = r.log_silent_s;
  const silentHtml =
    silent === null || silent === undefined
      ? ""
      : `<span class="label" style="color:${silent > 900 ? "var(--fail)" : "var(--dim)"}">
           last progress ${dur(silent)} ago${silent > 900 ? " · EXCEEDS 900s" : ""}
         </span>`;

  return `
  <div class="panel">
    <div class="phead">
      <span class="label">Cell — live</span>
      ${silentHtml}
    </div>
    <div class="pbody">
      <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px">${cells}</div>
      <div class="note">
        ${excluded !== null
          ? `${excluded} turn${excluded === 1 ? "" : "s"} recovered and excluded from scoring (raw session turns ${r.session_turns}).`
          : "scoring turns exclude guard-killed and finalize-killed turns."}
      </div>
    </div>
  </div>`;
}
