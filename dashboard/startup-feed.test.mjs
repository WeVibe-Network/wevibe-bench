// ─────────────────────────────────────────────────────────────────────────────
// STARTUP FEED — the derivation, and the yield rule
//
//     cd wevibe-bench/dashboard && node --test
//
// WHY THIS EXISTS
//
// The operator clicked [+ baseline]; the benchmark did not start; and NOTHING
// on the board said so. Both halves were defects, and the second is the one
// that made it expensive — the failure existed, was written to `ui.refusal` and
// to `console.error`, and reached no surface.
//
// So the assertions here are about VISIBILITY, not about styling:
//
//   · a refusal must reach the feed VERBATIM, with its code
//   · ARMED-and-unconfirmed must be reported as a distinct, named state — it is
//     the exact state the failed click left behind, and it looked like idle
//   · a process the board does not report must read `unknown`, NEVER ok — a
//     green light by omission is the failure mode this whole surface exists to
//     remove
//   · the feed must YIELD to a real terminal frame, and must NOT yield to a
//     withheld one
//
// PURE BY CONSTRUCTION: `startupFeed()` takes a board payload and returns a
// list. No DOM, no fetch, no clock beyond Date.now(). That is what makes the
// derivation testable at all, and it is why the renderer is a thin projection
// of it rather than a second derivation.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";

import { startupFeed, renderStartupFeed } from "./panels/startup.js";
import { renderTui, toggleTui, isTuiExpanded } from "./panels/tui.js";

// The dock is minimized by default; the yield rule is about the EXPANDED grid,
// so the tests operate on the view the rule actually governs.
if (!isTuiExpanded()) toggleTui();

/** A board payload with everything healthy and idle. Each test breaks ONE thing. */
function healthyBoard(over = {}) {
  return {
    control: {
      base_url: "http://127.0.0.1:7718",
      base_url_is_loopback: true,
      run: { state: "idle", can_start: true },
      roster: { proxy_ok: true, bench_models: [{ id: "m1", bench_eligible: true, resident: true }] },
      capabilities: {},
    },
    run: {},
    events: { connected: true, total: 5 },
    tui: { running: false, status: null, frame: null },
    hold: null,
    live: { ok: true, running: false, unwired: ["live-lane"], unwired_reasons: { "live-lane": "no live-gates.json" } },
    extraction: { state: "idle" },
    profile: { exists: false },
    ...over,
  };
}

const byId = (feed, id) => feed.processes.find((p) => p.id === id);

test("every process reports a state, and nothing is silently omitted", () => {
  const feed = startupFeed(healthyBoard(), null);
  const ids = feed.processes.map((p) => p.id);
  // Pinned explicitly: a process quietly dropped from the derivation is
  // invisible in exactly the way this surface exists to prevent.
  for (const id of [
    "control-plane", "run-lifecycle", "runner", "model-proxy", "event-feed",
    "tui-mirror", "hold-gate", "extraction", "live-lane", "profile-gate",
  ]) {
    assert.ok(ids.includes(id), `${id} is missing from the startup feed`);
  }
  for (const p of feed.processes) {
    assert.ok(p.name && p.state && p.why, `${p.id} is missing name/state/why`);
    assert.ok(
      ["ok", "busy", "idle", "off", "bad", "unknown"].includes(p.state),
      `${p.id} has an undeclared state: ${p.state}`,
    );
  }
});

test("a healthy idle board blocks nothing and says so", () => {
  const feed = startupFeed(healthyBoard(), { armed: false, pending: false, refusal: null, startedAt: null, starting: false });
  assert.equal(feed.blocking.length, 0);
  assert.equal(feed.ok, true);
  assert.match(feed.verdict, /ready to start/i);
});

// ── THE OPERATOR'S ACTUAL FAILURE ───────────────────────────────────────────

