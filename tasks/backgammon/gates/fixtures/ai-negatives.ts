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

function clampProbability(probability: number): number {
  return Math.max(0.02, Math.min(0.98, probability));
}

type WinProbabilityFn = (board: Board, player: Player) => number;

type CubeAiImplementation = {
  winProbability: WinProbabilityFn;
  shouldAiAccept: (
    board: Board,
    player: Player,
    difficulty: Difficulty,
  ) => CubeDecision;
  shouldAiDouble: (
    board: Board,
    player: Player,
    cube: { value: number; owner: Player | null },
    difficulty: Difficulty,
  ) => CubeDecision;
};

function baselineWinProbability(board: Board, player: Player): number {
  const myPip = pipCount(board, player);
  const oppPip = pipCount(board, opponent(player));
  const raw = 1 / (1 + Math.exp(-0.045 * (oppPip - myPip)));
  return clampProbability(raw);
}

function buildShouldAiAccept(winProbability: WinProbabilityFn) {
  return (board: Board, player: Player, difficulty: Difficulty): CubeDecision => {
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
  };
}

function buildBaselineShouldAiDouble(winProbability: WinProbabilityFn) {
  return (
    board: Board,
    player: Player,
    cube: { value: number; owner: Player | null },
    difficulty: Difficulty,
  ): CubeDecision => {
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
  };
}

const baselineShouldAiAccept = buildShouldAiAccept(baselineWinProbability);

/** Bug: developer never implemented cube offers, so the AI always holds. */
export const alwaysHold: CubeAiImplementation = {
  winProbability: baselineWinProbability,
  shouldAiAccept: baselineShouldAiAccept,
  shouldAiDouble: () => ({
    action: "no-double",
    reasoning: "Offer logic left unimplemented, so AI always holds.",
  }),
};

/** Bug: AI offers as soon as it is allowed to, skipping all policy thresholds/guards except ownership. */
export const alwaysDouble: CubeAiImplementation = {
  winProbability: baselineWinProbability,
  shouldAiAccept: baselineShouldAiAccept,
  shouldAiDouble: (
    _board: Board,
    player: Player,
    cube: { value: number; owner: Player | null },
  ): CubeDecision => {
    if (cube.owner !== null && cube.owner !== player) {
      return { action: "no-double", reasoning: "AI does not own the cube." };
    }

    return {
      action: "double",
      reasoning: "AI doubles whenever it can legally offer.",
    };
  },
};

function nonMonotonicWinProbability(board: Board, player: Player): number {
  const myPip = pipCount(board, player);
  const oppPip = pipCount(board, opponent(player));
  const logistic = 1 / (1 + Math.exp(-0.045 * (oppPip - myPip)));
  const penalty = oppPip >= 55 && oppPip <= 70 ? 0.4 : 0;
  return clampProbability(logistic - penalty);
}

/** Bug: a miscalibrated contact penalty creates a non-monotonic winProbability curve. */
export const nonMonotonic: CubeAiImplementation = {
  winProbability: nonMonotonicWinProbability,
  shouldAiAccept: buildShouldAiAccept(nonMonotonicWinProbability),
  shouldAiDouble: buildBaselineShouldAiDouble(nonMonotonicWinProbability),
};

/** Bug: offer threshold hard-coded to 50% (no too-good cap), ignoring the published double window. */
export const thresholdInconsistent: CubeAiImplementation = {
  winProbability: baselineWinProbability,
  shouldAiAccept: baselineShouldAiAccept,
  shouldAiDouble: (
    board: Board,
    player: Player,
    cube: { value: number; owner: Player | null },
    difficulty: Difficulty,
  ): CubeDecision => {
    if (cube.owner !== null && cube.owner !== player) {
      return { action: "no-double", reasoning: "AI does not own the cube." };
    }

    if (difficulty === "easy") {
      return {
        action: "no-double",
        reasoning: "Easy difficulty never offers doubles.",
      };
    }

    const wp = baselineWinProbability(board, player);
    if (wp >= 0.5) {
      return {
        action: "double",
        reasoning: "AI doubles on any >=50% edge.",
      };
    }

    return {
      action: "no-double",
      reasoning: "AI holds when below its hard-coded 50% threshold.",
    };
  },
};

/** Bug: easy mode incorrectly reuses medium doubling thresholds instead of easy-never behavior. */
export const easyDoubles: CubeAiImplementation = {
  winProbability: baselineWinProbability,
  shouldAiAccept: baselineShouldAiAccept,
  shouldAiDouble: (
    board: Board,
    player: Player,
    cube: { value: number; owner: Player | null },
    difficulty: Difficulty,
  ): CubeDecision => {
    if (cube.owner !== null && cube.owner !== player) {
      return { action: "no-double", reasoning: "AI does not own the cube." };
    }

    const wp = baselineWinProbability(board, player);
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
  },
};

/** Bug: missing the too-good hold check, so runaway positions still trigger an offer. */
export const noHoldTooGood: CubeAiImplementation = {
  winProbability: baselineWinProbability,
  shouldAiAccept: baselineShouldAiAccept,
  shouldAiDouble: (
    board: Board,
    player: Player,
    cube: { value: number; owner: Player | null },
    difficulty: Difficulty,
  ): CubeDecision => {
    if (cube.owner !== null && cube.owner !== player) {
      return { action: "no-double", reasoning: "AI does not own the cube." };
    }

    if (difficulty === "easy") {
      return {
        action: "no-double",
        reasoning: "Easy difficulty never offers doubles.",
      };
    }

    const wp = baselineWinProbability(board, player);
    const lower = difficulty === "hard" ? DOUBLE_WINDOW.hardLower : DOUBLE_WINDOW.mediumLower;

    if (wp >= lower) {
      return {
        action: "double",
        reasoning: "Win probability clears the lower bound, so AI offers.",
      };
    }

    return {
      action: "no-double",
      reasoning: "Win probability is below the published doubling window.",
    };
  },
};
