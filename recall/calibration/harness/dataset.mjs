import { existsSync, readFileSync } from 'node:fs';
import { dirname, isAbsolute, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const FIXTURE_VERSION = 'go-concurrency-v1';
const EXPECTED_MEMORY_COUNT = 12;
const EXPECTED_CASE_COUNT = 23;
const EXPECTED_EMBEDDING_DIM = 768;
const KEYWORD_TERM_PATTERN = /^[a-z][a-z0-9_]{1,39}$/;
const MAX_KEYWORDS_PER_MEMORY = 20;

function errorMessage(error) {
  if (error instanceof Error) {
    return error.stack ?? error.message;
  }
  return String(error);
}

function round6(value) {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function readJsonFile(filePath) {
  let raw = '';
  try {
    raw = readFileSync(filePath, 'utf8');
  } catch (error) {
    throw new Error(`Failed to read JSON file ${filePath}: ${errorMessage(error)}`);
  }

  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`Failed to parse JSON file ${filePath}: ${errorMessage(error)}`);
  }
}

function readJsonlFile(filePath) {
  let raw = '';
  try {
    raw = readFileSync(filePath, 'utf8');
  } catch (error) {
    throw new Error(`Failed to read JSONL file ${filePath}: ${errorMessage(error)}`);
  }

  const lines = raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  return lines.map((line, index) => {
    try {
      return JSON.parse(line);
    } catch (error) {
      throw new Error(`Failed to parse JSONL line ${index + 1} in ${filePath}: ${errorMessage(error)}`);
    }
  });
}

function requireObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value;
}

function requireString(value, label, { allowEmpty = false } = {}) {
  if (typeof value !== 'string') {
    throw new Error(`${label} must be a string`);
  }

  if (!allowEmpty && value.trim().length === 0) {
    throw new Error(`${label} must be a non-empty string`);
  }

  return value;
}

function requireBoolean(value, label) {
  if (typeof value !== 'boolean') {
    throw new Error(`${label} must be a boolean`);
  }
  return value;
}

function requireStringArray(value, label) {
  if (!Array.isArray(value)) {
    throw new Error(`${label} must be an array`);
  }
  return value.map((item, index) => requireString(item, `${label}[${index}]`));
}

function assertVector(vector, label) {
  if (!Array.isArray(vector)) {
    throw new Error(`${label} embedding is not an array`);
  }

  if (vector.length !== EXPECTED_EMBEDDING_DIM) {
    throw new Error(
      `${label} embedding_dim mismatch: expected ${EXPECTED_EMBEDDING_DIM}, got ${vector.length}`,
    );
  }

  for (let i = 0; i < vector.length; i += 1) {
    const value = vector[i];
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      throw new Error(`${label} embedding contains non-finite value at index ${i}`);
    }
  }
}

function normalizeMemoryRecord(rawMemory, index) {
  const m = requireObject(rawMemory, `corpus memory[${index}]`);
  return {
    id: requireString(m.id, `corpus memory[${index}].id`),
    text: requireString(m.text, `corpus memory[${index}].text`),
    keywords: requireStringArray(m.keywords, `corpus memory[${index}].keywords`),
    stack_hint: requireString(m.stack_hint, `corpus memory[${index}].stack_hint`),
  };
}

function normalizeCaseRecord(rawCase, index) {
  const c = requireObject(rawCase, `gold case[${index}]`);
  const session = requireObject(c.session, `gold case[${index}].session`);

  return {
    case_id: requireString(c.case_id, `gold case[${index}].case_id`),
    category: requireString(c.category, `gold case[${index}].category`),
    query: requireString(c.query, `gold case[${index}].query`),
    expected_slugs: requireStringArray(c.expected_slugs, `gold case[${index}].expected_slugs`),
    expect_injection: requireBoolean(c.expect_injection, `gold case[${index}].expect_injection`),
    session: {
      language: requireString(session.language, `gold case[${index}].session.language`),
      stack: requireStringArray(session.stack, `gold case[${index}].session.stack`),
      frameworks: requireStringArray(session.frameworks, `gold case[${index}].session.frameworks`),
      deps: requireStringArray(session.deps, `gold case[${index}].session.deps`),
      errorStrings: requireStringArray(session.errorStrings, `gold case[${index}].session.errorStrings`),
      directory: requireString(session.directory, `gold case[${index}].session.directory`, { allowEmpty: true }),
      projectName: requireString(session.projectName, `gold case[${index}].session.projectName`, {
        allowEmpty: true,
      }),
    },
  };
}

