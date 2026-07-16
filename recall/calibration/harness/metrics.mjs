function toFiniteNumber(value, fallback = 0) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function normalizeIdList(value) {
  if (Array.isArray(value)) {
    return value
      .map((entry) => String(entry ?? '').trim())
      .filter((entry) => entry.length > 0);
  }

  if (value === null || value === undefined) {
    return [];
  }

  const single = String(value).trim();
  return single ? [single] : [];
}

function normalizeRelevanceMap(relevanceMap) {
  const normalized = {};

  if (relevanceMap instanceof Map) {
    for (const [rawId, rawRel] of relevanceMap.entries()) {
      const id = String(rawId ?? '').trim();
      if (!id) {
        continue;
      }
      const rel = toFiniteNumber(rawRel, 1);
      if (rel > 0) {
        normalized[id] = rel;
      }
    }
    return normalized;
  }

  if (Array.isArray(relevanceMap)) {
    for (const rawId of relevanceMap) {
      const id = String(rawId ?? '').trim();
      if (id) {
        normalized[id] = 1;
      }
    }
    return normalized;
  }

  if (relevanceMap && typeof relevanceMap === 'object') {
    for (const [rawId, rawRel] of Object.entries(relevanceMap)) {
      const id = String(rawId ?? '').trim();
      if (!id) {
        continue;
      }
      const rel = toFiniteNumber(rawRel, 1);
      if (rel > 0) {
        normalized[id] = rel;
      }
    }
  }

  return normalized;
}

/**
 * Recall@K denominator is |gold|:
 *   recall@k = |gold ∩ top-k| / |gold|
 * Returns 0 when gold is empty or k <= 0.
 */
export function recallAtK(rankedIds, goldIds, k) {
  const ranking = Array.isArray(rankedIds)
    ? rankedIds.map((id) => String(id ?? '').trim()).filter(Boolean)
    : [];
  const gold = normalizeIdList(goldIds);
  const topK = Math.max(0, Math.floor(toFiniteNumber(k, 0)));

  if (gold.length === 0 || topK === 0) {
    return 0;
  }

  const seen = new Set(ranking.slice(0, topK));
  let hits = 0;

  for (const id of gold) {
    if (seen.has(id)) {
      hits += 1;
    }
  }

  return hits / gold.length;
}

/**
 * Binary hit@K:
 *   1 if any gold id appears in top-k, else 0.
 * Returns 0 when gold is empty or k <= 0.
 */
export function recallBinaryHitAtK(rankedIds, goldIds, k) {
  const ranking = Array.isArray(rankedIds)
    ? rankedIds.map((id) => String(id ?? '').trim()).filter(Boolean)
    : [];
  const gold = new Set(normalizeIdList(goldIds));
  const topK = Math.max(0, Math.floor(toFiniteNumber(k, 0)));

  if (gold.size === 0 || topK === 0) {
    return 0;
  }

  for (const id of ranking.slice(0, topK)) {
    if (gold.has(id)) {
      return 1;
    }
  }

  return 0;
}

/**
 * Precision@K denominator is |top-k|:
 *   precision@k = |gold ∩ top-k| / |top-k|
 * Returns null when top-k is empty and gold is empty.
 * Returns 0 when top-k is empty and gold is non-empty.
 */
export function precisionAtK(rankedIds, goldIds, k) {
  const ranking = Array.isArray(rankedIds)
    ? rankedIds.map((id) => String(id ?? '').trim()).filter(Boolean)
    : [];
  const gold = new Set(normalizeIdList(goldIds));
  const topK = Math.max(0, Math.floor(toFiniteNumber(k, 0)));
  const rankedTopK = ranking.slice(0, topK);

  if (rankedTopK.length === 0) {
    return gold.size === 0 ? null : 0;
  }

  let hits = 0;
  for (const id of rankedTopK) {
    if (gold.has(id)) {
      hits += 1;
    }
  }

  return hits / rankedTopK.length;
}

/**
 * Mean reciprocal rank (MRR):
 *   reciprocal rank = 1 / (rank position of first relevant item)
 *   mrr = reciprocal rank for this ranking (single-query form)
 * Returns 0 when gold is empty or no relevant id appears in the ranking.
 */
export function mrr(rankedIds, goldIds) {
  const ranking = Array.isArray(rankedIds)
    ? rankedIds.map((id) => String(id ?? '').trim()).filter(Boolean)
    : [];
  const gold = new Set(normalizeIdList(goldIds));

  if (gold.size === 0) {
    return 0;
  }

  for (let i = 0; i < ranking.length; i += 1) {
    if (gold.has(ranking[i])) {
      return 1 / (i + 1);
    }
  }

  return 0;
}

/**
 * NDCG@K denominator is IDCG@K (ideal discounted cumulative gain at K):
 *   ndcg@k = dcg@k / idcg@k
 * Returns 0 when k <= 0, relevance map is empty, or idcg@k is 0.
 */
export function ndcgAtK(rankedIds, relevanceMap, k) {
  const ranking = Array.isArray(rankedIds)
    ? rankedIds.map((id) => String(id ?? '').trim()).filter(Boolean)
    : [];
  const topK = Math.max(0, Math.floor(toFiniteNumber(k, 0)));

  if (topK === 0) {
    return 0;
  }

  const relevance = normalizeRelevanceMap(relevanceMap);
  const relValues = Object.values(relevance);
  if (relValues.length === 0) {
    return 0;
  }

  let dcg = 0;
  for (let i = 0; i < Math.min(topK, ranking.length); i += 1) {
    const rel = toFiniteNumber(relevance[ranking[i]], 0);
    if (rel <= 0) {
      continue;
    }
    dcg += (2 ** rel - 1) / Math.log2(i + 2);
  }

  const ideal = relValues
    .filter((value) => toFiniteNumber(value, 0) > 0)
    .sort((a, b) => b - a)
    .slice(0, topK);

  let idcg = 0;
  for (let i = 0; i < ideal.length; i += 1) {
    idcg += (2 ** ideal[i] - 1) / Math.log2(i + 2);
  }

  if (idcg <= 0) {
    return 0;
  }

  return dcg / idcg;
}

/**
 * Mean separation is a score difference (no denominator):
 *   meanSeparation = goldScore - bestNonGoldScore
 */
export function meanSeparation(goldScore, bestNonGoldScore) {
  const gold = toFiniteNumber(goldScore, 0);
  const nonGold = toFiniteNumber(bestNonGoldScore, 0);
  return gold - nonGold;
}

/**
 * Zero-injection rate denominator is total case count:
 *   zeroInjectionRate = (# cases with zero returned results) / (# cases)
 */
export function zeroInjectionRate(perCaseZeroFlags) {
  if (!Array.isArray(perCaseZeroFlags) || perCaseZeroFlags.length === 0) {
    return 0;
  }

  let zeroCount = 0;

  for (const row of perCaseZeroFlags) {
    if (row === true) {
      zeroCount += 1;
      continue;
    }

    if (!row || typeof row !== 'object') {
      continue;
    }

    if (row.zero_results === true || row.zeroResults === true) {
      zeroCount += 1;
      continue;
    }

    const rankedIds = Array.isArray(row.ranked_ids)
      ? row.ranked_ids
      : (Array.isArray(row.rankedIds) ? row.rankedIds : null);

    if (Array.isArray(rankedIds) && rankedIds.length === 0) {
      zeroCount += 1;
    }
  }

  return zeroCount / perCaseZeroFlags.length;
}
