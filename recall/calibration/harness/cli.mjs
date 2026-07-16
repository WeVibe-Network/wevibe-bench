#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { buildDataset } from './dataset.mjs';
import { classifyLostPositives, fineBandDiagnostic, kneeCandidates } from './diagnostics.mjs';
import { runLiveGate, writeConfirmation } from './livegate.mjs';
import { runSweep } from './sweep.mjs';

const LOG_PREFIX = '[RB1A-HARNESS]';
const HARNESS_DIR = dirname(fileURLToPath(import.meta.url));
const BENCH_ROOT = resolve(HARNESS_DIR, '..', '..', '..');
const RECALL_ROOT = resolve(BENCH_ROOT, 'recall');

const DEFAULT_CORPUS_REL = 'recall/corpus/go-concurrency-v1.json';
const DEFAULT_GOLD_REL = 'recall/gold/go-concurrency-v1.gold.jsonl';

function errorMessage(error) {
  if (error instanceof Error) {
    return error.stack ?? error.message;
  }
  return String(error);
}

function usageText() {
  return [
    'RB-1a calibration harness CLI',
    '',
    'Usage:',
    '  node recall/calibration/harness/cli.mjs build [--corpus <path>] [--gold <path>] [--cid-map <path>] --out <runs-path>',
    '  node recall/calibration/harness/cli.mjs sweep --dataset <path> --out <runs-path>',
    '  node recall/calibration/harness/cli.mjs diagnose --dataset <path>',
    '  node recall/calibration/harness/cli.mjs livegate --gold <path> --cid-map <path> --floor <f> --sim-positive <n> --mcp-url <url> --token <path> --org <id> --out <runs-path>',
    '',
    'Defaults (relative to wevibe-bench root):',
    `  --corpus ${DEFAULT_CORPUS_REL}`,
    `  --gold   ${DEFAULT_GOLD_REL}`,
    '',
    'Notes:',
    '  --out is required and must resolve to a runs/ style non-committed path.',
    '  This command prints content-safe output only (counts/model/dim/vector fingerprints).',
  ].join('\n');
}

function isPathInside(parentPath, maybeChildPath) {
  const rel = relative(parentPath, maybeChildPath);
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel));
}

function hasRunsSegment(filePath) {
  return filePath.split(sep).filter(Boolean).includes('runs');
}

function resolveFromBenchRoot(inputPath, flagLabel) {
  if (typeof inputPath !== 'string' || inputPath.trim().length === 0) {
    throw new Error(`${flagLabel} requires a non-empty path`);
  }
  return isAbsolute(inputPath) ? inputPath : resolve(BENCH_ROOT, inputPath);
}

function assertAllowedOutputPath(outPath) {
  if (!hasRunsSegment(outPath)) {
    throw new Error(
      `--out must include a "runs" path segment (non-committed output required); got ${outPath}`,
    );
  }

  if (isPathInside(RECALL_ROOT, outPath)) {
    throw new Error(`--out resolves inside committed recall/ tree (${outPath}); use runs/`);
  }

  if (isPathInside(BENCH_ROOT, outPath)) {
    const rel = relative(BENCH_ROOT, outPath);
    if (!rel.split(sep).includes('runs')) {
      throw new Error(`--out inside wevibe-bench must be under runs/; got ${outPath}`);
    }
  }
}

function requireFlagValue(args, index, flagName) {
  const value = args[index + 1];
  if (!value || value.startsWith('--')) {
    throw new Error(`${flagName} requires a value`);
  }
  return value;
}

function parseBuildArgs(args) {
  let corpusPath = resolveFromBenchRoot(DEFAULT_CORPUS_REL, '--corpus');
  let goldPath = resolveFromBenchRoot(DEFAULT_GOLD_REL, '--gold');
  let cidMapPath = null;
  let outPath = null;

  for (let i = 0; i < args.length; i += 1) {
    const token = args[i];
    if (token === '--corpus') {
      corpusPath = resolveFromBenchRoot(requireFlagValue(args, i, '--corpus'), '--corpus');
      i += 1;
      continue;
    }
    if (token === '--gold') {
      goldPath = resolveFromBenchRoot(requireFlagValue(args, i, '--gold'), '--gold');
      i += 1;
      continue;
    }
    if (token === '--cid-map') {
      cidMapPath = resolveFromBenchRoot(requireFlagValue(args, i, '--cid-map'), '--cid-map');
      i += 1;
      continue;
    }
    if (token === '--out') {
      outPath = resolveFromBenchRoot(requireFlagValue(args, i, '--out'), '--out');
      i += 1;
      continue;
    }

    throw new Error(`Unknown build argument: ${token}`);
  }

  if (!outPath) {
    throw new Error('build requires --out <runs-path>');
  }

  assertAllowedOutputPath(outPath);

  return {
    corpusPath,
    goldPath,
    cidMapPath,
    outPath,
  };
}

