// ─────────────────────────────────────────────────────────────────────────────
// LIVE LANE — provisional gate measurement while the agent is still working
//
//     node control/live-lane.mjs [--once] [--runs-root DIR] [--interval-ms N]
//
// WHY THIS EXISTS (WO-LIVE-GATES)
//
// The authoritative grader runs ONCE per attempt, at the end. Measured on this
// host: ~30 minutes of agent work during which the gate wall has nothing to
// show, because per-gate outcomes are written only at attempt end. The board is
// dark exactly while the interesting thing is happening.
//
// This lane measures the SAME gates continuously against a SNAPSHOT of the
// worktree, and publishes a PROVISIONAL result the board renders as a distinct,
// clearly-labelled overlay. It never writes into the scored path.
//
// ── WHY NO PORT ISOLATION (measured, 2026-08-13) ─────────────────────────────
//
// The obvious design is loopback aliases (127.0.0.2, .3 …) plus a bind-address
// shim so several lanes can hold :8002 at once. It was investigated and
// REJECTED on evidence:
//
//   1. `harness.ts freePort()` runs `lsof -nP -iTCP:8002 -sTCP:LISTEN -t` and
//      SIGKILLs every pid it returns. That selector is PORT-ONLY — probed
//      directly: a listener bound to 127.0.0.1 is returned by a query that
//      names no address. Aliases isolate bind(); they do NOT isolate freePort().
//      Every lane would kill every other lane's server, AND the oracle's.
//      Making it work requires editing the oracle — the one thing the design
//      was trying to avoid.
//   2. The whole backend suite is 2.24s (35 port-free gates in 236ms, 21
//      port-bound in 2.0s). Isolation buys nothing a 2-second serial run needs.
//   3. Aliases need persistent `sudo ifconfig lo0 alias` — a privileged, host-
//      wide mutation for a test lane.
//
// So the lane runs SERIALLY on the real port and SCHEDULES around the oracle,
// which is what every mature runner does with an exclusive resource (NCrunch
// ExclusivelyUses, JUnit @ResourceLock, linux perf "(exclusive)").
//
// ── THE PAUSE GATE IS THE SAFETY PROPERTY ────────────────────────────────────
//
// If this lane starts a server while the oracle is grading, the lane's own
// `freePort()` SIGKILLs the oracle's server mid-attempt and VOIDS REAL
// MEASUREMENT DATA. So a cycle is skipped when EITHER signal says the oracle is
// active, and an unreadable signal counts as active. Fails closed, always.
//
// ── WHAT IS DEFERRED, AND WHY IT IS A THIRD STATE ────────────────────────────
//
//   G16  asserts that a SECOND bind on :8002 fails. A lane holding the port
//        makes it pass or fail for reasons unrelated to the model under test —
//        both outcomes are noise. It is 652ms of the 2.24s lane besides.
//   14 frontend gates  need chromium beside a resident 35B model whose
//        wall-clock is RECORDED DATA. A browser's navigation bursts perturb the
//        measurement the benchmark reports.
//   CONF  the conformance pre-gate is a chromium project too.
//
// These are DEFERRED, never "pending" and never "passing". A deferred square
// must be structurally distinguishable from an unmeasured one, or the board
// implies a verdict it does not have.
// ─────────────────────────────────────────────────────────────────────────────

