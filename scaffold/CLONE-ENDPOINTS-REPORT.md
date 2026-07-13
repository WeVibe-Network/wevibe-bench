# CLONE ENDPOINTS REPORT — wevibe-mcp-clone

## Scope and location
- Repository clone used: `/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench/scaffold/wevibe-mcp-clone`
- Canonical repo touched: **No** (`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-mcp` untouched)

## WHAT changed
Implemented two benchmark-gated, bearer-auth endpoints in `src/http-server.ts`:

1. `POST /v1/submit` via new `handleSubmit(...)`
2. `GET /v1/identity/pubkeys` via new `handleIdentityPubkeys(...)`

Both routes are gated behind `WEVIBE_BENCH_ENDPOINTS === '1'` and only exist in routing when enabled.

## submitMemory signature found (verbatim)
From `src/contribution.ts`:

```ts
export async function submitMemory(
  rawNotes: string,
  orgId: string,
  hubUrl: string,
  membership: OrgMembership,
  memoryType: MemoryType,
  stackHint?: string[],
  sessionTranscript?: string,
  keywords?: MemoryKeywordMetadata,
  mc1?: Mc1WriteEnvelope,
): Promise<{ status: string; submissionHash?: string; error?: string; attestation?: AttestationMetadata }>
```

## HOW request body was mapped to submitMemory(...)

Incoming `/v1/submit` JSON body accepted:
```json
{
  "org_id": "string (required)",
  "memory_type": "memory (optional, defaults to memory)",
  "epoch_id": "integer (optional)",
  "plaintext": "string (required unless text provided)",
  "text": "string (fallback for plaintext)",
  "stack_hint": ["string", "..."],
  "keywords": {
    "classified": [{ "keyword": "string", "weight": 0, "base_weight": 0 }],
    "suggestions": [{ "keyword": "string", "weight": 0, "base_weight": 0, "rationale": "string" }]
  },
  "mc_version": 1
}
```

Mapping:
- `rawNotes` ← `plaintext` (or `text` fallback)
- `orgId` ← `org_id`
- `hubUrl` ← `HUB_URL`
- `membership` ← `await requireMembership(org_id)`
- `memoryType` ← `memory_type ?? 'memory'` (only `'memory'` allowed)
- `stackHint` ← `stack_hint`
- `sessionTranscript` ← `undefined` (not part of endpoint contract)
- `keywords` ← validated `keywords` object
- `mc1` ← omitted

### Adaptation/deviation (explicit)
`submitMemory(...)` does **not** accept `epoch_id` or scalar `mc_version` directly:
- `epoch_id`: handler validates provided `epoch_id` equals `membership.currentEpoch`; otherwise returns 400. Actual submission epoch still comes from membership (inside `submitMemory`).
- `mc_version`: handler validates integer and only supports `1` (`MC_VERSION`). Since `submitMemory` takes `mc1` envelope (not scalar), handler does not pass an override envelope; default `MC_VERSION` path remains.

## Handler and route locations (file:line)
`/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench/scaffold/wevibe-mcp-clone/src/http-server.ts`
- Gate constant: line **64**
- `handleSubmit(...)`: line **1089**
- `handleIdentityPubkeys(...)`: line **1231**
- Route match for `POST /v1/submit`: line **1996**
- Route match for `GET /v1/identity/pubkeys`: line **2002**

## Gate mechanism
- `const BENCH_ENDPOINTS_ENABLED = process.env.WEVIBE_BENCH_ENDPOINTS === '1';`
- Route table checks include `BENCH_ENDPOINTS_ENABLED && ...` for both endpoints.
- If env flag is not set, route conditions do not match, and request falls through to existing 404 path.
- Required bench comment was added on both route checks:
  - `// BENCH-ONLY (WEVIBE_BENCH_ENDPOINTS): headless adapter capability, not a production default (D-5.7/D3).`

## Endpoint contracts (adapter-facing)

### 1) `POST /v1/submit`
- Auth: Bearer token via existing `authorize(req)`.
- Request body:
```json
{
  "org_id": "org_123",
  "plaintext": "memory text",
  "memory_type": "memory",
  "epoch_id": 7,
  "stack_hint": ["ts", "node"],
  "keywords": {
    "classified": [{ "keyword": "redis", "weight": 0.9, "base_weight": 0.9 }],
    "suggestions": [{ "keyword": "cache", "weight": 0.4, "base_weight": 0.4, "rationale": "co-occurs" }]
  },
  "mc_version": 1
}
```

- 200 response shape:
```json
{
  "status": "pending",
  "submission_hash": "<hex or null>",
  "attestation": { "...": "optional" },
  "error": "optional"
}
```

- Error responses:
  - `401 {"status":"error","error":"unauthorized"}` (authorize helper)
  - `400 {"status":"error","error":"...validation message..."}`
  - `500 {"status":"error","error":"..."}`

### 2) `GET /v1/identity/pubkeys`
- Auth: Bearer token via existing `authorize(req)`.
- 200 response shape:
```json
{
  "ed25519": "<hex>",
  "x25519": "<hex>",
  "pre_pubkey": "<hex compressed secp256k1 pubkey>"
}
```

- Error responses:
  - `401 {"status":"error","error":"unauthorized"}`
  - `500 {"status":"error","error":"..."}`

## Logging behavior
- `handleSubmit`: `logOp('submit', ...)` entry/outcome with `trace`, `org_fp`, `submission_hash_fp`, `plaintext_len`, `dur_ms`, and full error string on failure.
- `handleIdentityPubkeys`: `logOp('identity.pubkeys', ...)` entry/outcome with fingerprints only (`ed25519_fp`, `x25519_fp`, `pre_pubkey_fp`), never private material.

## Verification commands and outputs (verbatim)

### 1) Type-check
Command:
```bash
npx tsc --noEmit
```
Output:
```text
(no output)
```

### 2) Build dist
Command:
```bash
npx tsc
```
Output:
```text
(no output)
```

### 3) Test suite
Command:
```bash
npx vitest run
```
Summary lines:
```text
✓ tests/seed-env-backend.test.ts  (4 tests) 52ms

Test Files  64 passed | 3 skipped (67)
     Tests  521 passed | 8 skipped (529)
  Start at  08:22:21
  Duration  45.95s (transform 3.22s, setup 1ms, collect 7.38s, tests 74.20s, environment 6ms, prepare 9.77s)
```
Full tool-captured output file:
`/Users/jerrysmith/.local/share/opencode/tool-output/tool_f477907fa001yq9d19m5Vd4NQy`

### 4) Gate sanity grep
Command:
```bash
rg -n "WEVIBE_BENCH_ENDPOINTS" src/http-server.ts
```
Output:
```text
64:const BENCH_ENDPOINTS_ENABLED = process.env.WEVIBE_BENCH_ENDPOINTS === '1';
1995:    // BENCH-ONLY (WEVIBE_BENCH_ENDPOINTS): headless adapter capability, not a production default (D-5.7/D3).
2001:    // BENCH-ONLY (WEVIBE_BENCH_ENDPOINTS): headless adapter capability, not a production default (D-5.7/D3).
```

### 5) dist artifact sanity
Command:
```bash
node -e "require('fs').accessSync('dist/http-server.js')"
```
Output:
```text
(no output)
```

## Files touched
- `/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench/scaffold/wevibe-mcp-clone/src/http-server.ts`
- `/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench/scaffold/CLONE-ENDPOINTS-REPORT.md` (this report)

## Dead code found/purged
- None found within authorized scope.
