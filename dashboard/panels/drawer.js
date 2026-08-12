// ─────────────────────────────────────────────────────────────────────────────
// THE DRAWER — live transcript · events · extraction, plus run control
//
// WHY IT LIVES OUTSIDE #root
// `render()` in board.js replaces the whole of #root.innerHTML on every 2s
// poll. That is correct for a read-only board and fatal for an interactive
// surface: a wholesale innerHTML swap destroys select focus, a half-typed
// selection, the scroll position of the feed, and the drawer's own open/closed
// state. So the drawer mounts as a SIBLING of #root and updates surgically —
// only the panes whose data actually changed are rewritten, and never while a
// control inside them has focus.
//
// ONE DRAWER, THREE TABS (design-confirmed). Three independent popouts would
// put two live surfaces in competition for the same screen edge; one drawer
// makes the choice explicit and costs one lip of permanent screen area.
//
// ANCHORED ABOVE THE PROVENANCE STRIP. The strip carries the attestation and
// anchor status — the two things that decide whether a run counts at all — so
// the drawer's travel stops short of it instead of covering it.
//
// WRITES GO DIRECT TO THE CONTROL PLANE. The dashboard server is read-only
// (GET-only, :ro mount, no docker socket) and stays that way; the browser POSTs
// to the control plane's own origin, published to us as `control.base_url`.
// The dashboard is never in the write path.
//
// EVERY DESTRUCTIVE ACTION IS TWO-STEP. The first click only ARMS the button
// and reveals the verbatim restatement of what is about to happen; the second
// click sends it, carrying a confirmation token the server itself minted. A
// benchmark run is expensive and a mis-click costs hours, so the cost of the
// second click is the cheapest insurance on the board.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul, dur, clip } from "../board.js";

// Discrete stops, never a free drag. Fixed stops keep the 34px event row and
// the 44px stage row landing on the same pixel grid at every size, and stop a
// viewer leaving the board in a half-broken layout mid-stream.
const LIP = 34;

/** Resolve viewport-relative stops. Recomputed on resize, never cached across. */
function stops() {
  const vh = window.innerHeight;
  return {
    closed: LIP,
    peek: 180,
    half: Math.round(vh * 0.45),
    full: Math.round(vh * 0.75),
  };
}

/**
 * Default stop by viewport height. The design asks for half by default at
 * >=1440 and a peek at 1366 — at 1366 a 45vh drawer would eat the wall, and the
 * wall is the board's signature element.
 */
function defaultStop() {
  return window.innerHeight >= 900 ? "half" : "peek";
}

// ── local UI state (never derived from the board payload) ────────────────────

const ui = {
  mounted: false,
  stop: "closed",
  tab: "events",
  // Per-kind filters. All on by default: a filter that starts engaged hides
  // data without the viewer ever having asked for it.
  filters: { tool: true, file: true, thinking: true, error: true, lifecycle: true },
  follow: true,          // stick to newest while already at the bottom
  armed: null,           // "start" | "extract" — which action is awaiting confirm
  token: null,           // server-minted confirmation token for the armed action
  restatement: null,
  pending: false,
  lastRefusal: null,
  sel: { model: "", context: "", org: "", task: "", extractModel: "" },
};

let latest = null;       // last board payload seen

// ── mount ────────────────────────────────────────────────────────────────────

export function mountDrawer() {
  if (ui.mounted) return;
  const el = document.createElement("div");
  el.className = "drawer";
  el.id = "drawer";
  el.innerHTML = shell();
  document.body.appendChild(el);
  ui.mounted = true;

  applyHeight();
  wire(el);

  // The provenance strip's height decides where the drawer's bottom edge sits.
  // It is measured rather than assumed because it WRAPS at narrow widths, and a
  // hardcoded offset would let the drawer creep over it exactly when the strip
  // has grown to two lines.
  measureProv();
  window.addEventListener("resize", () => {
    measureProv();
    applyHeight();
  });
}

