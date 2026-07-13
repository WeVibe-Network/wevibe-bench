// Backgammon AI: move selection (easy/medium/hard) + doubling-cube reasoning.
import {
  type Board,
  type Move,
  type Player,
  allSequences,
  cloneBoard,
  pipCount,
  opponent,
  allInHome,
} from "./game.ts";

// Evaluate a board from `player`'s perspective. Higher = better for player.
export function evaluate(b: Board, player: Player): number {
  const opp = opponent(player);
  const myPip = pipCount(b, player);
  const oppPip = pipCount(b, opp);

  let score = 0;
  // Race: being ahead in the pip count is good.
  score += (oppPip - myPip) * 1.0;

  // Checkers borne off are very valuable.
  const myOff = player === "white" ? b.off.white : b.off.black;
  const oppOff = player === "white" ? b.off.black : b.off.white;
  score += myOff * 12;
  score -= oppOff * 12;

  // Checkers on the bar are terrible (own) / great (opp).
  const myBar = player === "white" ? b.bar.white : b.bar.black;
  const oppBar = player === "white" ? b.bar.black : b.bar.white;
  score -= myBar * 30;
  score += oppBar * 30;

  const sign = player === "white" ? 1 : -1;

  // Points made (blocks), blots (exposed), home-board strength, primes.
  let myBlotDanger = 0;
  let homePointsMade = 0;
  let consecutiveHome = 0;
  let bestPrime = 0;
  let curPrime = 0;

  for (let p = 1; p <= 24; p++) {
    const v = b.points[p] * sign; // >0 mine
    if (v >= 2) {
      score += 3; // making a point is good (safety + blocking)
      // extra reward for points that block the opponent (in front of opp checkers)
      // and for home-board points.
      if (isHomePoint(p, player)) homePointsMade++;
      curPrime++;
      if (curPrime > bestPrime) bestPrime = curPrime;
      // stacking too many on one point is mildly wasteful
      if (v > 3) score -= (v - 3) * 0.6;
    } else {
      curPrime = 0;
      if (v === 1) {
        // a blot: penalize by how easily it can be hit
        myBlotDanger += blotDanger(b, p, player);
      }
    }
  }
  score -= myBlotDanger * 1.2;
  score += homePointsMade * 4; // strong home board => better to hit + contain
  score += bestPrime * bestPrime * 1.5; // primes are powerful

  // Anchor in opponent's home board (defensive) is valuable when behind.
  if (hasAnchor(b, player)) score += 6;

  // Bearing-off efficiency: if all home, reward having checkers deep/even.
  if (allInHome(b, player) && myBar === 0) score += 10;

  return score;
}

function isHomePoint(p: number, player: Player): boolean {
  return player === "white" ? p >= 1 && p <= 6 : p >= 19 && p <= 24;
}

function hasAnchor(b: Board, player: Player): boolean {
  // a made point in opponent's home board
  const sign = player === "white" ? 1 : -1;
  const range = player === "white" ? [19, 24] : [1, 6];
  for (let p = range[0]; p <= range[1]; p++) {
    if (b.points[p] * sign >= 2) return true;
  }
  return false;
}

// Approximate danger of a blot on point p for `player`: sum over opponent
// checkers of the chance a die/combination reaches it. Uses a shot-count model.
function blotDanger(b: Board, p: number, player: Player): number {
  const opp = opponent(player);
  const oppSign = opp === "white" ? 1 : -1;
  // opponent moves: white high->low, black low->high.
  // opponent checker on q hits p if it can travel (dist) = |direction| toward p.
  let shots = 0;
  const oppBar = opp === "white" ? b.bar.white : b.bar.black;

  // helper: is there an opponent checker that could be `dist` away and move onto p
  const canReachFrom = (dist: number): number => {
    if (dist < 1 || dist > 24) return 0;
    // direct: single die (1..6)
    let count = 0;
    // find opponent checker at the point that is `dist` before p in opp's direction
    let q: number;
    if (opp === "white") q = p + dist; // white moves down, so source is higher
    else q = p - dist; // black moves up, source is lower
    if (oppBar > 0) {
      // opponent on bar enters near their entry; treat entry reach roughly
    }
    if (q >= 1 && q <= 24 && b.points[q] * oppSign >= 1) count = 1;
    return count;
  };

  // Number of dice rolls (out of 36) that produce each distance. Simplified:
  // direct shots (1..6) and common combinations (up to 12).
  const rollsForDistance: Record<number, number> = {
    1: 11, 2: 12, 3: 14, 4: 15, 5: 15, 6: 17,
    7: 6, 8: 6, 9: 5, 10: 3, 11: 2, 12: 3,
  };
  const reachable = new Set<number>();
  for (let d = 1; d <= 12; d++) {
    if (canReachFrom(d) > 0) reachable.add(d);
  }
  for (const d of reachable) shots += rollsForDistance[d] || 0;
  return shots / 36; // expected-ish hit pressure (0..~1.5)
}

export interface AiMoveResult {
  moves: Move[];
  board: Board;
}

