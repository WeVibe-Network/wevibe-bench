# USER BENCHMARKING GUIDE — run the campaign yourself

Operator guide. Every command here is copy-pasteable and was executed on 2026-08-11.
Authority: `RUNBOOK.md` is the run card and wins on rules. This guide is the
*how do I type it* layer — it never overrides the card.

**Shell note.** All commands assume you are in the bench repo:

```bash
cd /Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench
```

---

## 0. THE FIVE RULES THAT BREAK RUNS

Read these once. Each one has cost a real run.

1. **`nohup ... &` needs `< /dev/null`.** Without it zsh suspends the job the moment
   the process touches stdin (`suspended (tty input)`), leaving a half-built manifest
   and a live container. Always redirect stdin.
2. **`$TS` does not survive between shell commands you paste separately.** Set it and
   launch in ONE command, or just use the literal filename. `runs/off-cell-<TS>.log`
   is a placeholder — substitute the real name.
3. **Main-parser flags go BEFORE the subcommand.** `--org --model --roster-model
   --task --seed --manifest` precede `run`. After `run`, argparse exits 2.
4. **One wipe, before the first cell. Never after.** A wipe once cells exist silently
   converts every later ON cell into an OFF cell and the campaign reports no lift.
5. **The stack must be verified clean before you launch** — 14/14, no exceptions.

---

## 1. MODELS — the full roster

Bench aliases are defined in `/Users/jerrysmith/Desktop/Local LLM Proxy/config/models.yaml`.
They are distinct from interactive aliases: bench aliases carry FULL native context
(262144) because a truncated context = no visible headroom = void cells.

| Alias (`--model`) | Backend | Upstream model | Context | Output |
|---|---|---|---|---|
| `qwen3.6-35b-a3b-bench` | oMLX | `Qwen3.6-35B-A3B-MLX-8bit` | 262144 | 32768 |
| `qwen3.6-40b-deckard-bench` | LM Studio | `qwen3.6-40b-claude-4.6-opus-deckard-heretic-uncensored-thinking-neo-code-di-imatrix-max` | 262144 | 32768 |
| `qwen3.6-27b-fable-bench` | LM Studio | `qwen3.6-27b-fable-fusion-711-uncensored-heretic-nm-dau-neo-max-mtp` | 262144 | 32768 |
| `wevibe-bench-worker` | oMLX | `auto` (whatever is resident) | 262144 | 32768 |

Two more aliases exist but are NOT subject models:
- `wevibe-bench-poller` — 32k, the poller's judge role only.
- `wevibe-bench-worker` — the neutral "whatever is resident" slug. Use it only when you
  deliberately do not want to pin a model.

**You never run `lms load` yourself.** Passing `--model <alias>` makes the proxy load
that exact model on the first request (exclusive load — other oMLX models unload, LM
Studio is evicted). This is the documented mechanism; a manual TTL'd load previously
auto-unloaded a model mid-campaign and voided a cell.

**Switching models mid-campaign changes the roster hash** and the existing manifest
will reject the run with "roster hash drift detected". That is by design — one manifest
= one subject model. To switch, archive and start a new manifest:

```bash
mv runs/cumulative runs/cumulative.<why>-<date>
```

Archiving does NOT touch the server corpus. Corpus lifetime is governed by the wipe
rules (§0 rule 4).

---

## 2. PREFLIGHT — four ports, one image

All four must answer:

```bash
for p in 4545 4550 4451 4440; do nc -z 127.0.0.1 $p && echo "$p OK" || echo "$p FAIL"; done
```

| Port | What | If it fails |
|---|---|---|
| 4545 | local relay proxy (subject + extraction models) | Do NOT restart it. Escalate. |
| 4550 | **leader** bench clone MCP (identity `f7733d6e`) | `cd ../wevibe-meta && bash scripts/bench-clone.sh start leader` |
| 4451 | **contributor** bench clone MCP (identity `5292550d`) | `cd ../wevibe-meta && bash scripts/bench-clone.sh start contributor` |
| 4440 | hub | Part of the docker stack — see §3 wipe. |

Both clones are **managed services**: the harness connects to them, it never spawns
them. See §7 for why this matters and what it broke.

**Worker image** — rebuild ONLY if `docker/worker/` changed since the last build. The
vendored opencode plugin is baked in at build time, so a stale image silently runs a
stale plugin:

