# wevibe-bench

A minimal **OFF/ON coding-agent memory ablation** harness scaffold for WeVibe.

This is a **scaffold, not a validated tool.** Consistent with BENCHMARK
INTEGRITY, it produces **no numbers until it is wired to the live hub and
end-to-end delivery is verified**.

## Install + run tests (offline)

From `wevibe-bench/`:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[test]'
python -m pytest -q
```

## What it does

For each `(model, task)` on a capability ladder, the harness runs the same task twice:

- **OFF** — `NoneBackend`, nothing injected (control).
- **ON** — `WeVibeBackend`, memory recalled through `/v1/recall` (treatment).

Then it diffs OFF vs ON: capability lift (pass@1), **total tokens** (input+output),
turns, and cost. If ON delivery verification fails, that ON cell is recorded as
`not_scored` and excluded from capability/cost/token deltas.

## Design commitments (structural)

- **INV-6** — only `NeedCard.prompt_digest` (intent + task prose) is dense-channel
  content; stack/deps/errors/files/directory/project are keyword-channel metadata.
- **MC-1 symmetry** — `AgentRunner.build_need_card` must mirror live plugin harvest.
- **BENCHMARK INTEGRITY** — with `require_delivery_verification=True`, any ON cell
  whose delivery verdict is not `YES` is `not_scored`.
- **Reproducibility** — `RunConfig.rng_seed` is pinned; scorecards embed full config,
  seed, harness version, and timestamp manifest.
- **Total tokens** — scorecard math is always `input + output`.
- **Seed → held-out** — `seeding.build_split` enforces temporal split and grouped
  disjointness (no group straddles train/eval).

## Seams (what still needs a human/Walter gate)

1. **AgentRunner substrate**
   - Build real `AgentRunner` implementations (Aider polyglot first, then
     SWE-ContextBench Lite).
   - This is the only seam that turns the scaffold into runnable benchmark numbers.

2. **Live `:4450 /v1/recall` wire test**
   - Requires MCP started with `WEVIBE_RECALL_MODE=test` **and**
     `WEVIBE_KEYSTORE_TEST=1` (bypasses Touch-ID prompt loops).
   - Requires Bearer token at `~/.wevibe/mcp-session-token`.
   - Touch-ID identity unlock remains a human gate for real delivery verification.

3. **Seeding execution**
   - `seeding.build_split` returns the split **plan only**.
   - Actual seeding must run through sanctioned manual extraction (D-5.7), never a
     bulk-loader back door.

## Spec vs reality deltas discovered

- `prompt_digest` is **not** a wire field. The server derives dense query behavior
  from intent+task; harness enforces INV-6 client-side by keeping keyword fields out
  of `prompt_digest`.
- There is no explicit YES/CALLED/NO field in the live API. Verdict is inferred:
  - **YES**: returned memories include non-empty decrypted `text`.
  - **CALLED**: `reason_code` indicates delivery blocked (`decrypt_failed`/`filtered_out`).
  - **NO**: unreachable/error/no candidates.

## Honesty clause

This repository is the benchmark **scaffold**. It produces **no benchmark numbers**
until wired to the live hub and ON delivery is verified end-to-end.
