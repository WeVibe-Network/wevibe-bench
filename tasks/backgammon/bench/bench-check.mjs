#!/usr/bin/env node
// bench-check.mjs — WeVibe bench-fixture predicate runner for the backgammon task.
// self-contained Node 18+ script, zero external deps (node:child_process, global fetch).
//
// The WeVibe plugin adapter (bench-fixture reporter) parses this script's STDOUT:
//   - first non-empty line must be the exact header  WEVIBE-BENCH-REPORT v1
//   - then one JSON line per check: {"test":"<stable-id>","status":"pass"|"fail"}
// ALL diagnostics/logging MUST go to STDERR so STDOUT stays adapter-parseable.
// Exit code: 0 if every check passes, 1 otherwise. Always emits the header even
// when the server cannot be booted.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const HEADER = "WEVIBE-BENCH-REPORT v1";
const PORT = 8002;
const BASE = `http://127.0.0.1:${PORT}`;

// ---- report plumbing (STDOUT is data-only; STDOUT carries the adapter report) ----
let failed = false;

function report(id, ok, detail) {
  const line = JSON.stringify({ test: id, status: ok ? "pass" : "fail" });
  process.stdout.write(line + "\n");
  if (!ok) {
    failed = true;
    process.stderr.write(`[bench-check] FAIL ${id}: ${detail}\n`);
  }
}

// ---- tiny HTTP helper ----
async function api(method, url, body) {
  const res = await fetch(BASE + url, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let json = {};
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      json = {};
    }
  }
  return { status: res.status, json, text };
}

// ---- helpers ----
const sorted = (a) => [...a].sort((x, y) => x - y);
const sameArray = (a, b) =>
  Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);

// Standard opening position — see CONTRACT.md REQ-INIT (26-length, index 1..24 used).
const OPENING_POINTS = [0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5, 5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0];

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- server boot ----
function freePort() {
  const lsof = spawnSync("lsof", ["-nP", `-iTCP:${PORT}`, "-sTCP:LISTEN", "-t"], { encoding: "utf8" });
  const pids = (lsof.stdout || "").trim().split(/\s+/).filter(Boolean);
  for (const pid of pids) {
    try {
      process.kill(Number(pid), "SIGKILL");
      process.stderr.write(`[bench-check] freed port ${PORT} pid ${pid}\n`);
    } catch {
      /* already gone */
    }
  }
}

// Resolve the server entrypoint from package.json scripts.start, else a src/server.* glob.
function resolveStartCommand() {
  const pkgPath = path.join(process.cwd(), "package.json");
  if (existsSync(pkgPath)) {
    try {
      const pkg = JSON.parse(readFileSync(pkgPath, "utf8"));
      const start = pkg && pkg.scripts && typeof pkg.scripts.start === "string" ? pkg.scripts.start.trim() : "";
      if (start) {
        const parts = start.split(/\s+/);
        return { cmd: parts[0], args: parts.slice(1) };
      }
    } catch {
      /* fall through to glob */
    }
  }
  for (const ext of ["ts", "js", "mjs", "cjs"]) {
    const cand = path.join(process.cwd(), "src", `server.${ext}`);
    if (existsSync(cand)) return { cmd: "node", args: [cand] };
  }
  return null;
}

