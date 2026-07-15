import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  loadEngine,
  startServer,
  stopServer,
  debugSetState,
  debugRoll,
  getState,
  api,
  emptyPoints,
  makeState,
} from "../lib/harness.ts";
import { withinParityBand } from "../lib/acceptance.ts";

const { game, ai } = await loadEngine();

const bd = (
  pts: number[],
  bar = { white: 0, black: 0 },
  off = { white: 0, black: 0 },
) => ({ points: pts, bar, off });

const TAKE_POINT = {
  easy: 0.32,
  medium: 0.27,
  hard: 0.24,
} as const;

const DOUBLE_WINDOW = {
  mediumLower: 0.72,
  hardLower: 0.68,
  upper: 0.9,
} as const;

const refWp = (myPip: number, oppPip: number): number => {
  if (myPip === 0 && oppPip === 0) return 0.5;
  const raw = 1 / (1 + Math.exp(-0.045 * (oppPip - myPip)));
  return Math.max(0.02, Math.min(0.98, raw));
};

describe("[G09] REQ-BEAROFF-GATE — bear-off blocked while a checker is outside home / on bar", () => {
  it("[G09] REQ-BEAROFF-GATE — blocks bear-off when any white checker is outside home", () => {
    const points = emptyPoints();
    points[3] = 1;
    points[8] = 1; // outside white home
    const board = bd(points);

    expect(game.allInHome(board, "white")).toBe(false);

    const moves = game.singleMoves(board, "white", 3);
    expect(moves.every((m: { to: number }) => m.to !== 25)).toBe(true);
  });

  it("[G09] REQ-BEAROFF-GATE — blocks bear-off when white has any checker on the bar", () => {
    const points = emptyPoints();
    points[2] = 1;
    const board = bd(points, { white: 1, black: 0 });

    expect(game.allInHome(board, "white")).toBe(false);

    const moves = game.singleMoves(board, "white", 2);
    expect(moves.every((m: { to: number }) => m.to !== 25)).toBe(true);
  });
});

describe("[G10] REQ-WINCLASS — win / gammon / backgammon classification", () => {
  it("[G10] REQ-WINCLASS — classifies single when loser has already borne off at least one", () => {
    const points = emptyPoints();
    points[13] = -1;
    const board = bd(points, { white: 0, black: 0 }, { white: 15, black: 2 });

    expect(game.checkWin(board, "white")).toEqual({ won: true, type: "single" });
  });

  it("[G10] REQ-WINCLASS — classifies gammon when loser has no off, no bar, and none in winner home", () => {
    const points = emptyPoints();
    points[13] = -3;
    const board = bd(points, { white: 0, black: 0 }, { white: 15, black: 0 });

    expect(game.checkWin(board, "white")).toEqual({ won: true, type: "gammon" });
  });

  it("[G10] REQ-WINCLASS — classifies backgammon when loser checker is in winner home board", () => {
    const points = emptyPoints();
    points[3] = -1;
    const board = bd(points, { white: 0, black: 0 }, { white: 15, black: 0 });

    expect(game.checkWin(board, "white")).toEqual({ won: true, type: "backgammon" });
  });

  it("[G10] REQ-WINCLASS — classifies backgammon when loser still has a checker on the bar", () => {
    const points = emptyPoints();
    points[13] = -1;
    const board = bd(points, { white: 0, black: 1 }, { white: 15, black: 0 });

    expect(game.checkWin(board, "white")).toEqual({ won: true, type: "backgammon" });
  });

  it("[G10] REQ-WINCLASS — reports not-won when off count is below 15", () => {
    const points = emptyPoints();
    points[1] = 1;
    const board = bd(points, { white: 0, black: 0 }, { white: 14, black: 0 });

    expect(game.checkWin(board, "white").won).toBe(false);
  });
});

describe("[G11] REQ-CUBE-STATE — doubling-cube STATE machine (via server)", () => {
  let serverHandle: Awaited<ReturnType<typeof startServer>> | null = null;

  beforeAll(async () => {
    serverHandle = await startServer({ debug: true });
  });

  afterAll(async () => {
    if (serverHandle) {
      await stopServer(serverHandle);
    }
  });

  it("[G11] REQ-CUBE-STATE — new game starts with centered cube and canDouble=true", async () => {
    await api("/api/new", { difficulty: "medium" });
    const state = await getState();

    expect(state.canDouble).toBe(true);
    expect(state.cube).toEqual({ value: 1, owner: null });
  });

  it("[G11] REQ-CUBE-STATE — accepted human double doubles cube and transfers ownership to the taker", async () => {
    // Standard backgammon: when the doubled player TAKES, the taker (here the AI,
    // black) owns the cube — not the doubler. From the opening position the AI's
    // win prob is 0.5 >= its take point, so it accepts.
    await api("/api/new", { difficulty: "medium" });
    const res = await api("/api/double");
    const state = await getState();

    expect(res.cube).toEqual({ value: 2, owner: "black" });
    expect(state.cube).toEqual({ value: 2, owner: "black" });
  });

  it("[G11] REQ-CUBE-STATE — illegal double in move phase must not mutate cube", async () => {
    await api("/api/new", {});
    await debugRoll([3, 1]);
    await api("/api/roll");

    const before = await getState();
    expect(before.phase).toBe("move");
    expect(before.canDouble).toBe(false);

    await api("/api/double");
    const after = await getState();

    expect(after.cube).toEqual(before.cube);
  });
});

