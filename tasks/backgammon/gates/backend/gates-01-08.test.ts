import { beforeAll, describe, expect, it } from "vitest";
import {
  api,
  debugRoll,
  debugSetState,
  emptyPoints,
  getState,
  loadEngine,
  makeState,
  startServer,
  stopServer,
} from "../lib/harness.ts";

type Move = { from: number; to: number; die: number };
type Board = {
  points: number[];
  bar: { white: number; black: number };
  off: { white: number; black: number };
};

const STARTING_POINTS_EXPECTED = [
  0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5, 5, 0, 0, 0, -3, 0, -5, 0, 0, 0,
  0, 2, 0,
];

const norm = (ms: Move[]) => ms.map((m) => `${m.from}-${m.to}-${m.die}`).sort();

const bd = (
  pts: number[],
  bar = { white: 0, black: 0 },
  off = { white: 0, black: 0 },
): Board => ({ points: pts, bar, off });

// Imported by directive for parity with future gates that exercise debug HTTP routes.
void { startServer, stopServer, debugSetState, debugRoll, getState, api, makeState };

describe("Backgammon backend gates 01-08", () => {
  let game: any;
  let ai: any;

  beforeAll(async () => {
    ({ game, ai } = await loadEngine());
    expect(game).toBeTruthy();
    expect(ai).toBeTruthy();
  });

  it("[G01] initial position", () => {
    const points = game.startingPoints();

    expect(points).toEqual(STARTING_POINTS_EXPECTED);

    const state = game.createGame("medium");
    expect(state.turn).toBe("white");
    expect(state.phase).toBe("roll");
    expect(state.cube).toEqual({ value: 1, owner: null });
    expect(state.bar).toEqual({ white: 0, black: 0 });
    expect(state.off).toEqual({ white: 0, black: 0 });
    expect(state.difficulty).toBe("medium");
    expect(state.winner).toBeNull();
    expect(state.points).toEqual(game.startingPoints());
    expect(state.points).toEqual(STARTING_POINTS_EXPECTED);

    expect(game.opponent("white")).toBe("black");
    expect(game.opponent("black")).toBe("white");
  });

  it("[G02] pip count", () => {
    const start = bd([...game.startingPoints()], { white: 0, black: 0 }, { white: 0, black: 0 });
    expect(game.pipCount(start, "white")).toBe(167);
    expect(game.pipCount(start, "black")).toBe(167);

    const whiteBarAndPoint = emptyPoints();
    whiteBarAndPoint[4] = 1;
    const whiteBoard = bd(
      whiteBarAndPoint,
      { white: 1, black: 0 },
      { white: 0, black: 0 },
    );
    expect(game.pipCount(whiteBoard, "white")).toBe(29);

    const blackBarAndPoint = emptyPoints();
    blackBarAndPoint[20] = -1;
    const blackBoard = bd(
      blackBarAndPoint,
      { white: 0, black: 2 },
      { white: 0, black: 0 },
    );
    expect(game.pipCount(blackBoard, "black")).toBe(55);

    const oneLeftBoardPts = emptyPoints();
    oneLeftBoardPts[2] = 1;
    const oneLeftBoard = bd(oneLeftBoardPts, { white: 0, black: 0 }, { white: 14, black: 0 });
    expect(game.pipCount(oneLeftBoard, "white")).toBe(2);
  });

  it("[G03] dice → moves", () => {
    const start = bd([...game.startingPoints()], { white: 0, black: 0 }, { white: 0, black: 0 });

    expect(game.maxPlies(start, "white", [3, 3, 3, 3])).toBe(4);
    expect(game.maxPlies(start, "white", [3, 1])).toBe(2);
    expect(game.maxPlies(start, "black", [6, 6, 6, 6])).toBe(4);
  });

  it("[G04] legal-move generation (blocked points + hits)", () => {
    const pts = emptyPoints();
    pts[8] = 1;
    pts[5] = -2;
    pts[6] = -1;
    const board = bd(pts);

    const blocked = game.singleMoves(board, "white", 3) as Move[];
    expect(norm(blocked)).toEqual(norm([]));

    const hit = game.singleMoves(board, "white", 2) as Move[];
    expect(norm(hit)).toEqual(norm([{ from: 8, to: 6, die: 2 }]));

    const start = bd([...game.startingPoints()], { white: 0, black: 0 }, { white: 0, black: 0 });
    const dieSixMoves = game.singleMoves(start, "white", 6) as Move[];
    const fromValues = [...new Set(dieSixMoves.map((m) => m.from))].sort((a, b) => a - b);

    expect(fromValues).toEqual([8, 13, 24]);
    expect(dieSixMoves.every((m) => m.to >= 1 && m.to <= 24)).toBe(true);
  });

  it("[G05] use higher die", () => {
    const pts = emptyPoints();
    pts[13] = 1;
    pts[6] = -2;
    const board = bd(pts);

    expect(game.maxPlies(board, "white", [3, 4])).toBe(1);

    const legal = game.legalMovesNow(board, "white", [3, 4]) as Move[];
    expect(norm(legal)).toEqual(norm([{ from: 13, to: 9, die: 4 }]));
    expect(norm(legal)).not.toContain("13-10-3");
  });

  it("[G06] bar re-entry + blocked pass", () => {
    const entryPts = emptyPoints();
    entryPts[10] = -1;
    entryPts[8] = 1;
    const entryBoard = bd(entryPts, { white: 1, black: 0 }, { white: 0, black: 0 });

    const entryMoves = game.singleMoves(entryBoard, "white", 2) as Move[];
    expect(norm(entryMoves)).toEqual(norm([{ from: 0, to: 23, die: 2 }]));
    expect(entryMoves.every((m) => m.from === 0)).toBe(true);

    const blockedPts = emptyPoints();
    blockedPts[23] = -2;
    blockedPts[21] = -2;
    const blockedBoard = bd(blockedPts, { white: 1, black: 0 }, { white: 0, black: 0 });

    expect(game.legalMovesNow(blockedBoard, "white", [2, 4])).toEqual([]);
  });

  it("[G07] hitting → bar", () => {
    const pts = emptyPoints();
    pts[8] = 1;
    pts[5] = -1;
    const board = bd(pts);

    const hit = game.applyMove(board, "white", { from: 8, to: 5, die: 3 });
    expect(hit).toBe(true);
    expect(board.points[5]).toBe(1);
    expect(board.bar.black).toBe(1);
    expect(board.points[8]).toBe(0);
  });

  it("[G08] bear-off incl overshoot", () => {
    const overshootAllowedPts = emptyPoints();
    overshootAllowedPts[3] = 2;
    const overshootAllowedBoard = bd(overshootAllowedPts, { white: 0, black: 0 }, { white: 13, black: 0 });

    expect(game.allInHome(overshootAllowedBoard, "white")).toBe(true);
    expect(norm(game.singleMoves(overshootAllowedBoard, "white", 5) as Move[])).toEqual(
      norm([{ from: 3, to: 25, die: 5 }]),
    );

    const overshootRejectedPts = emptyPoints();
    overshootRejectedPts[5] = 1;
    overshootRejectedPts[3] = 1;
    const overshootRejectedBoard = bd(overshootRejectedPts, { white: 0, black: 0 }, { white: 13, black: 0 });

    const dieFiveMoves = game.singleMoves(overshootRejectedBoard, "white", 5) as Move[];
    expect(norm(dieFiveMoves)).toEqual(norm([{ from: 5, to: 25, die: 5 }]));
    expect(norm(dieFiveMoves)).not.toContain("3-25-5");
  });
});
