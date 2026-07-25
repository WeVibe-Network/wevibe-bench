import { buildSubmitCommitmentMsg } from '../vendor/chain-client';

function hasBytes(haystack: Uint8Array, needle: Uint8Array): boolean {
  if (needle.length === 0) {
    return true;
  }
  for (let i = 0; i <= haystack.length - needle.length; i += 1) {
    let matched = true;
    for (let j = 0; j < needle.length; j += 1) {
      if (haystack[i + j] !== needle[j]) {
        matched = false;
        break;
      }
    }
    if (matched) {
      return true;
    }
  }
  return false;
}

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

function run(): void {
  const producerModel = 'tencent/hy3';
  const producerModelBytes = Buffer.from(producerModel, 'utf8');
  const encodedWithProducer = buildSubmitCommitmentMsg(
    'leader-address',
    'org-1',
    Uint8Array.from(Buffer.from('11'.repeat(32), 'hex')),
    [{ keyword: 'alpha', weight: '1.0' }],
    'contrib-pubkey',
    'contrib-wallet',
    'memory',
    1,
    producerModel,
  ).value;

  const expectedProducerField = Uint8Array.from([
    0x82,
    0x01,
    producerModelBytes.length,
    ...producerModelBytes,
  ]);
  assert(
    hasBytes(encodedWithProducer, expectedProducerField),
    'expected producer_model_id field bytes (0x82 0x01 + len + UTF-8 slug) were not found',
  );

  const encodedWithoutProducer = buildSubmitCommitmentMsg(
    'leader-address',
    'org-1',
    Uint8Array.from(Buffer.from('22'.repeat(32), 'hex')),
    [{ keyword: 'beta', weight: '1.0' }],
    'contrib-pubkey',
    'contrib-wallet',
    'memory',
    1,
    '   ',
  ).value;
  assert(
    !hasBytes(encodedWithoutProducer, Uint8Array.from([0x82, 0x01])),
    'producer_model_id field tag (0x82 0x01) must be absent when producerModelId is empty',
  );

  process.stdout.write('ok: chain-client producer_model_id encoding\n');
}

run();
