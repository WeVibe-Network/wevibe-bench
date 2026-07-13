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
The clone lives at `/Users/jerrysmith/Desktop/benchmark/scaffold/wevibe-mcp-clone`. Its `dist/` is
prebuilt (rebuild with `npx tsc` only if stale). Start it with the SAME env the bench lifecycle
orchestrator uses — the identity seed MUST be the bench leader seed (`$WEVIBE_BENCH_LEADER_SEED_HEX`)
or recall cannot decrypt the seeded corpus:
```
cd /Users/jerrysmith/Desktop/benchmark/scaffold/wevibe-mcp-clone && \
  WEVIBE_MCP_HTTP_PORT=4550 WEVIBE_HTTP_HOST=127.0.0.1 \
  WEVIBE_BENCH_ENDPOINTS=1 WEVIBE_SEED_BACKEND=env \
  WEVIBE_IDENTITY_SEED_HEX="$WEVIBE_BENCH_LEADER_SEED_HEX" \
  WEVIBE_HUB_URL=http://127.0.0.1:4440 \
  node dist/server.js
```
`WEVIBE_BENCH_ENDPOINTS=1` enables the bench-only `/v1/submit` + `/v1/identity/pubkeys` endpoints.
The `/v1/health` route is always present (no bench flag needed).

## THE HARD RULE

**NEVER build, compile, or start your own hub or mcp.** They already exist. If a recall fails:
1. Read the PreflightError — it names the down tier and the fix.
2. Bring the named service up with the command above (hub: `make redeploy`; clone: the `node dist/server.js` command).
3. If you cannot, STOP and report. Do NOT improvise infrastructure, do NOT compile a new hub/mcp, do NOT invent a fallback.