async function waitHealthy() {
  const deadline = Date.now() + 6000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BASE}/health`);
      if (res.status === 200) return true;
    } catch {
      /* not up yet */
    }
    await delay(100);
  }
  return false;
}

// ---- battery ----
async function checkBoot() {
  const res = await api("GET", "/health");
  report("REQ-BIND/boot", res.status === 200, `health ${res.status}`);
}

async function checkInit() {
  const s = (await api("POST", "/api/new", { difficulty: "medium" })).json;
  const ok =
    sameArray(s.points, OPENING_POINTS) &&
    s.phase === "roll" &&
    s.turn === "white" &&
    s.cube && s.cube.value === 1 && s.cube.owner === null &&
    s.bar && s.bar.white === 0 && s.bar.black === 0 &&
    s.off && s.off.white === 0 && s.off.black === 0 &&
    s.winner === null;
  report("REQ-INIT", ok, `points=${Array.isArray(s.points) ? s.points.length : typeof s.points} phase=${s.phase} turn=${s.turn}`);
}

async function checkPip() {
  await api("POST", "/api/new", { difficulty: "medium" });
  const s = (await api("POST", "/api/state", {})).json;
  const ok = s.pip && s.pip.white === 167 && s.pip.black === 167;
  report("REQ-PIP", ok, `pip=${JSON.stringify(s.pip)}`);
}

async function checkDebugRoll() {
  await api("POST", "/api/new", { difficulty: "medium" });
  await api("POST", "/api/debug/roll", { dice: [6, 1] });
  const s = (await api("POST", "/api/roll", {})).json;
  const ok = sameArray(sorted(s.dice || []), [1, 6]) && s.phase === "move";
  report("REQ-DEBUG/debug.roll", ok, `dice=${JSON.stringify(s.dice)} phase=${s.phase}`);
}

async function checkTurn() {
  await api("POST", "/api/new", { difficulty: "medium" });
  await api("POST", "/api/debug/roll", { dice: [6, 1] });
  let s = (await api("POST", "/api/roll", {})).json;
  const moves = Array.isArray(s.legalMoves) ? s.legalMoves : [];
  const m = moves.find((x) => x.die === 6);
  if (!m) {
    report("REQ-TURN", false, "no legal 6-die move to play");
    return;
  }
  const s2 = (await api("POST", "/api/move", { from: m.from, to: m.to, die: 6 })).json;
  const ok = sameArray(sorted(s2.remainingDice || []), [1]);
  report("REQ-TURN", ok, `remainingDice=${JSON.stringify(s2.remainingDice)}`);
}

async function checkCubeState() {
  await api("POST", "/api/new", { difficulty: "medium" });
  const afterDouble = (await api("POST", "/api/double", {})).json;
  const doubled =
    afterDouble.cube && afterDouble.cube.value === 2 && afterDouble.cube.owner === "black";

  // move-phase double must not mutate the cube
  await api("POST", "/api/debug/roll", { dice: [3, 1] });
  await api("POST", "/api/roll", {});
  await api("POST", "/api/double", {});
  const final = (await api("POST", "/api/state", {})).json;
  const unmutated = final.cube && final.cube.value === 2 && final.cube.owner === "black";

  const ok = doubled && unmutated;
  report("REQ-CUBE-STATE", ok, `afterDouble=${JSON.stringify(afterDouble.cube)} final=${JSON.stringify(final.cube)}`);
}

// Mirror the gate suite's REQ-COMPLETE scripted full-game drive (gates-13-16 G15).
async function checkComplete() {
  const scriptedRolls = [[6, 5], [6, 6, 6, 6], [5, 4], [3, 1], [2, 1]];
  let rollIndex = 0;
  const nextRoll = () => [...scriptedRolls[rollIndex++ % scriptedRolls.length]];

  await api("POST", "/api/new", { difficulty: "medium" });
  let state = (await api("POST", "/api/state", {})).json;
  let iterations = 0;
  let thrown = null;

  try {
    while (!state.winner && iterations < 500) {
      iterations++;

      if (state.turn === "white") {
        if (state.phase === "roll") {
          await api("POST", "/api/debug/roll", { dice: nextRoll() });
          state = (await api("POST", "/api/roll", {})).json;
          continue;
        }
        if (state.phase === "move") {
          const legalMoves = Array.isArray(state.legalMoves) ? state.legalMoves : [];
          if (legalMoves.length > 0) {
            const m = legalMoves[0];
            state = (await api("POST", "/api/move", { from: m.from, to: m.to, die: m.die })).json;
            continue;
          }
          if (state.turnOver) {
            state = (await api("POST", "/api/endturn", {})).json;
            continue;
          }
        }
        if (state.turnOver) {
          state = (await api("POST", "/api/endturn", {})).json;
          continue;
        }
        state = (await api("POST", "/api/state", {})).json;
        continue;
      }

      if (state.turn === "black") {
        if (state.phase === "doubleOffered") {
          state = (await api("POST", "/api/double/respond", { accept: true })).json;
          continue;
        }
        if (state.phase === "roll") {
          await api("POST", "/api/debug/roll", { dice: nextRoll() });
        }
        state = (await api("POST", "/api/ai", {})).json;
        continue;
      }

      state = (await api("POST", "/api/state", {})).json;
    }
  } catch (err) {
    thrown = err;
  }

  const ok = !thrown && !!state.winner && ["single", "gammon", "backgammon"].includes(state.winType);
  report(
    "REQ-COMPLETE",
    ok,
    `thrown=${thrown ? String(thrown) : "null"} winner=${state.winner} winType=${state.winType} iterations=${iterations}`,
  );
}

// ---- run ----
async function main() {
  freePort();
  await delay(300);

  const start = resolveStartCommand();
  if (!start) {
    report("REQ-BIND/boot", false, "could not resolve server entrypoint (no package.json start / src/server.*)");
    return;
  }

  process.stderr.write(`[bench-check] starting ${start.cmd} ${start.args.join(" ")}\n`);
  const child = spawn(start.cmd, start.args, {
    cwd: process.cwd(),
    env: { ...process.env, BENCH_DEBUG: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (d) => process.stderr.write(d));
  child.stderr.on("data", (d) => process.stderr.write(d));

  const booted = await waitHealthy();
  if (!booted) {
    report("REQ-BIND/boot", false, `server did not become healthy on :${PORT} within 6s`);
    try {
      child.kill("SIGKILL");
    } catch {
      /* already dead */
    }
    return;
  }

  await checkBoot();
  await checkInit();
  await checkPip();
  await checkDebugRoll();
  await checkTurn();
  await checkCubeState();
  await checkComplete();

  try {
    child.kill("SIGKILL");
  } catch {
    /* already dead */
  }
}

// Headers come first, unconditionally, so the adapter always sees a parseable report.
process.stdout.write(HEADER + "\n");

main()
  .catch((err) => {
    process.stderr.write(`[bench-check] CRASH: ${err && err.stack ? err.stack : String(err)}\n`);
    // guarantee at least the header + a fail line even on an unhandled crash
    failed = true;
  })
  .finally(() => {
    process.exit(failed ? 1 : 0);
  });