import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { buildDataset } from './dataset.mjs';
import { classifyLostPositives, fineBandDiagnostic } from './diagnostics.mjs';
import { runSweep } from './sweep.mjs';

const HARNESS_DIR = dirname(fileURLToPath(import.meta.url));
const BENCH_ROOT = resolve(HARNESS_DIR, '..', '..', '..');

const CORPUS_PATH = resolve(BENCH_ROOT, 'recall', 'corpus', 'go-concurrency-v1.json');
const GOLD_PATH = resolve(BENCH_ROOT, 'recall', 'gold', 'go-concurrency-v1.gold.jsonl');
const POSSIBLE_CACHE_PATHS = [
  resolve(BENCH_ROOT, 'runs', 'rb1a-harness-test', 'go-concurrency-v1.dataset.json'),
  resolve(BENCH_ROOT, 'runs', 'rb1a-calibration', 'go-concurrency-v1.dataset.json'),
];

let datasetPromise;
let sweepPromise;

function readJsonFile(filePath) {
  return JSON.parse(readFileSync(filePath, 'utf8'));
}

async function getDataset() {
  if (!datasetPromise) {
    datasetPromise = (async () => {
      for (const filePath of POSSIBLE_CACHE_PATHS) {
        if (existsSync(filePath)) {
          return readJsonFile(filePath);
        }
      }

      return buildDataset({
        corpusPath: CORPUS_PATH,
        goldPath: GOLD_PATH,
      });
    })();
  }

  return datasetPromise;
}

async function getSweep() {
  if (!sweepPromise) {
    sweepPromise = getDataset().then((dataset) => runSweep(dataset));
  }
  return sweepPromise;
}

function findFloorRow(sweepDoc, floor) {
  const row = sweepDoc.floors.find((entry) => Math.abs(Number(entry.f) - Number(floor)) < 1e-9);
  assert.ok(row, `missing floor row f=${floor}`);
  return row;
}

function vecForCosine(score) {
  const s = Number(score);
  return [s, Math.sqrt(Math.max(0, 1 - (s ** 2)))];
}

function makeSyntheticWideBandDataset() {
  const memories = [
    {
      slug: 'toy_gold',
      doc_vector: vecForCosine(0.68),
      keyword_weights: [{ keyword: 'gold', weight: 1 }],
    },
    {
      slug: 'toy_empty_top',
      doc_vector: vecForCosine(0.72),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_01',
      doc_vector: vecForCosine(0.6),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_02',
      doc_vector: vecForCosine(0.58),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_03',
      doc_vector: vecForCosine(0.56),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_04',
      doc_vector: vecForCosine(0.54),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_05',
      doc_vector: vecForCosine(0.52),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_06',
      doc_vector: vecForCosine(0.5),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_07',
      doc_vector: vecForCosine(0.48),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_08',
      doc_vector: vecForCosine(0.46),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_09',
      doc_vector: vecForCosine(0.44),
      keyword_weights: [],
    },
    {
      slug: 'toy_aux_10',
      doc_vector: vecForCosine(0.42),
      keyword_weights: [],
    },
  ];

  const positives = new Array(16).fill(null).map((_, i) => ({
    case_id: `toy_pos_${i + 1}`,
    category: 'single_hit',
    expect_injection: true,
    expected_slugs: ['toy_gold'],
    query_vector: [1, 0],
    query_keyword_weights: [{ keyword: 'gold', weight: 1 }],
  }));

  const empties = new Array(7).fill(null).map((_, i) => ({
    case_id: `toy_empty_${i + 1}`,
    category: 'no_match',
    expect_injection: false,
    expected_slugs: [],
    query_vector: [1, 0],
    query_keyword_weights: [],
  }));

  return {
    schema: 'rb1a-floor-dataset/v1',
    fixture_version: 'toy-wide-band-v1',
    embedding_model: 'toy',
    embedding_dim: 2,
    memories,
    cases: [...positives, ...empties],
  };
}