```bash
# image build time (UTC):
docker image inspect wevibe-bench-worker:v1 --format '{{.Created}}'
# newest source file under docker/worker (prints LOCAL time — convert before comparing):
find docker/worker -type f -exec stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%SZ' {} \; | sort -r | head -3
```

If the source is newer than the image:

```bash
docker build -t wevibe-bench-worker:v1 docker/worker
```

---

## 3. THE CAMPAIGN SEQUENCE

> **test** → **wipe** (once) → **OFF cell** → **extract** → **first scored ON cell** → **extract** → continue

Each stage is its own invocation. Nothing is nested. **There are no smoke stages**
(removed 2026-08-11 — a smoke needs a full session + extraction + approval to prove
recall, so it could never certify anything standalone).

### 3.1 TEST — gate, all green

```bash
.venv/bin/python -m pytest -q
```

Expect `596 passed, 1 skipped`. Nothing proceeds on a red suite, and nothing proceeds
on an unverified one. The suite runs under xdist (`-n auto` via pyproject addopts) —
do not pass `-p no:xdist`, it conflicts with the configured addopts and errors.

### 3.2 WIPE — once, before the first cell

```bash
cd ../wevibe-meta && make redeploy 2>&1 | tail -20
```

This stops both clones, sweeps strays, destroys compose volumes, wipes bench + host
state, brings the stack back up, rebuilds, restarts both clones, and verifies.

**Gate: `=== VERIFY-CLEAN: PASS (14/14) ===`.** Any FAIL: STOP and fix. The 14 checks:

| # | Check | What it protects |
|---|---|---|
| 1-3 | containers-up, no-stale-containers, volumes-whitelisted | stack shape |
| 4 | qdrant-empty | no residual vectors |
| 5 | chain-young | fresh chain (height ≤ 1500) |
| 6 | postgres-zero | orgs=0 members=0 |
| 7-8 | host-state-clean, preserved-intact | host residue gone, your keys untouched |
| 9-10 | bench-keystores-fresh, bench-state-clean | stale `K_master` cannot survive |
| 11 | clone-fresh-4550 | leader clone live **and identity `f7733d6e`** |
| 12 | clone-fresh-4451 | contributor clone live **and identity `5292550d`** |
| 13 | contributor-3001 | dashboard |
| 14 | no-stray-clones | no orphan clones from aborted runs |

Checks 11/12 assert the served ed25519 fingerprint, not just liveness — a live clone
serving the WRONG identity is exactly the defect that burned 2026-08-11 (§7).

**Why the keystore wipe is mandatory:** the chain wipe destroys the on-chain org and
its epoch key, so local `K_master` goes stale. Skip it and the next `register-org`
creates an org whose epoch key mismatches, and recall returns `decrypt_failed` — which
surfaces hours later on the first ON cell, looking like a recall bug.

### 3.3 OFF CELL — the first cell, always unscored

The corpus is empty by construction, so the first OFF cell exists to build one.

```bash
cd /Users/jerrysmith/Desktop/wevibe-workspace/wevibe-bench
TS=$(date +%Y%m%dT%H%M%S) && nohup .venv/bin/python scripts/run_cumulative.py \
  --model qwen3.6-35b-a3b-bench \
  run --until-review --mode off \
  < /dev/null > "runs/off-cell-$TS.log" 2>&1 & disown
echo "log=runs/off-cell-$TS.log"
```

Note the three things that matter: `--model` **before** `run`, `< /dev/null`, and
`TS` set in the SAME command as the launch.

`--mode off` only *validates* that the current cell is an OFF cell — it does not
restructure the schedule. `--org` is optional for OFF (falls back to `wevibe-org-0`).

### 3.4 EXTRACT — separate invocation, after every cell

```bash
.venv/bin/python scripts/run_cumulative.py \
  --org wevibe-org-0 --model qwen3.6-35b-a3b-bench extract
```

Never fold this into the run command.

**`--model` must match the manifest's model on EVERY subcommand**, not just `run`.
The model pin feeds the roster hash, so omitting it makes the tool compute a different
hash and refuse:

```
ValueError: cannot resume: roster hash drift detected (manifest=0e99e683… expected=2a8f3ecd…); start a fresh run
```

That error means "you forgot `--model`", not "your manifest is corrupt". Same applies
to `state`, `resume`, `list-pending`, and the rest.

### 3.5 ON CELL — the first scored cell

