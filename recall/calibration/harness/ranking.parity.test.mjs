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
function adaptCandidateForTrackedRank(fixtureCase, query, candidate) {
  const queryKeywordWeights = Array.isArray(query?.keyword_weights)
    ? query.keyword_weights
    : [];
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

  const preFreshnessVector = vectorScore;
  const preFreshnessScore = scoreCombined(
    query?.vector,
    queryKeywordWeights,
    vecForCosine(preFreshnessVector),
    docKeywordWeights,
    { gamma: 0.1, delta: 0.15 },
  );
  const floor = toFiniteNumber(fixtureCase?.opts?.floor, 0);
  let droppedByFloor = false;
  let projectedFinal = preFreshnessScore?.final ?? null;

  if (floor > 0 && preFreshnessScore !== null && preFreshnessScore.final < floor) {
    droppedByFloor = true;
  }

  let vectorForRank = preFreshnessVector;
  if (!droppedByFloor && fixtureCase?.opts?.newMemBoost === true && vectorForRank > 0) {
    const grace = toFiniteNumber(fixtureCase?.opts?.grace, 20);
    const boostWindow = toFiniteNumber(fixtureCase?.opts?.boostWindow, 30);
    const newMemMult = toFiniteNumber(fixtureCase?.opts?.newMemMult, 0.5);
    const age = toFiniteNumber(candidate?.age, 0);

    const window = grace + boostWindow;
    const fraction = window > 0
      ? Math.max(0, 1 - age / window)
      : 0;

    const freshnessMultiplier = 1 + newMemMult * fraction;
    vectorForRank = vectorForRank * freshnessMultiplier;
    if (projectedFinal !== null) {
      projectedFinal = projectedFinal * freshnessMultiplier;
    }
  }

  return {
    droppedByLegacyGate,
    droppedByFloor,
    projectedFinal,
    memory: {
      slug: String(candidate?.id ?? '').trim(),
      doc_vector: vecForCosine(vectorForRank),
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

if (fixtureCases.length !== 10) {
  throw new Error(`[parity] expected 10 fixture cases, got ${fixtureCases.length}`);
}

for (const fixtureCase of fixtureCases) {
  test(`[parity] ${fixtureCase.name}`, () => {
    const queryKeywordWeights = toKeywordWeightRows(fixtureCase?.query?.keywordWeights);
    const query = {
      vector: [1, 0],
      keyword_weights: queryKeywordWeights,
    };
    const floorEnabled = toFiniteNumber(fixtureCase?.opts?.floor, 0) > 0;

    const memories = [];
    const memoryBySlug = new Map();
    const projectedFinalBySlug = new Map();
    let legacyGateDrops = 0;
    let floorDrops = 0;
    const totalCandidates = Array.isArray(fixtureCase?.candidates)
      ? fixtureCase.candidates.length
      : 0;

    for (const candidate of fixtureCase.candidates ?? []) {
      const adapted = adaptCandidateForTrackedRank(fixtureCase, query, candidate);
      if (adapted.droppedByLegacyGate) {
        legacyGateDrops += 1;
      }
      if (adapted.droppedByFloor) {
        floorDrops += 1;
        continue;
      }

      memories.push(adapted.memory);
      memoryBySlug.set(adapted.memory.slug, adapted.memory);
      projectedFinalBySlug.set(adapted.memory.slug, adapted.projectedFinal);
    }

    const ranked = rankCase(query, memories, {
      gamma: 0.1,
      delta: 0.15,
    });

    const actualOrder = floorEnabled
      ? ranked.ranked.map((row) => row.slug)
      : ranked.preFloor.map((row) => row.slug);
    assert.deepEqual(
      actualOrder,
      fixtureCase.expected.order,
      `[parity] ${fixtureCase.name}: order mismatch`,
    );

    const actualDropCount = floorEnabled
      ? {
        gate: legacyGateDrops,
        vector: Math.max(0, ranked.dropCount.vector - legacyGateDrops),
        floor: floorDrops,
        kept: ranked.ranked.length,
        total: totalCandidates,
      }
      : {
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

    const rowsForFinalAssertions = floorEnabled ? ranked.ranked : ranked.preFloor;
    for (const row of rowsForFinalAssertions) {
      const expectedFinal = fixtureCase.expected.finals[row.slug];
      assert.notEqual(
        expectedFinal,
        undefined,
        `[parity] ${fixtureCase.name}: expected final missing for ${row.slug}`,
      );

      if (floorEnabled) {
        const projectedFinal = projectedFinalBySlug.get(row.slug);
        assert.notEqual(
          projectedFinal,
          undefined,
          `[parity] ${fixtureCase.name}: projected final missing for ${row.slug}`,
        );
        assertAlmostEqual(
          projectedFinal,
          expectedFinal,
          `[parity] ${fixtureCase.name}: final mismatch for ${row.slug}`,
        );
      } else {
        assertAlmostEqual(
          row.final,
          expectedFinal,
          `[parity] ${fixtureCase.name}: final mismatch for ${row.slug}`,
        );
      }

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

    if (!floorEnabled) {
      assert.deepEqual(
        ranked.ranked.map((row) => row.slug),
        actualOrder,
        `[parity] ${fixtureCase.name}: ranked and preFloor diverged with floor omitted`,
      );
    }
  });
}
