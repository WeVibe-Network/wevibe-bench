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
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import {
  confirmationToken,
  restatement,
  EXTRACT_STAGES,
  EVENT_MAP,
  RESUME_UNSUPPORTED,
  EVENT_RING_MAX,
  EVENT_RENDER_CAP,
  emptyExtraction,
  refuse,
} from "./contract.mjs";
import { matchRuntime, DECLARED_CONTEXT, CONTEXT_CHOICES } from "./roster.mjs";
import { sessionIdFrom, terminalFrom, pidAlive } from "./runstate.mjs";
import { mapEvent, EventRing } from "./events.mjs";
import { parseStageLines, foldStages, DECLARED_STAGE_IDS } from "./extraction.mjs";

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

test("a failed tool call is mapped to the error kind, not the tool kind", () => {
  const ev = mapEvent({
    id: "evt_1",
    type: "session.next.tool.failed",
    properties: { sessionID: "ses_1", tool: "edit", error: { data: { message: "boom" } } },
  });
  assert.equal(ev.kind, "error");
  assert.equal(ev.tool, "edit");
  assert.equal(ev.text, "boom");
});

test("a retry is lifecycle, NOT error", () => {
  // Post WO-NUDGE-INF-1 a retry is the system working as designed. Rendering it
  // in the fail colour would read as alarm at the moment the instrument is
  // behaving correctly.
  const ev = mapEvent({
    id: "evt_2",
    type: "session.next.retried",
    properties: { sessionID: "ses_1", attempt: 2 },
  });
  assert.equal(ev.kind, "lifecycle");
});

test("tool input is summarised, never dumped", () => {
  const big = "x".repeat(50000);
  const ev = mapEvent({
    id: "evt_3",
    type: "session.next.tool.called",
    properties: { sessionID: "ses_1", tool: "write", input: { filePath: "/a/b.ts", content: big } },
  });
  assert.equal(ev.text, "/a/b.ts");
  assert.ok(!String(ev.text).includes("xxxx"));
});

test("long reasoning text is truncated and says so", () => {
  const ev = mapEvent({
    id: "evt_4",
    type: "session.next.reasoning.delta",
    properties: { sessionID: "ses_1", delta: "y".repeat(5000) },
  });
  assert.equal(ev.truncated, true);
  assert.ok(ev.text.length <= 400);
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
    ring.push({ id: `t${i}`, type: "session.next.reasoning.delta", properties: { delta: "x" } });
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
  ring.push({ id: "b", type: "session.next.reasoning.delta", properties: { delta: "hm" } });
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
