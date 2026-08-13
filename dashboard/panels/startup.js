// ─────────────────────────────────────────────────────────────────────────────
// PANEL: STARTUP FEED — the one surface that says what the benchmark is doing
//
// ── THE DEFECT THIS EXISTS TO KILL ──────────────────────────────────────────
//
// The operator clicked [+ baseline] and the benchmark did not start. That is
// half the failure. The other half — the half that made it expensive — is that
// NOTHING ON THE BOARD SAID SO. Every background process the startup depends on
// published its state somewhere in the board payload, and no surface collected
// them, so "it silently did nothing" was indistinguishable from "it is working
// and slow", from "the control plane is down", and from "the server refused".
//
// A refusal that reaches only `console.error` has not been reported. A run that
// arms and never confirms is invisible. This module is the answer: ONE
// derivation of EVERY background process behind a benchmark start, each with a
// state, and — when it is not ok — the REASON, in the publisher's own words.
//
// ── ONE DERIVATION, MANY READERS ────────────────────────────────────────────
//
// `startupFeed(board, lifecycle)` is pure: board payload in, process list out.
// It renders nothing and touches no DOM, so it is unit-testable under `node
// --test` and cannot drift from what the board shows. The renderer below is a
// thin projection of it. Any future surface that needs this answer calls the
// same function rather than re-deriving it — a second derivation is a second
// source of truth, and two of those disagree eventually.
//
// ── ABSENT IS NOT OK, AND NEVER SILENT ──────────────────────────────────────
//
// The shape follows `control/live-surface.mjs`, which already publishes
// `unwired` + `unwired_reasons` for exactly this reason: a surface that cannot
// report is reported AS not reporting, with the reason attached. Nothing here
// invents an "ok". A process whose state the board does not carry is `unknown`
// with that fact stated — never a green light by omission.
//
// ── STATES ──────────────────────────────────────────────────────────────────
//
//   ok       running / reachable / healthy
//   busy     working right now (a live cell, an in-flight request)
//   idle     wired and reachable, nothing to do — the NORMAL resting state
//   off      not running, and that is expected/optional (never an alarm)
//   bad      broken, refused, or unreachable — the operator must act
//   unknown  the board does not carry this fact; said out loud, never assumed ok
//
// `bad` is the only state that raises the feed on its own. `off` and `idle` are
// deliberately quiet: a board that cries wolf about an optional lane teaches the
// operator to ignore it, and then the real failure scrolls past unread.
// ─────────────────────────────────────────────────────────────────────────────

import { esc, nul } from "../board.js";

/** Ranked worst-first. Drives ordering and the single headline verdict. */
const SEVERITY = { bad: 0, unknown: 1, busy: 2, ok: 3, idle: 4, off: 5 };

/**
 * THE SINGLE DERIVATION.
 *
 * @param {object} board      the board payload
 * @param {object} lifecycle  the run-start lifecycle, from runstart.js. Passed
 *                            IN rather than imported so this module stays pure
 *                            and free of a circular import with the panel that
 *                            owns that state.
 * @returns {{processes: Array, verdict: string, blocking: Array, ok: boolean}}
 */
export function startupFeed(board, lifecycle = null) {
  const b = board ?? {};
  const processes = [
    controlPlane(b),
    runLifecycle(b, lifecycle),
    runner(b),
    modelProxy(b),
    eventFeed(b),
    tuiMirror(b),
    holdGate(b),
    extraction(b),
    profileGate(b),
  ];

  // Blocking = anything that must be fixed before a benchmark can start. It is
  // derived from `bad`, never hand-listed, so a new process cannot be added and
  // silently forgotten by the summary.
  const blocking = processes.filter((p) => p.state === "bad");
  return {
    processes,
    blocking,
    ok: blocking.length === 0,
    verdict: verdict(processes, blocking),
  };
}

