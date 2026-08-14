import { SigningStargateClient, defaultRegistryTypes } from '@cosmjs/stargate';
import { OfflineSigner, Registry, GeneratedType } from '@cosmjs/proto-signing';
import { toBase64 } from '@cosmjs/encoding';
import { TxRaw } from 'cosmjs-types/cosmos/tx/v1beta1/tx';
import { getConfig } from './config';

export interface EncodeObject {
  typeUrl: string;
  value: Uint8Array;
}

export const WEVIBE_MSG_TYPE_URLS: string[] = [
  '/wevibe.memory.v1.MsgSubmitCommitment',
  '/wevibe.memory.v1.MsgApproveMemory',
  '/wevibe.memory.v1.MsgReportMemory',
  '/wevibe.org.v1.MsgRegisterOrg',
  '/wevibe.org.v1.MsgAddMember',
  '/wevibe.org.v1.MsgRemoveMember',
  '/wevibe.org.v1.MsgSetOrgConfig',
  '/wevibe.org.v1.MsgSetMemberCapabilities',
  '/wevibe.org.v1.MsgTransferLeadership',
  '/wevibe.org.v1.MsgCloseOrg',
  '/wevibe.org.v1.MsgSetServingKey',
  '/wevibe.org.v1.MsgSetServingInfo',
  '/wevibe.reputation.v1.MsgIncrementContribution',
  '/wevibe.reputation.v1.MsgIncrementServe',
  '/wevibe.reputation.v1.MsgRecordBan',
];

export function getChainRpcEndpoint(): string {
  let rpc = getConfig().chainRpc;
  rpc = rpc.replace(/^tcp:\/\//, 'http://');
  rpc = rpc.replace(/^rpc\./, 'http://rpc.');
  if (!rpc.startsWith('http')) {
    rpc = 'http://' + rpc;
  }
  return rpc;
}

interface OrgAccountQueryResponse {
  account_address?: string;
}

export async function getOrgAccountAddress(orgId: string): Promise<string> {
  const chainRest = getConfig().chainRest;
  const response = await fetch(`${chainRest}/wevibe/org/v1/account/${encodeURIComponent(orgId)}`);

  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({ error: response.statusText, message: response.statusText }));
    throw new Error(errorBody.error ?? errorBody.message ?? `Chain REST error ${response.status}`);
  }

  const body = (await response.json()) as OrgAccountQueryResponse;
  const accountAddress = body.account_address?.trim();
  if (!accountAddress) {
    throw new Error('Chain REST response missing account_address');
  }

  return accountAddress;
}

// The wevibe Msg `value` fields are already-encoded protobuf bytes (hand-rolled
// encoders above). This passthrough GeneratedType returns those bytes verbatim so
// CosmJS's Registry can place them into the tx body without re-encoding.
const rawProtoType: GeneratedType = {
  encode: (message: unknown) => {
    const bytes = message instanceof Uint8Array ? message : Uint8Array.from((message as number[]) ?? []);
    return { finish: () => bytes } as unknown as ReturnType<GeneratedType['encode']>;
  },
  decode: (input: unknown) => input,
  fromPartial: (object: unknown) => object,
};

function buildWevibeRegistry(): Registry {
  const registry = new Registry(defaultRegistryTypes);
  for (const typeUrl of WEVIBE_MSG_TYPE_URLS) {
    registry.register(typeUrl, rawProtoType);
  }
  return registry;
}

export async function getSigningClient(signer: OfflineSigner): Promise<SigningStargateClient> {
  const rpc = getChainRpcEndpoint();
  return SigningStargateClient.connectWithSigner(rpc, signer, { registry: buildWevibeRegistry() });
}

interface KeywordWeightInput {
  keyword: string;
  weight?: string;
}

function encodeRepeatedStringField(tag: number, values: string[]): number[] {
  const fields: number[] = [];
  for (const value of values) {
    fields.push(...encodeStringField(tag, value));
  }
  return fields;
}

function encodeVarint(value: number): number[] {
  const result: number[] = [];
  let v = value;
  while (v > 0x7f) {
    result.push((v & 0x7f) | 0x80);
    v = Math.floor(v / 128);
  }
  result.push(v & 0x7f);
  return result;
}

function encodeStringField(tag: number, value: string): number[] {
  const bytes = [...Buffer.from(value)];
  return [tag, ...encodeVarint(bytes.length), ...bytes];
}