function buildClassifiedKeywordWeights(rawKeywords, normalizeKeywordTerm, memorySlug) {
  const deduped = [];
  const seen = new Set();

  for (const raw of rawKeywords) {
    const normalized = normalizeKeywordTerm(String(raw));
    if (normalized === null || !KEYWORD_TERM_PATTERN.test(normalized) || seen.has(normalized)) {
      continue;
    }

    seen.add(normalized);
    deduped.push(normalized);
    if (deduped.length >= MAX_KEYWORDS_PER_MEMORY) {
      break;
    }
  }

  if (deduped.length === 0) {
    throw new Error(
      `memory ${memorySlug} has zero valid classified keywords after filtering (seed parity fail-loud)`,
    );
  }

  const n = deduped.length;
  const uniform = round6(1 / n);
  const weights = new Array(n).fill(uniform);
  if (n > 1) {
    const sumOthers = weights.slice(0, -1).reduce((sum, value) => sum + value, 0);
    weights[n - 1] = 1 - sumOthers;
  }

  return {
    normalized: deduped,
    weighted: deduped.map((keyword, index) => ({
      keyword,
      weight: weights[index],
    })),
  };
}

function resolveInputPath(inputPath, label) {
  const raw = requireString(inputPath, label);
  return isAbsolute(raw) ? raw : resolve(process.cwd(), raw);
}

function loadSlugCidMap(cidMapPath) {
  const payload = readJsonFile(cidMapPath);
  const root = requireObject(payload, 'recall-cid-map-proof root');
  const cases = requireObject(root.cases, 'recall-cid-map-proof.cases');
  const slugToCid = new Map();

  for (const [caseId, caseValue] of Object.entries(cases)) {
    const caseEntry = requireObject(caseValue, `cid-map case ${caseId}`);
    const expectedSlugs = requireStringArray(caseEntry.expected_slugs ?? [], `cid-map ${caseId}.expected_slugs`);
    const resolvedCids = requireStringArray(caseEntry.resolved_cids ?? [], `cid-map ${caseId}.resolved_cids`);

    if (expectedSlugs.length !== resolvedCids.length) {
      throw new Error(
        `cid-map ${caseId} mismatch: expected_slugs=${expectedSlugs.length} resolved_cids=${resolvedCids.length}`,
      );
    }

    for (let i = 0; i < expectedSlugs.length; i += 1) {
      const slug = expectedSlugs[i];
      const cid = resolvedCids[i];
      const previous = slugToCid.get(slug);
      if (previous && previous !== cid) {
        throw new Error(`cid-map conflict for slug ${slug}: ${previous} vs ${cid}`);
      }
      slugToCid.set(slug, cid);
    }
  }

  return slugToCid;
}

function findWorkspaceRoot(startDir) {
  const fourUp = resolve(startDir, '..', '..', '..', '..');
  if (existsSync(resolve(fourUp, 'wevibe-mcp', 'dist'))) {
    return fourUp;
  }

  let cursor = startDir;
  while (true) {
    if (existsSync(resolve(cursor, 'wevibe-mcp', 'dist'))) {
      return cursor;
    }

    const parent = dirname(cursor);
    if (parent === cursor) {
      break;
    }
    cursor = parent;
  }

  throw new Error(
    `Unable to locate workspace root from ${startDir}: no ancestor contains wevibe-mcp/dist`,
  );
}

async function importModuleOrFail(modulePath, requiredExports) {
  let mod;
  try {
    mod = await import(pathToFileURL(modulePath).href);
  } catch (error) {
    throw new Error(`Import failed for ${modulePath}: ${errorMessage(error)}`);
  }

  for (const exportName of requiredExports) {
    if (!(exportName in mod)) {
      throw new Error(`Import ${modulePath} missing export: ${exportName}`);
    }
  }

  return mod;
}

