# BENCH R3 restructure plan

Status: DRAFT awaiting Walter — costing rebuilt on TRUE-spend ledger basis 2026-07-30; amended 2026-07-30 to re-derive targets from the shipped held-credit policy per ordering constraint 10 (sim-first for every policy draft)

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

The primary endpoint also includes an analysis-side paired replay contrast over one bench event log. This does NOT add execution cells and MUST NOT be priced as another arm: the four-arm table above remains the execution design, while the paired contrast replays the same event log twice under frozen policies. The manifest lever is `L15_PAIRED_CONTRAST_ARMS = "shipped:edge-policy-v1|counterfactual:edge-policy-v1-outcomes-ignored"`.

`bmOFF` is constructible because D-INJECTION-CADENCE (`wevibe-docs/DECISIONS.md` §23) requires the memory block to be metered separately. Mechanical caveat: `injected_block_est_tokens` is derived post-hoc from plugin log scans (`wevibe_bench/adapters/backgammon.py:195-210,1281-1285`), so bmOFF must take its step budget from the paired ON cell run first, or from a pre-planned injection budget.

## 4. Headline order

Primary headline: **paired within-run policy contrast for outcome-driven retirement**. One bench event log is replayed under two policies: `edge-policy-v1` as shipped versus the same policy with E3 outcome events ignored. The endpoint compares survival of the SAME planted bads under the two policy replays. Unit is archetype (**K=14**); instance-level K=42 is secondary.

Pre-registered test: McNemar exact conditional binomial, one-sided in the pre-registered direction. The decision rule is the conjunction of four conditions: direction (`b>c`), minimum effect (`risk_difference >= 0.50`), reverse cap (`c <= 1`), and significance (`p<0.05`). At K=14, `risk_difference=(b-c)/14`, so the 0.50 floor requires **b-c >= 7**. The minimum-effect rule, not significance, is the binding constraint by design.

Secondary/supporting retirement endpoint: absolute badPersist as an estimate with an exact Clopper-Pearson 90% interval. The 0.20 line is a descriptive reference line, not a gate.

Secondary/supporting: lift. Lift is contested because token-budget-matched vanilla actors can match memory-augmented actors. Score same-task lift and held-out-variant lift separately. NEVER headline the same-task number. Walter's 2026-07-24 reframe stands: the corpus taught durable **solutions**, not capability lift; held-out variants are required before any capability-lift headline.

Targets of record live in `RECALL-PIVOT-SPEC.md` §5, not `BENCHMARK-DIARY.md` §22.2. This plan's shipped-policy re-derivation is recorded in §21: T1 is no longer a rho-threshold validity gate under held credit, T2/T3/T5 are open target conflicts for Walter, T4 remains blocked by the rho denominator/ceiling problem, T6 must be reported split, and T7/T8 remain non-null hygiene targets.

## 5. Rungs + N

Use two rungs:

- `kimi/kimi-k2.7-code` — mid.
- `tencent/hy3` — weak.

Do not include GLM-5.2. `RUNBOOK.md:287` records it DESELECTED 2026-07-27, and the live ledger has zero `bench`-consumer cost events for it. Its 2034 calls are consumer `opencode`, so the prior draft's "$0.653/cell (n=4)" figure is not reproducible.

Recommended **N=8 paired per cell**. Hard floor **N=6**. Paired means the same fixture, seed/scaffold, and time-adjacent block with arms as pair members.

Bench-only exploration rate: **ASSUMED** `0.10`, frozen through manifest lever `L14_BENCH_EXPLORATION_FRACTION` and env `BENCH_EXPLORATION_FRACTION` validated to `[0,1]`. This is BENCH-ONLY and MUST NOT be read as a production default. It is distinct from `L12_RETRIEVAL_OPEN_LOOP_FRACTION`, the hub's production open-loop fraction. Production exploration is 1-5%, but at bench query volume 1% yields zero exploration serves while 20% would be unacceptable in production; 0.10 is the smallest round fraction yielding usable data at our volume. Two rules both hold: exploration serves are EXCLUDED from lift because they are deliberately degraded, and INCLUDED in feedback-divergence because that is their purpose.

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

