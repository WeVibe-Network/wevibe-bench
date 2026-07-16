/**
 * Recall floor-calibration ranking core.
 *
 * FROZEN D-9.3 combined score:
 *   vector = cosine(queryVec, docVec)
 *   drop candidate if vector <= 0
 *   boost = Σ(shared keyword => queryWeight * docWeight)
 *   capped = min(gamma * boost, delta * vector)
 *   final = vector + capped
 *
 * Constants are frozen for production parity defaults:
 *   gamma = 0.1
 *   delta = 0.15
 *
 * Production recall path uses NO keyword gate and NO new-memory boost.
 * Optional floor is applied to the COMBINED `final` score with inclusive keep:
 * candidate survives when final >= floor.
 */

function toFiniteNumber(value, fallback = 0) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function normalizeKeywordWeightRows(keywordWeights) {
  const normalized = new Map();

  if (Array.isArray(keywordWeights)) {
    for (const row of keywordWeights) {
      if (!row || typeof row !== 'object') {
        continue;
      }

      const keyword = String(row.keyword ?? row.kw ?? row.term ?? '').trim().toLowerCase();
      if (!keyword) {
        continue;
      }

      const weight = toFiniteNumber(row.weight ?? row.score ?? 0, 0);
      normalized.set(keyword, (normalized.get(keyword) ?? 0) + weight);
    }

    return normalized;
  }

  if (keywordWeights && typeof keywordWeights === 'object') {
    for (const [rawKeyword, rawWeight] of Object.entries(keywordWeights)) {
      const keyword = String(rawKeyword ?? '').trim().toLowerCase();
      if (!keyword) {
        continue;
      }

      const weight = toFiniteNumber(rawWeight, 0);
      normalized.set(keyword, (normalized.get(keyword) ?? 0) + weight);
    }
  }

  return normalized;
}

function computeKeywordBoost(queryKwWeights, docKwWeights) {
  const queryMap = normalizeKeywordWeightRows(queryKwWeights);
  const docMap = normalizeKeywordWeightRows(docKwWeights);

  let boost = 0;

  for (const [keyword, queryWeight] of queryMap.entries()) {
    const docWeight = docMap.get(keyword);
    if (docWeight === undefined) {
      continue;
    }
    boost += queryWeight * docWeight;
  }

  return boost;
}

function sortByFinalThenInputIndex(rows) {
  rows.sort((a, b) => {
    if (b.final !== a.final) {
      return b.final - a.final;
    }
    return a.__index - b.__index;
  });
}

export function cosine(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b)) {
    throw new TypeError('cosine: both inputs must be arrays');
  }

  if (a.length !== b.length) {
    throw new Error(`cosine: vector length mismatch (${a.length} vs ${b.length})`);
  }

  let dot = 0;
  let normA = 0;
  let normB = 0;

  for (let i = 0; i < a.length; i += 1) {
    const av = Number(a[i]);
    const bv = Number(b[i]);

    if (!Number.isFinite(av) || !Number.isFinite(bv)) {
      throw new Error(`cosine: non-finite value at index ${i}`);
    }

    dot += av * bv;
    normA += av * av;
    normB += bv * bv;
  }

  if (normA === 0 || normB === 0) {
    return 0;
  }

  return dot / (Math.sqrt(normA) * Math.sqrt(normB));
}

export function scoreCombined(queryVec, queryKwWeights, docVec, docKwWeights, opts = {}) {
  const gamma = toFiniteNumber(opts.gamma, 0.1);
  const delta = toFiniteNumber(opts.delta, 0.15);

  const vector = cosine(queryVec, docVec);
  if (vector <= 0) {
    return null;
  }

  const boost = computeKeywordBoost(queryKwWeights, docKwWeights);
  const capped = Math.min(gamma * boost, delta * vector);
  const final = vector + capped;

  return { vector, boost, capped, final };
}

/**
 * Rank one calibration case.
 *
 * Returns both:
 * - ranked: survivors after vector-drop + optional floor filtering
 * - preFloor: full sorted ranking after vector-drop and before floor filtering
 */
export function rankCase(query, memories, opts = {}) {
  if (!query || typeof query !== 'object') {
    throw new TypeError('rankCase: query must be an object');
  }

  const queryVector = query.vector;
  const queryKeywordWeights = query.keyword_weights;

  const gamma = toFiniteNumber(opts.gamma, 0.1);
  const delta = toFiniteNumber(opts.delta, 0.15);
  const floor = toFiniteNumber(opts.floor, 0);

  const memoryRows = Array.isArray(memories) ? memories : [];
  const preFloorRows = [];
  let droppedByVector = 0;

  for (let i = 0; i < memoryRows.length; i += 1) {
    const memory = memoryRows[i] ?? {};
    const slug = String(memory.slug ?? '').trim();

    if (!slug) {
      throw new Error(`rankCase: memory at index ${i} missing slug`);
    }

    const score = scoreCombined(
      queryVector,
      queryKeywordWeights,
      memory.doc_vector,
      memory.keyword_weights,
      { gamma, delta },
    );

    if (score === null) {
      droppedByVector += 1;
      continue;
    }

    preFloorRows.push({
      slug,
      final: score.final,
      vector: score.vector,
      capped: score.capped,
      __index: i,
    });
  }

  sortByFinalThenInputIndex(preFloorRows);

  const preFloor = preFloorRows.map(({ __index, ...row }) => row);
  const ranked = floor > 0
    ? preFloor.filter((row) => row.final >= floor)
    : [...preFloor];

  const droppedByFloor = preFloor.length - ranked.length;

  return {
    ranked,
    preFloor,
    dropCount: {
      vector: droppedByVector,
      floor: droppedByFloor,
      kept: ranked.length,
      total: memoryRows.length,
    },
  };
}