function measureProv() {
  const prov = document.querySelector(".prov");
  const h = prov ? Math.round(prov.getBoundingClientRect().height) : 28;
  document.documentElement.style.setProperty("--prov-h", `${h}px`);
}

function shell() {
  return `
    <div class="dlip" id="dlip">
      <span class="dgrip"></span>
      <div class="dtabs" id="dtabs">
        ${tabBtn("transcript", "RAW TRANSCRIPT")}
        ${tabBtn("events", "EVENTS")}
        ${tabBtn("extraction", "EXTRACTION")}
      </div>
      <span class="spacer" style="flex:1"></span>
      <span class="label" id="dstatus" style="color:var(--dim)"></span>
    </div>
    <div class="dbody" id="dbody">
      <div class="dpane" id="pane-transcript"><div class="dscroll" id="sc-transcript"></div></div>
      <div class="dpane" id="pane-events">
        <div class="dfilters" id="evfilters"></div>
        <div class="dscroll" id="sc-events"></div>
      </div>
      <div class="dpane" id="pane-extraction">
        <div class="dscroll" id="sc-extraction"></div>
      </div>
      <div class="dctl" id="dctl"></div>
    </div>`;
}

function tabBtn(id, label) {
  return `<button class="dtab" data-tab="${id}"><span>${label}</span><span class="dcount" data-count="${id}"></span></button>`;
}

// ── height ───────────────────────────────────────────────────────────────────

function applyHeight() {
  const el = document.getElementById("drawer");
  if (!el) return;
  const h = stops()[ui.stop] ?? LIP;
  el.style.setProperty("--drawer-h", `${h}px`);
  el.classList.toggle("open", ui.stop !== "closed");
}

function toggle() {
  ui.stop = ui.stop === "closed" ? defaultStop() : "closed";
  applyHeight();
  if (ui.stop !== "closed") paint();
}

// ── events wiring ────────────────────────────────────────────────────────────

function wire(el) {
  el.querySelector("#dlip").addEventListener("click", (e) => {
    // A tab click selects that tab; it only opens the drawer if it was closed,
    // so clicking the active tab of an open drawer never collapses it by
    // surprise.
    const tab = e.target.closest(".dtab");
    if (tab) {
      const id = tab.dataset.tab;
      const wasClosed = ui.stop === "closed";
      if (!wasClosed && ui.tab === id) return;
      ui.tab = id;
      if (wasClosed) ui.stop = defaultStop();
      applyHeight();
      paint();
      return;
    }
    toggle();
  });

  // Filters, controls, and confirm buttons are delegated so the panes can be
  // rewritten freely without ever rebinding a listener.
  el.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip && chip.dataset.kind) {
      ui.filters[chip.dataset.kind] = !ui.filters[chip.dataset.kind];
      paint();
      return;
    }
    const act = e.target.closest("[data-action]");
    if (act) onAction(act.dataset.action);
  });

  el.addEventListener("change", (e) => {
    const s = e.target.closest("[data-sel]");
    if (!s) return;
    ui.sel[s.dataset.sel] = s.value;
    // Any parameter change invalidates a pending confirmation: the token was
    // minted for the OLD parameters, and silently reusing it would start a run
    // the operator never actually agreed to.
    disarm();
    paint();
  });

  // Following the tail is a mode the viewer controls by scrolling, which is the
  // interaction people already expect from a log.
  el.querySelector("#sc-events").addEventListener("scroll", (e) => {
    const n = e.target;
    ui.follow = n.scrollTop + n.clientHeight >= n.scrollHeight - 24;
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && ui.stop !== "closed") {
      if (ui.armed) { disarm(); paint(); return; }
      ui.stop = "closed";
      applyHeight();
    }
  });
}

function disarm() {
  ui.armed = null;
  ui.token = null;
  ui.restatement = null;
}

// ── update from the board poll ───────────────────────────────────────────────

export function updateDrawer(board) {
  latest = board;
  if (!ui.mounted) return;
  measureProv();
  paintLip();
  if (ui.stop !== "closed") paint();
}

