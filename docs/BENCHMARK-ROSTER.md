# WeVibe Benchmark — Model Roster (CONDITIONAL, historical pre-21-07-26)

**Status (superseded 21-07-26):** the BLOCKED-BY-PASSABILITY state below is historical only. Walter GO authorized
the Stage-7 scored roster as executed: Opus SOURCE (OFF, self-extract) → kimi OFF/ON → big-pickle OFF/ON (no mimo rung).
Keep this file as pre-GO context; active run/recovery state lives in `docs/BENCHMARK-STATE.md`.

**Current roster pointer (2026-07-30):** operative R3 roster is `kimi/kimi-k2.7-code` (mid) + `tencent/hy3` (weak), per `docs/PREREG-R3-2026-07-30.md`; the GLM/MiMo candidate sections below are historical.

**Snapshot caveat (READ FIRST):** every pricing, availability, provider-slug, uptime, parameter-support,
quantization, output-cap, and TTFT claim below is a **July-15-2026 snapshot** and **MUST be
authenticated / live-re-verified before any spend.** Do not treat any slug or pin as a verified current
endpoint — resolve against the live OpenRouter API first.

**⚙ LIVE-VERIFY RESULT (15-07-26, reports `…-1519-…-live-passability.md` + `…-2019-…-pins-docker-leg.md`):**
Real-transport passability through the host-side proxy is **PROVEN** at every layer (direct HTTP AND the
Docker worker→host-proxy leg), key auto-sourced from OpenCode `auth.json`, provider objects injected, budget
accrued. **Walter's pin forks are RESOLVED and live-verified — all three roster members are TRANSPORT+ELIGIBILITY GREEN:**
- **GLM `z-ai/glm-5.2` → `novita`, `quantizations:["fp8"]`** (the defective `fireworks`+fp8 pin was REMOVED — Fireworks has no fp8 endpoint). Live: HTTP 200 via **Novita**, real content, ~$0.00014.
- **MiMo `xiaomi/mimo-v2.5-pro` → `deepinfra`** (quant omitted). Live: HTTP 200 via **DeepInfra**, ~$0.00025.
- **Opus `anthropic/claude-opus-4.8` → first-party `anthropic`**. Live: HTTP 200 via **Anthropic**, real content, ~$0.00018.
Total real spend across all live smokes ≈ **$0.0007** (well under the $12 hard aggregate ceiling). Pricing is
runtime-sourced at launch (never baked); the provider pins are now the SoT in `wevibe_bench/adapters/openrouter_proxy.py::DEFAULT_PROFILES`.
**Docker-leg note:** opencode-in-docker reached the host proxy and completed a real Novita call, but the
`opencode run` process did not self-terminate within the 180s bound (a session-lifecycle behavior INDEPENDENT of the
proxy — the proxy handled every call correctly; it correctly 400-rejected opencode's auxiliary `google/gemini-3.1-flash-image`
call under the single-model pin). Full scored cells must configure opencode to use ONLY the pinned model (or the driver
must tolerate the aux 400, which it did — the main call succeeded). This is a benchmark-cell config item, NOT a transport defect.
**Superseded 21-07-26:** this former scored-run block is historical; Stage-7 scored execution was explicitly authorized
and started (see `docs/BENCHMARK-STATE.md` Stage-7 crash recovery subsection).


**Scope:** benchmark roster / measurement integrity ONLY. This does NOT alter locked network-wide memory
design or any locked DECISIONS. It sits alongside `BENCHMARK-STATE.md`, `RUNBOOK.md`,
`ORACLE-ISOLATION-DIRECTIVE.md`, `DOCKER-SANDBOX-PROPOSAL.md`.

---

## 1. Historical passability block (superseded 21-07-26)

Superseded pointer: this section records the pre-GO block rationale only; it is not the current run gate.

Walter's roster is **CONDITIONAL**: it is the primary progressive-learning roster **iff** the Opus-4.8
passability smoke works. It does not.

