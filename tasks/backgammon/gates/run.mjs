#!/usr/bin/env node
// Backgammon gate runner — runs the FULL deterministic oracle suite (conformance
// pre-gate → backend → frontend) against ONE target implementation dir and prints
// a pass/fail scorecard. The aesthetic judge is a SEPARATE polish axis and is NOT
// part of this deterministic gate (it is offline-stubbed in phase 2a).
//
// Usage:  node run.mjs [--target <impl-dir>]
//         BENCH_TARGET=<impl-dir> node run.mjs
// Default target: ./golden (the oracle).
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

const GATES_DIR = path.dirname(fileURLToPath(import.meta.url));
const argIdx = process.argv.indexOf("--target");
const TARGET = path.resolve(
  argIdx >= 0 ? process.argv[argIdx + 1]
    : process.env.BENCH_TARGET || path.join(GATES_DIR, "..", "golden"),
);
const env = { ...process.env, BENCH_TARGET: TARGET };

function run(label, cmd, args) {
  process.stdout.write(`\n=== ${label} (target: ${TARGET}) ===\n`);
  const r = spawnSync(cmd, args, { cwd: GATES_DIR, env, stdio: "inherit" });
  return r.status === 0;
}

const results = {
  conformance: run("CONFORMANCE PRE-GATE", "npx",
    ["playwright", "test", "--config", "playwright.conformance.config.ts", "--reporter=line"]),
  backend: run("BACKEND GATES", "npx", ["vitest", "run", "backend", "--reporter=dot"]),
  frontend: run("FRONTEND GATES", "npx",
    ["playwright", "test", "--project=chromium", "--reporter=line"]),
};

const allPass = Object.values(results).every(Boolean);
const scorecard = {
  target: TARGET,
  timestamp: new Date().toISOString(),
  results,
  verdict: allPass ? "PASS" : "FAIL",
};
const runsDir = path.join(GATES_DIR, "..", "runs");
fs.mkdirSync(runsDir, { recursive: true });
const outFile = path.join(runsDir,
  `${scorecard.timestamp.replace(/[:.]/g, "-")}-scorecard.json`);
fs.writeFileSync(outFile, JSON.stringify(scorecard, null, 2));

process.stdout.write(`\n=== SCORECARD ===\n${JSON.stringify(scorecard, null, 2)}\n`);
process.stdout.write(`scorecard: ${outFile}\n`);
process.exit(allPass ? 0 : 1);