import { execFile, spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { scanBuild } from "./build-scan.mjs";
import { readGateActivity } from "./gate-events.mjs";
import { newestLog } from "./runstate.mjs";

const execFileAsync = promisify(execFile);

const HERE = dirname(fileURLToPath(import.meta.url));
const BENCH_ROOT = resolve(HERE, "..");
const TASK_DIR = join(BENCH_ROOT, "tasks", "backgammon");
const GATES_DIR = join(TASK_DIR, "gates");

/** The contract version the board can assert against. */
export const LIVE_LANE_CONTRACT_VERSION = 1;

/** The port the oracle owns. Never bound by this lane except via vitest. */
export const ORACLE_PORT = 8002;

/**
 * Gates the live lane will NEVER measure, and the stated reason for each.
 *
 * The reason travels WITH the id to the front end, because "deferred" without a
 * reason is indistinguishable from "we forgot". A test-name substring is used
 * rather than a roster id so the vitest `-t` filter and this list cannot drift.
 */
export const DEFERRED = {
  G16: "owns :8002 exclusively — a live lane holding the port makes this gate pass or fail for reasons unrelated to the model under test",
  __frontend:
    "chromium is not run live — a browser beside the resident model perturbs the wall-clock this benchmark records as data",
  __conformance:
    "the conformance pre-gate is a chromium project and boots its own server on :8002",
};

/** The vitest `-t` pattern that excludes the exclusivity gate from the lane. */
const EXCLUDE_PATTERN = "^(?!.*REQ-BIND).*$";

/** Backend spec files the lane runs. Frontend + conformance are deferred. */
const LANE_FILES = [
  "backend/acceptance.test.ts",
  "backend/behavior-fixtures.test.ts",
  "backend/negative-controls.test.ts",
  "backend/gates-01-08.test.ts",
  "backend/gates-09-12.test.ts",
  "backend/gates-13-16.test.ts",
  "backend/edge/edge-gates.test.ts",
];

// ── SNAPSHOT ────────────────────────────────────────────────────────────────

/**
 * A content fingerprint over the tracked sources.
 *
 * mtime+size, NOT a content hash of every byte: the point is to SKIP work, and
 * the cheapest correct signal wins. Measured: the whole clone is 25ms, so the
 * gate is not about clone cost — it is about not re-running the suite (and not
 * republishing a "new" result) when nothing changed.
 */
export async function fingerprint(dir, rels) {
  const h = createHash("sha256");
  for (const rel of rels) {
    try {
      const st = await fs.stat(join(dir, rel));
      h.update(`${rel}:${st.size}:${Math.round(st.mtimeMs)}\n`);
    } catch {
      h.update(`${rel}:absent\n`);
    }
  }
  return `sha256:${h.digest("hex").slice(0, 32)}`;
}

/**
 * Clone the worktree with APFS copy-on-write.
 *
 * `cp -c -R` clones each file individually (the man page is explicit that APFS
 * has no constant-time directory clone), so cost scales with FILE COUNT. The
 * task worktree is ~98 files; measured on a real attempt worktree: 25ms, and
 * the clone consumes no additional disk.
 *
 * The snapshot exists because the agent is MID-EDIT. Grading the live worktree
 * measures half-written files, which is noise, not a result.
 */
async function snapshot(srcDir, dstDir) {
  await fs.rm(dstDir, { recursive: true, force: true });
  await fs.mkdir(dirname(dstDir), { recursive: true });
  await execFileAsync("cp", ["-c", "-R", srcDir, dstDir], { timeout: 30_000 });
}

// ── THE PAUSE GATE ──────────────────────────────────────────────────────────

/** Is anything listening on the oracle's port right now? */
export async function portBusy(port = ORACLE_PORT) {
  try {
    const { stdout } = await execFileAsync(
      "lsof",
      ["-nP", `-iTCP:${port}`, "-sTCP:LISTEN", "-t"],
      { timeout: 5_000 },
    );
    return stdout.trim().length > 0;
  } catch (err) {
    // lsof exits 1 with NO output when nothing matches — that is "free", not an
    // error. Any OTHER failure means the signal is unreadable, and an
    // unreadable safety signal must read as BUSY.
    if (err && typeof err.code === "number" && err.code === 1 && !String(err.stdout ?? "").trim()) {
      return false;
    }
    return true;
  }
}

/**
 * Decide whether this cycle may run. FAILS CLOSED.
 *
 * Two independent signals, either of which blocks:
 *   - the oracle's port is bound (it is booting or serving a graded test)
 *   - the harness says a grading phase is open
 *
 * Two signals rather than one because each covers the other's blind spot: the
 * port is free during the gap between a phase starting and its first server
 * boot, and the log signal is absent for a run this process did not observe.
 */
export async function pauseVerdict({ gradingActive, port = ORACLE_PORT }) {
  if (gradingActive === true) {
    return { may_run: false, reason: "the authoritative grader has an open phase — the lane never runs beside it" };
  }
  if (await portBusy(port)) {
    return {
      may_run: false,
      reason: `:${port} is bound — the lane's own freePort() would SIGKILL whatever holds it, including the oracle`,
    };
  }
  return { may_run: true, reason: null };
}

// ── THE RUN ─────────────────────────────────────────────────────────────────

/**
 * Parse vitest's JSON reporter into per-test outcomes.
 *
 * Keyed by FULL NAME (`describe > describe > it`), which is exactly the string
 * `roster.mjs` builds its ids from — so the join to the roster is the roster's
 * own key and no second matcher exists to drift.
 *
 * ── A LOAD FAILURE IS NOT A SKIP (measured 2026-08-13) ──────────────────────
 *
 * When a spec file fails to IMPORT — the normal case while the agent is
 * mid-edit and the TypeScript does not compile — vitest still writes a report,
 * and it marks the file `status:"failed"` with EVERY test inside it
 * `status:"skipped"`. Verified directly against a deliberately broken
 * `src/game.ts`: `gates-01-08.test.ts status=failed`, 8 assertions, all
 * "skipped", while `numFailedTests` was 0.
 *
 * A naive reader maps those to the same "skipped" bucket the `-t` filter
 * produces, and 23 gates then present as DEFERRED — a benign, intentional
 * state — when in truth they were never executed because the code is broken.
 * That is the exact false reassurance this whole surface exists to prevent.
 *
 * So the file's own status is carried through, and the discriminator is stated:
 * a file is a LOAD FAILURE when it failed and yet no individual test in it
 * failed. Its tests become `not_loaded`, never `skipped`.
 */
export function parseVitestJson(text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    return null;
  }
  const out = [];
  const failedToLoad = [];

  for (const file of doc?.testResults ?? []) {
    const rows = file?.assertionResults ?? [];
    const anyTestFailed = rows.some((t) => t.status === "failed");
    const loadFailed = file?.status === "failed" && !anyTestFailed;
    if (loadFailed) failedToLoad.push(file?.name ?? "<unnamed>");

    for (const t of rows) {
      const chain = [...(t.ancestorTitles ?? []), t.title].filter(Boolean);
      let status;
      if (t.status === "passed") status = "pass";
      else if (t.status === "failed") status = "fail";
      else status = loadFailed ? "not_loaded" : "skipped";

      out.push({
        full_name: chain.join(" > "),
        title: t.title ?? null,
        status,
        duration_ms: Number.isFinite(t.duration) ? Math.round(t.duration) : null,
      });
    }
  }
  return { results: out, failed_to_load: failedToLoad };
}