function encodeBoolField(tag: number, value: boolean): number[] {
  return [tag, ...encodeVarint(value ? 1 : 0)];
}

function encodeBytesField(tag: number, value: string): number[] {
  const bytes = [...Buffer.from(value, 'hex')];
  return [tag, ...encodeVarint(bytes.length), ...bytes];
}

function encodeBytesFieldFromBytes(tag: number, value: Uint8Array): number[] {
  const bytes = [...value];
  return [tag, ...encodeVarint(bytes.length), ...bytes];
}

function encodeNestedField(tag: number, fields: number[]): number[] {
  return [tag, ...encodeVarint(fields.length), ...fields];
}

function mapMemoryType(memoryType: string): number {
  if (memoryType !== 'memory') {
    throw new Error(`unsupported memory_type: ${memoryType}`);
  }
  return 1;
}

export function buildSubmitCommitmentMsg(
  signer: string,
  orgId: string,
  contentHash: Uint8Array,
  keywords: KeywordWeightInput[],
  contributorId: string,
  contributorWallet: string,
  memoryType: string,
  mcVersion: number,
  producerModelId?: string,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
    ...encodeBytesFieldFromBytes(0x1a, contentHash),
  ];

  for (const keyword of keywords) {
    const nested: number[] = [
      ...encodeStringField(0x0a, keyword.keyword),
      ...encodeStringField(0x12, keyword.weight ?? '1.0'),
    ];
    fields.push(...encodeNestedField(0x22, nested));
  }

  fields.push(
    ...encodeStringField(0x2a, contributorId),
    ...encodeStringField(0x32, contributorWallet),
    ...encodeVarint(0x38),
    ...encodeVarint(mapMemoryType(memoryType)),
    ...encodeVarint(0x50),
    ...encodeVarint(mcVersion),
  );

  const normalizedProducerModelId = producerModelId?.trim() ?? '';
  if (normalizedProducerModelId.length > 0) {
    // field 16 (producer_model_id), wire type 2 (length-delimited)
    // tag = (16 << 3) | 2 = 130 => varint bytes 0x82 0x01
    fields.push(...encodeVarint(130), ...encodeVarint(Buffer.byteLength(normalizedProducerModelId)), ...Buffer.from(normalizedProducerModelId));
  }

  return {
    typeUrl: '/wevibe.memory.v1.MsgSubmitCommitment',
    value: Uint8Array.from(fields),
  };
}

export function buildApproveMemoryMsg(
  signer: string,
  orgId: string,
  contentHash: Uint8Array,
  encryptedBlob: Uint8Array,
  committingLeader: string,
  wrappedDekEnc: Uint8Array,
  plaintextHash: Uint8Array,
  salt: Uint8Array,
  ciphertextHash: Uint8Array,
  contributorSig: Uint8Array,
  memoryType: string,
  mcVersion: number,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
    ...encodeBytesFieldFromBytes(0x1a, contentHash),
    ...encodeBytesFieldFromBytes(0x22, encryptedBlob),
    ...encodeStringField(0x32, committingLeader),
    ...encodeBytesFieldFromBytes(0x3a, wrappedDekEnc),
    ...encodeVarint(0x40),
    ...encodeVarint(mapMemoryType(memoryType)),
    ...encodeBytesFieldFromBytes(0x4a, plaintextHash),
    ...encodeBytesFieldFromBytes(0x52, salt),
    ...encodeBytesFieldFromBytes(0x5a, ciphertextHash),
    ...encodeBytesFieldFromBytes(0x62, contributorSig),
    ...encodeVarint(0x78),
    ...encodeVarint(mcVersion),
  ];

  return {
    typeUrl: '/wevibe.memory.v1.MsgApproveMemory',
    value: Uint8Array.from(fields),
  };
}

