import {
  meanSeparation,
  mrr,
  ndcgAtK,
  precisionAtK,
  recallAtK,
  recallBinaryHitAtK,
  zeroInjectionRate,
} from './metrics.mjs';
import { kneeCandidates } from './diagnostics.mjs';
import { rankCase } from './ranking.mjs';

const FROZEN_CONSTANTS = Object.freeze({
  gamma: 0.1,
  delta: 0.15,
  contested_threshold: 0.2,
});

const FLOOR_CONFIG = Object.freeze({
  count: 19,
  min: 0,
  max: 0.9,
  step: 0.05,
});

const DENOMINATORS = Object.freeze({
  positive: 16,
  expected_empty: 7,
  total: 23,
});

const DEFAULT_METHOD =
  'cosine-floor sweep over real nomic-768/card pipeline; combined-score gate; delta=0.15 gamma=0.1 frozen';

function round6(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? Math.round(numeric * 1_000_000) / 1_000_000
    : null;
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function validateDataset(dataset) {
  assert(dataset && typeof dataset === 'object', 'runSweep: dataset must be an object');
  assert(Array.isArray(dataset.memories), 'runSweep: dataset.memories must be an array');
  assert(Array.isArray(dataset.cases), 'runSweep: dataset.cases must be an array');
}

function buildFloorGrid() {
  const floors = [];

  for (let i = 0; i < FLOOR_CONFIG.count; i += 1) {
    floors.push(Number((FLOOR_CONFIG.min + i * FLOOR_CONFIG.step).toFixed(2)));
  }

  assert(
    floors.length === FLOOR_CONFIG.count,
    `runSweep: floor count mismatch: expected ${FLOOR_CONFIG.count}, got ${floors.length}`,
  );
  assert(
    floors[0] === Number(FLOOR_CONFIG.min.toFixed(2)),
    `runSweep: floor first mismatch: expected ${FLOOR_CONFIG.min.toFixed(2)}, got ${floors[0]}`,
  );
  assert(
    floors[floors.length - 1] === Number(FLOOR_CONFIG.max.toFixed(2)),
    `runSweep: floor last mismatch: expected ${FLOOR_CONFIG.max.toFixed(2)}, got ${floors[floors.length - 1]}`,
  );

  for (let i = 0; i < floors.length - 1; i += 1) {
    const step = Number((floors[i + 1] - floors[i]).toFixed(2));
    assert(
      step === Number(FLOOR_CONFIG.step.toFixed(2)),
      `runSweep: floor step mismatch at index ${i}: expected ${FLOOR_CONFIG.step.toFixed(2)}, got ${step}`,
    );
  }

  return floors;
}

function validateDenominators(cases) {
  const positive = cases.filter((entry) => entry?.expect_injection === true).length;
  const expectedEmpty = cases.filter((entry) => entry?.expect_injection === false).length;
  const total = cases.length;

  assert(
    positive === DENOMINATORS.positive,
    `runSweep: positive denominator mismatch: expected ${DENOMINATORS.positive}, got ${positive}`,
  );
  assert(
    expectedEmpty === DENOMINATORS.expected_empty,
    `runSweep: expected-empty denominator mismatch: expected ${DENOMINATORS.expected_empty}, got ${expectedEmpty}`,
  );
  assert(
    total === DENOMINATORS.total,
    `runSweep: total denominator mismatch: expected ${DENOMINATORS.total}, got ${total}`,
  );
}

function rankOne(caseDef, memories, floor) {
  return rankCase(
    {
      vector: caseDef.query_vector,
      keyword_weights: caseDef.query_keyword_weights,
    },
    memories,
    {
      gamma: FROZEN_CONSTANTS.gamma,
      delta: FROZEN_CONSTANTS.delta,
      floor,
    },
  );
}

function evaluateNearTieGate(cases, memories) {
  const nearTieCases = cases.filter((entry) => entry?.category === 'near_tie');

  assert(
    nearTieCases.length === 2,
    `runSweep: expected exactly 2 near_tie cases, got ${nearTieCases.length}`,
  );

  const details = nearTieCases.map((caseDef) => {
    const ranked = rankOne(caseDef, memories, 0).preFloor;
    assert(
      ranked.length === memories.length,
      `runSweep: near_tie case ${caseDef.case_id} did not rank all memories at floor=0 (got ${ranked.length}, expected ${memories.length})`,
    );
    const top1 = ranked[0] ?? null;
    const top2 = ranked[1] ?? null;
    const gap = top1 && top2
      ? Number(top1.final) - Number(top2.final)
      : Number.POSITIVE_INFINITY;

    return {
      case_id: String(caseDef.case_id ?? ''),
      top1_slug: top1?.slug ?? null,
      top2_slug: top2?.slug ?? null,
      gap: round6(gap),
      pass: Number.isFinite(gap) && gap < FROZEN_CONSTANTS.contested_threshold,
    };
  });

  return {
    status: details.every((row) => row.pass) ? 'PASS' : 'FAIL',
    cases: details,
  };
}

function buildCategoryBlueprint(cases) {
  const out = {};

  for (const caseDef of cases) {
    const category = String(caseDef?.category ?? '').trim();
    assert(category.length > 0, `runSweep: case ${caseDef?.case_id ?? '<unknown>'} missing category`);

    const partition = caseDef.expect_injection === true ? 'positive' : 'expected_empty';
    if (!out[category]) {
      out[category] = { partition };
      continue;
    }

    assert(
      out[category].partition === partition,
      `runSweep: category ${category} mixes partitions (${out[category].partition} vs ${partition})`,
    );
  }

  return out;
}

function initCategoryTracker(categoryBlueprint) {
  const tracker = {};

  for (const [category, entry] of Object.entries(categoryBlueprint)) {
    tracker[category] = {
      partition: entry.partition,
      total: 0,
      success: 0,
    };
  }

  return tracker;
}

function renderCategorySummary(categoryTracker) {
  const out = {};

  for (const [category, stats] of Object.entries(categoryTracker)) {
    if (stats.partition === 'positive') {
      out[category] = {
        partition: 'positive',
        cases: stats.total,
        recall_at_5_binary_hits: stats.success,
        recall_at_5_hit_rate: stats.total > 0 ? round6(stats.success / stats.total) : 0,
      };
      continue;
    }

    out[category] = {
      partition: 'expected_empty',
      cases: stats.total,
      zero_injection_hits: stats.success,
      zero_injection_rate: stats.total > 0 ? round6(stats.success / stats.total) : 0,
    };
  }

  return out;
}

function evaluateFloor(floor, cases, memories, categoryBlueprint) {
  const sums = {
    recall1: 0,
    recall5: 0,
    precision5: 0,
    precision5Count: 0,
    mrr: 0,
    ndcg5: 0,
    separation: 0,
  };

  let positiveBinaryHits = 0;

  const overallZeroRows = [];
  const positiveZeroRows = [];
  const emptyZeroRows = [];
  const categoryTracker = initCategoryTracker(categoryBlueprint);

  for (const caseDef of cases) {
    const ranked = rankOne(caseDef, memories, floor).ranked;
    const rankedSlugs = ranked.map((row) => row.slug);
    const zeroResults = rankedSlugs.length === 0;
    overallZeroRows.push(zeroResults);

    const categoryStats = categoryTracker[caseDef.category];
    if (categoryStats) {
      categoryStats.total += 1;
    }

    if (caseDef.expect_injection === true) {
      positiveZeroRows.push(zeroResults);

      const gold = Array.isArray(caseDef.expected_slugs) ? caseDef.expected_slugs : [];
      const recall1 = recallAtK(rankedSlugs, gold, 1);
      const recall5 = recallAtK(rankedSlugs, gold, 5);
      const hitAt5 = recallBinaryHitAtK(rankedSlugs, gold, 5);
      const pAt5 = precisionAtK(rankedSlugs, gold, 5);
      const rr = mrr(rankedSlugs, gold);
      const relevanceMap = Object.fromEntries(gold.map((slug) => [slug, 1]));
      const ndcg5 = ndcgAtK(rankedSlugs, relevanceMap, 5);

      const goldSet = new Set(gold);
      let goldTop = null;
      let bestNonGold = null;

      for (const row of ranked) {
        const final = Number(row.final);
        if (!Number.isFinite(final)) {
          continue;
        }

        if (goldSet.has(row.slug)) {
          goldTop = goldTop === null ? final : Math.max(goldTop, final);
          continue;
        }

        bestNonGold = bestNonGold === null ? final : Math.max(bestNonGold, final);
      }

      sums.recall1 += recall1;
      sums.recall5 += recall5;
      if (pAt5 !== null) {
        sums.precision5 += pAt5;
        sums.precision5Count += 1;
      }
      sums.mrr += rr;
      sums.ndcg5 += ndcg5;
      sums.separation += meanSeparation(goldTop, bestNonGold);

      if (hitAt5 === 1) {
        positiveBinaryHits += 1;
        if (categoryStats) {
          categoryStats.success += 1;
        }
      }

      continue;
    }

    emptyZeroRows.push(zeroResults);
    if (zeroResults && categoryStats) {
      categoryStats.success += 1;
    }
  }

  return {
    f: Number(floor.toFixed(2)),
    recall_at_1: round6(sums.recall1 / DENOMINATORS.positive),
    recall_at_5: round6(sums.recall5 / DENOMINATORS.positive),
    recall_at_5_binary_hits: positiveBinaryHits,
    precision_at_5: sums.precision5Count > 0
      ? round6(sums.precision5 / sums.precision5Count)
      : null,
    mrr: round6(sums.mrr / DENOMINATORS.positive),
    ndcg_at_5: round6(sums.ndcg5 / DENOMINATORS.positive),
    mean_separation: round6(sums.separation / DENOMINATORS.positive),
    zero_injection_overall: round6(zeroInjectionRate(overallZeroRows)),
    zero_injection_positive: round6(zeroInjectionRate(positiveZeroRows)),
    zero_injection_empty: round6(zeroInjectionRate(emptyZeroRows)),
    expected_empty_correct: emptyZeroRows.filter(Boolean).length,
    per_category: renderCategorySummary(categoryTracker),
  };
}

export function runSweep(dataset, opts = {}) {
  validateDataset(dataset);

  const cases = dataset.cases;
  const memories = dataset.memories;

  validateDenominators(cases);

  const floors = buildFloorGrid();
  const nearTieGate = evaluateNearTieGate(cases, memories);
  const categoryBlueprint = buildCategoryBlueprint(cases);

  const floorRows = floors.map((floor) => evaluateFloor(floor, cases, memories, categoryBlueprint));
  const knees = kneeCandidates(floorRows);

  return {
    schema: 'rb1a-floor-sweep/v1',
    fixture_version: dataset.fixture_version,
    embedding_model: dataset.embedding_model,
    embedding_dim: dataset.embedding_dim,
    method: typeof opts.method === 'string' && opts.method.trim().length > 0
      ? opts.method
      : DEFAULT_METHOD,
    generated_at: typeof opts.generated_at === 'string' && opts.generated_at.trim().length > 0
      ? opts.generated_at
      : new Date().toISOString(),
    denominators: {
      positive: DENOMINATORS.positive,
      expected_empty: DENOMINATORS.expected_empty,
      total: DENOMINATORS.total,
    },
    near_tie_gate: nearTieGate,
    floors: floorRows,
    knee_candidates: knees,
    knee_selected: null,
  };
}