function parseSweepArgs(args) {
  let datasetPath = null;
  let outPath = null;

  for (let i = 0; i < args.length; i += 1) {
    const token = args[i];

    if (token === '--dataset') {
      datasetPath = resolveFromBenchRoot(requireFlagValue(args, i, '--dataset'), '--dataset');
      i += 1;
      continue;
    }

    if (token === '--out') {
      outPath = resolveFromBenchRoot(requireFlagValue(args, i, '--out'), '--out');
      i += 1;
      continue;
    }

    throw new Error(`Unknown sweep argument: ${token}`);
  }

  if (!datasetPath) {
    throw new Error('sweep requires --dataset <path>');
  }

  if (!outPath) {
    throw new Error('sweep requires --out <runs-path>');
  }

  assertAllowedOutputPath(outPath);
  return { datasetPath, outPath };
}

function parseDiagnoseArgs(args) {
  let datasetPath = null;

  for (let i = 0; i < args.length; i += 1) {
    const token = args[i];

    if (token === '--dataset') {
      datasetPath = resolveFromBenchRoot(requireFlagValue(args, i, '--dataset'), '--dataset');
      i += 1;
      continue;
    }

    throw new Error(`Unknown diagnose argument: ${token}`);
  }

  if (!datasetPath) {
    throw new Error('diagnose requires --dataset <path>');
  }

  return { datasetPath };
}

function parseFloorFlag(raw) {
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    throw new Error(`--floor must be a finite number in [0,1]; got ${raw}`);
  }
  return Number(parsed.toFixed(6));
}

function parseSimPositiveFlag(raw) {
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 16) {
    throw new Error(`--sim-positive must be an integer in [0,16]; got ${raw}`);
  }
  return parsed;
}

function parseLiveGateArgs(args) {
  let goldPath = null;
  let cidMapPath = null;
  let floor = null;
  let simPositive = null;
  let mcpUrl = null;
  let tokenPath = null;
  let orgId = null;
  let outPath = null;

  for (let i = 0; i < args.length; i += 1) {
    const token = args[i];

    if (token === '--gold') {
      goldPath = resolveFromBenchRoot(requireFlagValue(args, i, '--gold'), '--gold');
      i += 1;
      continue;
    }

    if (token === '--cid-map') {
      cidMapPath = resolveFromBenchRoot(requireFlagValue(args, i, '--cid-map'), '--cid-map');
      i += 1;
      continue;
    }

    if (token === '--floor') {
      floor = parseFloorFlag(requireFlagValue(args, i, '--floor'));
      i += 1;
      continue;
    }

    if (token === '--sim-positive') {
      simPositive = parseSimPositiveFlag(requireFlagValue(args, i, '--sim-positive'));
      i += 1;
      continue;
    }

    if (token === '--mcp-url') {
      mcpUrl = requireFlagValue(args, i, '--mcp-url');
      i += 1;
      continue;
    }

    if (token === '--token') {
      tokenPath = resolveFromBenchRoot(requireFlagValue(args, i, '--token'), '--token');
      i += 1;
      continue;
    }

    if (token === '--org') {
      orgId = requireFlagValue(args, i, '--org').trim();
      i += 1;
      continue;
    }

    if (token === '--out') {
      outPath = resolveFromBenchRoot(requireFlagValue(args, i, '--out'), '--out');
      i += 1;
      continue;
    }

    throw new Error(`Unknown livegate argument: ${token}`);
  }

  if (!goldPath) {
    throw new Error('livegate requires --gold <path>');
  }
  if (!cidMapPath) {
    throw new Error('livegate requires --cid-map <path>');
  }
  if (floor === null) {
    throw new Error('livegate requires --floor <f>');
  }
  if (simPositive === null) {
    throw new Error('livegate requires --sim-positive <n>');
  }
  if (!mcpUrl || mcpUrl.trim().length === 0) {
    throw new Error('livegate requires --mcp-url <url>');
  }
  if (!tokenPath) {
    throw new Error('livegate requires --token <path>');
  }
  if (!orgId) {
    throw new Error('livegate requires --org <id>');
  }
  if (!outPath) {
    throw new Error('livegate requires --out <runs-path>');
  }

  assertAllowedOutputPath(outPath);

  return {
    goldPath,
    cidMapPath,
    floor,
    simPositive,
    mcpUrl: mcpUrl.trim(),
    tokenPath,
    orgId,
    outPath,
  };
}