function ctl() { return latest?.control ?? null; }

function paintLip() {
  const c = ctl();
  const ev = latest?.events ?? null;
  const ex = latest?.extraction ?? null;

  document.querySelectorAll(".dtab").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === ui.tab);
  });

  const count = (id, v) => {
    const n = document.querySelector(`[data-count="${id}"]`);
    if (n) n.textContent = v == null ? "" : String(v);
  };
  count("events", ev?.retained ?? null);
  count("extraction", ex && ex.state !== "idle" ? stageDone(ex) : null);
  count("transcript", null);

  // The lip is the one line that is ALWAYS on screen, so it carries the single
  // most load-bearing fact: whether the control plane is even there.
  const s = document.getElementById("dstatus");
  if (!s) return;
  if (!c) {
    s.innerHTML = `<span style="color:var(--dim)">control plane not enabled</span>`;
    return;
  }
  if (!ev?.connected) {
    s.innerHTML = `<span style="color:var(--dim)">event feed ${esc(clip(ev?.reason ?? "disconnected", 54))}</span>`;
    return;
  }
  const idle = ev.idle_s == null ? "" : ` · idle ${esc(dur(ev.idle_s) ?? "")}`;
  s.innerHTML = `<span style="color:var(--pass)">feed live</span> <span style="color:var(--dim)">${ev.retained ?? 0} events${idle}</span>`;
}

function stageDone(ex) {
  const st = ex?.stages ?? [];
  return `${st.filter((x) => x.state === "done").length}/${st.length}`;
}

function paint() {
  paintLip();
  document.querySelectorAll(".dpane").forEach((p) => {
    p.classList.toggle("on", p.id === `pane-${ui.tab}`);
  });
  if (ui.tab === "events") paintEvents();
  if (ui.tab === "extraction") paintExtraction();
  if (ui.tab === "transcript") paintTranscript();
  paintControls();
}

// ── events pane ──────────────────────────────────────────────────────────────

const KINDS = ["tool", "file", "thinking", "error", "lifecycle"];
const MARK = { tool: "$", file: "~", thinking: "·", error: "!", lifecycle: "◦" };

function paintEvents() {
  const ev = latest?.events ?? null;
  const box = document.getElementById("sc-events");
  const fbox = document.getElementById("evfilters");
  if (!box) return;

  const counts = ev?.counts ?? {};
  fbox.innerHTML = KINDS.map((k) => {
    const n = counts[k] ?? 0;
    const on = ui.filters[k];
    return `<button class="chip ${on ? "on" : "off"}" data-kind="${k}">${MARK[k]} ${k} ${n}</button>`;
  }).join("") + `<span class="dwhy" style="margin-left:auto">${esc(feedNote(ev))}</span>`;

  if (!ev) {
    box.innerHTML = pad("control plane not enabled — the event feed is opt-in and currently off.");
    return;
  }
  if (!ev.connected) {
    box.innerHTML = pad(`event feed disconnected — ${esc(ev.reason ?? "no reason given")}`);
    return;
  }

  const rows = (ev.events ?? []).filter((e) => ui.filters[e.kind] !== false);
  if (!rows.length) {
    box.innerHTML = pad(
      ev.retained
        ? "every retained event is hidden by the active filters."
        : "connected, no events yet — nothing has happened in the session.",
    );
    return;
  }

  // Oldest-first: the feed reads as the transcript does, top to bottom.
  const wasFollowing = ui.follow;
  box.innerHTML = rows.map(evRow).join("");
  if (wasFollowing) box.scrollTop = box.scrollHeight;
}

function evRow(e) {
  const t = e.at ? new Date(e.at).toLocaleTimeString("en-GB", { hour12: false }) : "";
  const detail = e.kind === "file"
    ? `<span class="evdetail evpath">${esc(e.detail ?? "")}</span>`
    : `<span class="evdetail">${esc(e.detail ?? "")}</span>`;
  return `
    <div class="evrow ${esc(e.kind)}">
      <span class="evt">${esc(t)}</span>
      <span class="evmark">${MARK[e.kind] ?? "·"}</span>
      <span class="evname">${esc(e.name ?? "")}</span>
      ${detail}
    </div>`;
}

