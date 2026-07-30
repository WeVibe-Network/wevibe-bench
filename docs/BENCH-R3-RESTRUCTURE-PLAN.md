# BENCH R3 restructure plan

Status: DRAFT awaiting Walter — costing rebuilt on TRUE-spend ledger basis 2026-07-30

## 1. Status + scope

This is a planning document only. Nothing here has been run. It rewrites R3 as a memory-trust / retirement benchmark, not as another ladder-lift claim.

Numbers are labeled **MEASURED**, **DERIVED**, or **ASSUMED**. Budget decisions use TRUE spend, not theoretical spend.

## 2. Why restructure

A June-2026 published result showed that a token-budget-matched vanilla actor matched or surpassed memory-augmented agents across multiple domains. Plain OFF vs ON does not control for this. Any lift claim without a token-budget-matched OFF arm has a refutation waiting.

The old per-rung ladder story is also structurally weak. Model strength and corpus maturity move together, so rung comparisons are collinear and inferentially inert. That collinearity is the same shape as the already-refuted 13-07 ladder claim.

R3 therefore moves the headline to outcome-driven retirement: does WeVibe retire planted bad memories when the outcome proves they are harmful?

## 3. Arms

R3 uses four arms. Primary comparison for lift is **ON vs bmOFF**, but lift is secondary to retirement.

| Arm | Definition | Role |
| --- | --- | --- |
| OFF | No memory. | Reference arm. |
| bmOFF | TOKEN-BUDGET-MATCHED OFF: OFF plus extra actor steps equal to the paired ON arm's memory-block token count. | Primary lift control. |
| ON | Own memories recalled. | Memory arm. |
| sham | HARD-NEGATIVE sham: injected memories are topically similar but non-solving. | Distraction / specificity stress test. |

The sham corpus MUST be hard-negative by embedding similarity, not random or unrelated. Distraction degradation scales with similarity. A random sham understates the cost and flatters ON.

`bmOFF` is constructible because D-INJECTION-CADENCE (`wevibe-docs/DECISIONS.md` §23) requires the memory block to be metered separately. Mechanical caveat: `injected_block_est_tokens` is derived post-hoc from plugin log scans (`wevibe_bench/adapters/backgammon.py:195-210,1281-1285`), so bmOFF must take its step budget from the paired ON cell run first, or from a pre-planned injection budget.

## 4. Headline order

Primary headline: **T1/T6 outcome-driven retirement**. This is novel and less defended-against.

Secondary/supporting: lift. Lift is contested because token-budget-matched vanilla actors can match memory-augmented actors. Score same-task lift and held-out-variant lift separately. NEVER headline the same-task number. Walter's 2026-07-24 reframe stands: the corpus taught durable **solutions**, not capability lift; held-out variants are required before any capability-lift headline.

Targets of record live in `RECALL-PIVOT-SPEC.md` §5, not `BENCHMARK-DIARY.md` §22.2: T1 badPersist<=0.20 CI95-excludes; T2 goodSurv>=0.90; T3 gap>=0.75; T4 rho>=0.80; T5 gfa<=0.05; T6 ttaBad split; T7 zero keyword-gated rejections; T8 funnel seams non-null.

## 5. Rungs + N

Use two rungs:

- `kimi/kimi-k2.7-code` — mid.
- `tencent/hy3` — weak.

Do not include GLM-5.2. `RUNBOOK.md:287` records it DESELECTED 2026-07-27, and the live ledger has zero `bench`-consumer cost events for it. Its 2034 calls are consumer `opencode`, so the prior draft's "$0.653/cell (n=4)" figure is not reproducible.

Recommended **N=8 paired per cell**. Hard floor **N=6**. Paired means the same fixture, seed/scaffold, and time-adjacent block with arms as pair members.

Why N=6 is the floor: a two-sided nonparametric paired sign test can first reach p<0.05 at N=6. The arithmetic floor is `2/2^6 = 0.031`. Below 6, a p-value is misleading regardless of effect size.

