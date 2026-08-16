// ─────────────────────────────────────────────────────────────────────────────
// CLOUD BASELINES — the models this bench can measure that are not resident
//
// ── WHY THIS FILE EXISTS ────────────────────────────────────────────────────
//
// The harness has been able to run a cloud cell for as long as `--cloud` has
// existed (`scripts/run_cumulative.py`, `_compose_cloud_slug`): it composes the
// slug `{router}/{provider}/{model}`, checks it against the OrcaRouter provider
// block in `wevibe_bench/config.py`, and routes the cell straight at the vendor
// instead of the local relay. THE CONTROL PLANE COULD NOT REACH ANY OF IT. The
// board's only launch path built a local invocation, so the bench could measure
// exactly one class of model and the operator's answer to "benchmark a frontier
// model against this corpus" was to leave the board and use the CLI.
//
// ── A MIRROR, AND THE MIRROR IS DELIBERATE ──────────────────────────────────
//
// The catalogue below is a copy of `CLOUD_ORCAROUTER_PROVIDER["models"]`. The
// control plane is JS and the registry is Python, so there is no shared import —
// the same standing condition that makes `roster.mjs` mirror the worker context
// registry rather than reading it. The copy is PINNED BY A DRIFT TEST
// (control.test.mjs) against config.py, so a model added on one side and not the
// other fails a test rather than presenting as "that model does not exist".
//
// ── THE KEY IS RESOLVED HERE AND NEVER LEAVES ───────────────────────────────
//
// A cloud cell needs ORCAROUTER_API_KEY. It is resolved SERVER-SIDE, from the
// same two places `wevibe_bench/spend_key.py` reads — the environment, then the
// dotenv-format key file (`config/cloud.env`, mode 0600) — and it is NEVER sent
// to the browser and never accepted FROM the browser. What crosses the wire is
// `{present, source, fingerprint}`: enough for the board to state whether a
// cloud launch can succeed and where the key came from, and useless to anyone
// who intercepts it.
//
// A KEY FIELD IN THE MODAL WOULD BE THE OBVIOUS DESIGN AND IT IS THE WRONG ONE.
// It would put a live credential in page memory, in the POST body, and in
// whatever the browser decides to autofill — to configure something that is
// already configured on disk, on a service that runs on the same machine.
//
// ── MONEY IS NOT A DETAIL ───────────────────────────────────────────────────
//
// A local cell costs hours. A cloud cell costs hours AND money, and the ceiling
// is real: the proxy refuses a reservation above ABSOLUTE_MAX_USD per cell. That
// number is mirrored here so the confirmation card can state the ceiling the
// operator is committing to, in the same breath as the model name.
// ─────────────────────────────────────────────────────────────────────────────

import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";

/**
 * The router the composed slug names. `run_cumulative.py` defaults to this when
 * `--router` is absent, and the slug it builds is `{router}/{provider}/{model}`.
 * Mirrors config.DEFAULT_CLOUD_ROUTER.
 */
export const DEFAULT_CLOUD_ROUTER = "orcarouter";

/** Mirrors spend_key.CLOUD_API_KEY_ENV. */
export const CLOUD_API_KEY_ENV = "ORCAROUTER_API_KEY";

/** Mirrors spend_key._CLOUD_KEY_FILE_ENV — an override for the key file path. */
export const CLOUD_KEY_FILE_ENV = "WEVIBE_BENCH_CLOUD_KEY_FILE";

/**
 * The per-cell spend ceiling, mirrored from
 * `wevibe_bench/adapters/openrouter_proxy.py` ABSOLUTE_MAX_USD.
 *
 * STATED ON THE CONFIRMATION CARD rather than left in the proxy. An operator
 * committing to a cloud cell is committing to a bill, and the one number that
 * bounds it should not require reading the adapter to find.
 */
export const ABSOLUTE_MAX_USD = 12.0;

/**
 * THE CATALOGUE — mirror of config.CLOUD_ORCAROUTER_PROVIDER["models"].
 *
 * Keyed by the `{provider}/{model}` key the harness validates against, which is
 * exactly what `--provider` and `--model` are split from. Storing the key in the
 * shape the harness checks means the control plane cannot compose a slug the
 * harness will reject: the two agree by construction rather than by care.
 */
