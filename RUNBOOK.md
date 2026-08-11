# RUNBOOK.md — the operative run card
**Version:** 7 · **Authored:** 2026-08-05 · **Status:** OPERATIVE · **Supersedes:** v6 (2026-08-07) · **Amended:** 2026-08-10 (WO-MODEL-FLAG): §0/§2 `--model` subject selection + CLI syntax correction (main-parser flags precede the subcommand) · §1/RC-7 operator-selects-model amendment · §6 reasoning posture rewritten self-explanatory — frozen at MAX, with the defensible-claim reasoning · **Amended:** 2026-08-11 (WO-NO-SMOKE, Walter): **all smoke stages removed** — a smoke requires a full session + extraction + approval to prove recall, which can only be done by running a session in full, so a standalone smoke proves nothing and has never worked; delivery is verified **in-band** by the first OFF cell + extraction + the first ON cell's injection-seam values (§2, §6) · **Amended:** 2026-08-11 (WO-DATA-CENTRAL): new §7 `data/` telemetry sink + 7-day retention (`cleanup_data.py`) — a retention layer, never a source of truth

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
nc -z 127.0.0.1 4550   # leader clone MCP (identity f7733d6e)
nc -z 127.0.0.1 4451   # contributor clone MCP (identity 5292550d)
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
TS=$(date +%Y%m%dT%H%M%S) && nohup .venv/bin/python scripts/run_cumulative.py \
  --model qwen3.6-35b-a3b-bench run --until-review --mode off \
  < /dev/null > "runs/off-cell-$TS.log" 2>&1 & disown
#    `< /dev/null` is MANDATORY: without it zsh suspends the job the instant the
#    process touches stdin ("suspended (tty input)"), leaving a half-built
#    manifest and a live container behind. Set TS in the SAME command as the
#    launch — a separately-pasted $TS is empty in the next command.

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
| Harness | OpenCode in a Docker worker image + plugin. **The version is not asserted here** — the worker image fingerprint is measured at run time and recorded in the manifest (RC-5); the operator confirms it matches the commit under test before the campaign (§0 step 2) |
| Task | the LOCKED backgammon prompt — unstructured, no requirements checklist |
| Oracle | deterministic: Playwright conformance + Vitest backend + Playwright chromium. **No LLM judge exists anywhere in scoring.** |

**Do not touch the frozen direct entry.** The proxy-bypass entry under `provider.lmstudio` serves
Walter's live interactive session so the proxy container can restart without cutting him off. Only
its `name` field may change.

---

## 2. THE SEQUENCE AND THE ENTRYPOINTS

**The campaign sequence, in full:**

> **test** (all green) → **wipe** (once) → **OFF cell** → **extract** →
> **first scored ON cell** → **extract** → continue

Each stage is **its own invocation**. Nothing here is nested inside anything else. If a procedure
cannot be expressed as one of the stages below, it is not a procedure — it is drift, and it gets
deleted rather than documented.

**There are no smoke stages (WO-NO-SMOKE, Walter, 2026-08-11).** A smoke that proves the recall seam
requires a full session, an extraction, and an approval first — and that can only be produced by
running a session in full. A standalone smoke therefore proves nothing and has never worked. Delivery
verification is **in-band**: the first post-wipe OFF cell builds the corpus, its extraction commits
it, and the first ON cell's status stream either carries non-null injection seams
(`injected_count`, `injected_block_chars`, `injected_block_est_tokens`, `consumer_injected_count`) —
delivery proven on the built artifact through the real transport — or it does not, which is the
rule-18 walk-back class. The first ON cell IS the delivery verification.

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

**SMOKE — REMOVED (WO-NO-SMOKE, Walter, 2026-08-11).** Both former smoke stages (stack smoke, ON
smoke) are deleted from the campaign. The ON smoke purported to verify recall delivery before the
first scored ON cell, but verifying recall requires a full session, an extraction, and an approval —
obtainable only by running a session in full — so the smoke could never certify anything and has
never worked. Delivery verification is in-band (see the sequence above): the first ON cell's
injection-seam values are the proof, and null injection values on an ON cell remain a rule-18
walk-back class.

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

