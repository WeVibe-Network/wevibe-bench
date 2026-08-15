// ─────────────────────────────────────────────────────────────────────────────
// CONTROL PLANE TESTS — stdlib runner only
//
//   cd wevibe-bench/control && node --test
//
// WHAT THESE TESTS ARE FOR. Two of them are DRIFT tests that assert this JS
// agrees with the Python harness it describes. Those are the ones that matter
// most: every other property here is local to this directory and would be
// caught by reading it, but a context registry or an alias list that silently
// stops matching the program it claims to describe produces a UI that lies
// confidently. Drift fails loudly here or not at all.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, mkdirSync, rmSync, writeFileSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import {
  confirmationToken,
  restatement,
  EVENT_MAP,
  RESUME_UNSUPPORTED,
  EVENT_RING_MAX,
  EVENT_RENDER_CAP,
  GATE_STALL_THRESHOLD_S,
  refuse,
} from "./contract.mjs";
import { matchRuntime, DECLARED_CONTEXT, CONTEXT_CHOICES, RETIRED_ALIASES, readRoster } from "./roster.mjs";
import { readBaselines, BASELINES_FILE, isArchivedRun } from "./baselines.mjs";
import { manifestArgFor, campaignDirName } from "./campaign.mjs";
import { sessionIdFrom, terminalFrom, pidAlive, newestLog } from "./runstate.mjs";
import { mapEvent, EventRing } from "./events.mjs";
import { parseGateEvents, gradingStatus } from "./gate-events.mjs";
import { createProfile, transferOf } from "./profiles.mjs";
import { readModelsLedger } from "./models-ledger.mjs";
import {
  attemptRecords,
  foldGateStates,
  readStatusRecords,
  readWall,
  resolveRunDir,
} from "./wall.mjs";
import {
  assignIds,
  parsePlaywrightList,
  parseVitestList,
  suiteFingerprint,
  tierOf,
} from "../tasks/backgammon/gates/roster.mjs";
import {
  foldGateResults,
  normalizeStatus,
} from "../tasks/backgammon/gates/gate-results.mjs";
import {
  FEEDBACK_CONTRACT_VERSION,
  feedbackRows,
  normalizeMessage,
  readFeedback,
  readSidecar,
} from "./feedback.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const BENCH = join(HERE, "..");

test("DRIFT: declared context matches WORKER_MODEL_REGISTRY in config.py", () => {
  const src = readFileSync(join(BENCH, "wevibe_bench", "config.py"), "utf8");
  for (const [alias, ctx] of Object.entries(DECLARED_CONTEXT)) {
    const idx = src.indexOf(`"${alias}"`);
    assert.ok(idx > -1, `alias '${alias}' is not present in config.py WORKER_MODEL_REGISTRY`);
    // The context literal is written with an underscore separator in Python.
    const block = src.slice(idx, idx + 800);
    const pretty = String(ctx).replace(/\B(?=(\d{3})+(?!\d))/g, "_");
    assert.ok(
      block.includes(pretty) || block.includes(String(ctx)),
      `alias '${alias}' declares context ${ctx} here but config.py disagrees`,
    );
  }
});

