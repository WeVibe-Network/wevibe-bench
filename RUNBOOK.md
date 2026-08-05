# RUNBOOK.md — the operative run card
**Version:** 6 · **Authored:** 2026-08-05 · **Status:** OPERATIVE · **Supersedes:** v5 (2026-08-04)

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

## 1. THE CAMPAIGN

**Goal:** measure lift on whatever model is resident — does an accumulated WeVibe corpus make the
same model resolve more of the same problem set, in fewer attempts, on a later run.

**Claim being made (do not overstate):** the corpus was taught durable **solutions to shared
problems**. A compiled-solutions system. **NOT** a capability-lift claim. Held-out variants are
required before any "models get better" statement, internal or external.

| | |
|---|---|
| Subject model | **whatever is resident** — the bench never selects a model (RC-7) |
| Org | **one org for the entire campaign**, recorded in the manifest; chain assigned `wevibe-org-3`, leader fp `f534aa02` |
| Runtime | **oMLX.** The operator manages what is loaded. Identity is read from the API response and recorded in the manifest, so this row is documentation, never a selector |
| Transport | local relay proxy `:4545`, single `auto (Local LLM Proxy - oMLX)` alias resolving to the resident model |
| Harness | OpenCode in a Docker worker image + plugin. **The version is not asserted here** — the worker image fingerprint is measured at run time and recorded in the manifest (RC-5), and the stack smoke asserts it matches the commit under test (§6) |
| Task | the LOCKED backgammon prompt — unstructured, no requirements checklist |
| Oracle | deterministic: Playwright conformance + Vitest backend + Playwright chromium. **No LLM judge exists anywhere in scoring.** |

**Do not touch the frozen direct entry.** The proxy-bypass entry under `provider.lmstudio` serves
Walter's live interactive session so the proxy container can restart without cutting him off. Only
its `name` field may change.

---

## 2. THE SEQUENCE AND THE ENTRYPOINTS

**The campaign sequence, in full:**

> **test** (all green) → **smoke** (all green) → **wipe** (once) → **bench** → **extract** →
> **bench** → **extract** → continue

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

**SMOKE**
1. Start the docker stack.
2. Run the smoke.
3. Check the smoke output.

A **one-time preflight before the campaign**, not a per-cell step. It is re-run after a pipeline
change (rule 5.1) — never between cells of an unchanged pipeline. Contents: §6.

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

**The wipe runs exactly ONCE, at campaign start, before the first bench.** A mid-campaign wipe
destroys the accumulated corpus the ON arm exists to measure. It does not fail loudly — it silently
converts every subsequent ON cell into an OFF cell with extra steps, and the campaign reports no
lift.

**The one exception (rule 5.13):** only a **true regression or total benchmark failure** justifies
re-baselining. That is a deliberate, declared act — never a casual re-wipe, never a "let's try it".

**BENCH `MODE=on|off`** — one cell. See §3.

**EXTRACT** — a separate invocation after each bench, never folded inside the bench command. The
integrity gate and the smart-leader procedure are §9.

### Consequences — these follow from the sequence and are not separately negotiable

- **The first bench after the wipe is necessarily OFF.** The corpus is empty by construction.
- **One org for the whole campaign.** Any scheme assigning an org per arm or per model is stale and
  wrong: it breaks corpus accumulation, which is the only thing being measured.
- **Mode toggles exactly one thing** — whether injection runs before attempt 1 (RC-4).
- **Smoke and wipe are not preconditions inside `bench`.** The operator runs each stage.

**Not every entrypoint above exists yet.** This section is the contract they are built to. See §11.

---

## 3. THE CELL — one per invocation, always

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

**RC-5 · One run directory, one manifest, one status stream.** Every run writes a manifest — model
identity as reported by the API, mode, org, commit, **worker image fingerprint**, seed, template
hash — and an append-only status file. The watcher reads **only** the status file. The scorecard is
generated **only** from the manifest plus the status file. No other artifact is a source of truth.
The status stream carries, per attempt: served model identity as reported by the API; the progress
vector; token accounting with **injected-memory-block tokens counted separately from work tokens**;
the injection observability values; extraction-attempt observability; and the terminal outcome with
its reason.

