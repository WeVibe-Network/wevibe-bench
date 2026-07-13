// Backgammon core engine — pure logic, no I/O.
// Conventions:
//   Points are numbered 1..24. Internally stored in `points[1..24]`.
//   White (the human) moves from HIGH points to LOW points (24 -> 1). White home = 1..6.
//   Black (the AI) moves from LOW points to HIGH points (1 -> 24).  Black home = 19..24.
//   points[p] > 0  => that many WHITE checkers on point p.
//   points[p] < 0  => that many BLACK checkers on point p (abs value).
//   Bearing off: white bears off past point 1 (to 0); black bears off past point 24 (to 25).

export type Player = "white" | "black";

export const BAR = 0; // sentinel "from" for entering from the bar
export const OFF = 25; // sentinel "to" for bearing off

export interface Move {
  from: number; // 1..24, or BAR(0)
  to: number; // 1..24, or OFF(25)
  die: number; // 1..6
}

// A single applied sub-move plus what it did (for undo + animation).
export interface AppliedMove extends Move {
  hit: boolean; // did it hit an opponent blot?
}

export interface GameState {
  points: number[]; // length 26, index 1..24 used
  bar: { white: number; black: number };
  off: { white: number; black: number };
  turn: Player;
  phase: "roll" | "move" | "gameover" | "doubleOffered";
  dice: number[]; // the dice as rolled this turn (2, or 4 for doubles)
  remainingDice: number[]; // dice not yet consumed
  cube: { value: number; owner: Player | null }; // null = centered
  difficulty: "easy" | "medium" | "hard";
  score: { white: number; black: number };
  winner: Player | null;
  winType: "single" | "gammon" | "backgammon" | null;
  pointsWon: number;
  doubleOfferedBy: Player | null;
  message: string;
  history: AppliedMove[]; // sub-moves played this turn (undo stack)
  canDouble: boolean; // may the current (human) player offer a double now?
  turnOver: boolean; // no more legal moves this turn; awaiting End Turn
  gamesPlayed: number;
}

export function opponent(p: Player): Player {
  return p === "white" ? "black" : "white";
}

export function startingPoints(): number[] {
  const pts = new Array(26).fill(0);
  // White (positive)
  pts[24] = 2;
  pts[13] = 5;
  pts[8] = 3;
  pts[6] = 5;
  // Black (negative)
  pts[1] = -2;
  pts[12] = -5;
  pts[17] = -3;
  pts[19] = -5;
  return pts;
}

export function createGame(difficulty: GameState["difficulty"]): GameState {
  return {
    points: startingPoints(),
    bar: { white: 0, black: 0 },
    off: { white: 0, black: 0 },
    turn: "white",
    phase: "roll",
    dice: [],
    remainingDice: [],
    cube: { value: 1, owner: null },
    difficulty,
    score: { white: 0, black: 0 },
    winner: null,
    winType: null,
    pointsWon: 0,
    doubleOfferedBy: null,
    message: "Your turn. Roll the dice (or offer a double).",
    history: [],
    canDouble: true,
    turnOver: false,
    gamesPlayed: 0,
  };
}

// ---- helpers on a lightweight board view (used heavily by the AI) ----

export interface Board {
  points: number[];
  bar: { white: number; black: number };
  off: { white: number; black: number };
}

export function cloneBoard(b: Board): Board {
  return {
    points: b.points.slice(),
    bar: { white: b.bar.white, black: b.bar.black },
    off: { white: b.off.white, black: b.off.black },
  };
}

function ownCount(b: Board, p: number, player: Player): number {
  const v = b.points[p];
  return player === "white" ? Math.max(0, v) : Math.max(0, -v);
}

function oppBlockCount(b: Board, p: number, player: Player): number {
  // number of OPPONENT checkers on point p
  const v = b.points[p];
  return player === "white" ? Math.max(0, -v) : Math.max(0, v);
}

// Can `player` land on point `to` (1..24)? Blocked only by 2+ opponent checkers.
function canLand(b: Board, to: number, player: Player): boolean {
  if (to < 1 || to > 24) return false;
  return oppBlockCount(b, to, player) <= 1;
}

