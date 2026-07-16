import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { buildDataset } from './dataset.mjs';

const HARNESS_DIR = dirname(fileURLToPath(import.meta.url));
const BENCH_ROOT = resolve(HARNESS_DIR, '..', '..', '..');
const WORKSPACE_ROOT = resolve(BENCH_ROOT, '..');

const CORPUS_PATH = resolve(BENCH_ROOT, 'recall', 'corpus', 'go-concurrency-v1.json');
const GOLD_PATH = resolve(BENCH_ROOT, 'recall', 'gold', 'go-concurrency-v1.gold.jsonl');

function readJsonFile(filePath) {
  return JSON.parse(readFileSync(filePath, 'utf8'));
}

function readJsonlFile(filePath) {
  return readFileSync(filePath, 'utf8')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .map((line) => JSON.parse(line));
}

test('RB-1a dataset adapter identity + fail-loud guards', async (t) => {
  let builtDataset;

  await t.test('builds dataset from committed fixture with expected counts + vector dims', async () => {
    builtDataset = await buildDataset({
      corpusPath: CORPUS_PATH,
      goldPath: GOLD_PATH,
    });

    assert.equal(builtDataset.memories.length, 12);
    assert.equal(builtDataset.cases.length, 23);
    assert.equal(builtDataset.embedding_dim, 768);

    for (const memory of builtDataset.memories) {
      assert.equal(memory.doc_vector.length, 768);
    }
    for (const item of builtDataset.cases) {
      assert.equal(item.query_vector.length, 768);
    }
  });

  await t.test('real pipeline identity: production retrieval-card text shape is preserved', async () => {
    const retrievalCardPath = resolve(WORKSPACE_ROOT, 'wevibe-mcp', 'dist', 'retrieval-card.js');
    const retrieval = await import(pathToFileURL(retrievalCardPath).href);
    const corpus = readJsonFile(CORPUS_PATH);
    const memory = corpus.memories.find((entry) => entry.id === 'gc_goroutine_leak_unbuffered_send');

    assert.ok(memory, 'fixture memory gc_goroutine_leak_unbuffered_send must exist');

    const parsed = retrieval.parseMemoryText(memory.text);
    const stack = memory.stack_hint
      .split(',')
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);

    const cardText = retrieval.buildRetrievalCard({
      implement: parsed.implement,
      context: parsed.context,
      dnd: parsed.dnd,
      stack,
    });

    const expected = `Applies when: unspecified\nStack: ${stack.join(', ')}\nImplement: ${parsed.implement}`;
    assert.equal(cardText, expected);
  });

  await t.test('fail-loud: wrong memory count and missing expected_slug both throw', async () => {
    const scratch = mkdtempSync(join(tmpdir(), 'rb1a-dataset-identity-'));

    const corpus = readJsonFile(CORPUS_PATH);
    const shortCorpusPath = resolve(scratch, 'short-corpus.json');
    writeFileSync(
      shortCorpusPath,
      `${JSON.stringify({ ...corpus, memories: corpus.memories.slice(0, 11) }, null, 2)}\n`,
      'utf8',
    );

    await assert.rejects(
      () => buildDataset({ corpusPath: shortCorpusPath, goldPath: GOLD_PATH }),
      /corpus memory count mismatch: expected 12, got 11/,
    );

    const goldCases = readJsonlFile(GOLD_PATH);
    const brokenGoldCases = goldCases.map((entry, index) => {
      if (index !== 0) {
        return entry;
      }
      return {
        ...entry,
        expected_slugs: ['missing_slug_for_fail_loud_test'],
      };
    });

    const brokenGoldPath = resolve(scratch, 'missing-slug.gold.jsonl');
    writeFileSync(
      brokenGoldPath,
      `${brokenGoldCases.map((entry) => JSON.stringify(entry)).join('\n')}\n`,
      'utf8',
    );

    await assert.rejects(
      () =>
        buildDataset({
          corpusPath: CORPUS_PATH,
          goldPath: brokenGoldPath,
        }),
      /references unknown expected_slug missing_slug_for_fail_loud_test/,
    );
  });
});
