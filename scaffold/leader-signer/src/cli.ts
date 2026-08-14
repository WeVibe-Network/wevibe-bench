import {
  buildApproveMemoryMsg,
  buildSubmitCommitmentMsg,
  directBroadcastWithSigner,
  EncodeObject,
} from '../vendor/chain-client';
import { createCommandLogger, CommandLogger, formatErrorForLog } from './log';
import { deriveLeaderWallet, getSeedFingerprint } from './wallet';

const DEFAULT_CHAIN_RPC = 'http://localhost:26657';

type CliFlags = Record<string, string>;

interface BatchEntry {
  submission_hash: string;
  keywords: string[];
  contributor_pubkey: string;
  contributor_wallet: string;
  memory_type: string;
  mc_version: number;
  encrypted_blob: string;
  committing_leader: string;
  wrapped_dek_enc: string;
  plaintext_hash: string;
  salt: string;
  ciphertext_hash: string;
  contributor_sig: string;
}

function usage(): string {
  return [
    'Usage:',
    '  commit-batch --org-id <id> [--producer-model-id <slug>] [--seed-hex <hex>]   (reads JSON from stdin)',
  ].join('\n');
}

function parseArgs(argv: string[]): { command: string; flags: CliFlags } {
  if (argv.length === 0) {
    throw new Error(`Missing command.\n${usage()}`);
  }

  const [command, ...rest] = argv;
  const flags: CliFlags = {};

  for (let i = 0; i < rest.length; i += 1) {
    const token = rest[i];
    if (!token.startsWith('--')) {
      throw new Error(`Unexpected positional argument: ${token}`);
    }

    const key = token.slice(2);
    if (!key) {
      throw new Error('Encountered empty flag name');
    }

    const next = rest[i + 1];
    if (!next || next.startsWith('--')) {
      throw new Error(`Flag --${key} requires a value`);
    }

    flags[key] = next;
    i += 1;
  }

  return { command, flags };
}

function getRequiredFlag(flags: CliFlags, name: string): string {
  const value = flags[name]?.trim();
  if (!value) {
    throw new Error(`Missing required flag --${name}`);
  }
  return value;
}

function getSeedHex(flags: CliFlags): string {
  const fromFlag = flags['seed-hex']?.trim();
  const fromEnv = process.env.WEVIBE_IDENTITY_SEED_HEX?.trim();
  const seedHex = fromFlag || fromEnv;
  if (!seedHex) {
    throw new Error('Missing seed. Provide --seed-hex or set WEVIBE_IDENTITY_SEED_HEX');
  }
  return seedHex;
}

function envOrDefault(name: string, fallback: string): string {
  const raw = process.env[name]?.trim();
  return raw && raw.length > 0 ? raw : fallback;
}

function ensureObject(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function ensureString(value: unknown, label: string): string {
  if (typeof value !== 'string') {
    throw new Error(`${label} must be a string`);
  }
  return value;
}

function ensureStringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value)) {
    throw new Error(`${label} must be an array of strings`);
  }

  const result: string[] = [];
  for (let i = 0; i < value.length; i += 1) {
    if (typeof value[i] !== 'string') {
      throw new Error(`${label}[${i}] must be a string`);
    }
    result.push(value[i]);
  }
  return result;
}

function hexToBytes(hex: string, fieldName: string): Uint8Array {
  const trimmed = hex.trim();
  if (trimmed.length === 0) {
    throw new Error(`${fieldName} cannot be empty`);
  }
  if (!/^[0-9a-fA-F]+$/.test(trimmed)) {
    throw new Error(`${fieldName} must be hex`);
  }
  if (trimmed.length % 2 !== 0) {
    throw new Error(`${fieldName} must have even-length hex`);
  }
  return Uint8Array.from(Buffer.from(trimmed, 'hex'));
}

function parseMcVersion(value: unknown, fieldName: string): number {
  if (value === null || value === undefined) {
    return 0;
  }

  if (typeof value === 'number' && Number.isInteger(value) && value >= 0) {
    return value;
  }

  if (typeof value === 'string' && /^[0-9]+$/.test(value)) {
    return Number(value);
  }

  throw new Error(`${fieldName} must be a non-negative integer`);
}

function parseBatchEntry(raw: unknown, index: number): BatchEntry {
  const entry = ensureObject(raw, `batch[${index}]`);
  return {
    submission_hash: ensureString(entry.submission_hash, `batch[${index}].submission_hash`),
    keywords: ensureStringArray(entry.keywords, `batch[${index}].keywords`),
    contributor_pubkey: ensureString(entry.contributor_pubkey, `batch[${index}].contributor_pubkey`),
    contributor_wallet: ensureString(entry.contributor_wallet, `batch[${index}].contributor_wallet`),
    memory_type: ensureString(entry.memory_type, `batch[${index}].memory_type`),
    mc_version: parseMcVersion(entry.mc_version, `batch[${index}].mc_version`),
    encrypted_blob: ensureString(entry.encrypted_blob, `batch[${index}].encrypted_blob`),
    committing_leader: ensureString(entry.committing_leader, `batch[${index}].committing_leader`),
    wrapped_dek_enc: ensureString(entry.wrapped_dek_enc, `batch[${index}].wrapped_dek_enc`),
    plaintext_hash: ensureString(entry.plaintext_hash, `batch[${index}].plaintext_hash`),
    salt: ensureString(entry.salt, `batch[${index}].salt`),
    ciphertext_hash: ensureString(entry.ciphertext_hash, `batch[${index}].ciphertext_hash`),
    contributor_sig: ensureString(entry.contributor_sig, `batch[${index}].contributor_sig`),
  };
}

