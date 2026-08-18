#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  foldGateResults,
  loadRoster,
  makeMatcher,
  playwrightGateResults,
  vitestGateResults,
  runnerFailureObserved,
} from "./gate-results.mjs";

const GATES_DIR = path.dirname(fileURLToPath(import.meta.url));

function argValue(flag) {
  const idx = process.argv.indexOf(flag);
  if (idx < 0) {
    return null;
  }
  const next = process.argv[idx + 1];
  return next && !next.startsWith("--") ? next : null;
}

const targetArg = argValue("--target");
const outArg = argValue("--out");
const rosterArg = argValue("--roster");

const TARGET = path.resolve(
  targetArg || process.env.BENCH_TARGET || path.join(GATES_DIR, "..", "golden"),
);
const env = { ...process.env, BENCH_TARGET: TARGET };

const nowIso = new Date().toISOString();
const defaultOut = path.join(
  GATES_DIR,
  "..",
  "runs",
  `${nowIso.replace(/[:.]/g, "-")}-report.json`,
);
const OUT_FILE = path.resolve(outArg || defaultOut);

function stripAnsi(text) {
  return String(text ?? "").replace(/\u001B\[[0-9;]*m/g, "");
}

function truncate(text, max) {
  const clean = String(text ?? "").trim();
  if (clean.length <= max) {
    return clean;
  }
  return `${clean.slice(0, max)}…`;
}

// ── PER-GATE OUTCOMES (WO-GATE-ROSTER) ──────────────────────────────────────
//
// The folding logic lives in `gate-results.mjs` — pure, roster-parameterised
// and therefore directly testable. It cannot be exercised from here: running
// this file grades a real target, which boots a server on :8002 and cannot be
// done beside a live cell.
const ROSTER = loadRoster(rosterArg);
const MATCHER = makeMatcher(ROSTER);

/**
 * Announce the gate set a phase is about to execute.
 *
 * PER-PHASE-SET, NOT PER-TEST. `spawnPhase` below uses `spawnSync`, so a
 * child's output is buffered and reaches no reader until the phase has already
 * ENDED — per-test hooks inside the runners would therefore arrive as a burst
 * at phase end, which is not a live signal at all. This line is written by THIS
 * process before the child starts, so it streams immediately, exactly like the
 * existing `[report] phase=` marker that the harness already republishes.
 *
 * It carries a COUNT, not 47 ids: gate identity already lives in the roster the
 * board reads, and republishing long slug ids (which contain spaces) through a
 * whitespace-delimited log line would corrupt them. The count is the one fact
 * the roster cannot supply — the runner attesting how many gates it is about to
 * execute, which is what makes roster/runner drift detectable at all.
 */
function announceGateSet(phase) {
  if (!ROSTER.available) return;
  const count = ROSTER.gates.filter((g) => g.phase === phase).length;
  process.stderr.write(`[report] gateset phase=${phase} count=${count}\n`);
}

function firstNonEmptyLine(text) {
  const lines = String(text ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  return lines[0] || "<empty>";
}

function textFromEntry(entry) {
  if (typeof entry === "string") {
    return entry;
  }
  if (entry && typeof entry === "object" && typeof entry.text === "string") {
    return entry.text;
  }
  return "";
}

function safeProblem(check, expected, observed) {
  return {
    check: String(check ?? "unknown"),
    expected: String(expected ?? ""),
    observed: String(observed ?? ""),
  };
}

function dedupeProblems(problems) {
  const seen = new Set();
  const out = [];
  for (const problem of problems) {
    const key = `${problem.check}\u0000${problem.expected}\u0000${problem.observed}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    out.push(problem);
  }
  return out;
}

function dedupeStrings(items) {
  return [...new Set(items.filter((item) => item && String(item).trim().length > 0))];
}

function parseJsonObject(text) {
  const raw = String(text ?? "").trim();
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw);
  } catch {
    // keep going
  }

  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(raw.slice(start, end + 1));
    } catch {
      return null;
    }
  }
  return null;
}

function extractBalancedSegment(text, open, close, startIndex) {
  let depth = 0;
  let inString = false;
  let escaped = false;
  let start = -1;

  for (let i = startIndex; i < text.length; i += 1) {
    const ch = text[i];

    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else if (ch === '"') {
        inString = false;
      }
      continue;
    }

    if (ch === '"') {
      inString = true;
      continue;
    }

    if (ch === open) {
      if (depth === 0) {
        start = i;
      }
      depth += 1;
      continue;
    }

    if (ch === close) {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        return text.slice(start, i + 1);
      }
      if (depth < 0) {
        return null;
      }
    }
  }

  return null;
}

function parseProblemLine(line) {
  const clean = stripAnsi(line).trim();
  if (!clean.startsWith("PROBLEM ")) {
    return null;
  }
  const payload = clean.slice("PROBLEM ".length);
  const expectedMarker = ": expected ";
  const expectedIdx = payload.indexOf(expectedMarker);
  if (expectedIdx < 0) {
    return null;
  }

  const check = payload.slice(0, expectedIdx).trim();
  const tail = payload.slice(expectedIdx + expectedMarker.length);
  const observedMarker = ", observed ";
  const observedIdx = tail.indexOf(observedMarker);
  if (observedIdx < 0) {
    return null;
  }

  const expected = tail.slice(0, observedIdx).trim();
  const observed = tail.slice(observedIdx + observedMarker.length).trim();
  return safeProblem(check, expected, observed);
}

function parseProblemsFromTextLines(text) {
  const out = [];
  const lines = String(text ?? "").split(/\r?\n/);
  for (const line of lines) {
    const parsed = parseProblemLine(line);
    if (parsed) {
      out.push(parsed);
    }
  }
  return out;
}

function parseProblemsFromErrorMessage(message) {
  const clean = stripAnsi(String(message ?? ""));
  const out = [];
  let idx = clean.indexOf("[");

  while (idx >= 0) {
    const segment = extractBalancedSegment(clean, "[", "]", idx);
    if (!segment) {
      break;
    }
    try {
      const parsed = JSON.parse(segment);
      if (Array.isArray(parsed)) {
        for (const item of parsed) {
          if (
            item
            && typeof item === "object"
            && typeof item.check === "string"
            && Object.prototype.hasOwnProperty.call(item, "expected")
            && Object.prototype.hasOwnProperty.call(item, "observed")
          ) {
            out.push(safeProblem(item.check, item.expected, item.observed));
          }
        }
        if (out.length > 0) {
          return out;
        }
      }
    } catch {
      // keep searching for a parseable JSON array
    }

    idx = clean.indexOf("[", idx + 1);
  }

  return out;
}

function collectPlaywrightSpecs(suites, out = []) {
  if (!Array.isArray(suites)) {
    return out;
  }

  for (const suite of suites) {
    if (Array.isArray(suite?.specs)) {
      out.push(...suite.specs);
    }
    if (Array.isArray(suite?.suites)) {
      collectPlaywrightSpecs(suite.suites, out);
    }
  }
  return out;
}

function extractPlaywrightRunError(report) {
  if (!report || !Array.isArray(report.errors) || report.errors.length === 0) {
    return "";
  }

  for (const errorEntry of report.errors) {
    if (typeof errorEntry === "string" && errorEntry.trim()) {
      return stripAnsi(errorEntry.trim());
    }
    if (errorEntry && typeof errorEntry === "object") {
      if (typeof errorEntry.message === "string" && errorEntry.message.trim()) {
        return stripAnsi(errorEntry.message.trim());
      }
      const serialized = stripAnsi(JSON.stringify(errorEntry));
      if (serialized.trim()) {
        return serialized;
      }
    }
  }
  return "";
}

/**
 * Announce a phase, then run its one command.
 *
 * `[report] phase=<name> …` IS A WIRE FORMAT. The python adapter
 * (`wevibe_bench/adapters/backgammon.py`) parses these lines out of stderr and
 * turns them into the board's live `gate-phase-start` / `gate-phase-end`
 * events: a line WITHOUT `status=` opens the phase, one WITH it closes it.
 * A second opener for the same phase would tell the board a new phase had
 * begun, so anything spawning more than one process per phase must use
 * `spawnRunner` and keep exactly one open/close pair around the whole set.
 */
function spawnPhase(phase, cmd, args) {
  process.stderr.write(`\n[report] phase=${phase} target=${TARGET}\n`);
  return spawnRunner(phase, cmd, args);
}

/**
 * Run one command inside an ALREADY-ANNOUNCED phase.
 *
 * Logs under `[report] runner=…`, which the adapter ignores — deliberately.
 * The phase boundary belongs to the caller.
 */
function spawnRunner(label, cmd, args) {
  process.stderr.write(`[report] runner=${label} cmd=${cmd} ${args.join(" ")}\n`);

  const run = spawnSync(cmd, args, {
    cwd: GATES_DIR,
    env,
    stdio: "pipe",
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });

  const stdout = String(run.stdout ?? "");
  const stderr = String(run.stderr ?? "");

  if (stdout) {
    process.stderr.write(stdout.endsWith("\n") ? stdout : `${stdout}\n`);
  }
  if (stderr) {
    process.stderr.write(stderr.endsWith("\n") ? stderr : `${stderr}\n`);
  }
  if (run.error) {
    process.stderr.write(`[report] phase=${phase} spawn_error=${run.error.message}\n`);
  }

  return {
    ok: run.status === 0 && !run.error,
    status: run.status,
    // A RUNNER THAT WAS KILLED IS NOT A RUNNER THAT FAILED. `spawnSync` reports
    // the terminating signal and nothing recorded it, so "vitest was killed
    // mid-suite" and "vitest exited 1 on a broken test" reached the report as
    // the same nonzero status.
    signal: run.signal ?? null,
    error: run.error,
    stdout,
    stderr,
  };
}

function runConformancePhase() {
  announceGateSet("conformance");
  const run = spawnPhase("conformance", "npx", [
    "playwright",
    "test",
    "--config",
    "playwright.conformance.config.ts",
    "--reporter=json",
  ]);

  const problems = [];
  const failedGates = [];

  const parsed = parseJsonObject(run.stdout);
  if (parsed) {
    const specs = collectPlaywrightSpecs(parsed.suites);
    for (const spec of specs) {
      for (const testEntry of spec.tests || []) {
        for (const result of testEntry.results || []) {
          for (const stderrEntry of result.stderr || []) {
            const text = textFromEntry(stderrEntry);
            problems.push(...parseProblemsFromTextLines(text));
          }

          if (result.error?.message) {
            problems.push(...parseProblemsFromErrorMessage(result.error.message));
          }

          for (const err of result.errors || []) {
            if (err?.message) {
              problems.push(...parseProblemsFromErrorMessage(err.message));
            }
          }
        }
      }
    }
  }

  if (problems.length === 0) {
    problems.push(...parseProblemsFromTextLines(`${run.stdout}\n${run.stderr}`));
  }

  const prefixed = dedupeProblems(problems).map((problem) => {
    const check = `conformance:${problem.check}`;
    failedGates.push(check);
    return safeProblem(check, problem.expected, truncate(stripAnsi(problem.observed), 400));
  });

  if (!run.ok && prefixed.length === 0) {
    const observedSource =
      run.stderr
      || extractPlaywrightRunError(parsed)
      || run.stdout
      || run.error?.message
      || "unknown conformance failure";
    const fallback = safeProblem(
      "conformance:boot",
      "server boots on :8002 and passes pre-gate",
      truncate(stripAnsi(observedSource), 300) || "<empty>",
    );
    prefixed.push(fallback);
    failedGates.push(fallback.check);
  }

  const uniqueFailedGates = dedupeStrings(failedGates);
  process.stderr.write(
    `[report] phase=conformance status=${run.ok ? "pass" : "fail"} problems=${prefixed.length}\n`,
  );
  // NOTE: the `conformance:REQ-*` entries in `failedGates` are SUB-CHECKS
  // inside the single `[CONF]` spec, not gates. They stay in `failed_gates`
  // for backward compatibility and are deliberately NOT mapped onto roster
  // ids — doing so would invent gates the suite does not contain.
  return {
    passed: run.ok,
    problems: prefixed,
    failedGates: uniqueFailedGates,
    gateResults: playwrightGateResults(parsed, MATCHER),
  };
}

function extractExpectedObserved(failureMessage, fallbackExpected) {
  const clean = stripAnsi(String(failureMessage ?? "")).trim();
  const expectedMatch = clean.match(/^Expected:\s*(.+)$/im);
  const observedMatch = clean.match(/^(?:Received|Actual|Observed):\s*(.+)$/im);

  if (expectedMatch && observedMatch) {
    return {
      expected: truncate(expectedMatch[1].trim(), 240),
      observed: truncate(observedMatch[1].trim(), 400),
    };
  }

  return {
    expected: fallbackExpected,
    observed: truncate(clean || "<no failure message>", 400),
  };
}

/**
 * Every backend gate file, in a stable order.
 *
 * Mirrors the `include` glob in `vitest.config.ts` — the runner decides WHICH
 * files exist; this only decides that they are invoked one at a time. Sorted so
 * slot order is reproducible across hosts.
 */
function backendTestFiles() {
  const root = path.join(GATES_DIR, "backend");
  const out = [];
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile() && entry.name.endsWith(".test.ts")) {
        out.push(path.relative(GATES_DIR, full));
      }
    }
  };
  walk(root);
  return out;
}

/**
 * THE BACKEND PHASE — ONE VITEST INVOCATION PER FILE.
 *
 * ── WHY NOT ONE INVOCATION FOR THE WHOLE SUITE ──────────────────────────────
 *
 * `vitest.config.ts` pins `fileParallelism:false` + `singleFork:true` because
 * every backend file binds the fixed port 8002 and they must not overlap. That
 * is correct and is preserved here: `spawnSync` is blocking, so these run
 * strictly one at a time, exactly as before.
 *
 * What is NOT preserved is the blast radius. In a single invocation all seven
 * files share one worker fork, and that fork's RPC to the vitest main process
 * has a fixed timeout which is NOT configurable in vitest 2.1.9. A test that
 * blocks the worker's event loop long enough kills the RPC —
 *
 *   Error: [vitest-worker]: Timeout calling "onTaskUpdate"
 *
 * — and the run collapses, taking every file that had not run yet with it.
 *
 * MEASURED, 2026-08-17 minimax-m3 cell. `backend/gates-13-16.test.ts` holds two
 * SYNCHRONOUS CPU-bound gates (G14: 200 samples × 3 difficulties of
 * `chooseMoves`, then 25 seeded self-play games × 400 half-turns). Against the
 * golden implementation the whole backend suite finishes in 1.63s. Against that
 * cell's code the same two gates took 30.6s and 44.3s, blocked the loop, killed
 * the RPC, and cost the run all six OTHER files: 53 of 56 gates recorded
 * `not_run` on attempts 2 AND 3. Re-grading that worktree scores 69/71.
 *
 * The suite is not at fault and neither is the config — the file passes 7/7 when
 * run alone. The fault is that one slow file could silently un-measure six
 * others. Per-file invocation makes a stall cost ITS OWN file and nothing more,
 * and changes no assertion, so results stay comparable across the change.
 *
 * ONE PHASE, ONE OPEN/CLOSE PAIR. The `[report] phase=backend` lines bracket the
 * whole set (see `spawnPhase`) — the board must see one backend phase, not seven.
 */
function runBackendPhase() {
  announceGateSet("backend");
  process.stderr.write(`\n[report] phase=backend target=${TARGET}\n`);

  const problems = [];
  const failedGates = [];
  const merged = { testResults: [] };
  const abortedFiles = [];
  let allOk = true;

  const expected = ROSTER.available
    ? ROSTER.gates.filter((g) => g.phase === "backend").length
    : null;

  for (const file of backendTestFiles()) {
    const tmpOutput = path.join(
      os.tmpdir(),
      `bg-vitest-${Date.now()}-${Math.random().toString(16).slice(2)}.json`,
    );

    const run = spawnRunner(`backend ${file}`, "npx", [
      "vitest",
      "run",
      file,
      "--reporter=json",
      `--outputFile=${tmpOutput}`,
    ]);
    if (!run.ok) allOk = false;

    let report = null;
    if (fs.existsSync(tmpOutput)) {
      try {
        report = JSON.parse(fs.readFileSync(tmpOutput, "utf8"));
      } catch (error) {
        const observed = truncate(
          stripAnsi(error instanceof Error ? error.message : String(error)),
          400,
        );
        problems.push(
          safeProblem(`backend:report-parse ${file}`, "vitest json report can be parsed", observed),
        );
        failedGates.push(`backend:report-parse ${file}`);
      }
    }

    let failuresHere = 0;
    if (report && Array.isArray(report.testResults)) {
      merged.testResults.push(...report.testResults);
      for (const suite of report.testResults) {
        for (const assertion of suite.assertionResults || []) {
          if (assertion.status !== "failed") continue;
          failuresHere += 1;

          const check =
            String(assertion.title || "").trim()
            || String(assertion.fullName || "").match(/(\[[A-Z]\d{2}\][^\n]*)/)?.[1]
            || String(assertion.fullName || "backend:unknown").trim()
            || "backend:unknown";
          const rawFailure = (assertion.failureMessages || []).join("\n\n");
          const eo = extractExpectedObserved(rawFailure, "backend gate assertion passes");
          problems.push(safeProblem(check, eo.expected, eo.observed));
          failedGates.push(check);
        }
      }
    }

    // THE ABORT CASE, NOW NAMED. A file that exits nonzero with no failing
    // assertion did not finish, and the gate is attributed to THAT FILE rather
    // than to "backend" — which is the difference between "one file stalled"
    // and "the backend phase is broken".
    if (!run.ok && failuresHere === 0) {
      const reported = (report?.testResults ?? []).reduce(
        (n, suite) => n + (suite.assertionResults?.length ?? 0),
        0,
      );
      const gate = `backend:runner ${file}`;
      problems.push(
        safeProblem(
          gate,
          "backend gates execute and pass",
          runnerFailureObserved(`backend ${file}`, run, { reported, expected }),
        ),
      );
      failedGates.push(gate);
      abortedFiles.push(file);
    }

    try {
      if (fs.existsSync(tmpOutput)) fs.unlinkSync(tmpOutput);
    } catch {
      // best effort
    }
  }

  const uniqueFailedGates = dedupeStrings(failedGates);
  process.stderr.write(
    `[report] phase=backend status=${allOk ? "pass" : "fail"} problems=${problems.length}\n`,
  );
  return {
    passed: allOk,
    problems: dedupeProblems(problems),
    failedGates: uniqueFailedGates,
    // EVERY assertion is recorded, not only the failures — recording only
    // failures is exactly what made a pass indistinguishable from an absence.
    gateResults: vitestGateResults(merged, MATCHER),
    abortedFiles,
  };
}

function firstFrontendFailureMessage(spec) {
  for (const testEntry of spec.tests || []) {
    for (const result of testEntry.results || []) {
      if (result.status === "passed" || result.status === "skipped") {
        continue;
      }

      if (result.error?.message) {
        return result.error.message;
      }
      if (Array.isArray(result.errors) && result.errors.length > 0) {
        if (typeof result.errors[0] === "string") {
          return result.errors[0];
        }
        if (result.errors[0]?.message) {
          return result.errors[0].message;
        }
      }
      return `status=${result.status || "unknown"}`;
    }
  }
  return "";
}

function specFailed(spec) {
  if (spec?.ok === false) {
    return true;
  }
  for (const testEntry of spec?.tests || []) {
    for (const result of testEntry?.results || []) {
      if (result.status !== "passed" && result.status !== "skipped") {
        return true;
      }
    }
  }
  return false;
}

function runFrontendPhase() {
  announceGateSet("frontend");
  const run = spawnPhase("frontend", "npx", [
    "playwright",
    "test",
    "--project=chromium",
    "--reporter=json",
  ]);

  const problems = [];
  const failedGates = [];
  const report = parseJsonObject(run.stdout);

  if (report) {
    const specs = collectPlaywrightSpecs(report.suites);
    for (const spec of specs) {
      if (!specFailed(spec)) {
        continue;
      }

      const check = String(spec.title || "frontend:unknown").trim() || "frontend:unknown";
      const message = firstFrontendFailureMessage(spec)
        || extractPlaywrightRunError(report)
        || "frontend gate failed";
      problems.push(
        safeProblem(
          check,
          "gate passes",
          truncate(stripAnsi(message), 400),
        ),
      );
      failedGates.push(check);
    }
  }

  // Same shape as the backend abort: nonzero exit, no failing test, so the
  // phase did not finish and its unreached gates are unmeasured, not failed.
  const aborted = !run.ok && problems.length === 0;
  if (aborted) {
    const runError = stripAnsi(String(extractPlaywrightRunError(report) ?? "")).trim();
    const observed = runError
      ? truncate(firstNonEmptyLine(runError), 400)
      : runnerFailureObserved("frontend", run);
    problems.push(safeProblem("frontend:boot", "frontend gates execute and pass", observed));
    failedGates.push("frontend:boot");
  }

  const uniqueFailedGates = dedupeStrings(failedGates);
  process.stderr.write(
    `[report] phase=frontend status=${run.ok ? "pass" : "fail"} problems=${problems.length}\n`,
  );
  return {
    passed: run.ok,
    problems: dedupeProblems(problems),
    failedGates: uniqueFailedGates,
    gateResults: playwrightGateResults(report, MATCHER),
    aborted,
  };
}

function writeReport(outPath, payload) {
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, `${JSON.stringify(payload, null, 2)}\n`);
}

/**
 * GRADABLE, OR NOT A SCORE.
 *
 * ── WHY AN ATTEMPT NEEDS THIS ───────────────────────────────────────────────
 *
 * `not_run` is honest about a single gate: it was not measured. What the report
 * could not say is that the ATTEMPT as a whole stopped being a measurement.
 *
 * On the 2026-08-17 minimax-m3 cell a stalled worker cost six of seven backend
 * files. The report published `16/71 pass` and pushed `backend:runner` into
 * `failed_gates` — so a harness abort arrived at the board as a gate the MODEL
 * failed, inside a ratio that read like a score. Re-grading that worktree gives
 * 69/71. Nothing in the artifact marked the number as unusable.
 *
 * A runner that aborts leaves gates unmeasured FOR HARNESS REASONS. The pass
 * count that survives is a lower bound on an unknown, not a result, and it must
 * never be compared against a run that completed. This states that in the
 * artifact so a consumer can refuse it rather than average it in.
 *
 * THE VERDICT IS UNTOUCHED. A cell that genuinely failed a gate still reads
 * FAIL — gradability answers "was this measured", not "did it pass", and
 * collapsing the two would let a broken harness launder a real failure.
 */
function gradability({ backend, frontend, folded }) {
  const aborted = [
    ...(backend.abortedFiles ?? []).map((f) => `backend ${f}`),
    ...(frontend.aborted ? ["frontend"] : []),
  ];

  if (aborted.length === 0) {
    return { gradable: true, ungradable_reason: null, aborted_runners: [] };
  }

  const unmeasured = (folded.gate_results ?? []).filter((g) => g.status === "not_run").length;
  return {
    gradable: false,
    ungradable_reason:
      `${aborted.join(", ")} aborted without reporting a failing test, leaving ${unmeasured} `
      + "gate(s) unmeasured — the pass count below is a lower bound on an unknown, not a score, "
      + "and must not be compared against a completed run",
    aborted_runners: aborted,
  };
}

function main() {
  const conformance = runConformancePhase();
  const backend = runBackendPhase();
  const frontend = runFrontendPhase();

  const results = {
    conformance: conformance.passed,
    backend: backend.passed,
    frontend: frontend.passed,
  };

  const problems = dedupeProblems([
    ...conformance.problems,
    ...backend.problems,
    ...frontend.problems,
  ]);
  const failedGates = dedupeStrings([
    ...conformance.failedGates,
    ...backend.failedGates,
    ...frontend.failedGates,
  ]);

  const verdict = Object.values(results).every(Boolean) ? "PASS" : "FAIL";

  // A phase "ran" when its runner produced at least one per-test result. That
  // distinguishes "the phase executed and this gate still has no result" from
  // "the phase never got far enough to execute anything", which are different
  // reasons for the same not_run and must not be collapsed (invariant I-2).
  const phaseRan = {
    conformance: conformance.gateResults.length > 0,
    backend: backend.gateResults.length > 0,
    frontend: frontend.gateResults.length > 0,
  };
  const folded = foldGateResults({
    roster: ROSTER,
    matcher: MATCHER,
    observed: [...conformance.gateResults, ...backend.gateResults, ...frontend.gateResults],
    phaseRan,
  });

  const report = {
    target: TARGET,
    timestamp: nowIso,
    results,
    verdict,
    conformed: results.conformance,
    problems,
    // UNCHANGED, deliberately: every existing consumer of the gate report reads
    // this key and it keeps its exact prior meaning and shape.
    failed_gates: failedGates,
    // WO-GATE-ROSTER additions. Purely additive — nothing above is altered.
    ...folded,
    // ── IS THIS ATTEMPT A MEASUREMENT AT ALL? ────────────────────────────
    ...gradability({ backend, frontend, folded }),
  };

  writeReport(OUT_FILE, report);
  process.stderr.write(`[report] out=${OUT_FILE}\n`);
  process.stdout.write(`BG_GATE_REPORT_JSON ${JSON.stringify(report)}\n`);
  process.exit(verdict === "PASS" ? 0 : 1);
}

try {
  main();
} catch (error) {
  const fatalObserved = truncate(
    stripAnsi(error instanceof Error ? `${error.message}\n${error.stack || ""}` : String(error)),
    400,
  );
  const fallback = {
    target: TARGET,
    timestamp: nowIso,
    results: {
      conformance: false,
      backend: false,
      frontend: false,
    },
    verdict: "FAIL",
    conformed: false,
    problems: [
      {
        check: "runner:exception",
        expected: "report runner executes without uncaught exceptions",
        observed: fatalObserved,
      },
    ],
    failed_gates: ["runner:exception"],
    // The runner threw. Nothing was measured, so there is no score to publish.
    gradable: false,
    ungradable_reason:
      "the gate runner threw before it could grade — no phase produced results, so this attempt "
      + "is not a measurement of the code under test",
    aborted_runners: [],
    // The runner died. NOTHING was measured, so every roster gate is not_run —
    // never fail (the gates did not fail, the harness did) and never absent
    // (absence reads as pass). Invariants I-3 and I-4.
    ...foldGateResults({
      roster: ROSTER,
      matcher: MATCHER,
      observed: [],
      phaseRan: { conformance: false, backend: false, frontend: false },
    }),
  };

  try {
    writeReport(OUT_FILE, fallback);
    process.stderr.write(`[report] out=${OUT_FILE}\n`);
  } catch {
    // ignore secondary write failure; still emit JSON line
  }

  process.stdout.write(`BG_GATE_REPORT_JSON ${JSON.stringify(fallback)}\n`);
  process.exit(1);
}
