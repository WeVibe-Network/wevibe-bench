# wevibe-bench RUNBOOK — recall topology (READ BEFORE running any recall/seed script)

## The two-tier recall topology (do NOT confuse these)

The bench recall data path is:
`bench script -> MCP /v1/recall (:4450 or :4550 clone) -> hub /v1/orgs/{org}/query (:4440)`

There are TWO separate services. They have DIFFERENT ports, DIFFERENT health paths,
and DIFFERENT auth. Confusing them is the #1 bench failure mode.

| Tier | What it is | Host addr | Health check | Auth |
|------|-----------|-----------|--------------|------|
| **HUB** | Docker container `wevibe-hub` — the ONE hub. Normally ALREADY RUNNING. | `127.0.0.1:4440` | `GET /health` -> `200 {"status":"ok","db":"connected",...}` | none (public) |
| **MCP recall client** | `wevibe-mcp` process / Option-C bench clone | `127.0.0.1:4450` (default) or `127.0.0.1:4550` (Option-C clone) | `GET /v1/health` -> `200` with bearer token / `401` without | bearer token |

Key facts:
- The hub is a **Docker container**, NOT a host process. `ps`/`lsof` for a host hub finds
  NOTHING — that absence is NORMAL and is NOT evidence the hub is down. Check `GET :4440/health`.
- The hub health path is `/health`. The mcp health path is `/v1/health`. They are NOT the same path.
- In `wevibe_bench/config.py`, `RunConfig.hub_url` = the hub (`:4440`) and `RunConfig.mcp_recall_url`
  = the mcp recall client (`:4450` default, `:4550` clone). The recall backend posts to
  `{mcp_recall_url}/v1/recall`. (Historically `hub_url` was mislabeled and pointed at the mcp —
  that is fixed.)

## Mandatory preflight

Every recall/seed/measure script calls `wevibe_bench.preflight.preflight(...)` BEFORE any recall op.
It checks the hub (`:4440/health`) and the mcp (`/v1/health`) with the CORRECT paths and RAISES a
loud `PreflightError` that names the exact remediation. If you see a PreflightError, READ IT — it
tells you which tier is down and the exact command to fix it. Do not "work around" it.

## Bringing services up

### Hub (`:4440`)
Run from `wevibe-meta` (Walter-run):
```
make redeploy
```
NOTE (cross-ref the PERMANENT block in SESSIONCONTINUANCE): the `backend-restart` error that
`make redeploy` prints is EXPECTED and HARMLESS — the redeploy still succeeds.

### Option-C recall clone (`:4550`)
The clone lives at `/Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench/scaffold/wevibe-mcp-clone`. Its `dist/` is
prebuilt (rebuild with `npx tsc` only if stale). Bench lifecycle points it via
`WEVIBE_BENCH_MCP_ROOT=<repo>/scaffold/wevibe-mcp-clone` because canonical `wevibe-mcp` lacks bench `/v1/submit` + `/v1/identity/pubkeys` endpoints. Start it with the SAME env the bench lifecycle
orchestrator uses — the identity seed MUST be the bench leader seed (`$WEVIBE_BENCH_LEADER_SEED_HEX`)
or recall cannot decrypt the seeded corpus. FOUR non-obvious requirements are mandatory, but
`config/bench.env` is NOT where they come from — bench lifecycle injects them via the one-path
(`wevibe_bench/lifecycle/orchestrator.py:_leader_admin_env` + `bring_up` -> `McpProcessManager.spawn`),
so a standalone start must set them explicitly with the same derivations:
- `WEVIBE_UMBRAL_SIDECAR_BIN` + `WEVIBE_GUARD_BIN` — the canonical wevibe-plugin normally injects
  both from wevibeRoot; a manual `node dist/server.js` does NOT, so `register-org` 500s with
  "WEVIBE_UMBRAL_SIDECAR_BIN environment variable is required" (epoch Umbral pubkey). This caused
  the 2026-07-13 cell-1 ladder abort. Derive both from `WEVIBE_BENCH_WEVIBE_ROOT` as lifecycle does.
- `WEVIBE_MCP_HTTP_ONLY=1` — without it the clone ALSO runs the stdio MCP server, which treats a
  backgrounded stdin-EOF as `stdin-end` and SHUTS DOWN. Required for any `nohup … &` start.
