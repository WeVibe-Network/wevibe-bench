// ─────────────────────────────────────────────────────────────────────────────
// EDGE GATES — admitted to the scored suite 2026-08-13 (WO-GATE-ROSTER).
//
// Nine boundary/degenerate-input gates that the original 62 did not pin. The
// suite total went 62 → 71; `tiers.json` classifies everything under a path
// segment named `edge` as `tier: "edge"`, so the board can render these
// distinctly and a scorecard can quote a core-only bar WITHOUT any gate
// vanishing from the denominator. A tier is a partition, never an exemption.
//
// WHY THESE, AND NOT MORE. Every gate here is a REAL rule of backgammon that
// the original 62 leave unpinned, and every one PASSES against the golden
// oracle. None is a trick, a performance bound, or a restatement of an existing
// gate. The suite is meant to be complete and inclusive, not impossible: a gate
// the oracle itself cannot satisfy is a bug in the benchmark, not a hard
// question.
//
// DELIBERATELY NOT ADDED:
//   · A forced-ordering gate (`maxPlies` must force the one ordering that plays
//     both dice). Real and uncovered, but every position constructed for it
//     degenerated into "the other die simply has no move", which restates G05.
//     A badly built forcing test is worse than none — it reads as a hard
//     question while measuring nothing.
//   · Frontend edge gates. They boot a server on :8002 and could not be
//     validated against the oracle while a campaign was live. Shipping gates
//     never observed passing risks shipping an impossible one.
//
// PURE ENGINE ONLY. These import `game.ts` directly through `loadEngine()` and
// bind no port, so they add no :8002 contention to the backend phase and cost
// milliseconds (measured: 9 tests in 14ms).
// ─────────────────────────────────────────────────────────────────────────────

import { beforeAll, describe, expect, it } from "vitest";
import { loadEngine } from "../../lib/harness.ts";

type Board = {
  points: number[];
  bar: { white: number; black: number };
  off: { white: number; black: number };
};

/** An empty 0..25 board; index 0 and 25 are the BAR/OFF sentinels. */
const emptyBoard = (): Board => ({
  points: new Array(26).fill(0),
  bar: { white: 0, black: 0 },
  off: { white: 0, black: 0 },
});

const boardKey = (b: Board): string =>
  `${b.points.join(",")}|${b.bar.white},${b.bar.black}|${b.off.white},${b.off.black}`;

