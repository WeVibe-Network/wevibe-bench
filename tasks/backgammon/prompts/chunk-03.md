GOAL: We are building a fully functional backgammon game in Node + TypeScript that runs on localhost. This is chunk 3 of 6. The engine (`src/game.ts`) is complete from chunk 2.

TASK: Implement the AI in `src/ai.ts` — evaluation, move choice, win probability, and the doubling-cube policy with accept/decline reasoning. Replace the stubs with real implementations.

The exact function surface (EXACT signatures — an external contract gates on these names):

```ts
type AiMoveResult = { moves: Move[]; board: Board };
type CubeDecision = { action: "double" | "no-double"; reasoning: string };

/** A scalar quality score of board `b` from `player`'s perspective (higher = better for player). */
export function evaluate(b: Board, player: Player): number;

/** Choose the AI's full-turn move sequence for the given `dice` at `difficulty`, returning the
 *  chosen moves and resulting board. Every returned move MUST be legal (REQ-AILEGAL). Difficulty
 *  selects strength (REQ-AISTRENGTH: hard is stronger than easy). */
export function chooseMoves(b: Board, player: Player, dice: number[], difficulty: "easy" | "medium" | "hard"): AiMoveResult;

/** Estimate `player`'s probability of winning from board `b`, in [0,1]. See REQ-WINPROB:
 *  ≈0.5 at equal pip counts, monotonically increasing in the player's pip lead, approaching its
 *  bounds at the extremes. */
export function winProbability(b: Board, player: Player): number;

/** Decide whether the AI (`player`) should OFFER a double, given the cube state and difficulty.
 *  `action:"double"` = offer, `"no-double"` = hold. See REQ-CUBE-AI. */
export function shouldAiDouble(b: Board, player: Player, cube: { value: number; owner: Player | null }, difficulty: "easy" | "medium" | "hard"): CubeDecision;

/** Decide whether the AI (`player`) should ACCEPT a double the human just offered.
 *  `action:"double"` = TAKE (accept), `"no-double"` = PASS (decline/concede). See REQ-CUBE-AI. */
export function shouldAiAccept(b: Board, player: Player, difficulty: "easy" | "medium" | "hard"): CubeDecision;
```

Requirements:

- **REQ-AILEGAL.** Every move the AI makes must be legal — build candidates with the engine's `legalMovesNow`/`singleMoves`, never by constructing moves directly.
- **REQ-AISTRENGTH.** `difficulty` selects real strength: over repeated self-play, **hard wins more games than easy**. (Typical shape: easy ≈ random/legal-simple, medium ≈ one-ply `evaluate`, hard ≈ deeper search or richer evaluation — any mechanism that truly orders strength is acceptable.)
- **REQ-WINPROB — win-probability semantics.** `winProbability(b, player) ∈ [0, 1]`, and MUST: be ≈ 0.5 (within ±0.01) at equal pip counts; be monotonically non-decreasing in the player's pip lead (`opponentPip − playerPip`); be below 0.24 for a hopelessly-behind position (e.g. opponent ~2 pips, player ~350) and above 0.90 for a nearly-certain win. Any function meeting these properties is accepted.
- **REQ-CUBE-AI — doubling-cube policy (exact published thresholds).**
  - **Take point** — `shouldAiAccept` returns `"double"` (TAKE) iff `winProbability ≥ take point`, else `"no-double"` (PASS). Take points: **easy 0.32, medium 0.27, hard 0.24**.
  - **Offer window** — `shouldAiDouble` returns `"double"` (offer) iff `lower ≤ winProbability ≤ 0.90`, else `"no-double"`. Lower bound: **medium 0.72, hard 0.68**. **Easy never offers a double.** A position above 0.90 is "too good" → hold (play on for a gammon), not double.
  - The AI only offers when it may double (cube centered or owned by the AI); it never offers when the opponent owns the cube.
  - Both cube functions return a human-readable `reasoning` string explaining the decision.

When you are finished with this task print CHUNK FINISHED at the end, then call the self_compact tool as the last action of your turn.