function verdict(processes, blocking) {
  if (blocking.length) {
    return blocking.length === 1
      ? `1 process is blocking a benchmark start — ${blocking[0].name}`
      : `${blocking.length} processes are blocking a benchmark start`;
  }
  const busy = processes.filter((p) => p.state === "busy");
  if (busy.length) return `${busy.length} running · nothing is blocking a start`;
  const unknown = processes.filter((p) => p.state === "unknown");
  if (unknown.length) return `${unknown.length} unreported · no failure observed, but not everything could be checked`;
  return "all wired · idle and ready to start a benchmark";
}

// ── THE PROCESSES ────────────────────────────────────────────────────────────
// Each returns the SAME shape so the renderer never special-cases one:
//   { id, name, state, detail, reason, why }
// `why` states what this process does for a benchmark start — an operator who
// has to ask "what is a live lane" has not been told anything by a status dot.

/**
 * THE CONTROL PLANE — the only thing on the board that can start a run.
 * The board is read-only by construction; the BROWSER posts to :7718 directly.
 * So if this is not reachable FROM THE BROWSER, no button on the board works,
 * and that must be the first line the operator reads.
 */
function controlPlane(b) {
  const c = b.control ?? null;
  const base = c?.base_url ?? null;
  if (!base) {
    // WHY IT IS ABSENT MATTERS, AND THE BOARD ALREADY KNOWS.
    //
    // `control` is null for two very different reasons, and collapsing them
    // sends the operator to the wrong fix. Either the service is genuinely not
    // running, OR it is running and the dashboard's own read of it FAILED —
    // which is what `sources[control-plane]` records, with the reason.
    //
    // Observed live: the control plane answered every endpoint in ~2ms from
    // inside the container while the aggregate source timed out at 2000ms,
    // because /api/wall alone took 2062ms. "It is not running" would have been
    // a lie, and would have sent the operator to restart a healthy service.
    const src = (b.sources ?? []).find((s) => s.id === "control-plane");
    if (src && src.ok === false && src.reason) {
      return proc("control-plane", "control plane", "bad", "read failed", null,
        "the board cannot start a run itself — every start is posted by the browser to the control service.",
        `${src.reason}. The service may be running and healthy — this is the DASHBOARD's read of it failing, so the board has no control surface to offer and no button on it can act.`);
    }
    return proc("control-plane", "control plane", "bad", "not wired", null,
      "the board cannot start a run itself — every start is posted by the browser to the control service. Without it, no control on this board can do anything.",
      "the board does not know where the control plane is — it is not running, or the dashboard was started without it.");
  }
  // The loopback/remote split is the documented LAN failure: 127.0.0.1 from
  // another device is THAT device, so the POST never leaves the browser.
  const remote =
    c?.base_url_is_loopback === true &&
    typeof location !== "undefined" &&
    !["localhost", "127.0.0.1", "[::1]", "::1"].includes(location.hostname);
  if (remote) {
    return proc("control-plane", "control plane", "bad", base, null,
      "the board cannot start a run itself — every start is posted by the browser to the control service.",
      `this board is open at ${location.hostname}, but the control plane is published as ${base}. That address means THIS device, not the bench host, so a start request would never leave your machine. Tunnel it: ssh -L 7717:127.0.0.1:7717 -L 7718:127.0.0.1:7718 <user>@${location.hostname}`);
  }
  return proc("control-plane", "control plane", "ok", base, null,
    "the only surface that can start a run; the browser posts to it directly.");
}

/**
 * THE RUN LIFECYCLE — the operator's actual failure, made visible.
 *
 * This is the process that had NO surface at all. `[+ baseline]` armed a run and
 * the confirm step was never rendered, so the run sat ARMED forever and the
 * board looked identical to idle. A refused preview was worse: the reason was
 * written to a variable nothing painted.
 */
