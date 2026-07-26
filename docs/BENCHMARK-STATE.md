# BENCHMARK STATE & BUILD PLAN — single source of truth (as of 2026-07-15)

> Read this FIRST next session to build the SxE+recall benchmark properly. This is the durable INDEX:
> what the benchmark is, current architecture, all results, WHERE every artifact lives, the key findings,
> and the next build step. Detail lives in the dated reports under `wevibe-meta/workspace/reports/`.
> Home repo: `wevibe-bench/` (own git repo, no remote). Backstop copy: `~/Desktop/benchmark/` (FROZEN, do not delete).

> **⚠ PRIMARY-MODEL CORRECTION (2026-07-23, Walter-ratified — canon `D-BENCH-CUMULATIVE-LOOP-2026-07-23`; reconstruction
> `wevibe-meta/workspace/reports/23-07-26-1110-benchmark-model-correction-cumulative-loop-report.md`).** The PRIMARY benchmark
> is the CUMULATIVE organizational-learning loop: one benchmark-owned org, empty corpus → per-model recall-OFF baseline (OFF =
> no injected memory, but the productive session still extracts+commits, seeding the corpus) → deterministic roster-seeded
> consecutive recall-ON sessions, each resetting the code fixture to the same failing state, preserving the cumulative corpus,
> recalling eligible memories, resolving the SAME target test, and extracting+committing before the next session. Success =
> the same test getting faster/better across successive ON runs. Frozen-corpus waves / peer-stack isolation / step-down relay
> are SECONDARY/out-of-scope or history. **Built/unbuilt truth:** `wevibe-bench 46e266d` = arbitrary-ordered OFF/ON grouping
> ONLY (no cumulative extract→commit→next-session persistence); current harness OFF writes no memory, ON recalls but does NOT
> post-run extract/commit, no smart leader, no roster-seeded ON order, no served-memory-store/`/v1/serves` population, and the
> zero-progress extraction gate (zero-resolution session ⇒ zero memories) is ABSENT. Clone HEAD `fbfde17` is dirty
> (reproducibility blocker). Prior Stage result: transport PROVEN, lift/degradation UNMEASURED — NO degradation claim is
> established; it remains a hypothesis/release criterion. The §1–§11 build state below predates this correction; read it
> through this banner.

---

## 1. WHAT THE BENCHMARK IS
Open-source, pluggable **SxE (Session×Extraction) + recall ablation**: does injecting accumulated "team
memories" help a coding model? Structure = task × model-ladder × {OFF=no-recall | ON=full-accumulated-recall}
→ scorecard. Headline metrics: **attempts-to-green + total tokens + gate-conformance trajectory**, OFF vs ON.
- **Task** = LOCKED backgammon prompt (build a full localhost backgammon game, backend+FE, 0 errors).
- **Oracle** = deterministic gates: Vitest backend (`gates-01-08/09-12/13-16`) + Playwright FE (core/edges) +
  conformance pregate + `BENCH_DEBUG` setState/seedDice seam; game binds **port 8002**. Golden ref = 100%,
  scaffold = fails. An aesthetic LLM-judge hook exists but is STUBBED (not in pass/fail).
- **Ladder** (memories flow DOWN, cumulative pool): OFF baseline → extract → ON recalls accumulated pool +
  own prior runs. Self-extraction (E = session model). 3× (now problems-only) between-attempt feedback.

## 2. ARCHITECTURE / COMPONENTS (all under `wevibe-bench/`)
- **Drivers:** `scripts/backgammon_scored_ladder.py` (Stage-7 scored-ladder outer driver, roster A: source
  Opus-4.8 + measure kimi-k2.7-code/big-pickle; derives its roster from `wevibe_bench/config.py`
  (`backgammon_scored_ladder_roster()`), writes `scored-ladder-manifest.json` (frozen roster+allocation+rung
  params, config fingerprint, schema v2; `--resume` fails on any drift), owns per-cell stage-7 ledger
  admission + proxy lifecycle + identity/delivery assertions + variance-policy N-logic
  (`scored-ladder-summary.json` discloses N per cell), checkpoint per cell/rep) → shells `scripts/backgammon_ladder.py` (per-rung, `--phase`, `--max-retries`) →
  `scripts/run_backgammon.py` (single cell/session, `--memory-modes off,on`, NO extraction) →
  `wevibe_bench/adapters/backgammon.py` (spawns headless `opencode run`, feedback loop, gate run, cheat-gate).
- **Extraction/SxE:** `scripts/backgammon_sxe.py` (extract→submit→leader_verify→commit→prove_delivery).
- **Coordinator gate (binding):** external operator must load/enforce RUNBOOK "Extraction-integrity gate" + local `AGENTS.md` and abort before `m2_proof.leader_verify_and_commit` on missing/uncorrelatable `extraction.integrity` terminal records or invariant breaches.
- **Cheat gate:** `wevibe_bench/adapters/cheat_detector.py` (`scan_events_for_oracle_access`, tests in
  `tests/test_cheat_detector.py`).
