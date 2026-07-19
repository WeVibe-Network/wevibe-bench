# Backgammon Task — Published CONTRACT (requirements-to-implementation instrument)

This is the interface **and the complete set of behavioural requirements** every worker
implementation MUST satisfy. It is the ONLY spec a worker receives. **Every behaviour required to
pass the hidden gate suite is derivable from this document** (Walter-locked "Option A", 15-07-26).
The gates may *verify* these requirements with hidden fixtures/values, but they never require a
constant, formula, string, count, or mechanism that is not published here. Where a precise constant
is required, it is published below. Where several implementations satisfy a requirement, the oracle
accepts any of them.

A worker fills `scaffold/` until the gate suite reports 100% against its work. Feedback on failure
names the failed **public requirement** (REQ-ID + gate ID); it never reveals expected/observed values.

Each requirement carries a **REQ-ID** used by gate labels and failure feedback so a failure points
back to the exact clause below.

---

## 1. Entrypoint & port — `REQ-BIND`

- Language/runtime: **Node + TypeScript**, zero external runtime deps. Node ≥ 22 runs the `.ts`
  files directly (type-stripping); engine imports use explicit `./x.ts` specifiers.
- Start command (from the implementation dir): `node src/server.ts` (also `npm start`).
- The HTTP server MUST bind **port 8002** (`http://localhost:8002/`).
- If port 8002 is already in use, the process MUST exit non-zero after printing a **clear,
  single-line message that (a) names the port `8002` and (b) states it is already in use** — i.e.
  the message contains the port number and wording indicating it is "in use"/"already" bound (not a
  raw unhandled-exception stack). It MUST NOT hang or silently swap ports.
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

### Standard opening position — `REQ-INIT`
`startingPoints()` MUST return exactly this 26-length array (index 0 and 25 unused = 0):
```
[0, -2, 0, 0, 0, 0, 5, 0, 3, 0, 0, 0, -5, 5, 0, 0, 0, -3, 0, -5, 0, 0, 0, 0, 2, 0]
```
This is the standard arrangement: white 2 on 24, 5 on 13, 3 on 8, 5 on 6; black (negative) 2 on 1,
5 on 12, 3 on 17, 5 on 19 — 15 checkers per side. A fresh `createGame(d)` has white to move, phase
`"roll"`, cube `{value:1, owner:null}`, empty bar/off, `winner:null`, `points === startingPoints()`.

### Pip counting — `REQ-PIP`
`pipCount(b, player)` = the sum, over all that player's checkers, of each checker's distance to
bearing off: a **white** checker on point `p` has distance `p`; a **black** checker on point `p` has
distance `25 − p`; a checker **on the bar counts as the maximum distance `25`**. From the standard
opening, each side's pip count is therefore **167**.

### `game.ts` functions (EXACT signatures + contract doc-comments)
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

### Movement rules (published)
- **`REQ-BAR` — bar entry.** A player with any checker on the bar MUST enter them before making any
  other move. A **white** bar checker enters on point **`25 − die`**; a **black** bar checker enters
  on point **`die`** (the bar is conceptually point 25 for white / point 0 for black). Entry is
  blocked if the destination point holds ≥2 opponent checkers; if every rolled die's entry point is
  blocked, the player has no legal move (auto-pass, see REQ-TURN). While a checker is on the bar,
  `singleMoves` returns only bar-entry moves (all with `from === BAR`).
- **`REQ-HIT` — landing & hitting.** A checker may land on an empty point, a point it owns, or a
  point with exactly one opponent checker (a blot) — landing on a blot **hits** it, sending that
  opponent checker to the bar (`applyMove` returns `true`). A point with ≥2 opponent checkers is
  blocked.
- **`REQ-USEMAX` — use as many dice as possible.** A player must play a sequence that consumes the
  maximum number of dice legally possible.
- **`REQ-HIGHER-DIE` — higher die when only one is playable.** When both dice cannot be played but
  either one alone can, the player MUST play the **higher** die.
- **`REQ-BEAROFF` — bearing off & overshoot.** Bearing off (`to === OFF`) is legal only when
  `allInHome(b, player)`. A die equal to a checker's exact distance bears that checker off. A die
  **larger** than a checker's distance may bear it off (overshoot) **ONLY when there is no checker on
  a higher point** (farther from bear-off) in the player's home board; otherwise the larger die must
  be played as an in-board move. (White point 6 is "highest"/farthest; black point 19 is highest.)
