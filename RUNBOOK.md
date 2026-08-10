# RUNBOOK.md — the operative run card
**Version:** 7 · **Authored:** 2026-08-05 · **Status:** OPERATIVE · **Supersedes:** v6 (2026-08-07) · **Amended:** 2026-08-10 (WO-MODEL-FLAG): §0/§2 `--model` subject selection + CLI syntax correction (main-parser flags precede the subcommand) · §1/RC-7 operator-selects-model amendment · §6 reasoning posture rewritten self-explanatory — frozen at MAX, with the defensible-claim reasoning

> **This is the only operative document. Read it and nothing else to operate the benchmark.**
> Every other document in this repository is history with no authority over what runs. Their binding
> rules have been transcribed here. If any of them disagrees with this card, **this card wins** —
> including on rules. Do not reconcile across documents; there is nothing to reconcile.

> **What changed from v5.**
> (1) **`TESTING.md` is absorbed and deleted.** There is now exactly one operative document (RC-8).
> (2) **The wipe was dangerously incomplete (§2).** A chain wipe destroys the org's epoch key, so the
> local master key in the bench keystores goes stale and **must be cleared in the same step**, or the
> first ON cell fails to decrypt. The mandatory residue check and the re-baseline exception are now
> stated too.
> (3) **New §7 (the stack), §8 (measurement integrity), §9 (extraction and review), §10 (variance
> policy).** These absorb the recall topology, clone bring-up, oracle isolation, the extraction
> integrity correlation mechanics, the smart-leader procedure and the full variance triggers — all of
> which previously existed only in documents now deleted.
> (4) **The harness version is no longer asserted.** Two documents disagreed about it. The card
> records the worker image fingerprint measured at run time instead (§1, RC-5, §6).
> (5) **Rule 5.7 is reconciled with Option A** — published requirements are deliberate; hidden
> constants are the defect (§8).
> (6) **The 900 s hung-process rule is defined** and re-based on the status stream (rule 5.15).

> **NOTATION RULE — absolute.** Never emit literal angle-bracket thinking tags in any chat response,
> commit message, log line, report, or config comment. Write `OPEN_THINK` / `CLOSE_THINK` in prose.
> Emitting the literal tag desyncs the streaming parser and terminates the turn.

---

## 0. OPERATOR QUICKSTART — start one cell

Everything an operator needs to launch a run. Rationale and rules live in the sections cited.

```bash
# 1. Preflight — all three must succeed (§7 for bring-up if any fail):
nc -z 127.0.0.1 4545   # local relay (session + extraction models)
nc -z 127.0.0.1 4460   # leader clone MCP
nc -z 127.0.0.1 4440   # hub

# 2. Worker image — rebuild ONLY when docker/worker/ changed since the last build
#    (the vendored opencode plugin is baked in at build time; a stale image runs
#    the stale plugin silently). Compare:
docker image inspect wevibe-bench-worker:v1 --format '{{.Created}}'
#    vs the newest mtime under docker/worker/. Rebuild:
docker build -t wevibe-bench-worker:v1 docker/worker

# 3. Run one cell (OFF; ON cells add `--mode on --org <org>`, §2).
#    `--model <alias>` pins the subject: the proxy makes that exact model
#    resident on the first request (exclusive load on call — no manual load
#    step). Flags before the subcommand are main-parser flags — argparse
#    rejects them after `run` (verified 2026-08-10: exit 2).
#    Omit --model and the run uses the neutral auto-resident slug.
nohup .venv/bin/python scripts/run_cumulative.py --model qwen3.6-35b-a3b-bench \
  run --until-review --mode off \
  > runs/off-cell-$(date +%Y%m%dT%H%M%S).log 2>&1 &

# 3a. Model switch mid-campaign: --model changes the roster hash, so the
#     existing manifest rejects the run ("roster hash drift detected").
#     This is BY DESIGN — one manifest = one subject model, so OFF/ON pairing
#     inside a manifest is always same-model. Archive (never delete) and rerun:
mv runs/cumulative runs/cumulative.<why>-<date>
#     The server corpus is untouched (§2 wipe rules still govern that).

# 4. Watch + attach. The runner logs the session id and attach command itself:
grep -E 'attach_cmd|session_id' runs/off-cell-<ts>.log | tail -5
#    then attach to the cell's live worker serve:
opencode attach http://127.0.0.1:4096 --session <ses_...>
```

**Optional — hold the stack for UI review (WO-HOLD-UI-1).** Set `WEVIBE_BENCH_HOLD_UI=1` on the
run environment. At benchmark end (all attempts + gates done) the cell is NOT torn down: the
artifact's server boots host-side from the bind-mounted worktree on `http://localhost:8002` —
the exact code the model wrote, via the same boot the gates perform — and the run waits. The log
carries a loud `HOLD-UI ACTIVE` line with the URL, the held container name, and the release
command; machine-readable state is `<run_dir>/hold-ui.json`. Browse the UI (the live view on
:4096 also stays up), then release: `touch <run_dir>/RELEASE_HOLD`. Teardown + reap then run
unconditionally as always (RC-6); heartbeat progress lines keep the status stream live during
the wait (rule 5.15 is not tripped). Never set this on an unattended cell — the run waits until
released or killed, and a kill still tears the stack down.

**Recovery — "roster hash drift detected ... start a fresh run":** the manifest pins the
model roster; a provider/model migration invalidates it. Archive (never delete) and rerun:
`mv runs/cumulative runs/cumulative.<why>-<date>` — the run recreates it fresh. This is a
harness-level reset only; the server corpus is untouched (§2 wipe rules still govern that).

Auth: no key setup needed for the local path — `LOCAL_LLM_PROXY_API_KEY` resolves from
`.env`, the environment, or `opencode.json` provider config, in that order (spend_key.py).
Extraction is a separate invocation after the cell (§9), never folded into the run.

---

## 1. THE CAMPAIGN

**Goal:** measure lift on whatever model is resident — does an accumulated WeVibe corpus make the
same model resolve more of the same problem set, in fewer attempts, on a later run.

**Claim being made (do not overstate):** the corpus was taught durable **solutions to shared
problems**. A compiled-solutions system. **NOT** a capability-lift claim. Held-out variants are
required before any "models get better" statement, internal or external.

**The honest limit (Walter, 2026-08-07):** this campaign cannot measure the human gate, restraint
under the real governor, stranger-contributed memory, real outcome lag, production coverage, corpus
scale, adversarial behaviour, or portability across people and machines. It proves that an
accumulated corpus makes the same model resolve more of the same problem in fewer attempts, under
ideal conditions with the human removed. **That is not a claim about the product.** (Also recorded
where the claim is stated in `BENCHMARK-DIARY.md` §2.4 — deliberate dual carriage, an exception to
the anti-bloat rule (Walter, 2026-08-07): a claim appearing anywhere without its limit is the failure
this fixes.)

| | |
|---|---|
| Subject model | **operator-selected via `--model <alias>`** (WO-MODEL-FLAG, 2026-08-10): the flag names a pinned proxy bench alias; the proxy makes that exact model resident on the first request. Omitting it keeps the neutral auto-resident slug. Identity is always read from the API response and recorded (RC-7) |
| Org | **one org for the entire campaign**, recorded in the manifest; chain assigned `wevibe-org-0`, leader fp `f534aa02` |
| Runtime | **oMLX.** The `--model` alias pins which checkpoint the proxy loads. Identity is read from the API response and recorded in the manifest, so this row is documentation, never a gate |
| Transport | session production model AND session extraction model → local relay proxy `:4545` → resident oMLX model. **ONLY** the embedding/vector-dim path bypasses the proxy to the local embedding endpoint directly |
| Harness | OpenCode in a Docker worker image + plugin. **The version is not asserted here** — the worker image fingerprint is measured at run time and recorded in the manifest (RC-5), and the stack smoke asserts it matches the commit under test (§6) |
| Task | the LOCKED backgammon prompt — unstructured, no requirements checklist |
| Oracle | deterministic: Playwright conformance + Vitest backend + Playwright chromium. **No LLM judge exists anywhere in scoring.** |

**Do not touch the frozen direct entry.** The proxy-bypass entry under `provider.lmstudio` serves
Walter's live interactive session so the proxy container can restart without cutting him off. Only
its `name` field may change.

---

## 2. THE SEQUENCE AND THE ENTRYPOINTS

**The campaign sequence, in full:**

> **test** (all green) → **stack smoke** (all green) → **wipe** (once) → **OFF cell** → **extract** →
> **ON smoke** (unscored, hard gate) → **first scored ON cell** → **extract** → continue

