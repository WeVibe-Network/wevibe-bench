# WeVibe Bench Dashboard

A live instrument for the WeVibe memory benchmark. Wall-dominant board, Midnight
phosphor palette, built to be read at a glance on a stream and to survive a
skeptical engineer reading it closely.

```bash
docker compose up -d          # → http://localhost:7717
```

In Docker Desktop it appears as `wevibe-bench-dashboard` with 7717 as a
clickable link and a health dot that goes green once the board assembles.

Host mode still works and needs no install — Node 18+ stdlib only:

```bash
node server.mjs               # → http://127.0.0.1:7717
node server.mjs --help
```

**No dependencies. No build step. No `npm install`.** If this ever needs a
package manager to start, something has been added that should not have been.
The tests run on the stdlib runner for the same reason:

```bash
cd wevibe-bench/dashboard && node --test
```

The bench repo is mounted **read-only** at `/bench`. That `:ro` is not
decoration: run artifacts under `runs/` are the authoritative record of a
measurement (RC-5), and a read-only mount makes "the dashboard corrupted a run"
structurally impossible rather than merely unlikely.

There is **no mock mode**. The board renders whatever the artifacts carry,
including nothing — the empty states are designed for exactly that.

---

## What it measures, and what it refuses to claim

Three claims, three artifacts, never merged:

| Artifact | Claims | Does **not** claim |
|---|---|---|
| serve | a memory was injected into context | that it helped |
| outcome | the episode resolved, or didn't | that the memory caused it |
| **arm delta** | memory-on resolves more than control | — this is the only causal surface |

Consequences that are enforced in code, not by convention:

- **A serve count is never a success metric.** Serves live in the honesty rail
  labelled *delivery, not outcome*, and that box is deliberately the quietest.
- **No delta below `min_cells_per_arm` (3).** The hero renders `COLLECTING`
  with the real cell counts instead. A number at n=1 is what gets you killed.
- **Only VALID cells enter the delta**, and `cells` counts scored cells only.
  A void-instrument cell (RUNBOOK rule 5.10 — provider-side truncation on a
  non-green terminal attempt) or a single-attempt cell is excluded and counted
  in `arm_delta.<arm>.excluded`, never scored as a measured 0%. Both used to
  contribute 0 to the numerator and their full gate count to the denominator,
  which manufactured apparent lift for the memory arm the moment the threshold
  unlocked. `contract.mjs::cellValidity` MIRRORS the scorecard's canonical rule
  in `wevibe_bench/cumulative/run_artifacts.py` — if that rule moves, this moves
  with it. Pinned by `arm-delta-validity.test.mjs`.
- **No confidence interval over gate counts, ever.** Gates cluster within cell —
  68 gates from one cell are not 68 independent samples. The standing note says
  so permanently.
- **Outcome is tri-state** (`worked` / `didnt_work` / `unobserved`). Silence is
  not a vote, and `unobserved` is styled as a third state, never as failure.
- **Memories render as four labelled atomic fields** (`implement`, `context`,
  `dnd`, `stack`). Never collapsed into one blob. A null `dnd` shows as null.
- **The gate-mode label is permanent and derived**, read from the recorded
  `L4_WEVIBE_RECALL_MODE` lever — never hardcoded. In benchmark mode the
  approval gate auto-approves and the board says so next to the recall panel.
- **`bench-mock/self-declared`** renders permanently as plain label text. Never
  a badge, never a tier.

Not present, because they do not exist upstream: verification tiers T0–T4,
ablation receipts, shadow recall. If you see one, it is a bug.

---

## Three kinds of nothing

The board distinguishes these everywhere, because collapsing them is how a
dashboard starts lying:

| State | Means |
|---|---|
| `unobserved` | not measured yet |
| `unwired` | the source that would carry it is not connected |
| `0` | measured, and the answer is zero — a real result |

Most of a real run is null. The empty states are designed, not incidental.

---

## Architecture

```
contract.mjs          the versioned JSON contract + null-safe helpers
server.mjs            zero-dep read-only HTTP server
Dockerfile            single stage — there is nothing to build
docker-compose.yml    read-only mount, host-only port, opt-in hub-db
sources/
  _runtime.mjs        module isolation: timeouts, tail-bounded reads, merge
  run-manifest.mjs    provenance: policy anchor, levers, org, model
  status-stream.mjs   AUTHORITATIVE — gates, arm, verdict (RC-5)
  run-log.mjs         the live pulse between attempt records
  truncation.mjs      transport anomalies
  funnel-cells.mjs    plugin funnel counters      (ON cells only)
  plugin-log.mjs      recall latency p50/p95      (ON cells only)
  opencode-serve.mjs  live token burn             (opt-in, host API)
  hub-db.mjs          candidate relevance/standing (opt-in, DISABLED default)
index.html + board.js + panels/   the board
```