```bash
TS=$(date +%Y%m%dT%H%M%S) && nohup .venv/bin/python scripts/run_cumulative.py \
  --org wevibe-org-0 --model qwen3.6-35b-a3b-bench \
  run --until-review --mode on \
  < /dev/null > "runs/on-cell-$TS.log" 2>&1 & disown
echo "log=runs/on-cell-$TS.log"
```

**`--org` is REQUIRED for ON cells** — omitting it errors before the run starts.
One org for the WHOLE campaign; an org per arm or per model breaks corpus
accumulation, which is the only thing being measured.

**The first ON cell IS the delivery verification.** Check the injection seams:

```bash
grep -E "injected_count|injected_block_chars|injected_block_est_tokens|consumer_injected_count" runs/on-cell-<TS>.log | tail
```

Non-null values = recall delivery proven in-band through the real transport. **Null
values on an ON cell = rule-18 walk-back** — stop and declare it, do not continue.

---

## 4. WATCHING A RUN

### Get the session id and attach

```bash
grep "step=live-view" runs/off-cell-<TS>.log | tail -3
```

That line carries `session_id=ses_...` and a ready-made `attach_cmd='...'`. Also
written to a marker file:

```bash
cat runs/cumulative/sessions/*/live-view.txt
```

Attach:

```bash
opencode attach http://127.0.0.1:4096 --session <ses_...>
```

**`--session` is not optional.** Without it the TUI opens its own new-session view
instead of the live worker session (`serve_client.py:600-608`).

### Confirm the org bootstrap cleared

This is where runs died all day on 2026-08-11. Check it FIRST — before waiting on the
model — because a failure here is instant and terminal:

```bash
grep -E "org_setup_mcp_identity_verified|step=create_org |poll_leader_membership|contributor_pubkeys|ERROR" runs/off-cell-<TS>.log
```

Healthy looks exactly like this:

```
phase=org_setup_mcp_identity_verified mcp_url=http://127.0.0.1:4550 leader_ed_fp=f7733d6e status=ok
step=create_org status=ok dur_ms=5312
step=poll_leader_membership status=ok dur_ms=7
step=contributor_pubkeys status=ok dur_ms=2
```

Any `ERROR` in that window: stop, read §7, do not wait.

### Progress

```bash
tail -f runs/off-cell-<TS>.log | grep PROGRESS
```

Deterministic sensor only — **no poller, no LLM judge**. At high accumulated context
(>~150K tokens) turns can take many minutes: SWA/hybrid prefill reprocessing is slow,
and tool-call argument frames can arrive in ONE chunk after long silence on the qwen35
architecture. **Argument-silence is not a stall.**

---

## 5. STOPPING / ABORTING A RUN CLEANLY

If you kill a run, it leaves a container and a partial manifest. Clean all three:

```bash
pkill -f run_cumulative.py
docker ps -a --filter "name=wevibe-bench-cell-" --format '{{.Names}}' | xargs -r docker rm -f
mv runs/cumulative runs/cumulative.aborted-$(date +%Y%m%dT%H%M)
```

Then decide honestly: if the aborted run minted an org on the chain (check
`postgres-zero`), the stack is no longer clean and you need a wipe — which is only
sanctioned if **zero cells have completed**. If cells have run, you do NOT re-wipe;
you declare a rule-18 walk-back.

```bash
docker exec wevibe-postgres psql -U wevibe -d wevibe_hub -tAc \
  "select (select count(*) from orgs), (select count(*) from members);"
```

---

## 6. HOLD THE STACK FOR UI REVIEW (optional)

Set `WEVIBE_BENCH_HOLD_UI=1` on the run environment. At benchmark end the cell is NOT
torn down: the artifact's server boots host-side from the bind-mounted worktree on
`http://localhost:8002` and the run waits. Release with:

```bash
touch <run_dir>/RELEASE_HOLD
```

Never set this on an unattended cell — the run waits until released or killed.

---

## 7. THE 2026-08-11 FAILURE — what it looked like, why it happened

Read this before debugging any org-bootstrap failure. Two independent defects produced
one symptom, which is why it took a full day.

### Symptom

```
RuntimeError: leader membership did not include org_id=wevibe-org-0
```

30 seconds after `create_org`, on a fresh stack. Sometimes preceded by an
**unexpected Touch ID / biometric prompt**.

### Cause 1 — the org was minted under the wrong identity

`lconfig.py` defaulted `leader_mcp_url` to `http://127.0.0.1:4450`. That is the **real
host wevibe-mcp** — Walter's daily driver. It has **no seed support** and always loads
the interactive keychain identity (`05c4b8cb…`).