Each stage is **its own invocation**. Nothing here is nested inside anything else. If a procedure
cannot be expressed as one of the stages below, it is not a procedure — it is drift, and it gets
deleted rather than documented.

**TEST**
1. Start the stack.
2. Run the tests.
3. Tear down the stack.
4. Reap — leftover processes and leaked memory.

Gate: all green. Nothing proceeds on a red suite, and nothing proceeds on an *unverified* one.
Targets and conventions: §14.

**Scope (Walter, 2026-08-07):** TEST means the bench pytest suite (§14). The wevibe-meta integration
suite is **not** part of the pre-campaign stage: its suites POST `/v1/test/reset` — a route the hub
build does not register (`cmd/wevibe-hub/main.go:390-397` vs `wevibe-meta/tests/lib/hub-client.ts:95`)
— and its mutating tests would create orgs and memories on the live campaign's hub and chain, the
same hazard class as a second wipe. The reset route must NOT be registered to accommodate it. The
suite is restored behind a guard after the campaign, not before. Recorded as
D-INTEGRATION-SUITE-QUARANTINE (§11).

**SMOKE**
1. Start the docker stack.
2. Run the smoke.
3. Check the smoke output.

The **stack smoke** is a **one-time preflight before the campaign**, not a per-cell step. It is
re-run after a pipeline change (rule 5.1) — never between cells of an unchanged pipeline. Contents: §6.

The **ON smoke** is the delivery-verification gate and runs at a **fixed mid-campaign point**: after
the first OFF-cell extraction (post-wipe), before the first scored ON cell — never skipped, never
merged into that first scored ON cell. Contents: §6.

**Why the ON smoke sits here and not pre-wipe.** A post-wipe corpus is empty, so an ON invocation
immediately after the wipe recalls nothing and certifies nothing. A pre-wipe smoke proves a seam the
wipe then destroys, because the epoch-key mismatch is specifically a post-wipe failure that surfaces
hours later looking like a recall bug. The ON smoke must therefore run after the wipe AND after the
first OFF-cell extraction (which builds a non-empty corpus), as an unscored hard gate before the
first scored ON cell. It is neither skipped nor merged into the first scored ON cell — merging makes
a broken seam indistinguishable from a null result.

**Session ruling (2026-08-07): the pairing token's real deadline was never the wipe.** An OFF cell
emits no serve and no outcome events, so the boundary is the first serve — the ON smoke.

Gate: all green.

**WIPE — the full procedure, and it is not one command**

A wipe is destructive across three separate stores, and doing only the first is the failure that
silently ruins a campaign.

1. **Run the wipe target** (it lives outside this repo). It stops the clone, brings the compose
   project down destroying its volumes, wipes bench state, wipes host state, brings the stack back
   up, rebuilds, restarts and verifies clean. It prints two manual operator steps; the
   `backend-restart` error it emits is **expected and harmless** — the redeploy still succeeds.
2. **Clear the bench keystores in the same step.** Remove `$WEVIBE_BENCH_LEADER_KEYSTORE` and
   `$WEVIBE_BENCH_CONTRIB_KEYSTORE` (defaulting to `~/.wevibe/bench/{leader,contrib}-keystore`).
   **Never `~/.wevibe/keys`** — that is Walter's canonical key directory.
   **Why this is mandatory:** the chain wipe destroys the on-chain org and its epoch key, so the
   local `K_master` in the bench keystores becomes **stale**. Skip this and the next `register-org`
   creates a fresh org whose epoch key mismatches the stale `K_master`, and recall returns
   `decrypt_failed`. The failure surfaces on the first ON cell, hours later, looking like a recall
   bug.
3. **Run the residue check.** All four must hold before anything proceeds:
   Qdrant memory collections empty or absent · chain and Postgres state fresh · served cache cleared
   · both bench keystores gone. **Any residue: STOP and FIX.** Do not proceed.
4. **Start the recall clone** (§7). Finalising org setup regenerates a fresh matching `K_master`
   into the leader keystore.

**The wipe is sanctioned only BEFORE THE FIRST CELL — a wipe AFTER THE FIRST CELL is barred.** The
boundary is the first cell, not "exactly once, ever": a genesis wipe at campaign start, when zero
cells have run and the corpus is empty, destroys nothing measurable and is the sanctioned wipe. A
wipe after even one cell has run destroys the accumulated corpus the ON arm exists to measure. It
does not fail loudly — it silently converts every subsequent ON cell into an OFF cell with extra
steps, and the campaign reports no lift. That silent-corruption protection is exactly why the
boundary is the first cell: the rule protects against a wipe once cells exist, and protects nothing
when the corpus is empty. **Reasoning (recorded, 2026-08-07, WO-ATTRIB-3):** the older "exactly
once, ever" phrasing forbade a legitimate second genesis wipe that occurs before the first cell;
the hazard the rule guards against — silently corrupting ON measurement — cannot arise until a cell
has run, so the bar is correctly placed at the first cell, not at "ever."

**The one exception (rule 5.13):** only a **true regression or total benchmark failure** justifies
re-baselining. That is a deliberate, declared act — never a casual re-wipe, never a "let's try it".
A re-baseline wipes the corpus and so is a walk-back (rule 18) — declared, never hidden.

**BENCH `MODE=on|off`** — one cell. See §3.

**`--org`** — a first-class input to the run command, with mode-dependent requirement.
**CLI syntax:** `--org`, `--model`, `--roster-model`, `--task`, `--seed`, `--manifest` are
MAIN-parser flags and must precede the subcommand
(`run_cumulative.py --org <org> run --until-review --mode on`). argparse rejects them after `run`
with exit 2 (verified 2026-08-10).
- **ON cells: REQUIRED.** `run_cumulative.py --org <org> run --until-review --mode on` — omitting
  `--org` with `--mode on` errors before any run begins ("`--mode on requires --org <org>`").