export function buildReportMemoryMsg(args: {
  signer: string;
  orgId: string;
  contentHash: Uint8Array;
  contributorPubkey: string;
  approvingModerators: string[];
  upholdingModerators: string[];
  reporterPubkey: string;
  reason: string;
  plaintext?: Uint8Array;
  ciphertext?: Uint8Array;
  capsule?: Uint8Array;
  plaintextHash?: Uint8Array;
  plaintextOversized?: boolean;
}): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, args.signer),
    ...encodeStringField(0x12, args.orgId),
    ...encodeBytesFieldFromBytes(0x1a, args.contentHash),
    ...encodeStringField(0x22, args.contributorPubkey),
  ];

  for (const mod of args.approvingModerators) {
    fields.push(...encodeStringField(0x2a, mod));
  }
  for (const mod of args.upholdingModerators) {
    fields.push(...encodeStringField(0x32, mod));
  }

  fields.push(
    ...encodeStringField(0x3a, args.reporterPubkey),
    ...encodeStringField(0x42, args.reason),
  );

  if (args.plaintext && args.plaintext.length > 0) {
    fields.push(...encodeBytesFieldFromBytes(0x4a, args.plaintext));
  }
  if (args.ciphertext && args.ciphertext.length > 0) {
    fields.push(...encodeBytesFieldFromBytes(0x52, args.ciphertext));
  }
  if (args.capsule && args.capsule.length > 0) {
    fields.push(...encodeBytesFieldFromBytes(0x5a, args.capsule));
  }
  if (args.plaintextHash && args.plaintextHash.length > 0) {
    fields.push(...encodeBytesFieldFromBytes(0x62, args.plaintextHash));
  }
  if (args.plaintextOversized) {
    fields.push(...encodeVarint(0x68), ...encodeVarint(1));
  }

  return {
    typeUrl: '/wevibe.memory.v1.MsgReportMemory',
    value: Uint8Array.from(fields),
  };
}

export function buildRemoveMemberMsg(
  signer: string,
  orgId: string,
  pubkey: string,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
    ...encodeStringField(0x1a, pubkey),
  ];

  return {
    typeUrl: '/wevibe.org.v1.MsgRemoveMember',
    value: Uint8Array.from(fields),
  };
}

export function buildSetMemberCapabilitiesMsg(
  signer: string,
  orgId: string,
  pubkey: string,
  canContribute: boolean,
  canModerate: boolean,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
    ...encodeStringField(0x1a, pubkey),
    ...encodeBoolField(0x20, canContribute),
    ...encodeBoolField(0x28, canModerate),
  ];

  return {
    typeUrl: '/wevibe.org.v1.MsgSetMemberCapabilities',
    value: Uint8Array.from(fields),
  };
}

export function buildTransferLeadershipMsg(
  signer: string,
  orgId: string,
  newLeaderPubkey: string,
  newLeaderWallet: string,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
    ...encodeStringField(0x1a, newLeaderPubkey),
    ...encodeStringField(0x22, newLeaderWallet),
  ];

  return {
    typeUrl: '/wevibe.org.v1.MsgTransferLeadership',
    value: Uint8Array.from(fields),
  };
}

export function buildCloseOrgMsg(
  signer: string,
  orgId: string,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
  ];

  return {
    typeUrl: '/wevibe.org.v1.MsgCloseOrg',
    value: Uint8Array.from(fields),
  };
}

export function buildSetOrgConfigMsg(
  signer: string,
  orgId: string,
  serveReceiptRequired: boolean,
  minContributionsPerEpoch: number,
  contestStakeVibe: number,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
    ...encodeVarint(0x18), ...encodeVarint(serveReceiptRequired ? 1 : 0),
    ...encodeVarint(0x20), ...encodeVarint(minContributionsPerEpoch),
    ...encodeVarint(0x28), ...encodeVarint(contestStakeVibe),
  ];

  return {
    typeUrl: '/wevibe.org.v1.MsgSetOrgConfig',
    value: Uint8Array.from(fields),
  };
}

export function buildSetServingKeyMsg(
  signer: string,
  orgId: string,
  newServingKey: string,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
    ...encodeStringField(0x1a, newServingKey),
  ];

  return {
    typeUrl: '/wevibe.org.v1.MsgSetServingKey',
    value: Uint8Array.from(fields),
  };
}

export function buildSetServingInfoMsg(
  signer: string,
  orgId: string,
  hubEndpoints: string[],
  hubResponsePubkey: string,
): EncodeObject {
  const fields: number[] = [
    ...encodeStringField(0x0a, signer),
    ...encodeStringField(0x12, orgId),
    ...encodeRepeatedStringField(0x1a, hubEndpoints),
  ];

  const responsePubkey = hubResponsePubkey.trim();
  if (responsePubkey.length > 0) {
    fields.push(...encodeStringField(0x22, responsePubkey));
  }

  return {
    typeUrl: '/wevibe.org.v1.MsgSetServingInfo',
    value: Uint8Array.from(fields),
  };
}

