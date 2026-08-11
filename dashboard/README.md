# WeVibe Bench Dashboard

A live instrument for the WeVibe memory benchmark. Wall-dominant board, Midnight
phosphor palette, built to be read at a glance on a stream and to survive a
skeptical engineer reading it closely.

```bash
node server.mjs            # http://127.0.0.1:7717 — live artifacts
node server.mjs --mock     # generated data, for design/demo without a run
```

**No dependencies. No build step. No `npm install`.** Node 18+ stdlib only.

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
sources/
  _runtime.mjs        module isolation: timeouts, tail-bounded reads, merge
  run-manifest.mjs    provenance: policy anchor, levers, org, model
  status-stream.mjs   AUTHORITATIVE — gates, arm, verdict (RC-5)
  run-log.mjs         the live pulse between attempt records
  truncation.mjs      transport anomalies
  funnel-cells.mjs    plugin funnel counters      (ON cells only)
  plugin-log.mjs      recall latency p50/p95      (ON cells only)
  opencode-serve.mjs  live token burn             (opt-in, localhost)
  hub-db.mjs          candidate relevance/standing (opt-in, DISABLED default)
index.html + board.js + panels/   the board
mock.mjs              realistic generator incl. the ugly cases
```

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

- **Read-only.** Nothing opens a file for write.
- **Binds `127.0.0.1`.** Exposing it requires an explicit `--host`.
- **Tail-bounded reads** (256KB). A six-hour log costs the same as a fresh one.
- **Fixed static allowlist** — no dynamic path resolution, so traversal is
  impossible by construction.
- **`hub-db` ships disabled** because it shells out to `docker`. You do not need
  docker, a database, or any part of the WeVibe stack to run this dashboard.
- **No network calls** except the opt-in localhost agent API.
- **No CDN, no webfont fetch.** The board renders offline.
- **Privacy:** memory plaintext, raw queries and full CIDs never reach the
  board. Everything rendered is assumed public forever.

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