- **OFF cells: OPTIONAL.** Omit `--org` and the run falls back to `wevibe-org-0`.
- **When `--org` is passed**, the run idempotently ensures the org exists first: it reuses the
  established `run_m1`/create-org path (seeds keywords + org profile — exactly what the preflight
  gate checks), reusing an existing org as specified or creating an absent one. **No separate manual
  `bootstrap_org_m1.py` run is required before a run.** The leader MCP endpoint comes from
  `WEVIBE_BENCH_LEADER_MCP_URL` (the run's existing source; live leader clone :4460). Hub :4440.
- The chain assigns fresh-mint org ids. When `--org` names an absent org on a fresh stack, the run
  proceeds with the chain-assigned id returned by creation, reported in the run log as
  `run_cumulative.org_ensured`.

**EXTRACT** — a separate invocation after each bench, never folded inside the bench command. The
integrity gate and the smart-leader procedure are §9.

### Consequences — these follow from the sequence and are not separately negotiable

- **The first bench after the wipe is necessarily OFF — and it is UNSCORED.** The corpus is empty
  by construction. This first OFF cell exists to build a non-empty corpus so the ON smoke (which
  follows its extraction) has something to recall.
- **One org for the whole campaign.** Any scheme assigning an org per arm or per model is stale and
  wrong: it breaks corpus accumulation, which is the only thing being measured. Walter's 2026-08-07
  reasoning, transcribed: someone must be responsible for a corpus, and the org focuses context for
  retrieval and extraction alike.
- **Mode toggles exactly one thing** — whether injection runs before attempt 1 (RC-4).
- **Smoke and wipe are separate invocations, never nested inside `bench`.** The operator runs each
  stage. The ON smoke is additionally a **hard gate** on the first scored ON cell: it is a separate
  stage run **between** cells, not inside a bench.

**Not every entrypoint above exists yet.** This section is the contract they are built to. See §11.

---

## 3. THE CELL — one per invocation, always

**The first pass is chunked (WO-77, 2026-08-09; compaction loop 2026-08-10).** Attempt 1 is a
sequence of chunk prompts (`tasks/backgammon/prompts/chunk-01..06.md`), driven in order through
the one serve session. Per chunk: drive → marker scan → compaction → next chunk.

- **Marker scan is watermark-windowed.** The harness watermarks the message list before each chunk
  and scans only what THAT chunk produced for `CHUNK FINISHED` — the worker emits the marker and
  its `WEVIBE_DISCOVERY` block in either order, sometimes in separate assistant messages, so a
  tail-only check misdetects. A missing marker is a stall: the harness nudges
  (`WEVIBE_BENCH_CHUNK_NUDGE_BUDGET`, default 3), then fails loudly `chunk_marker_missing`.
- **Inter-chunk compaction.** Chunks 1–5 instruct the worker to call its `self_compact` tool
  (baked plugin `plugins/self-compact.ts`, arm-on-idle → `session.summarize`, autocontinue always
  suppressed — the harness sends the next chunk itself). The harness watches for the compaction
  part (`WEVIBE_BENCH_COMPACT_GRACE_S` 15s / `WEVIBE_BENCH_COMPACT_TOTAL_S` 1800s) and fires a
  fail-open backstop summarize (`auto:false`) when the worker never armed. Compaction is an
  optimization, never a gate; `WEVIBE_BENCH_CHUNK_COMPACT=0` disables it. No compaction after
  chunk 6 — no chunk follows.
- **Relay-killed turns are recovered, never scored.** A relay loop-guard kill (`guard_abort`) or
  stream-finalize-watchdog kill is metered (tokens burned; guard kills excluded from scoring
  turns), then re-driven with a bounded nudge (`WEVIBE_BENCH_RECOVERY_NUDGE_BUDGET`, default 2;
  anti-repetition for a loop kill, resume for a finalize kill); exhaustion is a loud exit 1
  (`loop_guard_exhausted` / `stream_finalize_exhausted`). The anomaly classification is
  **watermark-windowed** like the marker scan: a killed message's `info.error` persists in the
  transcript forever, so each drive classifies only the messages produced since the last
  classification point — a stale kill can never re-trip a recovered, completed drive
  (2026-08-10 chunk-2 defect).

Attempts 2+ remain the error-only feedback loop.
Chunk content is arm-identical (mode toggles only injection, RC-4). Editing any chunk changes the
manifest's `chunk_plan_hash` → the roster-drift recovery applies (§0): archive `runs/cumulative`,
rerun. Serve-phase metering is per-phase **delta** against a pre-send baseline; a phase that ends
with zero new turns AND zero new tokens is a loud `silent_phase` failure, never a clean zero.

1. **Select ON or OFF.** Passed as a parameter.
2. **Watch progress** from the status stream the run publishes (RC-5). Deterministic sensor only —
   no poller, no LLM judge.
3. **The cell ends.** Extraction is the next invocation, not part of this one.

**One cell per invocation. Always.** A campaign is the operator running the command again. The
harness never loops over cells, never runs cells concurrently, never pairs arms, never pre-runs a
baseline set, never decides what runs next.

**What a cell does internally:** build from the fixture → gates run host-side regardless of how the
worker terminated → problems-only feedback (§8) → repeat, `max_attempts` 3 → resolved problems →
publish status → done.

**Resolution happens ACROSS attempts, not within one.** Attempt 1 builds; attempts 2–3 repair
against problems-only feedback. That repair is what produces `resolved_count`, and `resolved_count`
is what produces memories. A cell that cannot reach attempt 2 produces nothing, however good the
artifact is.

**The code fixture resets every run. The memory corpus persists.** Source edits are not
organizational learning; corpus growth is. Per-run reset is **code fixture only** — never re-wipe
chain, Postgres or Qdrant, never reset corpus state.

**Extraction runs in both modes.** An OFF cell is how the corpus gets built in the first place.

---

## 4. THE INVARIANTS THAT MAKE THIS HOLD

**RC-4 · Mode toggles exactly one thing.** `MODE` governs whether the recall/injection step runs
before attempt 1. **Nothing else in the codebase may branch on mode.** Gates, feedback, attempt
ceiling, extraction, teardown and scoring are byte-identical across ON and OFF. Fields that are null
by contract on OFF cells are not a branch; anything else is. This is enforceable by a test that
fails on any mode-conditional branch outside the injection call site, and that test closes the
entire class of "the arms were not comparable" defects that has voided runs before.

The ON delivery-verification gate (`index_ready`, scripts/run_cumulative.py:1255) is NOT a second
branch — it runs only inside the one permitted injection branch, and OFF has no equivalent by
design. It changes no computed metric; it only affects which ON cells are scored (not_scored), so
the per-arm numbers stay comparable.

**RC-5 · One run directory, one manifest, one status stream.** Every run writes a manifest — model
identity as reported by the API, mode, org, commit, **worker image fingerprint**, seed, template
hash — and an append-only status file. The watcher reads **only** the status file. The scorecard is
generated **only** from the manifest plus the status file. No other artifact is a source of truth.
The status stream carries, per attempt: served model identity as reported by the API; the progress
vector; token accounting with **injected-memory-block tokens counted separately from work tokens**;
the injection observability values; extraction-attempt observability; and the terminal outcome with
its reason.

**RC-5a · Task-template freeze (scaffold hash).** The frozen task-template hash for the benchmark
campaign is `a68ff9cba9470fa0ccf5fdee4604425a2ef38631c97a97498369ac2b6159d4d4`, computed as SHA-256
over the live `tasks/backgammon/scaffold/` directory (sorted relative path + raw bytes per file) —
the exact bytes the harness hashes at runtime (`compute_task_template_hash` / `_compute_task_template_hash`,
scripts/run_cumulative.py). Any change to the scaffold invalidates this hash and therefore every
previously scored cell; the run path fails closed (`verify_task_template_frozen`, wired at the start
of `prepare_fixture`). This is the **task template** (backgammon scaffold) — distinct from the agent
reasoning template referenced in §12/§17.

**RC-6 · Teardown and reap are unconditional.** They run on success, on failure, on abort and on
operator interrupt. The reaper kills the run's process group, reaps orphaned Playwright/node
children, brings the compose project down, asserts no listener remains on the bench ports, and
**reports what it killed.** A silent reaper is not a reaper. The gate path spawns real
`node report.mjs` Playwright subprocesses at `backgammon.py:1877` (`_run_gate_report`).

**RC-7 · The harness never selects a model; the operator does, by flag.** Amended 2026-08-10
(WO-MODEL-FLAG): the subject is chosen per run by the operator's `--model <alias>` flag, which names
a pinned proxy bench alias — the proxy then makes that exact model resident on the first request
(exclusive load on call; the swap is refused with retryable 409 while another stream is in flight).
The harness still never decides anything about identity: it is read from the API response and
recorded in the manifest and per-attempt status stream. No identity gate. A served-model change is
**observed and recorded, never aborted on**. Because the flag changes the roster hash, switching
models invalidates the manifest (§0 archive-and-rerun) — one manifest = one subject model, so
OFF/ON pairing inside a manifest is always same-model.

**The corpus is model-agnostic and switching models is expected.** It accumulates knowledge
regardless of which model produced an entry, which model consumes it, in what order, or how often.
Rule 5.2 binds a single cell and says nothing about the campaign. Nothing in this system ties a
corpus to one model, and no rule may be written that does.

**After any model load, before any scored run:** verify the loaded context length is what you
intend, verify parallelism, and get **one real completion through the transport**. **Never accept a
TTL'd load** — a load that auto-unloads mid-campaign voids the cell it was serving. The mechanism is
the operator's; the check is not optional.

**RC-8 · One operating document.** This one: `RUNBOOK.md`, the file you are reading. Anything else
is deleted or demoted to history with no authority over what runs.

**RC-9 · Open-source usability.** The bench must plug into a user's own provider module. It may
never carry hard constraints that force a user to configure their whole provider backend to fit the
benchmark. If it is designed that way, it is wrong.

**RC-10 · Simplify the bench before re-complicating the stack.** If the benchmark needs something
the simplified proxy does not give, first ask whether the benchmark can be simplified instead.

**RC-11 · Docker is the ONLY worker path.** If Docker is unavailable the adapter raises a clear
error and stops. **There is no silent host-side fallback run**, and none may be added: a host-side
run has none of the isolation guarantees in §8 and would be scored as though it did.

---

## 5. BINDING RULES

1. **R-BENCHMARK-INTEGRITY.** End-to-end delivery verification before any measurement run
   (echo-guard exists and exported, seed succeeds with vector stored, dry run shows delivery=YES).
   A full smoke requalification is required between **any** pipeline change and the next scored run.
2. **Extraction model equals producer model.** `extractor ≠ producer` is not a valid arm. This binds
   one cell. It says nothing about the campaign.
3. **Extraction-integrity hard abort.** See §9 — the condition, the discovery path and the
   correlation keys. This is the only extraction condition that aborts a run.
4. **A duplicate denial is never a failure.** The smart leader has exactly one question: *is this a
   duplicate of knowledge already in the corpus?* Not quality. Not novelty. Not usefulness. Not
   safety. Denying one, several, or all candidates aborts nothing.
5. **Quantity mapping.** One resolved problem ≈ one atomic memory candidate. A cell resolving N
   problems that emits ≈0 or ≫N candidates is a **measurement red flag to investigate**, not an
   auto-abort. This mapping stays diagnostic forever.
6. **No LLM judge in scoring. No LLM judge at injection.** The oracle is deterministic, and stays so.
7. **Do not change the task prompt.** It stays unstructured: no requirements checklist, no required
   filenames. **Reconciliation with Option A — read this before "fixing" anything:** pass-required
   behaviour *is* deliberately published in the worktree contract artifact, so a worker can derive
   what is required. That is intentional and does not weaken the instrument. What is withheld from
   the worker is **the oracle itself and the expected/observed values** (§8), never the
   requirements. Do not "restore" hidden constants — that is the defect class Option A fixed.
8. **VARIANCE-POLICY.** Full policy at §10. Never claim inside the noise floor.
9. **Set the seed. Do not rely on it.** It reaches the runtime and reduces variance, and it is free.
   But floating-point non-associativity means kernel reduction order varies with batch shape and MoE
   routing adds load-dependent variability, so determinism must never be claimed.
10. **VOID-INSTRUMENT classes — never scored as capability FAIL:** `finish_reason=length` with
    visible tokens < 100 · any cell run with an unproven seam (§6) · any cell run under a template
    configuration differing from its paired arm · any provider-side truncation. Since `9786da4`
    (WO-TRUNC-1), provider-side truncated turns are **recorded as first-class outcomes** in the
    manifest (`truncated_turns` / `truncated_turns_retried`), not dropped — a truncation is
    observable, metered, and retry-linked; the cell is still never scored as a capability FAIL.
11. **Never infer a pass from the absence of a violation flag.** A clean `invariant_violation:
    false` cannot distinguish "extraction never invoked" from "invoked and cut off by the gate."
12. **A safety mechanism firing is not automatically a pass.** Ask what evidence it destroyed.
13. **Wipe only BEFORE THE FIRST CELL; a wipe after the first cell is barred** — full procedure and
    the single re-baseline exception at §2. The boundary is the first cell, not "exactly once ever":
    a genesis wipe at campaign start (no cells run, corpus empty) is sanctioned; a wipe after any
    cell has run is barred. Reasoning: the protection the wipe rule exists for (a wipe silently
    corrupting ON measurement by destroying the corpus) cannot arise until a cell exists.
14. **Local runs are unmetered by construction.** Disclose that. Never synthesize a cost figure.
    There are no budget kills and no cost gates anywhere in the system.
15. **A run ends only on:** natural completion, variance-policy completion, extraction-integrity
    abort, a hung-process kill per the 900 s rule, or an explicit Walter stop order.
    **The 900 s rule, defined:** the signal is the status stream (RC-5), not a session database. If a
    run publishes no progress for **900 s** after a **180 s warmup grace**, a hung-process kill is
    authorized, and only then. **Log the evidence line first.** The kill is **process-scoped only** —
    kill the worker process inside the container. **Never tear the container down mid-attempt.** If
    the signal is absent or unreadable rather than stalled, the run is blind: **escalate, never
    kill.**
16. **Never emit literal thinking tags.** See the notation rule at the head of this file.
17. **Walter decides; agents do the work.** He does not run commands, paste files, or perform worker
    tasks. Escalate decisions, never chores.
18. **Walk-back versus rerun (Walter, 2026-08-07).** A **walk-back** is forced by: a serve that never
    reaches chain · an outcome that never pairs · standing moving with no human signal and no observed
    transition · the arms differing in anything but injection (RC-4) · a second org or manifest (§1,
    §2) · extraction from a session that resolved nothing (the §9 abort class) · injected-block tokens
    null on an ON cell (the §6 ON-smoke class) · a wipe after the first cell (rule 13, §2). A break
    confined to
    **one cell** is a **rerun** (§10 — a new disclosed run, never a merge). The distinction: anything
    breaking a pairing is a walk-back; anything breaking one cell is a rerun.

---

## 6. PREFLIGHT — checks, then two smokes

A run launched with an unproven seam is **VOID-INSTRUMENT by construction** and is never counted in
N, however clean its output looks. Proven means **observed emitting a real value, on the built
artifact, through the real transport.** Never compile-green. Never "it was dispatched." Never a code
reading.

**Before any scored run, three checks:**

1. **Policy anchor.** The hub's own log must show `status=anchor_verified` for
   `policy_version=edge-policy-v1`. **`anchor_absent`, `anchor_mismatch` or `anchor_unreachable`
   means STOP** — do not run the bench. An anchor mismatch is fatal to the hub at startup, so a
   drifted policy file takes the stack with it.
2. **Both tiers healthy** on their correct paths (§7). Confusing the two is the single most common
   bench failure.
3. **Model load verified** — context length, parallelism, one real completion, no TTL (RC-7).

**Stack smoke** — fails the campaign if it cannot assert all of:

| Assertion | Blocks |
|---|---|
| Worker image fingerprint matches the commit under test | everything below — and it is what settles which harness version is actually running |
| Oracle boots and scores a reference artifact, entrypoint resolved **from** the artifact across all spawn sites | everything — an oracle that cannot boot reports capability failure for an instrument fault |
| Progress vector populated on a real full-path cell: `problems_before` / `problems_after` / `resolved_count` / `agentic_cycles` / `tool_calls` / `test_invocations` / `attempts_to_green` | all convergence claims |
| Extraction-attempt observability distinguishes "never invoked" from "gate cut it off" | all integrity claims |
| Reasoning cap present in the outbound request, not deleted by the proxy | any cell that could produce the void signature above |

### Reasoning controls — verified 2026-08-10, oMLX 0.5.7 (live probes, Qwen3.6-35B-A3B)

What each knob ACTUALLY does on this stack, measured — not assumed:

| Knob | Where set | Verified behaviour |
|---|---|---|
| `max_tokens` | request (proxy clamps to alias `limits.output`) | The ONLY hard ceiling. Reasoning and visible content share this one budget; exhaustion = `finish_reason: length`, possibly severed tool-call JSON (the VOID-INSTRUMENT class of rule 5.10) |
| `max_reasoning_tokens` | bench alias `requestDefaults` (forced 8192; client value stripped) | **ACCEPTED but NOT enforced by oMLX 0.5.7.** Probe: `max_reasoning_tokens: 50` → full reasoning still flowed (284 completion tokens). The smoke assertion below is a PRESENCE check (proxy fills it, oMLX accepts it) — it is NOT an enforced clamp. Do not rely on it as one |
| `reasoning_effort` | request; **stripped by bench aliases** (`forbiddenRequestParams`) | Accepted by oMLX (no 400). Qualitative level (low/medium/high; DeepSeek V4 publishes effort levels). Not a token cap |
| `chat_template_kwargs.enable_thinking: false` | request | The real OFF switch, verified: zero reasoning, answer-only (3 tokens vs 200) |
| thinking on/off + `preserve_thinking` defaults | oMLX admin per-model settings | Native, apply at load. `preserve_thinking_default: true` is set for both Qwen3.6 models (2026-08-10) — the BENCH aliases override per-request with `preserve_thinking: false` (§12 remediation item 1) |
| `thinking_budget_enabled` | oMLX admin per-model settings | Exists in the schema; UNVERIFIED — do not enable mid-campaign |

**Frozen for the campaign: MAX reasoning — what that means, and why (Walter, 2026-08-10,
WO-MODEL-FLAG).**

*What "reasoning" is, plainly.* These models can think before they answer: they generate a
chain-of-thought that costs output tokens but is not part of the visible reply (it streams as
`reasoning_content` and is metered separately as `reasoning_tokens` in the status stream). The one
budget that matters mechanically is `max_tokens`: thinking and the visible answer **share** it. If
thinking eats the whole budget the turn ends `finish_reason: length`, possibly with severed
tool-call JSON — the VOID-INSTRUMENT class of rule 5.10.

*Why MAX is the posture.* "Max" on this stack means: **thinking ON, and no client reasoning
parameters at all** — the model thinks as long as it natively wants, bounded only by the output
budget. This is not a vibes choice; it is the only setting that survives scrutiny:

1. **There is no enforceable middle.** `max_reasoning_tokens` is accepted but NOT enforced by oMLX
   0.5.7 (probe: cap 50 → full reasoning flowed anyway). A token "cap" we cannot enforce is an
   assumption, not a control.
2. **`reasoning_effort` is an unverified dial.** Accepted (no 400), qualitative (low/medium/high),
   and its behavioural effect has never been measured on this stack. Pinning it would put an
   unproven knob inside the instrument — indefensible.
3. **The claim only needs frozen, not tuned.** The campaign's claim is a *within-model* OFF-vs-ON
   delta. Reasoning depth is identical in both arms by construction (RC-4: arms differ only in
   injection), so it cancels out of the comparison. What would break the claim is the two arms
   thinking *differently* — which is why the posture is frozen, not why it is large.
4. **Max gives each attempt its best shot** on a multi-hour agentic build task; the cost is bounded
   by the attempt ceiling and the 900 s rule, not by reasoning length.

*Why MAX does not endanger the instrument.* Its one failure mode — reasoning eating the shared
budget — is defended structurally, not by hope: bench aliases carry **output 32768** (2× the
interactive 16384, the R2 clamp-guillotine fix: thinking cannot starve the visible answer),
`failOnFinishReasonLength: true` so any truncation is flagged loud, and `truncated_turns` /
`truncated_turns_retried` are first-class manifest fields (rule 5.10). A tripped guard voids the
cell — it never scores as a capability FAIL, so the claim stays clean even when the posture bites.

*The mechanics.* The bench aliases own every reasoning-adjacent parameter
(`max_reasoning_tokens: 8192` as a presence-marker, `preserve_thinking: false`,
`reasoning_effort` client-stripped via `forbiddenRequestParams`). The worker sends none. Keep it
that way: the bench's request shape is byte-identical across cells, arms, and models.

*The change rule.* Frozen for the campaign. Touch only between campaigns, and declare it when you
do — a mid-campaign change makes the arms differ in something other than injection (rule 5.18
walk-back class).

*Benching a new model — the two-block pattern.* One proxy alias + one registry entry, nothing else:

1. **Proxy** (`Local LLM Proxy/config/models.yaml`): copy the `qwen3.6-35b-a3b-bench` block, set
   `upstreamModel` to the model's exact oMLX id and the card samplers in `requestDefaults`
   (oMLX cannot store `top_k` per-model — the proxy must fill it). Keep the bench contract
   untouched: `limits.output: 32768`, `preserve_thinking: false`, the `forbiddenRequestParams`
   pair, `concurrency.queueDepth: 0`, `streamHeartbeatMs: 0`, `loopPolicy.failOnFinishReasonLength:
   true`. **The proxy reads this file once at boot — adding an alias requires a proxy restart
   (operator step).** Never point the bench at an interactive alias: it inherits the 15 s SSE
   heartbeat the bench's undici worker hangs on, clamps output to 16384, and queues behind
   interactive traffic.
2. **Bench** (`wevibe_bench/config.py` `WORKER_MODEL_REGISTRY`): mirror Walter's daily
   `opencode.json` model block, with the ONE deliberate difference `limit.output: 32768` (the bench
   budget; opencode clamps `max_tokens` to the declared limit, and the alias default is fill-only —
   a 16384 declaration silently halves the cell's budget).
3. Run with `--model <alias>`. Roster hash changes → archive `runs/cumulative`, rerun (§0).

**Walter's personal opencode sessions are deliberately UNCAPPED** (OMLX-REASON-1, proxy
`config/models.yaml`): the interactive aliases send no reasoning parameters at all, so thinking runs
as long as it needs inside the 16384 output budget. His control, when he wants one, is per-model
`options.reasoningEffort` in `opencode.json` — the proxy passes it through untouched on interactive
aliases. Never add a reasoning default to an interactive alias.

**ON smoke** — the delivery-verification gate; gates every ON cell. It runs at a **fixed campaign
point**: after the first OFF-cell extraction (post-wipe), before the first scored ON cell (matching
§2). It is additionally re-run after any pipeline change (rule 5.1). Asserts non-null on the actual
worker image, through the real transport: `injected_count`, `injected_block_chars`,
`injected_block_est_tokens`, `consumer_injected_count`.

These four are **null BY CONTRACT on OFF cells** (`memory_mode != "on"` ⇒ `None`). An OFF cell
therefore proves nothing about them, and a null there is not a defect. Running ON without this smoke
is exactly what voided the paid R2 campaign. This is why the smoke sits after the wipe, matching §2:
a pre-wipe smoke proves a seam the wipe then destroys.

**The `missing_telemetry_seams` list is itself an instrument.** In R2 it named seven seams, four of
which had real values in the same record. A list that over-reports trains the operator to ignore it.

**Latency is a hard, measured seam (recorded 2026-08-08, WO-RT-O6).** Latency on the critical path is
a **hard blocker, not a budget with an escape hatch** — the gate blocks with **no timeout and no
fallthrough**, and a serve that exceeds the latency bound is a defect, not a degraded-but-acceptable
run. Latency is a **standing objective** and IS one of the seams the bench's seam scanners scan and
that production measures. Treat it as part of the seam set alongside `missing_telemetry_seams`, and
apply the same VOID-INSTRUMENT rule here: a run launched with an unproven — unmeasured, assumed —
latency seam is VOID-INSTRUMENT by construction. **Latency must be measured, never assumed.**

---

## 7. THE STACK — topology and bring-up

### The two tiers. Confusing them is the #1 bench failure mode.

The recall data path is: **bench script → MCP `/v1/recall` (`:4450`, or `:4550` for the clone) → hub
`/v1/orgs/{org}/query` (`:4440`)**. These are two separate services with **different ports,
different health paths and different auth**.

| Tier | What it is | Address | Health | Auth |
|---|---|---|---|---|
| **Hub** | Docker container `wevibe-hub` — the ONE hub, normally already running | `127.0.0.1:4440` | `GET /health` | none |
| **MCP recall client** | `wevibe-mcp` process, or the bench clone | `127.0.0.1:4450`, clone `:4550` | `GET /v1/health` | bearer token |

- **The hub is a container, not a host process.** `ps`/`lsof` finding nothing is **normal** and is
  not evidence the hub is down. Check `GET :4440/health`.
- The health paths are **not** the same path.
- In config, `hub_url` is the hub and `mcp_recall_url` is the recall client; the recall backend
  posts to `{mcp_recall_url}/v1/recall`.
- **Every recall, seed or measure path calls the preflight helper before any recall operation.** It
  checks both tiers on the correct paths and raises a loud error naming the exact remediation.
  **Read the error — do not work around it.**

### THE HARD RULE

**Never build, compile or start your own hub or MCP. They already exist.** If a recall fails: read
the preflight error, bring the named service up, and if you cannot — **STOP and report.** Do not
improvise infrastructure. Do not compile a new hub or MCP. Do not invent a fallback.

### Bringing up the recall clone

The preferred path is the lifecycle bring-up, which injects the environment for you. A standalone
start must reproduce the same environment exactly. **The identity seed must be the bench leader
seed** or recall cannot decrypt the seeded corpus. Six requirements are non-obvious, and each has a
known failure mode:

| Requirement | What breaks without it |
|---|---|
| `WEVIBE_UMBRAL_SIDECAR_BIN` + `WEVIBE_GUARD_BIN`, derived from the workspace root | `register-org` 500s demanding the sidecar binary. The plugin normally injects these; a manual start does not. This caused the 2026-07-13 cell-1 abort |
| `WEVIBE_MCP_HTTP_ONLY=1` | The clone also runs the stdio server, which treats a backgrounded stdin-EOF as shutdown. Required for any backgrounded start |
| `< /dev/null` on the launch | Belt-and-braces so the stdio path never sees an open-then-closed stdin |
| `WEVIBE_RECALL_MODE=test` | Recall is prod-governed (floor 0.55, budget 3) and a fresh low-trust memory is filtered out — **prove-delivery and the ON recall arm both return nothing.** Test mode also **auto-approves** recalled memories; prod or unset **headless injects NOTHING**, because it waits on a human approval popup that no headless run can answer |
| `WEVIBE_KEYSTORE_PATH="$WEVIBE_BENCH_LEADER_KEYSTORE"` | The org master-key envelope is written by the clone and read by the invite and provision-recall subprocesses. Omit it and the writer uses the default directory while the readers look in the bench keystore — `decrypt_failed` on recall, `no master key found` on invite. **Writer and readers must share this path.** This was the other half of the 2026-07-13 blocker |
| `WEVIBE_BENCH_ENDPOINTS=1` | The bench-only `/v1/submit` and `/v1/identity/pubkeys` endpoints are absent. `/v1/health` is always present |

**The clone serves from its build output.** Code changes require a rebuild **and a restart** before
they take effect. **Decryption happens in the clone, not in the worker plugin** — the worker needs
only HTTP to the clone, and no host keys or corpus ever enter the container.

To measure *filtered* recall headless, override the relevance floor and injection cap in the plugin
config while keeping test-mode auto-approve. That is the clean way; changing the mode is not.

### Worker isolation boundary

- The worktree is mounted read-write as the worker's only view.
- **Gate and golden material is NEVER mounted.** Gates run host-side after the worker exits.
- The worker reaches the clone, and through it the hub and the embedding service, on the recall path
  only, and only on ON cells.
- Egress is **not** domain-allowlisted — the worker retains general outbound network access. This is
  a known, accepted residual, not an oversight to rediscover.

---

## 8. MEASUREMENT INTEGRITY — three layers, all mandatory

**The invariant:** workers must never read or access gate oracle or test sources, and worker
feedback must be problems-only.

**Layer 1 — physical isolation.** Oracle material is never copied into the worker worktree: gates,
golden, judge, the gate runner and gate test sources all stay outside it. The worker's directory is
seeded only from the scaffold stubs. The gate runner executes from its own directory and reaches the
worker's output only through an environment variable.

**Layer 2 — permission deny.** The worker launches **without** any skip-permissions flag. A
worktree-local config grants autonomy inside the worktree and denies external reads, with targeted
denies for oracle paths. **Permissions are allow/deny only — never `ask`.** A headless run has no
human to answer a prompt and `ask` can hang forever; `deny` fails fast and the model continues.
Verified: a denied external read returns a tool error and exits cleanly in seconds.
**Known residual hole:** a shell can still exfiltrate external files by indirection that
path-pattern denies cannot fully close. Layer 3 exists because of this.

**Layer 3 — operator live view (primary) + transcript hard gate (stdout path only).** Amended
2026-08-10 (Walter): the founder watches the live worker session (`opencode attach` to the cell's
serve, §0 step 4) and IS the Layer-3 backstop on serve-driven cells — a cheat ATTEMPT the operator
witnesses is noted, never an automated verdict flip; PASS/FAIL is never gated on the anti-cheat
rule. The automated transcript hard gate stays enabled but its only operative input is the stdout
fallback path's event log: after such a cell, scan it for any tool call whose input references
oracle paths or distinctive oracle filenames, including the gate runner. **Any hit forces the
verdict to CHEAT → INVALID/FAIL — never PASS, even if every gate passes.** Write a loud marker and
surface it in the scorecard. On serve-driven cells (the primary path) the scan's input is not
exported — accepted posture, not a defect: live-view replaced it.

**Feedback content limits.** Worker-facing feedback carries **only the failing gate's ID and human
title**, in the form `- [G02] pip count: FAILING`. **Forbidden in worker-facing feedback:** expected
values, observed output, file paths, stack traces, oracle snippets. The rich detail stays in
host-side logs and is stripped before the worker sees anything. A failure points the worker at a
**public requirement**, never at a hidden value.

**Maintainer rules — each of these has already been violated once:**
- Never re-add a skip-permissions flag to worker launches.
- Never include expected, observed, path or stack detail in worker-facing feedback.
- Never copy oracle assets into a worker worktree.
- Keep the transcript hard gate enabled on the stdout fallback path (its only operative input). On
  serve-driven cells the backstop is the operator's live view of the worker session.

**Option-A invariant:** no gate may require a constant, formula, string, count or mechanism that is
not published in the worktree contract artifact. Publishing requirements is orthogonal to all three
layers above and weakens none of them (rule 5.7).

**Observability-funnel identity (recorded 2026-08-08, WO-RT-O6).** The PRODUCTION observability funnel
and the bench's T8 seam scanners read the SAME counter set. The recall-trigger path is instrumented at
every seam, and every counter is readable per session — production observability and the bench's seam
scanners are **two readers of one counter set**, not two separate instrumentations. This ties the
bench's T8 instrumentation to the recall-trigger funnel: what a T8 seam scanner proves on a bench run
is the same signal production reads in the field. Corollary, cross-referenced only (already recorded
in RECALL-PIVOT-SPEC's funnel, not re-ratified here): the normalizer is the sensitivity dial with a
silent failure mode, detectable only as a ratio between two seams (episodes opened vs repeats
detected); its counter is not optional instrumentation.

---

## 9. EXTRACTION AND SMART-LEADER REVIEW

### The integrity gate — runs first, always

After **every** extraction, read the matching terminal integrity record from the ops integrity log
for that UTC day, under the configured log directory.

**Correlation keys.** The outer trace does not propagate into the MCP — each REST call mints its
own. The reliable keys are: **the `job_id` returned in the extract call's 202 response**, and
**`session_fp = sha256-first8(session_id)`**. `org_id` further scopes a match.

**Abort** — before any leader verify or commit — if the record is **missing**, **cannot be
correlated**, or reports `resolved_problem_count == 0 && emitted_memory_count > 0`
(`invariant_violation == true`). Preserve the run log and checkpoint, and escalate with the job id,
trace, session fingerprint and the resolved and emitted counts.

**Do not continue. Do not self-heal. Do not retry around it. Do not approve or commit the memory.**

A record that is `completed` but **lacks the episode-count fields or lacks the violation flag is
uncorrelatable-for-invariant**, and is abort-worthy under the missing-record rule. Resumed or parked
jobs may report their episode metadata as unavailable on resume.

### Smart-leader review — only after the integrity gate passes

The run advances one session to the review boundary, then pauses and yields, returning the sequence
index, job id, session fingerprint and candidate count. It resumes only with an explicit decision.

1. **Reconcile.** Reconcile the authoritative chain and hub inventory against the private benchmark
   catalog. Any authoritative committed item with no matching catalog text is reported as
   unavailable and **must never be guessed or fabricated**. Fail closed when the authoritative
   inventory is non-empty but the catalog is incomplete, so completeness is never silently assumed.
2. **Compare.** Compare every new candidate against the catalog using the implemented duplicate
   signals: exact content-hash match, exact submission-hash match, and a keyword-overlap advisory.
   Carry duplicate references into the decision evidence.
3. **Decide all.** Emit a versioned decision manifest with every candidate set to verify or deny,
   each with a non-empty reason. The manifest must carry integrity attestation: job id, session
   fingerprint, resolved count, emitted count, violation flag, and whether the integrity record was
   seen. The manifest gate rejects missing or uncorrelatable attestation — but it does **not** re-run
   the runtime integrity check. That already happened, above.
4. **Apply — real leader and hub paths only.** Verify goes through leader verify-and-commit; denial
   goes through the real hub deny route, body-signed. **No direct database, vector-store or chain
   writes, ever.** Reapplying the same decision manifest is idempotent; a conflicting re-decision is
   rejected.

**Denial is non-fatal curation** and must not abort the benchmark (rule 5.4).

### The privacy boundary — do not conflate the two "leaders"

The **cryptographic leader-signer** on the commit path sees **no plaintext** — only ciphertext, a
wrapped key and an embedding card. The **smart-leader coordinator** necessarily **does** read
candidate and prior-accepted comparison text to make semantic decisions. **This is by design and is
not a leak.**

That authorized plaintext lives **only** in the mode-0600 private review card, catalog and review
material. It must never be copied into logs, reports, decision ledgers, the manifest checkpoint or
git — all of which stay **hash-only**: fingerprints, sizes, counts and reasons, never plaintext,
secrets or raw keys.

### Injection cadence

A recalled and accepted memory is injected **once at acceptance**, in a stable early position after
the system instructions — **not re-pushed per turn**. The served set is hub-ranked top-K within a
fixed token budget. The injected block is preserved **verbatim** across compaction: restore
verbatim, never summarize through. **In every measurement arm the memory block's tokens are metered
and reported separately from the model's work tokens** — every progress vector that reports tokens
carries the injected-memory-token count as its own field. Progressive disclosure is parked as a
future seam, not a flag.

**Caveat, unverified:** if the vendored plugin inside the worker image predates this cadence, the
plugin still re-injects every turn. **Do not report cadence effects as conformant until the image is
confirmed to carry the cadence code** — the stack smoke's fingerprint assertion is what confirms it.

---

## 10. VARIANCE POLICY — in full

1. **Baseline: N=1 per scored cell.**
2. **Borderline cells repeat to N=3.** If any trigger below fires for a cell, **that cell and only
   that cell** re-runs to a total of three. The reported verdict is the **majority** for discrete
   outcomes and the **median** for continuous metrics. All three runs' raw artifacts are retained.
3. **Every scorecard discloses N per cell. A cell reported without an explicit N is
   non-conforming.**

Repetition budget is spent exactly where uncertainty lives, instead of pretending N=1 is statistics.

**T1 — Gate margin ≤ 1.** The final attempt fails exactly one gate, or the cell passes only on its
last permitted attempt. Either way the verdict sits within one gate or one round of the boundary.

**T2 — Lift sign fragile.** For an ON/OFF pair: the relative token delta is **under 15%**, or the
attempts-to-green are equal so the sign rests on token and turn deltas alone. **A sign that flips
within ±15% single-run noise is not a reportable sign at N=1.** The 15% is a manager-set constant
and is vetoable.

**T3 — Instrument anomaly.** The run log shows a wall-clock kill or timeout, a nonzero worker exit
that was retried, or a mid-cell resume — while the cell still produced a scored verdict. Anomalous
instrumentation invalidates N=1 confidence regardless of the verdict.

**T4 — Classification flip.** The cell's result would re-classify the subject, or lands exactly on a
class boundary. **A single run never re-classifies on its own.**

**Procedure.** Triggers are evaluated **once, immediately after the N=1 run**, from the artifacts —
no judgment calls, no re-litigating afterwards. If fired, two more runs under the same config and
seed policy, then majority/median. **If the three runs disagree on class, escalate to Walter. Never
average across classes.**

**Rerun disclosure (locked): a rerun is a new disclosed run, never a merge.**

---

## 11. OPEN DEFECT REGISTER

Fixed defects are not listed. They are in git.

| ID | Status | Description | Blocks |
|---|---|---|---|
| **D-TEMPLATE-DESYNC** | 🔴 **top campaign risk** | The proof is partially executed. Low-context **PASSED** (WO-TEMPLATE-PROOF-1: two 105-turn sessions, 0 desyncs each); high-context (≥100K tokens) behaviour — what the §12 probe measures — remains **unverified**. Blocks every scored cell until high-context is proven. See §12. | every scored cell |
| **D-ENTRYPOINTS-MISSING** | 🟢 CLOSED by 09cab437 | `feat: split extraction invocation, add run --mode, unconditional reaper` makes test/smoke/wipe/bench/extract a coherent entrypoint set. | §2 |
| **D-MODE-DRIFT** | 🟢 CLOSED by 09cab437 + 98a286b + 0141930 + e60ccc1 | The 13 drift branches are gone: the delivery-scan arm is keyed on the injection record not mode (`98a286b`), the telemetry seam on `injected_count` (`0141930`), and the scorecard is label-invariant under `e60ccc1`'s test. The only remaining mode branch is the legitimate injection call site. The arms are comparable. | every scored comparison |
| **D-RUN-STATUS-MISSING** | 🟢 CLOSED by 1a50ba9 + e60ccc1 | Write-once run manifest + append-only status stream + scorecard landed in `1a50ba9`; `e60ccc1` adds the scorecard test. | the contract itself |
| **D-NO-REAPER** | 🟢 CLOSED by 09cab437 | `process_reaper.py` (RC-6 unconditional reaper) wired into every exit path, with tests. | §2 TEST step 4, bench |
| **D-PRERUN-PAIRING** | 🟢 CLOSED by fd427759 + e285ece | `fd427759` retires the prerun arm-pairing + consumer-bridge paths; `e285ece` makes it strictly serial single-consumer with no concurrency. | §2, §3 |
| **D-MODEL-ALIAS-RESIDUE** | 🟢 CLOSED by 8bdcabc + 186d34c | `8bdcabc` removes 4 dead model-registry aliases; `186d34c` removes the dead paid-era alias. The mode-drift work that gated this is cleared. | RC-7 |
| **D-ALIAS-RESIDUE** | 🟡 OPEN | The proxy still ships a poller alias plus bench aliases referenced by no bench code. | deletion hygiene |
| **D-DOC-DRIFT** | 🟢 CLOSED by WO-CONSOLIDATE-1 | `AGENTS.md` no longer carries the stale **org-per-arm** scheme or poller-era stanzas; it is now a pointer to the card (§2-consistent). | RC-8 |
| **D-TRACE-SEMANTICS** | 🟡 OPEN | Per-consumer attribution survives only as a random trace nanoid with no role semantics. | log-based attribution |
| **D-PROXY-UNTESTED** | 🟡 OPEN | The proxy has no git remote and no tests while sitting on the critical path for every bench call. | campaign safety |
| **D-RECALL-EMPTY-KEYWORDS** | 🟢 CLOSED by 33fe59a (wevibe-server) | Hub-side `fix(serves): accept vector-only serves with empty matched_keywords` — the serve endpoint now accepts vector-only serves with empty `matched_keywords`; the dead 400-mapping clause is removed. | ON-cell attribution |
| **D-STRAY-BENCH-KEY** | 🟡 OPEN — **Walter only** | A mis-configured clone once wrote a bench org master-key envelope into Walter's canonical key directory. It **may collide with his canonical org keys**. Not deleted, and **no agent may delete it** — Walter verifies and cleans deliberately. Bench now writes only to the bench keystores, so it will not recur. | nothing automated |
| **D-KV-PEAK-UNKNOWN** | 🟢 CURIOSITY | Peak resident footprint at full context is unknown. Not a threat given headroom. | nothing |
| **D-EMISSIONS-INERT-KEEPERS** | 🟡 OPEN | The emissions module carries an injected serve keeper and reputation keeper (`wevibe-chain/x/emissions/types/expected_keepers.go`, wired at `keeper/keeper.go:35-47`) whose methods are never invoked outside tests — inert today, and exactly the seam an accidental change would activate. Serve credit touches no economics: emissions qualify contributors on approvals only (`x/emissions/keeper/keeper.go:233`). Recorded 2026-08-07 (WO-CANON-1); cross-posted to RECALL-PIVOT-SPEC §8.7 F5. | nothing today — silent-economics drift if activated unnoticed |
| **D-INTEGRATION-SUITE-QUARANTINE** | 🟡 OPEN — post-campaign | The wevibe-meta integration suite is scoped out of the pre-campaign TEST stage (§2): it POSTs `/v1/test/reset`, a route the hub build does not register (`cmd/wevibe-hub/main.go:390-397` vs `wevibe-meta/tests/lib/hub-client.ts:95`), and its mutating e2e tests would write orgs and memories to the live campaign hub and chain — the same hazard class as a second wipe. The reset route must not be registered to accommodate it. Restored behind a guard after the campaign (Walter, 2026-08-07). | nothing while quarantined — pre-campaign TEST is the bench pytest suite |
| **D-RECALL-SELECTION-BIAS** | 🟡 OPEN — known, stated limitation | Recall fires only after a repeat — the second failure under the same stable `failureKey` while still red — so every serve is conditioned on an already-hard problem. Standing therefore measures **"works on stuck problems," not "works."** Defensible, and arguably the population that matters, but a further departure from the sim's uniform-serving assumption (recorded 2026-08-08, WO-RT-O6; claim and limit travel together — the §1/BENCHMARK-DIARY §2.4 dual-carriage principle). | every standing/recall conclusion — disclosed, not blocking |

**Memory is not a constraint — CLOSED, do not re-investigate.** Zero swap, ~211 GB wired headroom.
The trap that misled two sessions is `top`'s "unused" line, which excludes inactive pages macOS
reclaims on demand. A memory-pressure report derived from `top` or `free`-style arithmetic is a
misread, not a finding. One real constraint: a transient ~62.6 GB spike occurs **during** model load,
so never load two instances simultaneously.

---

## 12. THE TEMPLATE DEFECT

**What it is.** On tool-enabled turns the installed template pre-fills an unclosed opening reasoning
tag; the model's own tool instructions demonstrate a format that opens another; the tags nest; the
first close closes the outer block; the streaming parser flips to content early and misattributes
everything after. The fallback extractor then splits assistant content on newline-plus-closing-tag,
so a desynced turn gets its history mangled on the next render. Reproduced six times on demand.
Degradation grows with context length.

**CLEARED as the cause of the dead 27B cell's turn 85** — that was a genuine transport drop. Do not
rediscover this.

**Why it is still the top risk.** ON cells inject a memory block; that block consumes context; so an
ON cell reaches any context-length degradation ridge **earlier than its paired OFF cell, by
construction.** The defect does not add noise — it systematically penalises the arm that is supposed
to win. If lift is real but modest, this erases it and you would conclude the memory system does not
work.

**Remediation ladder — stop at the first that passes the ≥100-turn proof:**
1. `preserve_thinking: false` — **already set, unproven.** Prove or disprove it first.
2. Swap to a self-healing template that inserts the missing close. A **template** swap, not a model
   swap.
3. Characterise and disclose as a bounded constant, with a measured desync rate per 100 turns,
   reported on every scorecard.

**Whichever lands is FROZEN for the campaign and must be byte-identical across OFF and ON.**

---

## 13. WALL-CLOCK

Measured locally on the 27B: OFF cell attempt 1 = **4,369 s ≈ 72.8 min**. Full gate suite = **25.3
s**, negligible. Essentially all cost is model time.

A full 3-attempt cell plausibly runs **2–3.5 h**; repair attempts should be shorter than the initial
build, but that is **unmeasured — measure, do not assume.** OFF plus a handful of ON cells is
**days**. Size N from measured local numbers only. At ~72 min per attempt, an N=3 escalation is
several hours minimum, so a paired OFF/ON difference at N=1 is not a result.

---

## 14. TEST INFRASTRUCTURE

Config lives in `pyproject.toml` under `[tool.pytest.ini_options]` — there is **no `pytest.ini`**.
Addopts: `-n auto --dist=load --timeout=60 --strict-markers --tb=short -ra -m "not slow"`,
`timeout_method = "thread"`.

**Targets:** `test` (full suite) · `test-fast` (skips slow) · `test-file FILE=…` · `test-name
NAME=…` · `test-slowest` (ten slowest from the last run) · `test-all` (everything, including slow).

**Markers:** `slow` — excluded by default. `serial` — intended for tests that must not run in
parallel; NOTE: `tests/conftest.py` currently only WARNS on serial-marked tests and does not actually
force serialization (doc-vs-code drift) — do not rely on `serial` to order tests under `--dist=load`
until the conftest is fixed.

**`--dist=load` (work-stealing) is the default.** Tests spread across the `-n auto` workers as they
free up. This was switched in WO-NIGHT-2 Phase 2 item 3 to cut the suite runtime from ~20s to ~10s:
the previous `--dist=loadfile` serialized the 8 subprocess-launching truncated-turn tests on a single
worker (the dominant cost, ~19.65s), while `--dist=load` scatters them across workers to run in
parallel. Trade-off: work-stealing can scatter tests that share a fixture, a port or a temp path.
Tests that must stay ordered/serialized must carry the `serial` mark (see below) or be kept in a
shared-state-safe pattern; verify the full suite is green before relying on this mode. The truncation
tests are safe under `--dist=load` because they use per-test `tmp_path` + read-only `TASK_DIR`.

**The 120 s agent shell limit is load-bearing.**
1. **Never pipe suite output through `tail`, `head` or `grep` alone.** They buffer until process
   exit, so a timeout kill produces **no output at all**. Redirect to a file and read the file.
2. `--timeout=60` is deliberately below the shell limit so a hung test yields a named timeout
   failure rather than dying silently. `timeout_method = "thread"` is set because the default signal
   method does not reach xdist worker processes.

**NEVER raise the timeout.** If a test consistently needs more than 60 s it gets marked `slow` and
runs only with `test-all`. To resolve a timeout: find the hung test in the run log, run just that
file, mark it `slow` if it is genuinely slow or a flaky network test — and never raise the ceiling
without understanding why it was slow.

**Log rotation:** timestamped logs per run, last ten kept, with a stable pointer to the newest.

**The gates oracle is not pytest.** It is a separate JS suite run through its own runner. The test
target does not exercise it.

**Gates must resolve the entrypoint from the artifact**, never assume a fixed server filename — the
build pipeline may change it. A hardcoded filename here is what produced a whole dead campaign cell.

**Verified baseline: 442 passed / 0 failed / 1 skipped / 0 errors / 443 collected, in 10.09s**
on HEAD `aba1578` (WO-CONSOLIDATE-1). collected = 443 = passed + skipped + failed + errors; the
9 slow-marked tests are deselected by `-m "not slow"` (443/452 collected, 9 deselected). Any
change must return to this or account for the difference.

---

## 15. RULES FOR THE ORCHESTRATOR

1. **Report evidence, not conclusions.** Every factual claim carries `file:line` or a log path. If
   you cannot verify it, write **UNKNOWN**. Do not infer, do not fill gaps.
2. **Claims of a negative need evidence too.** "Pre-existing", "unrelated", "unchanged", "no longer
   referenced", "no artifacts present" are assertions, not observations. This is the class that has
   failed repeatedly.
3. **Contradiction rule.** A finding that contradicts an earlier report must quote the earlier claim
   verbatim, state which is wrong, and give the evidence. Never silently replace a prior finding.
4. **Confirm every distilled answer** with a gather against the content you previously found.
5. **Do not self-patch on a self-diagnosis.** Investigate and report; land fixes only when directed.
6. **One step at a time. Report after each. Never batch.** Do not proceed past a failure — escalate.
7. **Never close a defect on a configuration change without running its proof condition.**
8. **Encode fixes in the repo, not in directives.** If the next agent will hit the same wall, the fix
   goes in a committed file. That is why this card is rewritten rather than annotated.
9. **Never add a document.** Update this card. A companion report, a status file, a summary doc, a
   second runbook — each is a future reconciliation cost and a future contradiction. **Content walks
   backwards, never forwards.** This ban is on companion design docs, companion runbooks, and status
   or summary documents that drift from the card — never author those. It does NOT forbid delegate
   work reports: those ARE expected work product and belong in the normal reports location
   (`wevibe-meta/workspace/reports/`).
10. **For any purge: classify before deleting, delete before running, diagnose before repairing.**
    Map the irrelevant, map the needed, map the remainder, decide the remainder, delete en masse,
    then run, then diagnose case by case, then fix. Never interleave delete-versus-fix decisions
    item by item.

---

## 16. WORKER RELIABILITY — assume these failure modes

The agent runs on a local model. It executes; it does not reason well. Recorded confident wrong
claims: an attempt ceiling of 1 when it is 3 · "the worktree has no compiled artifacts" when they
were intact and already scored · "the failures are pre-existing" when half were its own regressions
· "under the shell limit" when it was over · a memory-pressure report that was a tool misread · a
defect declared resolved on a config change whose proof was never run.

**Every one of these is a claim of a negative or of a bound, asserted without evidence.**

**Work-order format that works** (prescriptive step lists do not — they break on the first wrong
assumption about repo structure and fill context fast):

> **OBJECTIVE** · **WHY** · **HARD INVARIANTS** · **ACCEPTANCE CRITERIA** · **explicit autonomy to
> iterate without asking** · **a short named escalation list**

**Never reference a file that exists only as a chat artifact** — the worker cannot see those.

**Never specify where anything is saved.** No paths, no directories, no filenames, no naming
conventions in a work order. Objective and acceptance criteria only; placement is the worker's.

---

## 17. TRAJECTORY

Ordered. Each step gates the next. One work order each.

**Done.** Census · verified baseline (`41c4568`) · suite finalization, 317 dead tests removed
(`937d893`) · source deletion, 19 files (`8bdcabc`) · run status contract, manifest + append-only
status stream + scorecard (`1a50ba9`) · entrypoints + unconditional reaper (`09cab437`) · mode-drift
removal + model-alias residue cleared (`98a286b` + `0141930` + `e60ccc1` + `8bdcabc` + `186d34c`) ·
baseline reconciled to `350f899` · template low-context proof PASSED (WO-TEMPLATE-PROOF-1).

1. **Document consolidation.** This card absorbs everything binding; the remaining documents are
   deleted and `AGENTS.md` is amended to stop contradicting §2.
2. **High-context template probe to ≥100K tokens** (§12): the low-context proof passed; the
   high-context behaviour this probe measures is still unverified. Only after it passes does the
   template get FREEZEd.
3. **Stack smoke** (§6) as the pre-campaign preflight; the **ON smoke** runs at its fixed point later — after the first OFF-cell extraction (post-wipe), before the first scored ON cell.
4. **FREEZE the template** (§12), only after the high-context probe passes.

> **Terminology:** §12/§17 "template" = the **agent reasoning template** (a different artifact from
> the task prompt/scaffold). The **task-template freeze** — the backgammon scaffold hash that the
> run path fails closed on — is recorded separately at RC-5a above.
5. **Wipe before the first cell** — the full four-step procedure (§2). Then first OFF cell (unscored)
   → extract → ON smoke (unscored, hard gate) → first scored ON cell → extract, continuing until
   performance drops or something needs Walter. A model switch is one of the things
   that needs Walter. The corpus carries across it; the wipe does not run again after the first cell.

**Do not skip the ON smoke.** The injection seams are null by contract on OFF cells, so no OFF cell can ever
prove them. Running ON without that smoke is exactly what voided the paid R2 campaign.