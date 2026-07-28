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

### Live-session observability (pre-run precondition, BINDING)

> "No benchmark run executes blind. Every run must expose its live worker session state (the opencode session DB or an equivalent event stream) at a known, timestamped path under the run directory, and a poller must observe it in realtime alongside the spend and stream signals. A run that cannot be observed live does not start."

Operator checklist before declaring a run started:
- Confirm the run dir will carry `session-db/opencode.db` (mount is automatic; launch progress
  line `step=session-db` proves the live path).
- Spawn the R-31 poller using `docs/POLLER-BRIEF.md`, pointed at the run dir plus proxy
  budget/log paths, BEFORE start declaration.
- Poller verdict comes ONLY from `scripts/session_db_poll.py`; never improvise hang detection.
- Kill is authorized ONLY on `VERDICT=DEAD`, and only after the evidence line is logged first.
- Budget thresholds remain watch-only and report-only (never auto-kill), per
  `D-BENCH-BUDGET-WATCH`.

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
  clone omits this it writes K_master to the DEFAULT `~/.wevibe/keys` while the readers look in the bench
  keystore home (`~/.wevibe/bench/leader-keystore` by default)
  → recall `decrypt_failed: Umbral re-encryption Internal validation failed` / invite `no master key
  found` (the 2026-07-13 blocker). Writer and readers MUST share this path. (K_master stays leader-local;
  hub never receives K_master or epoch_sk — this is a keystore-PATH routing fix, not a crypto change.)
Preferred one-path remains lifecycle `bring_up`; this standalone command only mirrors that lifecycle env.
```
cd /Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench && \
  WEVIBE_BENCH_WEVIBE_ROOT="${WEVIBE_BENCH_WEVIBE_ROOT:-$(cd .. && pwd)}" && \
  set -a && source config/bench.env && set +a && \
  WEVIBE_BENCH_LEADER_KEYSTORE="${WEVIBE_BENCH_LEADER_KEYSTORE:-$HOME/.wevibe/bench/leader-keystore}" && \
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
**CLEAN-START (wipe) INVARIANT — ONCE at benchmark start (not per run):** bench keystores now live under
`~/.wevibe/bench/{leader,contrib}-keystore` (moved off `/tmp` on 2026-07-26 after a power-loss `/tmp`
eviction destroyed the bench-org master key). A full chain wipe destroys the
on-chain org + its epoch key, so the LOCAL K_master in the bench keystores becomes STALE and MUST be
cleared IN THE SAME STEP, else the next `register-org` creates a fresh org whose epoch key mismatches
the stale K_master → `decrypt_failed`. Use the all-inclusive benchmark-start wipe path in
`Wipe cadence — ONE-WIPE at benchmark start, NEVER per run (Walter-locked 2026-07-24)` below.
```
make redeploy                               # (from wevibe-meta) ONE benchmark-start wipe: chain/pg/qdrant + served-cache
rm -rf "$WEVIBE_BENCH_LEADER_KEYSTORE" "$WEVIBE_BENCH_CONTRIB_KEYSTORE"   # clear STALE bench K_master (defaults resolve to ~/.wevibe/bench/{leader,contrib}-keystore; NEVER ~/.wevibe/keys)
# then run the residue check in the wipe-cadence section; any residue = STOP & FIX
# then start :4550 (above) — /v1/org-setup/finalize regenerates a fresh matching K_master into $WEVIBE_BENCH_LEADER_KEYSTORE
```
Per-run reset is CODE FIXTURE ONLY; do NOT re-wipe chain/pg/qdrant and do NOT reset the memory corpus.
`WEVIBE_BENCH_ENDPOINTS=1` enables the bench-only `/v1/submit` + `/v1/identity/pubkeys` endpoints.
The `/v1/health` route is always present (no bench flag needed).

## Open-source quickstart (.env)

For a clean open-source setup on the spend-proxy path:

1. Copy `.env.example` to `.env` in this repo.
2. Create a bench spend-proxy consumer token (shown once):
   ```bash
   uv run python scripts/spend_proxy_admin.py create-consumer --consumer-id bench
   ```
   - Admin token env is `SPEND_PROXY_ADMIN_TOKEN` (default: `spend_proxy_admin_dev`).
   - Save the returned raw token as `ORCAROUTER_API_KEY` in `.env` (never commit this).
   - There is no revoke endpoint today; use `list-consumers` to inspect existing consumers.
3. Optional local overrides in `.env`:
   - `WEVIBE_BENCH_SPEND_DB_DSN` (defaults to spend-proxy Postgres on `127.0.0.1:5440`).
   - `WEVIBE_BENCH_SPEND_PROXY_BASE_URL` (defaults to `http://127.0.0.1:4480/v1`).