function vectorFingerprint(vector) {
  return createHash('sha256').update(JSON.stringify(vector), 'utf8').digest('hex').slice(0, 8);
}

function readJsonFile(filePath, label) {
  let raw = '';
  try {
    raw = readFileSync(filePath, 'utf8');
  } catch (error) {
    throw new Error(`Failed to read ${label} file ${filePath}: ${errorMessage(error)}`);
  }

  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`Failed to parse ${label} file ${filePath}: ${errorMessage(error)}`);
  }
}

function printDatasetSummary(dataset, outPath) {
  console.log(`${LOG_PREFIX} build_complete out=${outPath}`);
  console.log(`${LOG_PREFIX} counts memories=${dataset.memories.length} cases=${dataset.cases.length}`);
  console.log(`${LOG_PREFIX} embedding model=${dataset.embedding_model} dim=${dataset.embedding_dim}`);

  for (const memory of dataset.memories) {
    console.log(`${LOG_PREFIX} doc_vector_fp slug=${memory.slug} fp=${vectorFingerprint(memory.doc_vector)}`);
  }

  for (const item of dataset.cases) {
    console.log(`${LOG_PREFIX} query_vector_fp case_id=${item.case_id} fp=${vectorFingerprint(item.query_vector)}`);
  }
}

async function runBuild(args) {
  const parsed = parseBuildArgs(args);
  const dataset = await buildDataset({
    corpusPath: parsed.corpusPath,
    goldPath: parsed.goldPath,
    cidMapPath: parsed.cidMapPath,
  });

  mkdirSync(dirname(parsed.outPath), { recursive: true });
  writeFileSync(parsed.outPath, `${JSON.stringify(dataset, null, 2)}\n`, 'utf8');

  printDatasetSummary(dataset, parsed.outPath);
}

function printSweepTable(sweepDoc) {
  console.log('');
  console.log('floor\trecall5_hit_rate\tprecision5\tzero_overall\tzero_positive\tzero_empty\texpected_empty_correct');
  for (const row of sweepDoc.floors) {
    console.log([
      Number(row.f).toFixed(2),
      (Number(row.recall_at_5_binary_hits) / Number(sweepDoc.denominators.positive)).toFixed(4),
      Number(row.precision_at_5 ?? 0).toFixed(4),
      Number(row.zero_injection_overall ?? 0).toFixed(4),
      Number(row.zero_injection_positive ?? 0).toFixed(4),
      Number(row.zero_injection_empty ?? 0).toFixed(4),
      `${row.expected_empty_correct}/${sweepDoc.denominators.expected_empty}`,
    ].join('\t'));
  }
  console.log('');
}

function formatInterval(interval) {
  if (!Array.isArray(interval) || interval.length !== 2) {
    return 'null';
  }

  return `${Number(interval[0]).toFixed(6)}..${Number(interval[1]).toFixed(6)}`;
}

async function runSweepCommand(args) {
  const parsed = parseSweepArgs(args);
  const dataset = readJsonFile(parsed.datasetPath, 'dataset');
  const sweepDoc = runSweep(dataset);

  mkdirSync(dirname(parsed.outPath), { recursive: true });
  writeFileSync(parsed.outPath, `${JSON.stringify(sweepDoc, null, 2)}\n`, 'utf8');

  console.log(`${LOG_PREFIX} sweep_complete out=${parsed.outPath}`);
  console.log(
    `${LOG_PREFIX} sweep_meta fixture=${sweepDoc.fixture_version} model=${sweepDoc.embedding_model} dim=${sweepDoc.embedding_dim}`,
  );
  console.log(`${LOG_PREFIX} near_tie_gate status=${sweepDoc.near_tie_gate.status}`);
  printSweepTable(sweepDoc);
}