export const CLOUD_MODELS = {
  "anthropic/claude-fable-5": { name: "Claude Fable 5", context: 1000000, output: 128000 },
  "anthropic/claude-opus-4.6": { name: "Claude Opus 4.6", context: 1000000, output: 128000 },
  "anthropic/claude-opus-4.7": { name: "Claude Opus 4.7", context: 1000000, output: 128000 },
  "anthropic/claude-opus-4.8": { name: "Claude Opus 4.8", context: 1000000, output: 128000 },
  "anthropic/claude-opus-5": { name: "Claude Opus 5", context: 1000000, output: 128000 },
  "anthropic/claude-sonnet-4.5": { name: "Claude Sonnet 4.5", context: 1000000, output: 64000 },
  "anthropic/claude-sonnet-4.6": { name: "Claude Sonnet 4.6", context: 1000000, output: 64000 },
  "anthropic/claude-sonnet-5": { name: "Claude Sonnet 5", context: 1000000, output: 128000 },
  "deepseek/deepseek-chat": { name: "DeepSeek V3", context: 1048576, output: 384000 },
  "deepseek/deepseek-v4-flash-0731": { name: "DeepSeek V4 Flash 0731", context: 1048576, output: 384000 },
  "deepseek/deepseek-v4-pro-0813": { name: "DeepSeek V4 Pro 0813", context: 1048576, output: 384000 },
  "google/gemini-2.5-flash": { name: "Gemini 2.5 Flash", context: 1048576, output: 65536 },
  "google/gemini-2.5-flash-lite": { name: "Gemini 2.5 Flash Lite", context: 1048576, output: 65536 },
  "google/gemini-2.5-pro": { name: "Gemini 2.5 Pro", context: 1048576, output: 65536 },
  "google/gemini-3-flash-preview": { name: "Gemini 3 Flash Preview", context: 1048576, output: 65536 },
  "google/gemini-3.1-flash-lite-preview": { name: "Gemini 3.1 Flash Lite Preview", context: 1048576, output: 65536 },
  "google/gemini-3.1-pro-preview": { name: "Gemini 3.1 Pro Preview", context: 1048576, output: 65536 },
  "google/gemini-3.1-pro-preview-customtools": { name: "Gemini 3.1 Pro Preview Custom Tools", context: 1048576, output: 65536 },
  "google/gemini-3.5-flash": { name: "Gemini 3.5 Flash", context: 1048576, output: 65536 },
  "google/gemini-3.5-flash-lite": { name: "Gemini 3.5 Flash-Lite", context: 1048576, output: 65536 },
  "google/gemini-3.6-flash": { name: "Gemini 3.6 Flash", context: 1048576, output: 65536 },
  "google/gemma-4-26b-a4b-it": { name: "Gemma 4 26B A4B", context: 262144, output: 65536 },
  "grok/grok-4.3": { name: "grok-4.3", context: 1000000, output: 65536 },
  "grok/grok-4.5": { name: "Grok 4.5", context: 500000, output: 65536 },
  "grok/grok-4.6": { name: "Grok 4.6", context: 500000, output: 65536 },
  "kimi/kimi-k2.5": { name: "kimi-k2.5", context: 262144, output: 32768 },
  "kimi/kimi-k2.6": { name: "kimi-k2.6", context: 262144, output: 32768 },
  "kimi/kimi-k2.7-code": { name: "Kimi K2.7 Code", context: 262144, output: 262144 },
  "kimi/kimi-k3": { name: "Kimi K3", context: 1048576, output: 65536 },
  "meta/muse-spark-1.1": { name: "Muse Spark 1.1", context: 1048576, output: 65536 },
  "meta/muse-spark-1.2": { name: "Muse Spark 1.2", context: 1048576, output: 65536 },
  "minimax/minimax-m3": { name: "MiniMax M3", context: 1048576, output: 512000 },
  "obsidian/Qwen3.6-35B-A3B": { name: "Qwen3.6 35B A3B Uncensored (Aggressive)", context: 262144, output: 65536 },
  "obsidian/Qwen3.8-27B": { name: "Qwen3.8 27B Uncensored (Aggressive)", context: 262144, output: 65536 },
  "obsidian/gemma-4-26B-A4B": { name: "Gemma4 26B A4B Uncensored (Balanced)", context: 262144, output: 65536 },
  "openai/gpt-4.1": { name: "GPT-4.1", context: 1047576, output: 32768 },
  "openai/gpt-4.1-2025-04-14": { name: "gpt-4.1-2025-04-14", context: 1047576, output: 32768 },
  "openai/gpt-4.1-mini": { name: "GPT-4.1 Mini", context: 1047576, output: 32768 },
  "openai/gpt-4.1-mini-2025-04-14": { name: "gpt-4.1-mini-2025-04-14", context: 1047576, output: 32768 },
  "openai/gpt-4.1-nano": { name: "GPT-4.1 Nano", context: 1047576, output: 32768 },
  "openai/gpt-4.1-nano-2025-04-14": { name: "gpt-4.1-nano-2025-04-14", context: 1047576, output: 32768 },
  "openai/gpt-5": { name: "GPT-5", context: 400000, output: 128000 },
  "openai/gpt-5-2025-08-07": { name: "gpt-5-2025-08-07", context: 400000, output: 128000 },
  "openai/gpt-5-codex": { name: "GPT-5 Codex", context: 400000, output: 128000 },
  "openai/gpt-5-mini": { name: "GPT-5 Mini", context: 400000, output: 128000 },
  "openai/gpt-5-mini-2025-08-07": { name: "gpt-5-mini-2025-08-07", context: 400000, output: 128000 },
  "openai/gpt-5-nano": { name: "GPT-5 Nano", context: 400000, output: 128000 },
  "openai/gpt-5-nano-2025-08-07": { name: "gpt-5-nano-2025-08-07", context: 400000, output: 128000 },
  "openai/gpt-5-pro": { name: "GPT-5 Pro", context: 400000, output: 272000 },
  "openai/gpt-5-pro-2025-10-06": { name: "gpt-5-pro-2025-10-06", context: 400000, output: 272000 },
  "openai/gpt-5.1": { name: "GPT-5.1", context: 400000, output: 128000 },
  "openai/gpt-5.1-2025-11-13": { name: "gpt-5.1-2025-11-13", context: 400000, output: 128000 },
  "openai/gpt-5.1-codex": { name: "GPT-5.1-Codex", context: 400000, output: 128000 },
  "openai/gpt-5.1-codex-mini": { name: "GPT-5.1-Codex-Mini", context: 400000, output: 128000 },
  "openai/gpt-5.2": { name: "GPT-5.2", context: 400000, output: 128000 },
  "openai/gpt-5.2-2025-12-11": { name: "gpt-5.2-2025-12-11", context: 400000, output: 128000 },
  "openai/gpt-5.2-codex": { name: "GPT-5.2-Codex", context: 400000, output: 128000 },
  "openai/gpt-5.2-pro": { name: "GPT-5.2 Pro", context: 400000, output: 128000 },
  "openai/gpt-5.2-pro-2025-12-11": { name: "gpt-5.2-pro-2025-12-11", context: 400000, output: 128000 },
  "openai/gpt-5.3-codex": { name: "GPT-5.3-Codex", context: 400000, output: 128000 },
  "openai/gpt-5.4": { name: "GPT-5.4", context: 1050000, output: 128000 },
  "openai/gpt-5.4-2026-03-05": { name: "gpt-5.4-2026-03-05", context: 1050000, output: 128000 },
  "openai/gpt-5.4-mini": { name: "GPT-5.4 Mini", context: 400000, output: 128000 },
  "openai/gpt-5.4-nano": { name: "GPT-5.4 Nano", context: 400000, output: 128000 },
  "openai/gpt-5.4-pro": { name: "GPT-5.4 Pro", context: 1050000, output: 128000 },
  "openai/gpt-5.4-pro-2026-03-05": { name: "gpt-5.4-pro-2026-03-05", context: 1050000, output: 128000 },
  "openai/gpt-5.6-luna": { name: "GPT-5.6 Luna", context: 1050000, output: 128000 },
  "openai/gpt-5.6-sol": { name: "GPT-5.6 Sol", context: 1050000, output: 128000 },
  "openai/gpt-5.6-terra": { name: "GPT-5.6 Terra", context: 1050000, output: 128000 },
  "qwen/qwen3-max": { name: "Qwen3 Max", context: 262144, output: 65536 },
  "qwen/qwen3-max-preview": { name: "qwen3-max-preview", context: 262144, output: 65536 },
  "qwen/qwen3.5-flash": { name: "qwen3.5-flash", context: 1048576, output: 65536 },
  "qwen/qwen3.5-flash-2026-02-23": { name: "qwen3.5-flash-2026-02-23", context: 1048576, output: 65536 },
  "qwen/qwen3.5-plus": { name: "qwen3.5-plus", context: 1048576, output: 65536 },
  "qwen/qwen3.5-plus-2026-02-15": { name: "qwen3.5-plus-2026-02-15", context: 1048576, output: 65536 },
  "qwen/qwen3.6-35b-a3b": { name: "Qwen3.6 35B A3B", context: 262144, output: 65536 },
  "qwen/qwen3.6-flash": { name: "Qwen3.6 Flash", context: 1048576, output: 65536 },
  "qwen/qwen3.6-flash-2026-04-16": { name: "qwen3.6-flash-2026-04-16", context: 1048576, output: 65536 },
  "qwen/qwen3.6-plus": { name: "Qwen3.6 Plus", context: 1048576, output: 65536 },
  "qwen/qwen3.6-plus-2026-04-02": { name: "qwen3.6-plus-2026-04-02", context: 1048576, output: 65536 },
  "qwen/qwen3.7-flash": { name: "Qwen3.7 Flash", context: 1000000, output: 65536 },
  "qwen/qwen3.7-plus": { name: "Qwen3.7 Plus", context: 1000000, output: 65536 },
  "qwen/qwen3.8-27b": { name: "Qwen3.8 27B", context: 262144, output: 65536 },
  "qwen/qwen3.8-27b-free": { name: "Qwen3.8 27B (free)", context: 262144, output: 65536 },
  "qwen/qwen3.8-max": { name: "Qwen3.8 Max", context: 1000000, output: 65536 },
  "tencent/hy3": { name: "Hy3", context: 262144, output: 65536 },
  "z-ai/glm-5.2": { name: "GLM 5.2", context: 1000000, output: 128000 },
};