**A failed cell needs an ARCHIVE, not a wipe (2026-08-11).** When a cell dies (`harness_error`, void
instrument, an aborted launch), the manifest still pins that cell's `sequence_index`, and
`build_off_order` emits exactly one OFF slot per roster entry — so a one-model roster has no second
OFF slot to run. The correct move is the §0 step 3a archive, which costs nothing measurable:
`mv runs/cumulative runs/cumulative.<why>-<date>`. **This is NOT a wipe and does not touch the server
corpus**; the wipe rules above still govern that. Do not reach for a wipe because a cell failed —
check whether the corpus is actually non-empty first (`memory_standing`, `extracted_sessions`,
`pending_submissions` in `wevibe_hub`). A cell that died before extraction committed anything leaves
the corpus empty, so a wipe would destroy nothing AND still be barred.

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
  `WEVIBE_BENCH_LEADER_MCP_URL` (the run's existing source; live leader clone **:4550**, NOT :4450 —
  see §7 "Known failure: org bootstrap"). Hub :4440.
- The chain assigns fresh-mint org ids. When `--org` names an absent org on a fresh stack, the run
  proceeds with the chain-assigned id returned by creation, reported in the run log as
  `run_cumulative.org_ensured`.

**EXTRACT** — a separate invocation after each bench, never folded inside the bench command. The
integrity gate and the smart-leader procedure are §9.

### Consequences — these follow from the sequence and are not separately negotiable

- **The first bench after the wipe is necessarily OFF — and it is UNSCORED.** The corpus is empty
  by construction. This first OFF cell exists to build a non-empty corpus so the first ON cell
  (which follows its extraction) has something to recall.
- **One org for the whole campaign.** Any scheme assigning an org per arm or per model is stale and
  wrong: it breaks corpus accumulation, which is the only thing being measured. Walter's 2026-08-07
  reasoning, transcribed: someone must be responsible for a corpus, and the org focuses context for
  retrieval and extraction alike.
- **Mode toggles exactly one thing** — whether injection runs before attempt 1 (RC-4).
- **Wipe and extract are separate invocations, never nested inside `bench`.** The operator runs each
  stage.

**Not every entrypoint above exists yet.** This section is the contract they are built to. See §11.

---

## 3. THE CELL — one per invocation, always

**The first pass is chunked (WO-77, 2026-08-09; compaction loop 2026-08-10).** Attempt 1 is a
sequence of chunk prompts (`tasks/backgammon/prompts/chunk-01..06.md`), driven in order through
the one serve session. Per chunk: drive → marker scan → compaction → next chunk.

- **Marker scan is watermark-windowed.** The harness watermarks the message list before each chunk
  and scans only what THAT chunk produced for `CHUNK FINISHED` — the worker emits the marker and
  its `WEVIBE_DISCOVERY` block in either order, sometimes in separate assistant messages, so a
  tail-only check misdetects. A missing marker is a stall, not a verdict: the harness re-nudges
  with the chunking reminder **without limit** until the marker lands (WO-NUDGE-INF-1).
- **Inter-chunk compaction.** Chunks 1–5 instruct the worker to call its `self_compact` tool
  (baked plugin `plugins/self-compact.ts`, arm-on-idle → `session.summarize`, autocontinue always
  suppressed — the harness sends the next chunk itself). The harness watches for the compaction
  part (`WEVIBE_BENCH_COMPACT_GRACE_S` 15s / `WEVIBE_BENCH_COMPACT_TOTAL_S` 1800s) and fires a
  fail-open backstop summarize (`auto:false`) when the worker never armed. Compaction is an
  optimization, never a gate; `WEVIBE_BENCH_CHUNK_COMPACT=0` disables it. No compaction after
  chunk 6 — no chunk follows.
  This `self_compact` lives ONLY in the benchmark worker's own OpenCode instance (in-image config
  `/etc/xdg/opencode/opencode.json` + the vendored plugin above) — NOT the daily-driver
  `~/.config/opencode` stack (workspace `AGENTS.md` §0.1). "Compact after every chunk / at end of
  turn" is bench-harness pacing and must never be ported into daily-driver agents; the two instances
  share nothing but the binary name.