test("ROSTER: a retired alias is refused bench eligibility even when the proxy blesses it", async () => {
  // `wevibe-bench-worker` maps upstream to `auto` — a cell run on it measures
  // whichever model happened to be resident and records no identity. The design
  // is retired, but the PROXY still advertises the alias with purpose
  // 'wevibe-bench' (its roster lives in another service). Eligibility computed
  // from purpose alone therefore kept offering it a [+ baseline] button.
  const proxy = {
    object: "list",
    data: [
      { id: "wevibe-bench-worker", upstream_model: "auto", purpose: "wevibe-bench" },
      { id: "qwen3.6-35b-a3b-bench", upstream_model: "Qwen3.6-35B-A3B-MLX-8bit", purpose: "wevibe-bench" },
    ],
  };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) =>
    new Response(JSON.stringify(String(url).includes("/v1/models") ? proxy : { data: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  try {
    const roster = await readRoster({ proxyUrl: "http://x", runtimeUrl: "http://y" });
    const byId = Object.fromEntries(roster.models.map((m) => [m.id, m]));

    assert.equal(byId["wevibe-bench-worker"].bench_eligible, false, "a retired alias is never bench-eligible");
    assert.match(byId["wevibe-bench-worker"].retired_reason, /resident behind the proxy/);
    // It is still LISTED — vanishing without explanation looks like the proxy
    // lost it, which is a different and false diagnosis.
    assert.equal(byId["wevibe-bench-worker"].purpose, "wevibe-bench");
    assert.equal(byId["qwen3.6-35b-a3b-bench"].bench_eligible, true);
    assert.equal(byId["qwen3.6-35b-a3b-bench"].retired_reason, null);
    assert.deepEqual(roster.bench_models.map((m) => m.id), ["qwen3.6-35b-a3b-bench"]);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("DRIFT: retired aliases match RETIRED_MODEL_ALIASES in config.py", () => {
  // The two sides cannot import each other (JS control plane, Python harness),
  // so they are pinned. A retirement declared on ONE side only is the dangerous
  // case: the CLI would refuse the alias while the board still offered it a
  // [+ baseline] button, or the reverse.
  const src = readFileSync(join(BENCH, "wevibe_bench", "config.py"), "utf8");
  const block = /RETIRED_MODEL_ALIASES: dict\[str, str\] = \{([\s\S]*?)\n\}/.exec(src);
  assert.ok(block, "RETIRED_MODEL_ALIASES not found in config.py");
  const pythonIds = [...block[1].matchAll(/^\s{4}"([^"]+)":/gm)].map((m) => m[1]);
  assert.deepEqual(
    Object.keys(RETIRED_ALIASES).sort(),
    pythonIds.sort(),
    "a retirement is declared on one side only — the bench and the board disagree about what may run",
  );
});

test("ROSTER: a retired alias declares no bench context", () => {
  // The two maps must not disagree: a declared context is a promise the bench
  // will run the alias at that window.
  for (const id of Object.keys(RETIRED_ALIASES)) {
    assert.equal(DECLARED_CONTEXT[id], undefined, `${id} is retired but still declares a bench context`);
    assert.match(RETIRED_ALIASES[id], /retired/i, "a retirement states its reason");
  }
});

// ── CONFIRMATION: a stale token must not validate ────────────────────────────

test("confirmation token is a pure function of the parameters", () => {
  const a = confirmationToken({ model: "m", arm: "off", org: null, context: null });
  const b = confirmationToken({ model: "m", arm: "off", org: null, context: null });
  assert.equal(a, b);
});

test("changing ANY parameter invalidates the token", () => {
  const base = { model: "m", arm: "off", org: null, context: null };
  const t = confirmationToken(base);
  assert.notEqual(t, confirmationToken({ ...base, model: "other" }));
  assert.notEqual(t, confirmationToken({ ...base, arm: "on" }));
  assert.notEqual(t, confirmationToken({ ...base, org: "org-1" }));
  assert.notEqual(t, confirmationToken({ ...base, context: 262144 }));
});

test("restatement names the arm in words, not just a code", () => {
  const on = restatement({ model: "m", arm: "on", org: "o", context: 262144 });
  assert.match(on, /MEMORY ON/);
  const off = restatement({ model: "m", arm: "off", org: null, context: null });
  assert.match(off, /CONTROL/);
  // A control cell must say the org is not applicable rather than silently
  // omitting the line — an absent line reads as an unanswered question.
  assert.match(off, /not applicable/);
});

// ── RESUME: the capability is declared false, with a reason ──────────────────

test("resume is unsupported and says why", () => {
  assert.equal(RESUME_UNSUPPORTED.supported, false);
  assert.match(RESUME_UNSUPPORTED.reason, /no mid-cell checkpoint/);
  assert.equal(RESUME_UNSUPPORTED.alternative, "archive_and_restart");
});

// ── EVENTS ───────────────────────────────────────────────────────────────────
//
// FIXTURES ARE REAL WIRE SHAPES. These were rebuilt from a live 45s capture
// against a running cell after the original fixtures — written from the `/doc`
// OpenAPI Event union — were found to describe events the pinned worker never
// emits. The schema advertises a full `session.next.*` family; the worker
// actually emits `message.part.updated` carrying a Part. Testing against the
// schema passed while production mapped 5 of 1635 events, so these fixtures
// must stay wire-shaped, never schema-shaped.

test("a failed tool call is mapped to the error kind, not the tool kind", () => {
  const ev = mapEvent({
    id: "evt_1",
    type: "message.part.updated",
    properties: {
      sessionID: "ses_1",
      part: {
        id: "prt_1", sessionID: "ses_1", type: "tool", tool: "edit",
        state: { status: "error", error: { data: { message: "boom" } } },
      },
    },
  });
  assert.equal(ev.kind, "error");
  assert.equal(ev.tool, "edit");
  assert.equal(ev.text, "boom");
});

test("a step is lifecycle, NOT error", () => {
  // A step finishing on `tool-calls` is the system working as designed.
  // Rendering it in the fail colour would read as alarm at the moment the
  // instrument is behaving correctly.
  const ev = mapEvent({
    id: "evt_2",
    type: "message.part.updated",
    properties: {
      sessionID: "ses_1",
      part: {
        id: "prt_2", sessionID: "ses_1", type: "step-finish",
        reason: "tool-calls", tokens: { input: 41000, output: 120 },
      },
    },
  });
  assert.equal(ev.kind, "lifecycle");
  assert.ok(ev.detail.includes("tool-calls"));
});

test("tool input is summarised, never dumped", () => {
  const big = "x".repeat(50000);
  const ev = mapEvent({
    id: "evt_3",
    type: "message.part.updated",
    properties: {
      sessionID: "ses_1",
      part: {
        id: "prt_3", sessionID: "ses_1", type: "tool", tool: "write",
        state: { status: "running", input: { filePath: "/a/b.ts", content: big } },
      },
    },
  });
  assert.equal(ev.text, "/a/b.ts");
  assert.ok(!String(ev.text).includes("xxxx"));
});

test("long tool input text is truncated and says so", () => {
  const ev = mapEvent({
    id: "evt_4",
    type: "message.part.updated",
    properties: {
      sessionID: "ses_1",
      part: {
        id: "prt_4", sessionID: "ses_1", type: "tool", tool: "bash",
        state: { status: "running", input: { command: "y".repeat(5000) } },
      },
    },
  });
  assert.equal(ev.truncated, true);
  assert.ok(ev.text.length <= 400);
});

test("the token stream is dropped, not rendered as one row per token", () => {
  // message.part.delta is ~83% of all traffic and carries no standalone
  // meaning — the completed part arrives separately. Mapping it would flood
  // the feed and push every real event out of the ring.
  const ev = mapEvent({
    id: "evt_5",
    type: "message.part.delta",
    properties: { sessionID: "ses_1", messageID: "msg_1", partID: "prt_1", field: "text", delta: "a" },
  });
  assert.equal(ev, null);
});

test("assistant prose is not an activity row", () => {
  // `text` parts belong in the TRANSCRIPT tab. In the EVENTS feed they would
  // drown the tool calls the feed exists to show.
  const ev = mapEvent({
    id: "evt_6",
    type: "message.part.updated",
    properties: {
      sessionID: "ses_1",
      part: { id: "prt_6", sessionID: "ses_1", type: "text", text: "hello" },
    },
  });
  assert.equal(ev, null);
});

test("an event with no timestamp keeps a null time, never a fabricated one", () => {
  // Stamping Date.now() on an event that never carried a time would fabricate
  // ordering evidence the feed then displays as fact.
  const ev = mapEvent({
    id: "evt_7",
    type: "message.part.updated",
    properties: {
      sessionID: "ses_1",
      part: { id: "prt_7", sessionID: "ses_1", type: "step-start" },
    },
  });
  assert.equal(ev.at, null);
});

test("an unmapped event is counted, never silently dropped", () => {
  const ring = new EventRing(10);
  ring.push({ id: "e", type: "some.future.event", properties: {} });
  const snap = ring.snapshot();
  assert.equal(snap.total, 1);
  assert.equal(snap.unmapped, 1);
  assert.equal(snap.events.length, 0);
  // An unmapped frame must NOT make the ring claim it dropped data.
  assert.equal(snap.capped, false, "unmapped frames are not lost renderable events");
});

// ── ADMIT: the out-of-ring rows, and the re-append defect ───────────────────
//
// Harness grading rows and the verbatim messages the model was sent are
// rebuilt FROM FILES on every poll. They are already in BoardEvent shape, so
// they cannot go through `push()`, and giving them a seq at request time from
// the ring's moving cursor made the same row arrive with a NEW seq every poll.
// The renderer appends anything with `seq > renderedSeq`, so it appended the
// same row again and again. Measured on a live run: one `task chunk
// (attempt 1)` came back as seq 706, then 713, then higher.

test("ADMIT: the same row keeps its seq no matter how far the ring advances", () => {
  const ring = new EventRing(50);
  const row = { id: "user-event:1", kind: "user", type: "user:chunk", name: "task chunk (attempt 1)" };

  const first = ring.admit(row);
  assert.ok(first, "the first admission returns the row");
  const assigned = first.seq;

  // The ring moves on, exactly as it does during a live run.
  for (let i = 0; i < 12; i += 1) {
    ring.push({ id: `e${i}`, type: "file.edited", properties: { file: `/f${i}` } });
  }

  // Every later poll re-offers the SAME row, rebuilt from the same file.
  for (let i = 0; i < 5; i += 1) {
    assert.equal(ring.admit(row), null, "a row already admitted is refused");
  }

  const rows = ring.snapshot({ limit: 100 }).events.filter((e) => e.id === "user-event:1");
  assert.equal(rows.length, 1, "it must appear EXACTLY once, however many polls occurred");
  assert.equal(rows[0].seq, assigned, "and its seq must never be recomputed");
});

test("ADMIT: an admitted seq can never collide with a pushed one", () => {
  // The reason admission goes through the ring's own counter rather than being
  // numbered from `cursor` at request time: a shared counter makes a collision
  // structurally impossible. A collision would make the renderer drop one of
  // the two rows, since it only ever appends strictly-increasing seqs.
  const ring = new EventRing(50);
  ring.push({ id: "a", type: "file.edited", properties: { file: "/a" } });
  ring.admit({ id: "harness:x", kind: "harness", type: "harness:gate-start" });
  ring.push({ id: "b", type: "file.edited", properties: { file: "/b" } });
  ring.admit({ id: "harness:y", kind: "harness", type: "harness:gate-end" });

  const seqs = ring.snapshot({ limit: 100 }).events.map((e) => e.seq);
  assert.deepEqual(seqs, [...new Set(seqs)], "no two rows share a seq");
  assert.deepEqual(seqs, [...seqs].sort((x, y) => x - y), "and the line stays monotonic");
});

test("ADMIT: a row with no identity is REFUSED, never admitted repeatedly", () => {
  // Without an id the row cannot be recognised next poll, so admitting it would
  // reproduce the exact re-append defect. Refusing is the honest failure: the
  // row is absent and traceable, rather than present five hundred times.
  const ring = new EventRing(10);
  assert.equal(ring.admit({ kind: "harness", type: "harness:gate-start" }), null);
  assert.equal(ring.admit({ id: "", kind: "harness" }), null);
  assert.equal(ring.snapshot().events.length, 0);
});

test("ADMIT: `since` excludes an already-rendered row, so it is sent once", () => {
  // The client's incremental contract. Once it has rendered up to `cursor`, the
  // admitted row must not come back on the next request.
  const ring = new EventRing(50);
  ring.admit({ id: "user-event:1", kind: "user", type: "user:chunk" });
  const first = ring.snapshot({ limit: 100 });
  assert.equal(first.events.length, 1);

  ring.admit({ id: "user-event:1", kind: "user", type: "user:chunk" });
  const next = ring.snapshot({ limit: 100, since: first.cursor });
  assert.equal(next.events.length, 0, "nothing new — the row is already on screen");
});

test("the ring is bounded and reports that it capped", () => {
  const ring = new EventRing(5);
  for (let i = 0; i < 20; i += 1) {
    ring.push({ id: `e${i}`, type: "file.edited", properties: { file: `/f${i}` } });
  }
  const snap = ring.snapshot();
  assert.equal(snap.retained, 5);
  assert.equal(snap.total, 20);
  assert.equal(snap.capped, true);
});

test("the feed is OLDEST-FIRST — it is a transcript, not a ticker", () => {
  // A reversed transcript is unreadable as narrative, and a reasoning delta
  // rendered above the tool call it preceded actively misleads.
  const ring = new EventRing(50);
  ring.push({ id: "a", type: "file.edited", properties: { file: "/first" } });
  ring.push({ id: "b", type: "file.edited", properties: { file: "/second" } });
  const snap = ring.snapshot();
  assert.equal(snap.order, "oldest_first");
  assert.equal(snap.events[0].file, "/first");
  assert.equal(snap.events[1].file, "/second");
});

test("per-kind counts survive an active filter", () => {
  // The filter chips must keep showing "THINKING 768" while thinking is hidden.
  // If counts were computed over the returned slice, switching a filter on
  // would zero its own count and the operator would lose the number that says
  // what they are hiding.
  const ring = new EventRing(50);
  for (let i = 0; i < 3; i += 1) {
    ring.push({
      id: `t${i}`,
      type: "message.part.updated",
      properties: { sessionID: "ses_1", part: { id: `prt_${i}`, type: "reasoning", time: { start: 1, end: 2000 } } },
    });
  }
  ring.push({ id: "f", type: "file.edited", properties: { file: "/one" } });

  const filtered = ring.snapshot({ kinds: ["file"] });
  assert.equal(filtered.events.length, 1);
  assert.equal(filtered.counts.thinking, 3, "thinking count must survive being filtered out");
  assert.equal(filtered.counts.file, 1);
  assert.equal(filtered.hidden_by_filter, 3);
});

test("a render window is not reported as data loss", () => {
  // `capped` means the ring DROPPED events. `windowed` means this response
  // merely returned fewer than the ring holds. Conflating them would tell the
  // operator data was lost when it was only paged.
  const ring = new EventRing(100);
  for (let i = 0; i < 10; i += 1) {
    ring.push({ id: `e${i}`, type: "file.edited", properties: { file: `/f${i}` } });
  }
  const snap = ring.snapshot({ limit: 4 });
  assert.equal(snap.capped, false, "nothing was dropped from the ring");
  assert.equal(snap.windowed, true, "but the response was windowed");
  assert.equal(snap.events.length, 4);
});

test("retention exceeds the render cap so filters stay honest", () => {
  // If retention == render cap, filtering to ERROR would show only the errors
  // inside the last 400 events rather than the last 400 errors.
  assert.ok(
    EVENT_RING_MAX > EVENT_RENDER_CAP,
    `ring retention ${EVENT_RING_MAX} must exceed render cap ${EVENT_RENDER_CAP}`,
  );
});

test("events are filterable by kind", () => {
  const ring = new EventRing(50);
  ring.push({ id: "a", type: "file.edited", properties: { file: "/one" } });
  ring.push({ id: "b", type: "message.part.updated", properties: { sessionID: "ses_1", part: { id: "prt_b", type: "reasoning", time: { start: 1, end: 2000 } } } });
  const files = ring.snapshot({ kinds: ["file"] });
  assert.equal(files.events.length, 1);
  assert.equal(files.events[0].file, "/one");
});

// ── RUN STATE ────────────────────────────────────────────────────────────────

test("session id is read from what the runner already publishes", () => {
  assert.equal(sessionIdFrom("blah session_id=ses_abc123 more"), "ses_abc123");
  assert.equal(sessionIdFrom("opencode attach http://x --session ses_XYZ"), "ses_XYZ");
  assert.equal(sessionIdFrom("nothing here"), null);
});

test("terminal status is read from the runner's own last JSON line", () => {
  const t = terminalFrom('noise\n{"status":"awaiting_extract","memory_mode":"off"}\n');
  assert.equal(t.status, "awaiting_extract");
  assert.equal(terminalFrom("no json at all"), null);
});

test("pidAlive is honest about a pid that cannot exist", () => {
  assert.equal(pidAlive(0), false);
  assert.equal(pidAlive(-1), false);
  assert.equal(pidAlive(process.pid), true);
});

// ── ROSTER ───────────────────────────────────────────────────────────────────

test("runtime matching survives the two services' different naming", () => {
  // The proxy says `Qwen3.6-35B-A3B-MLX-8bit`; the runtime says
  // `qwen/qwen3.6-35b-a3b`. A failed match must return null, never a guess.
  const idx = new Map([
    ["qwen/qwen3.6-35b-a3b", { state: "loaded", max_context: 262144, loaded_context: 262144 }],
  ]);
  const hit = matchRuntime("Qwen3.6-35B-A3B-MLX-8bit", idx);
  assert.ok(hit, "expected the proxy alias to match the runtime entry");
  assert.equal(hit.loaded_context, 262144);
  assert.equal(matchRuntime("something-entirely-else", idx), null);
  assert.equal(matchRuntime(null, idx), null);
});

test("context choices are offered as an explicit list", () => {
  assert.ok(CONTEXT_CHOICES.includes(262144));
  assert.ok(CONTEXT_CHOICES.every((n) => Number.isInteger(n) && n > 0));
});

// ── REFUSALS ─────────────────────────────────────────────────────────────────

test("every refusal carries a human-readable reason", () => {
  const r = refuse("run_in_flight", "a cell is running");
  assert.equal(r.ok, false);
  assert.equal(r.code, "run_in_flight");
  assert.ok(r.reason.length > 0);
});

// ── GUARD: preview must validate the same parameters start does ──────────────
//
// REAL DEFECT, 2026-08-12. /api/run/preview minted a confirmation token for ANY
// payload without validating it. An ON cell with no org returned 200, so the UI
// armed a confirm button for a run that /api/run/start would then refuse with
// `org_required` — the refusal arrived AFTER the operator committed, which is
// the one place it is useless.
//
// server.mjs calls listen() at import, so it cannot be imported into a test
// process. These assertions read the source instead. That is weaker than
// calling the function, and it is chosen deliberately: a source assertion that
// pins the two load-bearing details is worth more than no guard at all on a bug
// that already shipped once.
const SERVER_SRC = readFileSync(join(HERE, "server.mjs"), "utf8");

function previewHandlerSource() {
  const start = SERVER_SRC.indexOf('path === "/api/run/preview"');
  assert.notEqual(start, -1, "the /api/run/preview route disappeared");
  const end = SERVER_SRC.indexOf('path === "/api/run/start"', start);
  assert.notEqual(end, -1, "could not find the end of the preview handler");
  return SERVER_SRC.slice(start, end);
}

test("preview validates parameters through the same path as start", () => {
  const src = previewHandlerSource();
  assert.match(
    src,
    /validateStart\(/,
    "preview no longer calls validateStart — it can once again mint a token for a run start would refuse",
  );
});

test("preview does not demand the token it exists to mint", () => {
  const src = previewHandlerSource();
  assert.match(
    src,
    /requireConfirm:\s*false/,
    "preview must pass requireConfirm:false — requiring the confirmation there is " +
      "circular and refuses every valid preview with bad_confirmation",
  );
});

test("preview reports the serial gate without refusing on it", () => {
  const src = previewHandlerSource();
  // can_start is a fact about NOW, not about the parameters. Reviewing the next
  // cell while one is in flight must stay possible, so preview overrides the
  // gate for validation and surfaces it as a separate advisory field.
  assert.match(src, /can_start:\s*true/, "preview must not refuse on the serial gate");
  assert.match(src, /blocked_now/, "preview must still surface that a run is in flight");
});

test("start still requires the confirmation token", () => {
  const startIdx = SERVER_SRC.indexOf('path === "/api/run/start"');
  assert.notEqual(startIdx, -1, "the /api/run/start route disappeared");
  const src = SERVER_SRC.slice(startIdx, startIdx + 1600);
  assert.doesNotMatch(
    src,
    /requireConfirm:\s*false/,
    "start must NEVER skip confirmation — that is what makes the second click meaningful",
  );
});

test("completed sessions stamp complete_gate and never extracted_from", () => {
  const src = readFileSync(join(BENCH, "wevibe_bench", "cumulative", "sequencer.py"), "utf8");
  assert.match(src, /session\.complete_gate = True/);
  assert.doesNotMatch(src, /session\.extracted_from/);
});

// ─────────────────────────────────────────────────────────────────────────────
// GRADING VISIBILITY (WO-GRADE-VIS-1)
//
// Between attempts the agent is idle BY DESIGN while the harness grades. The
// worker's event stream correctly says nothing, so before this existed the feed
// went silent for the length of a grade — measured at 32 minutes on 2026-08-12,
// during which a slow grade was indistinguishable from a wedged one.
// ─────────────────────────────────────────────────────────────────────────────

test("gate events are parsed from the harness's own PROGRESS lines", () => {
  const rows = parseGateEvents(
    "2026-08-12 02:21:58,778 INFO run_cumulative PROGRESS run_label=x step=gate-attempt-start attempt=3 target=/wt\n" +
    "2026-08-12 02:22:01,000 INFO run_cumulative PROGRESS step=gate-phase-start phase=conformance log=/a.log\n" +
    "2026-08-12 02:22:08,000 INFO run_cumulative PROGRESS step=gate-phase-end phase=conformance status=fail problems=2 log=/a.log\n",
  );
  assert.equal(rows.length, 3);
  assert.equal(rows[0].kind, "harness");
  assert.equal(rows[1].phase, "conformance");
  assert.match(rows[2].detail, /conformance fail · 2 problems/);
});

// ─────────────────────────────────────────────────────────────────────────────
// EVENT SEQ CONTRACT — the defect that made the harness feed invisible.
//
// Gate rows and feedback rows are built OUTSIDE EventRing, so they never pass
// through push() — the only place `seq` is assigned. They reached the client
// with `seq: undefined`, and the renderer appends incrementally with
//   rows.filter((e) => (e.seq ?? -1) > renderedSeq)          [panels/live.js]
// so every one scored -1 and NOTHING was ever appended. Observed live: a
// harness filter chip counting 282 events beside a completely empty feed.
// ─────────────────────────────────────────────────────────────────────────────

test("EVENTS: appended gate/feedback rows carry a seq that CONTINUES the ring", () => {
  // The exact merge the endpoint performs. Restarting the numbering at 0 would
  // place these rows at or below the client's cursor and reproduce the silence,
  // so the assertion is specifically that they continue PAST the ring cursor.
  const ringCursor = 40;
  const appended = [
    { kind: "harness", type: "harness:gate-phase-start" },
    { kind: "harness", type: "harness:gate-phase-end" },
    { kind: "user", type: "user:chunk" },
  ];

  let tailSeq = ringCursor;
  const sequenced = appended.map((r) => ({ ...r, seq: (tailSeq += 1) }));

  assert.deepEqual(sequenced.map((r) => r.seq), [41, 42, 43]);
  assert.ok(
    sequenced.every((r) => Number.isInteger(r.seq)),
    "a row without an integer seq can never pass the renderer's append filter",
  );
  assert.ok(
    sequenced.every((r) => (r.seq ?? -1) > ringCursor),
    "appended rows must sort AFTER the ring's own rows, never at 0",
  );
  assert.equal(tailSeq, 43, "the reported cursor must cover the appended rows");
});

test("EVENTS: a row with no seq is invisible to the renderer's append filter", () => {
  // Encodes WHY the bug was silent, so a future change that drops seq fails
  // here with the reason rather than shipping an empty feed again.
  const renderedSeq = 0; // the state after any first paint
  const unsequenced = [{ kind: "harness" }, { kind: "harness" }];
  const fresh = unsequenced.filter((e) => (e.seq ?? -1) > renderedSeq);
  assert.equal(fresh.length, 0, "this is the defect: real rows, none renderable");
});

test("the doubled PROGRESS emission yields ONE row, not two", () => {
  // Every PROGRESS line is emitted twice by the harness — once through the
  // structured logger and once bare. Verified on disk; the dashboard's run-log
  // source carries the same dedupe for the same reason. Without this the whole
  // grading feed renders visibly doubled.
  const line = "PROGRESS step=gate-phase-start phase=backend log=/a.log";
  const rows = parseGateEvents(
    `2026-08-12 02:22:01,000 INFO run_cumulative run_cumulative.progress ${line}\n` +
    `2026-08-12 02:22:01,000 INFO run_cumulative ${line}\n`,
  );
  assert.equal(rows.length, 1, "duplicate emission must collapse to one row");
});

test("a FAILING gate phase is never rendered as an error", () => {
  // Gates failing is the normal, expected measurement outcome — the benchmark
  // exists to observe it. Colouring it as an error would make a healthy run
  // look broken and train the operator to ignore real errors.
  const [row] = parseGateEvents(
    "PROGRESS step=gate-phase-end phase=frontend status=fail problems=7 log=/a.log\n",
  );
  assert.equal(row.kind, "harness");
  assert.notEqual(row.kind, "error");
});

test("a gate TIMEOUT is an error — the attempt was never graded", () => {
  const [row] = parseGateEvents(
    "PROGRESS step=gate-timeout wall_s=3600.0 limit_s=3600 log=/a.log\n",
  );
  assert.equal(row.kind, "error");
  assert.match(row.detail, /never graded|not graded/);
});

test("grading status pairs phase START with END so an in-phase hang is visible", () => {
  const open = parseGateEvents("PROGRESS step=gate-phase-start phase=backend log=/a.log\n");
  const s1 = gradingStatus(open, { logMtimeMs: Date.now() - 700_000 });
  assert.equal(s1.grading, true, "an unclosed phase means grading is still in flight");
  assert.equal(s1.phase, "backend");
  assert.equal(s1.stalled, true, "700s past a 600s threshold is a stall");

  const closed = parseGateEvents(
    "PROGRESS step=gate-phase-start phase=backend log=/a.log\n" +
    "PROGRESS step=gate-phase-end phase=backend status=pass problems=0 log=/a.log\n",
  );
  const s2 = gradingStatus(closed, { logMtimeMs: Date.now() - 700_000 });
  assert.equal(s2.grading, false, "a closed phase is not grading");
  assert.equal(s2.silent_s, null, "no elapsed figure when nothing is open");
  assert.equal(s2.stalled, false, "an idle harness must never raise a stall alarm");
});

test("the stall ALARM fires well before the harness's destructive timeout", () => {
  // DRIFT TEST. Two different jobs: the alarm is a visual signal that must fire
  // early so a human can look; the timeout is a kill that must fire late so it
  // never truncates a slow-but-working grade. If these ever cross, the gate is
  // killed before the operator is ever told anything was wrong.
  const py = readFileSync(join(BENCH, "wevibe_bench", "adapters", "backgammon.py"), "utf8");
  const m = /DEFAULT_GATE_TIMEOUT_S\s*=\s*(\d+)/.exec(py);
  assert.ok(m, "DEFAULT_GATE_TIMEOUT_S vanished from backgammon.py");
  const timeout = Number(m[1]);
  assert.ok(
    GATE_STALL_THRESHOLD_S < timeout,
    `alarm (${GATE_STALL_THRESHOLD_S}s) must fire before the kill (${timeout}s)`,
  );
});

test("the harness streams gate output instead of buffering it", () => {
  // DRIFT TEST against the Python. A buffered gate writes ZERO bytes until it
  // exits, which is what made a 32-minute grade invisible. If this regresses to
  // capture_output the entire feature is silently dead while still "passing".
  const py = readFileSync(join(BENCH, "wevibe_bench", "adapters", "backgammon.py"), "utf8");
  const fn = py.slice(py.indexOf("def _run_gate_report"), py.indexOf("def _kill_process_group"));
  assert.ok(fn.length > 0, "_run_gate_report vanished");
  // Strip the docstring before asserting: it deliberately NAMES the old
  // buffered call to explain why streaming exists, and matching prose instead
  // of code would make this test fail on its own documentation.
  const code = fn.replace(/"""[\s\S]*?"""/g, "");
  assert.doesNotMatch(code, /capture_output\s*=\s*True/, "gate output must not be buffered");
  assert.match(code, /start_new_session\s*=\s*True/, "gate must own a process group so its tree can be killed");

  // The flush that matters is the one INSIDE the reader loop. Asserting a bare
  // `log_file.flush()` anywhere is too weak: the header and footer flush too,
  // so the assertion still passed with the per-line flush deleted (verified by
  // injecting exactly that regression). Scope the match to the loop body.
  const loop = code.slice(code.indexOf("for line in proc.stdout"));
  assert.ok(loop.length > 0, "the streaming reader loop vanished");
  const loopBody = loop.slice(0, loop.indexOf("proc.wait("));
  assert.match(
    loopBody,
    /log_file\.write\(line\)[\s\S]*?log_file\.flush\(\)/,
    "each streamed line must be flushed AS IT IS READ — an unflushed buffer " +
      "reintroduces exactly the invisibility this feature removes",
  );
});

// ── PROFILE STORE + RUN START (WO-BOARD-PROFILE-1) ───────────────────────────

test("a profile is frozen: the store exposes no update path", () => {
  // FROZEN IS AN ABSENCE, NOT A DISABLED BUTTON. Every run in a stack is
  // measured under one recall policy, so a profile edited at run 4 would make
  // runs 1-3 incomparable to 4-N and the transfer curve would be a line drawn
  // through two different experiments.
  //
  // An `update()` that no caller uses is still a loaded gun: the next person to
  // need "just one small edit" finds it and the invariant dies quietly. This
  // asserts the capability does not exist at all.
  const src = readFileSync(join(BENCH, "control", "profiles.mjs"), "utf8");
  const exported = [...src.matchAll(/export\s+(?:async\s+)?function\s+(\w+)/g)].map((m) => m[1]);
  for (const banned of ["updateProfile", "editProfile", "setProfile", "patchProfile", "deleteProfile"]) {
    assert.ok(
      !exported.includes(banned),
      `profiles.mjs exports ${banned} — a frozen record must have no write path beyond creation`,
    );
  }
  assert.ok(exported.includes("createProfile"), "createProfile vanished");
  assert.ok(exported.includes("readProfiles"), "readProfiles vanished");
});

test("the memory roster is stored DECLARED, never enforced", () => {
  // No recall request carries a producer-model allowlist: producer_model_id is
  // written to the payload and read back, but no consumer filters on it.
  // Persisting the roster to disk did NOT change that. If `enforced` ever goes
  // true here without the backend filter, every ON result becomes
  // unattributable — the operator would credit a result to a curated subset
  // that was never curated.
  //
  // The lookbehind matters: `subject_enforced: true` is a DIFFERENT and correct
  // claim (the subject IS enforced, at run start). Only the bare `enforced` key
  // — which refers to the memory roster — is pinned false.
  const src = readFileSync(join(BENCH, "control", "profiles.mjs"), "utf8");
  assert.match(src, /(?<![\w_])enforced:\s*false/, "the stored profile must record enforced:false");
  assert.ok(
    !/(?<![\w_])enforced:\s*true/.test(src),
    "profiles.mjs sets enforced:true — recall does not filter by producing model",
  );
});

test("a profile freezes TWO axes and refuses either one missing", async () => {
  // The first version froze a single flat `models[]` allowlist, which conflated
  // the MEASUREMENT (the OFF→ON subject) with the EXPERIMENT VARIABLE (whose
  // memories get injected). The consequence was not cosmetic: with no subject
  // recorded, the model being measured was decided by whatever the operator
  // later picked in the run-start dropdown, and the board could not say whether
  // a run was same-model or cross-model because it never asked.
  const root = mkdtempSync(join(tmpdir(), "wevibe-prof-"));

  const noSubject = await createProfile(root, { memoryModels: ["m-a"] });
  assert.equal(noSubject.ok, false);
  assert.equal(noSubject.code, "profile_no_subject");
  assert.match(noSubject.reason, /subject/i, "the refusal must name what is missing");

  const noRoster = await createProfile(root, { subjectModel: "m-a", memoryModels: [] });
  assert.equal(noRoster.ok, false);
  assert.equal(noRoster.code, "profile_empty");

  // The two refusals must be DISTINGUISHABLE. One code for both would tell an
  // operator who forgot the subject to go fix their roster.
  assert.notEqual(noSubject.code, noRoster.code);

  const ok = await createProfile(root, { subjectModel: "m-a", memoryModels: ["m-a"] });
  assert.equal(ok.ok, true);
  assert.equal(ok.profile.subject_model, "m-a");
  assert.deepEqual(ok.profile.memory_models, ["m-a"]);
  rmSync(root, { recursive: true, force: true });
});

test("the subject is NOT auto-added to the memory roster", async () => {
  // A profile qualifying only FOREIGN memories is a legitimate experiment: the
  // subject recalls nothing it authored. Auto-ticking the subject would convert
  // that into a mixed run behind the operator's back, and would make same-model
  // the silent default rather than a visible choice.
  const root = mkdtempSync(join(tmpdir(), "wevibe-prof-"));

  const res = await createProfile(root, { subjectModel: "m-a", memoryModels: ["m-b"] });
  assert.equal(res.ok, true);
  assert.deepEqual(res.profile.memory_models, ["m-b"], "the subject must not be inserted");
  assert.equal(res.profile.transfer.kind, "cross");
  assert.equal(res.profile.transfer.self, false);
  rmSync(root, { recursive: true, force: true });
});

test("transfer direction is INFERRED from the two axes, never declared", async () => {
  // There is no direction field, no rank input, and no picker. Saying which of
  // two models is "greater" is a claim about relative capability, and the only
  // honest basis this bench could have is each model's own OFF floor on this
  // task — which does not exist at freeze time. So `self` (pure identity, and
  // the base measurement running today) resolves completely, and every
  // cross-model edge reports `unranked` rather than guessing.

  const self = transferOf({ subject_model: "m-a", memory_models: ["m-a"] });
  assert.equal(self.kind, "self");
  assert.equal(self.direction, "same", "same-model needs no rank — up/down does not apply");

  const cross = transferOf({ subject_model: "m-a", memory_models: ["m-b", "m-c"] });
  assert.equal(cross.kind, "cross");
  assert.equal(cross.direction, "unranked");
  assert.deepEqual(cross.foreign, ["m-b", "m-c"]);

  const mixed = transferOf({ subject_model: "m-a", memory_models: ["m-a", "m-b"] });
  assert.equal(mixed.kind, "mixed", "own + foreign is neither self nor pure cross");
  assert.equal(mixed.direction, "unranked");
  assert.equal(mixed.self, true);

  // A direction is NEVER invented for a cross edge, whatever the model names.
  for (const d of [cross.direction, mixed.direction]) {
    assert.ok(!/up|down|greater|weaker/i.test(d), `direction '${d}' asserts a gradient the bench never measured`);
  }
});

test("the transfer edge is derived on read, never written to the frozen file", async () => {
  // Derived rather than frozen because the rank basis accumulates AFTER
  // creation. A direction written at freeze time would be a guess that hardens
  // into a permanent record — and a frozen file is never rewritten to correct
  // it.
  const root = mkdtempSync(join(tmpdir(), "wevibe-prof-"));

  const res = await createProfile(root, { subjectModel: "m-a", memoryModels: ["m-a"] });
  const onDisk = JSON.parse(readFileSync(join(root, "profiles", `${res.profile.id}.json`), "utf8"));
  assert.equal(onDisk.transfer, undefined, "transfer must not be persisted — it is derived");
  assert.equal(onDisk.subject_model, "m-a", "the two frozen facts ARE persisted");
  assert.deepEqual(onDisk.memory_models, ["m-a"]);
  rmSync(root, { recursive: true, force: true });
});

test("run start refuses a cell whose model is not the profile's subject", () => {
  // OFF is the floor for ON, so both arms must be the same model. The harness
  // catches a swap eventually and expensively — a model change alters the
  // roster hash, invalidating the manifest (RUNBOOK §0, archive-and-rerun) —
  // i.e. after the cell has burned hours. This turns that into a refusal at
  // launch, and it is the ONE half of the profile that is actually enforced.
  const src = readFileSync(join(BENCH, "control", "server.mjs"), "utf8");
  assert.match(src, /model_not_subject/, "server.mjs must carry the subject refusal");
  assert.match(
    src,
    /profile\?\.subject_model\s*&&\s*model\s*!==\s*profile\.subject_model/,
    "the subject check must compare the requested model against the frozen subject",
  );
  // PREVIEW must run it too — a preview that green-lights a model start will
  // refuse moves the refusal to after the operator has committed.
  assert.match(src, /requireConfirm:\s*false,\s*profile:/, "preview must pass the active profile through");
});

test("the board never mints its own confirmation token", () => {
  // A client-generated confirmation confirms nothing the server can trust. The
  // token fingerprints the exact parameters, so it must be RECEIVED from
  // /api/run/preview and echoed back verbatim — never reconstructed in the
  // browser, which would let a stale confirmation start a run with parameters
  // the operator never saw.
  const src = readFileSync(join(BENCH, "dashboard", "panels", "runstart.js"), "utf8");
  assert.match(src, /data\?\.token/, "runstart.js must read the token from the preview response");
  assert.ok(
    !/confirmationToken/.test(src),
    "runstart.js references confirmationToken — the browser must never compose one",
  );
  // The restatement shown to the operator is the SERVER's, so the words read
  // are the words acted on.
  assert.match(src, /data\?\.restatement/, "the restatement must come from the server");
});

test("changing a run parameter disarms a pending confirmation", () => {
  // The token is a fingerprint of the parameters. Reusing it after an edit
  // would start a run the operator never agreed to.
  const src = readFileSync(join(BENCH, "dashboard", "panels", "runstart.js"), "utf8");
  const fn = src.slice(src.indexOf("export function setRunSel"));
  const body = fn.slice(0, fn.indexOf("\n}"));
  assert.match(body, /disarm\(\)/, "setRunSel must disarm — a stale token must never survive an edit");
});

test("the dashboard server stays read-only: no write route, no POST handler", () => {
  // GET-only, bench repo mounted :ro, uid 1000, no docker socket. Those are
  // kernel-enforced properties and they are what make "the dashboard corrupted
  // a run" impossible rather than merely unlikely. Every write goes to the
  // control plane, which the browser posts to directly.
  const src = readFileSync(join(BENCH, "dashboard", "server.mjs"), "utf8");
  assert.ok(
    !/req\.method\s*===\s*"POST"/.test(src),
    "the dashboard server handles a POST — writes belong to the control plane alone",
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// GATE WALL (WO-GATE-ROSTER)
//
// The wall answers a question `failed_gates` structurally could not: for every
// gate in the suite, what happened to it? These tests pin the distinctions that
// make that answer honest — most importantly that "no result" is never allowed
// to read as "passed".
// ─────────────────────────────────────────────────────────────────────────────

/** A minimal two-phase roster; enough to exercise every fold branch. */
function fakeRoster() {
  return {
    schema_version: 1,
    total: 4,
    by_phase: { backend: 3, frontend: 1 },
    suite_fingerprint: "sha256:deadbeef",
    enumeration: { executed_tests: false, complete: true, incomplete_reason: null },
    gates: [
      { id: "G01", phase: "backend", req: "REQ-INIT", title: "initial position", gate_token: "G01", tier: "core" },
      { id: "G02", phase: "backend", req: "REQ-PIP", title: "pip count", gate_token: "G02", tier: "core" },
      { id: "G03", phase: "backend", req: "REQ-DICE", title: "dice", gate_token: "G03", tier: "core" },
      { id: "F01", phase: "frontend", req: "REQ-RENDER", title: "renders", gate_token: "F01", tier: "core" },
    ],
  };
}

test("WALL: a gate that never ran is never reported as passed", () => {
  // THE DEFECT THIS WHOLE SURFACE EXISTS TO REMOVE. Under `failed_gates` alone,
  // G03 (never executed) and G01 (executed, passed) were both simply "not in
  // the failing list" — indistinguishable, and the natural reading of that
  // silence is success.
  const roster = fakeRoster();
  const attempts = [
    {
      attempt: 1,
      gate_results: [
        { id: "G01", status: "pass" },
        { id: "G02", status: "fail" },
        { id: "G03", status: "not_run", reason: "phase aborted before execution" },
      ],
    },
  ];
  const { gates } = foldGateStates({ roster, attempts });
  const byId = Object.fromEntries(gates.map((g) => [g.id, g]));
  assert.equal(byId.G01.state, "passing");
  assert.equal(byId.G02.state, "failing");
  assert.equal(byId.G03.state, "untested", "a not_run gate must never be resolved");
  // A gate absent from the results array entirely is the same class of fact.
  assert.equal(byId.F01.state, "untested", "an unreported gate must never be resolved");
});

test("WALL: not_run is untested, but error is a failure", () => {
  // The two ways the three-way split goes wrong, pinned in one place.
  // `not_run`  — the runner never reached it. No measurement exists.
  // `error`    — it ran and could not complete. It has NOT been shown to work.
  const roster = fakeRoster();
  const attempts = [
    {
      attempt: 1,
      gate_results: [
        { id: "G01", status: "not_run" },
        { id: "G02", status: "error" },
      ],
    },
  ];
  const byId = Object.fromEntries(
    foldGateStates({ roster, attempts }).gates.map((g) => [g.id, g]),
  );
  assert.equal(byId.G01.state, "untested", "an unreached gate must not invent a red square");
  assert.equal(byId.G02.state, "failing", "a broken gate must not hide in the not-yet bucket");
});

test("WALL: totals partition the suite exactly", () => {
  // Every gate lands in exactly one of THREE states, so the totals must sum to
  // the suite size. If they ever do not, the board is rendering a suite that
  // does not exist.
  const roster = fakeRoster();
  const attempts = [
    { attempt: 1, gate_results: [{ id: "G01", status: "pass" }, { id: "G02", status: "fail" }] },
  ];
  const { gates, totals } = foldGateStates({ roster, attempts });
  const sum = totals.passing + totals.failing + totals.untested;
  assert.equal(sum, roster.total, "totals must sum to the suite total");
  assert.equal(sum, gates.length);
  assert.equal(Object.keys(totals).length, 3, "three states, and no more");
});

test("WALL: the LAST completed test run wins — a fixed gate turns green", () => {
  // The wall reports the current state of the code, not the history of how it
  // got there. Attempt 2 supersedes attempt 1 outright.
  const roster = fakeRoster();
  const attempts = [
    { attempt: 1, gate_results: [{ id: "G01", status: "fail" }, { id: "G02", status: "fail" }] },
    { attempt: 2, gate_results: [{ id: "G01", status: "pass" }, { id: "G02", status: "fail" }] },
  ];
  const byId = Object.fromEntries(
    foldGateStates({ roster, attempts }).gates.map((g) => [g.id, g]),
  );
  assert.equal(byId.G01.state, "passing", "fixed in attempt 2");
  assert.equal(byId.G02.state, "failing", "still broken in attempt 2");
});

test("WALL: a gate that REGRESSED reads red, not green", () => {
  // The mirror image, and the reason the fold takes the latest result rather
  // than "passed at least once". A gate that passed attempt 1 and broke in
  // attempt 2 is broken NOW, and a wall that showed it green would be reporting
  // a pass that no longer holds.
  const roster = fakeRoster();
  const attempts = [
    { attempt: 1, gate_results: [{ id: "G01", status: "pass" }] },
    { attempt: 2, gate_results: [{ id: "G01", status: "fail" }] },
  ];
  const byId = Object.fromEntries(
    foldGateStates({ roster, attempts }).gates.map((g) => [g.id, g]),
  );
  assert.equal(byId.G01.state, "failing");
});

test("WALL: a gate row carries NO phase and no live signal", () => {
  // The wall is a dumb surface: the server hands it three states and the
  // identity needed to check a square against the log. Anything else is a
  // second axis the panel would have to decide about, which is exactly what
  // this rebuild removed.
  const roster = fakeRoster();
  const attempts = [{ attempt: 1, gate_results: [{ id: "G01", status: "pass" }] }];
  const [row] = foldGateStates({ roster, attempts }).gates;
  assert.deepEqual(Object.keys(row).sort(), ["id", "req", "state", "title"]);
});

test("WALL: run_dir is confined to a child of the runs root", () => {
  // The value reaches an fs path. Traversal would let a caller read arbitrary
  // JSON off the host through a read-only endpoint.
  assert.equal(resolveRunDir("/runs", "../../etc"), null);
  assert.equal(resolveRunDir("/runs", "/etc/passwd"), null);
  assert.equal(resolveRunDir("/runs", "a/b"), null);
  assert.equal(resolveRunDir("/runs", ".."), null);
  assert.equal(resolveRunDir("/runs", "cumulative")?.name, "cumulative");
  assert.equal(resolveRunDir("/runs", "")?.name, "cumulative", "empty falls back to the default run dir");
});

// ── THE WIPE BOUNDARY ───────────────────────────────────────────────────────
//
// Cell logs are written to the runs ROOT; the run state they describe lives in
// `runs/<run_dir>/`. Archiving or wiping a run moves the directory and leaves
// the log, so the log outlives its own data.
//
// MEASURED 2026-08-13: after a wipe, `runs/off-cell-20260813T051334.log`
// remained at the root and every reader resolved it as the live run. /api/wall
// served `suite.total:null` (the run dir was gone) beside `grading.active:true
// phase:frontend stalled:true silent_s:4848` parsed out of that dead log — a
// wiped bench reporting a run in progress, which the operator could not clear
// without hand-deleting files after every wipe.

test("WIPE: a cell log whose run directory is gone is not resolved as the live run", async () => {
  const root = mkdtempSync(join(tmpdir(), "wipe-"));
  try {
    const runs = join(root, "runs");
    mkdirSync(runs, { recursive: true });
    // The exact post-wipe shape: the run dir archived away, the log left behind.
    mkdirSync(join(runs, "cumulative.wiped-sim", "sessions"), { recursive: true });
    writeFileSync(
      join(runs, "off-cell-orphan.log"),
      "PROGRESS step=worktree-git-init path=" +
        join(runs, "cumulative", "sessions", "cell", "worktree") +
        "\nPROGRESS step=gate-phase-start phase=frontend\n",
    );

    assert.equal(
      await newestLog(runs),
      null,
      "an orphan log describes a run that no longer exists and is not live",
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("WIPE: a cell log whose run directory still exists IS resolved as live", async () => {
  const root = mkdtempSync(join(tmpdir(), "wipe-"));
  try {
    const runs = join(root, "runs");
    mkdirSync(join(runs, "cumulative", "sessions"), { recursive: true });
    writeFileSync(
      join(runs, "off-cell-live.log"),
      "PROGRESS step=worktree-git-init path=" +
        join(runs, "cumulative", "sessions", "cell", "worktree") +
        "\n",
    );

    const log = await newestLog(runs);
    assert.ok(log, "a log whose run dir exists is still the live run");
    assert.equal(log.run_dir, "cumulative");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("WIPE: a fresh log that has not yet named a run dir is live, not orphaned", async () => {
  const root = mkdtempSync(join(tmpdir(), "wipe-"));
  try {
    const runs = join(root, "runs");
    mkdirSync(runs, { recursive: true });
    // A just-launched cell prints banner lines before any artifact path.
    writeFileSync(join(runs, "off-cell-new.log"), "This is mini-swe-agent version 2.4.5.\n");

    const log = await newestLog(runs);
    assert.ok(log, "a log that has not named a run dir yet must not be discarded");
    assert.equal(log.run_dir, undefined);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("WIPE: an orphan is skipped in favour of an older log that is still live", async () => {
  const root = mkdtempSync(join(tmpdir(), "wipe-"));
  try {
    const runs = join(root, "runs");
    mkdirSync(join(runs, "cumulative", "sessions"), { recursive: true });

    const livePath = join(runs, "off-cell-live.log");
    writeFileSync(
      livePath,
      "PROGRESS step=worktree-git-init path=" + join(runs, "cumulative", "s", "w") + "\n",
    );
    // Newer by mtime, but its run dir is gone: recency must not beat existence.
    const orphanPath = join(runs, "off-cell-orphan.log");
    writeFileSync(
      orphanPath,
      "PROGRESS step=worktree-git-init path=" + join(runs, "cumulative.gone", "s", "w") + "\n",
    );
    utimesSync(livePath, new Date(1000), new Date(1000));
    utimesSync(orphanPath, new Date(9000), new Date(9000));

    const log = await newestLog(runs);
    assert.equal(log?.name, "off-cell-live.log", "the newest LIVE log wins, not the newest log");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("WALL: a wiped bench shows the suite defined and every gate untested", async () => {
  const root = mkdtempSync(join(tmpdir(), "wiped-"));
  try {
    const runs = join(root, "runs");
    mkdirSync(runs, { recursive: true });

    const wall = await readWall({ runsRoot: runs, runDir: null, benchRoot: BENCH });

    assert.equal(wall.ok, true);
    assert.equal(wall.suite_source, "enumerated", "the suite came from the harness, not a run");
    // The count is whatever the harness enumerates — asserted as a real number
    // rather than a literal, so adding a gate does not fail this test.
    assert.ok(wall.suite.total > 0, "the suite size is known");
    assert.equal(
      wall.totals.untested,
      wall.suite.total,
      "every gate is untested: defined, not yet evaluated",
    );
    assert.equal(wall.totals.passing, 0);
    assert.equal(
      wall.totals.failing,
      0,
      "a bench that has not run must never read as everything-failed",
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("WALL: the suite denominator is never fabricated when the harness cannot be reached", async () => {
  const root = mkdtempSync(join(tmpdir(), "noharness-"));
  try {
    const runs = join(root, "runs");
    mkdirSync(runs, { recursive: true });
    // benchRoot with no gates dir: the enumerator cannot run.
    const wall = await readWall({ runsRoot: runs, runDir: null, benchRoot: root });

    assert.equal(wall.ok, true, "a missing enumerator is a state, not a 500");
    assert.equal(wall.suite.total, null, "unknowable stays null, never 0 (invariant I-2)");
    assert.ok(wall.unwired.includes("gate-roster"));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("WALL: a run's own pinned roster wins over live enumeration", async () => {
  const root = mkdtempSync(join(tmpdir(), "pinned-"));
  try {
    const runs = join(root, "runs");
    mkdirSync(join(runs, "cumulative"), { recursive: true });
    // A roster pinned to this run describes the suite it was GRADED against and
    // must not be replaced by today's suite, or every comparison re-baselines.
    writeFileSync(
      join(runs, "cumulative", "gate-roster.json"),
      JSON.stringify({
        schema_version: 1,
        total: 2,
        suite_fingerprint: "sha256:pinned",
        gates: [
          { id: "G01", phase: "backend", tier: "core" },
          { id: "G02", phase: "backend", tier: "core" },
        ],
      }),
    );

    const wall = await readWall({ runsRoot: runs, runDir: "cumulative", benchRoot: BENCH });
    assert.equal(wall.suite_source, "run", "the pinned roster is authoritative");
    assert.equal(wall.suite.total, 2);
    assert.equal(wall.suite.fingerprint, "sha256:pinned");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("WALL: a truncated status stream yields every intact record before the tear", async () => {
  const dir = mkdtempSync(join(tmpdir(), "wall-"));
  try {
    const path = join(dir, "manifest.status.jsonl");
    writeFileSync(
      path,
      '{"type":"attempt","attempt":1,"gate_results":[]}\n' +
        '{"type":"turn_terminal"}\n' +
        '{"type":"attempt","attempt":2,"gate_r',
    );
    const records = await readStatusRecords(path);
    assert.equal(records.length, 2, "the intact records survive");
    assert.equal(attemptRecords(records).length, 1, "the torn attempt record is not invented");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ── the producer side ───────────────────────────────────────────────────────

test("ROSTER: a colliding token falls back to a slug instead of merging gates", () => {
  // [G10] covers five separate tests in the real suite. Using the bare token as
  // an id would silently merge them into one square and drop four gates from
  // the denominator.
  const gates = assignIds([
    { gate_token: "G01", phase: "backend", file: "backend/a.test.ts", full_name: "s > [G01] one" },
    { gate_token: "G10", phase: "backend", file: "backend/b.test.ts", full_name: "s > [G10] first" },
    { gate_token: "G10", phase: "backend", file: "backend/b.test.ts", full_name: "s > [G10] second" },
  ]);
  assert.equal(gates[0].id, "G01", "a token identifying exactly one test IS the id");
  assert.notEqual(gates[1].id, "G10");
  assert.notEqual(gates[1].id, gates[2].id, "colliding tokens must not produce colliding ids");
  assert.equal(gates[1].gate_token, "G10", "the token survives for grouping");
});

test("ROSTER: the fingerprint changes when the suite changes, not when it is reordered", () => {
  // The fingerprint's whole job is detecting that the suite changed mid-campaign
  // — which invalidates cross-cell gate comparison. A reorder is not a change.
  const a = [{ id: "G01" }, { id: "G02" }];
  const b = [{ id: "G02" }, { id: "G01" }];
  const c = [{ id: "G01" }, { id: "G03" }];
  assert.equal(suiteFingerprint(a), suiteFingerprint(b), "order must not affect the fingerprint");
  assert.notEqual(suiteFingerprint(a), suiteFingerprint(c));
});

test("ROSTER: the list parsers read what the runners actually print", () => {
  const vit = parseVitestList(
    "backend/gates-01-08.test.ts > Backgammon backend gates 01-08 > [G01] REQ-INIT — initial position\n" +
      "not a test line\n",
  );
  assert.equal(vit.length, 1);
  assert.equal(vit[0].file, "backend/gates-01-08.test.ts");
  assert.deepEqual(vit[0].chain, ["Backgammon backend gates 01-08", "[G01] REQ-INIT — initial position"]);

  const pw = parsePlaywrightList(
    "Listing tests:\n" +
      "  [chromium] › core.spec.ts:108:1 › [F01] REQ-RENDER — page loads\n" +
      "Total: 1 test in 1 file\n",
  );
  assert.equal(pw.length, 1, "the banner and the total are not tests");
  assert.equal(pw[0].file, "core.spec.ts");
  assert.equal(pw[0].line, 108);
  assert.deepEqual(pw[0].chain, ["[F01] REQ-RENDER — page loads"]);
});

test("GATE RESULTS: every roster gate appears exactly once, not_run included", () => {
  // INVARIANT I-4 stated as a shape: the output array is the roster, always.
  const roster = {
    available: true,
    fingerprint: "sha256:x",
    gates: [
      { id: "G01", phase: "backend", file: "a.test.ts", full_name: "one", test_name: "one" },
      { id: "G02", phase: "backend", file: "a.test.ts", full_name: "two", test_name: "two" },
    ],
    byKey: new Map(),
  };
  const matcher = { unmatched: [] };
  const folded = foldGateResults({
    roster,
    matcher,
    observed: [{ id: "G01", status: "pass", phase: "backend", duration_ms: 3 }],
    phaseRan: { backend: true },
  });
  assert.equal(folded.gate_results.length, 2);
  const g2 = folded.gate_results.find((r) => r.id === "G02");
  assert.equal(g2.status, "not_run");
  assert.match(g2.reason, /produced no result/);
  assert.deepEqual(folded.gate_totals, { total: 2, pass: 1, fail: 0, not_run: 1, error: 0 });
});

test("GATE RESULTS: with no roster, the denominator is null — never zero", () => {
  // INVARIANT I-2. Zero reads as "nothing was missed"; null reads as "unknown".
  // Only one of those is true when the suite is unknown.
  const folded = foldGateResults({
    roster: { available: false, reason: "no --roster supplied", gates: [], byKey: new Map() },
    matcher: { unmatched: [] },
    observed: [{ id: "G01", status: "pass" }],
    phaseRan: {},
  });
  assert.equal(folded.gate_totals.total, null);
  assert.equal(folded.gate_totals.not_run, null);
  assert.equal(folded.gate_roster.available, false);
  assert.match(folded.gate_roster.reason, /no --roster/);
});

test("GATE RESULTS: a runner's status words map onto the published vocabulary", () => {
  assert.equal(normalizeStatus("passed").status, "pass");
  assert.equal(normalizeStatus("failed").status, "fail");
  // A test that blew its own timeout DID run and did NOT satisfy the gate.
  assert.equal(normalizeStatus("timedOut").status, "fail");
  // Skipped and interrupted did NOT run — and must never read as pass.
  assert.equal(normalizeStatus("skipped").status, "not_run");
  assert.equal(normalizeStatus("interrupted").status, "not_run");
  // An unknown word is surfaced as an error, never quietly treated as a pass.
  assert.equal(normalizeStatus("wat").status, "error");
});

test("ROSTER: tiers partition the suite without shrinking it", () => {
  // A tier says what KIND of gate this is so the board can render an edge-case
  // square differently and a scorecard can quote a core-only bar. It is NOT a
  // way to make a gate optional — `total` stays the true enumerated count
  // (invariant I-1), and tiers only slice it.
  const tiers = { fallback: "core", rules: [{ tier: "edge", path_segment: "edge" }] };
  assert.equal(tierOf("backend/edge/edge-gates.test.ts", tiers), "edge");
  assert.equal(tierOf("backend/gates-01-08.test.ts", tiers), "core");
  // Substring matches must not count — only a whole path SEGMENT named `edge`.
  assert.equal(tierOf("edges.spec.ts", tiers), "core", "edges.spec.ts is a core frontend file");
  assert.equal(tierOf("backend/hedge/x.test.ts", tiers), "core");
  // No rules at all: everything is labelled, nothing is dropped.
  assert.equal(tierOf("anything.test.ts", { fallback: "core", rules: [] }), "core");
});

// ─────────────────────────────────────────────────────────────────────────────
// GRADED TEXT (WO-FEEDBACK-1)
//
// The harness renders gate results into prose and hands it to the model as a
// user turn. These pin the one property that makes the surface worth having:
// the text is carried VERBATIM. A surface that cleaned it up would answer a
// different question than the one an operator opens it to judge.
// ─────────────────────────────────────────────────────────────────────────────

test("FEEDBACK: the message text is carried verbatim, byte for byte", () => {
  // Newlines, bullets, em-dashes and trailing whitespace all survive: the
  // operator is judging whether this reads like a person wrote it, and any
  // normalisation here would forge the evidence.
  const body = "These are still failing \u2014 fix it.\n\n- use higher die: FAILING\n";
  const m = normalizeMessage({ kind: "feedback", attempt: 2, timestamp: 5, text: body }, 0);
  assert.equal(m.text, body);
  assert.equal(m.chars, body.length);
  assert.equal(m.kind, "feedback");
  assert.equal(m.kind_inferred, false);
});

test("FEEDBACK: a record with no kind is defaulted BUT says so", () => {
  // Sidecar records written before `kind` existed are real data. Relabelling
  // them as if the writer had stated a kind it never stated is a small lie of
  // exactly the sort this whole surface exists to prevent.
  const m = normalizeMessage({ attempt: 1, text: "x" }, 0);
  assert.equal(m.kind, "feedback");
  assert.equal(m.kind_inferred, true, "the inference must be visible, not silent");
});

test("FEEDBACK: feed rows are user-kind, because that is the fiction under test", () => {
  // Filing them under `harness` would quietly answer the question the operator
  // opened the feed to judge — whether these read as user turns.
  const rows = feedbackRows([
    normalizeMessage({ kind: "feedback", attempt: 2, timestamp: 1, text: "still failing" }, 0),
    normalizeMessage({ kind: "chunk", attempt: 1, timestamp: 0, text: "build a game" }, 1),
  ]);
  assert.ok(rows.every((r) => r.kind === "user"));
  assert.equal(rows[0].type, "user:feedback");
  assert.equal(rows[1].type, "user:chunk");
  assert.match(rows[1].name, /task chunk/);
});

test("FEEDBACK: an oversized message is capped and SAYS it was capped", () => {
  // A 33KB chunk prompt would swamp the feed. Truncating silently would let the
  // tail vanish with nothing to indicate it existed.
  const big = "x".repeat(9000);
  const [row] = feedbackRows([normalizeMessage({ kind: "chunk", text: big }, 0)], { textCap: 100 });
  assert.equal(row.text.length, 100);
  assert.equal(row.truncated, true);
  const [small] = feedbackRows([normalizeMessage({ kind: "chunk", text: "short" }, 0)], { textCap: 100 });
  assert.equal(small.truncated, false);
});

test("FEEDBACK: a torn sidecar yields every intact message before the tear", async () => {
  const dir = mkdtempSync(join(tmpdir(), "fb-"));
  try {
    const path = join(dir, "worktree.user-events.jsonl");
    writeFileSync(
      path,
      '{"type":"user","kind":"chunk","attempt":1,"text":"a"}\n' +
        '{"type":"user","kind":"feedback","attempt":2,"text":"b"}\n' +
        '{"type":"user","kind":"feed',
    );
    const records = await readSidecar(path);
    assert.equal(records.length, 2);
    assert.equal(records[1].text, "b");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("FEEDBACK: no sidecar yet is ok:true + unwired, never an error", async () => {
  // Before the first prompt is sent there is genuinely nothing. That is a state
  // to report, not a failure — and it must stay distinguishable from "this
  // surface is not wired up".
  const dir = mkdtempSync(join(tmpdir(), "fb-"));
  try {
    const res = await readFeedback({ runsRoot: dir, runDir: "cumulative" });
    assert.equal(res.ok, true);
    assert.deepEqual(res.messages, []);
    assert.deepEqual(res.unwired, ["user-events"]);
    assert.match(res.unwired_reasons["user-events"], /first prompt/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("FEEDBACK: run_dir traversal is refused, same as the wall", async () => {
  const res = await readFeedback({ runsRoot: "/runs", runDir: "../../etc" });
  assert.equal(res.ok, false);
  assert.equal(res.code, "bad_run_dir");
});

test("FEEDBACK: text can be omitted for an index, and that is stated", async () => {
  const dir = mkdtempSync(join(tmpdir(), "fb-"));
  try {
    const cell = join(dir, "cumulative", "sessions", "cell-0");
    mkdirSync(cell, { recursive: true });
    writeFileSync(
      join(cell, "worktree.user-events.jsonl"),
      '{"type":"user","kind":"feedback","attempt":2,"text":"body here"}\n',
    );
    const withText = await readFeedback({ runsRoot: dir, runDir: "cumulative" });
    assert.equal(withText.messages[0].text, "body here");
    assert.equal(withText.text_included, true);
    assert.equal(withText.counts.feedback, 1);

    const without = await readFeedback({ runsRoot: dir, runDir: "cumulative", includeText: false });
    assert.equal(without.text_included, false, "a client must tell 'no text here' from 'no text sent'");
    assert.equal(without.messages[0].text, undefined);
    assert.equal(without.messages[0].chars, "body here".length, "the length still reports");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("FEEDBACK: the contract version is declared", () => {
  assert.equal(typeof FEEDBACK_CONTRACT_VERSION, "number");
});

// ─────────────────────────────────────────────────────────────────────────────
// THE MODEL LEDGER — the three launch gates
//
// These pin the rules that cost real hours when broken. A wrongly-OPEN gate
// starts a ~3h cell that cannot be scored; a wrongly-CLOSED one strands the
// campaign. Both are silent, which is why they are asserted rather than read.
// ─────────────────────────────────────────────────────────────────────────────

/** A run dir on disk: one schedule slot plus its status record. */
function writeRun(root, dir, { seq = 0, model = "m-a", arm = "off", status = null }) {
  const d = join(root, dir);
  mkdirSync(d, { recursive: true });
  writeFileSync(join(d, "manifest.json"), JSON.stringify({
    created_at: "2026-08-13T00:00:00Z",
    // The arm field is `memory_mode` and the model is bare in `provider_pin`.
    schedule: [{ sequence_index: seq, memory_mode: arm, provider_pin: model }],
  }));
  if (status) writeFileSync(join(d, "manifest.status.jsonl"), `${JSON.stringify(status)}\n`);
  return d;
}

const OFF_PASS = { type: "attempt", sequence_index: 0, verdict: "PASS", progress: { turns: 9, total_tokens: 400, wall_seconds: 60 } };

test("LEDGER: a valid OFF cell is the baseline, and it opens + profile but not + baseline", async () => {
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  writeRun(root, "cumulative", { status: OFF_PASS });

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [{ id: "m-a", bench_eligible: true }],
    runInFlight: false,
  });
  const m = led.models[0];
  assert.equal(m.baseline.scorable, true);
  assert.equal(m.can_profile.allowed, true);
  // Re-baselining is a declared act (RUNBOOK 5.13), never a live button.
  assert.equal(m.can_baseline.allowed, false);
  rmSync(root, { recursive: true, force: true });
});

test("LEDGER: a VOID baseline counts as NO baseline and re-opens + baseline", async () => {
  // The failure this prevents: void numbers exist and look like success, so a
  // gate keyed on "a cell completed" would green-light an ON run whose every Δ
  // is measured against the harness rather than the model.
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  writeRun(root, "cumulative", {
    status: { type: "attempt", sequence_index: 0, verdict: "FAIL", terminal_reason: "transport_incomplete", progress: { turns: 2 } },
  });

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [{ id: "m-a", bench_eligible: true }],
    runInFlight: false,
  });
  const m = led.models[0];
  assert.equal(m.baseline.scorable, false);
  assert.equal(m.baseline.voided, true);
  assert.equal(m.can_profile.allowed, false, "no profile may be frozen on a void floor");
  assert.equal(m.can_baseline.allowed, true, "the operator must be able to re-measure");
  assert.match(m.can_baseline.reason ?? "", /^$|void/i);
  rmSync(root, { recursive: true, force: true });
});

test("LEDGER: attempt_ceiling_reached is a real FAIL, not a void instrument", async () => {
  // A model that fails every attempt is the bench's most important finding.
  // Calling it an instrument fault would discard it.
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  writeRun(root, "cumulative", {
    status: { type: "attempt", sequence_index: 0, verdict: "FAIL", terminal_reason: "attempt_ceiling_reached", progress: { turns: 40 } },
  });

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [{ id: "m-a", bench_eligible: true }],
    runInFlight: false,
  });
  assert.equal(led.models[0].baseline.scorable, true, "a capability FAIL is a usable floor");
  rmSync(root, { recursive: true, force: true });
});

test("LEDGER: an archived run never supplies a baseline", async () => {
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  writeRun(root, "cumulative.wiped-recommission-20260813T0150", { status: OFF_PASS });

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [{ id: "m-a", bench_eligible: true }],
    runInFlight: false,
  });
  assert.equal(led.models[0].baseline.exists, false);
  rmSync(root, { recursive: true, force: true });
});

test("LEDGER: one cell in flight blocks EVERY button on EVERY model", async () => {
  // The serial rule is a property of the bench, not of a row. A per-row UI is
  // exactly where this gets broken, because each row looks independent.
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  writeRun(root, "cumulative", { status: OFF_PASS });

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [{ id: "m-a", bench_eligible: true }, { id: "m-b", bench_eligible: true }],
    runInFlight: true,
    blockedReason: "a cell is running (off-cell-3.log)",
  });
  assert.equal(led.run_in_flight, true);
  for (const m of led.models) {
    assert.equal(m.can_baseline.allowed, false);
    assert.equal(m.can_profile.allowed, false);
    assert.match(m.can_baseline.reason, /cell is running/);
    for (const p of m.profiles) assert.equal(p.can_run.allowed, false);
  }
  rmSync(root, { recursive: true, force: true });
});

test("LEDGER: + run is closed on any profile that is not the active one", async () => {
  // The launcher attributes a cell to the NEWEST profile and accepts no profile
  // id. An open button on an older row would start a real run and file it
  // under a different profile.
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  writeRun(root, "cumulative", { status: OFF_PASS });
  const older = await createProfile(root, { subjectModel: "m-a", memoryModels: ["m-a"] });
  await new Promise((r) => setTimeout(r, 5));
  const newer = await createProfile(root, { subjectModel: "m-a", memoryModels: ["m-b"] });
  assert.equal(older.ok, true);
  assert.equal(newer.ok, true);

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [{ id: "m-a", bench_eligible: true }],
    runInFlight: false,
  });
  const profs = led.models[0].profiles;
  assert.equal(profs.length, 2);
  const active = profs.filter((p) => p.can_run.allowed);
  assert.equal(active.length, 1, "exactly one profile may be run under");
  assert.equal(active[0].is_active, true);
  for (const p of profs.filter((p) => !p.can_run.allowed)) {
    assert.match(p.can_run.reason, /newest profile/);
  }
  rmSync(root, { recursive: true, force: true });
});

test("LEDGER: a profile on one model never blocks another model's + baseline", async () => {
  // THE DEFECT THIS PINS. The subject rule was applied to OFF cells as well as
  // ON, so freezing the first profile disabled [+ baseline] on every OTHER
  // bench model — and since [+ profile] gates on a floor, no second model could
  // ever be benchmarked. The whole bench silently locked to whichever model was
  // profiled first, with four permanently disabled buttons to show for it.
  //
  // A baseline is measured against nothing. One floor per model, and no other
  // model's profile has any bearing on it.
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  writeRun(root, "cumulative", { status: OFF_PASS });           // m-a has a floor
  const prof = await createProfile(root, { subjectModel: "m-a", memoryModels: ["m-a"] });
  assert.equal(prof.ok, true);

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [
      { id: "m-a", bench_eligible: true },
      { id: "m-b", bench_eligible: true },
      { id: "m-c", bench_eligible: true },
    ],
    runInFlight: false,
  });

  const byId = Object.fromEntries(led.models.map((m) => [m.id, m]));
  // m-a is profiled AND floored: no second baseline, but profiles are open.
  assert.equal(byId["m-a"].can_baseline.allowed, false);
  assert.match(byId["m-a"].can_baseline.reason, /already has a valid baseline/);
  // Every other model may still measure its own floor.
  for (const id of ["m-b", "m-c"]) {
    assert.equal(byId[id].can_baseline.allowed, true, `${id} must be able to measure its own floor`);
    assert.equal(byId[id].can_baseline.reason, null, "an open gate states no refusal");
    // …and cannot be profiled until it has one.
    assert.equal(byId[id].can_profile.allowed, false);
  }
  rmSync(root, { recursive: true, force: true });
});

test("CAMPAIGN: a second model gets its own directory; the first keeps runs/cumulative", async () => {
  // WHY THIS EXISTS. Every launch used to target runs/cumulative/manifest.json,
  // whose roster hash is frozen to ONE model. A baseline for a second model died
  // at startup with `roster hash drift detected` — so opening [+ baseline] for
  // every un-floored model is only honest if each model has somewhere to write.
  const root = mkdtempSync(join(tmpdir(), "wevibe-camp-"));
  mkdirSync(join(root, "cumulative"), { recursive: true });
  writeFileSync(join(root, "cumulative", "manifest.json"), JSON.stringify({
    roster: [{ model: "local-llm-proxy/m-a" }],
    schedule: [{ sequence_index: 0, memory_mode: "off", provider_pin: "m-a" }],
  }));

  // The owner keeps the default path — null means "pass no --manifest", so the
  // live campaign's invocation is byte-identical to what it has always been.
  assert.equal(await manifestArgFor("m-a", root), null);
  // Every other model is routed away from it.
  assert.equal(await manifestArgFor("m-b", root), join(root, "cumulative-m-b", "manifest.json"));

  // …and STABLY: a second call for the same model resolves to the same place,
  // so cell 2 continues the campaign cell 1 started.
  assert.equal(await manifestArgFor("m-b", root), join(root, "cumulative-m-b", "manifest.json"));
  rmSync(root, { recursive: true, force: true });
});

test("CAMPAIGN: a dotted model alias never produces an archive-shaped directory", () => {
  // `isArchivedRun()` reads ANY dot as the archive convention
  // (runs/cumulative.<why>-<date>). A directory named for `qwen3.6-…` would
  // therefore be treated as archived and its baseline would silently disappear
  // from the floor index — a measured ~3h cell, invisible, with no error.
  const name = campaignDirName("qwen3.6-35b-a3b-bench");
  assert.equal(name, "cumulative-qwen3-6-35b-a3b-bench");
  assert.equal(isArchivedRun(name), false, "the campaign directory must not read as archived");
  assert.equal(isArchivedRun("cumulative.void-truncation-20260812"), true, "…while a real archive still does");
});

test("CAMPAIGN: an unreadable legacy manifest never relocates the live campaign", async () => {
  // Corrupt manifest => the harness must fail loudly on the default path. Moving
  // the campaign to a fresh directory instead would present as the entire run
  // history having vanished.
  const root = mkdtempSync(join(tmpdir(), "wevibe-camp-"));
  mkdirSync(join(root, "cumulative"), { recursive: true });
  writeFileSync(join(root, "cumulative", "manifest.json"), "{ not json");
  assert.equal(await manifestArgFor("m-a", root), null, "a broken campaign is faced, not routed around");

  // Readable but model-less: also treated as "mine" rather than relocated.
  writeFileSync(join(root, "cumulative", "manifest.json"), JSON.stringify({ schedule: [] }));
  assert.equal(await manifestArgFor("m-a", root), null);

  // ABSENT is the different answer: nothing has claimed the default directory,
  // so this model names its own.
  rmSync(join(root, "cumulative"), { recursive: true, force: true });
  assert.equal(await manifestArgFor("m-a", root), join(root, "cumulative-m-a", "manifest.json"));
  rmSync(root, { recursive: true, force: true });
});

test("BASELINES: the index is the single export, and it is written to disk", async () => {
  const root = mkdtempSync(join(tmpdir(), "wevibe-base-"));
  writeRun(root, "cumulative", { status: OFF_PASS });           // m-a floored
  const models = [{ id: "m-a", bench_eligible: true }, { id: "m-b", bench_eligible: true }];

  const idx = await readBaselines({ runsRoot: root, models });
  assert.equal(idx.ok, true);
  assert.equal(idx.models["m-a"].scorable, true);
  // A model with no OFF cell still gets an ENTRY carrying the reason — an
  // absent key is indistinguishable from "not on the bench".
  assert.equal(idx.models["m-b"].scorable, false);
  assert.match(idx.models["m-b"].reason, /no OFF cell has ever been run/);

  // STORED: the export lands on disk, and matches what was served.
  assert.equal(idx.stored.written, true);
  const onDisk = JSON.parse(readFileSync(join(root, BASELINES_FILE), "utf8"));
  assert.deepEqual(onDisk.models, idx.models);

  // Re-derived with nothing changed: same answer, and the file is NOT rewritten
  // — the mtime stays meaningful as "when the floor last changed".
  const again = await readBaselines({ runsRoot: root, models });
  assert.deepEqual(again.models, idx.models);
  assert.equal(again.stored.written, false);

  // THE LEDGER READS THIS SAME INDEX rather than deriving its own.
  const led = await readModelsLedger({ runsRoot: root, benchModels: models, runInFlight: false });
  assert.deepEqual(led.baselines.models, idx.models);
  for (const m of led.models) assert.deepEqual(m.baseline.scorable, idx.models[m.id].scorable);
  rmSync(root, { recursive: true, force: true });
});

test("LEDGER: a profile reports no measured cell, and says why", async () => {
  // Runs are not joined to measured cells. The field is null WITH a reason, so
  // the board states that rather than rendering eight `unobserved` columns that
  // imply a measurement is merely pending.
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  writeRun(root, "cumulative", { status: OFF_PASS });
  await createProfile(root, { subjectModel: "m-a", memoryModels: ["m-a"] });

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [{ id: "m-a", bench_eligible: true }],
    runInFlight: false,
  });
  const p = led.models[0].profiles[0];
  assert.equal(p.latest_cell, null);
  assert.match(p.latest_cell_unavailable, /not joined/i);
  rmSync(root, { recursive: true, force: true });
});

test("LEDGER: profiles whose model left the roster are surfaced, not dropped", async () => {
  const root = mkdtempSync(join(tmpdir(), "wevibe-mled-"));
  await createProfile(root, { subjectModel: "gone", memoryModels: ["gone"] });

  const led = await readModelsLedger({
    runsRoot: root,
    benchModels: [{ id: "m-a", bench_eligible: true }],
    runInFlight: false,
  });
  assert.equal(led.orphaned_profiles.length, 1);
  assert.equal(led.orphaned_profiles[0].subject_model, "gone");
  rmSync(root, { recursive: true, force: true });
});