/**
 * The catalogue as rows, provider first.
 *
 * The board's model picker filters by provider and by text, so it needs the
 * provider as its own field rather than a prefix to be re-split in the browser.
 * One split, here, and every consumer reads the same answer.
 */
export function cloudCatalog() {
  return Object.entries(CLOUD_MODELS).map(([key, m]) => {
    const [provider, model] = splitCloudKey(key);
    return {
      key,
      provider,
      model,
      name: m.name,
      context: m.context,
      output: m.output,
      // The slug the manifest will record, composed the same way
      // `_compose_cloud_slug` composes it. Shown on the confirmation card so
      // the operator sees the identity that will be frozen, not a paraphrase.
      slug: `${DEFAULT_CLOUD_ROUTER}/${key}`,
    };
  });
}

/** The distinct providers, in catalogue order. The design's "4 providers". */
export function cloudProviders() {
  return [...new Set(cloudCatalog().map((m) => m.provider))];
}

/**
 * Split a `{provider}/{model}` key. Returns `[null, null]` for anything that is
 * not exactly two segments — a key with three segments is a composed slug that
 * still carries its router, and treating it as a provider key would compose
 * `orcarouter/orcarouter/...` and fail at the harness with a confusing message.
 */
export function splitCloudKey(key) {
  const parts = String(key ?? "").split("/").filter(Boolean);
  if (parts.length !== 2) return [null, null];
  return [parts[0], parts[1]];
}