test("a preview refusal reaches the feed VERBATIM, with its code", () => {
  const feed = startupFeed(healthyBoard(), {
    armed: false, pending: false, starting: false, startedAt: null,
    refusal: { code: "baseline_required", reason: "no scorable OFF cell exists for this subject" },
  });
  const lc = byId(feed, "run-lifecycle");
  assert.equal(lc.state, "bad");
  assert.match(lc.detail, /baseline_required/);
  // VERBATIM. Paraphrasing strips exactly the detail needed to fix the cause.
  assert.equal(lc.reason, "no scorable OFF cell exists for this subject");
  assert.equal(feed.ok, false);
  assert.ok(feed.blocking.some((p) => p.id === "run-lifecycle"));
});

test("ARMED-and-unconfirmed is named as its own state and says nothing is running", () => {
  // THIS IS THE STATE THE FAILED [+ baseline] CLICK LEFT BEHIND. It previously
  // looked identical to idle, which is why the operator could not tell that the
  // benchmark had not started.
  const feed = startupFeed(healthyBoard(), {
    armed: true, pending: false, starting: false, startedAt: null,
    refusal: null, model: "qwen3.6-35b-a3b-bench", arm: "off",
  });
  const lc = byId(feed, "run-lifecycle");
  assert.equal(lc.state, "busy");
  assert.match(lc.detail, /ARMED/);
  assert.match(lc.detail, /qwen3\.6-35b-a3b-bench/);
  assert.match(lc.reason, /NOTHING IS RUNNING YET/);
  // It must tell the operator WHERE to finish the job, not merely that it is armed.
  assert.match(lc.reason, /confirm/i);
});

test("a lifecycle the panel did not report reads unknown, never ok", () => {
  const feed = startupFeed(healthyBoard(), null);
  assert.equal(byId(feed, "run-lifecycle").state, "unknown");
  assert.match(feed.verdict, /unreported/);
});

// ── NEVER CRY WOLF, NEVER GREEN-LIGHT BY OMISSION ───────────────────────────

test("an absent live lane is off, not bad — its absence is the normal state", () => {
  const feed = startupFeed(healthyBoard(), null);
  const lane = byId(feed, "live-lane");
  assert.equal(lane.state, "off");
  assert.ok(!feed.blocking.includes(lane));
});

test("a disconnected event feed is off while idle but bad while a cell runs", () => {
  const idle = startupFeed(healthyBoard({ events: { connected: false, reason: "fetch failed", total: 0 } }), null);
  assert.equal(byId(idle, "event-feed").state, "off");

  const running = startupFeed(
    healthyBoard({
      events: { connected: false, reason: "fetch failed", total: 0 },
      control: { ...healthyBoard().control, run: { state: "running", can_start: false } },
    }),
    null,
  );
  // A cell IS running, so a dead stream is a real loss of visibility.
  assert.equal(byId(running, "event-feed").state, "bad");
});

test("no profile does NOT block a baseline, and the feed says so explicitly", () => {
  const feed = startupFeed(healthyBoard({ profile: { exists: false } }), null);
  const p = byId(feed, "profile-gate");
  assert.notEqual(p.state, "bad");
  // The operator must not conclude "no profile" means "cannot start".
  assert.match(p.reason, /does NOT block a baseline/i);
});

test("an unreachable model proxy blocks a start, with the reason attached", () => {
  const feed = startupFeed(
    healthyBoard({
      control: { ...healthyBoard().control, roster: { proxy_ok: false, reason: "connection refused on :4545" } },
    }),
    null,
  );
  const mp = byId(feed, "model-proxy");
  assert.equal(mp.state, "bad");
  assert.match(mp.reason, /connection refused on :4545/);
  assert.equal(feed.ok, false);
});

