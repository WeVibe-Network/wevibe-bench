import { recallBinaryHitAtK } from './metrics.mjs';
import { rankCase } from './ranking.mjs';

const FROZEN_CONSTANTS = Object.freeze({
  gamma: 0.1,
  delta: 0.15,
});

const DENOMINATORS = Object.freeze({
  positive: 16,
  expected_empty: 7,
  total: 23,
});

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

function assertDataset(dataset) {
  assert(dataset && typeof dataset === 'object', 'dataset must be an object');
  assert(Array.isArray(dataset.memories), 'dataset.memories must be an array');
  assert(Array.isArray(dataset.cases), 'dataset.cases must be an array');

  const positive = dataset.cases.filter((entry) => entry?.expect_injection === true).length;
  const expectedEmpty = dataset.cases.filter((entry) => entry?.expect_injection === false).length;
  const total = dataset.cases.length;

  assert(
    positive === DENOMINATORS.positive,
    `positive denominator mismatch: expected ${DENOMINATORS.positive}, got ${positive}`,
  );
  assert(
    expectedEmpty === DENOMINATORS.expected_empty,
    `expected-empty denominator mismatch: expected ${DENOMINATORS.expected_empty}, got ${expectedEmpty}`,
  );
  assert(
    total === DENOMINATORS.total,
    `total denominator mismatch: expected ${DENOMINATORS.total}, got ${total}`,
  );
}

function rankPrefloor(caseDef, memories) {
  const ranked = rankCase(
    {
      vector: caseDef.query_vector,
      keyword_weights: caseDef.query_keyword_weights,
    },
    memories,
    {
      gamma: FROZEN_CONSTANTS.gamma,
      delta: FROZEN_CONSTANTS.delta,
      floor: 0,
    },
  );

  const finalBySlug = new Map();
  for (const row of ranked.preFloor) {
    finalBySlug.set(row.slug, Number(row.final));
  }

  return {
    case_id: caseDef.case_id,
    category: caseDef.category,
    expect_injection: caseDef.expect_injection,
    expected_slugs: Array.isArray(caseDef.expected_slugs) ? caseDef.expected_slugs : [],
    pre_floor: ranked.preFloor,
    final_by_slug: finalBySlug,
  };
}

function precomputeCases(dataset) {
  assertDataset(dataset);
  return dataset.cases.map((caseDef) => rankPrefloor(caseDef, dataset.memories));
}

function collectFloors(caseScores, lo, hi, step) {
  const floors = new Set();

  floors.add(round6(lo));
  floors.add(round6(hi));

  const tickCount = Math.floor(((hi - lo) / step) + 1e-9);
  for (let i = 0; i <= tickCount; i += 1) {
    floors.add(round6(lo + i * step));
  }

  for (const caseDef of caseScores) {
    for (const row of caseDef.pre_floor) {
      const final = Number(row.final);
      if (!Number.isFinite(final)) {
        continue;
      }
      if (final < lo || final > hi) {
        continue;
      }
      floors.add(round6(final));
    }
  }

  return [...floors]
    .filter((value) => Number.isFinite(value))
    .sort((a, b) => a - b);
}

function evaluateFloor(caseScores, floor) {
  let positiveBinaryHits = 0;
  let expectedEmptyCorrect = 0;

  for (const caseDef of caseScores) {
    const survivors = caseDef.pre_floor.filter((row) => Number(row.final) >= floor);
    const rankedSlugs = survivors.map((row) => row.slug);

    if (caseDef.expect_injection) {
      positiveBinaryHits += recallBinaryHitAtK(rankedSlugs, caseDef.expected_slugs, 5);
      continue;
    }

    if (survivors.length === 0) {
      expectedEmptyCorrect += 1;
    }
  }

  return {
    f: floor,
    positive_binary_recall_at_5: positiveBinaryHits,
    expected_empty_correct: expectedEmptyCorrect,
  };
}

