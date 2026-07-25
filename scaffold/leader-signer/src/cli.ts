import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { TxMsgData } from 'cosmjs-types/cosmos/base/abci/v1beta1/abci';
import {
  buildAddMemberMsg,
  buildApproveMemoryMsg,
  buildRegisterOrgMsg,
  buildSubmitCommitmentMsg,
  directBroadcastWithSigner,
  EncodeObject,
} from '../vendor/chain-client';
import { createCommandLogger, CommandLogger, formatErrorForLog } from './log';
import { deriveLeaderWallet, getSeedFingerprint } from './wallet';

const DEFAULT_HUB_URL = 'http://127.0.0.1:4440';
const DEFAULT_MCP_URL = 'http://127.0.0.1:4550';
const DEFAULT_TOKEN_FILE = '~/.wevibe/mcp-session-token';
const DEFAULT_CHAIN_RPC = 'http://localhost:26657';
const DEFAULT_CHAIN_REST = 'http://localhost:1317';
const DEFAULT_FUND_AMOUNT = '100000000';
const REGISTER_ORG_STORAGE_QUOTA = 1000;
const REGISTER_ORG_RETRIEVAL_BUDGET = 500;
const MSG_REGISTER_ORG_RESPONSE_TYPE_URL = '/wevibe.org.v1.MsgRegisterOrgResponse';

type CliFlags = Record<string, string>;
const BOOLEAN_FLAG_NAMES = new Set(['can-contribute', 'can-moderate']);

interface OrgSetupPayload {
  leader_pubkey: string;
  leader_x25519_pubkey: string;
  leader_wallet: string;
  org_name: string;
  domain: string;
  fee_model: unknown;
  enc_envelope: string;
  search_envelope: string;
  mod_envelope: string;
  umbral_pk: string;
  pk_mod: string;
  signature: string;
}

interface OrgSetupResponse {
  setup_id: string;
  payload: OrgSetupPayload;
}

interface RegisterOrgOptions {
  orgId?: string;
  orgName: string;
  domain: string;
  description: string;
  techStack: string;
  focusAreas: string;
  fundAmount: bigint;
}