describe("Backgammon edge gates", () => {
  let game: any;

  beforeAll(async () => {
    ({ game } = await loadEngine());
    expect(game).toBeTruthy();
  });

  // ── dice ──────────────────────────────────────────────────────────────────

  it("[E01] REQ-DOUBLES — doubles are played as four plies, not two", () => {
    // The single most consequential dice rule the suite does not pin. An engine
    // that treats [3,3] as two moves plays a materially different game and
    // would still pass every existing gate.
    const b = emptyBoard();
    b.points = game.startingPoints();

    expect(game.maxPlies(b, "white", [3, 3, 3, 3])).toBe(4);
    // And the same dice as a non-double pair consume only two.
    expect(game.maxPlies(b, "white", [3, 5])).toBe(2);
  });

  // ── bear-off ──────────────────────────────────────────────────────────────

  it("[E02] REQ-BEAROFF-HIGHEST — overshoot bears off only from the highest occupied point", () => {
    // With a checker still on a HIGHER point, a die larger than a lower
    // checker's distance may not bear that lower checker off — it must be
    // played as an ordinary move. Getting this wrong lets a player bear off
    // several rolls early, which no existing gate detects.
    const b = emptyBoard();
    b.points[6] = 1;
    b.points[3] = 1;

    const withHigher = game.singleMoves(b, "white", 5);
    expect(withHigher.some((m: any) => m.to === 25)).toBe(false);
    expect(withHigher.some((m: any) => m.from === 6 && m.to === 1)).toBe(true);

    // Remove the higher checker and the SAME die now legitimately bears off.
    b.points[6] = 0;
    const alone = game.singleMoves(b, "white", 5);
    expect(alone.some((m: any) => m.from === 3 && m.to === 25)).toBe(true);
  });

  it("[E03] REQ-BEAROFF-EXACT — an exact die always bears off", () => {
    const b = emptyBoard();
    b.points[6] = 1;
    b.points[3] = 1;
    // Exact distance is legal even with a higher checker present.
    expect(game.singleMoves(b, "white", 3).some((m: any) => m.from === 3 && m.to === 25)).toBe(true);
  });

  // ── the bar ───────────────────────────────────────────────────────────────

  it("[E04] REQ-BAR-BLOCKED — a shut-out player has no legal move at all", () => {
    // A closed home board is a real, reachable position. The engine must return
    // an EMPTY move list rather than an illegal move or a throw — and the turn
    // must be forfeited, not silently skipped past the bar.
    const b = emptyBoard();
    b.bar.white = 1;
    // White enters on 25-die, so 1..6 map to points 24..19. Shut all six.
    for (let p = 19; p <= 24; p += 1) b.points[p] = -2;
    // A checker elsewhere that COULD otherwise move — it must stay frozen while
    // the bar is occupied.
    b.points[13] = 2;

    expect(game.maxPlies(b, "white", [3, 5])).toBe(0);
    expect(game.legalMovesNow(b, "white", [3, 5])).toEqual([]);
  });

  it("[E05] REQ-BAR-PRIORITY — a checker on the bar freezes every other checker", () => {
    // Entry is not merely preferred, it is mandatory. Here entry IS available,
    // so the only legal moves must be bar entries.
    const b = emptyBoard();
    b.bar.white = 1;
    b.points[13] = 2;

    const moves = game.singleMoves(b, "white", 3);
    expect(moves.length).toBeGreaterThan(0);
    expect(moves.every((m: any) => m.from === 0)).toBe(true);
  });

  // ── counting ──────────────────────────────────────────────────────────────

  it("[E06] REQ-PIP-BAR — a checker on the bar counts a full 25 pips", () => {
    // The bar is the most-commonly-dropped term in a pip count, and an
    // under-count silently biases every cube and AI decision that reads it.
    const b = emptyBoard();
    b.bar.white = 1;
    expect(game.pipCount(b, "white")).toBe(25);

    b.points[6] = 1;
    expect(game.pipCount(b, "white")).toBe(31);

    // Borne-off checkers contribute nothing.
    b.off.white = 3;
    expect(game.pipCount(b, "white")).toBe(31);
  });

  it("[E07] REQ-ALLINHOME-BAR — a checker on the bar means NOT all in home", () => {
    // The precondition for bearing off. An engine that ignores the bar here
    // lets a player bear off while still on the bar.
    const b = emptyBoard();
    b.points[3] = 1;
    expect(game.allInHome(b, "white")).toBe(true);

    b.bar.white = 1;
    expect(game.allInHome(b, "white")).toBe(false);
  });

  // ── move generation ───────────────────────────────────────────────────────

  it("[E08] REQ-SEQ-DEDUP — full-turn sequences are distinct by RESULTING BOARD", () => {
    // Two orderings of the same two moves reach one position. An engine that
    // enumerates paths instead of positions hands its AI duplicate candidates
    // and skews any search that weights them.
    const b = emptyBoard();
    b.points[10] = 2;

    const seqs = game.allSequences(b, "white", [1, 2]);
    const keys = new Set(seqs.map((s: any) => boardKey(s.board)));
    expect(keys.size).toBe(seqs.length);
    // Four move-paths exist here; they reach exactly two distinct positions.
    expect(seqs.length).toBe(2);
  });

  it("[E09] REQ-BOARD-ISOLATION — cloneBoard is a deep copy", () => {
    // Move search clones constantly. A shallow copy corrupts the real board
    // during lookahead, which surfaces as nondeterministic illegal moves rather
    // than as a clean failure — the worst possible failure mode to debug.
    const b = emptyBoard();
    b.points[10] = 2;
    b.bar.white = 1;
    b.off.black = 2;

    const clone = game.cloneBoard(b);
    clone.points[10] = 99;
    clone.bar.white = 99;
    clone.off.black = 99;

    expect(b.points[10]).toBe(2);
    expect(b.bar.white).toBe(1);
    expect(b.off.black).toBe(2);
  });
});