/**
 * Run the lane against a snapshot. Returns per-test outcomes, or a stated
 * failure — never a partial result presented as complete.
 */
async function runLane(targetDir) {
  const outFile = join(tmpdir(), `wevibe-live-lane-${process.pid}-${Date.now()}.json`);
  const args = [
    "vitest",
    "run",
    ...LANE_FILES,
    "-t",
    EXCLUDE_PATTERN,
    "--reporter=json",
    `--outputFile=${outFile}`,
  ];

  const started = Date.now();
  const code = await new Promise((res) => {
    const p = spawn("npx", args, {
      cwd: GATES_DIR,
      env: { ...process.env, BENCH_TARGET: targetDir, CI: "1" },
      stdio: ["ignore", "ignore", "pipe"],
      // NICED below the resident model. The lane must never steal a slice from
      // the 35B whose wall-clock this benchmark records.
    });
    let stderr = "";
    p.stderr.on("data", (c) => {
      stderr += String(c);
    });
    p.on("error", () => res({ code: null, stderr }));
    p.on("close", (c) => res({ code: c, stderr }));
  });

  const duration = Date.now() - started;
  const raw = await fs.readFile(outFile, "utf8").catch(() => null);
  await fs.rm(outFile, { force: true }).catch(() => {});

  if (raw === null) {
    return {
      ok: false,
      // A snapshot that does not PARSE is the expected case, not an error: the
      // agent is mid-edit and TypeScript frequently will not compile. It must be
      // reported as its own state so the board can hold the prior grid and badge
      // it stale, rather than falling back to "run everything" or blanking.
      reason:
        code.code === null
          ? "the lane runner could not be spawned"
          : "the snapshot produced no test report — it most likely does not parse (normal while the agent is mid-edit)",
      duration_ms: duration,
      results: null,
      failed_to_load: [],
    };
  }

  const parsed = parseVitestJson(raw);
  if (parsed === null) {
    return {
      ok: false,
      reason: "the lane's JSON report was unreadable",
      duration_ms: duration,
      results: null,
      failed_to_load: [],
    };
  }
  return {
    ok: true,
    reason: null,
    duration_ms: duration,
    results: parsed.results,
    failed_to_load: parsed.failed_to_load,
  };
}