test("a missing control plane is bad and is rendered FIRST", () => {
  const board = healthyBoard({ control: null });
  const feed = startupFeed(board, null);
  assert.equal(byId(feed, "control-plane").state, "bad");
  // The verdict names the blocking process, so the operator reads WHAT is
  // wrong before any row.
  assert.match(feed.verdict, /blocking a benchmark start — control plane/);

  // Worst-first ordering: the blocking row is the first one rendered. Asserted
  // on the FIRST `.sfname` rather than on substring order, because a later
  // mention anywhere in the document would satisfy a looser check.
  const html = renderStartupFeed(board, null);
  const firstName = html.slice(html.indexOf("sfrow")).match(/sfname">([^<]+)/)[1];
  assert.equal(firstName, "control plane");
});

test("a hold is reported as a deliberate stop, not a crash", () => {
  const feed = startupFeed(healthyBoard({ hold: { reason: "integrity gate awaiting review" } }), null);
  const h = byId(feed, "hold-gate");
  assert.equal(h.state, "bad");
  assert.match(h.reason, /on purpose/);
});

// ── RENDERING ───────────────────────────────────────────────────────────────

test("the rendered feed escapes publisher prose rather than interpolating it raw", () => {
  const html = renderStartupFeed(healthyBoard(), {
    armed: false, pending: false, starting: false, startedAt: null,
    refusal: { code: "x", reason: "<img src=x onerror=alert(1)>" },
  });
  assert.ok(!html.includes("<img src=x"), "refusal prose reached the DOM unescaped");
  assert.match(html, /&lt;img/);
});

test("a control-plane READ failure is not reported as 'not running'", () => {
  // OBSERVED LIVE, 2026-08-13: every control endpoint answered in ~2ms from
  // inside the container while the dashboard's aggregate source timed out at
  // 2000ms, because /api/wall alone took 2062ms. Reporting that as "it is not
  // running" would send the operator to restart a perfectly healthy service.
  const feed = startupFeed(
    healthyBoard({
      control: null,
      sources: [{ id: "control-plane", ok: false, reason: "control-plane: timed out after 2000ms" }],
    }),
    null,
  );
  const cp = byId(feed, "control-plane");
  assert.equal(cp.state, "bad");
  assert.match(cp.reason, /timed out after 2000ms/);
  assert.match(cp.reason, /may be running and healthy/);
  assert.ok(!/it is not running/.test(cp.reason));
});

test("worst-first: a blocking row is rendered before a healthy one", () => {
  const html = renderStartupFeed(
    healthyBoard({ hold: { reason: "held for review" } }),
    { armed: false, pending: false, starting: false, startedAt: null, refusal: null },
  );
  assert.ok(html.indexOf("hold gate") < html.indexOf("live lane (optional)"));
});

// ── THE YIELD RULE ──────────────────────────────────────────────────────────
// "Once the TUI renders, the data feed for the benchmark process disappears
// since we no longer need to see it." Asserted through renderTui's real output,
// because the rule is only worth anything if the ACTUAL panel obeys it.

const FRAME = [[{ t: "hello from the pty", fg: 2 }]];

test("with no terminal frame, the mirror shows the startup feed", () => {
  const html = renderTui(healthyBoard({ tui: { status: null, frame: null } }), null);
  assert.match(html, /BENCHMARK STARTUP/);
});

test("a painted live frame DISPLACES the feed entirely", () => {
  const html = renderTui(healthyBoard({ tui: { status: "live", frame: FRAME } }), null);
  assert.ok(!html.includes("BENCHMARK STARTUP"), "the feed must yield to a live terminal frame");
  assert.match(html, /hello from the pty/);
});

test("a WITHHELD frame does not count as painted — the feed stays", () => {
  // The server drops the frame for a minimized client. Treating that as "the
  // terminal is up" would hide the feed behind a frame that was never sent.
  const html = renderTui(healthyBoard({ tui: { status: "live", frame: null, frame_withheld: true } }), null);
  assert.match(html, /BENCHMARK STARTUP/);
});

test("the feed RETURNS when the mirror fails or exits after painting", () => {
  // This is exactly when an operator needs the process list again: a mirror
  // that died mid-run is a question the last frame cannot answer.
  for (const status of ["failed", "exited"]) {
    const html = renderTui(healthyBoard({ tui: { status, frame: FRAME, reason: "pty closed" } }), null);
    assert.match(html, /BENCHMARK STARTUP/, `the feed must return on status=${status}`);
  }
});

test("a SILENT terminal keeps its frame — silence is a healthy, legible state", () => {
  const html = renderTui(healthyBoard({ tui: { status: "silent", frame: FRAME, reason: "no output" } }), null);
  assert.ok(!html.includes("BENCHMARK STARTUP"));
  assert.match(html, /hello from the pty/);
});