- **`REQ-BEAROFF-GATE` — no bear-off while not all home.** `singleMoves` yields no bear-off (`to ===
  OFF`) move while any of the player's checkers is outside the home board or on the bar.

### Win classification — `REQ-WINCLASS`
When a player has borne off all 15 checkers (`checkWin.won === true`), the win type is:
- **single** — the loser has borne off at least one checker.
- **gammon** — the loser has borne off zero checkers, has none on the bar, and has none in the
  winner's home board.
- **backgammon** — the loser has borne off zero checkers AND has at least one checker either on the
  bar or in the winner's home board (white home = 1..6, black home = 19..24).

### `ai.ts` functions (EXACT signatures + contract doc-comments)
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

### `REQ-WINPROB` — win-probability semantics
`winProbability(b, player) ∈ [0, 1]`, and MUST:
- be **≈ 0.5** (within ±0.01) when both players have equal pip counts;
- be **monotonically non-decreasing** in the player's pip lead (`opponentPip − playerPip`);
- be **below the hard take point (< 0.24)** for a hopelessly-behind position (e.g. opponent ~2 pips,
  player ~350) and **above the double-window upper bound (> 0.90)** for a nearly-certain win.

Any function meeting these properties is accepted (the oracle checks the equal-pip value and the
correct side of the published thresholds — it does NOT require a specific formula).

### `REQ-CUBE-AI` — AI doubling-cube policy (published thresholds)
Win-probability thresholds the AI uses, by difficulty:
- **Take point** — `shouldAiAccept` returns `"double"` (TAKE/accept) iff `winProbability ≥ take
  point`, else `"no-double"` (PASS/decline). Take points: **easy 0.32, medium 0.27, hard 0.24**.
- **Offer window** — `shouldAiDouble` returns `"double"` (offer) iff `lower ≤ winProbability ≤ 0.90`,
  else `"no-double"`. Lower bound: **medium 0.72, hard 0.68**. **Easy never offers a double.** A
  position with `winProbability > 0.90` is "too good" → hold (play on for a gammon), not double.
- The AI only offers when it may double (cube centered or owned by the AI); it does not offer when
  the opponent owns the cube.

## 3. HTTP API — `REQ-API`

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

### Doubling-cube state machine — `REQ-CUBE-STATE`
- A new game's cube is `{value:1, owner:null}` (centered). `canDouble` is `true` only when it is the
  player's turn in the **`"roll"` phase (before rolling)** and the player may double (cube centered
  or owned by that player). During the **`"move"` phase** `canDouble` is `false`.
- When a double is **offered and accepted**, the cube **value doubles** and its **owner becomes the
  player who accepted (the taker)** — e.g. the human (white) offers, the AI (black) accepts → cube
  becomes `{value:2, owner:"black"}`.

