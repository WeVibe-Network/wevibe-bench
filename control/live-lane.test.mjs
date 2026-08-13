// ─────────────────────────────────────────────────────────────────────────────
// LIVE LANE + BUILD SCAN + CHOREOGRAPHY — the provisional instrument
//
//     cd wevibe-bench/control && node --test live-lane.test.mjs
//
// WHY THIS EXISTS (WO-LIVE-GATES)
//
// This surface can be wrong in ways that LOOK RIGHT. A lane that reports a
// broken tree as "deferred" renders as a calm grid of intentional exclusions; a
// lane that runs beside the grader silently kills the oracle's server and voids
// a real attempt; a choreography that keys "first look" on the attempt NUMBER
// paints a never-before-seen gate as familiar. None of those produce an error,
// a stack trace, or a red test anywhere else in the tree.
//
// So the dangerous behaviours are pinned here, against the REAL scaffold/golden
// fixture pair on disk rather than against invented data — the scaffold is the
// 0% endpoint and golden is the 100% endpoint, both are checked in, and both are
// what the harness itself uses.
// ─────────────────────────────────────────────────────────────────────────────

import { test } from "node:test";
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { choreographGate, historiesFrom } from "./choreography.mjs";
import { measureFile, scanBuild } from "./build-scan.mjs";
import { foldLive, parseVitestJson, pauseVerdict, DEFERRED } from "./live-lane.mjs";
import { readLive } from "./live-surface.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const TASK = join(HERE, "..", "tasks", "backgammon");

// ── CHOREOGRAPHY ────────────────────────────────────────────────────────────

test("CHOREO: the very first look is WHITE, and only a first look is", () => {
  const first = choreographGate({ history: [], underTest: true });
  assert.equal(first.fill, "white");
  assert.equal(first.motion, "pulse");
  assert.equal(first.first_worked, true);

  // Any history at all disqualifies white. This is the property that makes a
  // white square unambiguous: it can only ever mean "never seen before".
  for (const h of [
    [{ attempt: 1, status: "pass" }],
    [{ attempt: 1, status: "fail" }],
    [{ attempt: 2, status: "error" }],
  ]) {
    const c = choreographGate({ history: h, underTest: true });
    assert.notEqual(c.fill, "white", `history ${JSON.stringify(h)} must not be a first look`);
    assert.equal(c.motion, "pulse", "it still pulses — it is under test");
  }
});

test("CHOREO: first look is keyed on HISTORY, not on the attempt number", () => {
  // A phase can abort before reaching a gate, so a gate may first execute in
  // attempt 3. Keying on `attempt === 1` would paint that gate as already-seen
  // at the very moment it is being looked at for the first time.
  const c = choreographGate({ history: [], underTest: true });
  assert.equal(c.first_worked, true, "no executions yet → first look, whatever the attempt number");

  const seen = choreographGate({ history: [{ attempt: 3, status: "fail" }], underTest: true });
  assert.equal(seen.first_worked, false, "executed once in attempt 3 → no longer a first look");
  assert.equal(seen.fill, "red", "and it pulses the colour it earned");
});

test("CHOREO: not_run is never a verdict — it does not count as an execution", () => {
  // INVARIANT I-3. A gate its phase never reached was not measured, and
  // treating that as a failure would fabricate a result.
  const c = choreographGate({ history: [{ attempt: 1, status: "not_run" }], underTest: true });
  assert.equal(c.fill, "white", "an unreached gate has still never been executed");
  assert.equal(c.ever_ran, false);
});

test("CHOREO: blue is 'never failed', green is 'recovered'", () => {
  assert.equal(choreographGate({ history: [{ attempt: 1, status: "pass" }] }).fill, "blue");
  assert.equal(
    choreographGate({
      history: [{ attempt: 1, status: "fail" }, { attempt: 2, status: "pass" }],
    }).fill,
    "green",
  );
});