async function runDiagnoseCommand(args) {
  const parsed = parseDiagnoseArgs(args);
  const dataset = readJsonFile(parsed.datasetPath, 'dataset');

  const sweepDoc = runSweep(dataset);
  const band = fineBandDiagnostic(dataset);
  const lost = classifyLostPositives(dataset, 0.75);
  const knees = kneeCandidates(sweepDoc.floors);

  const categories = lost.lost_cases.map((entry) => entry.category);

  console.log(
    `${LOG_PREFIX} diagnose_verdict verdict=${band.verdict} robust_band=${band.robust_band} width=${Number(band.width).toFixed(6)} interval=${formatInterval(band.admissible_interval)}`,
  );
  console.log(`${LOG_PREFIX} lost_positive_count count=${lost.lost_cases.length}`);
  console.log(
    `${LOG_PREFIX} lost_positive_categories categories=${categories.length > 0 ? categories.join(',') : 'none'}`,
  );

  for (const entry of knees) {
    const fStar = entry.f_star === null ? 'null' : Number(entry.f_star).toFixed(2);
    console.log(`${LOG_PREFIX} knee algorithm=${entry.algorithm} f_star=${fStar}`);
  }

  const fallback = 0.75;
  console.log(
    `${LOG_PREFIX} selection_policy knee_selected=${sweepDoc.knee_selected === null ? 'null' : sweepDoc.knee_selected} fallback=${fallback.toFixed(2)}`,
  );
}

async function runLiveGateCommand(args) {
  const parsed = parseLiveGateArgs(args);
  const confirmation = await runLiveGate({
    goldPath: parsed.goldPath,
    cidMapPath: parsed.cidMapPath,
    mcpUrl: parsed.mcpUrl,
    tokenPath: parsed.tokenPath,
    orgId: parsed.orgId,
    floor: parsed.floor,
    sim: parsed.simPositive,
  });

  writeConfirmation(confirmation, parsed.outPath);

  console.log(
    `${LOG_PREFIX} livegate_result status=${confirmation.pass ? 'PASS' : 'FAIL'} out=${parsed.outPath}`,
  );
  console.log(
    `${LOG_PREFIX} livegate_counts floor=${Number(confirmation.floor).toFixed(2)} positive=${confirmation.live_positive_binary_recall5}/${confirmation.denominators.positive} expected_empty=${confirmation.live_expected_empty_correct}/${confirmation.denominators.expected_empty} sim_positive=${confirmation.sim_positive_count}`,
  );
  console.log(
    `${LOG_PREFIX} livegate_gates positive=${confirmation.positive_gate_pass} empty=${confirmation.empty_gate_pass} pass=${confirmation.pass}`,
  );

  if (!confirmation.pass && Array.isArray(confirmation.diagnosis_order)) {
    console.log(
      `${LOG_PREFIX} livegate_diagnosis_order order=${confirmation.diagnosis_order.join(',')}`,
    );
  }

  if (!confirmation.pass) {
    process.exitCode = 1;
  }
}

async function main() {
  const argv = process.argv.slice(2);

  if (argv.length === 0 || argv[0] === '--help' || argv[0] === '-h' || argv[0] === 'help') {
    console.log(usageText());
    return;
  }

  const subcommand = argv[0];
  const args = argv.slice(1);

  if (subcommand === 'build') {
    if (args.includes('--help') || args.includes('-h')) {
      console.log(usageText());
      return;
    }
    await runBuild(args);
    return;
  }

  if (subcommand === 'sweep') {
    if (args.includes('--help') || args.includes('-h')) {
      console.log(usageText());
      return;
    }
    await runSweepCommand(args);
    return;
  }

  if (subcommand === 'diagnose') {
    if (args.includes('--help') || args.includes('-h')) {
      console.log(usageText());
      return;
    }
    await runDiagnoseCommand(args);
    return;
  }

  if (subcommand === 'livegate') {
    if (args.includes('--help') || args.includes('-h')) {
      console.log(usageText());
      return;
    }
    await runLiveGateCommand(args);
    return;
  }

  throw new Error(`Unknown subcommand: ${subcommand}`);
}

main().catch((error) => {
  console.error(`${LOG_PREFIX} fatal ${errorMessage(error)}`);
  process.exit(1);
});