**BLOCKER B9:** rho also has a structural ceiling under held credit. Serve income is held and VOIDED if unpaired within the window, so rho can never exceed the fraction of episodes resolving inside that window. If the window captures only 60% of episodes, T4's `>=0.80` is structurally unreachable regardless of detector quality. Tuning detection breadth against that ceiling would be wasted effort.

**MEASURED:** `wevibe-bench/scripts/measure_episode_duration.py` was built and run against the live hub DB. Result: `episodes_n=0`, verdict `UNDETERMINED-INSUFFICIENT-DATA n=0 reason=no_paired_episode_rows`. No ceiling was fabricated.

Schema gap: `serve_events` carries no `episode_ref` and no `session_id`, so episode duration is only reachable through a weak proxy join: `outcome_events.session_id -> session_served_memories -> serve_events` by memory hash. The rho ceiling must be measured on real episode data before T4 `>=0.80` is treated as a live target. If the measured ceiling is below 0.80, that is a T4 amendment for Walter, not something to engineer around.

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

## 13. Production-governor tension — superseded framing

Top-K=3 at relevance floor 0.55 sits between two facts:

- the sim says K=1 breaches constraints;
- the distraction literature says smaller K is safer.

Neither was chosen with the other in mind. Do not change it here. Let the bench measure it.

Superseded wording: this was previously recorded as an unresolved open tension. The current limitation is narrower and sharper: the bench cannot exercise the production governor as configured. That limitation is headlined in §20 rather than duplicated here. "The tension is deferred" and "the bench cannot exercise the production governor" are different facts; only the latter belongs in limitations.

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

The paired within-run policy contrast adds **$0** to execution cost because it is two policy replays over an existing event log, not another cell or arm.

The transfer-check cell in §19 is one additional cell-set at the existing per-cell rate. The exact count depends on how many rungs are transfer-checked; no new per-cell rate is invented here.

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
| B8 | Transfer-check cell depends on production-governor execution in auto-approve mode. | Depends on the parallel auto-approve-with-production-governor work; owned elsewhere. |
| B9 | Rho ceiling is unmeasured because real paired episode data is absent and the schema only permits a weak proxy join. | Measure on real episode data before T4 `>=0.80` is a live target; if ceiling is below 0.80, escalate a T4 amendment to Walter. |

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
| Paired-contrast analysis functions | DONE | `wevibe_bench/stats.py` includes `mcnemar_exact`, `paired_binary_contrast`, and `meets_minimum_effect`. |
| Manifest levers | DONE | `wevibe_bench/cumulative/run_context.py` includes `L14_BENCH_EXPLORATION_FRACTION` and `L15_PAIRED_CONTRAST_ARMS`. |
| Episode-duration/rho-ceiling script | DONE, insufficient data | Script exists and returns `UNDETERMINED-INSUFFICIENT-DATA` because live DB has `episodes_n=0`. |
| Exploration filters | NOT BUILT | Scope: EXCLUDE exploration serves from lift and INCLUDE them in feedback-divergence; build after the arms exist. |

## 18. Launch position

R3 should not launch until Walter explicitly accepts the R3 scope/cap change, B1/B2/B7 are resolved, and the preregistration manifest is frozen next to the policy hash before numbers exist.

Until then, this document is a decision-useful restructure plan, not launch authority.

## 19. Transfer-check cell (production governor)

The bench runs `WEVIBE_RECALL_MODE=test` with floor `0` and budget `1000`, while production serves 3 at floor `0.55`. That is roughly 300x the serve breadth. A parallel work-stream is building an auto-approve-with-production-governor mode.

The transfer-check cell runs the SAME fixture/rungs under the PRODUCTION governor config: floor `0.55`, budget `3`, limit `3`. Its result is directly comparable to the test-mode cells because the fixture/rung design is unchanged; only the retrieval governor changes.

Purpose: this is a TRANSFER CHECK. It establishes whether test-mode numbers describe the production retrieval configuration at all. If this cell is NOT run, the limitation sentence in §20 stands as the fallback: "These results characterise the standing mechanism under test-mode retrieval and have NOT been shown to transfer to the production governor."

