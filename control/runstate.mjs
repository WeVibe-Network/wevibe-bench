// ─────────────────────────────────────────────────────────────────────────────
// RUN STATE — is a cell in flight, and may another be started?
//
// The control plane owns exactly one mutable fact: the launcher process it
// spawned. Everything else about a run is READ from the same artifacts the
// dashboard reads, so the two surfaces can never disagree about whether
// something is running.
//
// LIVENESS IS DERIVED FROM THE FILESYSTEM, NOT FROM A PARSED TIMESTAMP.
// See contract.mjs STALL_THRESHOLD_S for the measured defect this avoids
// (a constant phantom 7.1h silence caused by naive local timestamps read in a
// UTC container).
//
// PROCESS LIVENESS IS CHECKED WITH SIGNAL 0, NOT BY TRUSTING A PID FILE.
// A pid recorded at launch says nothing about whether the process still exists;
// `kill(pid, 0)` asks the kernel. A stale pid that happens to be reused by an
// unrelated process is the reason `state` ALSO requires a fresh log write —
// two independent signals, both of which must agree before a run is reported
// as running.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { join } from "node:path";
import { STALL_THRESHOLD_S } from "./contract.mjs";

async function statOrNull(path) {
  try {
    return await fs.stat(path);
  } catch {
    return null;
  }
}

async function listDir(path) {
  try {
    return await fs.readdir(path, { withFileTypes: true });
  } catch {
    return [];
  }
}

/** True iff a process with this pid currently exists. */
export function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM means it exists but is owned by another user — still alive.
    return err?.code === "EPERM";
  }
}

/**
 * The run directory a cell log writes into, read from the log's own text.
 *
 * The harness prints absolute artifact paths on its PROGRESS lines
 * (`step=worktree-git-init path=<runsRoot>/<run_dir>/sessions/...`), so the log
 * states which run it belongs to. Returns null when the log has not yet named
 * one — a just-created log is not an orphan, it is early.
 */
export function runDirOf(text) {
  const m = /\/runs\/([A-Za-z0-9._-]+)\//.exec(String(text ?? ""));
  return m ? m[1] : null;
}

/**
 * The newest LIVE cell launch log under the runs root.
 *
 * ORPHAN LOGS ARE SKIPPED. Cell logs are written to the runs ROOT while the run
 * state they describe lives in `runs/<run_dir>/`. Archiving or wiping a run
 * (`mv runs/cumulative runs/cumulative.<why>-<date>`, RUNBOOK §2) moves the run
 * directory and leaves the log behind, so the log outlives its own data.
 *
 * Measured 2026-08-13: after a wipe, `runs/off-cell-20260813T051334.log`
 * remained at the root. Every reader here resolved it as the live run, so
 * `/api/wall` served `suite.total:null` (the run dir was gone) alongside
 * `grading.active:true phase:frontend stalled:true` parsed out of that dead log
 * — a wiped bench reporting a run in progress. The gate wall could not return
 * to its ARMED state because the phantom never cleared.
 *
 * A log whose run directory no longer exists therefore describes a run that no
 * longer exists, and is not a candidate. This is the wipe boundary enforced at
 * the reader: no cleanup step has to be remembered for the board to read clean.
 */
export async function newestLog(runsRoot) {
  const candidates = [];
  for (const ent of await listDir(runsRoot)) {
    if (!ent.isFile() || !ent.name.endsWith(".log")) continue;
    if (!/^(off|on)-cell-|^cell-/.test(ent.name)) continue;
    const p = join(runsRoot, ent.name);
    const st = await statOrNull(p);
    if (st?.isFile()) {
      candidates.push({ path: p, mtime: st.mtimeMs, size: st.size, name: ent.name });
    }
  }

  // Newest first, then take the first whose run directory still exists.
  candidates.sort((a, b) => b.mtime - a.mtime);
  for (const cand of candidates) {
    const runDir = runDirOf(await readTail(cand.path));
    // Not yet named: a fresh log that has not printed an artifact path. Live by
    // default — refusing it would blind the board to a run that just started.
    if (runDir === null) return cand;
    const st = await statOrNull(join(runsRoot, runDir));
    if (st?.isDirectory()) return { ...cand, run_dir: runDir };
  }
  return null;
}

