export interface WevibeConfig {
  hubUrl: string;
  chainId: string;
  chainRpc: string;
  chainRest: string;
  socialGraphUrl: string;
  bech32Prefix: string;
  coinDenom: string;
  coinMinDenom: string;
  environment: string;
}

export const WEVIBE_CONFIG_GLOBAL = '__WEVIBE_CONFIG__';
export const DEFAULT_WEVIBE_MCP_HTTP_URL = 'http://127.0.0.1:4450';

// ── WEVIBE_ENV base-URL switch ────────────────────────────────────────────
// ONE flag selects the base URL set for every service. DEFAULT 'local'
// (local-first; no public URLs are live yet). Precedence (R-13): an explicit
// per-URL env var (WEVIBE_HUB_URL, WEVIBE_CHAIN_RPC, …) > the WEVIBE_ENV base.
export type WevibeEnv = 'local' | 'production';

export function resolveWevibeEnv(): WevibeEnv {
  return process.env.WEVIBE_ENV?.trim().toLowerCase() === 'production' ? 'production' : 'local';
}

interface EnvBaseUrls {
  hubUrl: string;
  chainRpc: string;
  chainRest: string;
  socialGraphUrl: string;
}

const LOCAL_BASE: EnvBaseUrls = {
  hubUrl: 'http://localhost:4440',
  chainRpc: 'http://localhost:26657',
  chainRest: 'http://localhost:1317',
  socialGraphUrl: 'http://localhost:4470',
};

// Public infra is NOT deployed yet. These are EXPLICIT PLACEHOLDERS on the
// reserved `.invalid` TLD (RFC 6761 — can never resolve to real infra), so they
// can't be mistaken for live hosts. Fill real values at VPS deploy via the
// per-URL env vars (WEVIBE_HUB_URL / WEVIBE_CHAIN_RPC / WEVIBE_CHAIN_REST /
// WEVIBE_SOCIAL_GRAPH_URL — they win over this base; see .env.example).
const PRODUCTION_BASE: EnvBaseUrls = {
  hubUrl: 'https://hub.PLACEHOLDER.invalid', // TODO(VPS): set real host at deploy
  chainRpc: 'https://chain-rpc.PLACEHOLDER.invalid', // TODO(VPS): set real host at deploy
  chainRest: 'https://chain-rest.PLACEHOLDER.invalid', // TODO(VPS): set real host at deploy
  socialGraphUrl: 'https://social-graph.PLACEHOLDER.invalid', // TODO(VPS): set real host at deploy
};

function envBaseUrls(): EnvBaseUrls {
  return resolveWevibeEnv() === 'production' ? PRODUCTION_BASE : LOCAL_BASE;
}

// Non-URL, mode-invariant defaults + the MCP sidecar (local in BOTH modes —
// the dashboard's org crypto always talks to the leader's local MCP).
const STATIC_DEFAULTS = {
  chainId: 'wevibe-local-1',
  bech32Prefix: 'wevibe',
  coinDenom: 'VIBE',
  coinMinDenom: 'uvibe',
};

export function readConfigFromEnv(): WevibeConfig {
  const base = envBaseUrls();
  return {
    hubUrl: process.env.WEVIBE_HUB_URL ?? base.hubUrl,
    chainId: process.env.WEVIBE_CHAIN_ID ?? STATIC_DEFAULTS.chainId,
    chainRpc: process.env.WEVIBE_CHAIN_RPC ?? base.chainRpc,
    chainRest: process.env.WEVIBE_CHAIN_REST ?? base.chainRest,
    socialGraphUrl: process.env.WEVIBE_SOCIAL_GRAPH_URL ?? base.socialGraphUrl,
    bech32Prefix: process.env.WEVIBE_BECH32_PREFIX ?? STATIC_DEFAULTS.bech32Prefix,
    coinDenom: process.env.WEVIBE_COIN_DENOM ?? STATIC_DEFAULTS.coinDenom,
    coinMinDenom: process.env.WEVIBE_COIN_MIN_DENOM ?? STATIC_DEFAULTS.coinMinDenom,
    environment: resolveWevibeEnv(),
  };
}

export function getConfig(): WevibeConfig {
  if (typeof window === 'undefined') {
    return readConfigFromEnv();
  }

  const runtimeConfig = (window as any)[WEVIBE_CONFIG_GLOBAL] as WevibeConfig | undefined;
  return runtimeConfig ?? readConfigFromEnv();
}

export function hubWsUrl(path: string): string {
  const hubUrl = new URL(getConfig().hubUrl);
  hubUrl.protocol = hubUrl.protocol === 'https:' ? 'wss:' : 'ws:';
  return new URL(path, hubUrl).toString();
}

export function getMcpHttpUrl(): string {
  const configuredUrl = process.env.WEVIBE_MCP_HTTP_URL?.trim();
  return configuredUrl && configuredUrl.length > 0
    ? configuredUrl
    : DEFAULT_WEVIBE_MCP_HTTP_URL;
}

export function isProductionEnv(): boolean {
  return getConfig().environment === 'production';
}