// ── FOLD TO THE PUBLISHED SHAPE ─────────────────────────────────────────────

/**
 * Join lane outcomes onto the roster and mark everything the lane cannot see.
 *
 * EVERY roster gate appears in the output — measured, deferred, not-loaded or
 * unmeasured — so the front end can key by id and never has to reason about
 * absence.
 *
 * ── FOUR DISTINCT NEGATIVES, DELIBERATELY NOT COLLAPSED ─────────────────────
 *
 *   deferred    the lane will NEVER measure this gate, by design, with a reason
 *   not_loaded  its spec file failed to import — the code is BROKEN, not benign
 *   unmeasured  the lane ran and simply produced no row for it
 *   fail        it executed and failed
 *
 * Collapsing `not_loaded` into `deferred` (which an earlier revision did, via
 * vitest's "skipped") makes 23 broken gates look intentionally excluded. These
 * stay separate because they mean opposite things to an operator.
 */
export function foldLive({ roster, results, deferredIds = new Set() }) {
  const byName = new Map();
  for (const r of results ?? []) byName.set(r.full_name, r);

  const gates = (roster?.gates ?? []).map((g) => {
    if (g.phase === "frontend") {
      return { id: g.id, live: "deferred", deferred_reason: DEFERRED.__frontend, duration_ms: null };
    }
    if (g.phase === "conformance") {
      return { id: g.id, live: "deferred", deferred_reason: DEFERRED.__conformance, duration_ms: null };
    }
    if (deferredIds.has(g.id)) {
      return { id: g.id, live: "deferred", deferred_reason: DEFERRED[g.id] ?? "excluded from the live lane", duration_ms: null };
    }
    const hit = byName.get(g.full_name);
    if (!hit) {
      // The lane ran but produced no row for this gate at all — the file was
      // never collected. NOT a failure and NOT a deferral.
      return { id: g.id, live: "unmeasured", deferred_reason: null, duration_ms: null };
    }
    if (hit.status === "not_loaded") {
      return {
        id: g.id,
        live: "not_loaded",
        deferred_reason: null,
        duration_ms: null,
        note: "its spec file failed to import against this snapshot — the source most likely does not compile",
      };
    }
    if (hit.status === "skipped") {
      // Reached only for a gate the `-t` filter genuinely excluded. G16 is
      // already handled above by id, so this is the honest residue.
      return { id: g.id, live: "deferred", deferred_reason: "excluded by the lane's test filter", duration_ms: hit.duration_ms };
    }
    return { id: g.id, live: hit.status, deferred_reason: null, duration_ms: hit.duration_ms };
  });

  const tally = (v) => gates.filter((g) => g.live === v).length;
  return {
    gates,
    counts: {
      pass: tally("pass"),
      fail: tally("fail"),
      deferred: tally("deferred"),
      not_loaded: tally("not_loaded"),
      unmeasured: tally("unmeasured"),
      total: gates.length,
    },
  };
}

// ── ONE CYCLE ───────────────────────────────────────────────────────────────

/**
 * Snapshot → build scan → (pause gate) → lane → publish.
 *
 * The BUILD SCAN runs even when the pause gate blocks the test lane: reading
 * files binds no port and cannot disturb the oracle, and the build axis is the
 * signal that matters most during the long pre-grading window.
 */
