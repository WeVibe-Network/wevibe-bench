import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { runLiveGate, writeConfirmation } from './livegate.mjs';

const FLOOR = 0.75;
const SIM_POSITIVE = 14;
const MCP_URL = 'http://127.0.0.1:4451';
const ORG_ID = 'rb1a-livegate-test-org';

const DIAGNOSIS_ORDER = [
  'card_embed_model_prefix_drift',
  'seed_slug_cid_mismatch',
];

function cidForSlug(slug, prefix) {
  return `${prefix}${slug}`;
}

function makeSession(language = 'go') {
  return {
    language,
    stack: [language],
    frameworks: [],
    deps: [],
    errorStrings: [],
    directory: '',
    projectName: '',
  };
}

function buildGoldCases(queryPrefix = 'query') {
  const cases = [];

  for (let i = 1; i <= 12; i += 1) {
    const caseId = `sh_case_${String(i).padStart(2, '0')}`;
    cases.push({
      case_id: caseId,
      category: 'single_hit',
      query: `${queryPrefix}_${caseId}`,
      expected_slugs: [`sh_slug_${String(i).padStart(2, '0')}`],
      expect_injection: true,
      session: makeSession('go'),
    });
  }

  for (let i = 1; i <= 2; i += 1) {
    const caseId = `nt_case_${String(i).padStart(2, '0')}`;
    cases.push({
      case_id: caseId,
      category: 'near_tie',
      query: `${queryPrefix}_${caseId}`,
      expected_slugs: [`nt_slug_${i}_a`, `nt_slug_${i}_b`],
      expect_injection: true,
      session: makeSession('go'),
    });
  }

  for (let i = 1; i <= 2; i += 1) {
    const caseId = `tp_case_${String(i).padStart(2, '0')}`;
    cases.push({
      case_id: caseId,
      category: 'thin_prompt',
      query: `${queryPrefix}_${caseId}`,
      expected_slugs: [`tp_slug_${String(i).padStart(2, '0')}`],
      expect_injection: true,
      session: makeSession('go'),
    });
  }

  for (let i = 1; i <= 5; i += 1) {
    const caseId = `neg_case_${String(i).padStart(2, '0')}`;
    cases.push({
      case_id: caseId,
      category: 'cross_stack_negative',
      query: `${queryPrefix}_${caseId}`,
      expected_slugs: [],
      expect_injection: false,
      session: makeSession('swift'),
    });
  }

  for (let i = 1; i <= 2; i += 1) {
    const caseId = `nm_case_${String(i).padStart(2, '0')}`;
    cases.push({
      case_id: caseId,
      category: 'no_match',
      query: `${queryPrefix}_${caseId}`,
      expected_slugs: [],
      expect_injection: false,
      session: makeSession('go'),
    });
  }

  return cases;
}

function buildCidMap(cases, cidPrefix, includeSlugMap = false) {
  const caseEntries = {};
  const slugToCid = {};

  for (const item of cases) {
    const resolved = item.expect_injection
      ? item.expected_slugs.map((slug) => cidForSlug(slug, cidPrefix))
      : [];

    caseEntries[item.case_id] = {
      category: item.category,
      expected_slugs: [...item.expected_slugs],
      resolved_cids: resolved,
      expect_injection: item.expect_injection,
    };

    for (let i = 0; i < item.expected_slugs.length; i += 1) {
      slugToCid[item.expected_slugs[i]] = resolved[i];
    }
  }

  const payload = {
    run_id: 'test-run',
    cases: caseEntries,
  };

  if (includeSlugMap) {
    payload.slug_to_cid = slugToCid;
  }

  return payload;
}