const RPC_REQUEST_TIMEOUT_MS = 10_000;
const TX_POLL_INTERVAL_MS = 1_000;
const TX_POLL_TIMEOUT_MS = 30_000;
const ACCOUNT_SEQUENCE_MISMATCH_CODE = 32;

function decodeRpcDataField(data: unknown): Uint8Array | undefined {
  if (data == null) {
    return undefined;
  }

  if (data instanceof Uint8Array) {
    return data.length > 0 ? data : undefined;
  }

  if (Array.isArray(data)) {
    if (data.length === 0) {
      return undefined;
    }
    return Uint8Array.from(data.map((value) => Number(value) & 0xff));
  }

  if (typeof data === 'string') {
    const trimmed = data.trim();
    if (trimmed === '') {
      return undefined;
    }

    const normalizedHex = trimmed.startsWith('0x') ? trimmed.slice(2) : trimmed;
    if (/^[0-9a-fA-F]+$/.test(normalizedHex) && normalizedHex.length % 2 === 0) {
      const hexBytes = Buffer.from(normalizedHex, 'hex');
      return hexBytes.length > 0 ? Uint8Array.from(hexBytes) : undefined;
    }

    const base64Bytes = Buffer.from(trimmed, 'base64');
    return base64Bytes.length > 0 ? Uint8Array.from(base64Bytes) : undefined;
  }

  return undefined;
}

function extractErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === 'string') {
    return error;
  }
  if (error == null) {
    return '';
  }

  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

function isAbortError(error: unknown): boolean {
  if (!error || typeof error !== 'object') {
    return false;
  }

  const name = (error as { name?: unknown }).name;
  return typeof name === 'string' && name === 'AbortError';
}

function isDuplicateBroadcastMessage(message: string): boolean {
  const normalized = message.toLowerCase();
  return (
    normalized.includes('tx already exists in cache') ||
    normalized.includes('already known') ||
    normalized.includes('tx already in mempool')
  );
}

function isSequenceMismatchMessage(message: string, code?: number): boolean {
  const normalized = message.toLowerCase();
  return (
    code === ACCOUNT_SEQUENCE_MISMATCH_CODE ||
    normalized.includes('account sequence mismatch') ||
    normalized.includes('incorrect account sequence') ||
    normalized.includes('wrong sequence')
  );
}

function isWalletRejectedMessage(message: string): boolean {
  const normalized = message.toLowerCase();
  return (
    normalized.includes('rejected') ||
    normalized.includes('denied') ||
    normalized.includes('declined') ||
    normalized.includes('request rejected')
  );
}

function mapChainError(rawLog: string, code?: number): string {
  const detail = rawLog.trim();
  const normalized = detail.toLowerCase();

  if (normalized.startsWith('transaction not confirmed within')) {
    return detail;
  }

  if (isWalletRejectedMessage(normalized)) {
    return 'Transaction rejected in wallet.';
  }

  if (isDuplicateBroadcastMessage(normalized)) {
    return 'Transaction already submitted; confirming…';
  }

  if (isSequenceMismatchMessage(normalized, code)) {
    return 'Account sequence mismatch. Please retry the transaction.';
  }

  if (
    normalized.includes('insufficient fee') ||
    normalized.includes('insufficient funds') ||
    normalized.includes('insufficient balance') ||
    normalized.includes('insufficient coins')
  ) {
    return 'Insufficient funds/fees for this transaction.';
  }

  if (normalized.includes('out of gas') || code === 11) {
    return 'Transaction ran out of gas. Please retry.';
  }

  if (
    normalized.includes('unauthorized') ||
    normalized.includes('signature verification failed') ||
    normalized.includes('invalid signature')
  ) {
    return 'Unauthorized or invalid signature. Please reconnect your wallet and retry.';
  }

  if (detail.length > 0) {
    return `Chain transaction failed${code !== undefined ? ` (code ${code})` : ''}: ${detail}`;
  }

  return `Chain transaction failed${code !== undefined ? ` (code ${code})` : ''}.`;
}