### Configuration

Env vars override the config file, so the container is reconfigured with
`docker run -e …` or a compose `environment:` block — never a rebuild:

| Var | Default | Purpose |
|---|---|---|
| `WEVIBE_DASH_HOST` | `127.0.0.1` (image: `0.0.0.0`) | bind address |
| `WEVIBE_DASH_PORT` | `7717` | port |
| `WEVIBE_DASH_BENCH_ROOT` | `..` (image: `/bench`) | bench repo root |
| `WEVIBE_DASH_POLL_MS` | `2000` | refresh cadence |
| `WEVIBE_DASH_OPENCODE_URL` | `http://127.0.0.1:4096` | live agent API |
| `WEVIBE_DASH_SOURCE_<NAME>` | — | force a source on/off |
| `WEVIBE_DASH_HUBDB` | off | enable the hub-db source |
| `WEVIBE_HUB_DB_{HOST,PORT,USER,NAME,PASSWORD}` | — | hub postgres |

The password is read from the environment at query time. It is never written to
config, never logged, and never returned by `/api/health`.

### Adding a source

Drop a file in `sources/` exporting `id`, `fields`, `describe()` and
`async read(ctx)` returning `{ ok, patch, provenance, reason? }`, then register
it in `dashboard.config.json`. The merge is additive: `null` never overwrites a
value, so enabling a source can only add information.

**A source cannot take the board down.** Each read is isolated behind a 2s
timeout and a try/catch. A module that throws, hangs, is absent, or fails to
import is reported `unwired` with a reason, and its fields stay null — which is
already a designed UI state.

---

## Safety

Written to be run by anyone, out of the box, without wrecking their machine:

- **Read-only.** Nothing opens a file for write. The bench mount is `:ro`, so
  this is enforced by the kernel, not by good intentions.
- **Runs as a non-root user** (`node`, uid 1000) with no writable state.
- **The docker socket is never mounted.** `hub-db` connects to postgres over
  TCP. Handing a read-only dashboard control of the host docker daemon in order
  to read four tables is an absurd trade, so it isn't made.
- **Host-only port by default.** `WEVIBE_BIND_HOST=0.0.0.0` to expose on the
  LAN — a deliberate act. That reaches your local network only: an RFC1918
  address is not routable from the internet, so it exposes nothing outward
  unless you separately add a router port-forward. Verified surface when
  exposed: `POST → 405`, traversal → `404`, non-allowlisted file → `404`,
  `touch /bench/…` → `Read-only file system`, container `uid=1000(node)`.
  Anyone already on the LAN can read gate ids, token counts and run metadata —
  no plaintext, no keys.
- **Tail-bounded reads** (256KB). A six-hour log costs the same as a fresh one.
- **Fixed static allowlist** — no dynamic path resolution, so traversal is
  impossible by construction.
- **`hub-db` ships disabled.** You do not need a database, docker, or any part
  of the WeVibe stack for the board to come up.
- **No CDN, no webfont fetch.** The board renders offline.
- **Privacy:** memory plaintext, raw queries and full CIDs never reach the
  board. `query_log.query_text` is never selected. Everything rendered is
  assumed public forever.

---

## Palette — Midnight (why)

Arm identity is the spine of the board, so the two accents must survive stream
compression. Midnight is the one sanctioned WeVibe theme that is not a
single-hue ramp — it ships two accents on the same surface:

| Token | Hex | Role |
|---|---|---|
| `--accent` | `#82aaff` | **ARM A · memory on** |
| `--num` | `#f78c6c` | **ARM B · control** |
| `--danger` | `#ff6b6b` | failing gate — **never** an arm |
| `--check` | `#5ad27a` | resolved gate — **never** an arm |

The two arm accents differ in **luminance as well as hue** (L\*≈70 vs ≈68 with
opposed hue), so they survive 4:2:0 chroma subsampling at 720p, stay distinct in
greyscale, and read for viewers with red-green colour vision deficiency. Arm
identity is additionally carried in **words** (`MEMORY ON` / `CONTROL`), so the
board never depends on colour alone.

Red and green are reserved for gate verdicts. An arm accent that reads as a
verdict is a lie.

## Type & motion

JetBrains Mono, system-loaded (no network). 14px floor, tabular figures
anywhere a number updates in place. No information requires hover.

**No ambient animation.** The board is up for hours; constant motion is
exhausting and reads as filler. State changes announce once (200ms) and settle.
The entire motion budget goes to the recall-moment takeover. `prefers-reduced-motion`
is honoured.

---

## Deliberate deviation from the brief

The brief asked for single-file React. This is dependency-free vanilla JS with
the component structure preserved (pure render functions over one state object).
React from a CDN would make the board a blank page the moment the network
hiccups — unacceptable on a live stream, for zero benefit. The contract still
sits at the top of `index.html` as a comment, as asked.
