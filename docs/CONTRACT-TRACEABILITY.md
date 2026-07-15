# Backgammon Benchmark — CONTRACT ↔ Oracle Traceability Matrix

**Status:** Option A repair, 15-07-26. Companion to `tasks/backgammon/CONTRACT.md`. Every gate is
listed (not only the four observed-failing ones). Columns: **REQ-ID** (public requirement) →
**public requirement text** (CONTRACT clause) → **observable behaviour** → **oracle assertion**
(gate) → **repair note** (how the former oracle-only dependency was published or relaxed so it is now
derivable from the CONTRACT and accepts multiple valid implementations).

This matrix is the audit that the LOCKED Option-A invariant holds: *no gate requires a constant,
formula, string, count, or mechanism that is not published in CONTRACT.md.*

## Backend gates (Vitest, `gates/backend/`)

| Gate | REQ-ID | Public requirement (CONTRACT §) | Observable behaviour | Oracle assertion | Repair note |
|---|---|---|---|---|---|
| G01 | REQ-INIT | §2 "Standard opening position" — exact 26-element array published | `startingPoints()`/`createGame()` yield the standard opening | `toEqual(startingArray)`, turn/phase/cube | Array now PUBLISHED verbatim in CONTRACT → derivable. |
| G02 | REQ-PIP | §2 "Pip counting" — distance rule + bar=25 + opening=167 published | pip counts for standard + custom boards | `toBe(167/29/55/2)` | Formula + bar=max-distance + opening 167 now PUBLISHED → the exact values are computable from the rule. |
| G03 | REQ-DICE | §2 doc-comment `maxPlies`; doubles→4 dice (§2 GameState) | max dice consumable | `toBe(4/2/4)` | Doubles-as-4 published; ply counts follow from movement rules. |
| G04 | REQ-MOVES | §2 `singleMoves` + REQ-HIT/REQ-BEAROFF | legal single moves, blocked/hit/landing | move-set equality | Landing/blocking/hit rules published (REQ-HIT). |
| G05 | REQ-HIGHER-DIE | §2 "REQ-HIGHER-DIE" — must play higher die when only one playable | forced higher-die selection | `legalMovesNow` set | **Was deliberate omission (a); now PUBLISHED.** |
| G06 | REQ-BAR | §2 "REQ-BAR" — white enters 25−die, black enters die; must enter first | bar entry destinations + blocked entry | move-set equality, `[]` when blocked | **Bar-entry arithmetic now PUBLISHED** (was unstated). |
| G07 | REQ-HIT | §2 "REQ-HIT" — landing on a blot hits it to the bar | hit sends opponent to bar | `applyMove` returns true, bar++ | Published. |
| G08 | REQ-BEAROFF | §2 "REQ-BEAROFF" — overshoot only when no checker on a higher home point | bear-off + overshoot legality | move-set equality | **Was deliberate omission (b); now PUBLISHED.** |
| G09 | REQ-BEAROFF-GATE | §2 "REQ-BEAROFF-GATE" — no bear-off while any checker outside home/on bar | bear-off blocked | `allInHome=false`, no `to===25` | Published (was mostly derivable). |
| G10 | REQ-WINCLASS | §2 "Win classification" — single/gammon/backgammon boundary published | win type classification | `checkWin.type` equality | **Was deliberate omission (c); boundary now PUBLISHED.** |
| G11 | REQ-CUBE-STATE | §3 "Doubling-cube state machine" — canDouble roll-phase-only, taker owns cube, value doubles | cube state transitions via HTTP | `cube` `toEqual({value:2,owner:"black"})`, `canDouble` | **Taker-owns + canDouble-in-roll-phase now PUBLISHED.** |
| G12 | REQ-CUBE-AI, REQ-WINPROB | §2 "REQ-CUBE-AI" (take-points .32/.27/.24; window .72/.68/.90; easy-never; >.90 too-good) + "REQ-WINPROB" (≈.5 parity, monotonic, clamp) | accept/decline + offer/hold decisions; winProb magnitude | decision `.action` at boards with margin; `winProbability` **`toBeCloseTo(0.5,2)` / `<TAKE_POINT.hard` / `>upper`** | **Thresholds now PUBLISHED**; winProbability relaxed from `toBe(0.5/0.02/0.98)` exact → behaviour-level (accepts any monotonic model). Decision boards keep clear margin so any published-threshold impl passes. |
| G13 | REQ-TURN | §6 "REQ-TURN" — no die reuse, alternate turns, auto-pass with msg | turn flow + auto-pass message | `remainingDice`, alternation, `message` `/no legal move\|pass/i` | Auto-pass **message content now PUBLISHED**; regex is a disjunction over published wording. |
| G14 | REQ-AILEGAL, REQ-AISTRENGTH | §2 `chooseMoves` "every move legal"; §6 "REQ-AISTRENGTH" hard>easy | AI legality over seeded self-play; hard wins more | replay-legality; `hardWins>easyWins` | "Every move legal" + "hard stronger than easy" PUBLISHED. Seed/counts are test determinism, not pass-constants (any legal+stronger-hard impl passes). |
| G15 | REQ-COMPLETE | §6 "REQ-COMPLETE" — game reaches a winner, no exceptions | scripted game completes | `winner` truthy, `winType` valid, no throw | Behavioural; dice script is test determinism. |
| G16 | REQ-BIND | §1 "REQ-BIND" — names port 8002 + "in use"/"already" wording | second bind fails with clear message | `toContain("8002")`, `/in use\|already/i` | **Message wording now PUBLISHED** ("names the port … states it is already in use"). |

