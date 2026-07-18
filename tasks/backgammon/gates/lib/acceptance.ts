export const PARITY_TOLERANCE = 0.01;

export function withinParityBand(wp: number): boolean {
  return Math.abs(wp - 0.5) <= PARITY_TOLERANCE;
}

export function anyDurationNonZero(d: string): boolean {
  return d.split(",").some((segment) => Number.parseFloat(segment) > 0);
}

export function checkerAnimates(s: {
  transitionProperty: string;
  transitionDuration: string;
  animationName: string;
  animationDuration: string;
}): boolean {
  return (
    (/transform|all/.test(s.transitionProperty) &&
      anyDurationNonZero(s.transitionDuration)) ||
    (s.animationName !== "none" && anyDurationNonZero(s.animationDuration))
  );
}

export function hintAnimates(s: {
  animationName: string;
  animationDuration: string;
}): boolean {
  return (
    s.animationName !== "none" && anyDurationNonZero(s.animationDuration)
  );
}

/** Published REQ-CUBE-AI take points (winProbability >= take point ⇒ accept a double). */
export const TAKE_POINT = { easy: 0.32, medium: 0.27, hard: 0.24 } as const;

/** Published REQ-CUBE-AI offer window bounds (lower per difficulty; shared upper "too-good" ceiling 0.90). */
export const DOUBLE_WINDOW = { mediumLower: 0.72, hardLower: 0.68, upper: 0.9 } as const;

export type Difficulty = "easy" | "medium" | "hard";

/**
 * The published REQ-CUBE-AI OFFER policy as a pure function of the candidate's OWN
 * win-probability estimate `wp`. Formula-agnostic: it consumes whatever monotonic wp the
 * candidate produced, then applies the published window/too-good/easy-never/ownership rules.
 * A conforming AI's shouldAiDouble(...).action MUST equal expectedOfferAction(itsOwnWp, ...).
 *   - not allowed to double (opponent owns cube)      -> "no-double"
 *   - easy difficulty                                  -> "no-double" (easy never offers)
 *   - wp > 0.90 (too good, play on for gammon)         -> "no-double"
 *   - lower <= wp <= 0.90 (inside the offer window)    -> "double"
 *   - wp < lower (not strong enough)                   -> "no-double"
 */
export function expectedOfferAction(
  wp: number,
  difficulty: Difficulty,
  mayDouble: boolean,
): "double" | "no-double" {
  if (!mayDouble) return "no-double";
  if (difficulty === "easy") return "no-double";
  const lower = difficulty === "hard" ? DOUBLE_WINDOW.hardLower : DOUBLE_WINDOW.mediumLower;
  if (wp > DOUBLE_WINDOW.upper) return "no-double";
  if (wp >= lower && wp <= DOUBLE_WINDOW.upper) return "double";
  return "no-double";
}

/** REQ-WINPROB: true iff `values` (win-probabilities ordered by INCREASING pip lead) are
 *  monotonically non-decreasing (each >= the previous within a tiny float tolerance). */
export function isNonDecreasing(values: readonly number[]): boolean {
  for (let i = 1; i < values.length; i++) {
    if (values[i] < values[i - 1] - 1e-9) return false;
  }
  return true;
}
