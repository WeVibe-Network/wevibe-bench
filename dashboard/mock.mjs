// ─────────────────────────────────────────────────────────────────────────────
// MOCK GENERATOR
//
//   node server.mjs --mock
//
// Emits a realistic board so the UI can be built and demoed without a live run.
// It deliberately produces the UGLY cases, because those are the ones a
// dashboard gets wrong:
//
//   - null fields everywhere early (most of a real run is null)
//   - a control arm with NO episodes at all (correct by construction)
//   - abandoned episodes that never conclude
//   - `unobserved` outcomes (silence is not a vote)
//   - serve_rejected > 0 (delivery failures are real)
//   - transport recoveries and excluded turns (post WO-NUDGE-INF-1)
//   - gates that never flip, and gates never observed at all
//
// HONESTY: mock data is labelled at the source. `board.mock === true` and the
// attestation string is suffixed, so a mock board can never be mistaken for a
// measurement on stream. Never remove that.
//
// The phase advances with wall-clock so the board visibly moves while you are
// designing against it.
// ─────────────────────────────────────────────────────────────────────────────

import { emptyBoard, finalizeDelta, percentiles, median } from "./contract.mjs";

const START = Date.now();

// Deterministic PRNG so a reload does not reshuffle the whole board.
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

const GATE_DEFS = [
  ["G01", "REQ-INIT", "initial position"],
  ["G02", "REQ-PIP", "pip count"],
  ["G03", "REQ-DICE", "dice → moves"],
  ["G04", "REQ-MOVES", "legal-move generation (blocked points + hits)"],
  ["G05", "REQ-HIGHER-DIE", "use higher die"],
  ["G06", "REQ-BAR", "bar re-entry + blocked pass"],
  ["G07", "REQ-HIT", "hitting → bar"],
  ["G08", "REQ-BEAROFF", "bear-off incl overshoot"],
  ["G09", "REQ-BEAROFF-GATE", "blocks bear-off when a checker is outside home"],
  ["G10", "REQ-WINCLASS", "classifies gammon when loser has no off"],
  ["G11", "REQ-CUBE-STATE", "accepted double transfers ownership"],
  ["G12", "REQ-CUBE-AI", "declines below hard take point"],
  ["G13", "REQ-TURN", "alternate turns white → black → white"],
  ["G14", "REQ-AILEGAL", "chooseMoves always returns legal sequences"],
  ["G15", "REQ-COMPLETE", "complete game to winner with fixed dice script"],
  ["F01", "REQ-RENDER", "page loads, no console errors"],
  ["F02", "REQ-RENDER", "start game renders full board"],
  ["F03", "REQ-HINT", "play vs AI (a real move advances state)"],
  ["F04", "REQ-HINT", "legal-move affordance + die attribution"],
  ["F05", "REQ-TURN", "no-legal-move notice"],
  ["F06", "REQ-PIPUI", "pip display cross-checked vs engine"],
  ["F07", "REQ-CUBEUI", "cube UI"],
  ["F08", "REQ-TESTID", "difficulty selector"],
  ["F09", "REQ-HIT", "hit → bar visual"],
  ["F10", "REQ-BAR", "bar re-entry visual"],
  ["F11", "REQ-BEAROFF", "bear-off visual"],
  ["F12", "REQ-NEWGAME", "win state + new game without reload"],
  ["F13", "REQ-COMPACT", "compact / no horizontal overflow"],
  ["F14", "REQ-ANIM", "animation present"],
];

const CONF_DEFS = [
  "state.points", "state.bar", "state.off", "state.turn", "state.phase",
  "state.dice", "state.remainingDice", "state.cube", "state.difficulty",
  "state.score", "state.winner", "state.winType", "state.pointsWon",
  "state.doubleOfferedBy", "state.message", "state.turnOver", "state.pip",
  "state.legalMoves", "state.canDouble", "debug.roll", "dom", "hint", "checker",
].map((loc) => [`C:${loc}`, "REQ-STATE", `/api/state carries "${loc}"`]);

