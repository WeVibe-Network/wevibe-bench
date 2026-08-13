// ─────────────────────────────────────────────────────────────────────────────
// LIVE SURFACE — serve the live lane's artifact to the board
//
// WHY THIS IS A SEPARATE FILE FROM live-lane.mjs
//
// The lane WRITES (it clones, spawns vitest, and publishes). The control plane
// is READ-ONLY for this feature by construction — it must never spawn the lane,
// because a control plane that can start a test run beside a graded attempt is
// a control plane that can void a measurement. So the lane is a standalone
// process the operator starts, and this module only ever READS what it left on
// disk.
//
// ── WHY THE LANE'S RESULT IS NOT MERGED INTO /api/wall ───────────────────────
//
// RC-5 names ONE scored source of truth. The lane's numbers are PROVISIONAL:
// measured against a snapshot, with 16 gates it will never run, at a moment the
// agent may be mid-thought. Folding them into the wall's `state` would make the
// scored grid disagree with `manifest.status.jsonl`, which is the precise
// failure this board was rebuilt to eliminate.
//
// They are therefore published on their OWN endpoint and rendered as an overlay
// the operator can always tell apart from a graded result.
//
// STALENESS IS A FIRST-CLASS FACT. A lane artifact whose mtime is old describes
// a measurement that may no longer hold; `age_s` is published so the board can
// say so rather than presenting a stale grid as current.
//
// READ-ONLY: reads one JSON file. Never writes, never spawns, never signals.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { join } from "node:path";

/** The contract version the board can assert against. */
export const LIVE_SURFACE_CONTRACT_VERSION = 1;

/** The artifact the lane publishes, relative to the runs root. */
export const LIVE_ARTIFACT = "live-gates.json";

/**
 * Beyond this age the lane's result is reported as stale.
 *
 * The lane's own cycle is ~2.3s of work behind a debounce; a healthy lane
 * republishes well inside 30s even when nothing changes. A gap larger than this
 * means the lane stopped, and a stopped lane's last grid must not be presented
 * as live.
 */
export const LIVE_STALE_AFTER_S = 30;

/**
 * Read the live lane's published surface.
 *
 * NEVER 500s AND NEVER FABRICATES. No artifact is the NORMAL state — the lane
 * is optional and most runs will not have one — so it returns ok:true with
 * `running:false` and a stated reason, exactly like every other unwired surface
 * on this control plane. An empty grid with no explanation is what made the
 * gate wall look broken during grading, and this must not repeat it.
 */
export async function readLive({ runsRoot, now = Date.now() }) {
  const path = join(runsRoot, LIVE_ARTIFACT);

  let raw;
  let mtime;
  try {
    const st = await fs.stat(path);
    if (!st.isFile()) throw new Error("not a file");
    mtime = st.mtimeMs;
    raw = await fs.readFile(path, "utf8");
  } catch {
    return {
      ok: true,
      contract_version: LIVE_SURFACE_CONTRACT_VERSION,
      running: false,
      unwired: ["live-lane"],
      unwired_reasons: {
        "live-lane":
          `no ${LIVE_ARTIFACT} under the runs root — the live lane is not running. ` +
          "This is the normal state: the lane is an optional, provisional instrument " +
          "and the authoritative gate wall is unaffected by its absence.",
      },
      lane: null,
      build: null,
    };
  }

  let doc;
  try {
    doc = JSON.parse(raw);
  } catch {
    // A half-written artifact is a real transient: the lane writes atomically,
    // but a reader must still never crash on a torn file.
    return {
      ok: true,
      contract_version: LIVE_SURFACE_CONTRACT_VERSION,
      running: false,
      unwired: ["live-lane"],
      unwired_reasons: {
        "live-lane": `${LIVE_ARTIFACT} could not be parsed — most likely read mid-write; it will resolve on the next poll`,
      },
      lane: null,
      build: null,
    };
  }

  const ageS = Math.max(0, Math.round((now - mtime) / 1000));
  const stale = ageS >= LIVE_STALE_AFTER_S;

  return {
    ok: true,
    contract_version: LIVE_SURFACE_CONTRACT_VERSION,
    // The lane is considered RUNNING while its artifact is fresh. Derived from
    // mtime — an absolute epoch — never from a timestamp inside the document,
    // for the same timezone reason recorded in gate-events.mjs.
    running: !stale,
    age_s: ageS,
    stale,
    stale_after_s: LIVE_STALE_AFTER_S,
    stale_reason: stale
      ? `the live lane has not republished for ${ageS}s — it has most likely stopped, and this grid is a past measurement, not a current one`
      : null,
    lane: {
      provisional: true,
      lane: doc.lane ?? null,
      run_dir: doc.run_dir ?? null,
      snapshot: doc.snapshot ?? null,
      ran_at: doc.ran_at ?? null,
      duration_ms: doc.duration_ms ?? null,
      gates: Array.isArray(doc.gates) ? doc.gates : [],
      counts: doc.counts ?? null,
    },
    // The build axis travels with the lane because the lane is what measures it
    // (both come from the same snapshot, so they always describe the same
    // instant). Splitting them across two endpoints would let the board render
    // a build state and a gate state from two different snapshots.
    build: doc.build ?? null,
    unwired: [],
    unwired_reasons: {},
  };
}