- **Nudges are UNBOUNDED and never void a run (WO-NUDGE-INF-1, Walter 2026-08-11).** Stalls, loops,
  oversized generations, and ignored chunking rules are NORMAL agentic behaviour under measurement —
  the price of benchmarking, not a fault that invalidates it. Every nudge path is uncapped: the
  transport recovery nudge (loop-guard + finalize-watchdog kills), the chunk-marker nudge, and the
  zero-tool resume. There is no budget env var and no exhaustion kill; `loop_guard_exhausted`,
  `stream_finalize_exhausted`, and `chunk_marker_missing` no longer exist. The only remaining
  zero-tool honest-fail is a turn with no resumable session id — an unaddressable transport dead
  end. **Consequence to plan for:** a permanently wedged relay is no longer self-terminating, so
  hang detection is the operator's / poller's job on the status stream, never a nudge cap.
- **Relay-killed turns are recovered, never scored.** A relay loop-guard kill (`guard_abort`) or
  stream-finalize-watchdog kill is metered (tokens burned — true burn is never hidden), then
  re-driven (anti-repetition nudge for a loop kill, resume nudge for a finalize kill). **Both**
  kinds of killed turn are subtracted from scoring turns and reported on `guard_aborted_turns` /
  `finalize_timeout_turns` — that exclusion is what keeps unbounded nudging from inflating the
  measurement: a phase nudged N times scores exactly what an un-nudged phase scores. The anomaly
  classification is **watermark-windowed** like the marker scan: a killed message's `info.error`
  persists in the transcript forever, so each drive classifies only the messages produced since the
  last classification point — a stale kill can never re-trip a recovered, completed drive
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
campaign is `08afc8011cde5b81e6e158def2bc040f42372bbc1e32e7ca125382c27031cdb1` (re-baselined
2026-08-10, WO-FEEDBACK-CONTRACT: CONTRACT.md moved into the scaffold so the published requirements
seed every worker worktree; supersedes `a68ff9cb…`, whose cells are walked back by the declared
re-baseline), computed as SHA-256
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

1. **R-BENCHMARK-INTEGRITY.** End-to-end delivery verification is **in-band** (WO-NO-SMOKE,
   2026-08-11): the first post-wipe ON cell's status stream must carry non-null injection seams
   (`injected_count`, `injected_block_chars`, `injected_block_est_tokens`,
   `consumer_injected_count`) — observed on the built artifact through the real transport. Null
   values there are the rule-18 walk-back class. After **any** pipeline change, the next ON cell
   re-proves delivery the same way before its result is counted.
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
    null on an ON cell (the §2/§6 in-band delivery-verification class) · a wipe after the first cell
    (rule 13, §2). A break confined to
    **one cell** is a **rerun** (§10 — a new disclosed run, never a merge). The distinction: anything
    breaking a pairing is a walk-back; anything breaking one cell is a rerun.

---

## 6. PREFLIGHT — checks

A run launched with an unproven seam is **VOID-INSTRUMENT by construction** and is never counted in
N, however clean its output looks. Proven means **observed emitting a real value, on the built
artifact, through the real transport.** Never compile-green. Never "it was dispatched." Never a code
reading. With the smoke stages removed (WO-NO-SMOKE, 2026-08-11), seam proof is delivered in-band:
the injection seams are proven by the first ON cell's status-stream values (rule 5.1), and the
progress-vector and extraction-observability seams are proven by the first post-wipe OFF cell's.

**Before any scored run, three checks:**

1. **Policy anchor.** The hub's own log must show `status=anchor_verified` for
   `policy_version=edge-policy-v1`. **`anchor_absent`, `anchor_mismatch` or `anchor_unreachable`
   means STOP** — do not run the bench. An anchor mismatch is fatal to the hub at startup, so a
   drifted policy file takes the stack with it.
2. **Both tiers healthy** on their correct paths (§7). Confusing the two is the single most common
   bench failure.
3. **Model load verified** — context length, parallelism, one real completion, no TTL (RC-7).

### Reasoning controls — verified 2026-08-10, oMLX 0.5.7 (live probes, Qwen3.6-35B-A3B)