const ALL_GATES = [...GATE_DEFS, ...CONF_DEFS];

const ERRORS = [
  "TypeError: cannot read property 'points' of undefined",
  "AssertionError: expected 15 checkers, received 14",
  "Error: legal move generation returned an illegal sequence",
  "TimeoutError: locator.click: waiting for [data-testid=checker]",
  "SyntaxError: unexpected token in src/game.ts:214",
];

const MEMORIES = [
  {
    cid: "bafyreig7x2m4qk9c21",
    implement:
      "Gate legal-move generation on bar occupancy: if the side to move has any checker on the bar, the ONLY legal moves are re-entry moves into the opponent home board.",
    context:
      "Backgammon engines that generate moves before checking the bar produce sequences the rules forbid, and the failure surfaces far from its cause — usually as an illegal-sequence assertion several turns later.",
    dnd: "Do not compute legal moves before checking the bar.",
    stack: "TypeScript / Node",
  },
  {
    cid: "bafyreiq3n8v1ab41ab",
    implement:
      "When only one die can be played, the rules require playing the HIGHER die. Check both single-die orderings and prefer the larger before emitting the move list.",
    context:
      "This is the single most commonly missed backgammon rule in generated engines; it passes casual play testing and fails conformance.",
    dnd: null,
    stack: "TypeScript / Node",
  },
  {
    cid: "bafyreih5k2p9zz7c04",
    implement:
      "Serve the frontend with explicit data-testid attributes on every checker, point and the cube. Conformance drives the DOM, not the visual layout.",
    context:
      "Playwright conformance checks locate by test id. A visually perfect board with no test ids fails every frontend gate at once.",
    dnd: "Do not rely on nth-child selectors for checkers.",
    stack: "TypeScript / Playwright",
  },
];