interface AddMemberOptions {
  orgId: string;
  memberPubkey: string;
  x25519Pubkey: string;
  role: string;
  canContribute: boolean;
  canModerate: boolean;
}

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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function usage(): string {
  return [
    'Usage:',
    '  derive-address [--seed-hex <hex>]',
    '  wallet-address [--seed-hex <hex>]',
    '  register-org --org-name <s> --domain <s> [--description <s>] [--tech-stack <s>] [--focus-areas <s>] [--fund-amount <uvibe>] [--seed-hex <hex>]',
    '  add-member --org-id <id> --member-pubkey <hex> --x25519 <hex> [--role <s>] [--can-contribute <bool>] [--can-moderate <bool>] [--seed-hex <hex>]',
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
      if (BOOLEAN_FLAG_NAMES.has(key)) {
        flags[key] = 'true';
        continue;
      }
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

function expandHomePath(filePath: string): string {
  if (filePath === '~') {
    return homedir();
  }
  if (filePath.startsWith('~/')) {
    return `${homedir()}${filePath.slice(1)}`;
  }
  return filePath;
}

function parsePositiveInteger(value: string, label: string): bigint {
  if (!/^[0-9]+$/.test(value)) {
    throw new Error(`${label} must be an integer string`);
  }

  const parsed = BigInt(value);
  if (parsed <= 0n) {
    throw new Error(`${label} must be greater than zero`);
  }

  return parsed;
}

function parseBooleanFlag(flags: CliFlags, name: string, defaultValue: boolean): boolean {
  const raw = flags[name];
  if (raw === undefined) {
    return defaultValue;
  }

  const normalized = raw.trim().toLowerCase();
  if (normalized === 'true' || normalized === '1') {
    return true;
  }
  if (normalized === 'false' || normalized === '0') {
    return false;
  }

  throw new Error(`Flag --${name} must be one of: true, false, 1, 0`);
}

function parseAmount(value: unknown, label: string): bigint {
  if (typeof value !== 'string' || !/^[0-9]+$/.test(value)) {
    throw new Error(`${label} must be an integer string`);
  }
  return BigInt(value);
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

function describeBody(body: unknown): string {
  if (body === undefined) {
    return 'empty response body';
  }
  if (typeof body === 'string') {
    return body;
  }
  return JSON.stringify(body);
}

async function requestJson(
  url: string,
  init: RequestInit,
  context: string,
): Promise<{ status: number; body: unknown }> {
  const response = await fetch(url, init);
  const responseText = await response.text();

  let body: unknown;
  if (responseText.length > 0) {
    try {
      body = JSON.parse(responseText);
    } catch {
      body = responseText;
    }
  }

  if (!response.ok) {
    throw new Error(`${context} failed (${response.status}): ${describeBody(body)}`);
  }

  return { status: response.status, body };
}

async function getUvibeBalance(chainRest: string, address: string): Promise<bigint> {
  const url = `${chainRest.replace(/\/$/, '')}/cosmos/bank/v1beta1/balances/${encodeURIComponent(address)}`;
  const { body } = await requestJson(url, { method: 'GET' }, 'balance query');
  const parsed = ensureObject(body, 'balance response');
  const balances = parsed.balances;
  if (!Array.isArray(balances)) {
    return 0n;
  }

  for (const coin of balances) {
    const coinObj = ensureObject(coin, 'balance coin');
    if (coinObj.denom === 'uvibe') {
      return parseAmount(coinObj.amount, 'uvibe amount');
    }
  }

  return 0n;
}

async function readMcpToken(tokenFilePath: string): Promise<string> {
  const content = await readFile(tokenFilePath, 'utf8');
  const token = content.trim();
  if (!token) {
    throw new Error(`Token file is empty: ${tokenFilePath}`);
  }
  return token;
}

function readVarint(bytes: Uint8Array, startOffset: number): { value: number; nextOffset: number } {
  let offset = startOffset;
  let value = 0;
  let shift = 0;

  while (offset < bytes.length) {
    const byte = bytes[offset];
    value |= (byte & 0x7f) << shift;
    offset += 1;

    if ((byte & 0x80) === 0) {
      return { value, nextOffset: offset };
    }

    shift += 7;
    if (shift > 28) {
      throw new Error('MsgRegisterOrgResponse varint is too large');
    }
  }

  throw new Error('Unexpected EOF while decoding MsgRegisterOrgResponse');
}

function decodeRegisterOrgResponseOrgId(responseBytes: Uint8Array): string {
  let offset = 0;

  while (offset < responseBytes.length) {
    const tag = readVarint(responseBytes, offset);
    offset = tag.nextOffset;

    const fieldNumber = tag.value >>> 3;
    const wireType = tag.value & 0x07;

    if (fieldNumber === 1 && wireType === 2) {
      const length = readVarint(responseBytes, offset);
      offset = length.nextOffset;
      const endOffset = offset + length.value;
      if (endOffset > responseBytes.length) {
        throw new Error('Invalid MsgRegisterOrgResponse org_id length');
      }
      return new TextDecoder().decode(responseBytes.slice(offset, endOffset));
    }

    if (wireType === 0) {
      const skip = readVarint(responseBytes, offset);
      offset = skip.nextOffset;
      continue;
    }

    if (wireType === 2) {
      const length = readVarint(responseBytes, offset);
      offset = length.nextOffset + length.value;
      if (offset > responseBytes.length) {
        throw new Error('Invalid MsgRegisterOrgResponse field length');
      }
      continue;
    }

    throw new Error(`Unsupported MsgRegisterOrgResponse wire type: ${wireType}`);
  }

  throw new Error('MsgRegisterOrgResponse missing org_id');
}

function extractOrgIdFromDeliverTxData(deliverTxData?: Uint8Array): string {
  if (!deliverTxData || deliverTxData.length === 0) {
    throw new Error('DeliverTx data missing; cannot decode MsgRegisterOrgResponse');
  }

  const txMsgData = TxMsgData.decode(deliverTxData);
  const registerOrgResponse = txMsgData.msgResponses.find(
    (response) => response.typeUrl === MSG_REGISTER_ORG_RESPONSE_TYPE_URL,
  );

  if (!registerOrgResponse) {
    throw new Error('MsgRegisterOrgResponse missing in DeliverTx msgResponses');
  }

  return decodeRegisterOrgResponseOrgId(registerOrgResponse.value);
}

async function fallbackFetchOrgId(chainRest: string): Promise<string> {
  const url = `${chainRest.replace(/\/$/, '')}/wevibe/org/v1/org/wevibe-org-0`;
  const { body } = await requestJson(url, { method: 'GET' }, 'org lookup fallback');
  const parsed = ensureObject(body, 'org lookup fallback response');
  if (typeof parsed.org_id === 'string' && parsed.org_id.trim().length > 0) {
    return parsed.org_id.trim();
  }

  if (parsed.org && typeof parsed.org === 'object' && parsed.org !== null) {
    const orgObj = parsed.org as Record<string, unknown>;
    if (typeof orgObj.id === 'string' && orgObj.id.trim().length > 0) {
      return orgObj.id.trim();
    }
  }

  throw new Error('Fallback org lookup did not return org_id');
}

function parseOrgSetupResponse(body: unknown): OrgSetupResponse {
  const response = ensureObject(body, 'org-setup response');
  const setup_id = ensureString(response.setup_id, 'org-setup.setup_id');
  const payloadObj = ensureObject(response.payload, 'org-setup.payload');

  return {
    setup_id,
    payload: {
      leader_pubkey: ensureString(payloadObj.leader_pubkey, 'payload.leader_pubkey'),
      leader_x25519_pubkey: ensureString(payloadObj.leader_x25519_pubkey, 'payload.leader_x25519_pubkey'),
      leader_wallet: ensureString(payloadObj.leader_wallet, 'payload.leader_wallet'),
      org_name: ensureString(payloadObj.org_name, 'payload.org_name'),
      domain: ensureString(payloadObj.domain, 'payload.domain'),
      fee_model: payloadObj.fee_model ?? null,
      enc_envelope: ensureString(payloadObj.enc_envelope, 'payload.enc_envelope'),
      search_envelope: ensureString(payloadObj.search_envelope, 'payload.search_envelope'),
      mod_envelope: ensureString(payloadObj.mod_envelope, 'payload.mod_envelope'),
      umbral_pk: ensureString(payloadObj.umbral_pk, 'payload.umbral_pk'),
      pk_mod: ensureString(payloadObj.pk_mod, 'payload.pk_mod'),
      signature: ensureString(payloadObj.signature, 'payload.signature'),
    },
  };
}

function normalizeRegisterOrgOptions(flags: CliFlags): RegisterOrgOptions {
  const orgId = flags['org-id']?.trim();
  const orgName = getRequiredFlag(flags, 'org-name');
  const domain = getRequiredFlag(flags, 'domain');
  const description = flags.description ?? '';
  const techStack = flags['tech-stack'] ?? '';
  const focusAreas = flags['focus-areas'] ?? '';
  const fundAmount = parsePositiveInteger(flags['fund-amount'] ?? DEFAULT_FUND_AMOUNT, '--fund-amount');

  return {
    orgId,
    orgName,
    domain,
    description,
    techStack,
    focusAreas,
    fundAmount,
  };
}

function normalizeAddMemberOptions(flags: CliFlags): AddMemberOptions {
  const orgId = getRequiredFlag(flags, 'org-id');
  const memberPubkey = getRequiredFlag(flags, 'member-pubkey');
  const x25519Pubkey = getRequiredFlag(flags, 'x25519');
  const role = flags.role?.trim() || 'member';
  const canContribute = parseBooleanFlag(flags, 'can-contribute', true);
  const canModerate = parseBooleanFlag(flags, 'can-moderate', false);

  return {
    orgId,
    memberPubkey,
    x25519Pubkey,
    role,
    canContribute,
    canModerate,
  };
}

function fingerprintHex(value: string, label: string): string {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    throw new Error(`${label} cannot be empty`);
  }
  if (!/^[0-9a-fA-F]+$/.test(trimmed)) {
    throw new Error(`${label} must be hex`);
  }
  if (trimmed.length % 2 !== 0) {
    throw new Error(`${label} must have even-length hex`);
  }
  return createHash('sha256').update(Buffer.from(trimmed, 'hex')).digest('hex').slice(0, 8);
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

async function runWalletAddress(flags: CliFlags, logger: CommandLogger): Promise<Record<string, unknown>> {
  const seedHex = getSeedHex(flags);
  const seedFp = getSeedFingerprint(seedHex);
  await logger.progress('wallet-address: deriving leader wallet', { seed_fp: seedFp });
  const { address } = await deriveLeaderWallet(seedHex);
  await logger.progress('wallet-address: derived', { seed_fp: seedFp, wallet_address: address });
  return { address };
}

async function runDeriveAddress(flags: CliFlags): Promise<void> {
  const seedHex = getSeedHex(flags);
  const seedFp = getSeedFingerprint(seedHex);
  const { address } = await deriveLeaderWallet(seedHex);
  console.log(JSON.stringify({ address, seed_fp: seedFp }));
}

async function runRegisterOrg(flags: CliFlags, logger: CommandLogger): Promise<Record<string, unknown>> {
  const seedHex = getSeedHex(flags);
  const seedFp = getSeedFingerprint(seedHex);
  const opts = normalizeRegisterOrgOptions(flags);

  const hubUrl = envOrDefault('HUB_URL', DEFAULT_HUB_URL);
  const mcpUrl = envOrDefault('WEVIBE_MCP_URL', DEFAULT_MCP_URL);
  const tokenFile = expandHomePath(envOrDefault('WEVIBE_MCP_TOKEN_FILE', DEFAULT_TOKEN_FILE));
  const chainRpc = envOrDefault('WEVIBE_CHAIN_RPC', DEFAULT_CHAIN_RPC);
  const chainRest = envOrDefault('WEVIBE_CHAIN_REST', DEFAULT_CHAIN_REST);
  process.env.WEVIBE_CHAIN_RPC = chainRpc;

  await logger.progress('register-org: start', {
    seed_fp: seedFp,
    org_name: opts.orgName,
    domain: opts.domain,
    description_size: opts.description.length,
    tech_stack_size: opts.techStack.length,
    focus_areas_size: opts.focusAreas.length,
    fund_amount_uvibe: opts.fundAmount.toString(),
    hub_url: hubUrl,
    mcp_url: mcpUrl,
    chain_rpc: chainRpc,
    chain_rest: chainRest,
  });

  const { signer, address } = await deriveLeaderWallet(seedHex);
  await logger.progress('register-org: wallet derived', {
    seed_fp: seedFp,
    wallet_address: address,
  });

  if (opts.orgId) {
    const url = `${chainRest.replace(/\/$/, '')}/wevibe/org/v1/org/${encodeURIComponent(opts.orgId)}`;
    const resp = await fetch(url);
    if (!resp.ok) {
      throw new Error(`register-org --org-id ${opts.orgId}: org not found on chain (HTTP ${resp.status})`);
    }
    const body = (await resp.json()) as { org_id?: string };
    const resolved = (body.org_id ?? '').trim();
    if (!resolved) {
      throw new Error(`register-org --org-id ${opts.orgId}: chain returned no org_id`);
    }
    await logger.progress('register-org: reuse existing org', { org_id: resolved });
    return { org_id: resolved, tx_hash: 'reuse-existing', leader_wallet: address, reused: true };
  }

  let uvibeBalance = await getUvibeBalance(chainRest, address);
  await logger.progress('register-org: current balance fetched', {
    wallet_address: address,
    uvibe_balance: uvibeBalance.toString(),
  });

  if (uvibeBalance < opts.fundAmount) {
    const amountAsNumber = Number(opts.fundAmount);
    if (!Number.isSafeInteger(amountAsNumber)) {
      throw new Error('--fund-amount exceeds Number.MAX_SAFE_INTEGER');
    }

    await logger.progress('register-org: funding via faucet', {
      wallet_address: address,
      current_uvibe: uvibeBalance.toString(),
      target_uvibe: opts.fundAmount.toString(),
    });

    await requestJson(
      `${hubUrl.replace(/\/$/, '')}/v1/faucet/fund`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          address,
          amount: amountAsNumber,
        }),
      },
      'faucet fund request',
    );

    for (let attempt = 1; attempt <= 8; attempt += 1) {
      await sleep(1000);
      uvibeBalance = await getUvibeBalance(chainRest, address);
      await logger.progress('register-org: funding poll', {
        attempt,
        uvibe_balance: uvibeBalance.toString(),
      });
      if (uvibeBalance >= opts.fundAmount) {
        break;
      }
    }

    if (uvibeBalance < opts.fundAmount) {
      throw new Error(
        `Faucet funding not reflected in balance. balance=${uvibeBalance.toString()} target=${opts.fundAmount.toString()}`,
      );
    }
  } else {
    await logger.progress('register-org: faucet skipped (already funded)', {
      uvibe_balance: uvibeBalance.toString(),
      required_uvibe: opts.fundAmount.toString(),
    });
  }

  const servingAddressResponse = await requestJson(
    `${hubUrl.replace(/\/$/, '')}/v1/hub/serving-address`,
    { method: 'GET' },
    'hub serving-address request',
  );
  const servingAddressObj = ensureObject(servingAddressResponse.body, 'serving-address response');
  const hubServingKey = ensureString(servingAddressObj.serving_address, 'serving_address').trim();
  if (!hubServingKey) {
    throw new Error('serving_address is empty');
  }

  await logger.progress('register-org: got hub serving address', {
    hub_serving_key_size: hubServingKey.length,
  });

  const mcpToken = await readMcpToken(tokenFile);
  await logger.progress('register-org: loaded mcp token file', {
    token_file: tokenFile,
    token_size: mcpToken.length,
  });

  const orgSetupResponse = await requestJson(
    `${mcpUrl.replace(/\/$/, '')}/v1/org-setup`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${mcpToken}`,
      },
      body: JSON.stringify({
        org_name: opts.orgName,
        domain: opts.domain,
        leader_wallet: address,
      }),
    },
    'mcp org-setup request',
  );

  const orgSetup = parseOrgSetupResponse(orgSetupResponse.body);
  await logger.progress('register-org: org-setup received', {
    setup_id: orgSetup.setup_id,
    payload_sizes: {
      leader_pubkey: orgSetup.payload.leader_pubkey.length,
      leader_x25519_pubkey: orgSetup.payload.leader_x25519_pubkey.length,
      leader_wallet: orgSetup.payload.leader_wallet.length,
      enc_envelope: orgSetup.payload.enc_envelope.length,
      search_envelope: orgSetup.payload.search_envelope.length,
      mod_envelope: orgSetup.payload.mod_envelope.length,
      umbral_pk: orgSetup.payload.umbral_pk.length,
      pk_mod: orgSetup.payload.pk_mod.length,
      signature: orgSetup.payload.signature.length,
    },
  });

  const registerOrgMsg = buildRegisterOrgMsg({
    signer: address,
    leader: orgSetup.payload.leader_pubkey,
    storageQuota: REGISTER_ORG_STORAGE_QUOTA,
    retrievalBudget: REGISTER_ORG_RETRIEVAL_BUDGET,
    domain: orgSetup.payload.domain,
    hubServingKey,
    leaderWallet: orgSetup.payload.leader_wallet,
    name: orgSetup.payload.org_name,
    description: opts.description,
    tech_stack: opts.techStack,
    focus_areas: opts.focusAreas,
  });

  await logger.progress('register-org: built MsgRegisterOrg', {
    type_url: registerOrgMsg.typeUrl,
    msg_value_bytes: registerOrgMsg.value.length,
  });

  const broadcastResult = await directBroadcastWithSigner(signer, address, [registerOrgMsg]);
  await logger.progress('register-org: chain tx broadcast+included', {
    tx_hash: broadcastResult.txHash,
    code: broadcastResult.code,
    raw_log_size: broadcastResult.rawLog.length,
    deliver_tx_data_bytes: broadcastResult.deliverTxData?.length ?? 0,
  });

  let orgId = extractOrgIdFromDeliverTxData(broadcastResult.deliverTxData).trim();
  if (!orgId) {
    await logger.progress('register-org: empty org_id decode, using fallback lookup', {
      fallback_endpoint: `${chainRest.replace(/\/$/, '')}/wevibe/org/v1/org/wevibe-org-0`,
    });
    orgId = await fallbackFetchOrgId(chainRest);
  }

  await logger.progress('register-org: decoded org_id', { org_id: orgId });

  await requestJson(
    `${mcpUrl.replace(/\/$/, '')}/v1/org-setup/finalize`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${mcpToken}`,
      },
      body: JSON.stringify({
        setup_id: orgSetup.setup_id,
        org_id: orgId,
      }),
    },
    'mcp org-setup finalize request',
  );

  await logger.progress('register-org: finalized mcp org setup', {
    setup_id: orgSetup.setup_id,
    org_id: orgId,
  });

  const recordBody = {
    leader_pubkey: orgSetup.payload.leader_pubkey,
    leader_x25519_pubkey: orgSetup.payload.leader_x25519_pubkey,
    leader_wallet: orgSetup.payload.leader_wallet,
    org_name: orgSetup.payload.org_name,
    domain: orgSetup.payload.domain,
    description: opts.description,
    tech_stack: opts.techStack,
    focus_areas: opts.focusAreas,
    fee_model: orgSetup.payload.fee_model,
    enc_envelope: orgSetup.payload.enc_envelope,
    search_envelope: orgSetup.payload.search_envelope,
    mod_envelope: orgSetup.payload.mod_envelope,
    umbral_pk: orgSetup.payload.umbral_pk,
    pk_mod: orgSetup.payload.pk_mod,
    signature: orgSetup.payload.signature,
    hub_serving_key: hubServingKey,
    org_id: orgId,
    tx_hash: broadcastResult.txHash,
  };

  const recordResponse = await requestJson(
    `${hubUrl.replace(/\/$/, '')}/v1/orgs`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(recordBody),
    },
    'hub org persistence request',
  );

  if (recordResponse.status !== 201) {
    throw new Error(`hub org persistence expected status 201, got ${recordResponse.status}`);
  }

  await logger.progress('register-org: persisted org in hub', {
    org_id: orgId,
    tx_hash: broadcastResult.txHash,
    status: recordResponse.status,
  });

  return {
    org_id: orgId,
    tx_hash: broadcastResult.txHash,
    leader_wallet: address,
    hub_serving_key: hubServingKey,
  };
}