async function readBatchFromStdin(): Promise<BatchEntry[]> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    chunks.push(typeof chunk === 'string' ? Buffer.from(chunk) : chunk);
  }

  const raw = Buffer.concat(chunks).toString('utf8').trim();
  if (raw.length === 0) {
    throw new Error('commit-batch requires JSON on stdin');
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error('Failed to parse commit-batch stdin JSON');
  }

  if (Array.isArray(parsed)) {
    return parsed.map((entry, index) => parseBatchEntry(entry, index));
  }

  const parsedObj = ensureObject(parsed, 'commit-batch stdin');
  if (!Array.isArray(parsedObj.batch)) {
    throw new Error('commit-batch stdin object must contain a batch array');
  }

  if (parsedObj.verification !== undefined && parsedObj.verification !== 'passed') {
    throw new Error('commit-batch stdin verification must be "passed" when present');
  }

  return parsedObj.batch.map((entry, index) => parseBatchEntry(entry, index));
}

async function runCommitBatch(flags: CliFlags, logger: CommandLogger): Promise<Record<string, unknown>> {
  const seedHex = getSeedHex(flags);
  const seedFp = getSeedFingerprint(seedHex);
  const orgId = getRequiredFlag(flags, 'org-id');
  const producerModelId = flags['producer-model-id']?.trim();
  const chainRpc = envOrDefault('WEVIBE_CHAIN_RPC', DEFAULT_CHAIN_RPC);
  process.env.WEVIBE_CHAIN_RPC = chainRpc;

  const entries = await readBatchFromStdin();
  await logger.progress('commit-batch: parsed stdin batch', {
    seed_fp: seedFp,
    org_id: orgId,
    producer_model_id: producerModelId || null,
    entry_count: entries.length,
    chain_rpc: chainRpc,
  });

  const { signer, address } = await deriveLeaderWallet(seedHex);
  await logger.progress('commit-batch: wallet derived', {
    seed_fp: seedFp,
    wallet_address: address,
  });

  const allMsgs: EncodeObject[] = [];
  let totalPayloadBytes = 0;

  for (let i = 0; i < entries.length; i += 1) {
    const entry = entries[i];
    const contentHash = hexToBytes(entry.submission_hash, `batch[${i}].submission_hash`);
    const encryptedBlob = hexToBytes(entry.encrypted_blob, `batch[${i}].encrypted_blob`);
    const wrappedDekEnc = hexToBytes(entry.wrapped_dek_enc, `batch[${i}].wrapped_dek_enc`);
    const plaintextHash = hexToBytes(entry.plaintext_hash, `batch[${i}].plaintext_hash`);
    const salt = hexToBytes(entry.salt, `batch[${i}].salt`);
    const ciphertextHash = hexToBytes(entry.ciphertext_hash, `batch[${i}].ciphertext_hash`);
    const contributorSig = hexToBytes(entry.contributor_sig, `batch[${i}].contributor_sig`);

    const submitMsg = buildSubmitCommitmentMsg(
      address,
      orgId,
      contentHash,
      entry.keywords.map((keyword) => ({ keyword, weight: '1.0' })),
      entry.contributor_pubkey,
      entry.contributor_wallet,
      entry.memory_type,
      entry.mc_version,
      producerModelId,
    );

    const approveMsg = buildApproveMemoryMsg(
      address,
      orgId,
      contentHash,
      encryptedBlob,
      entry.committing_leader,
      wrappedDekEnc,
      plaintextHash,
      salt,
      ciphertextHash,
      contributorSig,
      entry.memory_type,
      entry.mc_version,
    );

    allMsgs.push(submitMsg, approveMsg);
    totalPayloadBytes +=
      contentHash.length +
      encryptedBlob.length +
      wrappedDekEnc.length +
      plaintextHash.length +
      salt.length +
      ciphertextHash.length +
      contributorSig.length;
  }

  await logger.progress('commit-batch: built tx messages', {
    org_id: orgId,
    msg_count: allMsgs.length,
    entry_count: entries.length,
    aggregate_binary_field_bytes: totalPayloadBytes,
  });

  const broadcast = await directBroadcastWithSigner(signer, address, allMsgs);
  await logger.progress('commit-batch: chain tx broadcast+included', {
    tx_hash: broadcast.txHash,
    code: broadcast.code,
    raw_log_size: broadcast.rawLog.length,
    msg_count: allMsgs.length,
  });

  if (broadcast.code !== 0) {
    throw new Error(`commit-batch tx failed code=${broadcast.code}: ${broadcast.rawLog}`);
  }

  return {
    tx_hash: broadcast.txHash,
    code: broadcast.code,
    msg_count: allMsgs.length,
  };
}

export async function runCli(argv: string[]): Promise<number> {
  const logger = await createCommandLogger(argv[0] ?? 'cli');
  await logger.info('leader-signer log opened', { log_file: logger.logFilePath });

  try {
    const { command, flags } = parseArgs(argv);
    await logger.progress('command dispatch', { command });

    let result: Record<string, unknown> | undefined;
    if (command === 'commit-batch') {
      result = await runCommitBatch(flags, logger);
    } else {
      throw new Error(`Unknown command: ${command}\n${usage()}`);
    }

    await logger.progress('command complete', { command });
    if (result !== undefined) {
      process.stdout.write(`${JSON.stringify(result)}\n`);
    }
    return 0;
  } catch (error) {
    await logger.error('command failed', {
      error: formatErrorForLog(error),
    });
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    return 1;
  }
}