- `< /dev/null` on the launch — belt-and-suspenders so the stdio path never sees an open-then-closed stdin.
- `WEVIBE_RECALL_MODE=test` — the clone SERVER's recall governor reads this from its own env
  (`retrieve-cli.ts` `getRecallMode`); without it recall is `prod`-governed (floor 0.55/budget 3) and a
  fresh low-trust memory is filtered out, so prove-delivery AND the ON-run recall arm return nothing.
- `WEVIBE_KEYSTORE_PATH="$WEVIBE_BENCH_LEADER_KEYSTORE"` — the org MASTER KEY envelope is WRITTEN by
  the clone serving `/v1/org-setup/finalize` and READ by the invite + provision-recall `admin.js`
  subprocesses (which set `WEVIBE_KEYSTORE_PATH=$WEVIBE_BENCH_LEADER_KEYSTORE`). If the manual :4550
  clone omits this it writes K_master to the DEFAULT `~/.wevibe/keys` while the readers look in `/tmp`
  → recall `decrypt_failed: Umbral re-encryption Internal validation failed` / invite `no master key
  found` (the 2026-07-13 blocker). Writer and readers MUST share this path. (K_master stays leader-local;
  hub never receives K_master or epoch_sk — this is a keystore-PATH routing fix, not a crypto change.)
Preferred one-path remains lifecycle `bring_up`; this standalone command only mirrors that lifecycle env.
```
cd /Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench && \
  WEVIBE_BENCH_WEVIBE_ROOT="${WEVIBE_BENCH_WEVIBE_ROOT:-$(cd .. && pwd)}" && \
  set -a && source config/bench.env && set +a && \
  WEVIBE_BENCH_LEADER_KEYSTORE="${WEVIBE_BENCH_LEADER_KEYSTORE:-/tmp/wevibe-bench-leader-keystore.json}" && \
cd scaffold/wevibe-mcp-clone && \
  WEVIBE_MCP_HTTP_ONLY=1 WEVIBE_MCP_HTTP_PORT=4550 WEVIBE_HTTP_HOST=127.0.0.1 \
  WEVIBE_BENCH_ENDPOINTS=1 WEVIBE_SEED_BACKEND=env \
  WEVIBE_IDENTITY_SEED_HEX="$WEVIBE_BENCH_LEADER_SEED_HEX" \
  WEVIBE_KEYSTORE_PATH="$WEVIBE_BENCH_LEADER_KEYSTORE" \
  WEVIBE_UMBRAL_SIDECAR_BIN="${WEVIBE_BENCH_WEVIBE_ROOT}/wevibe-umbral/target/release/wevibe-umbral" \
  WEVIBE_GUARD_BIN="${WEVIBE_BENCH_WEVIBE_ROOT}/wevibe-guard/target/release/wevibe-guard" \
  WEVIBE_RECALL_MODE=test \
  WEVIBE_HUB_URL=http://127.0.0.1:4440 \
  nohup node dist/server.js < /dev/null > ../../runs/clone4550.log 2>&1 &
```
**CLEAN-START (wipe) INVARIANT — reproducible, not a one-off:** a full chain wipe (`make docker-down`
= `docker compose down -v`) destroys the on-chain org + its epoch key, so the LOCAL K_master in the
bench keystores becomes STALE and MUST be cleared IN THE SAME STEP, else the next `register-org`
creates a fresh org whose epoch key mismatches the stale K_master → `decrypt_failed`. The correct
clean-start sequence is therefore:
```
make docker-down && make docker-up          # (from wevibe-meta) wipe chain/pg/qdrant + served-cache
rm -rf "$WEVIBE_BENCH_LEADER_KEYSTORE" "$WEVIBE_BENCH_CONTRIB_KEYSTORE"   # clear STALE bench K_master (NEVER ~/.wevibe/keys)
# then start :4550 (above) — its /v1/org-setup/finalize regenerates a fresh matching K_master into $WEVIBE_BENCH_LEADER_KEYSTORE
```
`WEVIBE_BENCH_ENDPOINTS=1` enables the bench-only `/v1/submit` + `/v1/identity/pubkeys` endpoints.
The `/v1/health` route is always present (no bench flag needed).

## Docker sandbox workers (Architecture A)

### Build the pinned worker image (once / when Dockerfile changes)
```bash
cd wevibe-bench && docker build -t wevibe-bench-worker:v1 docker/worker
```
Creates the disposable worker image; no secrets are baked. Network `wevibe-bench-net` is auto-created by the adapter.

