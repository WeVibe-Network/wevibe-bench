GOAL: We are building a fully functional backgammon game in Node + TypeScript that runs on localhost. This is chunk 4 of 6. The engine (`src/game.ts`) and AI (`src/ai.ts`) are complete.

TASK: Implement the HTTP server and the full game API in `src/server.ts`, wiring the engine and AI into a playable backend.

Binding requirements:

- The server MUST bind **port 8002**. If 8002 is already in use, exit non-zero after printing a clear, single-line message that names `8002` and says it is already in use — never hang, never silently swap ports. On boot, print a startup line containing the URL.
- All game routes accept `POST` with a JSON body (empty `{}` allowed) and respond `200 application/json` with the **full serialized state** (schema below). Unknown `/api/*` → `404 {"error":"unknown endpoint"}`. Static files are served from `public/` for all other paths.

The API surface (EXACT):

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

Doubling-cube state machine (REQ-CUBE-STATE):

- A new game's cube is `{value:1, owner:null}` (centered). `canDouble` is `true` only when it is the player's turn in the `"roll"` phase (before rolling) and the player may double (cube centered or owned by that player). During `"move"` phase `canDouble` is `false`.
- When a double is offered and accepted, the cube value doubles and its owner becomes the player who accepted (the taker) — e.g. human (white) offers, AI (black) accepts → `{value:2, owner:"black"}`.

Debug seam (REQ-DEBUG) — gated by env `BENCH_DEBUG=1`; when `BENCH_DEBUG` is not `1` these routes MUST behave as unknown endpoints (404):

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/debug/state` | a full/partial state object (field names per the schema below) | Overwrite the in-memory game with the supplied fields. Returns the resulting serialized state. |
| POST | `/api/debug/roll` | `{dice:number[]}` | Enqueue `dice` as the next roll; the next dice-roll consumes this queue instead of `Math.random`. Doubles are a 4-length array (e.g. `[3,3,3,3]`). A subsequent `/api/roll` yields those dice (sorted ascending in `dice`). Returns serialized state. |

Serialized state schema (REQ-STATE) — every game-route response carries exactly these top-level keys (plus any route-specific `extra` fields spread in):

```
points, bar, off, turn, phase, dice, remainingDice, cube, difficulty, score,
winner, winType, pointsWon, doubleOfferedBy, message, turnOver, gamesPlayed,
pip, legalMoves, canDouble
```

- `pip`: `{ white: number, black: number }` — both players' pip counts from the engine (167 each at the opening).
- `legalMoves`: `Move[]` — the human's legal moves right now (`[]` unless it is the human's move phase).
- `canDouble`: boolean, per the cube state machine above.
- `history` is NOT serialized.

Turn flow the server must drive: doubles yield 4 moves; dice are consumed as used; when the human has no legal move the turn auto-passes (`turnOver === true`, `legalMoves === []`, `message` mentions "no legal move"/"pass"); after the human turn ends, `/api/ai` advances the AI using `chooseMoves`; win/gammon/backgammon ends the game with `winner`, `winType`, `pointsWon` (cube value × win multiplier) and a clear `message`; `/api/new` starts a fresh game without any reload and keeps `score`/`gamesPlayed`.

**Write in chunks:** never emit more than ~150 lines in a single write/edit tool call — build large files up in ~150-line chunks across several calls, never one giant call.

When you are finished with this task print CHUNK FINISHED at the end, then call the self_compact tool as the last action of your turn.
