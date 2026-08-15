import { deriveLeaderWallet } from './wallet';

// Non-secret test vector: bytes 0x00..0x1f. Must match the seed used by the
// ed25519 half of the parity suite so both halves derive from one identity.
const TEST_SEED_HEX =
  '000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f';

// Expected wevibe bech32 address for TEST_SEED_HEX at m/44'/118'/0'/0/0,
// computed from the live deriveLeaderWallet implementation and run twice to
// confirm determinism before hardcoding.
const EXPECTED_ADDRESS = 'wevibe16yy67pj7ncthhpmfmyascn2qp2al0g0evahexy';

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

async function run(): Promise<void> {
  const { address } = await deriveLeaderWallet(TEST_SEED_HEX);
  assert(
    address === EXPECTED_ADDRESS,
    `secp256k1 derivation drift: expected ${EXPECTED_ADDRESS}, got ${address}`,
  );

  const again = await deriveLeaderWallet(TEST_SEED_HEX);
  assert(
    again.address === EXPECTED_ADDRESS,
    `secp256k1 derivation not deterministic: expected ${EXPECTED_ADDRESS}, got ${again.address}`,
  );

  process.stdout.write('ok: wallet secp256k1 seed-derivation parity\n');
}

run()
  .then(() => {
    process.exit(0);
  })
  .catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  });