test("CHOREO: the regression mark is permanent for the run", () => {
  // "star persists forever for that run". A mark that cleared on the next pass
  // would hide a flapping gate — which is the exact pathology it exposes.
  const broke = choreographGate({
    history: [{ attempt: 1, status: "pass" }, { attempt: 2, status: "fail" }],
  });
  assert.equal(broke.fill, "red", "it reports where it stands NOW");
  assert.equal(broke.mark, "regression");

  const recovered = choreographGate({
    history: [
      { attempt: 1, status: "pass" },
      { attempt: 2, status: "fail" },
      { attempt: 3, status: "pass" },
    ],
  });
  assert.equal(recovered.fill, "green", "fixed again");
  assert.equal(recovered.mark, "regression", "but the instability is still recorded");
});

test("CHOREO: a gate that only ever failed is NOT a regression", () => {
  const c = choreographGate({
    history: [{ attempt: 1, status: "fail" }, { attempt: 2, status: "fail" }],
  });
  assert.equal(c.fill, "red");
  assert.equal(c.mark, null, "never passed, so nothing regressed — the star must mean something");
});

test("CHOREO: abandoned is slate, still, and keeps any star it earned", () => {
  const c = choreographGate({
    history: [{ attempt: 1, status: "pass" }, { attempt: 2, status: "fail" }],
    abandoned: true,
  });
  assert.equal(c.fill, "slate");
  assert.equal(c.motion, "still", "a stall must never look like work in progress");
  assert.equal(c.mark, "regression");
});

test("CHOREO: histories skip attempts whose gate_results were never published", () => {
  // null ≠ []. A null means the runner published nothing for that attempt,
  // which is different from an attempt in which every gate was absent.
  const h = historiesFrom([
    { attempt: 1, gate_results: null },
    { attempt: 2, gate_results: [{ id: "G01", status: "pass" }] },
  ]);
  assert.deepEqual(h.get("G01"), [{ attempt: 2, status: "pass" }]);
});

// ── BUILD SCAN ──────────────────────────────────────────────────────────────

test("BUILD: the scaffold is 0% and golden is 100% — the real fixtures", async () => {
  // The two endpoints are checked in, so this is a measurement, not a mock.
  const stub = await scanBuild({
    targetDir: join(TASK, "scaffold"),
    scaffoldDir: join(TASK, "scaffold"),
    goldenDir: join(TASK, "golden"),
  });
  assert.equal(stub.totals.fill, 0);
  assert.equal(stub.totals.stubs_remaining, 38, "the scaffold ships 38 stubs");
  assert.equal(stub.totals.stubs_initial, 38);

  const done = await scanBuild({
    targetDir: join(TASK, "golden"),
    scaffoldDir: join(TASK, "scaffold"),
    goldenDir: join(TASK, "golden"),
  });
  assert.equal(done.totals.fill, 1);
  assert.equal(done.totals.stubs_remaining, 0);
  assert.equal(done.totals.complete, 4, "all four tracked files complete");
});

test("BUILD: the denominator is READ from the scaffold, never hardcoded", () => {
  // Add a required function to the task and the denominator must follow with no
  // number to maintain anywhere.
  const baseline = 'a(){throw new Error("not implemented")}\nb(){throw new Error("not implemented")}';
  const target = 'a(){return 1}\nb(){throw new Error("not implemented")}';
  const m = measureFile({ rel: "src/x.ts", target, baseline, reference: null });
  assert.equal(m.stubs_initial, 2);
  assert.equal(m.stubs_remaining, 1);
  assert.equal(m.fill, 0.5);
  assert.equal(m.metric, "stub-ratio");
});

test("BUILD: an ABSENT file is `untouched`, never 0% — absence is not emptiness", () => {
  const m = measureFile({ rel: "src/x.ts", target: null, baseline: "x", reference: null });
  assert.equal(m.state, "untouched");
  assert.equal(m.fill, null, "null means unknowable; 0 would claim a measurement");
  assert.match(m.reason, /absent/);
});