## 6. Fixtures + flakiness screen

Spread N across about three fixtures, not repeats of one. Between-task variance dominates.

**BLOCKING GAP:** only one fixture exists today: `tasks/backgammon/`. Two more fixtures must be built before R3 can claim the intended fixture spread.

Run the flakiness screen first. Deterministic fixtures have zero power to show lift. Classify candidates as always-pass, always-fail, or flaky, then concentrate N on the flaky band.

Screen design:

| Rung | OFF-only repeats | Purpose |
| --- | ---: | --- |
| `tencent/hy3` | 5 x 3 fixtures = 15 cells | Weak-rung flakiness map. |
| `kimi/kimi-k2.7-code` | 3 x 3 fixtures = 9 cells | Mid-rung flakiness map. |

## 7. Planted bads + unit of analysis

Plant stratified bad memories: **14 archetypes x 3 instances = K=42**.

The archetype is the unit of analysis for the headline. Instance-level results are secondary. Forty-two bads sharing one failure archetype behave closer to K=1, so binomial framing needs independent bads. Report intra-archetype correlation (ICC) to justify pooling.

Fourteen archetypes is the arithmetic floor for a perfect result's one-sided exact Clopper-Pearson 95% upper bound to exclude the 0.20 T1 bar:

| Archetypes | Perfect result | One-sided 95% upper bound |
| ---: | ---: | ---: |
| 10 | 0/10 | 0.259 |
| 13 | 0/13 | 0.206 |
| 14 | 0/14 | 0.193 |

## 8. Reporting discipline

Headline badPersist as an estimate with an interval, not as a pass/fail threshold. Use exact Clopper-Pearson and report the one-sided upper bound alongside the estimate.

At K<100, a true-positive result can often fail to clear a 0.20 bar by luck alone. That is how the last claim got refuted. The T1 bar is a descriptive comparison for R3, not a gate that turns the benchmark into success/failure theater.

Pre-registration must declare exactly one primary endpoint. Everything else is secondary and descriptive. Freeze that manifest next to the policy hash before any numbers exist.

## 9. Theta reporting

Report theta with the full 2x2 confusion matrix and raw counts. Use Wilson intervals clustered by session. Report per rung and never pool rungs.

Checkable prediction: stronger models resolve episodes with or without the memory, producing false "worked" labels. Therefore theta_bad should be systematically worse on stronger rungs.

Scope limit: the sim's robust box theta(0.8/0.2) is a class-conditional-noise result. The harvester's error rate depends on task, rung, episode length, and whether the session visibly resolved. That is instance-dependent noise, so the sim threshold does not transfer cleanly.

Design the plant count for theta too. Theta needs uses, not memories. About **120 bad-uses** gives about **+/-0.065** half-width. Also report the derived quantity the sim consumes: accumulated didn't-work per serve for planted bads.

## 10. Rho denominator check

Primary rho estimate: seeded manual audit of about 30 completed sessions reconciled against transcripts.

Secondary rho estimate: capture-recapture across log sources as a lower bound on undercount only. Use Chapman's estimator, not Lincoln-Petersen. The sources are positively dependent because a crash can lose several records at once, which biases Lincoln-Petersen low.

Undercounting the denominator biases rho upward. That failure mode flatters us. Report rho two ways:

- raw rho;
- denominator-corrected rho.

Report a bootstrap interval over whole sessions, clustered by session, alongside the naive Wilson interval.

**BLOCKER B1:** rho/T4 is currently unmeasurable. The serve-on-chain path is broken: defect D-C; SMOKE-5 measured 0/24 serves. The plugin sends empty `matched_keywords` at session-start set-sync and the hub 400s under D-4.2. Rho is no longer a validity threshold for badPersist after D1 because badPersist is flat across rho (0.107 -> 0.091 within +/-CI95), but harvest coverage is load-bearing for GOOD-memory survival: goodSurv 0.116 at rho=0 vs 0.816 at rho=1.0; gfa 0.884 at rho=0. D-C must be fixed before R3 can validate its headline.