/**
 * The feed's own honesty line. `capped` and `windowed` are different facts and
 * are never collapsed: windowed means "more exist, ask for them", capped means
 * "events were DROPPED and are gone" — only the second is data loss.
 */
function feedNote(ev) {
  if (!ev) return "";
  const bits = [];
  if (ev.capped) bits.push(`ring full — oldest dropped (${ev.total} seen)`);
  else if (ev.returned < ev.retained) bits.push(`showing ${ev.returned} of ${ev.retained}`);
  if (ev.unmapped) bits.push(`${ev.unmapped} unmapped`);
  return bits.join(" · ");
}

function pad(msg) {
  return `<div style="padding:22px 18px"><div class="null" style="line-height:1.6;max-width:560px">${msg}</div></div>`;
}

// ── extraction pane ──────────────────────────────────────────────────────────

const STMARK = { done: "✓", running: "▸", failed: "✕", gated: "▚", pending: "·" };

function paintExtraction() {
  const ex = latest?.extraction ?? null;
  const box = document.getElementById("sc-extraction");
  if (!box) return;

  if (!ex) {
    box.innerHTML = pad("control plane not enabled — extraction state is unavailable.");
    return;
  }

  // ALL stages are shown from the start, including ones not yet reached. A
  // list that grows as it runs hides the shape of the work; showing the whole
  // pipeline up front makes "halted at stage 5 of 10" legible at a glance.
  const st = ex.stages ?? [];
  const head = `
    <div class="dfilters" style="border-bottom:1px solid var(--rule)">
      <span class="label" style="color:var(--type)">EXTRACTION — ${st.length} STAGES</span>
      <span class="dwhy">${esc(extractionHead(ex))}</span>
    </div>`;

  box.innerHTML = head + st.map(stRow).join("");
}

function extractionHead(ex) {
  if (ex.state === "idle") return "idle — no extraction started";
  const bits = [ex.state];
  if (ex.model) bits.push(ex.model);
  if (ex.elapsed_s != null) bits.push(dur(ex.elapsed_s) ?? "");
  if (ex.n_memories != null) bits.push(`${ex.n_memories} memories`);
  const gated = (ex.stages ?? []).find((s) => s.state === "gated");
  if (gated) bits.push(`HALTED AT ${gated.title.toUpperCase()}`);
  return bits.filter(Boolean).join(" · ");
}

function stRow(s) {
  const right = s.count != null ? String(s.count)
    : s.elapsed_s != null ? (dur(s.elapsed_s) ?? "")
    : s.state === "pending" ? "—" : "";
  // A gated stage's reason is the whole point of the gate, so it is never
  // clipped; every other detail is bounded to keep rows on the 44px grid.
  const detail = s.state === "gated"
    ? esc(s.detail ?? s.note ?? "")
    : esc(clip(s.detail ?? s.note ?? "", 96));
  return `
    <div class="strow ${esc(s.state)}">
      <span class="stmark">${STMARK[s.state] ?? "·"}</span>
      <span class="stname">${esc((s.title ?? s.id ?? "").toUpperCase())}</span>
      <span class="stdetail">${detail}</span>
      <span class="stright">${esc(right)}</span>
    </div>`;
}

// ── transcript pane ──────────────────────────────────────────────────────────

function paintTranscript() {
  const box = document.getElementById("sc-transcript");
  if (!box) return;
  const r = latest?.control?.run ?? latest?.run ?? null;
  const sid = r?.session_id ?? null;

  // The raw transcript is the session's own record. Until a live session is
  // observed there is nothing truthful to show, and inventing a placeholder
  // transcript would be worse than an empty pane.
  if (!sid) {
    box.innerHTML = pad("no session observed yet — the raw transcript appears once a cell is running.");
    return;
  }
  box.innerHTML = pad(
    `session <span style="color:var(--type)">${esc(sid)}</span><br><br>` +
    `raw transcript streaming is not wired yet. The EVENTS tab carries the ` +
    `live activity for this session in the meantime.`,
  );
}

