GOAL: We are building a fully functional backgammon game in Node + TypeScript that runs on localhost. This is the final chunk (6 of 6). All components exist: engine (`src/game.ts`), AI (`src/ai.ts`), server (`src/server.ts`), frontend (`public/`).

TASK: Wire everything together and verify the product end to end. Fix every defect you find — the acceptance bar is a complete product with **0 errors**.

Verification steps (do them all, for real, with tools):

1. **Boot.** `node src/server.ts` starts cleanly and prints the URL. Confirm `GET /health` returns `200 {"status":"ok","port":8002}`. Then start a second copy and confirm it exits non-zero with a clear single-line message naming port `8002` as already in use. (Stop both after.)
2. **Full game loop through the API.** Drive a real game with `curl`: `/api/new` → `/api/roll` → `/api/move` … → `/api/endturn` → `/api/ai` … repeat. A full human-vs-AI game must be playable toward completion with **no server exceptions** (REQ-COMPLETE). Use `BENCH_DEBUG=1` with `/api/debug/roll` to script dice where that speeds up coverage.
3. **Turn-flow integrity.** Confirm: a consumed die cannot be reused; turns alternate white → black → white; doubles give 4 moves; bar re-entry is forced before other moves; a player with no legal move auto-passes with `turnOver === true`, `legalMoves === []`, and a `message` mentioning "no legal move"/"pass" (REQ-TURN). Every move the UI offers and the AI makes is legal (REQ-AILEGAL).
4. **Win handling.** Force a near-end position (debug seam) and confirm `winner`/`winType`/`pointsWon` are set correctly for single, gammon, and backgammon cases, the message is shown, and `/api/new` resets to the standard opening position without reload while keeping score (REQ-NEWGAME).
5. **Doubling cube.** Offer a double as the human (`/api/double`), have the AI respond; answer an AI-offered double (`/api/double/respond`). Confirm cube value/owner transitions per the cube state machine, and that `canDouble` is only true in the roll phase for a player who may double.
6. **Frontend smoke.** Load `http://localhost:8002/` and check the page renders the board, all the `data-testid` hooks exist (24 points, 30 checkers, hints on selection, dice, cube, pips), checker/hint animations are present in the CSS, and there is no horizontal overflow at 1280×800.
7. **Clean up.** Kill any server processes you started. Leave the repo in a state where `npm start` alone runs the product.

Fix every defect you find before finishing. When you are finished with this task print CHUNK FINISHED at the end.
