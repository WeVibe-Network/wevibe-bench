import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { rankCase, scoreCombined } from './ranking.mjs';

const EPSILON = 1e-9;
const HARNESS_DIR = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.resolve(HARNESS_DIR, '../../../../');
const FIXTURE_PATH = path.resolve(
  WORKSPACE_ROOT,
  'wevibe-protocol/test-vectors/recall-ranking-parity.json',
);

function toFiniteNumber(value, fallback = 0) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function vecForCosine(score) {
  const s = toFiniteNumber(score, 0);
  return [s, Math.sqrt(Math.max(0, 1 - s * s))];
}

function toKeywordWeightRows(input) {
  if (Array.isArray(input)) {
    return input
      .map((entry) => {
        if (!entry || typeof entry !== 'object') {
          return null;
        }
        return {
          keyword: String(entry.keyword ?? '').trim(),
          weight: toFiniteNumber(entry.weight, 0),
        };
      })
      .filter((entry) => entry && entry.keyword.length > 0);
  }

  if (!input || typeof input !== 'object') {
    return [];
  }

  return Object.entries(input)
    .map(([keyword, weight]) => ({
      keyword: String(keyword ?? '').trim(),
      weight: toFiniteNumber(weight, 0),
    }))
    .filter((entry) => entry.keyword.length > 0);
}

function keywordSet(keywordWeights) {
  const out = new Set();

  for (const row of keywordWeights) {
    const keyword = String(row?.keyword ?? '').trim().toLowerCase();
    if (!keyword) {
      continue;
    }
    out.add(keyword);
  }

  return out;
}

function hasKeywordOverlap(queryKeywordWeights, docKeywordWeights) {
  const querySet = keywordSet(queryKeywordWeights);
  if (querySet.size === 0) {
    return false;
  }

  for (const keyword of keywordSet(docKeywordWeights)) {
    if (querySet.has(keyword)) {
      return true;
    }
  }

  return false;
}

/**
 * Fixture adapter:
 * - keeps the tracked harness formula frozen (`rankCase` + `scoreCombined`), and
 * - projects legacy parity-fixture knobs (gate/denials/newMem) into effective vectors
 *   before feeding tracked inputs.
 */
function adaptCandidateForTrackedRank(fixtureCase, queryKeywordWeights, candidate) {
  const docKeywordWeights = toKeywordWeightRows(candidate.keywordWeights);
  let vectorScore = toFiniteNumber(candidate.vectorScore, 0);
  let droppedByLegacyGate = false;

  const gateEnabled = fixtureCase?.opts?.gate === true;
  if (gateEnabled && queryKeywordWeights.length > 0 && !hasKeywordOverlap(queryKeywordWeights, docKeywordWeights)) {
    if (vectorScore > 0) {
      vectorScore = 0;
      droppedByLegacyGate = true;
    }
  }

  const pendingDenials = toFiniteNumber(candidate.pendingDenials, 0);
  if (pendingDenials > 0) {
    vectorScore = Math.max(0, vectorScore - pendingDenials * 0.05);
  }

  if (fixtureCase?.opts?.newMemBoost === true && vectorScore > 0) {
    const grace = toFiniteNumber(fixtureCase?.opts?.grace, 20);
    const boostWindow = toFiniteNumber(fixtureCase?.opts?.boostWindow, 30);
    const newMemMult = toFiniteNumber(fixtureCase?.opts?.newMemMult, 0.5);
    const age = toFiniteNumber(candidate?.age, 0);

    const window = grace + boostWindow;
    const fraction = window > 0
      ? Math.max(0, 1 - age / window)
      : 0;

    vectorScore = vectorScore * (1 + newMemMult * fraction);
  }

  return {
    droppedByLegacyGate,
    memory: {
      slug: String(candidate?.id ?? '').trim(),
      doc_vector: vecForCosine(vectorScore),
      keyword_weights: docKeywordWeights,
    },
  };
}

function assertAlmostEqual(actual, expected, label) {
  const delta = Math.abs(actual - expected);
  assert.ok(delta < EPSILON, `${label} expected=${expected} actual=${actual} |Δ|=${delta}`);
}

if (!fs.existsSync(FIXTURE_PATH)) {
  throw new Error(`[parity] fixture missing: ${FIXTURE_PATH}`);
}

const fixtureRaw = fs.readFileSync(FIXTURE_PATH, 'utf8');
const parity = JSON.parse(fixtureRaw);
const fixtureCases = Array.isArray(parity?.cases) ? parity.cases : [];

if (fixtureCases.length !== 9) {
  throw new Error(`[parity] expected 9 fixture cases, got ${fixtureCases.length}`);
}

for (const fixtureCase of fixtureCases) {
  test(`[parity] ${fixtureCase.name}`, () => {
    const queryKeywordWeights = toKeywordWeightRows(fixtureCase?.query?.keywordWeights);
    const query = {
      vector: [1, 0],
      keyword_weights: queryKeywordWeights,
    };

    const memories = [];
    const memoryBySlug = new Map();
    let legacyGateDrops = 0;

    for (const candidate of fixtureCase.candidates ?? []) {
      const adapted = adaptCandidateForTrackedRank(fixtureCase, queryKeywordWeights, candidate);
      memories.push(adapted.memory);
      memoryBySlug.set(adapted.memory.slug, adapted.memory);
      if (adapted.droppedByLegacyGate) {
        legacyGateDrops += 1;
      }
    }

    const ranked = rankCase(query, memories, {
      gamma: 0.1,
      delta: 0.15,
    });

    const actualOrder = ranked.preFloor.map((row) => row.slug);
    assert.deepEqual(
      actualOrder,
      fixtureCase.expected.order,
      `[parity] ${fixtureCase.name}: order mismatch`,
    );

    const actualDropCount = {
      gate: legacyGateDrops,
      vector: Math.max(0, ranked.dropCount.vector - legacyGateDrops),
      kept: ranked.preFloor.length,
      total: memories.length,
    };

    assert.deepEqual(
      actualDropCount,
      fixtureCase.expected.dropCount,
      `[parity] ${fixtureCase.name}: dropCount mismatch`,
    );

    for (const row of ranked.preFloor) {
      const expectedFinal = fixtureCase.expected.finals[row.slug];
      assert.notEqual(
        expectedFinal,
        undefined,
        `[parity] ${fixtureCase.name}: expected final missing for ${row.slug}`,
      );

      assertAlmostEqual(
        row.final,
        expectedFinal,
        `[parity] ${fixtureCase.name}: final mismatch for ${row.slug}`,
      );

      const adaptedMemory = memoryBySlug.get(row.slug);
      const rescored = scoreCombined(
        query.vector,
        query.keyword_weights,
        adaptedMemory.doc_vector,
        adaptedMemory.keyword_weights,
        { gamma: 0.1, delta: 0.15 },
      );

      assert.notEqual(
        rescored,
        null,
        `[parity] ${fixtureCase.name}: scoreCombined unexpectedly dropped ${row.slug}`,
      );

      assertAlmostEqual(
        rescored.final,
        row.final,
        `[parity] ${fixtureCase.name}: scoreCombined/rankCase divergence for ${row.slug}`,
      );
    }

    assert.deepEqual(
      ranked.ranked.map((row) => row.slug),
      actualOrder,
      `[parity] ${fixtureCase.name}: ranked and preFloor diverged with floor omitted`,
    );
  });
}