// ── control region ───────────────────────────────────────────────────────────

function paintControls() {
  const box = document.getElementById("dctl");
  if (!box) return;
  const c = ctl();

  if (!c) {
    box.innerHTML = `<div class="dwhy">control plane not enabled.<br><br>Set <span style="color:var(--fg)">"control-plane": true</span> in the dashboard config and start it with <span style="color:var(--fg)">node control/server.mjs</span>.</div>`;
    return;
  }

  // Focus is never stolen mid-selection: if the operator has a dropdown open,
  // the region is left exactly as it is until they are done.
  const active = document.activeElement;
  if (box.contains(active) && active.tagName === "SELECT") return;

  box.innerHTML = ui.tab === "extraction" ? extractControls(c) : runControls(c);
}

function runControls(c) {
  const run = c.run ?? {};
  const roster = c.roster ?? {};
  const models = roster.models ?? [];
  const caps = c.capabilities ?? {};

  const eligible = models.filter((m) => m.eligible !== false);
  const modelOpts = eligible.length
    ? eligible.map((m) => opt(m.id, `${m.id}${m.resident ? " · resident" : ""}`, ui.sel.model)).join("")
    : `<option value="">no eligible model</option>`;

  const ctxOpts = (roster.context_choices ?? [65536, 131072, 262144])
    .map((n) => opt(String(n), `${Math.round(n / 1024)}K`, ui.sel.context))
    .join("");

  const canStart = run.can_start === true && eligible.length > 0;
  const why = run.blocked_reason
    ? `<div class="dwhy bad">${esc(run.blocked_reason)}</div>`
    : run.state === "running"
      ? `<div class="dwhy">a cell is running — runs are strictly serial, so start is unavailable until it finishes.</div>`
      : "";

  // Resume is refused up front, in the UI, with the real reason. Offering a
  // button that always 501s would waste the operator's time and imply a
  // capability the harness does not have.
  const resume = caps.resume?.supported === false
    ? `<div class="dwhy">RESUME unavailable — ${esc(caps.resume.reason ?? "")} Use <span style="color:var(--fg)">${esc(caps.resume.alternative ?? "archive_and_restart")}</span>.</div>`
    : "";

  return `
    <div class="dctl-row">
      <label>CONTROLS — RUN</label>
      <div class="dwhy">${esc(run.state ?? "unknown")}${run.log_name ? ` · ${esc(clip(run.log_name, 30))}` : ""}</div>
    </div>
    <div class="dctl-row">
      <label>MODEL</label>
      <select class="dsel" data-sel="model" ${canStart ? "" : "disabled"}>${modelOpts}</select>
    </div>
    <div class="dctl-row">
      <label>CONTEXT</label>
      <select class="dsel" data-sel="context" ${canStart ? "" : "disabled"}>${ctxOpts}</select>
    </div>
    ${ui.armed === "start" && ui.restatement ? `<div class="drestate">${esc(ui.restatement)}</div>` : ""}
    ${refusalHtml()}
    <button class="dbtn ${ui.armed === "start" ? "armed" : ""}" data-action="${ui.armed === "start" ? "start-confirm" : "start-arm"}" ${canStart && !ui.pending ? "" : "disabled"}>
      ${ui.pending ? "…" : ui.armed === "start" ? "CONFIRM — START RUN" : "START RUN →"}
    </button>
    ${why}
    ${resume}`;
}

