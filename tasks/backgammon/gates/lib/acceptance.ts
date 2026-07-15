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