export async function cycle({ worktree, runDir, roster, gradingActive, snapshotDir, previous = null }) {
  const observedAt = Date.now();
  const tracked = ["src/game.ts", "src/ai.ts", "src/server.ts", "public/app.js"];
  const hash = await fingerprint(worktree, tracked);

  // ── THE HASH GATE. The cheapest cycle is the one that does not happen. ────
  // Unchanged sources mean the previous measurement is STILL TRUE, so it is
  // republished verbatim rather than recomputed — and the board sees no change,
  // so nothing repaints and no square re-pulses for work that did not occur.
  if (previous && previous.snapshot?.content_hash === hash) {
    return { ...previous, republished: true };
  }

  await snapshot(worktree, snapshotDir);

  const build = await scanBuild({
    targetDir: snapshotDir,
    scaffoldDir: join(TASK_DIR, "scaffold"),
    goldenDir: join(TASK_DIR, "golden"),
    observedAt,
  });

  const pause = await pauseVerdict({ gradingActive });
  if (!pause.may_run) {
    return {
      ok: true,
      contract_version: LIVE_LANE_CONTRACT_VERSION,
      provisional: true,
      lane: "vitest-backend",
      run_dir: runDir,
      snapshot: { taken_at: observedAt, content_hash: hash, parsed: null, stale_reason: pause.reason },
      ran_at: null,
      duration_ms: null,
      // The PRIOR gate result is held, not blanked: it is still the last thing
      // actually measured, and it is now labelled stale with a stated reason.
      gates: previous?.gates ?? [],
      counts: previous?.counts ?? null,
      build,
    };
  }

  const run = await runLane(snapshotDir);
  if (!run.ok) {
    return {
      ok: true,
      contract_version: LIVE_LANE_CONTRACT_VERSION,
      provisional: true,
      lane: "vitest-backend",
      run_dir: runDir,
      snapshot: { taken_at: observedAt, content_hash: hash, parsed: false, stale_reason: run.reason },
      ran_at: Date.now(),
      duration_ms: run.duration_ms,
      gates: previous?.gates ?? [],
      counts: previous?.counts ?? null,
      build,
    };
  }

  const folded = foldLive({ roster, results: run.results, deferredIds: new Set(["G16"]) });

  // ── `parsed` REPORTS THE SNAPSHOT, NOT THE RUNNER ────────────────────────
  //
  // vitest exits with a report even when specs fail to import, so "we got a
  // report" is NOT "the code compiles". When any file failed to load, the
  // snapshot did not fully parse and the board must badge it — otherwise a
  // broken tree renders as a clean run with a lot of grey squares.
  const brokeToLoad = run.failed_to_load.length > 0;
  return {
    ok: true,
    contract_version: LIVE_LANE_CONTRACT_VERSION,
    provisional: true,
    lane: "vitest-backend",
    run_dir: runDir,
    snapshot: {
      taken_at: observedAt,
      content_hash: hash,
      parsed: !brokeToLoad,
      stale_reason: brokeToLoad
        ? `${run.failed_to_load.length} spec file(s) failed to import against this snapshot — the source does not compile yet`
        : null,
      failed_to_load: run.failed_to_load,
    },
    ran_at: Date.now(),
    duration_ms: run.duration_ms,
    gates: folded.gates,
    counts: folded.counts,
    build,
  };
}

// ── THE DAEMON ──────────────────────────────────────────────────────────────

/**
 * Locate the live worktree to measure.
 *
 * Derived from the run directory the harness is writing into, exactly the way
 * every other reader here resolves the live run. Returns null when no worktree
 * exists yet — a cell that has not seeded one is early, not broken.
 */
