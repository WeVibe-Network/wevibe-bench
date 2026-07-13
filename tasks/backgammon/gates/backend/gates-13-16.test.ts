import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  PORT,
  TARGET_DIR,
  api,
  debugRoll,
  debugSetState,
  emptyPoints,
  freePort,
  getState,
  loadEngine,
  makeState,
  startServer,
  stopServer,
} from "../lib/harness.ts";

type Player = "white" | "black";
type Difficulty = "easy" | "medium" | "hard";
type Move = { from: number; to: number; die: number };
type Board = {
  points: number[];
  bar: { white: number; black: number };
  off: { white: number; black: number };
};

const DIFFICULTIES: Difficulty[] = ["easy", "medium", "hard"];

const sortedDice = (dice: number[]) => [...dice].sort((a, b) => a - b);

function sameMove(a: Move, b: Move): boolean {
  return a.from === b.from && a.to === b.to && a.die === b.die;
}

function removeDie(remaining: number[], die: number): void {
  const idx = remaining.indexOf(die);
  expect(idx).toBeGreaterThanOrEqual(0);
  if (idx >= 0) {
    remaining.splice(idx, 1);
  }
}

function cloneBoardFromState(state: any): Board {
  return {
    points: [...state.points],
    bar: { ...state.bar },
    off: { ...state.off },
  };
}

function mulberry32(seed: number): () => number {
  let t = seed >>> 0;
  return () => {
    t += 0x6d2b79f5;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

function rollDice(rng: () => number): number[] {
  const d1 = 1 + Math.floor(rng() * 6);
  const d2 = 1 + Math.floor(rng() * 6);
  return d1 === d2 ? [d1, d1, d1, d1] : [d1, d2];
}

function waitForExitWithTimeout(
  proc: ChildProcessWithoutNullStreams,
  timeoutMs: number,
): Promise<{ code: number | null; signal: NodeJS.Signals | null; timedOut: boolean }> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      try {
        proc.kill("SIGKILL");
      } catch {
        // best-effort cleanup only
      }
      resolve({ code: proc.exitCode, signal: proc.signalCode, timedOut: true });
    }, timeoutMs);

    proc.once("exit", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, timedOut: false });
    });
  });
}