function computeBreakpoints(caseScores) {
  let minPositiveThreshold = Number.POSITIVE_INFINITY;
  let maxExpectedEmptyTop = Number.NEGATIVE_INFINITY;

  for (const caseDef of caseScores) {
    if (caseDef.expect_injection) {
      let bestGold = Number.NEGATIVE_INFINITY;

      for (const slug of caseDef.expected_slugs) {
        const score = Number(caseDef.final_by_slug.get(slug));
        if (Number.isFinite(score) && score > bestGold) {
          bestGold = score;
        }
      }

      assert(
        Number.isFinite(bestGold),
        `case ${caseDef.case_id} has no finite expected-gold final score in pre-floor ranking`,
      );

      if (bestGold < minPositiveThreshold) {
        minPositiveThreshold = bestGold;
      }
      continue;
    }

    const top = Number(caseDef.pre_floor[0]?.final);
    if (Number.isFinite(top) && top > maxExpectedEmptyTop) {
      maxExpectedEmptyTop = top;
    }
  }

  const minPositiveOut = Number.isFinite(minPositiveThreshold) ? round6(minPositiveThreshold) : null;
  const maxExpectedOut = Number.isFinite(maxExpectedEmptyTop) ? round6(maxExpectedEmptyTop) : null;

  let orderingGap = null;
  if (minPositiveOut !== null && maxExpectedOut !== null) {
    orderingGap = round6(minPositiveOut - maxExpectedOut);
  }

  return {
    min_positive_threshold: minPositiveOut,
    max_expected_empty_top: maxExpectedOut,
    ordering_gap: orderingGap,
  };
}

export function fineBandDiagnostic(
  dataset,
  { lo = 0.65, hi = 0.75, step = 0.001, minWidth = 0.03 } = {},
) {
  const loFloor = Number(lo);
  const hiFloor = Number(hi);
  const stepSize = Number(step);
  const minBandWidth = Number(minWidth);

  assert(Number.isFinite(loFloor), 'fineBandDiagnostic: lo must be finite');
  assert(Number.isFinite(hiFloor), 'fineBandDiagnostic: hi must be finite');
  assert(Number.isFinite(stepSize) && stepSize > 0, 'fineBandDiagnostic: step must be finite and > 0');
  assert(hiFloor >= loFloor, 'fineBandDiagnostic: hi must be >= lo');
  assert(Number.isFinite(minBandWidth) && minBandWidth >= 0, 'fineBandDiagnostic: minWidth must be finite and >= 0');

  const caseScores = precomputeCases(dataset);
  const floors = collectFloors(caseScores, loFloor, hiFloor, stepSize);

  const admissible = [];
  for (const floor of floors) {
    const row = evaluateFloor(caseScores, floor);
    if (
      row.positive_binary_recall_at_5 === DENOMINATORS.positive
      && row.expected_empty_correct === DENOMINATORS.expected_empty
    ) {
      admissible.push(floor);
    }
  }

  const admissibleInterval = admissible.length > 0
    ? [round6(admissible[0]), round6(admissible[admissible.length - 1])]
    : null;
  const width = admissibleInterval
    ? round6(admissibleInterval[1] - admissibleInterval[0])
    : 0;
  const robustBand = width >= minBandWidth;

  return {
    admissible_interval: admissibleInterval,
    width,
    robust_band: robustBand,
    verdict: robustBand ? 'BAND' : '0.75_STANDS',
    breakpoints: computeBreakpoints(caseScores),
  };
}

export function classifyLostPositives(dataset, floor = 0.75) {
  const floorValue = Number(floor);
  assert(Number.isFinite(floorValue), 'classifyLostPositives: floor must be finite');

  const caseScores = precomputeCases(dataset);
  const lost = [];

  for (const caseDef of caseScores) {
    if (!caseDef.expect_injection) {
      continue;
    }

    const baselineSlugs = caseDef.pre_floor.map((row) => row.slug);
    const baselineHit = recallBinaryHitAtK(baselineSlugs, caseDef.expected_slugs, 5);
    if (baselineHit !== 1) {
      continue;
    }

    const survivors = caseDef.pre_floor.filter((row) => Number(row.final) >= floorValue);
    const floorSlugs = survivors.map((row) => row.slug);
    const floorHit = recallBinaryHitAtK(floorSlugs, caseDef.expected_slugs, 5);

    if (floorHit === 1) {
      continue;
    }

    let bestGoldSlug = null;
    let bestGoldFinal = Number.NEGATIVE_INFINITY;
    for (const slug of caseDef.expected_slugs) {
      const score = Number(caseDef.final_by_slug.get(slug));
      if (Number.isFinite(score) && score > bestGoldFinal) {
        bestGoldFinal = score;
        bestGoldSlug = slug;
      }
    }

    lost.push({
      case_id: caseDef.case_id,
      category: caseDef.category,
      best_gold_slug: bestGoldSlug,
      best_gold_final: Number.isFinite(bestGoldFinal) ? round6(bestGoldFinal) : null,
      zero_injected: survivors.length === 0,
    });
  }

  return {
    lost_cases: lost,
    all_thin_prompt: lost.length > 0 && lost.every((row) => row.category === 'thin_prompt'),
  };
}

