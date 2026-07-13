# Backgammon Task — Published CONTRACT (interface only)

This is the interface every worker implementation MUST satisfy. It is the ONLY spec a worker
receives. It defines *what* the surface looks like — **never** expected values, answers, or the
hard edge-rules (those live only in the hidden gate oracle). A worker fills `scaffold/` until the
gate suite reports 100% against its work.

> **Deliberate omissions (do NOT add them here):** the doc-comments below intentionally DO NOT
> state (a) the "use the higher die when only one die is playable" rule, (b) the bear-off
> *overshoot* condition, or (c) the gammon/backgammon boundary. Discovering those from behaviour
> is part of the task; the gates test them.

---

## 1. Entrypoint & port

- Language/runtime: **Node + TypeScript**, zero external runtime deps. Node ≥ 22 runs the `.ts`
  files directly (type-stripping); engine imports use explicit `./x.ts` specifiers.
- Start command (from the implementation dir): `node src/server.ts` (also `npm start`).
- The HTTP server MUST bind **port 8002** (`http://localhost:8002/`).
- If port 8002 is already in use, the process MUST exit non-zero after printing a **clear,
  single-line message that names the port** (not a raw unhandled-exception stack). It MUST NOT
  hang or silently swap ports.
- On successful boot it MUST print a startup line containing the URL.

## 2. Backend function surface (`src/game.ts` + `src/ai.ts`)

All names below are EXACT and are gated against. Types are the contract; bodies are the task.

### Types & constants (`game.ts`)
```ts
export type Player = "white" | "black";
export const BAR = 0;   // sentinel "from" for entering from the bar
export const OFF = 25;  // sentinel "to" for bearing off

export interface Move { from: number; to: number; die: number; }
export interface AppliedMove extends Move { hit: boolean; }

export interface Board {
  points: number[];                              // length 26, index 1..24 used; +n = n white, -n = n black
  bar: { white: number; black: number };
  off: { white: number; black: number };
}

export interface GameState {
  points: number[];
  bar: { white: number; black: number };
  off: { white: number; black: number };
  turn: Player;
  phase: "roll" | "move" | "gameover" | "doubleOffered";
  dice: number[];                                // dice rolled this turn (2, or 4 for doubles)
  remainingDice: number[];                       // dice not yet consumed
  cube: { value: number; owner: Player | null }; // null owner = centered
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
```
Board convention: points numbered 1..24. **White** (the human) moves HIGH→LOW (24→1), home = 1..6,
bears off past point 1. **Black** (the AI) moves LOW→HIGH (1→24), home = 19..24, bears off past 24.
`points[p] > 0` = that many white checkers; `points[p] < 0` = that many black checkers (abs value).

### `game.ts` functions (EXACT signatures + contract doc-comments)
```ts
/** The two-player identity: returns the other player. */
export function opponent(p: Player): number /* Player */;

/** The standard opening arrangement as a fresh points[] array (length 26, index 1..24 used). */
export function startingPoints(): number[];

/** A fresh GameState for a new game at the given difficulty (white to move, phase "roll"). */
export function createGame(difficulty: GameState["difficulty"]): GameState;

/** True when ALL of `player`'s checkers are in that player's home quadrant (none on the bar,
 *  none outside home). */
export function allInHome(b: Board, player: Player): boolean;

/** Deep-copy a Board (points + bar + off) so callers can explore without mutating. */
export function cloneBoard(b: Board): Board;

/** Every legal single-checker move `player` could make using ONE die of value `die` from board
 *  `b`, as {from,to,die}. `from` may be BAR; `to` may be OFF. (Considers entering from the bar,
 *  landing rules, and bearing off.) */
export function singleMoves(b: Board, player: Player, die: number): Move[];

/** Apply one single move to board `b` IN PLACE for `player`. Returns whether the move hit an
 *  opponent blot (sending it to the bar). */
export function applyMove(b: Board, player: Player, m: Move): boolean;

/** The maximum number of dice from `dice` that `player` can legally consume from board `b`
 *  (searching all orderings). */
export function maxPlies(b: Board, player: Player, dice: number[]): number;

/** The single moves `player` may legally choose RIGHT NOW given the remaining `dice`, honouring
 *  the rule that a player must use as many dice as possible. */
export function legalMovesNow(b: Board, player: Player, dice: number[]): Move[];

/** The pip count for `player` on board `b` (checkers on the bar count as the maximum distance). */
export function pipCount(b: Board, player: Player): number;

/** Whether `player` has borne off all 15 checkers, and if so the classification of the win. */
export function checkWin(b: Board, player: Player): { won: boolean; type: "single" | "gammon" | "backgammon" | null };
```