**BLOCKER B8:** the cell depends on the parallel auto-approve-with-production-governor work, owned elsewhere. Costing: one additional cell-set at the existing per-cell rate; the exact count depends on how many rungs are transfer-checked.

## 20. Limitations (headline, not backlog)

**Test-mode retrieval does not exercise the production governor.** These results characterise the standing mechanism under test-mode retrieval and have NOT been shown to transfer to the production governor. Supporting evidence: our own sim says the retrieval regime is the DOMINANT variable. The query-regime sweep moved both models by about 0.19 while the policies separated by only 0.01-0.05, and at top-K=1 both models breached constraints outright. Reason: the bench cannot exercise the production governor. This is explicitly not the claim that "the tension is deferred"; those are two different facts, and only the governor-exercise limitation belongs here.

**The sim can no longer serve as independent evidence for the policy it selected.** The bench is now validating the simulator as much as the policy. Treat sim results as policy-selection provenance and target re-derivation input, not independent confirmation.

**A window creates selection pressure toward fast-resolving knowledge.** The shipped policy holds serve income pending an outcome and VOIDS it if unpaired past 1440 epochs, or 24h. Architectural insight that takes three days to validate never gets credited, and no credit plus idle decay equals retirement. The corpus will systematically prefer quick fixes. This is a DISCLOSED DESIGN CONSEQUENCE. Hedge: window expiry should emit an explicit unresolved-within-window observation so a future policy can distinguish "never seen" from "seen but slow"; implementation is owned elsewhere, not by this plan.

**Already-agreed limits remain.** CCN-vs-IDN is a scope limit on theta. Theta is per rung and NEVER pooled. Archetype stratification is required. Retirement is headlined over lift.

**The 24h window is a DISCLOSED MAPPING ASSUMPTION, not a sim-proved constant.** The sim cannot discriminate window length because it emits its outcome event in the SAME epoch as the serve, so outcome lag is zero by construction. Swept windows `W in {1,5,25,1e9}` moved core metrics only in the third decimal. The sim epoch is also coarse: scenario epochs x 5 gives 200-300 epochs per memory lifetime, so literal 1440 exceeds every run length and never voids.

## 21. Shipped-policy target re-derivation

**MEASURED** source: `wevibe-sim/runs/policy-sim.js`, analyze `runs/policy-sim/2026-07-30T120517Z-c1-analyze.txt`, n=63 cells/row, `tp=0.10`.

| metric | immediate credit `out@rho0.8` | SHIPPED held credit `d1@rho0.8` | target | status |
| --- | ---: | ---: | --- | --- |
| badPersist | 0.1214 | 0.0945 | T1 <=0.20 | PASS, improved |
| goodSurv | 0.9535 | 0.7669 | T2 >=0.90 | BREACH |
| gap | 0.8321 | 0.6723 | T3 >=0.75 | BREACH |
| gfa | 0.0465 | 0.2331 | T5 <=0.05 | BREACH |
| badArchivedFrac | 0.7675 | 0.7944 | T6 split | improved |
| ttaBadMedianArchived | 34.36 | 33.71 | T6 split | improved |

T1 under held credit is flat across rho: 0.0902-0.0977. Rho is no longer a validity threshold for badPersist under held credit.

T6 MUST be reported SPLIT: `badArchivedFrac` and `ttaBadMedianArchived` separately, never averaged. When zero bads are archived, report NULL rather than the horizon.

Contradicted expectation, recorded as a finding rather than a failure: T6 was expected to worsen because latency was traded for correctness, and it did not. Median time-to-retirement is slightly shorter and archived fraction slightly higher. Reason: a realized pair credits one quantum instead of two, so bads accumulate less weight and cross the threshold marginally sooner.

Open target conflict escalated to Walter: max goodSurv anywhere in the sweep is 0.8162 at rho=1.0, so T2 `>=0.90` is unreachable at any rho under held credit. This plan does not propose replacement target numbers; that is Walter's call.