export function mockBoard() {
  const r = rng(20260811);
  const board = emptyBoard();
  const elapsed = Math.floor((Date.now() - START) / 1000);

  // Phase advances with wall clock so the board moves while you design.
  const chunk = Math.min(6, 1 + Math.floor(elapsed / 40));
  const triggered = elapsed > 25;

  board.mock = true;
  board.provenance = {
    ...board.provenance,
    attestation: "bench-mock/self-declared · MOCK DATA",
    gate_mode: "auto-approve",
    gate_mode_source: "L4_WEVIBE_RECALL_MODE=test",
    policy_version: "edge-policy-v1",
    policy_anchor_status: "anchor_verified",
    worker_image_fp: "8d1ead2e",
    leader_fp: "f7733d6e",
    corpus: "benchmark",
    seed: 20260709,
  };

  board.run = {
    org_id: "wevibe-org-0",
    model: "qwen3.6-35b-a3b-bench",
    arm: "on",
    cell_label: "cumulative-0006-on",
    started_at: START,
    elapsed_s: elapsed,
    phase: `initial-chunk-${chunk}`,
    chunk: { current: chunk, total: 6 },
    attempt: { current: 2, max: 3 },
    turns: 131,
    session_turns: 139, // > turns: 8 recovered turns excluded from scoring
    tokens: { input: 45091, output: 164629, injected_block: 1615 },
    state: "running",
    session_id: "ses_00e68fc7cffenTINru7aADMvX2",
    log_silent_s: 12,
  };

  // ── the wall ────────────────────────────────────────────────────────────
  // Arm A (memory on) has eroded further than arm B. Some gates are never
  // observed in either arm — that is a real and distinct third state.
  // Deterministic by index, not sampled — a mock whose headline number drifts
  // on every reload is useless for design review. Arm A resolves ~62% of
  // observed gates, arm B ~41%, giving a clear ~20pt delta to design against.
  // (A modulo of the index alone is NOT a spread: `i % 100` never wraps for 52
  // gates, so every gate lands on the same side of the threshold.)
  const spread = (n) => ((n * 2654435761) % 4294967296) / 4294967296;
  const gates = ALL_GATES.map(([id, req, title], i) => {
    const neverObserved = i % 17 === 0;
    const aGreenGate = !neverObserved && spread(i + 1) < 0.62;
    const bGreenGate = !neverObserved && spread(i + 9001) < 0.41;
    return {
      id,
      req,
      title,
      a: neverObserved ? "unobserved" : aGreenGate ? "green" : "red",
      b: neverObserved ? "unobserved" : bGreenGate ? "green" : "red",
      a_flipped_at_attempt: aGreenGate ? 2 : null,
      b_flipped_at_attempt: bGreenGate ? 3 : null,
    };
  });
  const tally = (side) =>
    gates.reduce(
      (acc, g) => ({ ...acc, [g[side]]: acc[g[side]] + 1 }),
      { red: 0, green: 0, unobserved: 0 },
    );
  board.wall = { gates, totals: { a: tally("a"), b: tally("b") } };

  // ── arm delta ───────────────────────────────────────────────────────────
  const aGreen = board.wall.totals.a.green;
  const bGreen = board.wall.totals.b.green;
  const aTotal = gates.length - board.wall.totals.a.unobserved;
  const bTotal = gates.length - board.wall.totals.b.unobserved;
  board.arm_delta = finalizeDelta({
    ...board.arm_delta,
    a: {
      cells: 3,
      gates_resolved: aGreen,
      gates_total: aTotal,
      resolution_rate: aGreen / aTotal,
      median_turns_to_green: 9,
    },
    b: {
      cells: 3,
      gates_resolved: bGreen,
      gates_total: bTotal,
      resolution_rate: bGreen / bTotal,
      median_turns_to_green: 14,
    },
  });

  // ── episodes ────────────────────────────────────────────────────────────
  // Includes an abandoned episode and control-arm rows that never fire.
  const states = ["green", "injected", "recall-fired", "red-again", "red", "abandoned"];
  board.episodes = states.map((state, i) => ({
    failure_key: Math.floor(r() * 0xffffffff).toString(16).padStart(8, "0"),
    arm: "on",
    state,
    turns: 4 + i * 3,
    elapsed_s: 120 + i * 97,
    error: ERRORS[i % ERRORS.length],
    opened_at: Date.now() - (i + 1) * 60000,
  }));

  // ── the recall moment ───────────────────────────────────────────────────
  if (triggered) {
    const chosen = MEMORIES[0];
    board.recall_moment = {
      failure_key: board.episodes[2].failure_key,
      fired_at: Date.now() - 4000,
      error: ERRORS[0],
      latency_ms: 340,
      gate_mode: "auto-approve",
      gate_decision_ms: 12,
      candidates: [
        {
          cid: chosen.cid,
          relevance: 0.81,
          standing_bps: 6400,
          keyword_overlap: 0.66,
          combined_score: 0.78,
          matched_keywords: ["bar-entry", "legal-moves"],
          rank_position: 0,
          disposition: "returned",
        },
        {
          cid: MEMORIES[1].cid,
          relevance: 0.58,
          standing_bps: 3100,
          keyword_overlap: 0.25,
          combined_score: 0.52,
          matched_keywords: ["higher-die"],
          rank_position: 1,
          disposition: "returned",
        },
        {
          cid: "bafyreiz9w4t2mm41ab",
          relevance: 0.38,
          standing_bps: 1200,
          keyword_overlap: 0,
          combined_score: 0.31,
          matched_keywords: [],
          rank_position: -1,
          disposition: "below_floor",
        },
      ],
      guard: { detections: [], scanned: true },
      chosen,
      // pending for a beat, then resolves — including the third state
      outcome: elapsed % 60 < 20 ? null : elapsed % 60 < 45 ? "worked" : "unobserved",
    };
  }

  // ── memory leaderboard ──────────────────────────────────────────────────
  // Sorted by OUTCOMES, never by serves. Self-authored uses are discounted in
  // the backend and that discount is surfaced, not hidden.
  board.memories = MEMORIES.map((m, i) => ({
    ...m,
    serves: [14, 9, 6][i],
    outcomes_resolved: [8, 5, 1][i],
    outcomes_unresolved: [3, 2, 4][i],
    outcomes_unobserved: [3, 2, 1][i],
    standing_bps: [6400, 3100, 900][i],
    self_authored_discounted: [2, 0, 3][i],
  })).sort((a, b) => b.outcomes_resolved - a.outcomes_resolved);

  // ── honesty rail ────────────────────────────────────────────────────────
  const latencies = [180, 210, 240, 290, 340, 380, 410, 520, 690, 1180];
  board.honesty = {
    coverage: {
      concluded: 11,
      total: 18,
      note: "uncovered episodes count as neither positive nor negative",
    },
    unresolved: 4,
    guard_detections: { secret_pattern: 1, prompt_injection: 2 },
    recall_latency_ms: percentiles(latencies),
    wasted_turns: 7, // burned before the gated trigger could fire
    recovered_turns: 8, // guard + finalize kills, excluded from scoring
    serves: {
      sent: 29,
      rejected: 2,
      confirmed_on_chain: 27,
      note: "delivery, not outcome",
    },
    transport: {
      truncations: 4,
      finalize_timeouts: 3,
      finalize_timeout_turns: 8,
      guard_aborts: 0,
      recovery_nudges: 5,
      recoveries: 5,
    },
  };

  // ── history ─────────────────────────────────────────────────────────────
  board.history = [
    { cell: "cumulative-0000-off", arm: "off", d: 5363, res: 3, v: "FAIL", t: [] },
    { cell: "cumulative-0001-on", arm: "on", d: 4870, res: 9, v: "FAIL", t: [1200, 2600, 3900] },
    { cell: "cumulative-0002-off", arm: "off", d: 5102, res: 4, v: "FAIL", t: [] },
    { cell: "cumulative-0003-on", arm: "on", d: 4210, res: 14, v: "PASS", t: [900, 2100, 3300, 3950] },
    { cell: "cumulative-0004-off", arm: "off", d: 5480, res: 5, v: "FAIL", t: [] },
    { cell: "cumulative-0005-on", arm: "on", d: 3980, res: 16, v: "PASS", t: [800, 1900, 3100] },
  ].map((h, i) => ({
    cell_label: h.cell,
    arm: h.arm,
    started_at: START - (7 - i) * 5400000,
    duration_s: h.d,
    segments: [
      { kind: "build", from_s: 0, to_s: Math.round(h.d * 0.62) },
      { kind: "error", from_s: Math.round(h.d * 0.62), to_s: h.d },
    ],
    triggers: h.t,
    gates_resolved: h.res,
    verdict: h.v,
  }));

  board.sources = [
    { id: "run-manifest", ok: true, fields: ["provenance"], reason: null, provenance: { path: "mock://manifest.json", mtime: Date.now() }, ms: 1 },
    { id: "status-stream", ok: true, fields: ["wall", "arm_delta"], reason: null, provenance: { path: "mock://manifest.status.jsonl", mtime: Date.now() }, ms: 2 },
    { id: "run-log", ok: true, fields: ["run.phase"], reason: null, provenance: { path: "mock://off-cell.log", mtime: Date.now() }, ms: 1 },
    { id: "truncation", ok: true, fields: ["honesty.transport"], reason: null, provenance: null, ms: 1 },
    { id: "funnel-cells", ok: true, fields: ["honesty.serves"], reason: null, provenance: null, ms: 1 },
    { id: "plugin-log", ok: true, fields: ["honesty.recall_latency_ms"], reason: null, provenance: null, ms: 1 },
    { id: "opencode-serve", ok: true, fields: ["run.tokens"], reason: null, provenance: null, ms: 3 },
    { id: "hub-db", ok: false, fields: ["recall_moment.candidates"], reason: "disabled by default — enable hubDb in dashboard.config.json", provenance: null, ms: 0 },
  ];

  board.generated_at = Date.now();
  return board;
}