function extractControls(c) {
  const ex = latest?.extraction ?? {};
  const run = c.run ?? {};
  const models = (c.roster?.models ?? []).filter((m) => m.eligible !== false);
  const busy = ex.state === "running";

  const modelOpts = [`<option value="">same as run${run.model ? ` — ${run.model}` : ""}</option>`]
    .concat(models.map((m) => opt(m.id, m.id, ui.sel.extractModel)))
    .join("");

  return `
    <div class="dctl-row">
      <label>CONTROLS — EXTRACT</label>
      <div class="dwhy">${esc(extractionHead(ex))}</div>
    </div>
    <div class="dctl-row">
      <label>MODEL</label>
      <select class="dsel" data-sel="extractModel" ${busy ? "disabled" : ""}>${modelOpts}</select>
    </div>
    <div class="dctl-row">
      <label>SOURCE</label>
      <div class="dwhy">${run.log_name ? esc(clip(run.log_name, 34)) : "no cell observed"}</div>
    </div>
    ${ui.armed === "extract" && ui.restatement ? `<div class="drestate">${esc(ui.restatement)}</div>` : ""}
    ${refusalHtml()}
    <button class="dbtn ${ui.armed === "extract" ? "armed" : ""}" data-action="${ui.armed === "extract" ? "extract-confirm" : "extract-arm"}" ${busy || ui.pending ? "disabled" : ""}>
      ${ui.pending ? "…" : ui.armed === "extract" ? "CONFIRM — EXTRACT" : "EXTRACT →"}
    </button>`;
}

function refusalHtml() {
  if (!ui.lastRefusal) return "";
  // A refusal is shown verbatim. The server's reason is the useful artifact;
  // paraphrasing it would strip exactly the detail needed to fix the cause.
  const r = ui.lastRefusal;
  return `<div class="dwhy bad">REFUSED — ${esc(r.code ?? "error")}<br>${esc(r.reason ?? "")}</div>`;
}

function opt(value, label, selected) {
  return `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`;
}

// ── actions ──────────────────────────────────────────────────────────────────

async function onAction(action) {
  const c = ctl();
  if (!c?.base_url) return;

  if (action === "start-arm" || action === "extract-arm") {
    const which = action === "start-arm" ? "start" : "extract";
    await preview(c.base_url, which);
    paint();
    return;
  }
  if (action === "start-confirm") { await send(c.base_url, "start"); return; }
  if (action === "extract-confirm") { await send(c.base_url, "extract"); return; }
}

/**
 * ARM step. The restatement and the confirmation token both come from the
 * SERVER — the browser never mints its own token, because a client-generated
 * confirmation confirms nothing the server can trust.
 */
async function preview(base, which) {
  ui.pending = true;
  ui.lastRefusal = null;
  paint();
  try {
    const body = which === "start"
      ? { model: ui.sel.model, context: Number(ui.sel.context) || undefined }
      : { model: ui.sel.extractModel || undefined };
    const res = await fetch(`${base}/api/run/preview`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: which, ...body }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || data?.ok === false) {
      ui.lastRefusal = { code: data?.code ?? `HTTP ${res.status}`, reason: data?.reason ?? "preview refused" };
      disarm();
    } else {
      ui.armed = which;
      ui.token = data?.confirmation_token ?? null;
      ui.restatement = data?.restatement ?? null;
    }
  } catch (err) {
    ui.lastRefusal = { code: "unreachable", reason: String(err?.message ?? err) };
    disarm();
  } finally {
    ui.pending = false;
  }
}

async function send(base, which) {
  if (!ui.token) { disarm(); paint(); return; }
  ui.pending = true;
  paint();
  const url = which === "start" ? `${base}/api/run/start` : `${base}/api/extraction/start`;
  const body = which === "start"
    ? { model: ui.sel.model, context: Number(ui.sel.context) || undefined, confirmation_token: ui.token }
    : { model: ui.sel.extractModel || undefined, confirmation_token: ui.token };
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || data?.ok === false) {
      ui.lastRefusal = { code: data?.code ?? `HTTP ${res.status}`, reason: data?.reason ?? "refused" };
    }
  } catch (err) {
    ui.lastRefusal = { code: "unreachable", reason: String(err?.message ?? err) };
  } finally {
    ui.pending = false;
    disarm();
    paint();
  }
}