export function allInHome(b: Board, player: Player): boolean {
  if (player === "white") {
    if (b.bar.white > 0) return false;
    for (let p = 7; p <= 24; p++) if (b.points[p] > 0) return false;
    return true;
  } else {
    if (b.bar.black > 0) return false;
    for (let p = 1; p <= 18; p++) if (b.points[p] < 0) return false;
    return true;
  }
}

// distance a white checker on p must travel to bear off = p; black = 25 - p
function bearDistance(p: number, player: Player): number {
  return player === "white" ? p : 25 - p;
}

// highest-distance checker still in home for the player (for overshoot bear-off rule)
function maxDistanceInHome(b: Board, player: Player): number {
  let maxD = 0;
  if (player === "white") {
    for (let p = 6; p >= 1; p--) if (b.points[p] > 0) return p; // p is the distance
  } else {
    for (let p = 19; p <= 24; p++) if (b.points[p] < 0) return 25 - p;
  }
  return maxD;
}

// Generate the legal SINGLE moves for `player` using a single die value `die`.
export function singleMoves(b: Board, player: Player, die: number): Move[] {
  const moves: Move[] = [];
  const bar = player === "white" ? b.bar.white : b.bar.black;

  if (bar > 0) {
    // must enter from the bar first
    const entry = player === "white" ? 25 - die : die;
    if (canLand(b, entry, player)) moves.push({ from: BAR, to: entry, die });
    return moves;
  }

  const home = allInHome(b, player);
  for (let p = 1; p <= 24; p++) {
    if (ownCount(b, p, player) === 0) continue;
    const to = player === "white" ? p - die : p + die;
    const onBoard = to >= 1 && to <= 24;
    if (onBoard) {
      if (canLand(b, to, player)) moves.push({ from: p, to, die });
    } else if (home) {
      // bearing off candidate
      const dist = bearDistance(p, player);
      if (die === dist) {
        moves.push({ from: p, to: OFF, die });
      } else if (die > dist) {
        // overshoot only allowed if no checker with greater distance in home
        if (maxDistanceInHome(b, player) <= dist) {
          moves.push({ from: p, to: OFF, die });
        }
      }
    }
  }
  return moves;
}

// Apply a single move to a board (mutates). Returns whether it hit a blot.
export function applyMove(b: Board, player: Player, m: Move): boolean {
  const sign = player === "white" ? 1 : -1;
  // remove from source
  if (m.from === BAR) {
    if (player === "white") b.bar.white--;
    else b.bar.black--;
  } else {
    b.points[m.from] -= sign;
  }
  let hit = false;
  if (m.to === OFF) {
    if (player === "white") b.off.white++;
    else b.off.black++;
  } else {
    // hit?
    if (oppBlockCount(b, m.to, player) === 1) {
      b.points[m.to] = 0; // remove opponent blot
      if (player === "white") b.bar.black++;
      else b.bar.white++;
      hit = true;
    }
    b.points[m.to] += sign;
  }
  return hit;
}

// Maximum number of dice that can be consumed from this position with these dice.
export function maxPlies(b: Board, player: Player, dice: number[]): number {
  if (dice.length === 0) return 0;
  let best = 0;
  const tried = new Set<number>();
  for (let i = 0; i < dice.length; i++) {
    const d = dice[i];
    if (tried.has(d)) continue;
    tried.add(d);
    const rest = dice.slice(0, i).concat(dice.slice(i + 1));
    const ms = singleMoves(b, player, d);
    for (const m of ms) {
      const nb = cloneBoard(b);
      applyMove(nb, player, m);
      const val = 1 + maxPlies(nb, player, rest);
      if (val > best) best = val;
      if (best === dice.length) return best; // can't do better
    }
  }
  return best;
}

