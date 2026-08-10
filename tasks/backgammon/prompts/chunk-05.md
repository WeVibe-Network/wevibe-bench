GOAL: We are building a fully functional backgammon game in Node + TypeScript that runs on localhost. This is chunk 5 of 6. The backend (engine, AI, API) is complete from chunks 2-4.

TASK: Build the frontend in `public/index.html`, `public/style.css`, and `public/app.js` — a compact, animated, fully playable board UI driven by the API.

Requirements:

- **Standard board orientation**, 24 points, white moving high→low toward home 1..6. Keep the viewport **compact** — no horizontal overflow at 1280×800 and 1440×900.
- Render from the serialized state: checkers on points/bar/off, dice, cube, pip counts, score, turn indicator, messages.
- **Move interaction:** clicking a movable checker shows legal destinations as hints; each hint shows the die value (or "off" for bear-off); clicking a hint executes that move and consumes that die. Selecting a bar checker with two playable entry dice shows two hints, one per die. When the player has no legal move, show a clear no-legal-move / pass notice.
- **Pip counts:** display both players' integer pip counts (167 each at the opening).
- **Doubling cube UI:** show the cube's integer value and its owner (human / AI / centered), a working double button, and a way to answer an AI-offered double.
- **Animation (acceptance contract):** each checker element animates position changes via a CSS `transition` whose `transition-property` includes `transform` (or `all`) with a non-zero duration, OR via a CSS `animation` (non-`none` name, non-zero duration); each move-hint element uses a CSS `animation` (non-`none` name, non-zero duration). Dice visibly roll.
- **Win flow:** win / gammon / backgammon end the game with a banner (the win message contains "You win" when the human wins; a modal, if used, is titled with "Win") and allow starting a new game WITHOUT a page reload.

Required `data-testid` hooks (EXACT — an external contract gates on these; static elements keep their existing `id` and ALSO carry a `data-testid` with the same string):

**Static:** `scoreWhite`, `scoreBlack`, `difficulty`, `newGameBtn`, `board`, `playfield`, `checkerLayer`, `pointHints`, `turnIndicator`, `pipWhite`, `pipBlack`, `cube`, `cubeVal`, `cubeOwner`, `dice`, `rollBtn`, `doubleBtn`, `undoBtn`, `endTurnBtn`, `message`, `modalOverlay`, `modalTitle`, `modalBody`, `modalBtns`.

**Dynamic:**

- Each board point: `data-testid="point"` and `data-point="<1..24>"` — exactly 24 points.
- Each checker: `data-testid="checker"`, `data-color="white|black"`, `data-loc="<1..24>|bar|off"` — exactly 30 checkers (15 per colour), positioned at their board/bar/off location.
- Each move hint: `data-testid="hint"` (visible text = the die value(s), or `"off"` for bear-off).
- Each die: `data-testid="die"` (classes `used` / `rolling` convey state).
- The bar column: `data-testid="bar"`.
- The off tray: `data-testid="off-tray"`, with halves `data-testid="off-ai"` and `data-testid="off-you"`.
- The end-of-game modal is shown by toggling the `hidden` class off `modalOverlay`.
- `cubeVal` displays the cube integer; `cubeOwner` displays owner wording ("you"/"white" for the human, "ai"/"black" for the AI, "center"/"centered" when unowned).
- `pipWhite` / `pipBlack` show integer pip counts equal to the API's `pip.white` / `pip.black`.

When you are finished with this task print CHUNK FINISHED at the end, then call the self_compact tool as the last action of your turn.
