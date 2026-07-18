import { describe, expect, it } from "vitest";
import {
  checkerAnimates,
  expectedOfferAction,
  hintAnimates,
  isNonDecreasing,
  withinParityBand,
} from "../lib/acceptance.ts";

describe("acceptance predicates", () => {
  it("parity band matches ±0.01 contract exactly", () => {
    expect(withinParityBand(0.5)).toBe(true);
    expect(withinParityBand(0.509)).toBe(true);
    expect(withinParityBand(0.491)).toBe(true);
    expect(withinParityBand(0.512)).toBe(false);
    expect(withinParityBand(0.488)).toBe(false);
  });

  it("parity band allows values that the old toBeCloseTo(0.5, 2) check would reject", () => {
    expect(withinParityBand(0.508)).toBe(true);
  });

  it("expectedOfferAction follows the published REQ-CUBE-AI offer policy", () => {
    // inside the hard window => offer
    expect(expectedOfferAction(0.8, "hard", true)).toBe("double");
    // inside the medium window => offer
    expect(expectedOfferAction(0.8, "medium", true)).toBe("double");
    // below the hard lower bound => hold
    expect(expectedOfferAction(0.6, "hard", true)).toBe("no-double");
    // above 0.90 => too good, hold
    expect(expectedOfferAction(0.95, "hard", true)).toBe("no-double");
    // easy never offers, regardless of win probability
    expect(expectedOfferAction(0.8, "easy", true)).toBe("no-double");
    // cannot double (opponent owns cube) => hold
    expect(expectedOfferAction(0.8, "hard", false)).toBe("no-double");
    // boundary: exactly at the upper ceiling is still inside the window
    expect(expectedOfferAction(0.9, "hard", true)).toBe("double");
  });

  it("isNonDecreasing accepts monotone series and rejects a dip", () => {
    expect(isNonDecreasing([0.02, 0.5, 0.8, 0.98])).toBe(true);
    expect(isNonDecreasing([0.5, 0.5, 0.5])).toBe(true);
    expect(isNonDecreasing([0.02, 0.5, 0.4, 0.98])).toBe(false);
  });

  it("checker animation predicate accepts transition-driven checker motion", () => {
    expect(
      checkerAnimates({
        transitionProperty: "transform",
        transitionDuration: "0.34s",
        animationName: "none",
        animationDuration: "0s",
      }),
    ).toBe(true);
  });

  it("checker animation predicate accepts keyframe-driven checker motion", () => {
    expect(
      checkerAnimates({
        transitionProperty: "all",
        transitionDuration: "0s",
        animationName: "pop",
        animationDuration: "0.5s",
      }),
    ).toBe(true);
  });

  it("checker animation predicate rejects no-op/default motion", () => {
    expect(
      checkerAnimates({
        transitionProperty: "all",
        transitionDuration: "0s",
        animationName: "none",
        animationDuration: "0s",
      }),
    ).toBe(false);
  });

  it("hint animation predicate accepts active hint animation", () => {
    expect(
      hintAnimates({
        animationName: "pulse",
        animationDuration: "1.1s",
      }),
    ).toBe(true);
  });

  it("hint animation predicate rejects no-op hint animation", () => {
    expect(
      hintAnimates({
        animationName: "none",
        animationDuration: "0s",
      }),
    ).toBe(false);
  });
});