## 11. Mixed-arm concurrency pool

Run ON, bmOFF, sham, and OFF cells in one randomized pool at the same concurrency. Never run one arm parallel and another serial. Contention, rate-limiting, routing variance, and timeouts penalize the parallel arm and would inflate apparent lift.

Block pairs adjacently in time so provider drift hits both members equally.

Record per-cell contention covariates and report whether they are balanced across arms:

- queue latency;
- retry count;
- HTTP 429s;
- wall-time.

**BLOCKER B7:** deterministic Docker container names mean two concurrent cells resolving to the same name kill each other via stale-remove. This is recorded in reports `28-07-26-0019` and `28-07-26-0249`. The mixed-arm pool requires per-cell unique container naming first.

## 12. Duplicate-event check

Report duplicate rate split by outcome type.

Standing turns on a rate, so outcome-independent duplication is first-order neutral. About 286% duplication concentrated entirely on didn't-work events would be needed to push a good memory across a 0.30 gate. Set **d<=0.05** as hygiene, report observed, and do not chase zero.

Do verify the outcome-independence assumption. Crash-and-retry can plausibly skew duplicates toward didn't-work events and would over-retire GOOD memories.

## 13. Open tension — flag, do not change

Top-K=3 at relevance floor 0.55 sits between two facts:

- the sim says K=1 breaches constraints;
- the distraction literature says smaller K is safer.

Neither was chosen with the other in mind. Do not change it here. Let the bench measure it.

Sharper unresolved problem: bench runs set `WEVIBE_RECALL_MODE=test` (floor=0, budget=1000), so the production governor K=3/floor=0.55 is not exercised by the bench as configured. The production governor is pinned only in `wevibe-docs` D-RECALL-GOVERNOR. Measuring it requires leaving test mode, which would filter fresh low-trust memories. Record this as unresolved.

## 14. Pre-registered possible outcome

Planted bads are actively harmful, not inert. A corpus with surviving bads can push ON below OFF.

If that happens, it is a genuine result. It is not a failed benchmark.

## 15. Costing — TRUE spend basis

**Basis rule:** `wevibe_bench/proxy_meter.py:81` makes TRUE spend `actual_spend_usd` the basis for budget decisions. `theoretical_spend_usd` is a full-price synthetic comparator for scoring only. All budget figures below are TRUE spend. Costing on the theoretical basis overstates spend about 2-3x.

Measured from live spend-proxy Postgres, read-only, with proxy untouched per R-45. Pricing version: `c58e194db3f6a20e7d41b8c9e2f05a17`.

| Item | Value |
| --- | ---: |
| **MEASURED** effective TRUE $/Mtok at ~0.81 cache-hit: kimi-k3 | $1.070 |
| **MEASURED** effective TRUE $/Mtok at ~0.81 cache-hit: kimi-k2.7-code | $0.492 |
| **MEASURED** effective TRUE $/Mtok at ~0.81 cache-hit: tencent/hy3 | $0.085 |
| **MEASURED anchor n=1** SMOKE-5 kimi-k3 ON, `ses_05838f1feffe6SCapcFY6G7VGk` | 85 calls; 4.359M tokens; $4.9482 TRUE / $13.7873 BENCH; 4073s wall |

The anchor is n=1 and is the only full-scale completed cell in the ledger. Everything else is scaled from it.

Derived token envelopes:

| Cell | Tokens |
| --- | ---: |
| OFF | ~3.11M (= 4.359M / recorded 1.4x ON multiplier) |
| bmOFF | ~3.27M (= 1.05x OFF) |
| ON | ~4.36M |
| sham | ~4.36M |
| 4-arm replicate | ~15.1M |

Derived TRUE costs:

