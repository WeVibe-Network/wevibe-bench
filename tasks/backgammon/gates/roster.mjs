#!/usr/bin/env node
// ─────────────────────────────────────────────────────────────────────────────
// GATE ROSTER — the full enumerable gate suite, without running a single test.
//
// WHY THIS EXISTS (WO-GATE-ROSTER)
//
// `report.mjs` emits ONLY `failed_gates`. That is enough to colour a gate red
// and nothing else. Four of the GATE WALL's six states (dashed/not-yet-tested,
// amber/under-test, blue+green/passed, slate/abandoned) need facts the harness
// never published: the SUITE (what gates exist at all) and per-gate OUTCOMES.
// Without a suite there is no denominator, and a board that invents one is
// lying. This module publishes the true denominator.
//
// EXECUTION-FREE BY CONSTRUCTION (invariant I-5). Both runners can enumerate
// without executing: `vitest list` and `playwright test --list` walk the test
// files and print names. Neither binds :8002, so this is safe to run beside a
// live cell — the frontend playwright config has a `webServer` block, and
// `--list` deliberately does NOT start it (verified by watching the port for
// the duration of a list: never bound).
//
// NEVER RENUMBER, NEVER PAD, NEVER INVENT. Every enumerable test is exactly one
// gate. The count is whatever the runners report. If a phase cannot be
// enumerated the roster says so (`complete:false` + `incomplete_reason`) and
// reports the smaller true number rather than a comfortable guess.
// ─────────────────────────────────────────────────────────────────────────────

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ROSTER_SCHEMA_VERSION = 1;

const GATES_DIR = path.dirname(fileURLToPath(import.meta.url));

/** Phases in the order the gate runner executes them. */
export const PHASES = /** @type {const} */ (["conformance", "backend", "frontend"]);

// ── identity ────────────────────────────────────────────────────────────────

/**
 * The bracket token a gate title may carry: `[G01]`, `[F07]`, `[CONF]`.
 *
 * The directive names `[Gnn]` only; the suite on disk ALSO carries `[Fnn]` on
 * every frontend spec and `[CONF]` on the pre-gate. Those are the same kind of
 * author-assigned stable identity, so they are honoured identically. Reading
 * them is not renumbering — it is refusing to throw away identity the suite
 * already declares.
 */
function tokenOf(title) {
  const m = /^\s*\[([A-Z]+[0-9]*)\]/.exec(String(title ?? ""));
  return m ? m[1] : null;
}

/** The REQ-* requirement token a title references, when it names one. */
function reqOf(titleChain) {
  const m = /\b(REQ-[A-Z0-9-]+?)(?=\s|—|$)/.exec(String(titleChain ?? ""));
  return m ? m[1] : null;
}

/**
 * The human-readable part of a gate title: token and REQ prefix stripped.
 * Falls back to the raw title so a gate is never left nameless.
 */
function titleOf(rawTitle) {
  let t = String(rawTitle ?? "").trim();
  t = t.replace(/^\s*\[[A-Z]+[0-9]*\]\s*/, "");
  t = t.replace(/^REQ-[A-Z0-9-]+\s*[—–-]\s*/, "");
  return t.trim() || String(rawTitle ?? "").trim();
}

/**
 * Deterministic slug for a gate with no usable token.
 *
 * Stable across runs because it is built only from facts that live in the test
 * file itself — phase, path, and the full describe/it chain. No ordinal, no
 * counter, no timestamp: re-enumerating an unchanged suite reproduces every id
 * byte-for-byte, which is what makes `suite_fingerprint` meaningful.
 */
function slugFor(phase, file, titleChain) {
  // Backend files are already stored phase-prefixed (`backend/foo.test.ts`)
  // while playwright's are bare (`core.spec.ts`). Normalising here keeps the id
  // shape identical across phases instead of emitting `backend/backend/…`.
  const rel = file.startsWith(`${phase}/`) ? file.slice(phase.length + 1) : file;
  return `${phase}/${rel}::${titleChain}`;
}

// ── enumeration ─────────────────────────────────────────────────────────────

function listCmd(args, { gatesDir, timeoutMs }) {
  const run = spawnSync("npx", args, {
    cwd: gatesDir,
    encoding: "utf8",
    timeout: timeoutMs,
    maxBuffer: 32 * 1024 * 1024,
    // Enumeration must never inherit a BENCH_TARGET that points at a
    // half-written worktree; the suite's shape does not depend on the target.
    env: { ...process.env },
  });
  return {
    ok: run.status === 0 && !run.error,
    stdout: String(run.stdout ?? ""),
    stderr: String(run.stderr ?? ""),
    status: run.status,
    error: run.error ? String(run.error.message ?? run.error) : null,
  };
}