- **Task assets:** `tasks/backgammon/` = `scaffold/` (7 stub files seeded into each worktree — patch-to-green,
  Walter's locked CONTRACT), `gates/` (oracle: report.mjs + backend/frontend/conformance + harness + judge),
  `golden/` (REFERENCE SOLUTION — the answer key; must never be worker-reachable), `CONTRACT.md` (edge-rules HIDDEN).
- **SxE prompts:** `E-fork-strategy.md`/`E-assembled.txt` (E2 evidence-bounded extractor), `S-fork-reasoning.md`
  (producer capture+compliance, wired into `backgammon.py::_build_task_prompt`). Refit commit `28a691d`.
- **Recall MCP clone:** `wevibe-bench/scaffold/wevibe-mcp-clone` (dist), runs on **:4550** (`WEVIBE_BENCH_ENDPOINTS=1`,
  seed identity, does recall+decrypt host-side; canonical wevibe-mcp UNTOUCHED). Dedup hard-drop lives in its
  `src/extraction.ts` + `dist/extraction.js:1024` (filters near_dup after `near_dup_drop` log; 0.93 threshold).
- **Host-side OpenRouter proxy:** `wevibe_bench/adapters/openrouter_proxy.py` (policy + budget ledger),
  `wevibe_bench/adapters/openrouter_proxy_server.py` (HTTP transport), `scripts/run_openrouter_proxy.py` (CLI shim).
  It serves `/api/v1/chat/completions` on host loopback, keeps the real OpenRouter key on host only, issues
  ephemeral per-run worker tokens, and persists checkpoint+R-37 logs under `runs/openrouter-proxy/`.
- **Config/runbook:** `config/bench.env` (durable env: umbral+guard bins, keystore paths, recall-mode, endpoints),
  `RUNBOOK.md` (clone launch + two-tier topology + clean-start), `docs/` (proposal + directive, see §6).
- **Benchmark-start clean-start (one-time per benchmark envelope):** all-inclusive `make redeploy` from `wevibe-meta` (full wipe chain/pg/qdrant + served cache; see §2E) PAIRED with
  `rm /tmp bench keystores` (NOT `~/.wevibe/keys`); then start :4550 clone with FULL env
  (`WEVIBE_KEYSTORE_PATH=$WEVIBE_BENCH_LEADER_KEYSTORE`, umbral/guard bins, `WEVIBE_MCP_HTTP_ONLY=1`, `< /dev/null`,
  `WEVIBE_RECALL_MODE=test`). org-0 self-seeds on cell 1 (genesis-fresh chain → first register-org = wevibe-org-0).

### 2A. Stage-7 roster status (Walter decision, 2026-07-21)
- `opencode/big-pickle` is a full Stage-7 MEASURE rung (resolves fork F1; supersedes the same-day smoke-only pin).
- Canonical roster: SOURCE Opus-4.8 (OFF cell + self-extraction) → MEASURE `moonshotai/kimi-k2.7-code` (OFF+ON, session-only)
  → MEASURE `opencode/big-pickle` (OFF+ON, session-only). big-pickle is `xiaomi/mimo-v2.5` upstream (Zen alias), not a
  separate rung.
- Identity note (2026-07-22 probe): since 2026-07-21T23:50Z Zen echoes alias `big-pickle` in response `model`
  (was `xiaomi/mimo-v2.5`) and no longer sends response `provider`; verified cosmetic-only via
  `runs/identity-probe/20260722T085209Z/` (system information self-IDs MiMo-v2.5/Xiaomi, key fp `b5ce6e5e`, endpoint and
  injected system-prompt prefix `cached_tokens=192` unchanged, identical workload fingerprint `in_bytes=61581` /
  `in_tokens_ub=61409` vs 2026-07-21T14:33Z qualification).
- Qualification evidence: `runs/qualification/stage3-opencode-big-pickle-20260721T142452Z.smoke.log` (4/4 transport checks pass:
  streaming/tools/structured/require-params; $0) + `runs/backgammon/20260721T143340Z-stage5-opencode-big-pickle-scorecard.json`
  and `runs/openrouter-proxy/20260721T143337Z-stage5-opencode-big-pickle-20260721T143337Z.log` (OFF transport-clean over real Zen;
  capability FAIL is measured capability, not a transport failure; pre-23:50Z echo shape `model=xiaomi/mimo-v2.5` with
  response `provider` present).
- Per-cell identity pin now expects alias echo `big-pickle` plus startup upstream-key fingerprint assert `b5ce6e5e`; any
  deviation trips one-way `503 identity_mismatch` and that cell is never scored.
- ON-cell recall delivery is asserted in-cell by `scripts/backgammon_scored_ladder.py::_scan_delivery`; failure aborts with
  `delivery_unproven`.
- Run forms (do not execute here; invoke from repo root with `PYTHONPATH=.`): real run requires `--rung-params` →
  `PYTHONPATH=. python3 scripts/backgammon_scored_ladder.py --rung-params <rung-params.json> [--runs-dir ...] [--ladder-runs-dir ...] [--clone-log ...] [--proxy-port ...] [--org-id ...] [--start-cell N] [--resume]`;
  dry-run requires no rung params → `PYTHONPATH=. python3 scripts/backgammon_scored_ladder.py --dry-run [--runs-dir ...] [--start-cell N]`.
- Checkpoint/resume path: `runs/backgammon-scored-ladder/scored-ladder-checkpoint.json`; caps enforced via `wevibe_bench.stage_ledger`
  (Stage-7 $40, global $115).

### 2B. Stage-7 crash recovery (Option B, 21-07-26) — SUPERSEDED 22-07 by stage-8 fresh baseline (§2D)
- Historical only: the Option-B continuation cells (crash-recovered ladder) are PRESERVED as evidence but NEVER
  merged into the stage-8 scorecard (pre-registered fresh-baseline rule).
- Walter GO (21-07-26) authorized the full Stage-7 scored ladder (superseding the prior passability-first gate).
  Run `20260721T195407Z` then crashed 34 minutes in after Cell 1 completed as a valid 3-attempt capability FAIL and
  self-extraction committed one org-0 memory (`cid 547b5c0b711fcbdfa8fc7cd8055d30e640a116a062ea2815804d4ef7aed947fd`, delivery YES).
  Crash cause: `scripts/backgammon_scored_ladder.py` cast `int("FAIL")` on the all-attempts-fail path (harness defect in `67052cd`);
  no scored-ladder checkpoint was written before crash assembly, and Cells 2–5 never started.
- Pinned continuation command (verbatim): `PYTHONPATH=. python3 scripts/backgammon_scored_ladder.py --rung-params runs/qualification/stage7-rung-params.json --import-cell runs/backgammon-scored-ladder/cell1-recovery-20260721T195407Z.json --start-cell 2 --resume`
- `--import-cell` is a machine-validated recovery contract: it validates digests/run-ID/manifest/model/roster plus live presence
  of the preserved Cell-1 memory in the pool, imports Cell 1 with `imported: true` provenance, and refuses drift/tampering;
  this continues from Cell 2 with no rerun/rebill of Cell 1 (`--dry-run` appends a zero-cost validation pass that skips the live pool probe).
- Disclosure: the final Stage-7 benchmark spans harness commits (`67052cd` crash run → post-crash patch commit); Walter explicitly
  accepted this continuity model for Option B.
- Spend disclosure: Stage-7 = `$20.04 / $40` (accrued `$13.23`; committed-unproven `$6.81` incl. ~$1.6 killed-smoke-run
  reservation retentions that never convert — by design, uncertainty is never released). Global = `$32.11 / $115`.
  The 22-07 canon smoke accrued `$0.00` (big-pickle free upstream; all spend was reservation-only).
 - Detail: `wevibe-meta/workspace/reports/21-07-26-1315-stage7-ladder-crash-fail-stats.md`.

## 2C. 22-07 CANON CONFORMANCE SMOKE (2026-07-22, big-pickle $0) — PASS, gate before re-baselined run
End-to-end smoke through the real transport after the 2026-07-22 canon (substrate events / atomic multi-memory
commit / budget-bounded attempts / one proxy meter) + stack wipe. Report:
`wevibe-meta/workspace/reports/22-07-26-0645-canon-conformance-smoke-e2e.md` (+ chunk reports 0426/0447/0507/0520/0532/0602).
- Asserts: (a) extraction request = 256 `SubstrateEvent[]` (user 8/assistant 37/reasoning 95/tool 94/edit 22,
  `skipped_error_events=1`) PASS; (b) failure-episode segmenter 9 episodes (1 resolved/6 unresolved/2 coincidental),
  evidence block 7539 chars in extraction input PASS; (c) 9 insights → 9 memories committed atomically, 9/9 delivery
  probes matched PASS; (d) fix-loop recall carries harvest fields — MECHANISM PASS (`need-harvest intent=debug` log
  live, hub query logged) but fields legitimately empty (no build/test executions in-session) — populated case
  suite-tested only, OPEN follow-up; (e) termination labels behave (`attempt_ceiling_reached`, single proxy budget
  meter in budget-decision lines) PASS; (f) identity assert PASS (all ordinals echo `big-pickle`, key fp `b5ce6e5e`,
  zero `identity_mismatch`).
- 4 latent defects found + fixed by the smoke (all suite-green): proxy checkpoint init persist; sxe errored
  edit/write events; harness-limit kill scope (container teardown → process kill + procps in image `9d389f8e376b`);
  sxe type=error event skip. Plus wevibe-mcp transcript adapter retired (events-only `/v1/extract`) + sxe
  proxy-routed extraction (`WEVIBE_BENCH_EXTRACT_*`) + clone num_ctx override + R-37 need-harvest logging.
 - OPEN before full run: rung-params `expected_upstream_model` still `xiaomi/mimo-v2.5` (in-code pin `big-pickle`
  wins; params file must be re-pinned); Zen upstream 500-storms can kill an extraction (no retry — §8 carry item);
  guard flagged one committed memory (`unicode_homoglyph_injection` " override") yet commit proceeded — policy
  observation for Walter.

## 2D. STAGE-8 RE-BASELINED 5-CELL RUN (2026-07-22) — COMPLETE, the current baseline
Fresh baseline under a NEW `stage8` envelope (manager ruling, Walter-disclosed): cap $32.00 (raised from $25.00
after the Cell-1 false start; the $7.20 false start stays inside stage8 — honest history). Stage-7 ledger history
untouched; stage-7 cells are historical evidence only, never merged. Reports:
`wevibe-meta/workspace/reports/22-07-26-0655-stage8-rebaselined-5cell-LEDGER.md` (running ledger) +
`22-07-26-<time>-stage8-5cell-baseline-result.md` (consolidated) + `22-07-26-0715-stage8-envelope-wiring.md` +
`22-07-26-0730-twin-aware-delivery-probe.md`.
- **Pre-registration:** frozen in `runs/backgammon-scored-ladder-stage8/scored-ladder-manifest.json` (fp
  `3910969ef0342144…`, schema v2 + preregistration block + disclosures). Harness fixes disclosed in-manifest:
  stage8 envelope wiring; twin-aware delivery probe (harness measurement fix — a memory suppressed as
  contested-twin-of-a-RETURNED memory counts delivered, evidence {winner/twin cid fp, score_gap} in scorecard;
  protocol semantics unchanged; regression-tested).
- **Run:** trace `stage8-ladder-20260722T151533Z-40ddef2054`, 15:15:33Z→18:23:16Z (3h07m), status=ok, 7 reps
  (kimi OFF borderline T3 → N=3, disclosed; all other cells N=1). Artifacts: `runs/backgammon-scored-ladder-stage8/`
  (checkpoint/summary/manifest/cell logs) + `runs/backgammon-stage8/` (scorecards/details/worktrees). Aborted run 1
  (delivery_unproven on contested-twin suppression — the defect the twin-aware probe fixed) preserved at
  `runs/backgammon-scored-ladder-stage8-aborted1/` + false start at `…-falsestart1/`.
- **Scorecard (all transport clean, identity fps asserted, ON-cell delivery proven):**
  run1 opus OFF/source: harness_error (D6 mid-work SIGTERM, gates never ran) FAIL, $2.158, 39.2k tok; extraction
  6 memories committed, delivery YES 6/6 (all direct matches).
  run2 kimi OFF: majority BUDGET_STOP (rep1 BUDGET_STOP $1.44 / rep2 harness_error SIGKILL / rep3 1 gated attempt
  4 failed gates [G08,F10,F12,F14] then attempts_exhausted_by_budget $1.92); N=3.
  run3 kimi ON: BUDGET_STOP, 1 gated attempt 2 failed gates [G13,F06], delivery ok (5 recalled), $1.58; N=1.
  run4 big-pickle OFF: harness_error (D6 SIGTERM, gates never ran) FAIL, $0.
  run5 big-pickle ON: FAIL attempt_ceiling_reached, gate trajectory 65→2→2 failed (final [F08,F12]), delivery ok
  (5+2+2 recalled), identity 90/90 `big-pickle` echoes, $0.
- **Lift statement:** NO defensible OFF→ON lift claim — opus OFF and big-pickle OFF baselines are unmeasured (D6
  harness deaths); kimi OFF↔ON is budget-truncated (1 gated attempt each, 4 vs 2 failed gates) under N=1 caution.
  Delivery/identity/transport/budget-meter assertions all GREEN.
- **D6 anomaly (baseline run evidence, now resolved):** 3/7 reps died mid-work by EXTERNAL signal (run1/run4
  SIGTERM 143, rep2 SIGKILL 137; `killed=none`, no adapter/proxy/docker cause; matches stage-7 defect D6). Root
  cause + fix are closed in §8; baseline scorecard remains unchanged.
- **D6-rerun disclosed run (NEW, never merged):** trace lineage `runs/backgammon-scored-ladder-stage8-rerun-d6/`
  (+ inner `runs/backgammon-stage8-rerun-d6-fix1/`), manifest fp unchanged `3910969ef0342144…`, disclosure
  recorded in-manifest. Cell-1 opus OFF PASS `gates_green` attempt 1 (85,794 tok, $5.7144, 1183s). Cell-4
  big-pickle OFF FAIL `attempt_ceiling_reached` after 3 attempts, problems 23→19→4, final failed gates
  [F03 REQ-HINT, F04 REQ-HINT, F12 REQ-NEWGAME, F14 REQ-ANIM], conformed=True, identity 92/92 ($0, 4182s).
  Cell-2 rep2 kimi OFF BUDGET_STOP (budget-stop mid attempt-1 ungated; 87 turns, 98,887 tok, $1.8518, 1638s;
  honest 402 at $2.70 cap). Disclosed N=3 kimi OFF picture now: rep1 BUDGET_STOP ungated / rep2 BUDGET_STOP
  ungated / rep3 FAIL 4 gates [G08,F10,F12,F14].
- **Spend (single proxy meter):** baseline run stage8 $16.76/$32.00 (accrued $13.31 = $5.83 false start + $7.48
  run; committed-unproven $3.45) plus disclosed D6-rerun $9.69 (incl. one $0.39 poller false-hang kill of a
  healthy kimi rep) → stage8 cumulative $26.4483/$32.00. Global $58.5602/$115.
- **Harvest observation:** need-harvest fired on all ON-cell recalls (incl. intent=debug) but
  buildFailing/testFailing/errorStrings stayed unpopulated (worker session events are not clone-side) — populated
  case remains suite-tested only (observation: NOT observed, structurally expected).

### 2E. Wipe cadence directive (2026-07-24, Walter-locked)
- EXTENDS canon `D-BENCH-CUMULATIVE-LOOP-2026-07-23` (do not reinterpret as per-run wipe); full canonical statement lives in `BENCHMARK-DIARY.md` §18.
- **ONE-WIPE / PERSISTENT-CORPUS:** wipe EXACTLY ONCE at benchmark start via all-inclusive `make redeploy`
  (from `wevibe-meta`, wiping chain/pg/qdrant + served cache), then run a residue check; ANY residue = STOP & FIX
  before benchmark execution.
- After that benchmark-start wipe, subsequent scored runs NEVER restart the chain and NEVER reset the memory corpus;
  each run resets only the code fixture to the same failing state while the corpus persists cumulatively in storage.
- Re-wipe/re-baseline is allowed ONLY on declared TRUE REGRESSION or TOTAL BENCHMARK FAILURE, and must be explicit,
  deliberate, and documented (never casual).
- Success/failure framing (Walter pragmatic bar): success means visible convergence across ON runs (more problems
  resolved; fewer cycles/tool calls/tests/tokens/time; attempts-to-green down); failure means no demonstrable
  improvement or integrity collapse.
- **CAVEAT (MUST TRAVEL WITH THIS RULE):** the producer-model-provenance → hub capability-eligibility provider-slug
  path (`D-PRODUCER-MODEL-PROVENANCE` / `D-CAPABILITY-ELIGIBILITY`) is canonized but UNBUILT and unproven through
  real transport (prod `attestation:null`; provenance does not reach Qdrant) and has never been measured/used as a
  recall filter; therefore this persistence enforcement is design INTENT until that path is proven in real transport,
  then enforcement is permanent. **Amendment 2026-07-24:** provenance NOW reaches Qdrant for post-24-07 commits
  (R1: bench `5394032` + hub `a171630`; born-stamped 9/9 × 3 legs; pre-fix 8 unstamped by Option A); the FILTER
  remains unbuilt — caveat narrowed for the stamping leg only.


## 3. RECALL / INJECTION SEMANTICS (verified 2026-07-13/14)
- Plugin re-injects ALL approved memories EVERY turn + across compaction (`wevibe-plugin.ts:1349` transform →
  `:1412-1447` inject-all-eligible → `:1503` compaction). Per-session dedup gates ATTRIBUTION only, not injection.
  **⚠ SUPERSEDED BY CANON 2026-07-24 (PENDING implementation):** D-INJECTION-CADENCE-2026-07-24
  (`wevibe-docs/DECISIONS.md` §23) replaces per-turn re-injection with inject-ONCE-at-acceptance (stable early
  position), a bounded hub-ranked top-K served set within a fixed token budget, and VERBATIM compaction
  preservation (never summarize-through); JIT progressive disclosure is PARKED as a future seam. Implementation
  dispatched 2026-07-25 (plugin inject-once + verbatim preserve + bench metering); **worker-image REVENDOR
  required before R2.** Until the revendored image lands, the per-turn behavior above is still the live truth.
  **Benchmark metering rule (same canon):** in all measurement arms the memory block's tokens MUST be metered and
  reported separately from the model's work tokens — every OFF/ON progress vector carries injected-memory tokens
  as their own field.
- `WEVIBE_RECALL_MODE=test`: floor 0 / budget 1000 / limit 1000 + AUTO-APPROVE (`:1105`, skips human popup) →
  headless injection works. **PROD/unset headless = injects NOTHING** (needs human approval; no TUI → dropped).
- Real relevance floor overridable via `~/.wevibe/plugin-config.json` (`recall_relevance_floor`/`recall_max_injected`)
  while KEEPING test-mode auto-approve — the clean way to measure filtered recall headless.
- Decrypt happens in the :4550 clone (seed identity), NOT the worker plugin → worker needs only HTTP to :4550.

## 4. RESULTS TO DATE
### 4a. 13-07 runs — T1 ONLY; the "full T1→T4 ladder / monotonic lift" claim is REFUTED
13-07 STOPPED after T1 as instructed (no T1 ON cell; no T2/T3/T4 scorecards exist). The prior §4a mirror
("Directional: weaker=more lift") copied a false continuance claim (added retrospectively 15-07, `293caa9`)
with no primary artifact behind it — refutation: report `21-07-26-0845-bench-1307-ladder-claim-refutation.md`.
The ONLY defensible prior statement is §4b's compromised 14-cell summary. reports `13-07-26-0407/0600/0700/0750`.
### 4b. 14-07 clean-wipe 14-cell ladder (opus-4.6×2/GLM×3/kimi-k2.7×4/minimax×5) — ran 12/14, VALID
- Aborted at cell-12 (minimax SELF-extraction false-negatived its own valid transcript `off_task_output` 5× →
  whole-ladder abort; NO skip-past-extraction-fail flag). Cells 13-14 never ran.
- DATA VALID (forensic): recall PROVABLY injected (decrypt cids == prior cells' submission_hash; ON cells quote
  "team memory", OFF none), pool grew 1→11. `delivery=N/A` = harmless hardcoded placeholder (`backgammon.py:364`).
- RESULT = **efficiency not capability** (ALL 12 PASS — because feedback leaked answers, see §5). Token OFF→ON:
  opus +21% (input unmeasured artifact), GLM −23% (helped), kimi −16% (helped), minimax +381% (hurt, verbose output).
- Full result+table: report `14-07-26-0400-14cell-ladder-result.md`. Log `runs/backgammon-14cell/20260713T224242Z.log`.
### 4c. Real-recall probe (minimax ON, floor 0.55 top-3 vs test-mode all-11) — N=1
Filtering 11→3 did NOT cut the bloat (235,844 tok vs OFF 49k; floor-0 was 193k/280k). minimax cost = its OWN
verbose output, not injection volume. "all-11=bloat" UNSUPPORTED. mgr-verified via hub query_log. report `14-07-26-0130`.
### 4d. 2-model integrity VALIDATION (GLM + minimax, OFF/ON) — post-fix, THE KEY RESULT
- **Lift signal RESTORED.** GLM OFF (clean, no-cheat) stuck 21 problems, never conformed; ON 21→2→1, conformed.
  minimax OFF 18→10→4 (3 attempts); ON attempt-1 3-problems, all gates pass (would-be-PASS), −31% tokens.
  Lift is real + pre-cheat.
- **BUT 3/4 cells CHEATED** (read `gates/` + `golden/` on disk) when stuck near green. Cheat-gate CAUGHT all
  (invalidated, not false-PASS) → NO clean scored PASS yet. Perm-deny only DETECTS, doesn't PREVENT.
- Log `runs/measurement-integrity/20260714T094314Z-miv-chain.log`; scorecards `runs/backgammon/20260714T094314Z-miv-glm-*`
  + `20260714T105152Z-miv-minimax-*`; CHEAT.json in `runs/backgammon/miv-{glm,minimax}/{off,on}/`.

## 5. KEY FINDINGS (why the benchmark measures what it measures)
1. **Root cause of the pre-fix "all-PASS / no capability lift":** between-attempt feedback leaked the gate's
   EXPECTED VALUES (targets, e.g. "expected 208 to be 167"), letting OFF match answers without the rule →
   OFF converged like ON → lift collapsed. NOT prose remediation (the Opus judge is stubbed). NOT task-easiness
   (9/11 cells needed feedback incl all OFF). Audit: report `14-07-26-0218`.
2. **Fix WORKS:** problems-only feedback (gate IDs+titles) restored the OFF-vs-ON gap (§4d).
3. **New threat:** capable agents cheat by reading the on-disk oracle/golden when the answer-crutch is removed.
   Detection (cheat-gate) works; PREVENTION requires the oracle to be UNREACHABLE (→ Docker, §6).
4. **Strong models know standard backgammon from pre-training** → memory can't lift them on standard rules even
   with perfect feedback. Lift on strong models needs NOVEL/non-pre-trainable rules (Tier-2, §6).

## 6. INTEGRITY MECHANISMS IN PLACE (14-07, bench-only, UNCOMMITTED working-tree)
- **Tier-1 problems-only feedback:** `_build_feedback_prompt` emits gate IDs+titles only (no expected/observed/paths).
- **Tier-1b oracle lockout:** physical isolation + permission-deny (dropped `--dangerously-skip-permissions`;
  worktree-local opencode.json allow-worktree/DENY-oracle, allow/deny only NO `ask`) + HARD CHEAT GATE
  (cheat_detector, verdict=CHEAT overrides PASS). Directive: `wevibe-bench/docs/ORACLE-ISOLATION-DIRECTIVE.md`.
  reports `14-07-26-0210/0207/0215/0218`.
- **Paid transport hardening (proxy):** host-side proxy is the one paid path and hard-injects per-profile
  OpenRouter `provider` policy (pinned `order`/`only`, `allow_fallbacks:false`, `require_parameters:true`,
  profile quantizations when configured), rejects worker-side `provider` overrides, and clamps `max_tokens`.
- **Hard-ceiling spend ledger (proxy):** conservative reservation before dispatch, refuse-before-dispatch on
  projected spend over cap, retain-on-uncertainty (never release), atomic checkpoint resume. Ceiling is
  genuinely hard at `min(configured cap, $12)` and restart cannot reset spend.
- **Known limitation:** perm-deny leaks (bash indirection, file-copy) → cheat-gate is the guaranteed backstop,
  but for clean scored data the oracle must be physically absent.

## 6A. Option A — requirements-instrument repair (15-07-26)
- **LOCKED (Walter):** the benchmark is repaired into a defensible requirements-to-implementation instrument.
  Every pass-required behavior must be derivable from `tasks/backgammon/CONTRACT.md`; hidden gates may verify
  public requirements but may not require oracle-only constants/formulas/exact strings/counts/mechanisms absent
  from the contract; if a precise constant is required it is published; if multiple implementations satisfy a
  requirement the oracle accepts them; feedback stays problems-only and names failed public requirements/checks by
  REQ-ID + gate ID only.
- **Name disambiguation:** this Option A is the benchmark-instrument repair, distinct from the unrelated
  root `BENCHMARK-DIRECTIVES.md` "Benchmark Option A" memory-value-model lock.
- **Repair executed this session:** `CONTRACT.md` rewritten with public constants + REQ-IDs; latent oracle-only
  gates repaired (including 2×G12 cube-AI, F10 bar-entry, F14 animation + other hidden-constant gates); G12
  `winProbability` assertions relaxed to behavior-level constraints; F14 broadened to accept CSS
  transition-on-transform OR CSS animation; all gate labels now carry public REQ-IDs.
- **Traceability matrix:** `wevibe-bench/docs/CONTRACT-TRACEABILITY.md` (authored separately).
- **Golden reconciliation:** divergent uncommitted golden rewrite was discarded (preserved as
  `runs/optionA-repair/dirty-golden-rewrite-15-07-26.patch`); committed reference golden `4b4ec74` restored and
  proven to pass all conformance+backend+frontend gates from a clean export.
- **Exact passability gate (LOCKED):**
  > "Before the conditional GLM-5.2/MiMo-V2.5-Pro roster unlocks, Opus-4.8 at HIGH reasoning must pass ONE clean recall-OFF Docker smoke of the repaired benchmark under a genuinely hard cumulative paid ceiling of $12 (operational target lower). PASS = conformed + all host-oracle gates green + cheat-clean, from a clean checkout, before another paid smoke."
- **Superseded 21-07-26:** Walter GO authorized Stage-7 scored roster execution without waiting for this passability gate; active path is §2B.

## 7. NEXT BUILD STEP (the plan)
**Docker Architecture A is IMPLEMENTED** — `wevibe-bench-worker:v1` image, Docker layer module
`wevibe_bench/adapters/docker_worker.py` (`DockerCell`), adapter cutover in `wevibe_bench/adapters/backgammon.py`,
and isolation coverage (`tests/test_docker_isolation.py` + `scripts/docker_isolation_smoke.py`).
- Validation sequence (locked): **spike OFF cell → ON smoke → cutover → full ladder**.
- Current status: the Opus-4.8 HIGH passability smoke already ran on 15-07 and FAILED at $11.8035 on four
  now-repaired gates under problems-only feedback (report `15-07-26-1019-opus48-high-passability-smoke.md`), which
  motivated Option A (§6A).
- **Superseded gate (historical):** the §6A Opus passability-PASS unlock requirement was superseded by Walter's
  21-07-26 Stage-7 GO; scored execution started, then entered Option-B recovery (§2B).
- **Then Tier-2** (separate): to show lift on STRONG models, introduce novel/non-pre-trainable rule twists that
  ONLY the memories reveal — else lift only shows on weak models + obscure integration traps (by design).

## 8. OPEN DECISIONS / FORKS
- **D6 mid-work worker deaths (stage-8, RESOLVED):** root cause was the worker's own bash cleanup
  (`kill $(ps aux | grep 'node.*server.ts' …)` / `pkill -f "node.*src/server.ts"`) self-matching the opencode
  process argv, because argv carried the full 29,157-char task prompt (including `node src/server.ts` and `8002`).
  Fix: prompt now delivered by STDIN (`opencode run` reads piped stdin via `docker exec -i`); harness logs
  `op=worker-stdin-write … text_fp=ca05c3ec`. Proof: two forced recurrences captured worker `opencode.db`
  (fatal tool call with last event left `running`), then fixed-harness 3/3 reps ran D6-free (87–91 turns,
  including `ps`/`pkill` cleanup commands surviving).
- **Rerun disclosure rule (locked):** rerun = new disclosed run, never a merge. The D6 rerun is recorded in §2D
  (`runs/backgammon-scored-ladder-stage8-rerun-d6/` lineage).
- **Superseded 21-07-26:** the prior BLOCKED-BY-PASSABILITY gate is closed by Walter GO; Stage-7 scored roster
  execution is authorized and currently in Option-B recovery (§2B).
- **Provider-routing mechanism status:** the prior "harness provider-routing UNPROVEN" gap is now closed by the
  host-side proxy enforcement path (policy injection + persistent budget ledger). This closure does NOT authorize
  scored runs by itself.
- **Opus provider-pin gate resolved-by-execution (21-07-26):** Stage-7 run `20260721T195407Z` executed with
  proxy-injected Opus provider pin (`only:["anthropic"]`), so this prior OPEN gate is stale for Stage-7 continuation.
- The prior Opus-4.8 HIGH passability smoke already ran (15-07) and failed on four now-repaired gates, motivating
  Option A (`15-07-26-1019-opus48-high-passability-smoke.md`).
- Finish minimax rung 13-14? Low marginal value; needs extractor override (confounds self-extraction) or skip-flag.
- Commit the 14-07 integrity-fix files (5 items: backgammon.py, cheat_detector.py, test_cheat_detector.py,
  docs/ORACLE-ISOLATION-DIRECTIVE.md, RUNBOOK oracle stanza) — mgr-verified, awaiting Walter approval; stage ONLY
  those (working tree has unrelated pre-existing WIP: golden/*, other scripts, config/bench.env, RUNBOOK env chunk).
- Add a skip-past-extraction-failure option to the ladder (avoids whole-run abort on one self-extract miss).
- **Provider-slug caveat (current-state truth):** the producer-model-provenance → hub capability-eligibility filter
  path (provider slug; `D-PRODUCER-MODEL-PROVENANCE` / `D-CAPABILITY-ELIGIBILITY`, consolidated by
  `D-PROVENANCE-ADMISSIBILITY-2026-07-23`) is canonized but UNBUILT/unproven (prod `attestation:null`; provenance
  does not reach Qdrant) and has NEVER been measured/used as a recall filter; consequence: the 2026-07-24
  persistence directive is design INTENT until this path is proven through real transport, then enforcement is
  permanent.

## 9. LIVE DATA / STACK STATE (⚠ re-derive next session)
- **2026-07-25 (post OrcaRouter migration + step-6 smoke + R0/R1 + zero-progress gate):** qdrant
  `org_wevibe-org-0_memories` = **17** (8 pre-fix UNSTAMPED, Option A leave-and-disclose + 9 born-stamped
  `tencent/hy3` × 3 legs; R2 will wipe = declared experiment reset, diary §22.2). 9/9 containers healthy; clones
  :4550 (PID 4243) + :4451 (PID 4473) running gate code `1a04bae`; canonical mcp tip `c5304d9` (1 push-held);
  wevibe-server 3 push-held (`6740207`, `a171630`, +1 pre-existing); identity unlocked in :4450; Ollama nomic-768
  + LM Studio qwen+nomic live. Suites: bench **572**, canonical mcp 553, clone 557. Worker image
  `wevibe-bench-worker:v1` = `9d389f8e376b` — REVENDOR required before R2 (injection-cadence impl, §3). Spend
  24–25-07 ≈ **$5.54/$115** (step-6 $4.08 + R0 $0.73 + probes). Staged program (diary §22): R0 ✅ R1 ✅ → **R2
  NEXT** (fresh wipe + GLM-5.2 OFF→ON self-lift) → tier-pin → noise calibration → R3 ($40 cap) → R4.
- **22-07-22 (post-stage8 + disclosed D6 rerun):** qdrant `org_wevibe-org-0_memories` = **6 memories** (stage-8
  Cell-1 Opus self-extraction, delivery-proven 6/6; pool frozen). hub :4440 instanceId `94184b05`; 9/9 containers
  healthy; Ollama :11434 (nomic-embed-text:v1.5) up. :4550 clone running pid 55392 (`runs/clone4550.pid`,
  `WEVIBE_RECALL_MODE=test`, dist = clone HEAD + recorded Jul-9 seed seam + 22-07 bench-gated `suppression`
  response field). Worker image `wevibe-bench-worker:v1` = `9d389f8e376b`. Stage ledger: **stage8
  $26.4483/$32.00 (includes disclosed rerun cost $9.69, incl. one $0.39 poller false-hang kill of a healthy kimi
  rep), stage7 $20.04/$40 (frozen history), global $58.5602/$115**. Scored-ladder driver now STAGE_NUMBER=8;
  fresh runs need a FRESH outer runs-dir AND fresh `--ladder-runs-dir` (the shared
  `runs/backgammon/ladder-checkpoint.json` will resume-skip cells off 13-07-era history — the stage-8 false-start
  failure mode). Disclosed rerun run-dirs: `runs/backgammon-scored-ladder-stage8-rerun-d6/` + inner
  `runs/backgammon-stage8-rerun-d6-fix1/`.
- Postgres org-0 = committed rows; chain org-0 exists. A fresh scored run follows §2E: one benchmark-start CLEAN-WIPE (all-inclusive `make redeploy` + residue check), then per-run fixture reset only while corpus persists; re-wipe only on declared true-regression/total-failure re-baseline.

## 10. ⚠ FOR-WALTER CARRY ITEM (unresolved)
A mis-configured clone earlier wrote a bench `org-wevibe-org-0-master` envelope into `~/.wevibe/keys/keys.json`
(mtime 2026-07-14 ~15:19 local) — MAY collide with Walter's CANONICAL org keys. NOT deleted (Walter to verify +
clean deliberately). Bench now writes only durable `~/.wevibe/bench/{leader,contrib}-keystore` keystores (moved off `/tmp` on 2026-07-26 after a power-loss eviction destroyed the bench-org master key) → won't recur.

## 11. ARTIFACT MAP (where everything is)
- **Reports (KB):** `wevibe-meta/workspace/reports/` — this thread: 14-07-26-`0400`(result)/`0130`(recall-probe)/
  `0218`+`0210`+`0207`+`0215`(integrity)/`0313`(validation); 13-07-26-`1319`(SxE refit)/`1354`(dedup-drop)/
  `1357`(14cell driver)/`1407`(session-id)/`1427`(launch-ready)/`1506`(slug)/`1525`(crypto blocker+fix);
  13-07-26-`0407`→`0750`(phase-2 build + N=1 ladder); 13-07-26-`0120`→`0345`(phase-1 consolidation + task build).
- **Docs:** `wevibe-bench/docs/DOCKER-SANDBOX-PROPOSAL.md`, `…/ORACLE-ISOLATION-DIRECTIVE.md`, `wevibe-bench/RUNBOOK.md`.
- **Run logs:** `wevibe-bench/runs/backgammon-14cell/` (14-cell ladder logs + `ladder14-checkpoint.json` +
  `LADDER14-ESCALATE.json` + `*-clone4550.log` recall evidence + wipe/crypto-smoke logs);
  `wevibe-bench/runs/measurement-integrity/` (2-model validation chain log + pid).
- **Scorecards/detail/CHEAT:** `wevibe-bench/runs/backgammon/*-scorecard.json` (per cell: run1-12 = 14-cell ladder;
  miv-glm/miv-minimax = validation; recall-floor055 = probe), `*-backgammon-detail.json` (per-attempt), CHEAT.json
  under `runs/backgammon/miv-*/{off,on}/`, checkpoints `*-checkpoint.json`.
- **Config:** `wevibe-bench/config/bench.env`. **Clone:** `wevibe-bench/scaffold/wevibe-mcp-clone`.