function runLifecycle(b, lc) {
  const why =
    "the arm → confirm → start handshake. A benchmark cell costs hours, so the server mints a token and restates what it will do; nothing starts until that restatement is confirmed.";

  if (!lc) {
    return proc("run-lifecycle", "run start (arm → confirm)", "unknown", null, null, why,
      "the run panel did not report its state to this feed");
  }
  if (lc.refusal) {
    // VERBATIM, WITH ITS CODE. This is the line that was invisible.
    return proc("run-lifecycle", "run start (arm → confirm)", "bad",
      `refused — ${lc.refusal.code ?? "error"}`, null, why,
      lc.refusal.reason ?? "the control plane refused without a stated reason");
  }
  if (lc.pending) {
    return proc("run-lifecycle", "run start (arm → confirm)", "busy", "request in flight", null, why);
  }
  if (lc.starting) {
    const s = lc.startedAt ? Math.round((Date.now() - lc.startedAt) / 1000) : 0;
    return proc("run-lifecycle", "run start (arm → confirm)", "busy",
      `starting · ${s}s`, null, why,
      "the start was accepted; waiting for the runner to write its log. This is normal, not a hang.");
  }
  if (lc.armed) {
    // ARMED AND WAITING IS THE FAILURE STATE THE OPERATOR HIT. Say so plainly,
    // and say where the confirm is — an armed run that is never confirmed never
    // starts, and that silence is what cost the run.
    return proc("run-lifecycle", "run start (arm → confirm)", "busy",
      `ARMED — awaiting confirmation${lc.model ? ` · ${lc.model}` : ""}${lc.arm ? ` · ${lc.arm.toUpperCase()}` : ""}`,
      null, why,
      "the server has approved these parameters and minted a token. NOTHING IS RUNNING YET — the benchmark starts only when you confirm in the RUN CONTROL panel. An armed run left unconfirmed never starts.");
  }
  return proc("run-lifecycle", "run start (arm → confirm)", "idle", "not armed", null, why);
}

/**
 * THE RUNNER — the python child the control plane spawns, and the cell it runs.
 * `control.run` is the control plane's own view of its launcher; `board.run` is
 * the manifest's view of the cell. They answer different questions and both are
 * shown, because a launcher that exited while a manifest still claims a live
 * cell is precisely the disagreement worth seeing.
 */
function runner(b) {
  const r = b.control?.run ?? null;
  const why =
    "the harness process that actually runs the cell — spawned by the control plane, writes the run log the whole board reads.";
  if (!r) {
    return proc("runner", "benchmark runner", "unknown", null, null, why,
      "the control plane did not report a run state");
  }
  const detail = [r.state ?? "unknown", r.log_name ? String(r.log_name) : null].filter(Boolean).join(" · ");
  if (r.state === "running") {
    return proc("runner", "benchmark runner", "busy", detail, null, why,
      r.log_silent_s != null && r.log_silent_s > 300
        ? `the log has been silent for ${r.log_silent_s}s — at high accumulated context this can be normal prefill, but it is worth watching`
        : null);
  }
  if (r.can_start === false) {
    return proc("runner", "benchmark runner", "busy", detail, null, why,
      r.blocked_reason ?? "a cell is already in flight — runs are strictly serial");
  }
  return proc("runner", "benchmark runner", "idle", detail || "idle", null, why,
    "no cell in flight; the runner is free to start one.");
}

/**
 * THE MODEL PROXY + ROSTER. `/api/run/start` refuses outright when the proxy is
 * unreachable, so a dead proxy is a hard block on starting — and an empty
 * bench-eligible roster is the same block wearing a different hat.
 */