test("BUILD: the two metrics are LABELLED and never blended", () => {
  // public/app.js has no stubs — its fill is a line ratio, and a reader must be
  // able to tell that apart from an implemented-functions ratio.
  const m = measureFile({
    rel: "public/app.js",
    target: "a\nb\nc\nd\ne\nf",
    baseline: "a\nb",
    reference: "a\nb\nc\nd\ne\nf\ng\nh\ni\nj",
  });
  assert.equal(m.metric, "line-ratio");
  assert.equal(m.stubs_initial, null, "a line-ratio file has no stub denominator, and says so");
});

test("BUILD: fill is clamped — a worker that adds stubs never goes negative", () => {
  const baseline = 'a(){throw new Error("not implemented")}';
  const target = 'a(){throw new Error("not implemented")}\nb(){throw new Error("not implemented")}';
  const m = measureFile({ rel: "src/x.ts", target, baseline, reference: null });
  assert.ok(m.fill >= 0 && m.fill <= 1, `fill ${m.fill} escaped [0,1]`);
});

test("BUILD: the headline total is one ratio over one population, not a mean", async () => {
  // A mean of per-file fills would weight a 5-stub file the same as a 21-stub
  // file and report a number matching nothing countable on disk.
  const r = await scanBuild({
    targetDir: join(TASK, "golden"),
    scaffoldDir: join(TASK, "scaffold"),
    goldenDir: join(TASK, "golden"),
  });
  const stubFiles = r.files.filter((f) => f.metric === "stub-ratio");
  assert.equal(
    r.totals.stubs_initial,
    stubFiles.reduce((n, f) => n + f.stubs_initial, 0),
    "the denominator is the summed stub population",
  );
});

// ── THE PAUSE GATE (the safety property) ────────────────────────────────────

test("PAUSE: an open grading phase blocks the lane unconditionally", async () => {
  const v = await pauseVerdict({ gradingActive: true });
  assert.equal(v.may_run, false);
  assert.match(v.reason, /grader/);
});

test("PAUSE: a bound port blocks the lane — freePort() would kill the oracle", async () => {
  // THE FAILURE THIS PREVENTS: harness.ts freePort() runs
  // `lsof -nP -iTCP:8002 -sTCP:LISTEN -t` and SIGKILLs every pid returned. That
  // selector is PORT-ONLY — probed directly on this host — so a lane starting a
  // server while the oracle holds :8002 kills the oracle's server mid-attempt
  // and voids real measurement data.
  const net = await import("node:net");
  const srv = net.createServer();
  const port = 18777;
  await new Promise((r) => srv.listen(port, r));
  try {
    const v = await pauseVerdict({ gradingActive: false, port });
    assert.equal(v.may_run, false, "a bound port MUST block the lane");
    assert.match(v.reason, /SIGKILL/, "the reason must name the actual danger");
  } finally {
    await new Promise((r) => srv.close(r));
  }
});

test("PAUSE: a free port with no grading permits the lane", async () => {
  const v = await pauseVerdict({ gradingActive: false, port: 18778 });
  assert.equal(v.may_run, true);
});

// ── THE VITEST PARSE (the false-reassurance bug) ────────────────────────────

test("PARSE: a spec that fails to IMPORT is not_loaded, never skipped", () => {
  // MEASURED 2026-08-13: when a spec file fails to import — the normal case
  // while the agent is mid-edit — vitest marks the FILE `failed` and every test
  // inside it `skipped`, with numFailedTests 0. Mapping those to the same bucket
  // the `-t` filter produces made 23 broken gates render as intentional
  // exclusions: a calm grid, hiding a tree that does not compile.
  const doc = JSON.stringify({
    testResults: [
      {
        name: "/x/gates-01-08.test.ts",
        status: "failed",
        assertionResults: [
          { ancestorTitles: ["S"], title: "a", status: "skipped" },
          { ancestorTitles: ["S"], title: "b", status: "skipped" },
        ],
      },
      {
        name: "/x/acceptance.test.ts",
        status: "passed",
        assertionResults: [{ ancestorTitles: ["S"], title: "c", status: "passed", duration: 1 }],
      },
    ],
  });
  const out = parseVitestJson(doc);
  assert.deepEqual(out.failed_to_load, ["/x/gates-01-08.test.ts"]);
  assert.equal(out.results[0].status, "not_loaded");
  assert.equal(out.results[1].status, "not_loaded");
  assert.equal(out.results[2].status, "pass");
});

