import { describe, expect, it } from "vitest";
import {
  checkerAnimates,
  hintAnimates,
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
