import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { basename, dirname, isAbsolute, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const LIVE_GATE_SCHEMA = 'rb1a-live-gate/v1'
const DIAGNOSIS_ORDER = Object.freeze([
  'card_embed_model_prefix_drift',
  'seed_slug_cid_mismatch',
])
const DENOMINATORS = Object.freeze({
  positive: 16,
  expected_empty: 7,
  total: 23,
})
const PIPELINE_FINGERPRINT = Object.freeze({
  embedding_model: 'nomic-embed-text:v1.5',
  embedding_dim: 768,
})
const MC_VERSION = 1
const SURFACE_BUDGET = 1000
const LIMIT = 1000

const HARNESS_DIR = dirname(fileURLToPath(import.meta.url))
const BENCH_ROOT = resolve(HARNESS_DIR, '..', '..', '..')
const RECALL_ROOT = resolve(BENCH_ROOT, 'recall')

function errorMessage(error) {
  if (error instanceof Error) {
    return error.stack ?? error.message
  }
  return String(error)
}

function requireObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`)
  }
  return value
}

function requireString(value, label, { allowEmpty = false } = {}) {
  if (typeof value !== 'string') {
    throw new Error(`${label} must be a string`)
  }

  const trimmed = value.trim()
  if (!allowEmpty && trimmed.length === 0) {
    throw new Error(`${label} must be a non-empty string`)
  }

  return allowEmpty ? value : trimmed
}

function requireBoolean(value, label) {
  if (typeof value !== 'boolean') {
    throw new Error(`${label} must be a boolean`)
  }
  return value
}

function requireStringArray(value, label) {
  if (!Array.isArray(value)) {
    throw new Error(`${label} must be an array`)
  }
  return value.map((entry, index) => requireString(entry, `${label}[${index}]`))
}

function optionalStringArray(value, label) {
  if (value === undefined || value === null) {
    return []
  }
  return requireStringArray(value, label)
}

function resolveInputPath(inputPath, label) {
  const raw = requireString(inputPath, label)
  return isAbsolute(raw) ? raw : resolve(process.cwd(), raw)
}

function parseJsonFile(filePath, label) {
  const resolvedPath = resolveInputPath(filePath, label)
  let raw = ''

  try {
    raw = readFileSync(resolvedPath, 'utf8')
  } catch (error) {
    throw new Error(`Failed to read ${label} file ${resolvedPath}: ${errorMessage(error)}`)
  }

  try {
    return {
      path: resolvedPath,
      value: JSON.parse(raw),
    }
  } catch (error) {
    throw new Error(`Failed to parse ${label} file ${resolvedPath}: ${errorMessage(error)}`)
  }
}

function parseJsonlFile(filePath, label) {
  const resolvedPath = resolveInputPath(filePath, label)
  let raw = ''

  try {
    raw = readFileSync(resolvedPath, 'utf8')
  } catch (error) {
    throw new Error(`Failed to read ${label} file ${resolvedPath}: ${errorMessage(error)}`)
  }

  const lines = raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)

  return {
    path: resolvedPath,
    value: lines.map((line, index) => {
      try {
        return JSON.parse(line)
      } catch (error) {
        throw new Error(`Failed to parse ${label} line ${index + 1} in ${resolvedPath}: ${errorMessage(error)}`)
      }
    }),
  }
}

function normalizeSession(rawSession, label) {
  const session = requireObject(rawSession, label)

  return {
    language: requireString(session.language, `${label}.language`),
    stack: optionalStringArray(session.stack, `${label}.stack`),
    frameworks: optionalStringArray(session.frameworks, `${label}.frameworks`),
    deps: optionalStringArray(session.deps, `${label}.deps`),
    errorStrings: optionalStringArray(session.errorStrings, `${label}.errorStrings`),
    directory: requireString(session.directory ?? '', `${label}.directory`, { allowEmpty: true }),
    projectName: requireString(session.projectName ?? '', `${label}.projectName`, { allowEmpty: true }),
  }
}

function normalizeGoldCase(rawCase, index) {
  const item = requireObject(rawCase, `gold case[${index}]`)

  return {
    case_id: requireString(item.case_id, `gold case[${index}].case_id`),
    category: requireString(item.category, `gold case[${index}].category`),
    query: requireString(item.query, `gold case[${index}].query`),
    expected_slugs: optionalStringArray(item.expected_slugs, `gold case[${index}].expected_slugs`),
    expect_injection: requireBoolean(item.expect_injection, `gold case[${index}].expect_injection`),
    session: normalizeSession(item.session, `gold case[${index}].session`),
  }
}

function loadGoldCases(goldPath) {
  const parsed = parseJsonlFile(goldPath, 'gold')
  return {
    path: parsed.path,
    cases: parsed.value.map((entry, index) => normalizeGoldCase(entry, index)),
  }
}

function validateDenominators(goldCases) {
  const positive = goldCases.filter((entry) => entry.expect_injection === true).length
  const expectedEmpty = goldCases.filter((entry) => entry.expect_injection === false).length
  const total = goldCases.length

  if (positive !== DENOMINATORS.positive) {
    throw new Error(`gold positive denominator mismatch: expected ${DENOMINATORS.positive}, got ${positive}`)
  }

  if (expectedEmpty !== DENOMINATORS.expected_empty) {
    throw new Error(
      `gold expected-empty denominator mismatch: expected ${DENOMINATORS.expected_empty}, got ${expectedEmpty}`,
    )
  }

  if (total !== DENOMINATORS.total) {
    throw new Error(`gold total denominator mismatch: expected ${DENOMINATORS.total}, got ${total}`)
  }
}

function deriveFixtureVersion(goldPath) {
  const fileName = basename(goldPath)
  const match = fileName.match(/^(.*)\.gold\.jsonl$/)
  if (match && match[1]) {
    return match[1]
  }
  return fileName.replace(/\.jsonl$/i, '')
}

function addSlugToCid(slugToCid, slug, cid, sourceLabel) {
  const previous = slugToCid.get(slug)
  if (previous && previous !== cid) {
    throw new Error(`cid-map slug conflict for ${slug} at ${sourceLabel}: ${previous} vs ${cid}`)
  }
  slugToCid.set(slug, cid)
}

function normalizeCidMapCaseEntry(rawCase, label, explicitCaseId = null) {
  const item = requireObject(rawCase, label)
  const caseId = explicitCaseId ?? requireString(item.case_id ?? item.caseId, `${label}.case_id`)

  return {
    caseId,
    expectedSlugs: optionalStringArray(item.expected_slugs ?? item.expectedSlugs, `${label}.expected_slugs`),
    resolvedCids: optionalStringArray(item.resolved_cids ?? item.resolvedCids, `${label}.resolved_cids`),
  }
}

function readOptionalSlugMap(rawMap, label, slugToCid) {
  if (rawMap === undefined || rawMap === null) {
    return
  }

  const mapObject = requireObject(rawMap, label)
  for (const [slug, cid] of Object.entries(mapObject)) {
    addSlugToCid(slugToCid, requireString(slug, `${label} key`), requireString(cid, `${label}.${slug}`), label)
  }
}

function loadCidMap(cidMapPath) {
  const parsed = parseJsonFile(cidMapPath, 'cid-map')
  const root = requireObject(parsed.value, 'cid-map root')
  const slugToCid = new Map()
  const caseToResolved = new Map()

  readOptionalSlugMap(root.slug_to_cid, 'cid-map.slug_to_cid', slugToCid)
  readOptionalSlugMap(root.slugToCid, 'cid-map.slugToCid', slugToCid)
  readOptionalSlugMap(root.slug_cid_map, 'cid-map.slug_cid_map', slugToCid)
  readOptionalSlugMap(root.slugCidMap, 'cid-map.slugCidMap', slugToCid)

  const casesRaw = root.cases
  if (casesRaw !== undefined && casesRaw !== null) {
    if (Array.isArray(casesRaw)) {
      for (let i = 0; i < casesRaw.length; i += 1) {
        const entry = normalizeCidMapCaseEntry(casesRaw[i], `cid-map.cases[${i}]`)
        caseToResolved.set(entry.caseId, new Set(entry.resolvedCids))

        if (entry.expectedSlugs.length > 0 && entry.expectedSlugs.length === entry.resolvedCids.length) {
          for (let j = 0; j < entry.expectedSlugs.length; j += 1) {
            addSlugToCid(slugToCid, entry.expectedSlugs[j], entry.resolvedCids[j], `cid-map.cases[${i}]`)
          }
        }
      }
    } else {
      const caseObject = requireObject(casesRaw, 'cid-map.cases')
      for (const [caseId, rawCase] of Object.entries(caseObject)) {
        const entry = normalizeCidMapCaseEntry(rawCase, `cid-map.cases.${caseId}`, requireString(caseId, 'cid-map case id'))
        caseToResolved.set(entry.caseId, new Set(entry.resolvedCids))

        if (entry.expectedSlugs.length > 0 && entry.expectedSlugs.length === entry.resolvedCids.length) {
          for (let j = 0; j < entry.expectedSlugs.length; j += 1) {
            addSlugToCid(slugToCid, entry.expectedSlugs[j], entry.resolvedCids[j], `cid-map.cases.${caseId}`)
          }
        }
      }
    }
  }

  return {
    path: parsed.path,
    slugToCid,
    caseToResolved,
  }
}

function setsEqual(a, b) {
  if (a.size !== b.size) {
    return false
  }

  for (const value of a) {
    if (!b.has(value)) {
      return false
    }
  }

  return true
}

function buildExpectedCidsByCase(goldCases, cidMap) {
  const out = new Map()

  for (const caseDef of goldCases) {
    if (caseDef.expect_injection !== true) {
      out.set(caseDef.case_id, new Set())
      continue
    }

    const fromSlugMap = []
    let allSlugMapped = caseDef.expected_slugs.length > 0

    for (const slug of caseDef.expected_slugs) {
      const cid = cidMap.slugToCid.get(slug)
      if (!cid) {
        allSlugMapped = false
        break
      }
      fromSlugMap.push(cid)
    }

    const slugMapSet = new Set(fromSlugMap)
    const caseSet = cidMap.caseToResolved.get(caseDef.case_id)

    if (allSlugMapped) {
      if (caseSet && caseSet.size > 0 && !setsEqual(slugMapSet, caseSet)) {
        throw new Error(`cid-map mismatch for case ${caseDef.case_id}: slug map set disagrees with case resolved_cids`)
      }
      out.set(caseDef.case_id, slugMapSet)
      continue
    }

    if (caseSet && caseSet.size > 0) {
      out.set(caseDef.case_id, new Set(caseSet))
      continue
    }

    throw new Error(`cid-map missing expected CID set for positive case ${caseDef.case_id}`)
  }

  return out
}

function parseFloor(floor) {
  const value = Number(floor)
  if (!Number.isFinite(value) || value < 0 || value > 1) {
    throw new Error(`floor must be a finite number in [0,1]; got ${floor}`)
  }
  return Number(value.toFixed(6))
}

function normalizeSimPositiveCount(sim) {
  let raw = sim
  if (sim && typeof sim === 'object' && !Array.isArray(sim)) {
    raw = sim.sim_positive_count ?? sim.positive_binary_recall5 ?? sim.positive_count
  }

  const numeric = Number(raw)
  if (!Number.isInteger(numeric) || numeric < 0 || numeric > DENOMINATORS.positive) {
    throw new Error(
      `sim positive count must be an integer in [0,${DENOMINATORS.positive}]; got ${raw}`,
    )
  }

  return numeric
}

function parseMcpUrl(mcpUrl) {
  const raw = requireString(mcpUrl, 'mcpUrl')
  let url
  try {
    url = new URL(raw)
  } catch (error) {
    throw new Error(`Invalid mcpUrl ${raw}: ${errorMessage(error)}`)
  }

  if (!url.host) {
    throw new Error(`Invalid mcpUrl host: ${raw}`)
  }

  return {
    endpoint: url.toString().replace(/\/+$/, ''),
    host: url.host,
  }
}

function readBearerToken(tokenPath) {
  const resolvedTokenPath = resolveInputPath(tokenPath, 'tokenPath')
  let raw = ''

  try {
    raw = readFileSync(resolvedTokenPath, 'utf8')
  } catch (error) {
    throw new Error(`Failed to read token file ${resolvedTokenPath}: ${errorMessage(error)}`)
  }

  const token = raw.trim()
  if (token.length === 0) {
    throw new Error(`Token file ${resolvedTokenPath} is empty`)
  }

  return token
}

function buildCaseRequest(caseDef, floor, orgId) {
  return {
    query: caseDef.query,
    language: caseDef.session.language,
    stack: [...caseDef.session.stack],
    frameworks: [...caseDef.session.frameworks],
    deps: [...caseDef.session.deps],
    errorStrings: [...caseDef.session.errorStrings],
    directory: caseDef.session.directory,
    projectName: caseDef.session.projectName,
    org_id: orgId,
    mc_version: MC_VERSION,
    session_id: randomUUID(),
    relevance_floor: floor,
    surface_budget: SURFACE_BUDGET,
    limit: LIMIT,
  }
}

function firstNonEmptyString(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim().length > 0) {
      return value.trim()
    }
  }
  return null
}

function extractReturnedCids(recallResponse, caseId) {
  const root = requireObject(recallResponse, `recall response for case ${caseId}`)

  if (typeof root.status === 'string' && root.status.toLowerCase() === 'error') {
    throw new Error(`Live gate recall returned error status for case ${caseId}`)
  }

  if (root.memories === undefined || root.memories === null) {
    return []
  }

  if (!Array.isArray(root.memories)) {
    throw new Error(`Live gate recall response memories must be an array for case ${caseId}`)
  }

  const cids = []
  for (const memory of root.memories) {
    if (!memory || typeof memory !== 'object' || Array.isArray(memory)) {
      continue
    }

    const cid = firstNonEmptyString(memory.cid, memory.submission_hash, memory.hash)
    if (cid) {
      cids.push(cid)
    }
  }

  return cids
}

function buildCategoryEntry(categoryMap, caseDef) {
  const partition = caseDef.expect_injection === true ? 'positive' : 'expected_empty'
  const existing = categoryMap.get(caseDef.category)
  if (!existing) {
    const created = {
      partition,
      cases: 0,
      positive_binary_recall5_hits: 0,
      expected_empty_correct: 0,
    }
    categoryMap.set(caseDef.category, created)
    return created
  }

  if (existing.partition !== partition) {
    throw new Error(
      `gold category partition mismatch for ${caseDef.category}: ${existing.partition} vs ${partition}`,
    )
  }

  return existing
}

function renderPerCategory(categoryMap) {
  const out = {}
  const categories = [...categoryMap.keys()].sort()

  for (const category of categories) {
    const stats = categoryMap.get(category)
    if (!stats) {
      continue
    }

    if (stats.partition === 'positive') {
      out[category] = {
        partition: 'positive',
        cases: stats.cases,
        positive_binary_recall5_hits: stats.positive_binary_recall5_hits,
      }
      continue
    }

    out[category] = {
      partition: 'expected_empty',
      cases: stats.cases,
      expected_empty_correct: stats.expected_empty_correct,
    }
  }

  return out
}

function createDefaultRecallFn(mcpUrl, tokenPath) {
  if (typeof fetch !== 'function') {
    throw new Error('Global fetch is unavailable in this Node runtime')
  }

  const parsedUrl = parseMcpUrl(mcpUrl)
  const token = readBearerToken(tokenPath)
  const endpoint = `${parsedUrl.endpoint}/v1/recall`

  return async function defaultRecallFn(caseRequest) {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
        'X-WeVibe-Trace-Id': randomUUID(),
      },
      body: JSON.stringify(caseRequest),
    })

    if (!response.ok) {
      throw new Error(`Live gate recall transport failed with status=${response.status}`)
    }

    try {
      return await response.json()
    } catch (error) {
      throw new Error(`Live gate recall response JSON parse failed: ${errorMessage(error)}`)
    }
  }
}

function selectRecallFn({ recallFn, mcpUrl, tokenPath }) {
  if (recallFn === null || recallFn === undefined) {
    return createDefaultRecallFn(mcpUrl, tokenPath)
  }

  if (typeof recallFn !== 'function') {
    throw new Error('recallFn must be a function when provided')
  }

  return recallFn
}

function isPathInside(parentPath, maybeChildPath) {
  const rel = relative(parentPath, maybeChildPath)
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel))
}

function hasRunsSegment(filePath) {
  return filePath.split(sep).filter(Boolean).includes('runs')
}

function assertAllowedOutputPath(outPath) {
  if (!hasRunsSegment(outPath)) {
    throw new Error(
      `confirmation output must include a "runs" path segment (non-committed output required); got ${outPath}`,
    )
  }

  if (isPathInside(RECALL_ROOT, outPath)) {
    throw new Error(`confirmation output resolves inside committed recall/ tree (${outPath}); use runs/`)
  }

  if (isPathInside(BENCH_ROOT, outPath)) {
    const rel = relative(BENCH_ROOT, outPath)
    if (!rel.split(sep).includes('runs')) {
      throw new Error(`confirmation output inside wevibe-bench must be under runs/; got ${outPath}`)
    }
  }
}

export async function runLiveGate({
  goldPath,
  cidMapPath,
  mcpUrl,
  tokenPath,
  orgId,
  floor,
  sim = null,
  recallFn = null,
}) {
  const normalizedFloor = parseFloor(floor)
  const normalizedOrgId = requireString(orgId, 'orgId')
  const simPositiveCount = normalizeSimPositiveCount(sim)
  const parsedMcp = parseMcpUrl(mcpUrl)

  const { path: resolvedGoldPath, cases: goldCases } = loadGoldCases(goldPath)
  validateDenominators(goldCases)

  const cidMap = loadCidMap(cidMapPath)
  const expectedCidsByCase = buildExpectedCidsByCase(goldCases, cidMap)
  const recall = selectRecallFn({ recallFn, mcpUrl, tokenPath })

  const categoryMap = new Map()
  let positiveHits = 0
  let expectedEmptyCorrect = 0

  for (const caseDef of goldCases) {
    const categoryEntry = buildCategoryEntry(categoryMap, caseDef)
    categoryEntry.cases += 1

    const caseRequest = buildCaseRequest(caseDef, normalizedFloor, normalizedOrgId)
    let recallResponse
    try {
      recallResponse = await recall(caseRequest)
    } catch {
      throw new Error(`Live gate recall request failed for case ${caseDef.case_id}`)
    }

    const returned = extractReturnedCids(recallResponse, caseDef.case_id)
    const returnedTop5 = returned.slice(0, 5)

    if (caseDef.expect_injection === true) {
      const expectedSet = expectedCidsByCase.get(caseDef.case_id)
      if (!expectedSet || expectedSet.size === 0) {
        throw new Error(`Expected CID set missing for positive case ${caseDef.case_id}`)
      }

      const hit = returnedTop5.some((cid) => expectedSet.has(cid))
      if (hit) {
        positiveHits += 1
        categoryEntry.positive_binary_recall5_hits += 1
      }
      continue
    }

    const empty = returned.length === 0
    if (empty) {
      expectedEmptyCorrect += 1
      categoryEntry.expected_empty_correct += 1
    }
  }

  const positiveGatePass = Math.abs(positiveHits - simPositiveCount) <= 1
  const emptyGatePass = expectedEmptyCorrect === DENOMINATORS.expected_empty
  const pass = positiveGatePass && emptyGatePass

  const result = {
    schema: LIVE_GATE_SCHEMA,
    fixture_version: deriveFixtureVersion(resolvedGoldPath),
    floor: normalizedFloor,
    generated_at: new Date().toISOString(),
    denominators: {
      positive: DENOMINATORS.positive,
      expected_empty: DENOMINATORS.expected_empty,
      total: DENOMINATORS.total,
    },
    pipeline_fingerprint: {
      embedding_model: PIPELINE_FINGERPRINT.embedding_model,
      embedding_dim: PIPELINE_FINGERPRINT.embedding_dim,
      mcp_url_host: parsedMcp.host,
      org: normalizedOrgId,
    },
    sim_positive_count: simPositiveCount,
    live_positive_binary_recall5: positiveHits,
    live_expected_empty_correct: expectedEmptyCorrect,
    positive_gate_pass: positiveGatePass,
    empty_gate_pass: emptyGatePass,
    pass,
    per_category: renderPerCategory(categoryMap),
  }

  if (!pass) {
    result.diagnosis_order = [...DIAGNOSIS_ORDER]
  }

  return result
}

export function writeConfirmation(obj, outPath) {
  requireObject(obj, 'confirmation object')
  const resolvedOutPath = resolveInputPath(outPath, 'outPath')
  assertAllowedOutputPath(resolvedOutPath)

  mkdirSync(dirname(resolvedOutPath), { recursive: true })
  const tmpPath = resolve(
    dirname(resolvedOutPath),
    `.${basename(resolvedOutPath)}.tmp-${process.pid}-${randomUUID()}`,
  )

  try {
    writeFileSync(tmpPath, `${JSON.stringify(obj, null, 2)}\n`, 'utf8')
    renameSync(tmpPath, resolvedOutPath)
  } finally {
    if (existsSync(tmpPath)) {
      unlinkSync(tmpPath)
    }
  }
}
