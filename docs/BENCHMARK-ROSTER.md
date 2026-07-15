# WeVibe Benchmark — Model Roster (CONDITIONAL)

**Status:** BLOCKED-BY-PASSABILITY — **NOT authorized to run.** This roster becomes the primary
progressive-learning roster **only if** the Opus-4.8 passability smoke works. The current verified smoke
result is **FAIL** (report `15-07-26-1019-opus48-high-passability-smoke.md`), so **candidate runs remain
forbidden** and this document is a captured proposal, not a Go.

**Snapshot caveat (READ FIRST):** every pricing, availability, provider-slug, uptime, parameter-support,
quantization, output-cap, and TTFT claim below is a **July-15-2026 snapshot** and **MUST be
authenticated / live-re-verified before any spend.** Do not treat any slug or pin as a verified current
endpoint — resolve against the live OpenRouter API first.

**Scope:** benchmark roster / measurement integrity ONLY. This does NOT alter locked network-wide memory
design or any locked DECISIONS. It sits alongside `BENCHMARK-STATE.md`, `RUNBOOK.md`,
`ORACLE-ISOLATION-DIRECTIVE.md`, `DOCKER-SANDBOX-PROPOSAL.md`.

---

## 1. The passability block (why this roster is not authorized)

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
- **Unblocking requirement remains:** a fresh clean Opus-4.8 HIGH recall-OFF Docker passability smoke must PASS on
  the repaired instrument under the hard $12 cumulative ceiling before any scored/candidate roster run.
- **Still separate + blocked:** harness provider-routing capability remains UNPROVEN; no candidate is authorized;
  corpus remains 0.

---

## 2. PRIMARY-CANDIDATE roster (proposed — NOT authorized)

Two models. Per-request OpenRouter `provider` object is the **intended routing control** (see §3).

### 2.1 GLM 5.2 — `z-ai/glm-5.2`
- **Recommended provider pin:** `fireworks`.
- **Routing policy (per-request `provider` object):**
  - `only` / `order` = `fireworks` (pin the single endpoint),
  - `allow_fallbacks: false` (no silent route swap during a run),
  - `require_parameters: true`,
  - `quantizations: ["fp8"]`.
- **Research-note reserves (NOT run-time fallbacks):** `novita`, `siliconflow`. These are research reserves
  only — they must NOT be used as fallbacks during a run (fallbacks are disabled by policy).

### 2.2 MiMo-V2.5-Pro — `xiaomi/mimo-v2.5-pro`
- **Recommended provider pin:** `deepinfra`.
- **Routing policy (per-request `provider` object):** same no-fallback / require-parameters policy
  (`allow_fallbacks: false`, `require_parameters: true`, single-endpoint pin).
- **Quantization:** **OMITTED pending live verification** (do not assume a quant tier until re-verified live).
- **Research-note reserve (NOT run-time fallback):** `novita` — research reserve only, not a runtime fallback.

> **Provider-control mechanism (important):** the per-request OpenRouter **provider object** is the intended
> pinning control. The **account ignore-list is NOT per-key and is NOT the benchmark pinning mechanism** — do
> not rely on it to pin endpoints for a scored run.

---

## 3. Hard prerequisites before ANY scored run

1. **Harness provider-routing capability is UNPROVEN.** The current harness does **not yet prove** it can
   **send, enforce, and persist** endpoint-level provider routing (the per-request `provider` object above).
   **No scored run may occur until that capability is implemented and verified.** (Documented as an open gap:
   OpenRouter's `provider` object is a real API capability + prescribed policy, but harness send/enforce/persist
   is not a built, verified feature.)
2. **Live re-verification before spend** of every July-15-2026 snapshot claim (§ snapshot caveat).
3. **Passability instrument fixed / Opus smoke passing** (§1) — Walter's drawing-board decision.
4. **Roster confirmation by Walter** — the full/scored run remains FORBIDDEN until explicit confirmation
   (`BENCHMARK-STATE.md`, `RUNBOOK.md`, `DOCKER-SANDBOX-PROPOSAL.md`, `BENCHMARK-DIRECTIVES.md`).

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
