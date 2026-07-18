import { describe, expect, it } from "vitest";
import { pipCount, type Board } from "../../golden/src/game.ts";
import { expectedOfferAction, isNonDecreasing } from "../lib/acceptance.ts";
import {
  alwaysDouble,
  alwaysHold,
  easyDoubles,
  noHoldTooGood,
  nonMonotonic,
  thresholdInconsistent,
} from "../fixtures/ai-negatives.ts";

function emptyPoints(): number[] {
  return new Array(26).fill(0);
}

const bd = (
  points: number[],
  bar = { white: 0, black: 0 },
  off = { white: 0, black: 0 },
): Board => ({ points, bar, off });

describe("[G12] negative controls — the revised grader still rejects broken cube AIs", () => {
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

  it("rejects alwaysHold via internal-offer consistency on a true doubling-window board", () => {
    // Defect: the offer branch was never implemented, so this AI always returns no-double.
    // Catch: expectedOfferAction(itsOwnWp, hard, mayDouble=true) requires "double" on window.
    expect(pipCount(window, "black")).toBe(30);
    expect(pipCount(window, "white")).toBe(61);

    const wp = alwaysHold.winProbability(window, "black");
    expect(
      alwaysHold.shouldAiDouble(window, "black", { value: 1, owner: null }, "hard")
        .action,
    ).not.toBe(expectedOfferAction(wp, "hard", true));
  });

  it("rejects alwaysDouble via internal-offer consistency in too-good positions", () => {
    // Defect: this AI doubles whenever allowed, skipping too-good and threshold checks.
    // Catch: on farAhead (>0.90), policy requires hold; candidate still offers.
    const wp = alwaysDouble.winProbability(farAhead, "black");
    expect(
      alwaysDouble.shouldAiDouble(
        farAhead,
        "black",
        { value: 1, owner: null },
        "hard",
      ).action,
    ).not.toBe(expectedOfferAction(wp, "hard", true));
  });

  it("rejects nonMonotonic via REQ-WINPROB monotonicity", () => {
    // Defect: an incorrect contact penalty causes window-board wp to dip below near-even wp.
    // Catch: the grader's isNonDecreasing check over canonical pip-lead ordering fails.
    expect(
      isNonDecreasing(
        [farBehind, nearEven, window, farAhead].map((b) =>
          nonMonotonic.winProbability(b, "black"),
        ),
      ),
    ).toBe(false);
  });

  it("rejects thresholdInconsistent via internal-offer consistency near parity", () => {
    // Defect: a 50% hard-coded offer threshold ignores published lower/upper window bounds.
    // Catch: near-even wp (~0.5) should hold per policy, but this AI offers.
    const wp = thresholdInconsistent.winProbability(nearEven, "black");
    expect(
      thresholdInconsistent.shouldAiDouble(
        nearEven,
        "black",
        { value: 1, owner: null },
        "hard",
      ).action,
    ).not.toBe(expectedOfferAction(wp, "hard", true));
  });

  it("rejects easyDoubles via internal-offer consistency for easy difficulty", () => {
    // Defect: easy mode incorrectly behaves like medium for cube offers.
    // Catch: policy says easy never offers, but this AI offers on window at easy.
    const wp = easyDoubles.winProbability(window, "black");
    expect(
      easyDoubles.shouldAiDouble(window, "black", { value: 1, owner: null }, "easy")
        .action,
    ).not.toBe(expectedOfferAction(wp, "easy", true));
  });

  it("rejects noHoldTooGood via internal-offer consistency in runaway leads", () => {
    // Defect: too-good hold cap is missing, so high wp still triggers a double.
    // Catch: farAhead wp should map to no-double, but candidate offers.
    const wp = noHoldTooGood.winProbability(farAhead, "black");
    expect(
      noHoldTooGood.shouldAiDouble(
        farAhead,
        "black",
        { value: 1, owner: null },
        "hard",
      ).action,
    ).not.toBe(expectedOfferAction(wp, "hard", true));
  });
});
