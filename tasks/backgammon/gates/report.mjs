#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

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

function spawnPhase(phase, cmd, args) {
  process.stderr.write(`\n[report] phase=${phase} target=${TARGET}\n`);
  process.stderr.write(`[report] cmd=${cmd} ${args.join(" ")}\n`);

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
    error: run.error,
    stdout,
    stderr,
  };
}

function runConformancePhase() {
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
  return { passed: run.ok, problems: prefixed, failedGates: uniqueFailedGates };
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

function runBackendPhase() {
  const tmpOutput = path.join(
    os.tmpdir(),
    `bg-vitest-${Date.now()}-${Math.random().toString(16).slice(2)}.json`,
  );

  const run = spawnPhase("backend", "npx", [
    "vitest",
    "run",
    "backend",
    "--reporter=json",
    `--outputFile=${tmpOutput}`,
  ]);

  const problems = [];
  const failedGates = [];

  let report = null;
  if (fs.existsSync(tmpOutput)) {
    try {
      report = JSON.parse(fs.readFileSync(tmpOutput, "utf8"));
    } catch (error) {
      const observed = truncate(stripAnsi(error instanceof Error ? error.message : String(error)), 400);
      problems.push(
        safeProblem(
          "backend:report-parse",
          "vitest json report can be parsed",
          observed,
        ),
      );
      failedGates.push("backend:report-parse");
    }
  }

  if (report && Array.isArray(report.testResults)) {
    for (const suite of report.testResults) {
      for (const assertion of suite.assertionResults || []) {
        if (assertion.status !== "failed") {
          continue;
        }

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

  if (!run.ok && problems.length === 0) {
    const observed = truncate(
      stripAnsi(
        firstNonEmptyLine(run.stderr || run.stdout || run.error?.message || "backend phase failed"),
      ),
      400,
    );
    problems.push(safeProblem("backend:runner", "backend gates execute and pass", observed));
    failedGates.push("backend:runner");
  }

  try {
    if (fs.existsSync(tmpOutput)) {
      fs.unlinkSync(tmpOutput);
    }
  } catch {
    // best effort
  }

  const uniqueFailedGates = dedupeStrings(failedGates);
  process.stderr.write(
    `[report] phase=backend status=${run.ok ? "pass" : "fail"} problems=${problems.length}\n`,
  );
  return { passed: run.ok, problems: dedupeProblems(problems), failedGates: uniqueFailedGates };
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

  if (!run.ok && problems.length === 0) {
    const observed = truncate(
      stripAnsi(
        firstNonEmptyLine(
          extractPlaywrightRunError(report)
          || run.stderr
          || run.stdout
          || run.error?.message
          || "frontend phase failed",
        ),
      ),
      400,
    );
    problems.push(safeProblem("frontend:boot", "frontend gates execute and pass", observed));
    failedGates.push("frontend:boot");
  }

  const uniqueFailedGates = dedupeStrings(failedGates);
  process.stderr.write(
    `[report] phase=frontend status=${run.ok ? "pass" : "fail"} problems=${problems.length}\n`,
  );
  return { passed: run.ok, problems: dedupeProblems(problems), failedGates: uniqueFailedGates };
}

function writeReport(outPath, payload) {
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, `${JSON.stringify(payload, null, 2)}\n`);
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
  const report = {
    target: TARGET,
    timestamp: nowIso,
    results,
    verdict,
    conformed: results.conformance,
    problems,
    failed_gates: failedGates,
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
