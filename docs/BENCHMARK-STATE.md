# BENCHMARK STATE & BUILD PLAN — single source of truth (as of 2026-07-15)

> Read this FIRST next session to build the SxE+recall benchmark properly. This is the durable INDEX:
> what the benchmark is, current architecture, all results, WHERE every artifact lives, the key findings,
> and the next build step. Detail lives in the dated reports under `wevibe-meta/workspace/reports/`.
> Home repo: `wevibe-bench/` (own git repo, no remote). Backstop copy: `~/Desktop/benchmark/` (FROZEN, do not delete).

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
- **Clean-start:** `make docker-down && docker-up` (full wipe chain/pg/qdrant) PAIRED with
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

### 2B. Stage-7 crash recovery (Option B, 21-07-26)
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
- Spend disclosure: Stage-7 = `$7.54 / $40` = `$6.17` (completed Cell 1) + `$1.37` (crash-adjacent committed-unproven residue,
  unattributed; never reassigned to another cell). Global ≈ `$19.62 / $115`.
- Detail: `wevibe-meta/workspace/reports/21-07-26-1315-stage7-ladder-crash-fail-stats.md`.

## 3. RECALL / INJECTION SEMANTICS (verified 2026-07-13/14)
- Plugin re-injects ALL approved memories EVERY turn + across compaction (`wevibe-plugin.ts:1349` transform →
  `:1412-1447` inject-all-eligible → `:1503` compaction). Per-session dedup gates ATTRIBUTION only, not injection.
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

## 9. LIVE DATA / STACK STATE (⚠ re-derive next session)
- qdrant `org_wevibe-org-0_memories` = **1 preserved memory** from Stage-7 Cell 1 self-extraction
  (`cid 547b5c0b711fcbdfa8fc7cd8055d30e640a116a062ea2815804d4ef7aed947fd`; preserved, not wiped).
- :4550 clone was left running (pid changes across restarts — re-check). hub :4440, chain, qdrant :6333 up from
  the 14-07 clean wipe. Ollama :11434 (nomic-embed-text:v1.5) — MUST stay up for recall+dedup embeddings.
- Postgres org-0 = committed rows; chain org-0 exists. A fresh scored run should CLEAN-WIPE first (§2 clean-start).

## 10. ⚠ FOR-WALTER CARRY ITEM (unresolved)
A mis-configured clone earlier wrote a bench `org-wevibe-org-0-master` envelope into `~/.wevibe/keys/keys.json`
(mtime 2026-07-14 ~15:19 local) — MAY collide with Walter's CANONICAL org keys. NOT deleted (Walter to verify +
clean deliberately). Bench now writes only /tmp keystores → won't recur.

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