function bestBy(list, scoreFn) {
  let best = null;

  for (const item of list) {
    const score = Number(scoreFn(item));
    if (!Number.isFinite(score)) {
      continue;
    }

    if (!best) {
      best = { item, score };
      continue;
    }

    if (score > best.score) {
      best = { item, score };
      continue;
    }

    if (score === best.score && Number(item?.row?.f) > Number(best.item?.row?.f)) {
      best = { item, score };
    }
  }

  return best;
}

function curvature(points, index) {
  if (index <= 0 || index >= points.length - 1) {
    return 0;
  }

  const p0 = points[index - 1];
  const p1 = points[index];
  const p2 = points[index + 1];

  const a = Math.hypot(p1.x - p0.x, p1.y - p0.y);
  const b = Math.hypot(p2.x - p1.x, p2.y - p1.y);
  const c = Math.hypot(p2.x - p0.x, p2.y - p0.y);

  if (a === 0 || b === 0 || c === 0) {
    return 0;
  }

  const cross = Math.abs((p1.x - p0.x) * (p2.y - p0.y) - (p1.y - p0.y) * (p2.x - p0.x));
  return cross / (a * b * c);
}

export function kneeCandidates(sweepFloors) {
  assert(Array.isArray(sweepFloors), 'kneeCandidates: sweepFloors must be an array');

  const withRates = sweepFloors.map((row) => {
    const recallHit = Number(row.recall_at_5_binary_hits) / DENOMINATORS.positive;
    const specificity = Number(row.expected_empty_correct) / DENOMINATORS.expected_empty;
    const precision = Number(row.precision_at_5 ?? 0);
    return {
      row,
      recallHit,
      specificity,
      precision,
      youden: recallHit + specificity - 1,
    };
  });

  const eligibleRecallOne = withRates.filter(
    (entry) => Number(entry.row.recall_at_5_binary_hits) === DENOMINATORS.positive,
  );
  const a1 = bestBy(eligibleRecallOne, (entry) => entry.precision);

  const a2 = bestBy(withRates, (entry) => entry.youden);

  const prPoints = withRates.map((entry) => ({
    f: Number(entry.row.f),
    x: entry.recallHit,
    y: entry.precision,
  }));
  const curvatureRows = prPoints.map((point, index) => ({
    row: { f: point.f },
    point,
    curv: curvature(prPoints, index),
  }));
  const a3 = bestBy(curvatureRows, (entry) => entry.curv);

  const zeroFalsePositiveEligible = withRates.filter(
    (entry) => Number(entry.row.expected_empty_correct) === DENOMINATORS.expected_empty
      && Number(entry.row.recall_at_5_binary_hits) === DENOMINATORS.positive,
  );
  const a4 = bestBy(zeroFalsePositiveEligible, (entry) => Number(entry.row.f));

  const a5 = bestBy(withRates, (entry) => Number(entry.row.mean_separation));

  return [
    {
      algorithm: 'max_precision_subject_to_recall5_ge_1.0',
      f_star: a1 ? Number(Number(a1.item.row.f).toFixed(2)) : null,
      rationale: a1
        ? `max precision@5=${round6(a1.score)} while recall@5 binary hit-rate stayed 1.0`
        : 'no floor kept recall@5 binary hit-rate at 1.0',
    },
    {
      algorithm: 'youden_j',
      f_star: a2 ? Number(Number(a2.item.row.f).toFixed(2)) : null,
      rationale: a2
        ? `max J=${round6(a2.score)} (sensitivity=${round6(a2.item.recallHit)}, specificity=${round6(a2.item.specificity)})`
        : 'unable to compute Youden J',
    },
    {
      algorithm: 'kneedle_max_curvature',
      f_star: a3 ? Number(Number(a3.item.point.f).toFixed(2)) : null,
      rationale: a3
        ? `max discrete curvature=${round6(a3.score)} on precision-vs-recall@5-hit curve`
        : 'unable to compute curvature',
    },
    {
      algorithm: 'largest_f_zero_false_positive',
      f_star: a4 ? Number(Number(a4.item.row.f).toFixed(2)) : null,
      rationale: a4
        ? 'largest floor with zero expected-empty violations and full positive recall@5 hit-rate'
        : 'no floor achieved both zero expected-empty violations and full positive recall@5 hit-rate',
    },
    {
      algorithm: 'max_mean_separation',
      f_star: a5 ? Number(Number(a5.item.row.f).toFixed(2)) : null,
      rationale: a5
        ? `max mean separation=${round6(a5.score)}`
        : 'unable to compute mean separation',
    },
  ];
}