/**
 * Is this a model the bench can route to the cloud, and if not, WHY not.
 *
 * Returns the same `{ok, code, reason}` shape every other gate in this service
 * returns, so a refusal here renders through the board's existing refusal path
 * rather than needing one of its own.
 */
export function resolveCloudModel(key) {
  const k = String(key ?? "").trim();
  if (!k) {
    return { ok: false, code: "cloud_model_missing", reason: "no cloud model was named" };
  }
  const [provider, model] = splitCloudKey(k);
  if (!provider || !model) {
    return {
      ok: false,
      code: "cloud_model_malformed",
      reason:
        `'${k}' is not a {provider}/{model} key. The harness validates the composed slug against ` +
        "the OrcaRouter provider block, and a key of any other shape cannot be composed.",
    };
  }
  const entry = CLOUD_MODELS[k];
  if (!entry) {
    return {
      ok: false,
      code: "cloud_model_unknown",
      reason:
        `'${k}' is not in the OrcaRouter provider block. available: ${Object.keys(CLOUD_MODELS).sort().join(", ")}`,
    };
  }
  return {
    ok: true,
    key: k,
    provider,
    model,
    name: entry.name,
    context: entry.context,
    output: entry.output,
    slug: `${DEFAULT_CLOUD_ROUTER}/${k}`,
  };
}

/**
 * Parse a dotenv-format file the way `spend_key._read_dotenv` does, minus the
 * `${VAR}` expansion.
 *
 * Expansion is deliberately NOT mirrored: this reader answers one question —
 * does a usable key exist — and a key whose value is an unexpanded reference to
 * another variable is not usable by this reader either way. Implementing half of
 * an expansion rule would be worse than implementing none, because the half that
 * worked would imply the other half did too.
 */