async function runAddMember(flags: CliFlags, logger: CommandLogger): Promise<Record<string, unknown>> {
  const opts = normalizeAddMemberOptions(flags);
  const seedHex = getSeedHex(flags);
  const seedFp = getSeedFingerprint(seedHex);
  const chainRpc = envOrDefault('WEVIBE_CHAIN_RPC', DEFAULT_CHAIN_RPC);
  process.env.WEVIBE_CHAIN_RPC = chainRpc;

  const memberPubkeyFp = fingerprintHex(opts.memberPubkey, '--member-pubkey');
  const x25519PubkeyFp = fingerprintHex(opts.x25519Pubkey, '--x25519');
  await logger.progress('add-member: start', {
    seed_fp: seedFp,
    org_id: opts.orgId,
    member_pubkey_fp: memberPubkeyFp,
    member_pubkey_size: opts.memberPubkey.length,
    x25519_pubkey_fp: x25519PubkeyFp,
    x25519_pubkey_size: opts.x25519Pubkey.length,
    role: opts.role,
    can_contribute: opts.canContribute,
    can_moderate: opts.canModerate,
    chain_rpc: chainRpc,
  });

  const { signer, address } = await deriveLeaderWallet(seedHex);
  await logger.progress('add-member: wallet derived', {
    seed_fp: seedFp,
    wallet_address: address,
  });

  const addMemberMsg = buildAddMemberMsg(
    address,
    opts.orgId,
    opts.memberPubkey,
    opts.role,
    opts.x25519Pubkey,
    opts.canContribute,
    opts.canModerate,
  );
  await logger.progress('add-member: built MsgAddMember', {
    org_id: opts.orgId,
    member_pubkey_fp: memberPubkeyFp,
    x25519_pubkey_fp: x25519PubkeyFp,
    role: opts.role,
    can_contribute: opts.canContribute,
    can_moderate: opts.canModerate,
    type_url: addMemberMsg.typeUrl,
    msg_value_bytes: addMemberMsg.value.length,
  });

  const broadcast = await directBroadcastWithSigner(signer, address, [addMemberMsg]);
  await logger.progress('add-member: chain tx broadcast+included', {
    org_id: opts.orgId,
    member_pubkey_fp: memberPubkeyFp,
    tx_hash: broadcast.txHash,
    code: broadcast.code,
    raw_log_size: broadcast.rawLog.length,
  });

  if (broadcast.code !== 0) {
    throw new Error(`add-member tx failed code=${broadcast.code}: ${broadcast.rawLog}`);
  }

  return {
    tx_hash: broadcast.txHash,
    code: broadcast.code,
  };
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
    if (command === 'derive-address') {
      await runDeriveAddress(flags);
    } else if (command === 'wallet-address') {
      result = await runWalletAddress(flags, logger);
    } else if (command === 'register-org') {
      result = await runRegisterOrg(flags, logger);
    } else if (command === 'add-member') {
      result = await runAddMember(flags, logger);
    } else if (command === 'commit-batch') {
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
