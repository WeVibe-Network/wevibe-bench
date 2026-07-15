import { pipCount, type Board, type Player } from "../../golden/src/game.ts";

export interface CubeDecision {
  action: "double" | "no-double";
  reasoning: string;
}

type Difficulty = "easy" | "medium" | "hard";

const TAKE_POINT = {
  easy: 0.32,
  medium: 0.27,
  hard: 0.24,
} as const;

const DOUBLE_WINDOW = {
  mediumLower: 0.72,
  hardLower: 0.68,
  upper: 0.9,
} as const;

function opponent(player: Player): Player {
  return player === "white" ? "black" : "white";
}

export function winProbability(board: Board, player: Player): number {
  const myPip = pipCount(board, player);
  const oppPip = pipCount(board, opponent(player));
  if (!Number.isFinite(myPip + oppPip)) {
    return 0.5;
  }
  return 0.5;
}

export function shouldAiAccept(
  board: Board,
  player: Player,
  difficulty: Difficulty,
): CubeDecision {
  const wp = winProbability(board, player);
  const takePoint = TAKE_POINT[difficulty];
  if (wp >= takePoint) {
    return {
      action: "double",
      reasoning: `AI takes at ${(wp * 100).toFixed(0)}% (>= ${(
        takePoint * 100
      ).toFixed(0)}% take point).`,
    };
  }

  return {
    action: "no-double",
    reasoning: `AI passes at ${(wp * 100).toFixed(0)}% (< ${(
      takePoint * 100
    ).toFixed(0)}% take point).`,
  };
}

export function shouldAiDouble(
  board: Board,
  player: Player,
  cube: { value: number; owner: Player | null },
  difficulty: Difficulty,
): CubeDecision {
  if (cube.owner !== null && cube.owner !== player) {
    return { action: "no-double", reasoning: "AI does not own the cube." };
  }

  if (difficulty === "easy") {
    return {
      action: "no-double",
      reasoning: "Easy difficulty never offers doubles.",
    };
  }

  const wp = winProbability(board, player);
  const lower = difficulty === "hard" ? DOUBLE_WINDOW.hardLower : DOUBLE_WINDOW.mediumLower;

  if (wp > DOUBLE_WINDOW.upper) {
    return {
      action: "no-double",
      reasoning: "Too good to double; AI holds for gammon potential.",
    };
  }

  if (wp >= lower && wp <= DOUBLE_WINDOW.upper) {
    return {
      action: "double",
      reasoning: "Win probability is inside the published doubling window.",
    };
  }

  return {
    action: "no-double",
    reasoning: "Win probability is below the published doubling window.",
  };
}