The chain of consequences:
1. `create_org` passes that URL to leader-signer as `WEVIBE_MCP_URL`.
2. leader-signer calls `POST /v1/org-setup` on it.
3. That endpoint stamps **that MCP's** pubkey as the org's leader → `05c4b8cb…`.
4. The hub writes it as the org's only `members` row.
5. The harness then polls for membership as **itself** (`8d46fc08…`, fp `f7733d6e`).
6. It never finds itself. 30 s later: RuntimeError.

The Touch ID prompt was the tell: it appeared only when the run took the *fresh-create*
path. Every earlier run reused an existing org (`phase=reuse`) and never called
org-setup at all — which is why the prompt seemed to come and go at random.

**Fix:** default is now `:4550`, the seed-derived bench leader clone. Plus a fail-fast
guard in `create_org` that probes `/v1/identity/pubkeys` and refuses to register unless
the org-setup MCP's ed25519 key IS the harness leader's. Unreachable is also a hard
fail — a run on an unverified seam is VOID-INSTRUMENT by construction.

### Cause 2 — the contributor MCP was an orphan process

The cumulative run path **never calls `bring_up()`**. It assumes both MCPs are already
running. But only `:4550` was a managed service — nothing started `:4451`.

The campaign had been silently depending on an **Aug-7 orphan process** to serve the
contributor identity. It survived every wipe and predated the current dist by days.
When it was finally swept, runs failed at:

```
step=contributor_pubkeys err=mcp unreachable for /v1/identity/pubkeys
```

**Fix:** `bench-clone.sh` now takes a role (`leader`|`contributor`), `make redeploy`
starts both, and verify-clean checks 11/12 assert BOTH clones' identity fingerprints.

**A subtle trap this exposed:** `config/bench.env` exports `WEVIBE_IDENTITY_SEED_HEX`
globally as the **leader** seed. A contributor started without an explicit per-role
seed override silently serves the **leader's** identity on `:4451` — two ports, one
identity, contributor memories attributed to the leader. `bench-clone.sh` now sets the
seed per role explicitly, and check 12 would catch it.

### The general lesson

Both defects passed every liveness check. A process was up; a port answered; health
returned 200. **Liveness is not identity.** Anything that mints or signs must have its
identity asserted, not assumed — which is why the checks now compare fingerprints.

---

## 8. QUICK REFERENCE

```bash
# preflight
for p in 4545 4550 4451 4440; do nc -z 127.0.0.1 $p && echo "$p OK" || echo "$p FAIL"; done

# test
.venv/bin/python -m pytest -q

# wipe (once, before first cell)
cd ../wevibe-meta && make redeploy 2>&1 | tail -20

# start either clone by hand
cd ../wevibe-meta && bash scripts/bench-clone.sh start leader
cd ../wevibe-meta && bash scripts/bench-clone.sh start contributor
cd ../wevibe-meta && bash scripts/bench-clone.sh sweep     # kill orphans, keep managed

# OFF cell
TS=$(date +%Y%m%dT%H%M%S) && nohup .venv/bin/python scripts/run_cumulative.py \
  --model qwen3.6-35b-a3b-bench run --until-review --mode off \
  < /dev/null > "runs/off-cell-$TS.log" 2>&1 & disown

# ON cell
TS=$(date +%Y%m%dT%H%M%S) && nohup .venv/bin/python scripts/run_cumulative.py \
  --org wevibe-org-0 --model qwen3.6-35b-a3b-bench run --until-review --mode on \
  < /dev/null > "runs/on-cell-$TS.log" 2>&1 & disown

# extract (after every cell) — --model REQUIRED, must match the manifest
.venv/bin/python scripts/run_cumulative.py \
  --org wevibe-org-0 --model qwen3.6-35b-a3b-bench extract

# session id + attach
grep "step=live-view" runs/<log> | tail -3
opencode attach http://127.0.0.1:4096 --session <ses_...>

# state — --model REQUIRED (omitting it = "roster hash drift detected")
.venv/bin/python scripts/run_cumulative.py --model qwen3.6-35b-a3b-bench state

# abort cleanly
pkill -f run_cumulative.py
docker ps -a --filter "name=wevibe-bench-cell-" --format '{{.Names}}' | xargs -r docker rm -f
mv runs/cumulative runs/cumulative.aborted-$(date +%Y%m%dT%H%M)
```