async function rpcRequest(method: string, params: Record<string, unknown>): Promise<unknown> {
  const controller = new AbortController();
  const timeoutHandle = setTimeout(() => controller.abort(), RPC_REQUEST_TIMEOUT_MS);

  try {
    const response = await fetch(`${getChainRpcEndpoint()}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method,
        params,
      }),
      signal: controller.signal,
    });

    const payload = await response.json().catch(() => {
      throw new Error(`Invalid RPC response for ${method}`);
    });

    if (!response.ok) {
      const rpcError =
        payload?.error?.data ?? payload?.error?.message ?? `HTTP ${response.status} ${response.statusText}`;
      throw new Error(String(rpcError));
    }

    return payload;
  } catch (error) {
    if (isAbortError(error)) {
      throw new Error(`RPC request timed out after ${Math.floor(RPC_REQUEST_TIMEOUT_MS / 1000)}s`);
    }
    throw error;
  } finally {
    clearTimeout(timeoutHandle);
  }
}

async function computeTxHash(txBytes: Uint8Array): Promise<{ hashBytes: Uint8Array; hashHex: string }> {
  if (!globalThis.crypto?.subtle) {
    throw new Error('Web Crypto API is unavailable for transaction hashing');
  }

  const digestInput = new Uint8Array(txBytes.length);
  digestInput.set(txBytes);
  const digest = await globalThis.crypto.subtle.digest('SHA-256', digestInput);
  const hashBytes = new Uint8Array(digest);
  return {
    hashBytes,
    hashHex: Buffer.from(hashBytes).toString('hex').toUpperCase(),
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface TxInclusionResult {
  rawLog: string;
  deliverTxData?: Uint8Array;
}

async function pollForInclusion(txHashHex: string, txHashBytes: Uint8Array): Promise<TxInclusionResult> {
  const txHashBase64 = toBase64(txHashBytes);
  const deadline = Date.now() + TX_POLL_TIMEOUT_MS;

  while (Date.now() < deadline) {
    let payload:
      | {
          error?: { message?: string; data?: string };
          result?: {
            tx_result?: { code?: number; log?: string; raw_log?: string; data?: unknown };
            TxResult?: { code?: number; log?: string; raw_log?: string; data?: unknown };
          };
        }
      | undefined;

    try {
      payload = (await rpcRequest('tx', {
        hash: txHashBase64,
        prove: false,
      })) as {
        error?: { message?: string; data?: string };
        result?: {
          tx_result?: { code?: number; log?: string; raw_log?: string; data?: unknown };
          TxResult?: { code?: number; log?: string; raw_log?: string; data?: unknown };
        };
      };
    } catch (error) {
      // Any polling RPC error before inclusion is treated as not-yet-committed.
      await sleep(TX_POLL_INTERVAL_MS);
      continue;
    }

    if (payload?.error) {
      await sleep(TX_POLL_INTERVAL_MS);
      continue;
    }

    const txResult = payload?.result?.tx_result ?? payload?.result?.TxResult;
    if (txResult) {
      const deliverCode = Number(txResult.code ?? 0);
      const rawLog = String(txResult.log ?? txResult.raw_log ?? '');

      if (deliverCode !== 0) {
        throw new Error(mapChainError(rawLog, deliverCode));
      }

      return {
        rawLog,
        deliverTxData: decodeRpcDataField(txResult.data),
      };
    }

    await sleep(TX_POLL_INTERVAL_MS);
  }

  throw new Error(
    mapChainError(`Transaction not confirmed within 30s (hash ${txHashHex}). It may still land — check before retrying.`),
  );
}

export async function directBroadcast(
  walletAddress: string,
  msgs: EncodeObject[],
  feeGranter?: string,
): Promise<{ txHash: string; code: number; rawLog: string; deliverTxData?: Uint8Array }> {
  const { getOfflineSigner } = await import('./wallet-connect');
  const chainId = getConfig().chainId;
  let signer: OfflineSigner;
  try {
    signer = getOfflineSigner(chainId);
  } catch (error) {
    throw new Error(mapChainError(extractErrorMessage(error)));
  }

  return directBroadcastWithSigner(signer, walletAddress, msgs, feeGranter);
}

export async function directBroadcastWithSigner(
  signer: OfflineSigner,
  walletAddress: string,
  msgs: EncodeObject[],
  feeGranter?: string,
): Promise<{ txHash: string; code: number; rawLog: string; deliverTxData?: Uint8Array }> {

  let account: { address: string } | undefined;
  try {
    [account] = await signer.getAccounts();
  } catch (error) {
    throw new Error(mapChainError(extractErrorMessage(error)));
  }

  if (!account) {
    throw new Error(mapChainError('No account found'));
  }
  if (account.address !== walletAddress) {
    throw new Error(mapChainError('Signer account mismatch for requested wallet address'));
  }

  // Gas must scale with the transaction: a batch of N memories is 2N messages,
  // so a fixed limit cannot work. Simulate the tx to get the real gas cost and
  // apply a safety buffer. Fall back to a generous per-message budget only if
  // the node's simulation service is unreachable. Never go below a sane floor.
  const GAS_PRICE_UVIBE = 0.025; // matches average gasPriceStep
  const GAS_SIM_BUFFER = 1.5;
  const GAS_FLOOR = 200_000;
  const GAS_PER_MSG_FALLBACK = 150_000;

  const normalizedFeeGranter = feeGranter?.trim();
  const sequenceRetryMax = 1;

  for (let sequenceRetry = 0; sequenceRetry <= sequenceRetryMax; sequenceRetry += 1) {
    let client: SigningStargateClient;
    try {
      client = await getSigningClient(signer);
    } catch (error) {
      throw new Error(mapChainError(extractErrorMessage(error)));
    }

    let gasLimit: number;
    try {
      const simulatedGas = await client.simulate(account.address, msgs, '');
      gasLimit = Math.ceil(simulatedGas * GAS_SIM_BUFFER);
    } catch {
      gasLimit = GAS_FLOOR + msgs.length * GAS_PER_MSG_FALLBACK;
    }
    if (gasLimit < GAS_FLOOR) {
      gasLimit = GAS_FLOOR;
    }
    const feeAmount = Math.max(1, Math.ceil(gasLimit * GAS_PRICE_UVIBE));

    const fee: {
      amount: { denom: string; amount: string }[];
      gas: string;
      granter?: string;
    } = {
      amount: [{ denom: 'uvibe', amount: String(feeAmount) }],
      gas: String(gasLimit),
    };

    if (normalizedFeeGranter) {
      fee.granter = normalizedFeeGranter;
    }

    let txBytes: Uint8Array;
    let txHashHex: string;
    let txHashBytes: Uint8Array;

    try {
      const txRaw = await client.sign(account.address, msgs, fee, '');
      txBytes = TxRaw.encode(txRaw).finish();
      const computedHash = await computeTxHash(txBytes);
      txHashHex = computedHash.hashHex;
      txHashBytes = computedHash.hashBytes;
    } catch (error) {
      throw new Error(mapChainError(extractErrorMessage(error)));
    }

    let rpcPayload: {
      error?: { message?: string; data?: string };
      result?: { code?: number; log?: string; raw_log?: string; codespace?: string; hash?: string };
    };

    try {
      rpcPayload = (await rpcRequest('broadcast_tx_sync', { tx: toBase64(txBytes) })) as {
        error?: { message?: string; data?: string };
        result?: { code?: number; log?: string; raw_log?: string; codespace?: string; hash?: string };
      };
    } catch (error) {
      throw new Error(mapChainError(extractErrorMessage(error)));
    }

    const rpcResult = rpcPayload?.result ?? {};
    const checkCode = rpcResult.code === undefined ? undefined : Number(rpcResult.code);
    const checkLog = String(rpcResult.log ?? rpcResult.raw_log ?? rpcResult.codespace ?? '');
    const rpcErrorText = String(rpcPayload?.error?.data ?? rpcPayload?.error?.message ?? '');
    const checkText = `${checkLog} ${rpcErrorText}`.trim();

    if (isSequenceMismatchMessage(checkText, checkCode)) {
      if (sequenceRetry < sequenceRetryMax) {
        continue;
      }
      throw new Error(mapChainError('Account sequence mismatch after retry. Please refresh and try again.', checkCode));
    }

    const acceptedIntoMempool = (checkCode === 0 && !rpcPayload?.error) || isDuplicateBroadcastMessage(checkText);
    if (!acceptedIntoMempool) {
      throw new Error(mapChainError(checkText || 'broadcast_tx_sync failed', checkCode));
    }

    const inclusion = await pollForInclusion(txHashHex, txHashBytes);
    return {
      txHash: txHashHex,
      code: 0,
      rawLog: inclusion.rawLog,
      deliverTxData: inclusion.deliverTxData,
    };
  }

  throw new Error(
    mapChainError('Account sequence mismatch after retry. Please refresh and try again.', ACCOUNT_SEQUENCE_MISMATCH_CODE),
  );
}