function modelProxy(b) {
  const roster = b.control?.roster ?? null;
  const why =
    "the local relay the benchmarked model is reached through. A start is refused outright when it is unreachable.";
  if (!roster) {
    return proc("model-proxy", "model proxy + roster", "unknown", null, null, why,
      "the control plane did not report a roster");
  }
  if (roster.proxy_ok === false) {
    return proc("model-proxy", "model proxy + roster", "bad", "unreachable", null, why,
      roster.reason ?? "the model proxy is unreachable — /api/run/start refuses every run while this is true");
  }
  const bench = roster.bench_models ?? (roster.models ?? []).filter((m) => m.bench_eligible !== false);
  if (!bench.length) {
    return proc("model-proxy", "model proxy + roster", "bad", "no bench-eligible model", null, why,
      "no bench-eligible model in the roster — an interactive slot contends with live daily-driver use and produces a measurement that cannot be defended");
  }
  const resident = bench.filter((m) => m.resident);
  return proc("model-proxy", "model proxy + roster", "ok",
    `${bench.length} bench-eligible${resident.length ? ` · ${resident.length} resident` : ""}`, null, why,
    resident.length ? null : "no bench model is resident right now — the first call will load one, which takes time but is not a failure.");
}

/**
 * THE EVENT FEED (SSE). This is the board's live narrative. Disconnected, the
 * board goes quiet and looks wedged while the run may be perfectly healthy —
 * so the distinction has to be stated rather than inferred from silence.
 */
function eventFeed(b) {
  const e = b.events ?? null;
  const why =
    "the live event stream from the running agent — tool calls, files, grading. When it is down the board goes quiet even though the run may be fine.";
  if (!e) {
    return proc("event-feed", "event feed (SSE)", "unknown", null, null, why,
      "the board carries no event section");
  }
  if (e.connected === false) {
    // NOT `bad`: with no cell running there is no session to stream, which is
    // the normal resting state. Calling it broken here would cry wolf.
    const running = b.control?.run?.state === "running";
    return proc("event-feed", "event feed (SSE)", running ? "bad" : "off",
      "disconnected", null, why,
      running
        ? `${e.reason ?? "the event feed is disconnected"} — a cell IS running, so this is a real loss of visibility, not an idle stream`
        : `${e.reason ?? "disconnected"} — expected while no cell is running: there is no session to stream.`);
  }
  return proc("event-feed", "event feed (SSE)", e.total > 0 ? "ok" : "idle",
    `${e.total ?? 0} events`, null, why);
}

/**
 * THE TUI MIRROR — the pty capture whose window hosts this very feed.
 * Included deliberately: the feed lives inside the mirror, so the mirror's own
 * health is the one thing the operator cannot infer from looking at it.
 */
function tuiMirror(b) {
  const t = b.tui ?? null;
  const why =
    "a strictly read-only mirror of the run's terminal. It attaches to the session the runner opens; it never writes to the pty.";
  if (!t) {
    return proc("tui-mirror", "TUI mirror", "off", "not attached", null, why,
      "the control plane is not enabled, so no capture can be started");
  }
  if (t.status === "failed") {
    return proc("tui-mirror", "TUI mirror", "bad", "failed", null, why,
      t.reason ?? "could not attach to the session");
  }
  if (t.status === "starting") {
    return proc("tui-mirror", "TUI mirror", "busy", "attaching", null, why,
      t.reason ?? "first paint takes about 10 seconds — normal, not a hang");
  }
  if (t.status === "live") return proc("tui-mirror", "TUI mirror", "ok", "live", null, why);
  if (t.status === "silent") return proc("tui-mirror", "TUI mirror", "idle", "attached · silent", null, why, t.reason ?? null);
  if (t.status === "exited") return proc("tui-mirror", "TUI mirror", "off", "exited", null, why, t.reason ?? null);
  return proc("tui-mirror", "TUI mirror", "off", "not attached", null, why,
    t.reason ?? "no session observed yet — the mirror attaches when a run opens one");
}

/** THE HOLD GATE. A hold BLOCKS the run by design, and must never look like a crash. */
function holdGate(b) {
  const h = b.hold ?? null;
  const why = "a deliberate stop the harness places on a run — it blocks progress on purpose and waits for a human.";
  if (!h) return proc("hold-gate", "hold gate", "idle", "no hold", null, why);
  return proc("hold-gate", "hold gate", "bad", "HELD", null, why,
    (h.reason ?? "a hold is in place") + " — the run is stopped on purpose and will not continue until it is released.");
}