/**
 * Read the tail of a file, bounded. A multi-hour log must cost the same as a
 * fresh one — the same rule the dashboard sources follow.
 */
export async function readTail(path, bytes = 64 * 1024) {
  const st = await statOrNull(path);
  if (!st?.isFile()) return "";
  const fh = await fs.open(path, "r").catch(() => null);
  if (!fh) return "";
  try {
    const start = Math.max(0, st.size - bytes);
    const len = st.size - start;
    const buf = Buffer.alloc(len);
    await fh.read(buf, 0, len, start);
    return buf.toString("utf8");
  } catch {
    return "";
  } finally {
    await fh.close().catch(() => {});
  }
}

/**
 * Extract the live session id from a launch log. The harness writes it itself
 * (backgammon.py emits `attach_cmd=` and `session_id=` lines), so this reads
 * what the runner already published rather than inventing a second channel.
 */
export function sessionIdFrom(text) {
  const m = /\bsession[_-]?id[=:]\s*(ses_[A-Za-z0-9]+)/i.exec(text ?? "");
  if (m) return m[1];
  const a = /--session\s+(ses_[A-Za-z0-9]+)/.exec(text ?? "");
  return a ? a[1] : null;
}

/** The terminal status object the runner writes as its last log line. */
export function terminalFrom(text) {
  for (const raw of String(text ?? "").split("\n").slice(-8)) {
    const t = raw.trim();
    if (!t.startsWith("{") || !t.endsWith("}")) continue;
    try {
      const o = JSON.parse(t);
      if (typeof o?.status === "string") return o;
    } catch {
      /* not a status object */
    }
  }
  return null;
}

/**
 * Assemble the run state.
 *
 * `launcher` is the control plane's own record of the process it spawned
 * (null if it did not spawn one — e.g. the operator launched from the CLI,
 * which is still the documented path and must not be misreported as idle).
 */
export async function readRunState({ runsRoot, launcher }) {
  const log = await newestLog(runsRoot);

  if (!log) {
    return {
      state: "idle",
      run_dir: null,
      log_path: null,
      log_name: null,
      pid: null,
      model: null,
      arm: null,
      session_id: null,
      started_at: null,
      log_silent_s: null,
      terminal_status: null,
      can_start: true,
      blocked_reason: null,
      launched_by: null,
    };
  }

  const text = await readTail(log.path);
  const terminal = terminalFrom(text);
  const silent = Math.max(0, Math.round((Date.now() - log.mtime) / 1000));

  // TWO INDEPENDENT SIGNALS. A run is only "running" when the process exists
  // AND the log is being written. Either alone is not enough: a pid can be
  // stale/reused, and a log can stop being written by a process that is wedged
  // but alive (the known unbounded-nudge failure mode).
  const alive = launcher ? pidAlive(launcher.pid) : false;
  const arm = /^on-cell-/.test(log.name) ? "on" : /^off-cell-/.test(log.name) ? "off" : null;

  let state;
  if (terminal) {
    state = terminal.status === "ok" || terminal.status === "awaiting_extract" ? "complete" : "failed";
  } else if (alive || silent < STALL_THRESHOLD_S) {
    state = silent >= STALL_THRESHOLD_S ? "stalled" : "running";
  } else {
    // No terminal record, no live process, and the log has gone cold. This is
    // an ABANDONED run — reported distinctly from `complete`, because a cell
    // that died without writing a terminal status was never measured.
    state = "failed";
  }

  const running = state === "running" || state === "starting" || state === "stalled";

  return {
    state,
    run_dir: null,
    log_path: log.path,
    log_name: log.name,
    pid: launcher?.pid ?? null,
    model: launcher?.model ?? null,
    arm: launcher?.arm ?? arm,
    session_id: sessionIdFrom(text),
    started_at: launcher?.started_at ?? null,
    log_silent_s: silent,
    terminal_status: terminal?.status ?? null,
    can_start: !running,
    blocked_reason: running
      ? `a cell is ${state} (${log.name}) — the campaign is strictly serial, ` +
        "one cell at a time (RUNBOOK: OFF-concurrency = 1)"
      : null,
    // Distinguishes a run this service started from one launched at the CLI.
    // Both are real; conflating them would let the UI claim ownership of a run
    // it cannot actually stop.
    launched_by: launcher ? "control-plane" : "external",
  };
}