describe("[G12] REQ-CUBE-AI — cube AI accept/decline and offer window thresholds", () => {
  it("[G12] REQ-CUBE-AI — accepts near-even cube offers across all difficulties", () => {
    const points = emptyPoints();
    points[6] = 5; // white pip 30
    points[19] = -5; // black pip 30
    const board = bd(points);

    const myPip = game.pipCount(board, "black");
    const oppPip = game.pipCount(board, "white");
    const wp = refWp(myPip, oppPip);

    expect({ myPip, oppPip }).toEqual({ myPip: 30, oppPip: 30 });
    expect(wp).toBe(0.5);
    expect(withinParityBand(ai.winProbability(board, "black"))).toBe(true);
    expect(wp).toBeGreaterThanOrEqual(TAKE_POINT.easy);
    expect(wp).toBeGreaterThanOrEqual(TAKE_POINT.medium);
    expect(wp).toBeGreaterThanOrEqual(TAKE_POINT.hard);
    expect(ai.shouldAiAccept(board, "black", "easy").action).toBe("double");
    expect(ai.shouldAiAccept(board, "black", "medium").action).toBe("double");
    expect(ai.shouldAiAccept(board, "black", "hard").action).toBe("double");
  });

  it("[G12] REQ-CUBE-AI — declines when black win chance is below hard take point", () => {
    const points = emptyPoints();
    points[2] = 1; // white pip 2
    const board = bd(points, { white: 0, black: 14 }); // black pip 350 from bar

    const myPip = game.pipCount(board, "black");
    const oppPip = game.pipCount(board, "white");
    const wp = refWp(myPip, oppPip);

    expect({ myPip, oppPip }).toEqual({ myPip: 350, oppPip: 2 });
    expect(wp).toBe(0.02);
    expect(ai.winProbability(board, "black")).toBeLessThan(TAKE_POINT.hard);
    expect(wp).toBeLessThan(TAKE_POINT.hard);
    expect(ai.shouldAiAccept(board, "black", "hard").action).toBe("no-double");
  });

  it("[G12] REQ-CUBE-AI — offers only inside hard window, never when blocked/easy/too-good", () => {
    const anyPoints = emptyPoints();
    anyPoints[6] = 1;
    anyPoints[19] = -1;
    const anyBoard = bd(anyPoints);
    expect(
      ai.shouldAiDouble(anyBoard, "black", { value: 2, owner: "white" }, "hard")
        .action,
    ).toBe("no-double");

    const windowPoints = emptyPoints();
    windowPoints[19] = -5; // black pip 30
    windowPoints[6] = 10; // white pip 60
    windowPoints[1] = 1; // white pip +1 => total 61
    const windowBoard = bd(windowPoints);

    const windowMyPip = game.pipCount(windowBoard, "black");
    const windowOppPip = game.pipCount(windowBoard, "white");
    const windowWp = refWp(windowMyPip, windowOppPip);

    expect({ windowMyPip, windowOppPip }).toEqual({ windowMyPip: 30, windowOppPip: 61 });
    expect(windowWp).toBeGreaterThanOrEqual(DOUBLE_WINDOW.hardLower);
    expect(windowWp).toBeGreaterThanOrEqual(DOUBLE_WINDOW.mediumLower);
    expect(windowWp).toBeLessThanOrEqual(DOUBLE_WINDOW.upper);

    expect(
      ai.shouldAiDouble(windowBoard, "black", { value: 1, owner: null }, "easy")
        .action,
    ).toBe("no-double");
    expect(
      ai.shouldAiDouble(windowBoard, "black", { value: 1, owner: null }, "hard")
        .action,
    ).toBe("double");

    const tooGoodPoints = emptyPoints();
    tooGoodPoints[24] = -1; // black pip 1
    const tooGoodBoard = bd(tooGoodPoints, { white: 14, black: 0 }); // white pip 350 from bar

    const tooGoodMyPip = game.pipCount(tooGoodBoard, "black");
    const tooGoodOppPip = game.pipCount(tooGoodBoard, "white");
    const tooGoodWp = refWp(tooGoodMyPip, tooGoodOppPip);

    expect({ tooGoodMyPip, tooGoodOppPip }).toEqual({ tooGoodMyPip: 1, tooGoodOppPip: 350 });
    expect(tooGoodWp).toBeGreaterThan(DOUBLE_WINDOW.upper);
    expect(ai.winProbability(tooGoodBoard, "black")).toBeGreaterThan(DOUBLE_WINDOW.upper);
    expect(
      ai.shouldAiDouble(tooGoodBoard, "black", { value: 1, owner: null }, "hard")
        .action,
    ).toBe("no-double");
  });
});