4. Key resolution order for `ORCAROUTER_API_KEY` is:
   - `.env` first,
   - then process env,
   - then fallback to `~/.config/opencode/opencode.json` at
     `provider.orcarouter.options.apiKey`.
   Missing key/config fails loudly (`SpendKeyError`) and points to `.env.example`.

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
# If venv is not active: PYTHONPATH=. python scripts/docker_isolation_smoke.py
```
`tests/test_docker_isolation.py` checks isolation + wiring. `scripts/docker_isolation_smoke.py` is the local synthetic
smoke proving oracle/golden/runner/host paths are absent inside the container and exported `/work` edits are scored
host-side by a dummy oracle (NO paid model, NO live corpus).

### Worker model-acceptance probe (mandatory after any roster/worker-path change)
A model swap (or any worker-path change) is **not verified** until the worker image's OpenCode resolves the new slug
**inside the real worker container**. Curl-only checks are never sufficient.

Worker-catalog acceptance and transport acceptance are distinct gates:
- Catalog acceptance is whether OpenCode accepts the slug at model-resolution time.
- Transport acceptance is whether the HTTP/proxy path works once a request is emitted.
- `ProviderModelNotFoundError` (`Model not found`) is a catalog rejection thrown pre-HTTP; curl cannot observe it.

Enforcement is automatic: `verify_worker_model_acceptance` runs in `scripts/run_cumulative.py` preflight (alongside
`verify_org_checklist`) before any paid cell. It does a scratch `docker run --rm` per roster slug and fails fast in
seconds at $0, naming the rejected slug.

Manual repro shape: generate worktree `opencode.json` via `build_worker_opencode_config`, then run `docker run --rm -i -v <worktree>:/work:ro wevibe-bench-worker:v1 opencode run --model <slug> --dir /work --print-logs`.
`ProviderModelNotFoundError`/`Model not found` means catalog rejection; later classes (connection/auth) mean catalog
accepted and failure is downstream.

Incident refs: `27-07-26-1038-smoke3-kimik3-harness-error-model-not-found.md` (SMOKE-3 `harness_error` at turn 0)
and `27-07-26-0941-bench-glm-to-kimi-k3-swap.md`.

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

## Spend-proxy (:4480) — the ONE paid transport path

This is the only paid transport path for benchmark cells. The old per-cell
`:8789` openrouter proxy launch path is retired.

### Runtime architecture (sealed :4480 path)

- The bench uses the standing spend-proxy at `http://127.0.0.1:4480/v1`.
- Upstream routing is OrcaRouter-only.
- A dedicated bench consumer token is created once via
  `scripts/spend_proxy_admin.py create-consumer --consumer-id bench`; token is
  shown once and stored locally as `ORCAROUTER_API_KEY` in `.env`.
- Worker config sets provider `orcarouter` with container base URL
  `http://host.docker.internal:4480/v1`, `apiKey:"{env:ORCAROUTER_API_KEY}"`,
  and a model block mirroring manager session knobs (`name` / `reasoning` /
  `tool_call` / `limit`) plus worker-only
  `interleaved:{field:"reasoning_content"}` and
  `headers:{"X-Session-Id":<run_id>}`.
- `max_reasoning_tokens` is a dead field (not used).

### Key/config resolution and consumer lifecycle

- Key layer SoT: `wevibe_bench/spend_key.py`.
- `ORCAROUTER_API_KEY` resolution order:
  1. `.env` (repo-local, gitignored),
  2. process env,
  3. `~/.config/opencode/opencode.json`
     `provider.orcarouter.options.apiKey` fallback.
- Missing key raises loud `SpendKeyError` naming all checked locations and
  pointing to `.env.example`.
