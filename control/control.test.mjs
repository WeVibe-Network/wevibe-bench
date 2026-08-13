// ─────────────────────────────────────────────────────────────────────────────
// CONTROL PLANE TESTS — stdlib runner only
//
//   cd wevibe-bench/control && node --test
//
// WHAT THESE TESTS ARE FOR. Two of them are DRIFT tests that assert this JS
// agrees with the Python harness it describes. Those are the ones that matter
// most: every other property here is local to this directory and would be
// caught by reading it, but a stage list or a context registry that silently
// stops matching the program it claims to describe produces a UI that lies
// confidently. Drift fails loudly here or not at all.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import {
  confirmationToken,
  restatement,
  EXTRACT_STAGES,
  STAGE_STATES,
  EVENT_MAP,
  RESUME_UNSUPPORTED,
  EVENT_RING_MAX,
  EVENT_RENDER_CAP,
  GATE_STALL_THRESHOLD_S,
  emptyExtraction,
  refuse,
} from "./contract.mjs";
import { matchRuntime, DECLARED_CONTEXT, CONTEXT_CHOICES } from "./roster.mjs";
import { sessionIdFrom, terminalFrom, pidAlive } from "./runstate.mjs";
import { mapEvent, EventRing } from "./events.mjs";
import { parseGateEvents, gradingStatus } from "./gate-events.mjs";
import { parseStageLines, foldStages, DECLARED_STAGE_IDS } from "./extraction.mjs";
import { createProfile, transferOf } from "./profiles.mjs";
import { readModelsLedger } from "./models-ledger.mjs";
import {
  attemptRecords,
  foldGateStates,
  inFlightAtStop,
  maxAttemptsFrom,
  readStatusRecords,
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

// ── DRIFT: the stage list must describe the actual Python program ────────────

test("DRIFT: every declared stage exists in backgammon_sxe.py", () => {
  const src = readFileSync(join(BENCH, "scripts", "backgammon_sxe.py"), "utf8");
  // Collect every stage id the emitter is actually called with.
  const emitted = new Set(
    [...src.matchAll(/emit_stage\(\s*"([a-z_]+)"/g)].map((m) => m[1]),
  );
  for (const id of DECLARED_STAGE_IDS) {
    assert.ok(
      emitted.has(id),
      `stage '${id}' is declared in contract.mjs but never emitted by backgammon_sxe.py — ` +
        "the UI would render a stage that can never leave 'pending'",
    );
  }
});

test("DRIFT: the emitter never emits a stage the contract does not declare", () => {
  const src = readFileSync(join(BENCH, "scripts", "backgammon_sxe.py"), "utf8");
  const emitted = new Set(
    [...src.matchAll(/emit_stage\(\s*"([a-z_]+)"/g)].map((m) => m[1]),
  );
  const declared = new Set(DECLARED_STAGE_IDS);
  for (const id of emitted) {
    assert.ok(
      declared.has(id),
      `backgammon_sxe.py emits stage '${id}' which contract.mjs does not declare — ` +
        "it would be silently dropped by foldStages and never shown",
    );
  }
});

test("DRIFT: the extraction panel's stage vocabulary covers every contract state", () => {
  // A state the UI does not know falls through to the PENDING glyph rather than
  // erroring, so this drift is invisible on screen: it shipped once with the UI
  // keyed on `done` while the contract emits `complete`, which rendered every
  // finished stage as unstarted and reported "0/10" for a clean extraction.
  //
  // RE-POINTED TWICE, and the trail is kept deliberately — this guard has
  // outlived two of the files that rendered the invariant, which is the whole
  // argument for not deleting a guard alongside the code it was written against:
  //   1. drawer.js  — dead code, deleted (WO-BOARD-PROFILE-1)
  //   2. live.js    — extraction column removed from the run panel
  //   3. extraction.js (here) — the unified popout (WO-BOARD-EXTRACT-1)
  // The invariant never changed; only its address did.
  const src = readFileSync(join(BENCH, "dashboard", "panels", "extraction.js"), "utf8");
  const m = /export const STAGE_STATES = \[([^\]]*)\]/.exec(src);
  assert.ok(m, "STAGE_STATES not found in extraction.js — the stage vocabulary moved");
  const keys = [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
  for (const state of STAGE_STATES) {
    assert.ok(
      keys.includes(state),
      `contract state '${state}' is absent from extraction.js STAGE_STATES — it would ` +
        "render with the pending mark and read as a stage that never started",
    );
  }
});

test("DRIFT: the extraction panel counts completions using the contract's state name", () => {
  const src = readFileSync(join(BENCH, "dashboard", "panels", "extraction.js"), "utf8");
  assert.ok(
    src.includes('s.state === "complete"'),
    "the stage counter must compare against 'complete' — the contract emits no 'done' state",
  );
});

test("DRIFT: the extraction panel declares the same stage list as the contract", () => {
  // The queue's stage pips are drawn from the panel's OWN copy of the stage
  // ids, because the browser cannot import control/contract.mjs. A copy that
  // drifts would silently mis-draw progress — a pipeline that grew an 11th
  // stage would render as permanently 10/10 with one stage invisible.
  const src = readFileSync(join(BENCH, "dashboard", "panels", "extraction.js"), "utf8");
  const m = /export const EXTRACT_STAGES = \[([\s\S]*?)\]/.exec(src);
  assert.ok(m, "EXTRACT_STAGES not found in extraction.js");
  const ids = [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
  assert.deepEqual(
    ids,
    DECLARED_STAGE_IDS,
    "the extraction panel's stage list has drifted from control/contract.mjs — " +
      "the queue's pips would report progress against a pipeline that no longer exists",
  );
});

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

// ── EXTRACTION ───────────────────────────────────────────────────────────────

test("every declared stage is present from the start, in order", () => {
  const v = emptyExtraction();
  assert.equal(v.stages.length, EXTRACT_STAGES.length);
  assert.deepEqual(v.stages.map((s) => s.id), DECLARED_STAGE_IDS);
  // The UI must be able to show what is COMING, so nothing starts absent.
  assert.ok(v.stages.every((s) => s.state === "pending"));
});

test("stage lines are parsed and folded", () => {
  const text = [
    'BACKGAMMON_SXE_STAGE {"at":1,"stage":"init","state":"running"}',
    "noise line that is not a stage",
    'BACKGAMMON_SXE_STAGE {"at":2,"stage":"init","state":"complete"}',
  ].join("\n");
  const { stages } = parseStageLines(text);
  assert.equal(stages.length, 2);
  const v = foldStages(stages);
  assert.equal(v.stages.find((s) => s.id === "init").state, "complete");
});

test("a GATED stage is never overwritten by a later failed", () => {
  // This is the WO-DBVOL-1 distinction. A corrupt substrate gates, then the
  // exception path fires; if `failed` won, the deliberate refusal would be
  // rendered as a crash and the operator would misdiagnose it.
  const v = foldStages([
    { stage: "substrate", state: "running", at: 1 },
    { stage: "substrate", state: "gated", at: 2, detail: "database disk image is malformed" },
    { stage: "substrate", state: "failed", at: 3, detail: "raised" },
  ]);
  const s = v.stages.find((x) => x.id === "substrate");
  assert.equal(s.state, "gated");
  assert.match(s.detail, /malformed/);
});

test("a measured zero is preserved, not treated as absence", () => {
  const v = foldStages([
    { stage: "extract", state: "running", at: 1 },
    { stage: "extract", state: "complete", at: 2, count: 0 },
  ]);
  const s = v.stages.find((x) => x.id === "extract");
  assert.equal(s.count, 0);
  assert.notEqual(s.count, null);
});

test("an unknown stage id is ignored rather than injected into the UI", () => {
  const v = foldStages([{ stage: "not_a_real_stage", state: "running", at: 1 }]);
  assert.equal(v.stages.length, EXTRACT_STAGES.length);
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

test("extraction requires a stamped complete gate before it runs", () => {
  const route = SERVER_SRC.slice(
    SERVER_SRC.indexOf('path === "/api/extraction/start"'),
    SERVER_SRC.indexOf('path === "/api/health"'),
  );
  assert.match(route, /extractionEligibility\(/, "extraction start must gate on source-cell eligibility");
  assert.match(SERVER_SRC, /complete_gate_missing/, "missing complete_gate must refuse extraction");
  assert.match(SERVER_SRC, /already_extracted/, "already-extracted cells must refuse re-extraction");
});

test("completed sessions stamp both complete_gate and extracted_from", () => {
  const src = readFileSync(join(BENCH, "wevibe_bench", "cumulative", "sequencer.py"), "utf8");
  assert.match(src, /session\.complete_gate = True/);
  assert.match(src, /session\.extracted_from = True/);
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
  const { gates } = foldGateStates({
    roster,
    attempts,
    grading: null,
    stopped: { stopped: false, phase: null },
  });
  const byId = Object.fromEntries(gates.map((g) => [g.id, g]));
  assert.equal(byId.G01.state, "resolved");
  assert.equal(byId.G02.state, "failing");
  assert.equal(byId.G03.state, "untested", "a not_run gate must never be resolved");
  assert.equal(byId.G03.last_status, "not_run");
  // A gate absent from the results array entirely is the same class of fact.
  assert.equal(byId.F01.state, "untested", "an unreported gate must never be resolved");
  assert.equal(byId.F01.last_status, null);
});

test("WALL: totals partition the suite exactly", () => {
  // Every gate lands in exactly one state, so the four totals must sum to the
  // suite size. If they ever do not, the board is rendering a suite that does
  // not exist.
  const roster = fakeRoster();
  const attempts = [
    { attempt: 1, gate_results: [{ id: "G01", status: "pass" }, { id: "G02", status: "fail" }] },
  ];
  const { gates, totals } = foldGateStates({
    roster,
    attempts,
    grading: null,
    stopped: { stopped: false, phase: null },
  });
  const sum = totals.resolved + totals.failing + totals.untested + totals.abandoned;
  assert.equal(sum, roster.total, "totals must sum to the suite total");
  assert.equal(sum, gates.length);
});

test("WALL: blue vs green — resolved_at_attempt is the FIRST passing attempt", () => {
  // The design distinguishes "solved on the first try" from "solved only after
  // feedback". That distinction is the entire information content of the
  // blue/green split, and it is lost if a later attempt overwrites the first.
  const roster = fakeRoster();
  const attempts = [
    { attempt: 1, gate_results: [{ id: "G01", status: "pass" }, { id: "G02", status: "fail" }] },
    { attempt: 2, gate_results: [{ id: "G01", status: "pass" }, { id: "G02", status: "pass" }] },
  ];
  const byId = Object.fromEntries(
    foldGateStates({ roster, attempts, grading: null, stopped: { stopped: false, phase: null } })
      .gates.map((g) => [g.id, g]),
  );
  assert.equal(byId.G01.resolved_at_attempt, 1, "passed in attempt 1 → blue");
  assert.equal(byId.G02.resolved_at_attempt, 2, "passed only in attempt 2 → green");
});

test("WALL: a gate that passed then regressed stays resolved at its first pass", () => {
  // A later failure does not un-resolve the gate: the run DID demonstrate the
  // capability once, and the attempt number records when.
  const roster = fakeRoster();
  const attempts = [
    { attempt: 1, gate_results: [{ id: "G01", status: "pass" }] },
    { attempt: 2, gate_results: [{ id: "G01", status: "fail" }] },
  ];
  const g = foldGateStates({
    roster, attempts, grading: null, stopped: { stopped: false, phase: null },
  }).gates.find((x) => x.id === "G01");
  assert.equal(g.state, "resolved");
  assert.equal(g.resolved_at_attempt, 1);
  assert.equal(g.last_status, "fail", "the regression must still be visible");
});

test("WALL: a stall is not a verdict — in-flight gates go slate, never red", () => {
  // INVARIANT I-3. When the gate runner is killed mid-phase, the gates it was
  // executing were never measured. Calling them failed would attribute a
  // harness death to the model under test.
  const roster = fakeRoster();
  const { gates, totals } = foldGateStates({
    roster,
    attempts: [],
    grading: { grading: true, phase: "backend", timed_out: true, phases: [] },
    stopped: { stopped: true, phase: "backend" },
  });
  const byId = Object.fromEntries(gates.map((g) => [g.id, g]));
  assert.equal(byId.G01.state, "abandoned");
  assert.equal(byId.G03.state, "abandoned");
  assert.equal(byId.F01.state, "untested", "a phase never reached is untested, not abandoned");
  assert.equal(totals.failing, 0, "an abandoned gate must never be counted as failing");
});

test("WALL: a cold log is not a stop — a silent live run keeps its gates untested", () => {
  // MEASURED REGRESSION (2026-08-13). `readRunState` reports "failed" for any
  // run it did not launch once the log goes cold — which is what a live,
  // CLI-launched campaign looks like mid-grade. Treating that as a stop marked
  // all 14 frontend gates abandoned while they were still executing.
  //
  // Abandonment is a verdict; only the run's OWN terminal record may declare it.
  const grading = { grading: true, phase: "frontend", timed_out: false, phases: [] };
  const coldButAlive = { state: "failed", terminal_status: null, log_silent_s: 976 };
  assert.deepEqual(inFlightAtStop(grading, coldButAlive), { stopped: false, phase: null });

  const declaredOver = { state: "failed", terminal_status: "harness_error" };
  assert.deepEqual(inFlightAtStop(grading, declaredOver), { stopped: true, phase: "frontend" });
});

test("WALL: an abandoned gate is never also shown as under test", () => {
  // Amber says "being measured right now". An abandoned gate never will be.
  const roster = fakeRoster();
  const { gates } = foldGateStates({
    roster,
    attempts: [],
    grading: { grading: true, phase: "backend", timed_out: true, phases: [] },
    stopped: { stopped: true, phase: "backend" },
  });
  const abandoned = gates.filter((g) => g.state === "abandoned");
  assert.ok(abandoned.length > 0);
  assert.ok(abandoned.every((g) => g.under_test === false));
});

test("WALL: a live grading run does NOT abandon its own in-flight gates", () => {
  // The mirror-image error: marking gates abandoned while the phase is still
  // running would show slate squares for work in progress.
  const roster = fakeRoster();
  const { totals } = foldGateStates({
    roster,
    attempts: [],
    grading: { grading: true, phase: "backend", timed_out: false, phases: [] },
    stopped: inFlightAtStop(
      { grading: true, phase: "backend", timed_out: false, phases: [] },
      { state: "running" },
    ),
  });
  assert.equal(totals.abandoned, 0);
  assert.equal(totals.untested, 4);
});

test("WALL: under_test marks the open phase's gates as a set", () => {
  // PER-PHASE-SET is what the harness can actually publish live; the response
  // says so in `live_signal` so the board never implies per-test precision it
  // was not given.
  const roster = fakeRoster();
  const { gates } = foldGateStates({
    roster,
    attempts: [],
    grading: { grading: true, phase: "backend", timed_out: false, phases: [] },
    stopped: { stopped: false, phase: null },
  });
  const byId = Object.fromEntries(gates.map((g) => [g.id, g]));
  assert.equal(byId.G01.under_test, true);
  assert.equal(byId.F01.under_test, false, "a phase that is not running is not under test");
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

test("WALL: the attempt ceiling is read from the harness, never assumed", () => {
  assert.equal(maxAttemptsFrom("run_cumulative.pacing max_attempts=3 max_steps=40"), 3);
  // Unobservable is null, not a plausible-looking default.
  assert.equal(maxAttemptsFrom("nothing here"), null);
  assert.equal(maxAttemptsFrom(""), null);
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