### Verify image + isolation (no paid model)
```bash
docker image inspect wevibe-bench-worker:v1 --format '{{.Size}}'
python -m pytest tests/test_docker_isolation.py -q
python scripts/docker_isolation_smoke.py
```
`tests/test_docker_isolation.py` checks isolation + wiring. `scripts/docker_isolation_smoke.py` is the local synthetic
smoke proving oracle/golden/runner/host paths are absent inside the container and exported `/work` edits are scored
host-side by a dummy oracle (NO paid model, NO live corpus).

### How a cell runs (informational)
- `backgammon.py` preflights `docker_available()` + `image_exists()`.
- The adapter starts one container per cell, mounting ONLY the worktree at `/work`.
- Each cell worktree pins OpenCode to exactly one model: both `model` and `small_model` are set to that cell's
  selected roster model, so the cell only requests that model. The host proxy remains a hard backstop and rejects
  any non-matching model.
- It runs `docker exec … opencode run … --dir /work` (`--pure` for OFF), then re-injects problems-only feedback into
  the same container/session.
- The driver watches streamed events for the model's final stop. If OpenCode 1.18.1 finishes work but does not exit,
  the driver waits a bounded idle grace (`--completion-grace`, default 30s) and then terminates the finished process
  instead of waiting the full `--run-timeout`; `--run-timeout` remains the ceiling for runs that never finish.
- Cell teardown is deterministic: `docker rm -f` at cell end, which always reaps the container and remaining
  processes.
- OFF/ON recall reaches the host clone at `host.docker.internal:4550`; NO host keys/corpus enter the container.

## OpenRouter proxy (the ONE paid transport path)

This proxy is the only paid-transport path for benchmark cells.

- Host-side proxy process (`scripts/run_openrouter_proxy.py`) sources the REAL
  OpenRouter upstream key at launch from OpenCode auth (`~/.local/share/opencode/auth.json`).
  Use `--auth-path` only to override location in tests; there is no env-var path and no fallback.
- Docker workers receive only an ephemeral per-run bearer token + proxy base URL.
- The proxy hard-injects provider routing policy and hard-clamps output-token ceilings.
- R-13 one path: direct provider-key forwarding/fallback is removed.
- R-37 observability: run with a dedicated timestamped logfile under
  `runs/openrouter-proxy/`.

### Start the proxy (host shell only)

```bash
uv run python scripts/run_openrouter_proxy.py \
  --run-id <id> \
  --model openrouter/<vendor>/<model> \
  --profile <glm|mimo|opus> \
  --cap-usd 12 \
  --target-usd <lower operational target> \
  --port 8789 \
  --checkpoint runs/openrouter-proxy/<run>-budget.json \
  --log runs/openrouter-proxy/<iso>.log \
  --max-output-tokens 8192 \
  --token-file runs/openrouter-proxy/<run>.token
```

To authorize a blocked profile with live pricing at launch, add:
`--authorize --pricing-input <USD/Mtok> --pricing-output <USD/Mtok> [--pricing-cache-read <USD/Mtok>] [--pricing-cache-write <USD/Mtok>]`.
Pricing values must be live-verified from OpenRouter immediately before each authorized run; pricing snapshots are never baked into code.

The proxy prints `http://host.docker.internal:8789/api/v1` on startup and writes
an ephemeral run token to `--token-file` (0600 permissions).

### Wire the worker (the worker never gets the real key)

```bash
scripts/run_backgammon.py \
  --proxy-base-url http://host.docker.internal:8789/api/v1 \
  --proxy-token-file runs/openrouter-proxy/<run>.token \
  ...
```

`run_backgammon.py` reads the token file and threads only that token into Docker
as `OPENROUTER_API_KEY`. The real OpenRouter key never enters the worker.

### Policy + budget invariants enforced by proxy

- Exact per-profile `provider` object is hard-injected on every request:
  - pinned `order`/`only` (SoT = `DEFAULT_PROFILES` in `wevibe_bench/adapters/openrouter_proxy.py`:
    GLM `z-ai/glm-5.2`→`novita`, MiMo `xiaomi/mimo-v2.5-pro`→`deepinfra`, Opus `anthropic/claude-opus-4.8`→`anthropic`),
  - `allow_fallbacks: false`,
  - `require_parameters: true`,
  - `quantizations` only when configured (GLM `["fp8"]`; MiMo/Opus omit — no fp8-tagged endpoint).
  - All three pins live-verified 15-07-26 (HTTP 200 via Novita/DeepInfra/Anthropic) — see `docs/BENCHMARK-ROSTER.md`.
