// ─────────────────────────────────────────────────────────────────────────────
// UI RUNTIME — Preact + htm, vendored, no build step
//
// ── WHY A COMPONENT TREE AT ALL ─────────────────────────────────────────────
//
// The board was pure string concatenation morphed into the DOM by dom.js. That
// worked, and it was measured: 598 tags, 16.5KB per render, cheap. The DOM was
// never the bottleneck and this is NOT a performance change.
//
// It exists for ONE reason that string rendering cannot serve: MOTION.
//
// The design system ships sixteen motion tokens — --type-char-min/max, the
// typed-not-faded cadence, --cursor-blink, --trail-hold/fade/life,
// --sweep-step, @keyframes wv-blink / wv-trail. NONE of them were implemented,
// and the README recorded the deviation plainly: "No ambient animation."
//
// The reason is structural, not laziness. Enter/exit choreography needs to know
// that a row is NEW, that a row is LEAVING, and it needs a stable handle on the
// node in between. A string that is diffed into the DOM has none of those: a
// new ledger row simply appears, and there is no unmount to animate away from.
// Keys, refs and effects are the machinery that makes the spec's motion
// expressible. That is the whole justification.
//
// ── WHY PREACT AND NOT REACT ────────────────────────────────────────────────
//
// 36KB vendored, versus a React + react-dom + build toolchain. The three
// properties this artifact states as SAFETY, not preference, all survive:
//
//   · renders offline — no CDN, no network fetch, ever
//   · no `npm install` in the image — there is still no package.json
//   · no build step — these are published ESM builds, copied verbatim
//
// A Vite + React setup was the considered alternative. It buys JSX and
// type-checking, and the product dashboard (wevibe-server/wevibe-dashboard) is
// already React 18 so components could in principle be shared. Both were
// rejected: sharing components between an operator INSTRUMENT and the product
// UI is coupling that would be regretted, and every chart here is hand-drawn
// SVG per the design spec — the React chart ecosystem is exactly what must not
// be pulled in. `preact/compat` remains the escape hatch if that judgement ever
// turns out to be wrong; it is an import swap, not a rewrite.
//
// ── THE KNOWN WEAKNESS, STATED ──────────────────────────────────────────────
//
// htm has NO COMPILE STEP, therefore no compile-time error for a typo'd
// component name or a misspelled prop — the failure is silent at runtime, which
// is the same class of defect that let 78 CSS classes go missing while every
// check stayed green. The mitigation is the pattern this repo already invented:
// style-coverage.test.mjs pins emitted classes against the stylesheet, and
// component-coverage does the same for component names and props.
// ─────────────────────────────────────────────────────────────────────────────

import { h, render as preactRender, Fragment } from "preact";
import { useState, useEffect, useRef, useMemo, useCallback, useLayoutEffect } from "preact/hooks";
import htmModule from "htm";

/** The tagged-template compiler, bound to Preact's createElement. */
export const html = htmModule.bind(h);

export { h, Fragment, preactRender as render };
export { useState, useEffect, useRef, useMemo, useCallback, useLayoutEffect };

// ── MOTION ───────────────────────────────────────────────────────────────────
//
// The design system's motion tokens, read from CSS rather than duplicated here.
// Duplicating them in JS would create a second source of truth for a value the
// stylesheet already owns, and the two would drift — the exact failure mode
// that produced a board emitting classes no stylesheet defined.

/** Read a CSS custom property as a number of milliseconds. */
export function motionToken(name, fallback) {
  if (typeof getComputedStyle === "undefined") return fallback;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return fallback;
  const n = Number.parseFloat(raw);
  if (!Number.isFinite(n)) return fallback;
  return raw.endsWith("ms") ? n : raw.endsWith("s") ? n * 1000 : n;
}

/**
 * Does the operator want motion at all?
 *
 * Honoured everywhere, per the design system's own reduced-motion stanza and
 * the board's existing rule. A caller that skips this check is a bug.
 */
export function motionAllowed() {
  if (typeof matchMedia === "undefined") return false;
  return !matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * THE TYPED REVEAL — the design system's signature cadence.
 *
 * "Nothing fades in. Everything is TYPED, as if a developer is at the keyboard
 * right now. Cadence is human, not metronomic." (tokens/motion.css)
 *
 * The cadence is deliberately NOT uniform: per-character delay is randomised
 * between --type-char-min and --type-char-max, spaces run slower
 * (--type-space-mult), punctuation lingers (--type-punct-mult), and there is a
 * --type-think-chance probability of a longer pause. A metronomic reveal reads
 * as a machine; this reads as a person.
 *
 * Returns the substring visible so far. Under reduced motion it returns the
 * full text immediately — the information is never withheld for the sake of an
 * effect.
 */
export function useTyped(text, { enabled = true } = {}) {
  const full = String(text ?? "");
  const [shown, setShown] = useState(enabled && motionAllowed() ? "" : full);

  useEffect(() => {
    if (!enabled || !motionAllowed()) {
      setShown(full);
      return undefined;
    }
    const min = motionToken("--type-char-min", 7);
    const max = motionToken("--type-char-max", 18);
    const spaceMult = motionToken("--type-space-mult", 1.2);
    const punctMult = motionToken("--type-punct-mult", 1.3);
    const thinkChance = motionToken("--type-think-chance", 0.02);

    let i = 0;
    let timer = null;
    let cancelled = false;
    setShown("");

    const step = () => {
      if (cancelled) return;
      i += 1;
      setShown(full.slice(0, i));
      if (i >= full.length) return;
      const ch = full[i] ?? "";
      let d = min + Math.random() * (max - min);
      if (ch === " ") d *= spaceMult;
      else if (".,:;!?—·/".includes(ch)) d *= punctMult;
      if (Math.random() < thinkChance) d += 40 + Math.random() * 70;
      timer = setTimeout(step, d);
    };
    timer = setTimeout(step, 0);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [full, enabled]);

  return shown;
}

/**
 * Was this value just added? Drives the one-shot arrival mark.
 *
 * Returns true for `ms` after `key` first becomes non-null, then false. This is
 * the hook that a string renderer could not provide: it needs to remember what
 * it saw last render, which is precisely what component identity buys.
 */
export function useArrival(key, ms = 1000) {
  const [fresh, setFresh] = useState(false);
  const seen = useRef(null);

  useEffect(() => {
    if (key === null || key === undefined) return undefined;
    if (seen.current === key) return undefined;
    const first = seen.current === null;
    seen.current = key;
    // The FIRST value is not an arrival — it was already there when the board
    // loaded. Flashing every row on first paint would make a static board look
    // like a busy one.
    if (first || !motionAllowed()) return undefined;
    setFresh(true);
    const t = setTimeout(() => setFresh(false), ms);
    return () => clearTimeout(t);
  }, [key, ms]);

  return fresh;
}
