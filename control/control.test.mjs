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
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
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