What each knob ACTUALLY does on this stack, measured — not assumed:

| Knob | Where set | Verified behaviour |
|---|---|---|
| `max_tokens` | request (proxy clamps to alias `limits.output`) | The ONLY hard ceiling. Reasoning and visible content share this one budget; exhaustion = `finish_reason: length`, possibly severed tool-call JSON (the VOID-INSTRUMENT class of rule 5.10) |
| `max_reasoning_tokens` | bench alias `requestDefaults` (forced 8192; client value stripped) | **ACCEPTED but NOT enforced by oMLX 0.5.7.** Probe: `max_reasoning_tokens: 50` → full reasoning still flowed (284 completion tokens). Treat it as a PRESENCE marker only (proxy fills it, oMLX accepts it) — it is NOT an enforced clamp. Do not rely on it as one |
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

**The injection-seam delivery check (in-band).** The four injection values — `injected_count`,
`injected_block_chars`, `injected_block_est_tokens`, `consumer_injected_count` — are **null BY
CONTRACT on OFF cells** (`memory_mode != "on"` ⇒ `None`). An OFF cell therefore proves nothing about
them, and a null there is not a defect. They are proven on the **first ON cell** (rule 5.1): non-null
values on the actual worker image through the real transport = delivery proven; null = rule-18
walk-back. A pre-wipe ON cell proves a seam the wipe then destroys, so no ON cell runs pre-wipe.

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
| **Bench leader clone** | managed service — seed-derived identity `f7733d6e` | `127.0.0.1:4550` | `GET /v1/health` (401 = up) | bearer token |
| **Bench contributor clone** | managed service — seed-derived identity `5292550d` | `127.0.0.1:4451` | `GET /v1/health` (401 = up) | bearer token |

**BOTH bench clones are managed services started by `make redeploy`** (`bench-clone.sh start
leader|contributor`). The harness CONNECTS to them; it never spawns them — the cumulative run path
never calls `bring_up()`. `:4450` is the operator's daily-driver host MCP and is **never** part of
the bench identity path; pointing any bench component at it mints orgs under the operator's keychain
identity (§7 Cause B).

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

### Known failure signature — org bootstrap (TWO distinct causes, same symptom)

> **READ BOTH CAUSES BEFORE DEBUGGING.** A fresh-stack org-bootstrap failure has had two entirely
> separate root causes. Cause A was fixed 2026-08-07; Cause B was fixed 2026-08-11. They present
> almost identically, and the earlier text here — which said "do NOT re-investigate an identity
> mismatch … do not re-open this" — actively delayed the Cause-B diagnosis by a full day. That
> instruction was correct **only** for the narrow claim it disproved (see Cause A) and is NOT a
> general ban on identity investigation. Full incident record:
> `wevibe-meta/workspace/reports/1786461718-WO-ORG-BOOTSTRAP-IDENTITY.md`.

#### Cause A — leader-membership sequencing race (FIXED 2026-08-07, commit `e2b4562`)

**Symptom.** Fresh post-wipe first OFF cell fails `run_m1` bootstrap in ~8s with a hub HTTP 403 at
`seed_keywords`: `{"error":"not a member of this org"}`, arriving ~3ms after `create_org`.

**Root cause.** `seed_keywords` fired immediately after `create_org` with no confirmation that the
leader's `members.active=true` row existed. The route `POST /v1/orgs/{org}/keywords` is gated by
`RequireVerifiedMembership` (wevibe-server/wevibe-hub/internal/auth/middleware.go:38, 403 at line 62,
existence check at lines 55-62). The only membership poll ran for the *contributor*, after seeding.

**Narrow disproven hypothesis (still disproven).** That the clone's *keystore-derived* identity
differs from the seed-derived leader after a wipe. It does not: `WEVIBE_IDENTITY_SEED_HEX` is
injected on both launch paths (mcp_process.py:193; preflight.py:54), both from
`WEVIBE_BENCH_LEADER_SEED_HEX`, and the clone keystore is only a fallback when that env var is
absent. A wipe regenerating the keystore does NOT change the clone identity. **This disproves one
specific mechanism — it does NOT mean "identity can never be the problem" (see Cause B).**