// Pick the AI's full-turn move sequence for the given dice.
export function chooseMoves(
  b: Board,
  player: Player,
  dice: number[],
  difficulty: "easy" | "medium" | "hard",
): AiMoveResult {
  const seqs = allSequences(b, player, dice);
  if (seqs.length === 0) return { moves: [], board: cloneBoard(b) };
  if (seqs.length === 1) return { moves: seqs[0].moves, board: seqs[0].board };

  if (difficulty === "easy") {
    // Mostly random, with a mild bias against blundering huge blots.
    // 65% pure random, 35% avoid the worst.
    if (Math.random() < 0.65) {
      const pick = seqs[Math.floor(Math.random() * seqs.length)];
      return { moves: pick.moves, board: pick.board };
    }
    difficulty = "medium";
  }

  const scored = seqs.map((s) => ({ ...s, score: evaluate(s.board, player) }));
  scored.sort((a, b2) => b2.score - a.score);

  if (difficulty === "medium") {
    // choose among the top third with weighting, so it's decent but beatable
    const topN = Math.max(1, Math.ceil(scored.length / 3));
    const top = scored.slice(0, topN);
    // 70% best, else random within top group
    if (Math.random() < 0.7) return { moves: top[0].moves, board: top[0].board };
    const pick = top[Math.floor(Math.random() * top.length)];
    return { moves: pick.moves, board: pick.board };
  }

  // hard: always the best-evaluated sequence
  return { moves: scored[0].moves, board: scored[0].board };
}

// ---------------- Doubling cube reasoning ----------------

// Rough win-probability estimate from the current position for `player`,
// based on pip counts and race state. Returns 0..1.
export function winProbability(b: Board, player: Player): number {
  const opp = opponent(player);
  const myPip = pipCount(b, player);
  const oppPip = pipCount(b, opp);
  // If someone is way ahead in bear-off, reflect it.
  const total = myPip + oppPip;
  if (total === 0) return 0.5;
  // Logistic on normalized pip difference. Leader with fewer pips is favored.
  const diff = oppPip - myPip; // positive => player ahead
  // scale: a lead of ~8 pips ~ small edge; contact games are noisier.
  const k = 0.045;
  let prob = 1 / (1 + Math.exp(-k * diff));
  // account for being on the bar (bad) — checkers on bar already inflate pip, ok.
  return Math.max(0.02, Math.min(0.98, prob));
}

export interface CubeDecision {
  action: "double" | "no-double";
  reasoning: string;
}

// Should the AI offer a double at the start of its turn?
export function shouldAiDouble(
  b: Board,
  player: Player,
  cube: { value: number; owner: Player | null },
  difficulty: "easy" | "medium" | "hard",
): CubeDecision {
  // can only double if centered or AI owns the cube
  if (cube.owner !== null && cube.owner !== player) {
    return { action: "no-double", reasoning: "AI does not hold the cube." };
  }
  if (difficulty === "easy") {
    return { action: "no-double", reasoning: "Easy AI plays a straightforward game and keeps the cube centered." };
  }
  const wp = winProbability(b, player);
  const myPip = pipCount(b, player);
  const oppPip = pipCount(b, opponent(player));
  // Double window: strong but not certain (to avoid opponent's easy pass being pointless).
  const lower = difficulty === "hard" ? 0.68 : 0.72;
  const upper = 0.9; // too-good positions: play on for gammon rather than double out
  if (wp >= lower && wp <= upper) {
    return {
      action: "double",
      reasoning: `AI estimates a ${(wp * 100).toFixed(0)}% winning chance (pips ${myPip} vs ${oppPip}). It is in the doubling window, so it offers the cube.`,
    };
  }
  if (wp > upper) {
    return {
      action: "no-double",
      reasoning: `AI is winning strongly (${(wp * 100).toFixed(0)}%) and plays on for a gammon rather than doubling you out.`,
    };
  }
  return {
    action: "no-double",
    reasoning: `AI's winning chances (${(wp * 100).toFixed(0)}%) are not yet high enough to double.`,
  };
}

// Should the AI accept a double offered to it?
export function shouldAiAccept(
  b: Board,
  player: Player,
  difficulty: "easy" | "medium" | "hard",
): CubeDecision {
  const wp = winProbability(b, player); // AI's own win probability
  const myPip = pipCount(b, player);
  const oppPip = pipCount(b, opponent(player));
  // Take point ~ 25% (drop if below). Easy AI is timid and drops earlier.
  const takePoint = difficulty === "easy" ? 0.32 : difficulty === "medium" ? 0.27 : 0.24;
  if (wp >= takePoint) {
    return {
      action: "double", // reuse field: "double" == accept
      reasoning: `AI takes: it still wins about ${(wp * 100).toFixed(0)}% of the time (pips ${myPip} vs ${oppPip}), above its take point.`,
    };
  }
  return {
    action: "no-double", // == decline/pass
    reasoning: `AI passes: only about ${(wp * 100).toFixed(0)}% winning chance (pips ${myPip} vs ${oppPip}), below its take point, so it declines and concedes the current stake.`,
  };
}