- Client-supplied `provider` is rejected.
- Request model must match the selected profile model.
- `max_tokens` is hard-clamped to `--max-output-tokens`.
- Hard paid ceiling is absolute: `hard_cap = min(--cap-usd, 12.0)`.
- `--target-usd` is an optional lower operational target (not the hard ceiling).
- Before dispatch, proxy reserves a conservative worst-case USD bound.
- If projected cumulative spend would exceed hard cap, request is refused
  before dispatch.
- On uncertainty (missing/invalid `usage.cost`, upstream error, stream failure),
  reservation is retained as committed-unproven (never released).
- Budget checkpoint persists atomically and is policy-bound to
  `(run-id, model, profile, hard_cap)`; resume under mismatched policy is
  refused.
- `--reject-on-equality` is optional stricter mode (default allows projected
  equality and refuses only projected greater-than cap).

### Checkpoint / restart

Restarting the proxy cannot reset spend. If the same run binding is reused, the
ledger resumes from checkpoint. Any run-id/model/profile/cap mismatch is refused
at startup.

### Verification (zero-cost)

Zero-cost verification procedure (fake upstream; no paid OpenRouter call):

```bash
python -m pytest \
  tests/test_openrouter_proxy.py \
  tests/test_openrouter_proxy_server.py \
  tests/test_openrouter_proxy_docker_e2e.py -q
```

These suites are the policy/budget/routing/streaming/logging proof surface for
the proxy path.

### 1.18.1 residual

Base-URL override wiring is proven on OpenCode 1.17.20 and structurally certain
for the worker's 1.18.1 configuration path. One remaining live-request
byte-verification is intentionally deferred because it cannot be done zero-cost.
Known OpenCode 1.18.1 post-completion exit hangs are handled by the driver's
bounded `--completion-grace` termination path documented above.

### Later (paid) smoke — status update (21-07-26)
The prior 15-07 passability-first gate text is now historical. Walter GO 21-07-26 authorized the full Stage-7 scored ladder
(Opus SOURCE OFF+self-extract → kimi OFF/ON → big-pickle OFF/ON), superseding the earlier "Opus passability PASS before scored roster" block.

Execution happened and crashed: run `20260721T195407Z` completed Cell 1 as a valid 3-attempt capability FAIL, committed one org-0 memory,
then the harness crashed on `int("FAIL")`. Recovery path is Option B (preserve Cell 1 + memory, patch harness, continue from Cell 2 without
rerunning/rebilling Cell 1). Use the pinned continuation path in `docs/BENCHMARK-STATE.md §2B`.

**$12 ceiling facts remain unchanged:** paid-cell hard ceiling enforcement is still `min(configured cap, $12)` with cumulative Stage-7/global
ledger enforcement. This remains paid execution (Walter-authorized), not routine setup/build verification.

## THE HARD RULE

**NEVER build, compile, or start your own hub or mcp.** They already exist. If a recall fails:
1. Read the PreflightError — it names the down tier and the fix.
2. Bring the named service up with the command above (hub: `make redeploy`; clone: the `node dist/server.js` command).
3. If you cannot, STOP and report. Do NOT improvise infrastructure, do NOT compile a new hub/mcp, do NOT invent a fallback.

## Backgammon oracle-isolation invariant (measurement integrity)

**Invariant:** workers must never access gate oracle/test sources; worker feedback is
problems-only.

**Option-A note (15-07-26):** publishing edge-rules/constants into the public
`tasks/backgammon/CONTRACT.md` so pass-required behavior is derivable is ORTHOGONAL to integrity; it does NOT weaken
the three enforcement layers below (physical isolation, cheat-gate invalidation, problems-only feedback).

- Enforcement is three layers: (1) physical isolation (oracle never copied into
  worktree), (2) permission-deny (no `--dangerously-skip-permissions`; headless
  permissions are allow/deny only, never `ask`), and (3) transcript hard-gate
  (oracle-reference hit in `events.jsonl` forces `CHEAT` + INVALID/FAIL).
- Feedback reinjection includes only failing gate IDs/titles; never expected values,
  observed output, file paths, or stack traces.