**Fix.** `poll_leader_membership` between `create_org` and `seed_keywords` (orchestrator.py), raising
`RuntimeError("leader membership did not include org_id=...")` before seeding, plus regression tests.
A silent 403 became an early, diagnosable failure. **It fixed the symptom's timing, not any identity
source** — which is exactly why Cause B could still occur and surface through this same message.

#### Cause B — the org was minted under the WRONG MCP's identity (FIXED 2026-08-11)

**Symptom.** `RuntimeError: leader membership did not include org_id=wevibe-org-0` — the Cause-A
guard firing, ~30 s after `create_org` returned ok. Often preceded by an **unexpected Touch ID
prompt**.

**Root cause.** `lconfig.py` defaulted `leader_mcp_url` to `:4450` — the **real host wevibe-mcp**,
which has no seed support and always loads the operator's biometric keychain identity `05c4b8cb…`.
`create_org` hands that URL to leader-signer as `WEVIBE_MCP_URL`; `POST /v1/org-setup` stamps *that
MCP's* pubkey as the org leader; the hub writes it as the org's only `members` row. The harness then
polls for its own membership (`8d46fc08…`, pubkey fp `f7733d6e`) and never finds it.

**Why it hid for weeks.** Every earlier run took the `reuse` path (`phase=reuse`,
`tx_hash=reuse-existing`) and never called org-setup. The first true fresh-create after a genuine
wipe triggered it — and the Touch ID prompt appearing "randomly" was the same defect, not a separate
annoyance.

**Vocabulary trap that misled the earlier diagnosis:** `f534aa02` is `fp(seed_bytes)` and `f7733d6e`
is `fp(ed_pubkey_bytes)` — **the same leader identity, two different hashed inputs.** Seeing an
unfamiliar fingerprint does not by itself indicate a different identity. Always state which input a
fingerprint hashes.

**Fix.** Default is now `:4550` (the seed-derived bench leader clone), plus a fail-fast guard in
`create_org` that probes `GET /v1/identity/pubkeys` and refuses to register unless the org-setup
MCP's ed25519 key IS the harness leader's. Unreachable is also a hard failure — a run on an
unverified seam is VOID-INSTRUMENT (§6).

**Healthy bootstrap looks exactly like this:**

```
phase=org_setup_mcp_identity_verified mcp_url=http://127.0.0.1:4550 leader_ed_fp=f7733d6e status=ok
step=create_org status=ok
step=poll_leader_membership status=ok dur_ms=7
step=contributor_pubkeys status=ok dur_ms=2
```

#### Cause C — contributor MCP `:4451` not running (FIXED 2026-08-11)

**Symptom.** `step=contributor_pubkeys err=mcp unreachable for /v1/identity/pubkeys`.

**Root cause.** The cumulative run path **never calls `bring_up()`** — it only connects to MCPs it
assumes are running. Only `:4550` was a managed service; `:4451` was started by hand. The campaign
had been silently relying on an **Aug-7 orphan process** that survived every wipe and predated the
clone dist by days.

**Fix.** `bench-clone.sh` takes a role (`leader`|`contributor`), `make redeploy` starts both, and
verify-clean checks 11/12 assert BOTH clones' identity fingerprints (`f7733d6e` / `5292550d`).
`config/bench.env` exports `WEVIBE_IDENTITY_SEED_HEX` globally as the **leader** seed, so the
contributor start MUST override it per-role or `:4451` silently serves the leader's identity.

**Value.** The next benchmark start recognizes these signatures by name and proceeds to the known
fix instead of re-deriving it. **Liveness is not identity** — a process being up, a port answering,
and health returning 200 proved nothing in either Cause B or C.

### data/ — centralized telemetry sink and retention layer

**The problem it solves.** The plugin's observable recall surface (funnel snapshot + plugin error log)
is container-side under `~/.wevibe` and is destroyed at teardown today. OFF-arm cells strip the plugin
recall substrate entirely (no org marker, no MCP/hub env, no state mount), so the four diagnostic
questions (distinct failureKeys, repeats, registry survival across compactions, recall round-trip
latency) are unanswerable from OFF runs — and even ON-cell telemetry is lost when the container dies.

