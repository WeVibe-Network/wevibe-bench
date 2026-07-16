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
or recall cannot decrypt the seeded corpus. FOUR non-obvious env requirements (all baked into
`config/bench.env`; a standalone start that omits ANY of them fails):
- `WEVIBE_UMBRAL_SIDECAR_BIN` + `WEVIBE_GUARD_BIN` — the canonical wevibe-plugin normally injects
  both from wevibeRoot; a manual `node dist/server.js` does NOT, so `register-org` 500s with
  "WEVIBE_UMBRAL_SIDECAR_BIN environment variable is required" (epoch Umbral pubkey). This caused
  the 2026-07-13 cell-1 ladder abort. `source config/bench.env` provides both.
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
```
cd /Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench && set -a && source config/bench.env && set +a && \
cd scaffold/wevibe-mcp-clone && \
  WEVIBE_MCP_HTTP_ONLY=1 WEVIBE_MCP_HTTP_PORT=4550 WEVIBE_HTTP_HOST=127.0.0.1 \
  WEVIBE_BENCH_ENDPOINTS=1 WEVIBE_SEED_BACKEND=env \
  WEVIBE_IDENTITY_SEED_HEX="$WEVIBE_BENCH_LEADER_SEED_HEX" \
  WEVIBE_KEYSTORE_PATH="$WEVIBE_BENCH_LEADER_KEYSTORE" \
  WEVIBE_UMBRAL_SIDECAR_BIN="$WEVIBE_UMBRAL_SIDECAR_BIN" \
  WEVIBE_GUARD_BIN="$WEVIBE_GUARD_BIN" \
  WEVIBE_RECALL_MODE="$WEVIBE_RECALL_MODE" \
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

### Later (paid) smoke — DO NOT RUN in setup; Walter-gated
After a clean wipe (`make redeploy` from `wevibe-meta` + clear `/tmp` bench keystores): start `:4550`, run ONE
GLM-5.2 OFF cell in Docker, let the host oracle score it, extract one memory, verify accepted storage + retrieval
through the real transport, then wipe again.

**Prerequisite gate (Option A, 15-07-26):** before the conditional GLM-5.2/MiMo-V2.5-Pro roster unlocks,
Opus-4.8 at HIGH reasoning must first PASS one clean recall-OFF Docker smoke of the repaired benchmark under a hard
cumulative paid ceiling of $12. The prior Opus-4.8 HIGH passability smoke already ran and FAILED at $11.8035 on four
now-repaired gates (`15-07-26-1019-opus48-high-passability-smoke.md`), so the repaired instrument awaits a fresh
clean PASS before any scored roster/candidate work.

**Provider-pinning/roster is separate and still blocked:** provider-routing enforcement now exists via the host-side
proxy, and all three pins are live-verified transport+eligibility GREEN (Novita/DeepInfra/Anthropic, 15-07-26). Real-transport
passability is PROVEN at both the direct-HTTP and Docker worker→host-proxy layers. Candidate/scored authorization
nonetheless remains blocked pending the repaired-instrument Opus passability smoke (the prior HIGH smoke FAILED the oracle)
and Walter's explicit run authorization. (Live GLM/MiMo/Opus PRICING and the Opus provider pin `anthropic` are now supplied/verified.)

**This is the paid validation smoke; requires Walter roster confirmation; NOT part of build/verify.**

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
