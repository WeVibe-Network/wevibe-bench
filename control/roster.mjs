// ─────────────────────────────────────────────────────────────────────────────
// ROSTER — what models exist, and what is actually resident
//
// TWO SOURCES, NEVER MERGED INTO ONE CLAIM:
//
//   proxy   :4545/v1/models       the aliases the bench can address
//   runtime :1234/api/v0/models   what is loaded, and at what context length
//
// The proxy answers "can I name this model?". The runtime answers "is it in
// memory, and how much context did it actually get?". Those are different
// questions and a mismatch between them has voided cells before:
//
//   - the proxy's /control/load cannot set `parallel` (the SDK drops
//     maxParallelPredictions) and once shipped a 1h TTL that auto-unloaded a
//     model mid-campaign;
//   - the RUNBOOK therefore requires preflighting loaded_context_length ==
//     262144 after EVERY swap, via the runtime endpoint, not the proxy.
//
// So this module reports both and computes `context_match` rather than picking
// a winner. The UI must show the discrepancy BEFORE a run can start.
//
// UNREACHABLE IS A STATE, NOT AN ERROR. If either endpoint is down the roster
// still returns, with that side null and a reason attached. The board then
// renders an explicit unwired state instead of an empty dropdown that looks
// like "no models exist".
// ─────────────────────────────────────────────────────────────────────────────

import { BENCH_PURPOSE } from "./contract.mjs";

/**
 * Declared context per alias, mirroring wevibe_bench/config.py
 * WORKER_MODEL_REGISTRY. This is a MIRROR, and the mirror is deliberate: the
 * control plane is JS and the registry is Python, so there is no shared import.
 * `roster.test.mjs` pins these values against the Python source so drift fails
 * a test rather than silently offering a context the worker will not use.
 */
export const DECLARED_CONTEXT = {
  "qwen3.6-35b-a3b-bench": 262144,
  "deepseek-v4-flash-bench": 262144,
  "nemotron-3-nano-30b-bench": 262144,
  "gemma-4-26b-a4b-bench": 262144,
};

/**
 * RETIRED BENCH ALIASES — advertised by the proxy, refused by the bench.
 *
 * `wevibe-bench-worker` is the auto-detect slug: it maps upstream to `auto`, so
 * a cell run on it measures WHICHEVER model happened to be resident and records
 * no model identity. That design is retired — every cell now names its subject —
 * and the alias must not appear as a bench-eligible model on any surface.
 *
 * IT IS EXCLUDED HERE RATHER THAN WISHED AWAY. The proxy still advertises it
 * with purpose=wevibe-bench (its roster lives in the local-llm-proxy service,
 * not this repo), so eligibility computed from `purpose` alone would keep
 * offering it a [+ baseline] button. Naming the retirement in one place means
 * the ledger, the run-start gate and the profile modal all refuse it for the
 * same stated reason, and the entry can be deleted outright the day the proxy
 * stops serving it.
 */
export const RETIRED_ALIASES = {
  "wevibe-bench-worker":
    "retired: this alias resolves to whatever model is resident behind the proxy, so a cell "
    + "run on it measures an unrecorded subject. Benchmark a named bench alias instead.",
};

/** Context options offered by the UI. */
export const CONTEXT_CHOICES = [65536, 131072, 262144];

async function getJson(url, timeoutMs = 2000) {
  try {
    const res = await fetch(url, {
      signal: AbortSignal.timeout(timeoutMs),
      headers: { accept: "application/json" },
    });
    if (!res.ok) return { ok: false, reason: `HTTP ${res.status} from ${url}` };
    return { ok: true, data: await res.json() };
  } catch (err) {
    return { ok: false, reason: `unreachable: ${url} (${String(err?.message ?? err)})` };
  }
}

/**
 * Normalise the runtime's model list into a lookup by upstream model id.
 * The runtime reports `state` ("loaded" | "not-loaded"), `max_context_length`,
 * and — when loaded — `loaded_context_length`.
 */
function indexRuntime(data) {
  const out = new Map();
  const rows = Array.isArray(data?.data) ? data.data : [];
  for (const r of rows) {
    const id = typeof r?.id === "string" ? r.id : null;
    if (!id) continue;
    out.set(id, {
      state: typeof r.state === "string" ? r.state : null,
      max_context: Number.isFinite(r.max_context_length) ? r.max_context_length : null,
      loaded_context: Number.isFinite(r.loaded_context_length) ? r.loaded_context_length : null,
    });
  }
  return out;
}