- Optional overrides:
  - `WEVIBE_BENCH_SPEND_DB_DSN`
  - `WEVIBE_BENCH_SPEND_PROXY_BASE_URL`
- Consumer admin:
  - Create: `uv run python scripts/spend_proxy_admin.py create-consumer --consumer-id bench`
  - List: `uv run python scripts/spend_proxy_admin.py list-consumers`
  - Admin token env: `SPEND_PROXY_ADMIN_TOKEN` (default `spend_proxy_admin_dev`)
  - No revoke endpoint exists today.

### Run path and attribution

- Scored ladder does not launch `:8789`; it uses standing `:4480` + bench token.
- Every worker/extraction LLM call carries `X-Session-Id:<run_id>` for spend
  attribution.
- Extraction LLM leg routes through the vendored MCP clone to `:4480` and now
  sets `X-Session-Id` on outbound calls.
- **Important:** the clone serves from `dist/`; attribution code changes require
  `npm run build` plus restart before they take effect.

### Metering (read-only) and budget semantics

- `wevibe_bench/proxy_meter.py` (`SpendMeter`) reads Postgres (default
  `127.0.0.1:5440`, DSN from key layer) and scopes by `session_id=<run_id>`.
- TRUE spend (budget truth): `SUM(actual_spend_usd)` (cache-discounted real money).
- BENCHMARK spend (scoring-only): `SUM(theoretical_spend_usd)` (synthetic full-price).
- Direct SQL is intentional (not `/v1/spend/sessions`) because that endpoint has
  no session filter and omits `upstream_model`.
- `budget.json` checkpoint flow is retired and kept only as marked reference.

### Safeguards on the spend-proxy path

- Run-start pricing verify via `/v1/pricing/models`; start aborts on drift;
  pinned pricing version:
  `c58e194db3f6a20e7d41b8c9e2f05a17`.
- In-run cap poll uses `SpendMeter.run_spend(run_id).true_usd`; harness aborts
  at cap. Proxy is watch-only here (it does not refuse requests).
- Model-identity watch compares `spend_events.upstream_model` basename against
  requested model; mismatch aborts the cell.
- `finish_reason:"length"` is logged as TRUNCATION (not treated as clean).
- `tool_choice:"required"` is banned and guard-tested.

### Zero-tool-turn bounded resume

If a worker turn ends with zero tool calls and no file writes, harness resumes
the SAME OpenCode session (`opencode run --session <id> --dir /work --format json`)
with a short nudge, bounded to at most 2 resumes, then fails honestly.
The large prompt is never resent.

### Per-model empirical probe matrix (REQUIRED before new rungs)

Before introducing any scored ladder rung beyond `kimi/kimi-k3`, run and record
an empirical multi-turn tool-call probe through `:4480` for each candidate.
Current pending roster:

- `gemini-3.1-pro`
- `tencent/hy3`
- `kimi/kimi-k2.7-code`
- `anthropic/claude-opus-4.8`
- `minimax/minimax-m3`

Status note: GLM-5.2 was not profiled (deselected on 2026-07-27).

### Retired: :8789 openrouter proxy + budget.json

Reference-only (do not re-activate):

- Prior flow launched `scripts/run_openrouter_proxy.py` per cell on `:8789`.
- Worker path used `http://host.docker.internal:8789/api/v1` plus per-run token files.
- Budgeting relied on per-run proxy checkpoint JSON (`budget.json` lineage).

This path was retired after spend-proxy `:4480` migration because standing
consumer tokens + DB-attributed spend by session provide the single sealed
transport/metering route. Legacy code/tests remain in-tree and explicitly
retired-marked for reference.

## THE HARD RULE

**NEVER build, compile, or start your own hub or mcp.** They already exist. If a recall fails:
1. Read the PreflightError — it names the down tier and the fix.
2. Bring the named service up with the command above (hub: `make redeploy`; clone: the `node dist/server.js` command).
3. If you cannot, STOP and report. Do NOT improvise infrastructure, do NOT compile a new hub/mcp, do NOT invent a fallback.

## Wipe cadence — ONE-WIPE at benchmark start, NEVER per run (Walter-locked 2026-07-24)