async function loadRealPipeline(workspaceRoot) {
  const distRoot = resolve(workspaceRoot, 'wevibe-mcp', 'dist');

  const retrievalCardModulePath = resolve(distRoot, 'retrieval-card.js');
  const embeddingModulePath = resolve(distRoot, 'embedding.js');
  const embeddingConfigModulePath = resolve(distRoot, 'embedding-config.js');
  const keywordsModulePath = resolve(distRoot, 'mc1', 'keywords.js');
  const sessionModulePath = resolve(distRoot, 'session.js');

  const retrievalCard = await importModuleOrFail(retrievalCardModulePath, [
    'parseMemoryText',
    'buildRetrievalCard',
    'buildPromptDigest',
    'buildNeedCard',
  ]);
  const embedding = await importModuleOrFail(embeddingModulePath, ['computeLocalEmbedding']);
  const embeddingConfig = await importModuleOrFail(embeddingConfigModulePath, ['loadEmbeddingConfig']);
  const keywords = await importModuleOrFail(keywordsModulePath, [
    'boostKeywordsByVocab',
    'constrainKeywordsToVocab',
    'normalizeKeywordTerm',
  ]);
  const session = await importModuleOrFail(sessionModulePath, ['dissect_to_keywords']);

  return {
    parseMemoryText: retrievalCard.parseMemoryText,
    buildRetrievalCard: retrievalCard.buildRetrievalCard,
    buildPromptDigest: retrievalCard.buildPromptDigest,
    buildNeedCard: retrievalCard.buildNeedCard,
    computeLocalEmbedding: embedding.computeLocalEmbedding,
    loadEmbeddingConfig: embeddingConfig.loadEmbeddingConfig,
    boostKeywordsByVocab: keywords.boostKeywordsByVocab,
    constrainKeywordsToVocab: keywords.constrainKeywordsToVocab,
    normalizeKeywordTerm: keywords.normalizeKeywordTerm,
    dissect_to_keywords: session.dissect_to_keywords,
  };
}

function validateExpectedSlugs(goldCases, memorySlugs, slugToCid) {
  for (const item of goldCases) {
    for (const expectedSlug of item.expected_slugs) {
      if (!memorySlugs.has(expectedSlug)) {
        throw new Error(`case ${item.case_id} references unknown expected_slug ${expectedSlug}`);
      }
      if (slugToCid && !slugToCid.has(expectedSlug)) {
        throw new Error(`case ${item.case_id} expected_slug ${expectedSlug} missing CID in cid-map`);
      }
    }
  }
}

const HARNESS_DIR = dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = findWorkspaceRoot(HARNESS_DIR);
const REAL_PIPELINE = await loadRealPipeline(WORKSPACE_ROOT);

