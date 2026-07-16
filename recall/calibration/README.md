# RB-1a Floor Calibration Artifact (go-concurrency-v1)

`go-concurrency-v1.floor-sweep.json` is a cosine-floor calibration sweep over the fixed Go-concurrency recall benchmark: a 12-memory corpus evaluated against a 23-case gold set.

The sweep is generated with:

- embeddings from `nomic-embed-text:v1.5` (768 dimensions), and
- the production retrieval-card / prompt-digest pipeline (`buildRetrievalCard` and `buildPromptDigest`).

## What each floor row reports

Each floor `f` (0.00 to 0.90 inclusive, step 0.05) records:

- `recall_at_1`
- `recall_at_5`
- `precision_at_5`
- `mrr`
- `ndcg_at_5`
- `mean_separation`
- `zero_injection_overall`
- `zero_injection_positive`
- `zero_injection_empty`
- `expected_empty_correct`

Partitioned denominators are fixed and reported in the artifact:

- positive cases (`expect_injection=true`): **n=16**
- expected-empty cases (`expect_injection=false`): **n=7**
- total: **n=23**

Expected-empty strictness is evaluated separately from positive recall quality.

## Near-tie gate

Near-tie validation is evaluated on contested cases where the top-2 score gap is below the contested threshold (`gap < 0.20`) under the real embedding geometry. The artifact records the gate outcome and case-level gaps.

## Operating floor decision

The artifact publishes `knee_candidates` from multiple candidate-selection algorithms, but does **not** auto-select the operating floor. `knee_selected` is intentionally `null` because candidate algorithms diverge and final `f*` selection is pending policy decision.

## Live gate status (public evidence)

Offline 23-case results are directional small-sample evidence, not a production population statistic.

- Provisional floor: **0.75**.
- Live agreement gate: **FAILED**.
  - Positive: live **16** vs sim **14** (outside the ±1 admissible band).
  - Expected-empty: **0/7** correct, with strict pass requiring **7/7**.
- No production calibration is claimed.
- Production relevance floor remains **0.55**, pending canon reconciliation of the floor-gating scale and a passing rerun.
- Machine-readable evidence: `go-concurrency-v1.live-gate.json`.