export async function findWorktree(runsRoot) {
  const log = await newestLog(runsRoot);
  if (!log?.run_dir) return null;
  const sessions = join(runsRoot, log.run_dir, "sessions");
  let entries;
  try {
    entries = await fs.readdir(sessions, { withFileTypes: true });
  } catch {
    return null;
  }

  // Newest session directory that actually contains a worktree.
  const candidates = [];
  for (const ent of entries) {
    if (!ent.isDirectory()) continue;
    const wt = join(sessions, ent.name, "worktree");
    const st = await fs.stat(wt).catch(() => null);
    if (st?.isDirectory()) candidates.push({ path: wt, mtime: st.mtimeMs });
  }
  candidates.sort((a, b) => b.mtime - a.mtime);
  return candidates.length > 0 ? { worktree: candidates[0].path, run_dir: log.run_dir } : null;
}

/**
 * Publish atomically.
 *
 * tmp + rename, because the control plane reads this file on an independent
 * schedule and a torn read must be impossible rather than merely unlikely.
 */
async function publish(runsRoot, doc) {
  const final = join(runsRoot, "live-gates.json");
  const tmp = `${final}.tmp-${process.pid}`;
  await fs.writeFile(tmp, `${JSON.stringify(doc, null, 2)}\n`, "utf8");
  await fs.rename(tmp, final);
}

/** Is the authoritative grader mid-phase? Read from the harness's own log. */
async function gradingIsActive(runsRoot) {
  try {
    const { status } = await readGateActivity(runsRoot);
    return status?.grading === true;
  } catch {
    // An unreadable signal must read as ACTIVE. Fails closed — the lane never
    // guesses that it is safe to bind the oracle's port.
    return true;
  }
}

/** Enumerate the suite via the harness's own roster script. Never invents one. */
async function loadRoster() {
  const out = join(tmpdir(), `wevibe-live-roster-${process.pid}.json`);
  try {
    await execFileAsync("node", [join(GATES_DIR, "roster.mjs"), "--out", out], {
      cwd: GATES_DIR,
      timeout: 60_000,
      maxBuffer: 8 * 1024 * 1024,
    });
    const doc = JSON.parse(await fs.readFile(out, "utf8"));
    return Array.isArray(doc?.gates) && doc.gates.length > 0 ? doc : null;
  } catch {
    return null;
  } finally {
    await fs.rm(out, { force: true }).catch(() => {});
  }
}

async function main() {
  const argv = process.argv.slice(2);
  const flag = (name, dflt) => {
    const i = argv.indexOf(name);
    return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
  };
  const once = argv.includes("--once");
  const runsRoot = resolve(flag("--runs-root", join(BENCH_ROOT, "runs")));
  // The DEBOUNCE. Long enough that a burst of edits inside one agent turn
  // collapses into a single measurement, short enough to stay a live instrument
  // (Wallaby's published guidance is that the loop must stay under 1–2s to be
  // worth having; the lane's own work is ~2.3s).
  const intervalMs = Number(flag("--interval-ms", "3000"));
  const snapshotDir = join(tmpdir(), "wevibe-live-lane-snapshot");

  const roster = await loadRoster();
  if (!roster) {
    process.stderr.write("live-lane: the gate roster could not be enumerated — refusing to publish a suite it cannot describe\n");
    process.exit(1);
  }
  process.stderr.write(`live-lane: suite=${roster.total} runs-root=${runsRoot} interval=${intervalMs}ms\n`);

  let previous = null;
  for (;;) {
    const found = await findWorktree(runsRoot);
    if (found) {
      try {
        const doc = await cycle({
          worktree: found.worktree,
          runDir: found.run_dir,
          roster,
          gradingActive: await gradingIsActive(runsRoot),
          snapshotDir,
          previous,
        });
        previous = doc;
        await publish(runsRoot, doc);
      } catch (err) {
        // NEVER SWALLOW. A lane that dies silently leaves a stale grid on the
        // board that looks live; the operator must see why.
        process.stderr.write(`live-lane: cycle failed — ${err?.message ?? err}\n`);
      }
    }
    if (once) return;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

// Only run as a program, never on import — the fold functions above are
// imported by tests and by the control plane's reader.
if (process.argv[1] && process.argv[1].endsWith("live-lane.mjs")) {
  main().catch((err) => {
    process.stderr.write(`live-lane: fatal — ${err?.stack ?? err}\n`);
    process.exit(1);
  });
}