/**
 * Parse `vitest list` output.
 *
 * One line per test: `<file> > <describe> > … > <test title>`. The file is the
 * first segment; everything after it is the describe/it chain that vitest's
 * JSON reporter reproduces as `[...ancestorTitles, title]`, which is exactly
 * what makes results matchable back to this roster without guesswork.
 */
export function parseVitestList(stdout) {
  const out = [];
  for (const raw of String(stdout ?? "").split("\n")) {
    const line = raw.trim();
    if (!line || !line.includes(" > ")) continue;
    const parts = line.split(" > ").map((s) => s.trim());
    const file = parts[0];
    if (!/\.(test|spec)\.[tj]sx?$/.test(file)) continue;
    const chain = parts.slice(1);
    if (chain.length === 0) continue;
    out.push({ file, chain });
  }
  return out;
}

/**
 * Parse `playwright test --list` output.
 *
 * One line per test: `  [project] › <file>:<line>:<col> › <describe> › … › <title>`.
 * The leading `Listing tests:` banner and trailing `Total: N tests…` summary
 * are not tests and are skipped by the shape check rather than by line index.
 */
export function parsePlaywrightList(stdout) {
  const out = [];
  for (const raw of String(stdout ?? "").split("\n")) {
    const line = raw.trim();
    if (!line.startsWith("[")) continue;
    const segments = line.split(" › ").map((s) => s.trim());
    if (segments.length < 2) continue;
    const loc = segments[1];
    const m = /^(.+?):(\d+):(\d+)$/.exec(loc);
    if (!m) continue;
    const chain = segments.slice(2);
    if (chain.length === 0) continue;
    out.push({ file: m[1], line: Number(m[2]), chain });
  }
  return out;
}

/**
 * Turn parsed list rows into gate records for one phase.
 *
 * `test_name` is the leaf title (what a human reads); `full_name` is the whole
 * chain (what a result is matched on). Keeping both means the board can show a
 * short name without the matcher losing the precision it needs.
 */
/**
 * Load the tier rules.
 *
 * Absent or unreadable, every gate is `core` — never unlabelled, and never
 * silently dropped from the suite. A tier is a partition of the denominator,
 * never a way to shrink it.
 */
function loadTiers(gatesDir) {
  try {
    const parsed = JSON.parse(fs.readFileSync(path.join(gatesDir, "tiers.json"), "utf8"));
    return {
      fallback: String(parsed.default ?? "core"),
      rules: Array.isArray(parsed.rules) ? parsed.rules : [],
    };
  } catch {
    return { fallback: "core", rules: [] };
  }
}

export function tierOf(file, tiers) {
  const segments = String(file ?? "").split("/");
  for (const rule of tiers.rules) {
    if (rule?.path_segment && segments.includes(String(rule.path_segment))) {
      return String(rule.tier ?? tiers.fallback);
    }
  }
  return tiers.fallback;
}

function gatesFromRows(rows, phase, tiers) {
  return rows.map((row) => {
    const leaf = row.chain[row.chain.length - 1];
    const fullName = row.chain.join(" > ");
    return {
      // `id` is assigned after the whole suite is known — a token only becomes
      // an id when it identifies exactly one test (see assignIds).
      id: null,
      gate_token: tokenOf(leaf) ?? tokenOf(row.chain[0]) ?? null,
      phase,
      req: reqOf(fullName),
      title: titleOf(leaf),
      file: row.file,
      line: Number.isFinite(row.line) ? row.line : null,
      test_name: leaf,
      full_name: fullName,
      // What KIND of gate this is (see tiers.json). Partitions the suite so the
      // board can render an edge-case square differently and a scorecard can
      // quote a core-only bar — WITHOUT removing anything from the denominator.
      tier: tierOf(row.file, tiers),
    };
  });
}

/**
 * Assign final ids.
 *
 * A bracket token becomes the id ONLY when it identifies exactly one enumerable
 * test. On this suite `[G10]` covers five separate tests and `[G12]` four, so
 * using the bare token as an id would collide and silently merge distinct
 * gates — the precise "absence is indistinguishable from success" defect class
 * this work exists to remove. Colliding tokens fall back to the deterministic
 * slug and keep the token in `gate_token`, so the board can still GROUP by
 * requirement without the roster pretending five tests are one.
 */
export function assignIds(gates) {
  const tokenCounts = new Map();
  for (const g of gates) {
    if (!g.gate_token) continue;
    tokenCounts.set(g.gate_token, (tokenCounts.get(g.gate_token) ?? 0) + 1);
  }

  const seen = new Set();
  for (const g of gates) {
    const unique = g.gate_token && tokenCounts.get(g.gate_token) === 1;
    let id = unique ? g.gate_token : slugFor(g.phase, g.file, g.full_name);
    // Defensive: two identical full names in one file would otherwise collide.
    // Never renumber silently — the suffix names the collision for what it is.
    if (seen.has(id)) {
      let n = 2;
      while (seen.has(`${id}#dup${n}`)) n += 1;
      id = `${id}#dup${n}`;
    }
    seen.add(id);
    g.id = id;
  }
  return gates;
}