**RC-6 · Teardown and reap are unconditional.** They run on success, on failure, on abort and on
operator interrupt. The reaper kills the run's process group, reaps orphaned Playwright/node
children, brings the compose project down, asserts no listener remains on the bench ports, and
**reports what it killed.** A silent reaper is not a reaper. The gate path spawns real
`node report.mjs` Playwright subprocesses at `backgammon.py:1789-1795`.

**RC-7 · The stack never selects a model.** Identity is read from the API response and recorded in
the manifest. No model name in any bench config. No identity gate. No roster. Procedure: run an OFF
cell → run an ON cell → **STOP and ask Walter to switch the model.**

A served-model change is **observed and recorded, never aborted on**. It appears in the status
stream per attempt (RC-5); reading it is the scorecard's job.

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
    configuration differing from its paired arm · any provider-side truncation.
11. **Never infer a pass from the absence of a violation flag.** A clean `invariant_violation:
    false` cannot distinguish "extraction never invoked" from "invoked and cut off by the gate."
12. **A safety mechanism firing is not automatically a pass.** Ask what evidence it destroyed.
13. **Wipe once, at campaign start. Never again** — full procedure and the single exception at §2.
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

**ON smoke** — run once per pipeline change; gates every ON cell. Asserts non-null on the actual
worker image, through the real transport: `injected_count`, `injected_block_chars`,
`injected_block_est_tokens`, `consumer_injected_count`.

These four are **null BY CONTRACT on OFF cells** (`memory_mode != "on"` ⇒ `None`). An OFF cell
therefore proves nothing about them, and a null there is not a defect. Running ON without this smoke
is exactly what voided the paid R2 campaign.

**The `missing_telemetry_seams` list is itself an instrument.** In R2 it named seven seams, four of
which had real values in the same record. A list that over-reports trains the operator to ignore it.

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

**Layer 3 — transcript hard gate.** After every cell, scan the worker's event log for any tool call
whose input references oracle paths or distinctive oracle filenames, including the gate runner.
**Any hit forces the verdict to CHEAT → INVALID/FAIL — never PASS, even if every gate passes.** Write
a loud marker and surface it in the scorecard.

**Feedback content limits.** Worker-facing feedback carries **only the failing gate's ID and human
title**, in the form `- [G02] pip count: FAILING`. **Forbidden in worker-facing feedback:** expected
values, observed output, file paths, stack traces, oracle snippets. The rich detail stays in
host-side logs and is stripped before the worker sees anything. A failure points the worker at a
**public requirement**, never at a hidden value.

**Maintainer rules — each of these has already been violated once:**
- Never re-add a skip-permissions flag to worker launches.
- Never include expected, observed, path or stack detail in worker-facing feedback.
- Never copy oracle assets into a worker worktree.
- Keep the transcript hard gate enabled. It is the guaranteed backstop when everything else is
  bypassed.