/**
 * Match a proxy alias's upstream model to a runtime entry.
 *
 * The two services name the same model DIFFERENTLY: the proxy reports
 * `Qwen3.6-35B-A3B-MLX-8bit` while the runtime reports `qwen/qwen3.6-35b-a3b`.
 * Matching is therefore normalised (lowercased, separators and quant/format
 * suffixes stripped) rather than exact.
 *
 * A FAILED MATCH RETURNS NULL AND STAYS NULL. It is never guessed at, because a
 * wrong match would report a context length belonging to a different model —
 * exactly the confusion the preflight rule exists to catch.
 */
export function matchRuntime(upstreamModel, runtimeIndex) {
  if (!upstreamModel) return null;
  const norm = (s) =>
    String(s)
      .toLowerCase()
      .replace(/[_\s]/g, "-")
      .replace(/-(mlx|gguf|mxfp4|q\d[a-z0-9_]*|\d+bit)\b/g, "")
      .replace(/[^a-z0-9.]/g, "");

  const want = norm(upstreamModel);
  if (!want) return null;

  for (const [id, info] of runtimeIndex) {
    const have = norm(id.includes("/") ? id.split("/").pop() : id);
    if (have && (have === want || want.endsWith(have) || have.endsWith(want))) {
      return { id, ...info };
    }
  }
  return null;
}

/**
 * Build the roster. Never throws — an unreachable upstream is reported as a
 * null side with a reason, so the caller always has something honest to render.
 */
export async function readRoster({ proxyUrl, runtimeUrl }) {
  const [proxyRes, runtimeRes] = await Promise.all([
    getJson(`${proxyUrl}/v1/models`),
    getJson(`${runtimeUrl}/api/v0/models`),
  ]);

  const notes = [];
  if (!proxyRes.ok) notes.push(`proxy roster unwired — ${proxyRes.reason}`);
  if (!runtimeRes.ok) notes.push(`runtime residency unwired — ${runtimeRes.reason}`);

  const runtimeIndex = runtimeRes.ok ? indexRuntime(runtimeRes.data) : new Map();
  const rows = proxyRes.ok && Array.isArray(proxyRes.data?.data) ? proxyRes.data.data : [];

  const models = rows.map((r) => {
    const id = String(r?.id ?? "");
    const upstream = typeof r?.upstream_model === "string" ? r.upstream_model : null;
    const purpose = typeof r?.purpose === "string" ? r.purpose : null;
    const rt = runtimeRes.ok ? matchRuntime(upstream, runtimeIndex) : null;

    const declared = DECLARED_CONTEXT[id] ?? null;
    const loaded = rt?.loaded_context ?? null;

    const retired = RETIRED_ALIASES[id] ?? null;

    return {
      id,
      upstream_model: upstream,
      purpose,
      // A retired alias is NOT bench-eligible however the proxy labels it. The
      // reason travels with the row so a surface that wants to explain the
      // absence can, rather than the model simply vanishing.
      bench_eligible: purpose === BENCH_PURPOSE && !retired,
      retired_reason: retired,
      // `resident` is null (unobserved) when the runtime is unreachable —
      // NOT false. "We cannot see whether it is loaded" and "it is not loaded"
      // are different facts and must not collapse.
      resident: runtimeRes.ok ? rt?.state === "loaded" : null,
      declared_context: declared,
      max_context: rt?.max_context ?? null,
      loaded_context: loaded,
      // null when either side is unobserved — never a false "match".
      context_match:
        declared === null || loaded === null ? null : declared === loaded,
      runtime_id: rt?.id ?? null,
    };
  });

  return {
    ok: proxyRes.ok,
    models,
    bench_models: models.filter((m) => m.bench_eligible),
    context_choices: CONTEXT_CHOICES,
    proxy_ok: proxyRes.ok,
    runtime_ok: runtimeRes.ok,
    // Verbatim, human-readable, rendered in the control region.
    notes,
    reason: proxyRes.ok ? null : proxyRes.reason,
  };
}