// The legal single moves the player may choose RIGHT NOW, honoring the
// "use the maximum number of dice" rule and the "higher die if only one" rule.
export function legalMovesNow(b: Board, player: Player, dice: number[]): Move[] {
  const mp = maxPlies(b, player, dice);
  if (mp === 0) return [];
  const result: Move[] = [];
  const tried = new Set<number>();
  for (let i = 0; i < dice.length; i++) {
    const d = dice[i];
    if (tried.has(d)) continue;
    tried.add(d);
    const rest = dice.slice(0, i).concat(dice.slice(i + 1));
    for (const m of singleMoves(b, player, d)) {
      const nb = cloneBoard(b);
      applyMove(nb, player, m);
      if (1 + maxPlies(nb, player, rest) === mp) result.push(m);
    }
  }

  // "Higher die" rule: non-doubles, exactly one die can be played (mp === 1),
  // and both dice are individually playable -> only the higher die is legal.
  const isDouble = dice.length >= 2 && dice.every((x) => x === dice[0]);
  if (mp === 1 && !isDouble && dice.length === 2) {
    const uniq = Array.from(new Set(dice));
    if (uniq.length === 2) {
      const playable = uniq.filter((d) => singleMoves(b, player, d).length > 0);
      if (playable.length === 2) {
        const higher = Math.max(uniq[0], uniq[1]);
        return result.filter((m) => m.die === higher);
      }
    }
  }
  return result;
}

// All maximal full-turn sequences from this position. Returns distinct resulting
// boards with one representative move-path each. Used by the AI.
export function allSequences(
  b: Board,
  player: Player,
  dice: number[],
): { board: Board; moves: Move[] }[] {
  const mp = maxPlies(b, player, dice);
  const out: { board: Board; moves: Move[] }[] = [];
  const seen = new Set<string>();

  function boardKey(bd: Board): string {
    return bd.points.join(",") + "|" + bd.bar.white + "," + bd.bar.black + "|" + bd.off.white + "," + bd.off.black;
  }

  function recurse(cur: Board, remaining: number[], path: Move[], depth: number) {
    if (depth === mp) {
      const key = boardKey(cur);
      if (!seen.has(key)) {
        seen.add(key);
        out.push({ board: cur, moves: path });
      }
      return;
    }
    const legal = legalMovesNowForSequence(cur, player, remaining, mp - depth);
    for (const m of legal) {
      const nb = cloneBoard(cur);
      applyMove(nb, player, m);
      const idx = remaining.indexOf(m.die);
      const rest = remaining.slice(0, idx).concat(remaining.slice(idx + 1));
      recurse(nb, rest, path.concat(m), depth + 1);
    }
  }

  if (mp === 0) return out;
  recurse(b, dice, [], 0);
  return out;
}

// like legalMovesNow but for sequence building: keep only moves that preserve
// the ability to reach `needed` further plies.
function legalMovesNowForSequence(b: Board, player: Player, dice: number[], needed: number): Move[] {
  const result: Move[] = [];
  const tried = new Set<number>();
  for (let i = 0; i < dice.length; i++) {
    const d = dice[i];
    if (tried.has(d)) continue;
    tried.add(d);
    const rest = dice.slice(0, i).concat(dice.slice(i + 1));
    for (const m of singleMoves(b, player, d)) {
      const nb = cloneBoard(b);
      applyMove(nb, player, m);
      if (1 + maxPlies(nb, player, rest) >= needed) result.push(m);
    }
  }
  return result;
}

export function pipCount(b: Board, player: Player): number {
  let pip = 0;
  for (let p = 1; p <= 24; p++) {
    const c = ownCount(b, p, player);
    if (c > 0) pip += c * bearDistance(p, player);
  }
  pip += (player === "white" ? b.bar.white : b.bar.black) * 25;
  return pip;
}

// Check whether `player` has just won and classify the win.
export function checkWin(b: Board, player: Player): { won: boolean; type: "single" | "gammon" | "backgammon" | null } {
  const off = player === "white" ? b.off.white : b.off.black;
  if (off < 15) return { won: false, type: null };
  const loser = opponent(player);
  const loserOff = loser === "white" ? b.off.white : b.off.black;
  if (loserOff > 0) return { won: true, type: "single" };
  // gammon (loser borne off none). backgammon if loser has checker on bar or in winner's home.
  const loserBar = loser === "white" ? b.bar.white : b.bar.black;
  let inWinnerHome = false;
  if (player === "white") {
    // winner home 1..6 -> loser (black) checkers there
    for (let p = 1; p <= 6; p++) if (b.points[p] < 0) inWinnerHome = true;
  } else {
    for (let p = 19; p <= 24; p++) if (b.points[p] > 0) inWinnerHome = true;
  }
  if (loserBar > 0 || inWinnerHome) return { won: true, type: "backgammon" };
  return { won: true, type: "gammon" };
}