test("PARSE: a genuinely skipped test in a healthy file stays skipped", () => {
  const doc = JSON.stringify({
    testResults: [
      {
        name: "/x/ok.test.ts",
        status: "passed",
        assertionResults: [{ ancestorTitles: ["S"], title: "a", status: "skipped" }],
      },
    ],
  });
  assert.equal(parseVitestJson(doc).results[0].status, "skipped");
});

// ── THE FOLD ────────────────────────────────────────────────────────────────

const ROSTER = {
  gates: [
    { id: "CONF", phase: "conformance", full_name: "[CONF] conformance pre-gate" },
    { id: "G16", phase: "backend", full_name: "S > [G16] REQ-BIND — x" },
    { id: "G01", phase: "backend", full_name: "S > [G01] a" },
    { id: "G02", phase: "backend", full_name: "S > [G02] b" },
    { id: "F01", phase: "frontend", full_name: "F > [F01] c" },
  ],
};

test("FOLD: every roster gate appears — the board never reasons about absence", () => {
  const f = foldLive({ roster: ROSTER, results: [], deferredIds: new Set(["G16"]) });
  assert.equal(f.gates.length, ROSTER.gates.length);
  assert.equal(f.counts.total, ROSTER.gates.length);
});

test("FOLD: the four negatives stay distinct — they mean opposite things", () => {
  const f = foldLive({
    roster: ROSTER,
    results: [
      { full_name: "S > [G01] a", status: "pass", duration_ms: 2 },
      { full_name: "S > [G02] b", status: "not_loaded", duration_ms: null },
    ],
    deferredIds: new Set(["G16"]),
  });
  const by = Object.fromEntries(f.gates.map((g) => [g.id, g.live]));
  assert.equal(by.G01, "pass");
  assert.equal(by.G02, "not_loaded", "broken code must NOT read as an intentional exclusion");
  assert.equal(by.G16, "deferred");
  assert.equal(by.F01, "deferred");
  assert.equal(by.CONF, "deferred");
});

test("FOLD: the deferral reasons are stated, never left implicit", () => {
  const f = foldLive({ roster: ROSTER, results: [], deferredIds: new Set(["G16"]) });
  const g16 = f.gates.find((g) => g.id === "G16");
  assert.match(g16.deferred_reason, /8002/, "G16's reason must name the port it owns");
  const f01 = f.gates.find((g) => g.id === "F01");
  assert.match(f01.deferred_reason, /wall-clock/, "the browser exclusion must name why");
  assert.equal(DEFERRED.G16.length > 0, true);
});

test("FOLD: a gate the lane produced no row for is unmeasured, not failing", () => {
  const f = foldLive({ roster: ROSTER, results: [], deferredIds: new Set() });
  const g01 = f.gates.find((g) => g.id === "G01");
  assert.equal(g01.live, "unmeasured");
  assert.equal(f.counts.fail, 0, "absence must never be counted as a failure");
});

// ── THE SERVED SURFACE ──────────────────────────────────────────────────────

test("SURFACE: a missing artifact is ok:true with a reason, never an error", async () => {
  // The lane is OPTIONAL. Its absence is the normal state and must not present
  // as a broken board — the authoritative wall is unaffected either way.
  const r = await readLive({ runsRoot: join(HERE, "..", "does-not-exist-xyz") });
  assert.equal(r.ok, true);
  assert.equal(r.running, false);
  assert.deepEqual(r.unwired, ["live-lane"]);
  assert.match(r.unwired_reasons["live-lane"], /not running/);
  assert.equal(r.lane, null);
  assert.equal(r.build, null);
});