| Item | kimi-k3 | kimi-k2.7-code | hy3 |
| --- | ---: | ---: | ---: |
| Per 4-arm replicate | $16.09 | $7.40 | $1.28 |
| Per OFF screen cell | $3.33 | $1.53 | $0.26 |

Screen total: 15 hy3 OFF cells x $0.26 = $3.95, plus 9 k2.7-code OFF cells x $1.53 = $13.79, for **$17.74**.

Main cells, two rungs (`kimi/kimi-k2.7-code` + `tencent/hy3`), per-replicate combined cost $8.68:

| Main N | Main TRUE cost |
| ---: | ---: |
| 6 | $52.08 |
| 8 | $69.44 |
| 10 | $86.80 |

Totals including screen and 30% contingency:

| Configuration | TRUE total |
| --- | ---: |
| 2-rung N=6 | $90.8 |
| 2-rung N=8 | $113.2 |
| 2-rung N=10 | $135.9 |
| 3-rung adding kimi-k3 at N=6 | ~$255; does not fit |

Budget truth: consumer `bench` lifetime TRUE spend is **MEASURED $7.9359 over 236 calls**. The ledger starts 2026-07-26, so stage-7/8 spend predates it and is not included. Headroom vs the $150 session budget is about **$142**.

Recommended: **2 rungs x 4 arms x N=8 x 3 fixtures = $113.2**.

Cuts to fit:

- kimi-k3 as a rung; one k3 rung at N=6 alone is $96.5, or 68% of headroom;
- any third rung;
- N=10, because only about $6 margin remains and one observed cell alone cost $4.95.

**Wall-clock is the binding constraint, not dollars.** N=8 means 64 main cells + 24 screen cells = 88 cells. At about 40-68 min/cell and concurrency 3, this is about 20-24 hours. N=6 is 72 cells. The mixed-arm concurrency pool is required, not optional.

## 16. Blockers / deviations to resolve before launch

| ID | Blocker / deviation | Required disposition |
| --- | --- | --- |
| B1 | rho/T4 currently unmeasurable because serve-on-chain is broken. | Fix D-C before R3 headline validation. |
| B2 | Only one of three fixtures exists. | Build two more fixtures before intended design. |
| B3 | GLM-5.2 unusable as rung. | Keep roster to k2.7-code + hy3. |
| B4 | `BENCHMARK-DIARY.md` §22.4 defines R3 as CROSS-MODEL TRANSFER and §22.5 caps R3 at $40. This plan redefines R3 as memory-trust/retirement at $113.2. | Walter decision required; do not assume approval. |
| B5 | `docs/VARIANCE-POLICY.md` sets baseline N=1 with T1-T4 escalation to N=3. | Prereg must explicitly supersede/amend it for R3 with N=8 paired. |
| B6 | Targets of record are in `RECALL-PIVOT-SPEC.md` §5, not `BENCHMARK-DIARY.md` §22.2. | Cite correct location. |
| B7 | Container-name collision breaks concurrent cells. | Unique per-cell container names before mixed pool. |

## 17. Harness work required

| Work item | Status | Why needed |
| --- | --- | --- |
| bmOFF arm | Required | Token-budget-matched control. |
| Hard-negative sham arm | Required | Similar-but-non-solving distraction test. |
| Mixed-arm randomized pool | Required | Balanced contention and provider drift. |
| Per-cell contention covariates | Required | Balance report across arms. |
| Unique per-cell container names | Blocking | Prevent concurrent cells killing each other. |
| Two new fixtures | Blocking | Avoid one-fixture repeat design. |
| Archetype-stratified plant path | Required | K=42 bads, headline at archetype unit. |
| Theta/rho/duplicate interval reporting | Required | Declared secondary/descriptive measurements. |

## 18. Launch position

R3 should not launch until Walter explicitly accepts the R3 scope/cap change, B1/B2/B7 are resolved, and the preregistration manifest is frozen next to the policy hash before numbers exist.

Until then, this document is a decision-useful restructure plan, not launch authority.
