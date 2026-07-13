// Backgammon core engine — pure logic, no I/O.
// Conventions:
//   Points are numbered 1..24. Internally stored in `points[1..24]`.
//   White (the human) moves from HIGH points to LOW points (24 -> 1). White home = 1..6.
//   Black (the AI) moves from LOW points to HIGH points (1 -> 24).  Black home = 19..24.
//   points[p] > 0  => that many WHITE checkers on point p.
//   points[p] < 0  => that many BLACK checkers on point p (abs value).
//   Bearing off: white bears off past point 1 (to 0); black bears off past point 24 (to 25).

export type Player = "white" | "black";

export const BAR = 0; // sentinel "from" for entering from the bar
export const OFF = 25; // sentinel "to" for bearing off

export interface Move {
  from: number;
  to: number;
  die: number;
}

export interface AppliedMove extends Move {
  hit: boolean;
}

export interface Board {
  points: number[];
  bar: { white: number; black: number };
  off: { white: number; black: number };
}

export interface GameState {
  points: number[];
  bar: { white: number; black: number };
  off: { white: number; black: number };
  turn: Player;
  phase: "roll" | "move" | "gameover" | "doubleOffered";
  dice: number[];
  remainingDice: number[];
  cube: { value: number; owner: Player | null };
  difficulty: "easy" | "medium" | "hard";
  score: { white: number; black: number };
  winner: Player | null;
  winType: "single" | "gammon" | "backgammon" | null;
  pointsWon: number;
  doubleOfferedBy: Player | null;
  message: string;
  history: AppliedMove[];
  canDouble: boolean;
  turnOver: boolean;
  gamesPlayed: number;
}

/** The two-player identity: returns the other player. */
export function opponent(p: Player): Player {
  throw new Error("not implemented");
}

/** The standard opening arrangement as a fresh points[] array (length 26, index 1..24 used). */
export function startingPoints(): number[] {
  throw new Error("not implemented");
}

/** A fresh GameState for a new game at the given difficulty (white to move, phase "roll"). */
export function createGame(difficulty: GameState["difficulty"]): GameState {
  throw new Error("not implemented");
}

/** Deep-copy a Board (points + bar + off) so callers can explore without mutating. */
export function cloneBoard(b: Board): Board {
  throw new Error("not implemented");
}

/** True when ALL of `player`'s checkers are in that player's home quadrant (none on the bar,
 *  none outside home). */
export function allInHome(b: Board, player: Player): boolean {
  throw new Error("not implemented");
}

/** Every legal single-checker move `player` could make using ONE die of value `die` from board
 *  `b`, as {from,to,die}. `from` may be BAR; `to` may be OFF. (Considers entering from the bar,
 *  landing rules, and bearing off.) */
export function singleMoves(b: Board, player: Player, die: number): Move[] {
  throw new Error("not implemented");
}

/** Apply one single move to board `b` IN PLACE for `player`. Returns whether the move hit an
 *  opponent blot (sending it to the bar). */
export function applyMove(b: Board, player: Player, m: Move): boolean {
  throw new Error("not implemented");
}

/** The maximum number of dice from `dice` that `player` can legally consume from board `b`
 *  (searching all orderings). */
export function maxPlies(b: Board, player: Player, dice: number[]): number {
  throw new Error("not implemented");
}

/** The single moves `player` may legally choose RIGHT NOW given the remaining `dice`, honouring
 *  the rule that a player must use as many dice as possible. */
export function legalMovesNow(b: Board, player: Player, dice: number[]): Move[] {
  throw new Error("not implemented");
}

/** Returns all maximal full-turn move sequences and their resulting boards for the AI. */
export function allSequences(
  b: Board,
  player: Player,
  dice: number[],
): { board: Board; moves: Move[] }[] {
  throw new Error("not implemented");
}

/** The pip count for `player` on board `b` (checkers on the bar count as the maximum distance). */
export function pipCount(b: Board, player: Player): number {
  throw new Error("not implemented");
}

/** Whether `player` has borne off all 15 checkers, and if so the classification of the win. */
export function checkWin(b: Board, player: Player): { won: boolean; type: "single" | "gammon" | "backgammon" | null } {
  throw new Error("not implemented");
}