/** EXTRACTION — the post-run memory pipeline. Never blocks a start. */
function extraction(b) {
  const x = b.extraction ?? null;
  const why = "turns a finished cell's session into memories. It runs AFTER a cell, so it never blocks a start.";
  if (!x) return proc("extraction", "extraction pipeline", "unknown", null, null, why, "not reported");
  if (x.state === "running") return proc("extraction", "extraction pipeline", "busy", x.model ? `running · ${x.model}` : "running", null, why);
  if (x.status === "failed" || (x.exit_code != null && x.exit_code !== 0)) {
    return proc("extraction", "extraction pipeline", "bad", `failed${x.exit_code != null ? ` · exit ${x.exit_code}` : ""}`, null, why,
      x.reason ?? "extraction failed without a stated reason");
  }
  return proc("extraction", "extraction pipeline", "idle", x.state ?? "idle", null, why);
}

/**
 * THE PROFILE GATE. An OFF/baseline cell needs NO profile — that is the whole
 * point of a floor. An ON cell does. Stating that here is what stops an operator
 * concluding "no profile" means "cannot start", which is false for a baseline.
 */
function profileGate(b) {
  const p = b.profile ?? {};
  const why = "the frozen policy an ON cell runs under. A CONTROL/baseline cell needs no profile — the floor comes first.";
  if (!p.exists) {
    return proc("profile-gate", "memory profile", "idle", "none frozen", null, why,
      "no profile is frozen. This does NOT block a baseline: an OFF cell needs no profile. It blocks only a MEMORY ON cell.");
  }
  return proc("profile-gate", "memory profile", "ok",
    `${p.id ?? "frozen"}${p.subject_model ? ` · ${p.subject_model}` : ""}`, null, why);
}

function proc(id, name, state, detail, _unused, why, reason = null) {
  return { id, name, state, detail: detail ?? null, reason, why };
}

// ── RENDER ───────────────────────────────────────────────────────────────────

/**
 * THE FEED, AS DRAWN INSIDE THE TUI MIRROR.
 *
 * Ordered worst-first so the thing blocking a start is the first line read —
 * never sorted alphabetically or by declaration order, either of which buries a
 * `bad` under six healthy rows.
 */
export function renderStartupFeed(board, lifecycle = null) {
  const feed = startupFeed(board, lifecycle);
  const rows = [...feed.processes].sort((a, b2) => SEVERITY[a.state] - SEVERITY[b2.state]);

  return `
    <div class="sfeed">
      <div class="sfeed-head">
        <span class="kick">BENCHMARK STARTUP — BACKGROUND PROCESSES</span>
        <span class="spacer"></span>
        <span class="sfeed-verdict ${feed.ok ? "" : "bad"}">${esc(feed.verdict)}</span>
      </div>
      <div class="sfeed-rows">
        ${rows.map(row).join("")}
      </div>
      <div class="sfeed-foot">${esc(
        "This feed is replaced by the terminal the moment the mirror paints a live frame — it reports the startup, not the run.",
      )}</div>
    </div>`;
}

function row(p) {
  return `
    <div class="sfrow ${esc(p.state)}">
      <span class="sfdot ${esc(p.state)}"></span>
      <span class="sfname">${esc(p.name)}</span>
      <span class="sfstate">${esc(p.state.toUpperCase())}</span>
      <span class="sfdetail">${p.detail ? esc(p.detail) : nul("no detail reported")}</span>
      <span class="sfwhy">${esc(p.why)}</span>
      ${p.reason ? `<span class="sfreason ${p.state === "bad" ? "bad" : ""}">${esc(p.reason)}</span>` : ""}
    </div>`;
}