**Option-A invariant:** no gate may require a constant, formula, string, count or mechanism that is
not published in the worktree contract artifact. Publishing requirements is orthogonal to all three
layers above and weakens none of them (rule 5.7).

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
| **D-TEMPLATE-DESYNC** | 🔴 **top campaign risk** | Closed on a config change with **no test run**. The proof — a ≥100-turn tool-enabled session with zero parser desyncs and zero tool calls trapped inside a reasoning block — has never been executed. See §12. | every scored cell |
| **D-ENTRYPOINTS-MISSING** | 🔴 OPEN | Test, smoke, wipe, bench and extract are not a coherent set of entrypoints; the wipe target lives outside this repo. §2 is a contract, not a description. | §2 |
| **D-MODE-DRIFT** | 🔴 OPEN | 19 behaviour-changing branches on mode. Six are the legitimate injection call site; **13 are drift** — ten in scoring/metrics, two gates, one telemetry. Direct violation of RC-4: the arms are not currently comparable. | every scored comparison |
| **D-RUN-STATUS-MISSING** | 🔴 OPEN | No manifest and no append-only status stream (RC-5). §3 step 2 has nothing to read and the scorecard has no single source. | the contract itself |
| **D-NO-REAPER** | 🔴 OPEN | Nothing kills orphaned processes or asserts a clean host after a run (RC-6). Process-survival behaviour is **unknown**, not clean. | §2 TEST step 4, bench |
| **D-PRERUN-PAIRING** | 🟡 OPEN | The phase machine is one-cell-per-invocation and compliant, but an OFF-baseline prerun path loops all pending OFF cells in one invocation with three-way concurrency and pairs the arms. | §2, §3 |
| **D-MODEL-ALIAS-RESIDUE** | 🟡 OPEN | One paid-era registry alias survives because a budget-named test reaches it through the docker-mode config path; the profile aliases likewise remain reachable by flag. Model-selection residue under RC-7. Clear it with the mode-drift work, not before. | RC-7 |
| **D-ALIAS-RESIDUE** | 🟡 OPEN | The proxy still ships a poller alias plus bench aliases referenced by no bench code. | deletion hygiene |
| **D-DOC-DRIFT** | 🟡 OPEN | `AGENTS.md` is always-applied and still carries a stale **org-per-arm** scheme marked BINDING, contradicting §2, plus poller-era stanzas. It must be amended, not merely left behind. | RC-8 |
| **D-TRACE-SEMANTICS** | 🟡 OPEN | Per-consumer attribution survives only as a random trace nanoid with no role semantics. | log-based attribution |
| **D-PROXY-UNTESTED** | 🟡 OPEN | The proxy has no git remote and no tests while sitting on the critical path for every bench call. | campaign safety |
| **D-RECALL-EMPTY-KEYWORDS** | 🟡 OPEN | Vector-only serves with empty `matched_keywords` get 400 from the serve endpoint and are silently dropped. | ON-cell attribution |
| **D-STRAY-BENCH-KEY** | 🟡 OPEN — **Walter only** | A mis-configured clone once wrote a bench org master-key envelope into Walter's canonical key directory. It **may collide with his canonical org keys**. Not deleted, and **no agent may delete it** — Walter verifies and cleans deliberately. Bench now writes only to the bench keystores, so it will not recur. | nothing automated |
| **D-KV-PEAK-UNKNOWN** | 🟢 CURIOSITY | Peak resident footprint at full context is unknown. Not a threat given headroom. | nothing |

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
Addopts: `-n auto --dist=loadfile --timeout=60 --strict-markers --tb=short -ra -m "not slow"`,
`timeout_method = "thread"`.

**Targets:** `test` (full suite) · `test-fast` (skips slow) · `test-file FILE=…` · `test-name
NAME=…` · `test-slowest` (ten slowest from the last run) · `test-all` (everything, including slow).

**Markers:** `slow` — excluded by default. `serial` — must not run in parallel; **triggers
`--dist no`**.

**Same-file grouping.** `--dist=loadfile` keeps every test in a file on one worker, in file order.
Without it xdist scatters tests and breaks anything sharing a fixture, a port or a temp path. The
work-stealing alternative can steal mid-file, which is riskier for shared state.

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

**Verified baseline: 465 passed / 0 failed / 1 skipped / 0 errors / 474 collected** (8 slow
deselected), on commit `8bdcabc`. Any change must return to this or account for the difference.

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
   backwards, never forwards.**
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
(`937d893`) · source deletion, 19 files (`8bdcabc`).

1. **Document consolidation.** This card absorbs everything binding; the remaining documents are
   deleted and `AGENTS.md` is amended to stop contradicting §2.
2. **Run status contract** (RC-5): manifest + append-only status stream; scorecard generated from
   those alone. This is what the sensor reads, and where served-model identity per attempt lands.
3. **The entrypoints + an unconditional reaper** (§2, RC-6).
4. **Mode-drift removal** — the 13 branches outside the injection call site (RC-4), with the
   model-alias residue cleared alongside.
5. **Stack smoke,** then **ON smoke** (§6).
6. **The ≥100-turn template proof, then FREEZE** (§12).
7. **Wipe once** — the full four-step procedure (§2). Then bench OFF → extract → bench ON → extract,
   continuing until performance drops or something needs Walter. A model switch is one of the things
   that needs Walter. The corpus carries across it; the wipe does not run again.

**Do not skip 5.** The injection seams are null by contract on OFF cells, so no OFF cell can ever
prove them. Running ON without that smoke is exactly what voided the paid R2 campaign.