/** sha256 over the sorted gate ids — how a suite change is detected. */
export function suiteFingerprint(gates) {
  const ids = gates.map((g) => g.id).sort();
  return `sha256:${createHash("sha256").update(ids.join("\n")).digest("hex")}`;
}

/**
 * Enumerate the whole gate suite. Runs three list commands; executes no tests.
 *
 * A phase that fails to enumerate is reported, not guessed at: its gates are
 * simply absent and `incomplete_reason` names the phase and the failure.
 */
export function enumerateGates({ gatesDir = GATES_DIR, timeoutMs = 180_000 } = {}) {
  const failures = [];
  const gates = [];
  const tiers = loadTiers(gatesDir);

  const conf = listCmd(
    ["playwright", "test", "--config", "playwright.conformance.config.ts", "--list"],
    { gatesDir, timeoutMs },
  );
  if (conf.ok) {
    gates.push(...gatesFromRows(parsePlaywrightList(conf.stdout), "conformance", tiers));
  } else {
    failures.push(`conformance: ${conf.error ?? `exit ${conf.status}`}`);
  }

  const backend = listCmd(["vitest", "list", "backend"], { gatesDir, timeoutMs });
  if (backend.ok) {
    gates.push(...gatesFromRows(parseVitestList(backend.stdout), "backend", tiers));
  } else {
    failures.push(`backend: ${backend.error ?? `exit ${backend.status}`}`);
  }

  const frontend = listCmd(["playwright", "test", "--list"], { gatesDir, timeoutMs });
  if (frontend.ok) {
    gates.push(...gatesFromRows(parsePlaywrightList(frontend.stdout), "frontend", tiers));
  } else {
    failures.push(`frontend: ${frontend.error ?? `exit ${frontend.status}`}`);
  }

  assignIds(gates);

  return {
    gates,
    complete: failures.length === 0,
    incomplete_reason: failures.length === 0 ? null : failures.join("; "),
  };
}

function countBy(gates, key) {
  const out = {};
  for (const g of gates) out[g[key]] = (out[g[key]] ?? 0) + 1;
  return out;
}

/** Assemble the published roster artifact. */
export function buildRoster({
  gates,
  complete,
  incomplete_reason = null,
  task = "backgammon-cumulative-primary",
  capturedAt = new Date().toISOString(),
}) {
  const byPhase = {};
  for (const phase of PHASES) byPhase[phase] = 0;
  Object.assign(byPhase, countBy(gates, "phase"));

  return {
    schema_version: ROSTER_SCHEMA_VERSION,
    captured_at: capturedAt.replace(/\.\d{3}Z$/, "Z"),
    task,
    suite_fingerprint: suiteFingerprint(gates),
    enumeration: {
      method: "vitest list + playwright --list + playwright --list (conformance config)",
      executed_tests: false,
      complete,
      incomplete_reason,
    },
    // The TRUE enumerated count. Never padded toward a design comp's number.
    total: gates.length,
    by_phase: byPhase,
    by_tier: countBy(gates, "tier"),
    gates,
  };
}

// ── CLI ─────────────────────────────────────────────────────────────────────

function argValue(flag) {
  const idx = process.argv.indexOf(flag);
  if (idx < 0) return null;
  const next = process.argv[idx + 1];
  return next && !next.startsWith("--") ? next : null;
}

function main() {
  const out = argValue("--out");
  const task = argValue("--task") ?? "backgammon-cumulative-primary";
  const force = process.argv.includes("--force");

  // WRITE-ONCE (invariant I-6). The roster describes the suite as it stood when
  // the run began; rewriting it mid-run would silently re-baseline every gate
  // comparison that had already been made against it.
  if (out && fs.existsSync(out) && !force) {
    process.stderr.write(`[roster] exists, not overwriting: ${out}\n`);
    process.exit(0);
  }

  const { gates, complete, incomplete_reason } = enumerateGates();
  const roster = buildRoster({ gates, complete, incomplete_reason, task });

  const text = `${JSON.stringify(roster, null, 2)}\n`;
  if (out) {
    fs.mkdirSync(path.dirname(out), { recursive: true });
    // Atomic: a reader must never observe a half-written roster.
    const tmp = `${out}.tmp-${process.pid}`;
    fs.writeFileSync(tmp, text);
    fs.renameSync(tmp, out);
    process.stderr.write(
      `[roster] out=${out} total=${roster.total} complete=${complete} fp=${roster.suite_fingerprint}\n`,
    );
  } else {
    process.stdout.write(text);
  }
  process.exit(complete ? 0 : 2);
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main();
}
