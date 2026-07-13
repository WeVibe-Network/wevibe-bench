import { OfflineSigner } from '@cosmjs/proto-signing';

interface KeplrLike {
  getOfflineSigner(chainId: string): OfflineSigner;
}

export function getOfflineSigner(chainId: string): OfflineSigner {
  const maybeKeplr = (globalThis as { keplr?: KeplrLike }).keplr;
  if (!maybeKeplr) {
    throw new Error('Keplr wallet is unavailable in this runtime');
  }
  return maybeKeplr.getOfflineSigner(chainId);
}
