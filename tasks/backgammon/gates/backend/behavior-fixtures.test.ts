import { describe, expect, it } from "vitest";
import { pipCount, type Board } from "../../golden/src/game.ts";
import {
  shouldAiAccept as shouldAiAcceptValid,
  shouldAiDouble as shouldAiDoubleValid,
  winProbability as winProbabilityValid,
} from "../fixtures/ai-valid-tanh.ts";
import {
  shouldAiAccept as shouldAiAcceptInvalid,
  winProbability as winProbabilityInvalid,
} from "../fixtures/ai-invalid-const.ts";
import { withinParityBand } from "../lib/acceptance.ts";

function emptyPoints(): number[] {
  return new Array(26).fill(0);
}

const bd = (
  points: number[],
  bar = { white: 0, black: 0 },
  off = { white: 0, black: 0 },
): Board => ({ points, bar, off });

describe("G12 behavior fixtures", () => {
  const nearEvenPoints = emptyPoints();
  nearEvenPoints[6] = 5;
  nearEvenPoints[19] = -5;
  const nearEven = bd(nearEvenPoints);

  const farBehindPoints = emptyPoints();
  farBehindPoints[2] = 1;
  const farBehind = bd(farBehindPoints, { white: 0, black: 14 });

  const farAheadPoints = emptyPoints();
  farAheadPoints[24] = -1;
  const farAhead = bd(farAheadPoints, { white: 14, black: 0 });

  const windowPoints = emptyPoints();
  windowPoints[19] = -5;
  windowPoints[6] = 10;
  windowPoints[1] = 1;
  const window = bd(windowPoints);

  it("builds the same canonical G12 boards", () => {
    expect({
      nearEven: {
        black: pipCount(nearEven, "black"),
        white: pipCount(nearEven, "white"),
      },
      farBehind: {
        black: pipCount(farBehind, "black"),
        white: pipCount(farBehind, "white"),
      },
      farAhead: {
        black: pipCount(farAhead, "black"),
        white: pipCount(farAhead, "white"),
      },
      window: {
        black: pipCount(window, "black"),
        white: pipCount(window, "white"),
      },
    }).toEqual({
      nearEven: { black: 30, white: 30 },
      farBehind: { black: 350, white: 2 },
      farAhead: { black: 1, white: 350 },
      window: { black: 30, white: 61 },
    });
  });

  it("accepts a materially different valid AI implementation", () => {
    expect(withinParityBand(winProbabilityValid(nearEven, "black"))).toBe(true);
    expect(winProbabilityValid(farBehind, "black")).toBeLessThan(0.24);
    expect(winProbabilityValid(farAhead, "black")).toBeGreaterThan(0.9);

    expect(shouldAiAcceptValid(nearEven, "black", "hard").action).toBe("double");
    expect(shouldAiAcceptValid(farBehind, "black", "hard").action).toBe(
      "no-double",
    );

    expect(
      shouldAiDoubleValid(window, "black", { value: 1, owner: null }, "hard")
        .action,
    ).toBe("double");
    expect(
      shouldAiDoubleValid(window, "black", { value: 1, owner: null }, "easy")
        .action,
    ).toBe("no-double");
  });

  it("rejects invalid constant win-probability behavior", () => {
    expect(winProbabilityInvalid(farBehind, "black") < 0.24).toBe(false);
    expect(shouldAiAcceptInvalid(farBehind, "black", "hard").action).not.toBe(
      "no-double",
    );
  });
});
