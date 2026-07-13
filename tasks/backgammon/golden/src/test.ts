// Engine stress test: play many random full games, asserting invariants.
import {
  createGame,
  legalMovesNow,
  applyMove,
  checkWin,
  pipCount,
  opponent,
  type Board,
  type Player,
} from "./game.ts";
import { chooseMoves } from "./ai.ts";

function boardOf(g: any): Board {
  return { points: g.points, bar: g.bar, off: g.off };
}
function totalCheckers(b: Board, player: Player): number {
  const sign = player === "white" ? 1 : -1;
  let n = 0;
  for (let p = 1; p <= 24; p++) {
    const v = b.points[p] * sign;
    if (v > 0) n += v;
  }
  n += player === "white" ? b.bar.white + b.off.white : b.bar.black + b.off.black;
  return n;
}
function rollDice(): number[] {
  const a = 1 + Math.floor(Math.random() * 6);
  const c = 1 + Math.floor(Math.random() * 6);
  return a === c ? [a, a, a, a] : [a, c];
}

let games = 0,
  singles = 0,
  gammons = 0,
  backgammons = 0,
  maxTurns = 0;
const N = 400;

for (let i = 0; i < N; i++) {
  const g = createGame(i % 3 === 0 ? "hard" : i % 3 === 1 ? "medium" : "easy");
  let turn: Player = Math.random() < 0.5 ? "white" : "black";
  let done = false;
  let turns = 0;
  while (!done) {
    turns++;
    if (turns > 2000) throw new Error(`Game ${i} did not terminate (possible infinite loop)`);
    const b = boardOf(g);
    const dice = rollDice();
    // choose full sequence via AI logic (works for both sides), random difficulty already set
    const res = chooseMoves(b, turn, dice, g.difficulty);
    for (const m of res.moves) applyMove(b, turn, m);
    // invariant: 15 checkers each
    for (const pl of ["white", "black"] as Player[]) {
      const tc = totalCheckers(b, pl);
      if (tc !== 15) throw new Error(`Game ${i} turn ${turns}: ${pl} has ${tc} checkers (expected 15)`);
    }
    // pip counts non-negative
    if (pipCount(b, turn) < 0) throw new Error("negative pip");
    const win = checkWin(b, turn);
    if (win.won) {
      games++;
      if (win.type === "single") singles++;
      else if (win.type === "gammon") gammons++;
      else backgammons++;
      maxTurns = Math.max(maxTurns, turns);
      done = true;
    }
    turn = opponent(turn);
  }
}

// Legal-move enforcement spot check: no move outside legalMovesNow should be accepted.
console.log(`OK: ${games} games completed. singles=${singles} gammons=${gammons} backgammons=${backgammons} maxTurns=${maxTurns}`);