describe("Backgammon backend gates 13-16", () => {
  describe("[G13] turn-flow integrity (via server)", () => {
    let server: Awaited<ReturnType<typeof startServer>>;

    beforeAll(async () => {
      server = await startServer({ debug: true });
      expect(server.baseUrl).toContain(`:${PORT}`);
    });

    afterAll(async () => {
      await stopServer(server);
    });

    it("[G13] no die reuse after consumption", async () => {
      await api("/api/new", { difficulty: "easy" });
      await debugRoll([3, 1]);

      let state = await api("/api/roll", {});
      expect(state.phase).toBe("move");
      expect(sortedDice(state.remainingDice)).toEqual([1, 3]);

      const moveUsingThree = (state.legalMoves as Move[]).find((m) => m.die === 3);
      expect(moveUsingThree).toBeTruthy();
      if (!moveUsingThree) return;

      state = await api("/api/move", {
        from: moveUsingThree.from,
        to: moveUsingThree.to,
        die: 3,
      });
      expect(sortedDice(state.remainingDice)).toEqual([1]);

      const secondAttempt = await api("/api/move", {
        from: moveUsingThree.from,
        to: moveUsingThree.to,
        die: 3,
      });
      expect(sortedDice(secondAttempt.remainingDice)).toEqual([1]);
    });

    it("[G13] alternate turns white -> black -> white", async () => {
      await api("/api/new", { difficulty: "easy" });
      await debugRoll([4, 2]);

      let state = await api("/api/roll", {});
      expect(state.turn).toBe("white");
      expect(state.phase).toBe("move");

      let moveSteps = 0;
      while (!state.turnOver && moveSteps < 16) {
        const legalMoves = (state.legalMoves ?? []) as Move[];
        expect(legalMoves.length).toBeGreaterThan(0);
        const m = legalMoves[0];
        state = await api("/api/move", { from: m.from, to: m.to, die: m.die });
        moveSteps++;
      }

      expect(state.turnOver).toBe(true);

      state = await api("/api/endturn", {});
      let sawBlack = state.turn === "black";

      let guard = 0;
      while (state.turn !== "white" && guard < 12) {
        if (state.turn === "black") {
          sawBlack = true;
          state = await api("/api/ai", {});
        } else {
          state = await getState();
        }
        guard++;
      }

      expect(sawBlack).toBe(true);
      expect(state.turn).toBe("white");
    });

    it("[G13] auto-pass when stuck on bar", async () => {
      const points = emptyPoints();
      points[23] = -2;
      points[21] = -2;

      await debugSetState(
        makeState({
          points,
          bar: { white: 1, black: 0 },
          turn: "white",
          phase: "roll",
          dice: [],
          remainingDice: [],
          message: "",
        }),
      );

      await debugRoll([2, 4]);
      const state = await api("/api/roll", {});

      expect(state.turnOver).toBe(true);
      expect(Array.isArray(state.legalMoves)).toBe(true);
      expect(state.legalMoves.length).toBe(0);
      expect(typeof state.message).toBe("string");
      expect(state.message.trim().length).toBeGreaterThan(0);
      expect(state.message).toMatch(/no legal move|pass/i);
    });
  });

  describe("[G14] AI legality + hard beats easy (seeded self-play)", () => {
    let game: any;
    let ai: any;
    const originalRandom = Math.random;

    function randomReachablePosition(rng: () => number): { board: Board; player: Player } {
      let board: Board = {
        points: [...game.startingPoints()],
        bar: { white: 0, black: 0 },
        off: { white: 0, black: 0 },
      };
      let player: Player = "white";

      const prepHalfTurns = 1 + Math.floor(rng() * 7);
      for (let i = 0; i < prepHalfTurns; i++) {
        const dice = rollDice(rng);
        const remaining = [...dice];

        while (remaining.length > 0) {
          const legal = game.legalMovesNow(board, player, remaining) as Move[];
          if (legal.length === 0) break;

          const pick = legal[Math.floor(rng() * legal.length)];
          game.applyMove(board, player, pick);
          removeDie(remaining, pick.die);
        }

        const win = game.checkWin(board, player);
        if (win.won) {
          board = {
            points: [...game.startingPoints()],
            bar: { white: 0, black: 0 },
            off: { white: 0, black: 0 },
          };
          player = "white";
          continue;
        }

        player = game.opponent(player);
      }

      return { board: game.cloneBoard(board), player };
    }

    function replayAndAssertLegal(
      startBoard: Board,
      player: Player,
      dice: number[],
      moves: Move[],
    ): Board {
      const board = game.cloneBoard(startBoard);
      const remaining = [...dice];

      for (const move of moves) {
        const legalNow = game.legalMovesNow(board, player, remaining) as Move[];
        const legal = legalNow.some((candidate) => sameMove(candidate, move));
        expect(legal).toBe(true);

        game.applyMove(board, player, move);
        removeDie(remaining, move.die);
      }

      return board;
    }

    beforeAll(async () => {
      ({ game, ai } = await loadEngine());
    });

    afterAll(() => {
      Math.random = originalRandom;
    });

    it("[G14] chooseMoves always returns legal move sequences", () => {
      const seed = 0x13a4b6c8;
      Math.random = mulberry32(seed);

      const samples = 200;
      for (let i = 0; i < samples; i++) {
        const { board, player } = randomReachablePosition(Math.random);
        const dice = rollDice(Math.random);

        for (const difficulty of DIFFICULTIES) {
          const start = game.cloneBoard(board);
          const result = ai.chooseMoves(game.cloneBoard(board), player, [...dice], difficulty) as {
            moves: Move[];
            board: Board;
          };

          const replayed = replayAndAssertLegal(start, player, [...dice], result.moves);
          expect(result.board).toEqual(replayed);
        }
      }
    });

    it("[G14] hard(black) wins more than easy(white) in seeded self-play", () => {
      const seed = 0x5eed1337;
      Math.random = mulberry32(seed);

      const games = 25;
      let hardWins = 0;
      let easyWins = 0;
      let noResult = 0;

      for (let g = 0; g < games; g++) {
        const start = game.createGame("medium");
        const board = cloneBoardFromState(start);
        let player: Player = "white";
        let winner: Player | null = null;

        for (let halfTurns = 0; halfTurns < 400; halfTurns++) {
          const dice = rollDice(Math.random);
          const difficulty: Difficulty = player === "black" ? "hard" : "easy";
          const choice = ai.chooseMoves(game.cloneBoard(board), player, [...dice], difficulty) as {
            moves: Move[];
            board: Board;
          };
          board.points = [...choice.board.points];
          board.bar = { ...choice.board.bar };
          board.off = { ...choice.board.off };

          const win = game.checkWin(board, player);
          if (win.won) {
            winner = player;
            break;
          }

          player = game.opponent(player);
        }

        if (winner === "black") hardWins++;
        else if (winner === "white") easyWins++;
        else noResult++;
      }

      expect(hardWins + easyWins + noResult).toBe(games);
      expect(hardWins + easyWins).toBeGreaterThan(0);
      expect(hardWins).toBeGreaterThan(easyWins);
    });
  });

  describe("[G15] scripted full game reaches winner with zero exceptions (via server)", () => {
    let server: Awaited<ReturnType<typeof startServer>>;

    beforeAll(async () => {
      server = await startServer({ debug: true });
    });

    afterAll(async () => {
      await stopServer(server);
    });

    it("[G15] complete game to winner with fixed debug dice script", async () => {
      const scriptedRolls = [[6, 5], [6, 6, 6, 6], [5, 4], [3, 1], [2, 1]];
      let rollIndex = 0;
      const nextRoll = () => {
        const roll = scriptedRolls[rollIndex % scriptedRolls.length];
        rollIndex++;
        return [...roll];
      };

      let state = await api("/api/new", { difficulty: "medium" });
      let iterations = 0;
      let thrown: unknown = null;

      try {
        while (!state.winner && iterations < 500) {
          iterations++;

          if (state.turn === "white") {
            if (state.phase === "roll") {
              await debugRoll(nextRoll());
              state = await api("/api/roll", {});
              continue;
            }

            if (state.phase === "move") {
              const legalMoves = (state.legalMoves ?? []) as Move[];
              if (legalMoves.length > 0) {
                const m = legalMoves[0];
                state = await api("/api/move", { from: m.from, to: m.to, die: m.die });
                continue;
              }

              if (state.turnOver) {
                state = await api("/api/endturn", {});
                continue;
              }
            }

            if (state.turnOver) {
              state = await api("/api/endturn", {});
              continue;
            }

            state = await getState();
            continue;
          }

          if (state.turn === "black") {
            if (state.phase === "doubleOffered") {
              state = await api("/api/double/respond", { accept: true });
              continue;
            }

            if (state.phase === "roll") {
              await debugRoll(nextRoll());
            }

            state = await api("/api/ai", {});
            continue;
          }

          state = await getState();
        }
      } catch (err) {
        thrown = err;
      }

      expect(thrown).toBeNull();
      expect(
        state.winner,
        `Expected winner within 500 iterations, but loop stopped at ${iterations}.`,
      ).toBeTruthy();
      expect(["single", "gammon", "backgammon"]).toContain(state.winType);
      expect(iterations).toBeLessThanOrEqual(500);
    });
  });

  describe("[G16] port 8002 bind + clear failure when taken", () => {
    it("[G16] second server exits non-zero with clear 8002 in-use message", async () => {
      await freePort(PORT);
      const h = await startServer({ debug: true });

      let p2: ChildProcessWithoutNullStreams | null = null;
      let stdout = "";
      let stderr = "";

      try {
        p2 = spawn("node", ["src/server.ts"], {
          cwd: TARGET_DIR,
          env: { ...process.env, BENCH_DEBUG: "1" },
          stdio: ["ignore", "pipe", "pipe"],
        });

        p2.stdout.setEncoding("utf8");
        p2.stderr.setEncoding("utf8");
        p2.stdout.on("data", (chunk: string) => {
          stdout += chunk;
        });
        p2.stderr.on("data", (chunk: string) => {
          stderr += chunk;
        });

        const result = await waitForExitWithTimeout(p2, 6_000);
        expect(result.timedOut).toBe(false);
        expect(result.code).not.toBe(0);

        const combined = `${stdout}\n${stderr}`;
        expect(combined).toContain(String(PORT));
        expect(combined).toMatch(/in use|already/i);
      } finally {
        if (p2 && p2.exitCode === null) {
          try {
            p2.kill("SIGKILL");
          } catch {
            // best-effort cleanup only
          }
        }

        await stopServer(h);
        await freePort(PORT);
      }
    });
  });
});