function writeFixture(t, {
  queryPrefix = 'query',
  cidPrefix = 'cid-',
  includeSlugMap = false,
} = {}) {
  const tmpDir = mkdtempSync(join(tmpdir(), 'rb1a-livegate-'));
  t.after(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  const cases = buildGoldCases(queryPrefix);
  const goldPath = join(tmpDir, 'fixture.gold.jsonl');
  const cidMapPath = join(tmpDir, 'cid-map.json');
  const tokenPath = join(tmpDir, 'token.txt');

  writeFileSync(goldPath, `${cases.map((entry) => JSON.stringify(entry)).join('\n')}\n`, 'utf8');
  writeFileSync(
    cidMapPath,
    `${JSON.stringify(buildCidMap(cases, cidPrefix, includeSlugMap), null, 2)}\n`,
    'utf8',
  );
  writeFileSync(tokenPath, 'test-token\n', 'utf8');

  return {
    tmpDir,
    cases,
    goldPath,
    cidMapPath,
    tokenPath,
    cidPrefix,
  };
}

function hasOwn(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

function makeFakeRecallFn({
  cases,
  cidPrefix,
  livePositiveCount,
  emptyCorrectCount,
  memoryTextPrefix = 'memory',
}) {
  const byQuery = new Map(cases.map((entry) => [entry.query, entry]));
  const positiveCaseIds = cases
    .filter((entry) => entry.expect_injection === true)
    .map((entry) => entry.case_id);
  const emptyCaseIds = cases
    .filter((entry) => entry.expect_injection === false)
    .map((entry) => entry.case_id);

  const hitPositiveCases = new Set(positiveCaseIds.slice(0, livePositiveCount));
  const emptyCorrectCases = new Set(emptyCaseIds.slice(0, emptyCorrectCount));

  return async function fakeRecall(caseRequest) {
    assert.equal(caseRequest.relevance_floor, FLOOR);
    assert.equal(caseRequest.surface_budget, 1000);
    assert.equal(caseRequest.limit, 1000);

    assert.equal(hasOwn(caseRequest, 'expected_slugs'), false);
    assert.equal(hasOwn(caseRequest, 'expected_cids'), false);
    assert.equal(hasOwn(caseRequest, 'category'), false);

    const caseDef = byQuery.get(caseRequest.query);
    assert.ok(caseDef, `unexpected query ${caseRequest.query}`);

    if (caseDef.expect_injection) {
      if (hitPositiveCases.has(caseDef.case_id)) {
        return {
          status: 'ok',
          memories: [
            {
              cid: cidForSlug(caseDef.expected_slugs[0], cidPrefix),
              text: `${memoryTextPrefix}:target:${caseDef.case_id}`,
            },
            {
              cid: `noise:${caseDef.case_id}`,
              text: `${memoryTextPrefix}:noise:${caseDef.case_id}`,
            },
          ],
        };
      }

      return {
        status: 'ok',
        memories: [
          {
            cid: `noise-miss:${caseDef.case_id}`,
            text: `${memoryTextPrefix}:miss:${caseDef.case_id}`,
          },
        ],
      };
    }

    if (emptyCorrectCases.has(caseDef.case_id)) {
      return {
        status: 'ok',
        memories: [],
      };
    }

    return {
      status: 'ok',
      memories: [
        {
          cid: `noise-unexpected:${caseDef.case_id}`,
          text: `${memoryTextPrefix}:unexpected:${caseDef.case_id}`,
        },
      ],
    };
  };
}

async function runScenario(fixture, {
  livePositiveCount,
  emptyCorrectCount,
  simPositive = SIM_POSITIVE,
  memoryTextPrefix = 'memory',
}) {
  return runLiveGate({
    goldPath: fixture.goldPath,
    cidMapPath: fixture.cidMapPath,
    mcpUrl: MCP_URL,
    tokenPath: fixture.tokenPath,
    orgId: ORG_ID,
    floor: FLOOR,
    sim: simPositive,
    recallFn: makeFakeRecallFn({
      cases: fixture.cases,
      cidPrefix: fixture.cidPrefix,
      livePositiveCount,
      emptyCorrectCount,
      memoryTextPrefix,
    }),
  });
}

test('positive tolerance gate: sim=14 accepts live=15 and live=13, rejects live=12', async (t) => {
  const fixture = writeFixture(t);

  const live15 = await runScenario(fixture, { livePositiveCount: 15, emptyCorrectCount: 7 });
  assert.equal(live15.live_positive_binary_recall5, 15);
  assert.equal(live15.positive_gate_pass, true);
  assert.equal(live15.pass, true);

  const live13 = await runScenario(fixture, { livePositiveCount: 13, emptyCorrectCount: 7 });
  assert.equal(live13.live_positive_binary_recall5, 13);
  assert.equal(live13.positive_gate_pass, true);
  assert.equal(live13.pass, true);

  const live12 = await runScenario(fixture, { livePositiveCount: 12, emptyCorrectCount: 7 });
  assert.equal(live12.live_positive_binary_recall5, 12);
  assert.equal(live12.positive_gate_pass, false);
  assert.equal(live12.empty_gate_pass, true);
  assert.equal(live12.pass, false);
});

test('strict empty gate: 7/7 empties pass, 6/7 empties fail and force overall FAIL', async (t) => {
  const fixture = writeFixture(t);

  const empty7 = await runScenario(fixture, { livePositiveCount: 14, emptyCorrectCount: 7 });
  assert.equal(empty7.live_expected_empty_correct, 7);
  assert.equal(empty7.empty_gate_pass, true);
  assert.equal(empty7.positive_gate_pass, true);
  assert.equal(empty7.pass, true);

  const empty6 = await runScenario(fixture, { livePositiveCount: 14, emptyCorrectCount: 6 });
  assert.equal(empty6.live_expected_empty_correct, 6);
  assert.equal(empty6.empty_gate_pass, false);
  assert.equal(empty6.positive_gate_pass, true);
  assert.equal(empty6.pass, false);
});

test('combined gate: both pass => pass true; either gate fails => pass false', async (t) => {
  const fixture = writeFixture(t);

  const bothPass = await runScenario(fixture, { livePositiveCount: 14, emptyCorrectCount: 7 });
  assert.equal(bothPass.positive_gate_pass, true);
  assert.equal(bothPass.empty_gate_pass, true);
  assert.equal(bothPass.pass, true);

  const positiveFailOnly = await runScenario(fixture, { livePositiveCount: 12, emptyCorrectCount: 7 });
  assert.equal(positiveFailOnly.positive_gate_pass, false);
  assert.equal(positiveFailOnly.empty_gate_pass, true);
  assert.equal(positiveFailOnly.pass, false);

  const emptyFailOnly = await runScenario(fixture, { livePositiveCount: 14, emptyCorrectCount: 6 });
  assert.equal(emptyFailOnly.positive_gate_pass, true);
  assert.equal(emptyFailOnly.empty_gate_pass, false);
  assert.equal(emptyFailOnly.pass, false);
});

test('failure diagnosis order is locked and emitted exactly on FAIL', async (t) => {
  const fixture = writeFixture(t);

  const failed = await runScenario(fixture, { livePositiveCount: 12, emptyCorrectCount: 6 });
  assert.equal(failed.pass, false);
  assert.deepEqual(failed.diagnosis_order, DIAGNOSIS_ORDER);
});

test('content-free confirmation artifact and log: no CIDs, memory text, or query text', async (t) => {
  const fixture = writeFixture(t, {
    queryPrefix: 'QUERY_SECRET_TEXT',
    cidPrefix: 'bafySECRETCID-',
    includeSlugMap: true,
  });

  const result = await runScenario(fixture, {
    livePositiveCount: 14,
    emptyCorrectCount: 7,
    memoryTextPrefix: 'MEMORY_SECRET_TEXT',
  });

  const outPath = join(fixture.tmpDir, 'runs', 'rb1a-livegate-confirmation.json');
  writeConfirmation(result, outPath);

  const serializedResult = JSON.stringify(result);
  const serializedArtifact = readFileSync(outPath, 'utf8');
  const capturedLog = [
    `[RB1A-HARNESS] livegate_result status=${result.pass ? 'PASS' : 'FAIL'}`,
    `[RB1A-HARNESS] livegate_counts floor=${Number(result.floor).toFixed(2)} positive=${result.live_positive_binary_recall5}/${result.denominators.positive} expected_empty=${result.live_expected_empty_correct}/${result.denominators.expected_empty}`,
    `[RB1A-HARNESS] livegate_gates positive=${result.positive_gate_pass} empty=${result.empty_gate_pass} pass=${result.pass}`,
  ].join('\n');

  for (const secret of ['bafySECRETCID-', 'QUERY_SECRET_TEXT', 'MEMORY_SECRET_TEXT']) {
    assert.equal(serializedResult.includes(secret), false, `result leaked ${secret}`);
    assert.equal(serializedArtifact.includes(secret), false, `artifact leaked ${secret}`);
    assert.equal(capturedLog.includes(secret), false, `log leaked ${secret}`);
  }
});