### `ai.ts` functions (EXACT signatures + contract doc-comments)
```ts
type AiMoveResult = { moves: Move[]; board: Board };
type CubeDecision = { action: "double" | "no-double"; reasoning: string };

/** A scalar quality score of board `b` from `player`'s perspective (higher = better for player). */
export function evaluate(b: Board, player: Player): number;

/** Choose the AI's full-turn move sequence for the given `dice` at `difficulty`, returning the
 *  chosen moves and the resulting board. Every returned move MUST be legal. */
export function chooseMoves(b: Board, player: Player, dice: number[], difficulty: "easy" | "medium" | "hard"): AiMoveResult;

/** Estimate `player`'s probability of winning from board `b`, in [0,1]. */
export function winProbability(b: Board, player: Player): number;

/** Decide whether the AI (`player`) should OFFER a double, given the cube state and difficulty.
 *  `action:"double"` = offer, `"no-double"` = hold. `reasoning` is a human-readable string. */
export function shouldAiDouble(b: Board, player: Player, cube: { value: number; owner: Player | null }, difficulty: "easy" | "medium" | "hard"): CubeDecision;

/** Decide whether the AI (`player`) should ACCEPT a double the human just offered.
 *  `action:"double"` = TAKE (accept), `"no-double"` = PASS (decline/concede). */
export function shouldAiAccept(b: Board, player: Player, difficulty: "easy" | "medium" | "hard"): CubeDecision;
```

## 3. HTTP API

All game routes accept `POST` with a JSON body (empty `{}` allowed) and respond `200
application/json` with the **full serialized state** (§4). Unknown `/api/*` → `404
{"error":"unknown endpoint"}`. Static files served from `public/` for all other paths.

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/state` | — | Return current state (no mutation). |
| POST | `/api/new` | `{difficulty?}` | New game, keep score. |
| POST | `/api/roll` | — | Human rolls; phase → move. |
| POST | `/api/move` | `{from,to,die}` | Apply one human sub-move. |
| POST | `/api/undo` | — | Undo last sub-move this turn. |
| POST | `/api/endturn` | — | End human turn (only when `turnOver`). |
| POST | `/api/double` | — | Human offers a double; AI responds. |
| POST | `/api/double/respond` | `{accept:boolean}` | Human answers an AI-offered double. |
| POST | `/api/ai` | — | Advance one AI step. |
| GET | `/health` | — | `200 {"status":"ok","port":8002}` (liveness; always available). |

### Debug seam (gated by env `BENCH_DEBUG=1`)
The debug API is part of the published contract; it lets the harness force deterministic
positions and dice. When `BENCH_DEBUG` is not `1`, these routes MUST behave as unknown endpoints
(`404`).

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/debug/state` | a full/partial state object (§4 field names) | Overwrite the in-memory game with the supplied fields (points/bar/off/turn/phase/dice/remainingDice/cube/difficulty/score/winner/winType/pointsWon/doubleOfferedBy/message). Returns the resulting serialized state. |
| POST | `/api/debug/roll` | `{dice:number[]}` | Enqueue `dice` as the next roll; the next dice-roll consumes this queue instead of `Math.random`. Supply doubles as a 4-length array (e.g. `[3,3,3,3]`); a normal roll is 2-length. Returns serialized state. |

> A position supplied to `/api/debug/state` should represent a real backgammon position — **exactly 15
> checkers per side** across points+bar+off. The frontend renders a fixed 15 checkers per colour, so a
> partial position is not a valid render target.

## 4. Serialized state schema (`/api/state` response)

Exactly these top-level keys (plus any route-specific `extra` fields spread in):
```
points, bar, off, turn, phase, dice, remainingDice, cube, difficulty, score,
winner, winType, pointsWon, doubleOfferedBy, message, turnOver, gamesPlayed,
pip, legalMoves, canDouble
```
- `pip`: `{ white: number, black: number }` — computed pip counts for both players.
- `legalMoves`: `Move[]` — the human's legal moves right now (`[]` unless it is the human's move phase).
- `canDouble`: `boolean` — may the human offer a double now.
- `history` is NOT serialized.

## 5. Frontend — required `data-testid` list

The page (`public/index.html` + `public/app.js`) MUST expose these hooks. Static ids keep their
existing `id`; each also carries a `data-testid` with the SAME string. Dynamically-created
elements carry the attributes listed.

**Static (`data-testid` == the value):**
`scoreWhite`, `scoreBlack`, `difficulty`, `newGameBtn`, `board`, `playfield`, `checkerLayer`,
`pointHints`, `turnIndicator`, `pipWhite`, `pipBlack`, `cube`, `cubeVal`, `cubeOwner`, `dice`,
`rollBtn`, `doubleBtn`, `undoBtn`, `endTurnBtn`, `message`, `modalOverlay`, `modalTitle`,
`modalBody`, `modalBtns`.

**Dynamic:**
- Each board point: `data-testid="point"` **and** `data-point="<1..24>"`.
- Each checker: `data-testid="checker"`, `data-color="white|black"`, `data-loc="<1..24>|bar|off"`.
- Each move hint: `data-testid="hint"` (visible text = the die value(s), or `"off"` for bear-off).
- Each die: `data-testid="die"` (classes `used` / `rolling` convey state).
- The bar column: `data-testid="bar"`.
- The off tray: `data-testid="off-tray"`, with halves `data-testid="off-ai"` and `data-testid="off-you"`.
- The end-of-game modal is shown by toggling the `hidden` class off `modalOverlay`.

## 6. Behavioural expectations (interface-level, NOT answers)

- A full human-vs-AI game is playable to completion with no server exceptions.
- Every move the UI offers, and every move the AI makes, is legal.
- `difficulty` selects easy/medium/hard AI strength.
- Win / gammon / backgammon end the game with a banner and allow a new game without a page reload.
- The board is compact (no horizontal overflow) at 1280×800 and 1440×900.
- Checker movement / dice / hints are visibly animated.