- Any cheat detection = INVALID cell (never PASS, even if gate outputs pass).
- Full directive/spec: `docs/ORACLE-ISOLATION-DIRECTIVE.md`.

## Extraction-integrity gate (external coordinator responsibility)

**Binding rule (load before any scored benchmark run):**

Before any scored benchmark run, the external coordinator (the OpenCode
operator agent driving the benchmark) MUST load this directive.

After **every** extraction completion during that run, the coordinator MUST read
the matching MCP terminal integrity record in the `extraction.integrity` ops
log at
`<WEVIBE_LOG_DIR, else <workspace>/wevibe-meta>/.logs/ops/extraction.integrity-<YYYYMMDD>.log`
(UTC day), and correlate it by returned `job_id` and/or
`session_fp = sha256-first8(session_id)`, scoped by `org_id`.

If the matching terminal record is **missing** or **cannot be correlated**, or
if it reports `resolved_problem_count == 0 && emitted_memory_count > 0`
(`invariant_violation == true`), the coordinator MUST ABORT the whole benchmark
BEFORE `m2_proof.leader_verify_and_commit`, preserve the run logfile and
checkpoint, and escalate with `job_id`, `trace`, session fingerprint, and the
resolved/emitted counts.

The coordinator MUST NOT continue, MUST NOT self-heal or retry around the
violation, and MUST NOT approve or commit the memory.

For `failed` or `parked` outcomes, follow existing extraction-failure handling
and NEVER infer an invariant pass from the absence of a violation flag.

**Correlation limitation (must be handled explicitly):** the outer scored-ladder
`trace` is not propagated into MCP; each MCP REST call mints its own
`X-WeVibe-Trace-Id`. Reliable cross-boundary correlation keys are: (1) the
`job_id` returned in the `POST /v1/extract` 202 response body, and (2) the
`session_id` sent by the coordinator (matched via `session_fp`). `org_id`
further scopes matches.

Resumed/parked jobs may carry `episode_metadata: "unavailable_on_resume"`. A
`completed` record that lacks episode-count fields
(`resolved_problem_count`/`unresolved_problem_count`/`coincidental_count`) or
lacks `invariant_violation` is uncorrelatable-for-invariant and therefore
abort-worthy under the missing-record rule above.

## Smart-leader review (post-extraction, at the AWAIT_COORDINATOR_REVIEW boundary)

**Scope + ordering:** this is a coordinator post-pass at the durable
`AWAIT_COORDINATOR_REVIEW` checkpoint. It does NOT supersede the
extraction-integrity gate above. That gate is unchanged, runs first, and stays
binding.

1. **Boundary and control handoff (durable checkpoint)**
   - `scripts/run_cumulative.py run --until-review` advances one session to
     `AWAIT_COORDINATOR_REVIEW`, then pauses and returns:
     `{sequence_index, job_id, session_fp, candidate_count}` (plus
     `status="awaiting_coordinator_review"`).
   - At that boundary, the sequencer yields to the external coordinator.
   - Resume only with `scripts/run_cumulative.py resume --decision <path>`.

2. **Extraction-integrity gate still runs first (unchanged hard-abort rule)**
   - The coordinator MUST perform the existing integrity check against
     `extraction.integrity` first, before any leader verify/commit action.
   - Correlate by `job_id` and/or `session_fp`, scoped by `org_id`, using the
     terminal record in
     `<WEVIBE_LOG_DIR, else <workspace>/wevibe-meta>/.logs/ops/extraction.integrity-<YYYYMMDD>.log`.
   - If the matching terminal record is missing/uncorrelatable, or if
     `resolved_problem_count == 0 && emitted_memory_count > 0`
     (`invariant_violation == true`), the coordinator MUST HARD-ABORT the whole
     benchmark before `m2_proof.leader_verify_and_commit`.
   - Smart-leader `deny_final` does NOT supersede this abort. Denial is
     curation; abort is an integrity stop. Both coexist.