**INVARIANT (extends `D-BENCH-CUMULATIVE-LOOP-2026-07-23`):** wipe the benchmark stack EXACTLY ONCE at benchmark start.
Run the all-inclusive wipe from `wevibe-meta` via `make redeploy`, pair it with bench keystore clear
(`rm -rf "$WEVIBE_BENCH_LEADER_KEYSTORE" "$WEVIBE_BENCH_CONTRIB_KEYSTORE"`, defaults
`~/.wevibe/bench/{leader,contrib}-keystore`), then run residue verification before
any scored run. This is the only wipe/reset event for that benchmark campaign.

After that one wipe, subsequent runs MUST NEVER restart chain/pg/qdrant and MUST NEVER reset corpus state.
Each run reuses accumulated memories already in storage; only the run-local code fixture resets.

**Residue check (mandatory, immediately after the one wipe):** verify all of the following are true before proceeding:
1. Qdrant memory collections are empty or absent (no leftover benchmark vectors/documents).
2. Chain + Postgres state is fresh (new org/session state expected after redeploy).
3. Served cache is cleared.
4. Bench keystore residue is gone (`$WEVIBE_BENCH_LEADER_KEYSTORE`, `$WEVIBE_BENCH_CONTRIB_KEYSTORE` removed; defaults are `~/.wevibe/bench/{leader,contrib}-keystore`).
If any residue exists, STOP and FIX it before bringing benchmark flow back up.

**Re-baseline bar:** only a TRUE REGRESSION or TOTAL BENCHMARK FAILURE justifies re-baselining (going back to
drawing board). That action is deliberate + declared, never a casual re-wipe.

**Success/failure framing:** success is visible convergence across ON runs (more problems resolved and fewer
cycles/tool calls/tests/tokens/time, attempts-to-green trending down); failure is no demonstrable improvement
or integrity collapse.

**CAVEAT (required with this rule):** the provider-slug provenance → capability-eligibility path is canonized
(`D-PRODUCER-MODEL-PROVENANCE`, `D-CAPABILITY-ELIGIBILITY`) but UNBUILT/unproven in transport today
(`prod` attestation is `null`; provenance does not reach Qdrant), so this persistence ideology is design
INTENT until that path is proven through the real transport. Once proven, enforcement is permanent.

For the full canonical statement, see `BENCHMARK-DIARY.md` §18.

> **Caveat amendment (2026-07-24):** producer provenance NOW reaches Qdrant for memories committed after this date
> (R1 fix, bench `5394032` + hub `a171630`; first born-stamped commits 9/9 × 3 legs). The pre-fix 8 stay unstamped
> (Option A leave-and-disclose). Production attestation is still `null` and the capability-eligibility FILTER is
> still unbuilt — the caveat above stands for enforcement, narrowed for the stamping leg.

## Injection cadence canon + memory-block token metering (PENDING implementation — D-INJECTION-CADENCE-2026-07-24)

Canon ratified 2026-07-24 (`wevibe-docs/DECISIONS.md` §23; Walter, "resounding yes"): a recalled-and-accepted
memory is injected ONCE at acceptance (stable early position after system instructions), NOT re-pushed per turn;
the served set is hub-ranked top-K within a fixed token budget; the injected block is preserved VERBATIM across
compaction (restore-verbatim, never summarize-through). **In all benchmark measurement arms, the memory block's
tokens MUST be metered and reported separately from the model's work tokens** — every OFF/ON progress vector that
reports tokens must carry the injected-memory-token count as its own field. JIT/reference-based progressive
disclosure is PARKED as a future architecture seam, not a cadence flag.

Status: implementation DISPATCHED 2026-07-25 (plugin inject-once + verbatim compaction preserve + bench token
metering; ledger `wevibe-meta/workspace/reports/25-07-26-0341-injection-cadence-implementation-LEDGER.md`).
**Worker-image REVENDOR is required before R2** — the vendored plugin in `wevibe-bench-worker:v1` must carry the
new cadence code. Until the revendored image lands, the plugin still re-injects the eligible block every turn
(superseded MM-1 behavior); do not report cadence effects as canon-conformant before then. This canon supersedes
per-turn re-injection and resolves MM-1 in the spec's direction (code→spec).

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
