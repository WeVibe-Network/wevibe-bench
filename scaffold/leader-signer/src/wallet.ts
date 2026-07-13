import { createHash } from 'node:crypto';
import { Bip39, stringToPath } from '@cosmjs/crypto';
import { DirectSecp256k1HdWallet, OfflineSigner } from '@cosmjs/proto-signing';

const SEED_BYTES_LENGTH = 32;
const HD_PATH = "m/44'/118'/0'/0/0";
const ADDRESS_PREFIX = 'wevibe';

function parseSeedHex(seedHex: string): Uint8Array {
  const trimmed = seedHex.trim();
  if (!/^[0-9a-fA-F]+$/.test(trimmed)) {
    throw new Error('WEVIBE_IDENTITY_SEED_HEX must be valid hex');
  }

  if (trimmed.length !== SEED_BYTES_LENGTH * 2) {
    throw new Error('WEVIBE_IDENTITY_SEED_HEX must be exactly 32 bytes (64 hex chars)');
  }

  const seed = Buffer.from(trimmed, 'hex');
  if (seed.length !== SEED_BYTES_LENGTH) {
    throw new Error('WEVIBE_IDENTITY_SEED_HEX decoded length mismatch');
  }

  return Uint8Array.from(seed);
}

export function getSeedFingerprint(seedHex: string): string {
  const seedBytes = parseSeedHex(seedHex);
  return createHash('sha256').update(seedBytes).digest('hex').slice(0, 8);
}

export async function deriveLeaderWallet(seedHex: string): Promise<{ signer: OfflineSigner; address: string }> {
  const seed = Buffer.from(parseSeedHex(seedHex));
  const mnemonic = Bip39.encode(seed).toString();
  const signer = await DirectSecp256k1HdWallet.fromMnemonic(mnemonic, {
    prefix: ADDRESS_PREFIX,
    hdPaths: [stringToPath(HD_PATH)],
  });
  const [account] = await signer.getAccounts();
  if (!account) {
    throw new Error('Derived wallet has no account');
  }

  return { signer, address: account.address };
}