## Conformance (Playwright pregate, `gates/conformance/`)

| Check | REQ-ID | Public requirement | Oracle assertion | Repair note |
|---|---|---|---|---|
| boot/health | REQ-BIND/REQ-API | §1/§3 `/health` → `{status:"ok",port:8002}` | listening, health 200/ok | Published. |
| debug.setState/roll | REQ-DEBUG | §3 debug seam — echoes supplied fields; roll enqueues | field echo, sorted dice | §3 now states the response echoes supplied fields. |
| state keys | REQ-STATE | §4 exact serialized keys | all 20 keys present | Published list. |
| DOM testids / counts | REQ-TESTID | §5 static+dynamic ids; 24 points / 30 checkers (15/side) | count `===24` / `===30` | Counts derive from §5 + "15 per side". `.selectable` is an OPTIONAL optimization; the click-based hint fallback IS the published REQ-HINT behaviour. |

## Frontend gates (Playwright, `gates/frontend/`)

| Gate | REQ-ID | Public requirement | Oracle assertion | Repair note |
|---|---|---|---|---|
| F01 | REQ-RENDER | §5 board renders, no console errors | no errors, board visible | Behavioural. |
| F02 | REQ-RENDER | §5 15/side, 24 points | `toHaveCount(24)/(30)`, 15 each | Counts published. |
| F03 | REQ-HINT | §5 hint text = die value/off; move advances state | remainingDice decrements | Hint semantics published. |
| F04 | REQ-HINT | §5 hint text = rolled die values or "off" | hint text parts in dice/"off" | Published. |
| F05 | REQ-TURN | §6 auto-pass message wording | `message` `/no legal move\|pass/i` | Message content published. |
| F06 | REQ-PIPUI | §5 "Pip display" = engine pip; opening 167 | DOM pip === state.pip; `===167` | Published (REQ-PIP + REQ-PIPUI). |
| F07 | REQ-CUBEUI | §5 "Cube UI" — value integer + owner vocabulary | `cubeVal` text "1"/"2"; owner label regex | Value + owner vocabulary (you/ai/center) now PUBLISHED. |
| F08 | REQ-TESTID | §5 difficulty selector | difficulty field reflects selection | Published. |
| F09 | REQ-HIT | §5 checker `data-loc="bar"` after hit | bar checker count 1 | Published. |
| F10 | REQ-BAR | §2 REQ-BAR bar entry (white 25−die → loc 20/22) + REQ-HINT (one hint per die) | bar checker enters; lands at 25−die loc; 2 hints | **Bar-entry arithmetic now PUBLISHED** → loc "20"/"22" derivable; hint-per-die published. |
| F11 | REQ-BEAROFF | §5 off `data-loc="off"` | off checker count | Published. |
| F12 | REQ-NEWGAME | §6 "You win" banner + new game no reload → opening position | "You win"; per-loc opening counts; modal hidden toggle | Win message + opening position now PUBLISHED (REQ-INIT). |
| F13 | REQ-COMPACT | §6 no horizontal overflow @ 1280×800 / 1440×900 | scrollWidth ≤ clientWidth+1 | Published verbatim. |
| F14 | REQ-ANIM | §6 "REQ-ANIM" acceptance contract — checker: CSS transition on transform/all OR CSS animation; hint: CSS animation | checker (transition matches transform/all) OR (animation non-none, non-0s); hint animation non-none/non-0s | **Mechanism now PUBLISHED as the narrowest acceptance contract, and BROADENED** to accept transition-on-transform OR animation for checkers (was transition-only). Accepts multiple valid CSS implementations. |

## Latent oracle-only dependencies found & resolved (the full audit, not just the 4 observed)

Publishing (now derivable from CONTRACT): REQ-INIT array (G01/F12), REQ-PIP 167 (G02/F06),
REQ-HIGHER-DIE (G05), REQ-BAR 25−die (G06/F10), REQ-BEAROFF overshoot (G08), REQ-WINCLASS boundary
(G10), REQ-CUBE-STATE taker-owns (G11), REQ-CUBE-AI thresholds (G12), REQ-TURN/REQ-BIND message
wording (G13/F05/G16), REQ-CUBEUI value+owner vocab (F07).

Relaxed to accept multiple implementations: REQ-WINPROB winProbability (G12) exact→behavioural;
REQ-ANIM checker animation (F14) transition-only→transition-or-animation.

Test-determinism (NOT pass-constants — any conforming implementation passes): seeds/sample counts in
G14/G15, dice scripts in G15. These do not encode a hidden requirement; they make the behavioural
requirement (legal AI / hard>easy / completable game) deterministic.

## Feedback integrity
Worker-facing feedback (built in `wevibe_bench/adapters/backgammon.py`) forwards ONLY the gate
`check` label — which now carries `[Gxx]/[Fxx]` + its public `REQ-ID` — as `- {label}: FAILING`.
Expected/observed values from the vitest/playwright failure messages are DROPPED before the worker
sees them (they remain only in host-side internal logs per R-37 / ORACLE-ISOLATION-DIRECTIVE). So a
failure points the worker at a PUBLIC requirement with semantic traction, never at a hidden value.

## Integrity invariants (unchanged by Option A)
Gates/golden remain physically absent from Docker workers (`/work` = scaffold copy only; gates run
host-side via `BENCH_TARGET`); the cheat detector (oracle-reference in `events.jsonl` → CHEAT →
INVALID) stays; problems-only feedback stays. Publishing edge-rules into the CONTRACT is orthogonal
to these layers.
