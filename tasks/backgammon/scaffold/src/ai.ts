// Backgammon AI: move selection (easy/medium/hard) + doubling-cube reasoning.
import {
  type Board,
  type Move,
  type Player,
  allSequences,
  cloneBoard,
  pipCount,
  opponent,
  allInHome,
} from "./game.ts";

export type AiMoveResult = { moves: Move[]; board: Board };
export type CubeDecision = { action: "double" | "no-double"; reasoning: string };

/** A scalar quality score of board `b` from `player`'s perspective (higher = better for player). */
export function evaluate(b: Board, player: Player): number {
  throw new Error("not implemented");
}

/** Choose the AI's full-turn move sequence for the given `dice` at `difficulty`, returning the
 *  chosen moves and the resulting board. Every returned move MUST be legal. */
export function chooseMoves(
  b: Board,
  player: Player,
  dice: number[],
  difficulty: "easy" | "medium" | "hard",
): AiMoveResult {
  throw new Error("not implemented");
}

/** Estimate `player`'s probability of winning from board `b`, in [0,1]. */
export function winProbability(b: Board, player: Player): number {
  throw new Error("not implemented");
}

/** Decide whether the AI (`player`) should OFFER a double, given the cube state and difficulty.
 *  `action:"double"` = offer, `"no-double"` = hold. `reasoning` is a human-readable string. */
export function shouldAiDouble(
  b: Board,
  player: Player,
  cube: { value: number; owner: Player | null },
  difficulty: "easy" | "medium" | "hard",
): CubeDecision {
  throw new Error("not implemented");
}

/** Decide whether the AI (`player`) should ACCEPT a double the human just offered.
 *  `action:"double"` = TAKE (accept), `"no-double"` = PASS (decline/concede). */
export function shouldAiAccept(
  b: Board,
  player: Player,
  difficulty: "easy" | "medium" | "hard",
): CubeDecision {
  throw new Error("not implemented");
}