3. **Smart-leader review steps (only after the integrity gate passes)**
   - **RECONCILE:** reconcile authoritative chain/hub committed inventory
     against the private benchmark catalog via
     `scripts/run_cumulative.py reconcile-inventory --authoritative <json>`.
     Any authoritative committed item with no matching private catalog text is
     reported as `content_unavailable` and MUST NEVER be guessed/fabricated.
     Pass `--require-complete` to FAIL CLOSED before review when the org's
     authoritative inventory is non-empty but the catalog is incomplete
     (`catalog_complete == false`): the command surfaces the `in_chain_not_catalog`
     discrepancy and exits non-zero, so completeness is never silently assumed.
     (Denial stays non-fatal and is unrelated to this gate.)
   - **COMPARE:** compare every new extraction candidate against the private
     catalog using duplicate signals already implemented in
     `wevibe_bench.cumulative.catalog.PrivateCatalog.find_duplicates`:
     exact content-hash match, exact submission-hash match, plus keyword-overlap
     advisory. Carry duplicate references into the decision evidence.
     The coordinator obtains the plaintext it needs for this semantic cross-check
     — the NEW pending candidates' synthetic text AND the prior-accepted committed
     text — from `scripts/run_cumulative.py review-material`, which writes ONE
     `0600` private artifact (`*.private.json`, gitignored). The manifest/checkpoint
     itself stores candidate refs REDACTED to hash-only (`content_hash`, no text);
     new-candidate synthetic text lives only in the `0600` `*.review.jsonl` review
     card, and prior-accepted text only in the `0600` `*.catalog.jsonl`.
   - **DECIDE ALL:** emit a versioned decision manifest
     (`emit-decision-template` scaffolds it) and set every candidate verdict to
     `verify` or `deny_final`, each with a non-empty reason (and duplicate refs
     / evidence as applicable). The manifest MUST also carry integrity
     attestation: `job_id`, `session_fp`, `resolved_problem_count`,
     `emitted_memory_count`, `invariant_violation`, `integrity_record_seen`.
     The manifest-contract gate (`validate-decision`) rejects missing or
     uncorrelatable attestation, but does NOT re-run the runtime extraction
     integrity check.
   - **APPLY (real leader/hub path only):**
     - `verify` → `m2_proof.leader_verify_and_commit` (real hub
       moderation queue/embed/keyword verify/batch flow + leader-signer commit).
     - `deny_final` → real hub leader deny route
       `POST /v1/orgs/{org}/moderation/{hash}/deny` via
       `HubClient.deny_submission`, body-signed canonical
       `wevibe.deny_submission.v1`.
     - No direct DB/Qdrant/chain writes.
   - **DENIAL IS NON-FATAL:** denying some or all candidates is normal curation
     and MUST NOT abort the benchmark. Each denial is recorded in the safe
     decision ledger (hashes/CIDs/reasons/counts only), then the run continues
     through `COMMIT_INDEX_READY`/`NEXT_SESSION`. Reapplying the same decision
     manifest is idempotent; conflicting re-decision is rejected.

4. **Discovery, correlation, and privacy boundaries**
   - Extraction integrity record discovery path is exactly:
     `<WEVIBE_LOG_DIR, else <workspace>/wevibe-meta>/.logs/ops/extraction.integrity-<YYYYMMDD>.log`.
   - In the scored cumulative path, the private committed-memory catalog is
     under `runs/` (manifest-relative `*.catalog.jsonl`), enforced `0600`,
     gitignored by suffix, and stores synthetic benchmark comparison text only.
     It is chain-bound for comparison workflows, but never authoritative.
   - Private comparison text MUST NOT be copied into logs/reports/git. The
     safe/public decision ledger carries hashes/CIDs/counts/reasons only
     (R-37 / D-MISSION-INVARIANT: fingerprints + sizes, never
     plaintext/secrets/raw keys).
   - Authoritative identity reconciliation for the authoritative inventory input
     uses chain `x/org` (`GetMembers` / `GetOrg`) plus hub `ListMembers`. Each
     accepted catalog entry binds to the committing identity RETURNED by
     verify/commit when the payload carries it (`committing_leader_pubkey` etc.),
     else the local leader pubkey; `reconcile` flags any `identity_mismatch`.
   - **Two distinct "leader" roles (do NOT conflate):** the cryptographic
     leader-signer on the hub/chain commit path sees NO plaintext (only ciphertext
     + wrapped DEK + embedding card). The authorized external smart-leader
     COORDINATOR necessarily DOES read synthetic candidate + prior-accepted catalog
     comparison text to make semantic verify/deny decisions — this is by design and
     is NOT a leak. That authorized plaintext lives ONLY in the `0600` private
     review card (`*.review.jsonl`), catalog (`*.catalog.jsonl`), and the `0600`
     `review-material` artifact (`*.private.json`) — never in op/decision logs, the
     manifest checkpoint, reports, or git (all of which stay hash-only).