### Debug seam (gated by env `BENCH_DEBUG=1`) — `REQ-DEBUG`
The debug API is part of the published contract; it lets the harness force deterministic
positions and dice. When `BENCH_DEBUG` is not `1`, these routes MUST behave as unknown endpoints
(`404`).

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/debug/state` | a full/partial state object (§4 field names) | Overwrite the in-memory game with the supplied fields (points/bar/off/turn/phase/dice/remainingDice/cube/difficulty/score/winner/winType/pointsWon/doubleOfferedBy/message). The response echoes the supplied fields back in the resulting serialized state. Returns the resulting serialized state. |
| POST | `/api/debug/roll` | `{dice:number[]}` | Enqueue `dice` as the next roll; the next dice-roll consumes this queue instead of `Math.random`. Supply doubles as a 4-length array (e.g. `[3,3,3,3]`); a normal roll is 2-length. A subsequent `/api/roll` yields those dice (sorted ascending in `dice`). Returns serialized state. |

> A position supplied to `/api/debug/state` should represent a real backgammon position — **exactly 15
> checkers per side** across points+bar+off. The frontend renders a fixed 15 checkers per colour, so a
> partial position is not a valid render target.

## 4. Serialized state schema (`/api/state` response) — `REQ-STATE`

Exactly these top-level keys (plus any route-specific `extra` fields spread in):
```
points, bar, off, turn, phase, dice, remainingDice, cube, difficulty, score,
winner, winType, pointsWon, doubleOfferedBy, message, turnOver, gamesPlayed,
pip, legalMoves, canDouble
```
- `pip`: `{ white: number, black: number }` — computed pip counts for both players (REQ-PIP).
- `legalMoves`: `Move[]` — the human's legal moves right now (`[]` unless it is the human's move phase).
- `canDouble`: `boolean` — may the human offer a double now (REQ-CUBE-STATE).
- `history` is NOT serialized.

## 5. Frontend — required `data-testid` list — `REQ-TESTID`

The page (`public/index.html` + `public/app.js`) MUST expose these hooks. Static ids keep their
existing `id`; each also carries a `data-testid` with the SAME string. Dynamically-created
elements carry the attributes listed.

**Static (`data-testid` == the value):**
`scoreWhite`, `scoreBlack`, `difficulty`, `newGameBtn`, `board`, `playfield`, `checkerLayer`,
`pointHints`, `turnIndicator`, `pipWhite`, `pipBlack`, `cube`, `cubeVal`, `cubeOwner`, `dice`,
`rollBtn`, `doubleBtn`, `undoBtn`, `endTurnBtn`, `message`, `modalOverlay`, `modalTitle`,
`modalBody`, `modalBtns`.

**Dynamic:**
- Each board point: `data-testid="point"` **and** `data-point="<1..24>"` — exactly 24 points.
- Each checker: `data-testid="checker"`, `data-color="white|black"`, `data-loc="<1..24>|bar|off"` —
  exactly 30 checkers (15 per colour), positioned at their board/bar/off location (REQ-RENDER).
- Each move hint: `data-testid="hint"` (visible text = the die value(s), or `"off"` for bear-off).
  A hint appears for each playable die when a movable checker is selected (REQ-HINT). Selecting a
  bar checker with two playable entry dice shows two hints, one per die. Clicking a hint executes
  that move for the selected checker, consuming the hint's die.
- Each die: `data-testid="die"` (classes `used` / `rolling` convey state).
- The bar column: `data-testid="bar"`.
- The off tray: `data-testid="off-tray"`, with halves `data-testid="off-ai"` and `data-testid="off-you"`.
- The end-of-game modal is shown by toggling the `hidden` class off `modalOverlay`.

### Cube UI — `REQ-CUBEUI`
`cubeVal` displays the cube's integer value (e.g. `"1"`, then `"2"` after a double). `cubeOwner`
displays the owner: wording for the human/white (e.g. "you"/"your"/"white"), for the AI/black (e.g.
"ai"/"opponent"/"black"), or centered when unowned (e.g. "center"/"centered"/"centre").

### Pip display — `REQ-PIPUI`
`pipWhite` / `pipBlack` show integer pip counts equal to the engine's `pip.white` / `pip.black`
(167 each at the opening).

## 6. Behavioural / gameplay requirements (published)

- **`REQ-COMPLETE`** — a full human-vs-AI game is playable to completion, reaching a `winner` with a
  valid `winType`, with **no server exceptions**.
- **`REQ-TURN`** — turn-flow integrity: a die cannot be reused once consumed; turns alternate
  white → black → white; and when a player has **no legal move** the turn **auto-passes**
  (`turnOver === true`, `legalMoves === []`) and `message` states there is no legal move / that a
  pass occurred (wording containing "no legal move" or "pass").
- **`REQ-AILEGAL`** — every move the UI offers and every move the AI makes is legal.
- **`REQ-AISTRENGTH`** — `difficulty` selects strength; over repeated self-play, **hard wins more
  games than easy**.
- **`REQ-NEWGAME`** — win / gammon / backgammon end the game with a banner (the win message contains
  "You win" when the human wins; a modal, if used, is titled with "Win") and allow a new game
  **without a page reload** (which resets to the standard opening position).
- **`REQ-COMPACT`** — the board is compact (no horizontal overflow) at 1280×800 and 1440×900.
- **`REQ-ANIM`** — checker movement, dice, and hints are visibly animated. Acceptance contract
  (the narrowest publicly-testable form of "visibly animated"): each **checker** element animates
  position changes via a CSS `transition` whose `transition-property` includes `transform` (or
  `all`) **and has a non-zero duration**, **OR** via a CSS `animation` (non-`none` `animation-name`,
  non-zero duration); each move **hint** element uses a CSS `animation` (non-`none` `animation-name`,
  non-zero duration). Any of these mechanisms is accepted.