**Propagation contract (LIVE).** At cell end the harness exports the worktree's
`.wevibe/state/funnel-snapshot.json` + `.wevibe/logs/wevibe-plugin-errors.log` host-side into
`data/cells/<unix_ts>-<run_label>/` (`_export_cell_telemetry`, `wevibe_bench/adapters/backgammon.py`).
It runs for **BOTH arms** — an OFF cell is the baseline the ON arm is measured against, so exporting
only on injection-record cells would rebuild the blind spot this sink exists to close. Fail-open by
contract: a missing surface is a silent no-op and an unwritable sink is logged and swallowed, so
telemetry export can never fail a scored cell. Extraction jobs land under
`data/extract/<unix_ts>-<job_id>/` (extraction-side wiring still pending).

**`data/` is a TELEMETRY/RETENTION layer, never a source of truth.** RC-5's manifest + status stream
under `runs/` stay authoritative. `data/` never duplicates or competes with `runs/` content.

**Retention: exactly 7 days** on `data/cells/` and `data/extract/` entries; enforced by
`scripts/cleanup_data.py` (run fail-open at the start of each run via `_handle_run`;
`WEVIBE_BENCH_SKIP_CLEANUP=1` disables). It deletes only under `data/cells/` and `data/extract/`; it
never touches `runs/`. The 7-day window exceeds a full OFF+ON pair, so no scored cell's telemetry is
aged out mid-campaign.

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
confirmed to carry the cadence code** — confirmed by comparing the manifest's recorded worker image
fingerprint against an image built from the commit under test (§0 step 2).

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
| **D-ENTRYPOINTS-MISSING** | 🟢 CLOSED by 09cab437 | `feat: split extraction invocation, add run --mode, unconditional reaper` makes test/wipe/bench/extract a coherent entrypoint set (smokes later removed, WO-NO-SMOKE 2026-08-11). | §2 |
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
| **D-SERVE-MESSAGE-500** | 🟡 OPEN — intermittent, cell-voiding | `GET /session/{id}/message` on the worker serve intermittently returns **HTTP 500**, which the serve-drive path reports as `status=metrics_error` and meters as `turns=0`; the phase is then correctly declared `harness_error` rather than gating a half-written worktree. Root cause is inside opencode, not the harness: `EffectDrizzleQueryError: Failed query: select … from "part" where "message_id" in (?×36)` (worker `opencode.log`, 2026-08-11T21:46:57Z). Observed ONCE, on the 14:35 cell at the chunk-1→2 boundary; the 14:55 cell cleared the same boundary with `turns=32`, so it is **intermittent, not a structural chunk-2 defect**. A TUI attach hits the same endpoint and was in flight at the failure — **contribution neither established nor excluded**. Until characterised, prefer the dashboard over attaching to a live cell. | any cell, intermittently — voids the cell loudly, never silently |
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
3. **Preflight checks** (§6) before the campaign; delivery is then verified in-band by the first
   OFF cell + extraction + the first ON cell's injection seams (rule 5.1) — there are no smoke
   stages (WO-NO-SMOKE, 2026-08-11).
4. **FREEZE the template** (§12), only after the high-context probe passes.

> **Terminology:** §12/§17 "template" = the **agent reasoning template** (a different artifact from
> the task prompt/scaffold). The **task-template freeze** — the backgammon scaffold hash that the
> run path fails closed on — is recorded separately at RC-5a above.
5. **Wipe before the first cell** — the full four-step procedure (§2). Then first OFF cell (unscored)
   → extract → first scored ON cell → extract, continuing until
   performance drops or something needs Walter. A model switch is one of the things
   that needs Walter. The corpus carries across it; the wipe does not run again after the first cell.

**The first ON cell is the delivery verification.** The injection seams are null by contract on OFF
cells, so no OFF cell can ever prove them — and no standalone smoke can either (WO-NO-SMOKE): proving
recall requires a full session + extraction + approval, which only a real cell produces. Read the
first ON cell's status stream: non-null `injected_count` / `injected_block_chars` /
`injected_block_est_tokens` / `consumer_injected_count` = delivery proven; null = rule-18 walk-back.