- **Verified smoke result = FAIL.** One paid Opus-4.8 HIGH, recall-OFF, clean-corpus cell ($11.8035 ≤ $12
  hard ceiling) verdict FAIL: conformed + cheat-clean, but 4 gates STUCK across all 3 attempts under
  problems-only feedback (2× G12 doubling-cube-AI, F10, F14). HIGH effort proven; corpus stayed 0.
- Because the smoke FAILs, **no scored/full/paired candidate run is authorized.** The full/scored ladder was
  already FORBIDDEN until Walter explicitly confirms the roster; this block is additional, not a replacement.
- The independent diagnosis of *why* the smoke is not (yet) a defensible passability instrument is recorded in
  R-08 report `15-07-26-1039-benchmark-roster-conditional-passability-block.md` §Diagnosis. In short: N=1 does
  NOT prove Opus incapable, but the present no-leak contract/oracle is not a defensible passability instrument
  (hidden-rule gates + problems-only feedback give no blind traction), so the roster cannot be unblocked on the
  current instrument.
- **Option-A instrument repair is now complete, but does not unblock by itself.** The passability instrument was
  repaired into a requirements-to-implementation benchmark (report
  `15-07-26-1057-benchmark-optionA-repair-LEDGER.md`), with full requirement↔gate mapping in
  `docs/CONTRACT-TRACEABILITY.md`.
- **Historical unblocking requirement (superseded 21-07-26):** a fresh clean Opus-4.8 HIGH recall-OFF Docker
  passability smoke PASS was previously required before scored/candidate roster runs.
- **Historical separate block (superseded 21-07-26):** provider-routing enforcement existed but candidate authorization
  remained blocked in this pre-GO state.

---

## 2. PRIMARY-CANDIDATE roster (proposed — NOT authorized)

Two models. Per-request OpenRouter `provider` object is now the **implemented routing
control** via the host-side proxy (see §3).

### 2.1 GLM 5.2 — `z-ai/glm-5.2`
- **Provider pin (Walter-approved 15-07-26, live-verified):** `novita`.
- **Routing policy (per-request `provider` object):**
  - `only` / `order` = `novita` (pin the single endpoint),
  - `allow_fallbacks: false` (no silent route swap during a run),
  - `require_parameters: true`,
  - `quantizations: ["fp8"]`.
- **⚠ The prior `fireworks` pin was REMOVED (defective):** live OpenRouter shows Fireworks serves
  `z-ai/glm-5.2` only at `quant=unknown` (no fp8 endpoint), so `fireworks`+`fp8` matched zero endpoints
  (`404 No endpoints found … quantization: fp8`). Novita serves fp8 (px $0.9786/$3.0756/Mtok, uptime 99.0%) and is live-verified (HTTP 200).
- **Enforcement path (implemented):** the OpenRouter proxy hard-injects this exact
  `provider` object on every request; worker-side `provider` overrides are rejected.
- **Research-note reserves (NOT run-time fallbacks):** `siliconflow`, `baidu`, `streamlake` (other fp8 endpoints).
  These are research reserves only — they must NOT be used as fallbacks during a run (fallbacks are disabled by policy).


### 2.2 MiMo-V2.5-Pro — `xiaomi/mimo-v2.5-pro`
- **Recommended provider pin:** `deepinfra`.
- **Routing policy (per-request `provider` object):** same no-fallback / require-parameters policy
  (`allow_fallbacks: false`, `require_parameters: true`, single-endpoint pin).
- **Quantization:** **OMITTED pending live verification** (do not assume a quant tier until re-verified live).
- **Enforcement path (implemented):** the OpenRouter proxy hard-injects this profile's
  `provider` object on every request; worker-side `provider` overrides are rejected.
- **Research-note reserve (NOT run-time fallback):** `novita` — research reserve only, not a runtime fallback.