function parseDotenv(text) {
  const out = {};
  for (const raw of String(text).split("\n")) {
    let line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    if (line.startsWith("export ")) line = line.slice(7).trimStart();
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (value.length >= 2 && value[0] === value[value.length - 1] && (value[0] === "'" || value[0] === '"')) {
      value = value.slice(1, -1);
    }
    if (key && !(key in out)) out[key] = value;
  }
  return out;
}

/** First eight of the sha256, mirroring `spend_key.key_fingerprint`. */
function fingerprint(token) {
  return createHash("sha256").update(String(token)).digest("hex").slice(0, 8);
}

/**
 * WHERE THE KEY COMES FROM, AND WHETHER IT IS THERE.
 *
 * The environment wins over the file, mirroring spend_key: a key exported into
 * the control plane's own environment is the one the spawned harness inherits,
 * so reporting the file's key while the harness would use the environment's
 * would be a report about a run that is not the one about to happen.
 *
 * NEVER RETURNS THE KEY. The fingerprint is returned instead — it identifies
 * WHICH key is in play (the useful question when two are configured) and
 * discloses nothing. This object is published to the browser.
 */
export async function readCloudKey({ benchRoot, env = process.env } = {}) {
  const fromEnv = typeof env?.[CLOUD_API_KEY_ENV] === "string" ? env[CLOUD_API_KEY_ENV].trim() : "";
  if (fromEnv) {
    return {
      present: true,
      source: "environment",
      source_detail: `${CLOUD_API_KEY_ENV} is exported in the control plane's environment`,
      fingerprint: fingerprint(fromEnv),
      reason: null,
    };
  }

  const path = env?.[CLOUD_KEY_FILE_ENV] || (benchRoot ? join(benchRoot, "config", "cloud.env") : null);
  if (!path) {
    return {
      present: false,
      source: null,
      source_detail: null,
      fingerprint: null,
      reason:
        `${CLOUD_API_KEY_ENV} is not set and no key file path could be resolved — the control ` +
        "plane does not know where its bench root is",
    };
  }

  let text;
  try {
    text = await fs.readFile(path, "utf8");
  } catch (err) {
    return {
      present: false,
      source: null,
      source_detail: path,
      fingerprint: null,
      // The path is named. "No key" with no location is a dead end for an
      // operator who believes they configured one.
      reason:
        `${CLOUD_API_KEY_ENV} is not exported and ${path} could not be read ` +
        `(${String(err?.code ?? err?.message ?? err)}). A cloud cell cannot authenticate.`,
    };
  }

  const value = parseDotenv(text)[CLOUD_API_KEY_ENV] ?? "";
  if (!value) {
    return {
      present: false,
      source: null,
      source_detail: path,
      fingerprint: null,
      reason: `${path} exists but defines no ${CLOUD_API_KEY_ENV}`,
    };
  }

  return {
    present: true,
    source: "key_file",
    source_detail: path,
    fingerprint: fingerprint(value),
    reason: null,
  };
}

/**
 * The whole cloud capability, in one object.
 *
 * Assembled here rather than in the route so the launch gate and the board read
 * the SAME answer — a picker that offers a model the launch would refuse for
 * want of a key is the class of lie this codebase spends most of its comments
 * refusing to tell.
 */
export async function readCloud({ benchRoot, env = process.env } = {}) {
  const key = await readCloudKey({ benchRoot, env });
  return {
    ok: true,
    contract_version: CLOUD_CONTRACT_VERSION,
    router: DEFAULT_CLOUD_ROUTER,
    providers: cloudProviders(),
    models: cloudCatalog(),
    key,
    // The ceiling belongs beside the models, not in a footnote: it is the
    // number that bounds what a confirmation on this surface costs.
    spend_ceiling_usd: ABSOLUTE_MAX_USD,
    spend_note:
      `the proxy refuses any reservation above $${ABSOLUTE_MAX_USD.toFixed(2)} for a single cell. ` +
      "That is a hard ceiling on one cell, not a budget for the campaign.",
    // ONE PLACE SAYS WHETHER A CLOUD CELL CAN START AT ALL.
    can_start: key.present,
    can_start_reason: key.present ? null : key.reason,
  };
}

export const CLOUD_CONTRACT_VERSION = 1;