test('threshold inclusivity: 19 floors from 0.00 to 0.90 with 0.05 step', async () => {
  const sweepDoc = await getSweep();
  const floors = sweepDoc.floors.map((row) => Number(row.f));

  assert.equal(floors.length, 19);
  assert.equal(floors[0], 0);
  assert.equal(floors[floors.length - 1], 0.9);

  for (let i = 0; i < floors.length - 1; i += 1) {
    assert.ok(Math.abs((floors[i + 1] - floors[i]) - 0.05) < 1e-9, `step mismatch at index ${i}`);
  }
});

test('curve reproduction: frozen key curve at f=0.65/0.70/0.75', async () => {
  const sweepDoc = await getSweep();

  const at065 = findFloorRow(sweepDoc, 0.65);
  const at070 = findFloorRow(sweepDoc, 0.7);
  const at075 = findFloorRow(sweepDoc, 0.75);

  assert.equal(at065.recall_at_5_binary_hits, 16);
  assert.equal(at065.expected_empty_correct, 5);

  assert.equal(at070.recall_at_5_binary_hits, 14);
  assert.equal(at070.expected_empty_correct, 6);

  assert.equal(at075.recall_at_5_binary_hits, 14);
  assert.equal(at075.expected_empty_correct, 7);
});

test('fine-band detector: real data stands at 0.75; synthetic wide band triggers robust detection', async () => {
  const dataset = await getDataset();

  const realDiagnostic = fineBandDiagnostic(dataset);
  assert.equal(realDiagnostic.verdict, '0.75_STANDS');
  assert.ok(realDiagnostic.admissible_interval === null || realDiagnostic.width < 0.03);

  const synthetic = makeSyntheticWideBandDataset();
  const syntheticDiagnostic = fineBandDiagnostic(synthetic, {
    lo: 0.7,
    hi: 0.8,
    step: 0.001,
    minWidth: 0.03,
  });

  assert.equal(syntheticDiagnostic.verdict, 'BAND');
  assert.equal(syntheticDiagnostic.robust_band, true);
  assert.ok(syntheticDiagnostic.width >= 0.05, `expected width >= 0.05, got ${syntheticDiagnostic.width}`);
});

test('category classification: exactly two lost positives at f=0.75 and both are near_tie', async () => {
  const dataset = await getDataset();
  const lost = classifyLostPositives(dataset, 0.75);

  assert.equal(lost.lost_cases.length, 2);
  assert.deepEqual(
    lost.lost_cases.map((entry) => entry.case_id).sort(),
    ['nt_bounded_pool_ambiguous', 'nt_goroutine_leak_ambiguous'],
  );
  assert.ok(lost.lost_cases.every((entry) => entry.category === 'near_tie'));
  assert.equal(lost.all_thin_prompt, false);
});

test('selected-floor logic: no robust band -> no auto-selection and 0.75 fallback policy', async () => {
  const dataset = await getDataset();
  const sweepDoc = await getSweep();
  const diagnostic = fineBandDiagnostic(dataset);

  assert.equal(sweepDoc.knee_selected, null);
  assert.equal(diagnostic.verdict, '0.75_STANDS');

  const policyFallback = 0.75;
  const selectedFloor = diagnostic.robust_band
    ? diagnostic.admissible_interval?.[0]
    : policyFallback;

  assert.equal(selectedFloor, 0.75);
});

test('near-tie gate: both near_tie cases have gap < 0.20 and PASS', async () => {
  const sweepDoc = await getSweep();
  assert.equal(sweepDoc.near_tie_gate.status, 'PASS');
  assert.equal(sweepDoc.near_tie_gate.cases.length, 2);

  for (const row of sweepDoc.near_tie_gate.cases) {
    assert.equal(row.pass, true);
    assert.ok(Number(row.gap) < 0.2, `expected near-tie gap < 0.2 for ${row.case_id}, got ${row.gap}`);
  }
});
