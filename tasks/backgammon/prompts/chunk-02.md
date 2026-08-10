GOAL: We are building a fully functional backgammon game in Node + TypeScript that runs on localhost. This is chunk 2 of 6.

TASK: Implement the complete backgammon engine in `src/game.ts` — pure logic, no I/O. The shared types/constants are already in place from chunk 1 (do not change them). Replace the stubs with real implementations.

The exact function surface (EXACT signatures — an external contract gates on these names and doc-comments):

```ts
/** The two-player identity: returns the other player. */
export function opponent(p: Player): number /* Player */;

/** The standard opening arrangement as a fresh points[] array (see REQ-INIT for the exact array). */
export function startingPoints(): number[];

/** A fresh GameState for a new game at the given difficulty (white to move, phase "roll"). */
export function createGame(difficulty: GameState["difficulty"]): GameState;

/** True when ALL of `player`'s checkers are in that player's home quadrant (none on the bar,
 *  none outside home). White home = points 1..6; black home = points 19..24. */
export function allInHome(b: Board, player: Player): boolean;

/** Deep-copy a Board (points + bar + off) so callers can explore without mutating. */
export function cloneBoard(b: Board): Board;

/** Every legal single-checker move `player` could make using ONE die of value `die` from board
 *  `b`, as {from,to,die}. `from` may be BAR; `to` may be OFF. Rules — see REQ-BAR (bar entry),
 *  REQ-HIT (landing/hitting), REQ-BEAROFF (bearing off & overshoot), REQ-BEAROFF-GATE. */
export function singleMoves(b: Board, player: Player, die: number): Move[];

/** Apply one single move to board `b` IN PLACE for `player`. Returns whether the move hit an
 *  opponent blot (sending it to the bar). See REQ-HIT. */
export function applyMove(b: Board, player: Player, m: Move): boolean;

/** The maximum number of dice from `dice` that `player` can legally consume from board `b`
 *  (searching all orderings). Doubles present as four dice. */
export function maxPlies(b: Board, player: Player, dice: number[]): number;

/** The single moves `player` may legally choose RIGHT NOW given the remaining `dice`, honouring
 *  REQ-USEMAX (must use as many dice as possible) and REQ-HIGHER-DIE (must play the higher die when
 *  only one of the two can be played). */
export function legalMovesNow(b: Board, player: Player, dice: number[]): Move[];

/** The pip count for `player` on board `b` (see REQ-PIP; bar checkers count as max distance 25). */
export function pipCount(b: Board, player: Player): number;

/** Whether `player` has borne off all 15 checkers, and if so the classification of the win
 *  (see REQ-WINCLASS for the single/gammon/backgammon boundary). */
export function checkWin(b: Board, player: Player): { won: boolean; type: "single" | "gammon" | "backgammon" | null };
```

Rules (all mandatory):

- **REQ-INIT — opening position.** `startingPoints()` MUST return exactly this 26-length array (index 0 and 25 unused = 0):
  `[0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5, 5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0]`
  (white 2 on 24, 5 on 13, 3 on 8, 5 on 6; black 2 on 1, 5 on 12, 3 on 17, 5 on 19 — 15 per side.) A fresh `createGame(d)` has white to move, phase `"roll"`, cube `{value:1, owner:null}`, empty bar/off, `winner:null`, `points === startingPoints()`.
- **REQ-PIP — pip counting.** A white checker on point `p` has distance `p`; a black checker on point `p` has distance `25 − p`; a checker on the bar counts as the maximum distance `25`. From the standard opening each side is **167**.
- **REQ-BAR — bar entry.** A player with any checker on the bar MUST enter before any other move. A white bar checker enters on point `25 − die`; a black bar checker enters on point `die`. Entry is blocked if the destination holds ≥2 opponent checkers; if every rolled die's entry is blocked there is no legal move. While a checker is on the bar, `singleMoves` returns only bar-entry moves (`from === BAR`).
- **REQ-HIT — landing & hitting.** A checker may land on an empty point, a point it owns, or a blot (exactly one opponent checker) — landing on a blot hits it to the bar (`applyMove` returns `true`). A point with ≥2 opponent checkers is blocked.
- **REQ-USEMAX — use as many dice as possible.** A player must play a sequence that consumes the maximum number of dice legally possible.
- **REQ-HIGHER-DIE — higher die when only one is playable.** When both dice cannot be played but either one alone can, the player MUST play the higher die.
- **REQ-BEAROFF — bearing off & overshoot.** Bearing off (`to === OFF`) is legal only when `allInHome(b, player)`. A die equal to a checker's exact distance bears it off. A die LARGER than the distance may bear it off ONLY when no checker sits on a higher point (farther from bear-off) in that player's home board; otherwise the larger die must be played as an in-board move. (White point 6 is highest/farthest; black point 19 is highest.)
- **REQ-BEAROFF-GATE — no bear-off while not all home.** `singleMoves` yields no bear-off move while any of the player's checkers is outside the home board or on the bar.
- **REQ-WINCLASS — win classification.** When a player has borne off all 15: **single** — loser bore off at least one; **gammon** — loser bore off zero, none on the bar, none in the winner's home board; **backgammon** — loser bore off zero AND has at least one checker on the bar or in the winner's home board.

Also required by the full product (keep these in mind so your engine shapes support them): full turn flow — doubles give 4 moves, dice are consumed as used, turns alternate white → black → white, and a player with no legal move auto-passes (`turnOver === true`, `legalMoves === []`, message mentions "no legal move"/"pass"). The turn driver itself is wired up in chunk 4.

When you are finished with this task print CHUNK FINISHED at the end, then call the self_compact tool as the last action of your turn.