> **Provider-control mechanism (important):** the per-request OpenRouter
> **provider object** (hard-injected by the host-side proxy) is the benchmark
> pinning control. The **account ignore-list is NOT per-key and is NOT the
> benchmark pinning mechanism** — do not rely on it to pin endpoints for a scored run.

---

## 3. Hard prerequisites before ANY scored run (historical pre-GO snapshot)

1. **Provider-routing enforcement mechanism = CLOSED-BY-PROXY.** The host-side
   OpenRouter proxy now hard-injects per-request provider policy, clamps
   `max_tokens`, and persists cumulative spend via checkpoint ledger.
   This closes the specific "send/enforce/persist provider routing" capability gap.
2. **Passability gate still BLOCKING:** repaired-instrument Opus-4.8 HIGH
   recall-OFF Docker smoke must PASS under the hard $12 cumulative ceiling (§1).
3. **Opus provider-pin gate still BLOCKING:** Walter has not yet supplied an
   explicit OpenRouter provider pin for Opus; no Opus paid smoke/scored path is
   authorized until that pin is supplied and live-verified.
4. **Live re-verification before spend** of July-15-2026 snapshot claims,
   including GLM/MiMo provider availability + pricing.
5. **Explicit run authorization by Walter** — full/scored runs remain FORBIDDEN
   until explicit confirmation (`BENCHMARK-STATE.md`, `RUNBOOK.md`,
   `DOCKER-SANDBOX-PROPOSAL.md`, `BENCHMARK-DIRECTIVES.md`).

---

## 4. Anti-cheat

The existing full anti-cheat stack (oracle isolation / Docker sandbox / cheat-gate — see
`ORACLE-ISOLATION-DIRECTIVE.md`, `DOCKER-SANDBOX-PROPOSAL.md`) **applies from cell 1** for this roster.

---

## 5. Later-confirmation items (NOT settled by Walter's supplied text — do NOT invent)

- **Run counts** per candidate — not specified. Confirm with Walter before any run.
- **Ordering / ladder position** of GLM 5.2 vs MiMo-V2.5-Pro (peer pool vs step-down) — not specified.
- **MiMo-V2.5-Pro quantization** — omitted pending live verification.
- **Corpus/state:** current corpus = **0** (`org-0` wiped); it must remain 0 until an authorized run.

---

## 6. Reconciliation flags (surfaced, not decided — R-36)

- **Provider-routing status changed:** the prior "harness provider-routing
  UNPROVEN" gap is now closed by the host-side proxy enforcement path. This is
  a mechanism closure only; it does NOT authorize roster runs by itself.

- **GLM 5.2 tension:** GLM 5.2 currently sits on the roster **EXCLUSIONS** list
  (`OPENROUTER-BENCHMARK-ROSTER-RESEARCH-HANDOFF.md §10`: "cheated with recall ON by reading oracle files;
  cheat-gate caught it"). Walter's supplied roster promotes it to a PRIMARY candidate. This is not necessarily
  a conflict — an exclusion can be overturned by Walter, and the anti-cheat stack now applies from cell 1 — but
  it is a **deliberate reversal to confirm**, not a silent one.
- **MiMo-V2.5-Pro is new:** it appears in NO prior benchmark doc. All its details here are Walter-supplied
  July-15-2026 snapshots awaiting live verification.
- **Relationship to prior rosters:** this conditional GLM-5.2 + MiMo-V2.5-Pro pairing is distinct from the
  earlier (also unconfirmed) `BENCHMARK-DIRECTIVES.md §B` rosters (Target-1 opus-4.6+glm-5.2; Target-3
  opus-4.8→gemini-3.1-pro→glm-5.2→minimax-m3). Which roster is canonical is a Walter decision.

---

*Authored 2026-07-15 from Walter's supplied roster + the verified passability diagnosis. Detail:
report `15-07-26-1039-benchmark-roster-conditional-passability-block.md`. Local/untracked; not committed.*