export async function buildDataset({ corpusPath, goldPath, cidMapPath = null }) {
  const resolvedCorpusPath = resolveInputPath(corpusPath, 'corpusPath');
  const resolvedGoldPath = resolveInputPath(goldPath, 'goldPath');
  const resolvedCidMapPath = cidMapPath === null ? null : resolveInputPath(cidMapPath, 'cidMapPath');

  const cfg = REAL_PIPELINE.loadEmbeddingConfig();
  const embeddingModel = requireString(cfg.model, 'embedding config model');

  const corpusRoot = requireObject(readJsonFile(resolvedCorpusPath), 'corpus root');
  const rawMemories = corpusRoot.memories;
  if (!Array.isArray(rawMemories)) {
    throw new Error('corpus.memories must be an array');
  }
  if (rawMemories.length !== EXPECTED_MEMORY_COUNT) {
    throw new Error(
      `corpus memory count mismatch: expected ${EXPECTED_MEMORY_COUNT}, got ${rawMemories.length}`,
    );
  }
  const corpusMemories = rawMemories.map((memory, index) => normalizeMemoryRecord(memory, index));

  const rawCases = readJsonlFile(resolvedGoldPath);
  if (rawCases.length !== EXPECTED_CASE_COUNT) {
    throw new Error(`gold case count mismatch: expected ${EXPECTED_CASE_COUNT}, got ${rawCases.length}`);
  }
  const goldCases = rawCases.map((item, index) => normalizeCaseRecord(item, index));

  const slugToCid = resolvedCidMapPath ? loadSlugCidMap(resolvedCidMapPath) : null;
  const memorySlugs = new Set(corpusMemories.map((memory) => memory.id));
  validateExpectedSlugs(goldCases, memorySlugs, slugToCid);

  const memoryBySlug = new Map();
  const orgVocab = [];
  const orgVocabSet = new Set();
  const datasetMemories = [];

  for (const memory of corpusMemories) {
    const parsed = REAL_PIPELINE.parseMemoryText(memory.text);
    const structured = {
      implement: parsed.implement,
      context: parsed.context,
      dnd: parsed.dnd,
      stack: memory.stack_hint
        .split(',')
        .map((entry) => entry.trim())
        .filter((entry) => entry.length > 0),
    };

    const cardText = REAL_PIPELINE.buildRetrievalCard(structured);
    const docVector = await REAL_PIPELINE.computeLocalEmbedding(
      cardText,
      { role: 'document', prefix: true },
      cfg,
    );
    assertVector(docVector, `memory ${memory.id}`);

    const { normalized, weighted } = buildClassifiedKeywordWeights(
      memory.keywords,
      REAL_PIPELINE.normalizeKeywordTerm,
      memory.id,
    );

    for (const keyword of normalized) {
      if (!orgVocabSet.has(keyword)) {
        orgVocabSet.add(keyword);
        orgVocab.push(keyword);
      }
    }

    const record = {
      slug: memory.id,
      doc_vector: docVector,
      keyword_weights: weighted,
    };

    const cid = slugToCid?.get(memory.id);
    if (cid) {
      record.cid = cid;
    }

    memoryBySlug.set(memory.id, record);
    datasetMemories.push(record);
  }

  const datasetCases = [];
  for (const item of goldCases) {
    for (const expectedSlug of item.expected_slugs) {
      if (!memoryBySlug.has(expectedSlug)) {
        throw new Error(`case ${item.case_id} references unknown expected_slug ${expectedSlug}`);
      }
    }

    const needHarvest = {
      task: item.query,
      language: item.session.language,
      stack: item.session.stack,
      frameworks: item.session.frameworks,
      deps: item.session.deps,
      errorStrings: item.session.errorStrings,
      files: [],
    };

    const promptDigest = REAL_PIPELINE.buildPromptDigest(needHarvest);
    const queryVector = await REAL_PIPELINE.computeLocalEmbedding(
      promptDigest,
      { role: 'query', prefix: true },
      cfg,
    );
    assertVector(queryVector, `case ${item.case_id}`);

    const rawKeywords = REAL_PIPELINE.dissect_to_keywords({
      description: item.query,
      technologies: item.session.stack,
      recentActivity: item.session.errorStrings,
      directory: item.session.directory,
      projectName: item.session.projectName,
    });
    const boostedKeywords = REAL_PIPELINE.boostKeywordsByVocab(rawKeywords, orgVocab);
    const queryKeywordWeights = boostedKeywords.map((entry, index) => {
      const keyword = requireString(entry.term, `case ${item.case_id} boosted keyword term[${index}]`);
      const weight = entry.weight;
      if (typeof weight !== 'number' || !Number.isFinite(weight)) {
        throw new Error(`case ${item.case_id} boosted keyword weight[${index}] must be finite number`);
      }
      return {
        keyword,
        weight,
      };
    });

    datasetCases.push({
      case_id: item.case_id,
      category: item.category,
      expect_injection: item.expect_injection,
      expected_slugs: item.expected_slugs,
      query_vector: queryVector,
      query_keyword_weights: queryKeywordWeights,
    });
  }

  const embeddingDim = datasetMemories[0]?.doc_vector?.length ?? 0;
  if (embeddingDim !== EXPECTED_EMBEDDING_DIM) {
    throw new Error(
      `embedding_dim mismatch after build: expected ${EXPECTED_EMBEDDING_DIM}, got ${embeddingDim}`,
    );
  }

  return {
    schema: 'rb1a-floor-dataset/v1',
    fixture_version: FIXTURE_VERSION,
    embedding_model: embeddingModel,
    embedding_dim: embeddingDim,
    built_at: new Date().toISOString(),
    memories: datasetMemories,
    cases: datasetCases,
  };
}
