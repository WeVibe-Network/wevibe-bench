import {
  execFile,
  spawn,
  type ChildProcess,
  type ChildProcessWithoutNullStreams,
} from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";
import { pathToFileURL } from "node:url";

const execFileAsync = promisify(execFile);

export const PORT = 8002;
export const BASE_URL = "http://localhost:8002";
const DEFAULT_TARGET_DIR =
  "/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench/tasks/backgammon/golden";

export const TARGET_DIR = process.env.BENCH_TARGET
  ? path.resolve(process.env.BENCH_TARGET)
  : DEFAULT_TARGET_DIR;

export async function loadEngine(): Promise<{ game: any; ai: any }> {
  const gameUrl = pathToFileURL(path.join(TARGET_DIR, "src/game.ts")).href;
  const aiUrl = pathToFileURL(path.join(TARGET_DIR, "src/ai.ts")).href;
  const [game, ai] = await Promise.all([import(gameUrl), import(aiUrl)]);
  return { game, ai };
}

export async function freePort(port = PORT): Promise<void> {
  try {
    const { stdout } = await execFileAsync("lsof", [
      "-nP",
      `-iTCP:${port}`,
      "-sTCP:LISTEN",
      "-t",
    ]);
    const pids = stdout
      .split(/\s+/)
      .map((raw) => Number(raw.trim()))
      .filter((pid) => Number.isInteger(pid) && pid > 0);

    for (const pid of pids) {
      try {
        process.kill(pid, "SIGKILL");
      } catch {
        // Best-effort cleanup only.
      }
    }
  } catch {
    // Best-effort cleanup only.
  }
}

export interface ServerHandle {
  proc: ChildProcess;
  baseUrl: string;
  stdout?: string;
  stderr?: string;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function attachOutput(
  proc: ChildProcessWithoutNullStreams,
  handle: ServerHandle,
): void {
  proc.stdout.setEncoding("utf8");
  proc.stderr.setEncoding("utf8");

  proc.stdout.on("data", (chunk: string) => {
    handle.stdout = `${handle.stdout ?? ""}${chunk}`;
  });
  proc.stderr.on("data", (chunk: string) => {
    handle.stderr = `${handle.stderr ?? ""}${chunk}`;
  });
}

function normalizePath(pathname: string): string {
  return pathname.startsWith("/") ? pathname : `/${pathname}`;
}

function waitForExit(proc: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (proc.exitCode !== null) {
    return Promise.resolve(true);
  }

  return new Promise((resolve) => {
    const onDone = () => {
      cleanup();
      resolve(true);
    };
    const timeout = setTimeout(() => {
      cleanup();
      resolve(proc.exitCode !== null);
    }, timeoutMs);
    const cleanup = () => {
      clearTimeout(timeout);
      proc.off("exit", onDone);
      proc.off("close", onDone);
    };

    proc.once("exit", onDone);
    proc.once("close", onDone);
  });
}

export async function startServer(opts?: {
  debug?: boolean;
  env?: Record<string, string>;
}): Promise<ServerHandle> {
  await freePort();

  const proc = spawn("node", ["src/server.ts"], {
    cwd: TARGET_DIR,
    env: {
      ...process.env,
      BENCH_DEBUG: opts?.debug === false ? "" : "1",
      ...(opts?.env ?? {}),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  const handle: ServerHandle = {
    proc,
    baseUrl: BASE_URL,
    stdout: "",
    stderr: "",
  };
  attachOutput(proc as ChildProcessWithoutNullStreams, handle);

  let spawnError: Error | null = null;
  proc.once("error", (err) => {
    spawnError = err;
    handle.stderr = `${handle.stderr ?? ""}${String(err)}\n`;
  });

  const deadline = Date.now() + 6_000;
  while (Date.now() < deadline) {
    if (spawnError) {
      throw new Error(
        `Failed to spawn server process: ${spawnError.message}\nstderr:\n${handle.stderr ?? ""}`,
      );
    }
    if (proc.exitCode !== null) {
      throw new Error(
        `Server exited before becoming healthy (exit=${proc.exitCode}, signal=${proc.signalCode ?? "none"}).\nstderr:\n${handle.stderr ?? ""}`,
      );
    }

    try {
      const res = await fetch(`${BASE_URL}/health`, {
        signal: AbortSignal.timeout(500),
      });
      if (res.status === 200) {
        return handle;
      }
    } catch {
      // Retry until timeout.
    }

    await sleep(100);
  }

  await stopServer(handle);
  const stderr = (handle.stderr ?? "").trim() || "<empty>";
  throw new Error(
    `Timed out waiting for /health at ${BASE_URL} after 6000ms.\nstderr:\n${stderr}`,
  );
}

export async function stopServer(h: ServerHandle): Promise<void> {
  const proc = h.proc;
  if (!proc) {
    return;
  }

  if (proc.exitCode !== null) {
    return;
  }

  try {
    proc.kill("SIGTERM");
  } catch {
    // Best-effort shutdown.
  }

  const exitedAfterTerm = await waitForExit(proc, 1_500);
  if (exitedAfterTerm || proc.exitCode !== null) {
    return;
  }

  try {
    proc.kill("SIGKILL");
  } catch {
    // Best-effort shutdown.
  }

  await waitForExit(proc, 1_500);
}

export async function api(pathname: string, body?: any): Promise<any> {
  const response = await fetch(`${BASE_URL}${normalizePath(pathname)}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify(body ?? {}),
  });

  return response.json();
}

export async function getState(): Promise<any> {
  return api("/api/state");
}

export async function debugSetState(partial: Record<string, any>): Promise<any> {
  return api("/api/debug/state", partial);
}

export async function debugRoll(dice: number[]): Promise<any> {
  return api("/api/debug/roll", { dice });
}

export async function health(): Promise<Response> {
  return fetch(`${BASE_URL}/health`);
}

export function emptyPoints(): number[] {
  return new Array(26).fill(0);
}

export function makeState(partial: Record<string, any>): Record<string, any> {
  const base = {
    points: emptyPoints(),
    bar: { white: 0, black: 0 },
    off: { white: 0, black: 0 },
    turn: "white",
    phase: "move",
    dice: [] as number[],
    remainingDice: [] as number[],
    cube: { value: 1, owner: null as "white" | "black" | null },
    difficulty: "medium",
    score: { white: 0, black: 0 },
    winner: null as "white" | "black" | null,
    winType: null as "single" | "gammon" | "backgammon" | null,
    pointsWon: 0,
    doubleOfferedBy: null as "white" | "black" | null,
    message: "",
  };

  return {
    ...base,
    ...partial,
    points: Array.isArray(partial.points) ? [...partial.points] : base.points,
    bar: { ...base.bar, ...(partial.bar ?? {}) },
    off: { ...base.off, ...(partial.off ?? {}) },
    dice: Array.isArray(partial.dice) ? [...partial.dice] : base.dice,
    remainingDice: Array.isArray(partial.remainingDice)
      ? [...partial.remainingDice]
      : base.remainingDice,
    cube: { ...base.cube, ...(partial.cube ?? {}) },
    score: { ...base.score, ...(partial.score ?? {}) },
  };
}
