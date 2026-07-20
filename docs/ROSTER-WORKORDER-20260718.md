# WeVibe Bench — OpenRouter Roster Work Order
> Provenance: external research handoff, adopted by Walter 20-07-26; ring-2.6-1t floor-anchor added by manager. Saved verbatim 20-07-26.
**Document type:** Implementation work order (for OpenCode)
**Parent document:** OpenRouter Benchmark Roster Research Handoff (commissioning brief)
**Status:** Roster decided by Walter. Stage 1 (metadata) complete, live-verified. Stages 2–5 (reliability window, transport probes, OFF spike, ON smoke) are the implementation work this order covers.
**Scope carried over unchanged from parent brief:** benchmark substrate, oracle design, validation sequence, task (backgammon build), attempt/feedback policy, Docker isolation contract, anti-cheat contract.

## 1. What changed since the parent brief
The parent brief's §10 exclusion table listed GLM 5.2 as excluded for cheating during a hardened-isolation validation run. **That finding is now understood to be an infra gap, not a property of the model or its providers** — the prior test environment allowed a cheat path that has since been closed by moving qualification/scoring fully into the Docker isolation contract (§2.5–2.6 of the parent brief) and validating benchmark results externally to the container. GLM 5.2 is reinstated and is the **primary roster pick**.
Per Walter's direction, this roster is selected on two axes only:
1. **Reliability** — provider redundancy / uptime signal, tool-use and structured-output support, versioning stability.
2. **Benchmark scores** — standardized third-party capability benchmarks (Artificial Analysis coding/agentic indices, design-arena ranks where AA data isn't published), used as a proxy for Goldilocks-bracket fit ahead of the real OFF-spike (Stage 4) that actually confirms bracket placement.
Anti-cheat status is explicitly **not** a roster-selection criterion for this list — it's an infra property already handled at the Docker/oracle layer, not a per-candidate qualification gate.

## 2. Final Roster
| Rank | Candidate | Role | Why |
|---|---|---|---|
| 1 | **`z-ai/glm-5.2`** | **PRIMARY** | Strongest reliability signal in the set — 29 distinct provider endpoints observed live. AA coding_index 68.8, mid-tier relative to frontier (Opus 4.8 74.3), a plausible Goldilocks fit pending Stage 4 confirmation. Prior exclusion was an infra finding, now resolved. |
| 2 | **`xiaomi/mimo-v2.5-pro`** | **PRIMARY** | Second-best reliability signal (6 independent providers). AA coding_index 60.2 / agentic_index 29.1 — clearly mid-tier, not ceiling. Newer entrant (released 2026-04-22), well below frontier pricing/capability, plausible Goldilocks candidate. |
| 3 | `xiaomi/mimo-v2.5` (base) | RESERVE | 7 providers (best redundancy in the set). Cheaper and faster than Pro; vendor-claimed "Pro-level agentic performance at roughly half cost" is unverified by independent benchmark — reserve until Stage 4 spike run, likely floor-adjacent. |
| 4 | `tencent/hy3` | RESERVE | Mid-tier design-arena signal, 295B MoE (21B active), cheap. Provider count/uptime not yet confirmed live — needs Stage 2 before promotion. |
| 5 | `moonshotai/kimi-k2.7-code` | RESERVE | Resolves the parent brief's generic `kimi-k2.x` seed to a live slug. Prior "unreliable" exclusion predates this exact coding-focused variant — needs a fresh Stage 2/3 read rather than inheriting the old finding. |

**Excluded — capability-based, unrelated to the reliability/cheat question, still valid under the reliability+benchmark-only standard:**
| Candidate | Reason |
|---|---|
| `minimax/minimax-m3` | AA coding_index 58.6, floor risk; also independently observed ~+380% token bloat from verbosity (235,844 ON vs 49,166 OFF tokens on the same task), which would corrupt the efficiency-delta metric regardless of capability band. |
| `anthropic/claude-opus-4.8` / strong Opus tier | AA coding_index 74.3 — ceiling risk, consistent with Walter's empirical ceiling observation on the Opus line. |
| `inclusionai/ring-2.6-1t` | AA coding_index 42.8 / agentic_index 18.9 — likely floor risk. Cheap enough to keep as a last-resort reserve if the primary+reserve set all disqualify at Stage 4, not ranked above. |

**Flagged, not scored into the roster:** `google/gemini-3.1-pro-preview` — this is the live slug for the parent brief's `gemini-3.1-pro` seed; that exact seed name does not exist on OpenRouter. Still preview-tagged as of this research, pricing in frontier range ($2.00/$12.00 per M) — plausible ceiling risk and a versioning/reproducibility risk given the preview tag. Not included in primary/reserve; available if the roster needs a sixth candidate.

## 3. Live-Verified Spec Sheet
All figures below were pulled live from `GET https://openrouter.ai/api/v1/models` and public OpenRouter model pages. Re-verify before spending against these — catalog data drifts daily per the parent brief's §8.3.
| Slug | Ctx | Max out | Price in/out (per M) | Tool-use | Structured output | Providers (redundancy signal) | AA coding_index | AA agentic_index | Release |
|---|---|---|---|---|---|---|---|---|---|
| `z-ai/glm-5.2` | 1,048,576 | — | Catalog raw: $0.42/$1.32. OpenRouter blended-avg page: $0.88/$2.78 across ~29 listings — **discrepancy, confirm exact figure per pinned provider at probe time** | Yes | Yes | 29 | 68.8 | — | — |
| `xiaomi/mimo-v2.5-pro` | 1,048,576 | 131,072 | $0.435/$0.87 | Yes | Yes | 6 | 60.2 | 29.1 | 2026-04-22 |
| `xiaomi/mimo-v2.5` | 1,048,576 | — | $0.105/$0.28 | Yes | Yes (unconfirmed — verify at Stage 3) | 7 | not independently benchmarked yet | — | 2026-04-22 |
| `tencent/hy3` | 262,144 | — | $0.14/$0.58 | Yes (assumed — confirm at Stage 1 re-check) | Yes (assumed — confirm) | not yet confirmed live | not captured | design-arena mid-tier (~rank 44–55) | — |
| `moonshotai/kimi-k2.7-code` | 262,144 | — | $0.72/$3.50 | Yes | Yes | not yet confirmed live | 60.8 | — | — |
| `minimax/minimax-m3` (excluded) | 1,048,576 (out capped 131,072) | 131,072 | $0.30/$1.20 | Yes | Yes | — | 58.6 | — | — |
| `anthropic/claude-opus-4.8` (excluded) | 1,000,000 | — | $5.00/$25.00 | Yes | Yes | — | 74.3 | — | — |
| `inclusionai/ring-2.6-1t` (excluded) | 262,144 | — | $0.075/$0.625 | assumed | assumed | not yet confirmed | 42.8 | 18.9 | — |
| `google/gemini-3.1-pro-preview` (flagged only) | 1,048,576 | — | $2.00/$12.00 | Yes (reasoning-mandatory) | assumed | not captured | not captured | — | still preview-tagged |

**Known gaps for OpenCode to close in Stage 2:** exact provider counts for Hy3, Kimi K2.7-code, and Ring-2.6-1T; 30-day success-rate percentages for every candidate (not obtainable from static catalog data — requires either an authenticated `/endpoints` call or the JS-rendered dashboard); p50/p90 latency for all five roster candidates.

## 4. Provider Pinning — Implementation Spec
Per the parent brief §8.4 (pin one provider endpoint per candidate during qualification, disable silent fallback), every probe call from Stage 2 onward must carry an explicit `provider` object. Do not rely on `:nitro`/`:floor` shortcuts for qualification runs — they don't guarantee `allow_fallbacks: false`.
Template:
```json
{
  "model": "<candidate-slug>",
  "messages": [ ... ],
  "provider": {
    "order": ["<exact-provider-slug-from-/providers-page>"],
    "only": ["<same-provider-slug>"],
    "allow_fallbacks": false,
    "require_parameters": true,
    "quantizations": ["<confirmed-quant-level>"]
  }
}
```
Requirements for OpenCode's probe harness:
- Pull the exact provider slug (not the display name) from each candidate's live `/providers` listing immediately before probing — do not hardcode from this document, since provider rosters change.
- Use the **full** slug (e.g. `deepinfra/turbo`, not `deepinfra`) when a provider offers multiple regional/precision variants, so the pin is reproducible.
- Log the resolved `provider` object actually used on every probe call (§11.4 observability requirement) — not just the model slug.
- `require_parameters: true` on every call that uses tool-use or structured-output — a provider silently dropping an unsupported parameter is a false-pass risk for Stage 3 transport checks.
- If GLM 5.2's provider count (29) makes first-choice selection non-obvious, prioritize whichever endpoint reports Normal uptime tier and the lowest observed latency in Stage 2 — record the selection reasoning in the snapshot JSON's `notes` field.

## 5. Stages Owed (OpenCode implementation scope)
This work order covers building and running Stages 2–5 of the funnel defined in the parent brief §6, against the 5-candidate roster in §2 above.
| Stage | Scope | Cost cap (from parent brief §11.2) |
|---|---|---|
| 2 — Provider reliability evidence | Per candidate: observed uptime window, routing tier (Normal/Degraded/Down), error taxonomy. Model reliability and provider reliability reported separately. | ≤ $10 total |
| 3 — Minimal real-transport probes | Tiny real calls per pinned provider: response shape, streaming integrity, tool/structured-output honoring. | ≤ $25 total; ≤ 8,000 tokens/candidate |
| 4 — OFF spike / headroom check | One cheap OFF-condition probe per candidate on the real task (or reduced proxy) to classify ceiling/floor/bracket. Terminate a candidate immediately on ceiling/floor result — no further spend. | ≤ $40 total; 1 probe/candidate |
| 5 — ON smoke | Only for Stage-4 survivors, only once Walter opens this gate. Verifies recall delivery reaches the model via real transport — not a lift claim. | ≤ $40 total; 1 smoke/candidate |
Global qualification cap: **≤ $115 total**, stop at cap hit. Stop rules, observability/resumability requirements (background execution, `PROGRESS` logging, checkpointing, `--resume` support, no swallowed errors, no logged secrets) all carry over unchanged from the parent brief §11.3–§11.4.
**Explicitly out of scope for this work order:** Stage 6 (Walter roster confirmation — this document IS that confirmation for the 5 listed candidates) and Stage 7 (full/scored/paired ladder run). No scored run happens from this work order. That remains gated on Walter's explicit go-ahead after reviewing Stage 2–5 evidence, per the parent brief's Final Boundary (§14).

## 6. Machine-readable snapshot — starter skeleton
OpenCode should populate this per the parent brief's §12.2 schema as Stage 2–5 evidence comes in. Skeleton below is pre-filled with everything already live-verified in this handoff; blanks are Stage 2+ work.
```json
{
  "captured_at": "2026-07-18T00:00:00Z",
  "catalog_source": "https://openrouter.ai/api/v1/models",
  "candidates": [
    {
      "slug": "z-ai/glm-5.2",
      "providers": [{ "provider_slug": null, "endpoint": null, "pinned_for_tests": false }],
      "context": 1048576,
      "max_out": null,
      "price_in": 0.00000042,
      "price_out": 0.00000132,
      "uptime_pct": null,
      "uptime_window": null,
      "routing_tier": null,
      "tool_use": true,
      "streaming": null,
      "latency_p50": null,
      "latency_p90": null,
      "off_spike": null,
      "on_smoke": null,
      "goldilocks_verdict": null,
      "recommend": "PRIMARY"
    },
    {
      "slug": "xiaomi/mimo-v2.5-pro",
      "providers": [{ "provider_slug": null, "endpoint": null, "pinned_for_tests": false }],
      "context": 1048576,
      "max_out": 131072,
      "price_in": 0.000000435,
      "price_out": 0.00000087,
      "uptime_pct": null,
      "uptime_window": null,
      "routing_tier": null,
      "tool_use": true,
      "streaming": null,
      "latency_p50": null,
      "latency_p90": null,
      "off_spike": null,
      "on_smoke": null,
      "goldilocks_verdict": null,
      "recommend": "PRIMARY"
    },
    { "slug": "xiaomi/mimo-v2.5", "recommend": "RESERVE" },
    { "slug": "tencent/hy3", "recommend": "RESERVE" },
    { "slug": "moonshotai/kimi-k2.7-code", "recommend": "RESERVE" }
  ]
}
```
(Reserve entries left minimal — expand to full schema as Stage 1 re-checks and Stage 2 data land.)

## 7. Open items carried into this work order
1. GLM 5.2 pricing discrepancy (catalog raw vs. blended-average page) needs resolution against the specific pinned provider before any cost accounting is trusted.
2. Provider counts for Hy3, Kimi K2.7-code, and Ring-2.6-1T are unconfirmed — first Stage 2 task.
3. No candidate in this roster has a confirmed numeric 30-day uptime percentage yet — all "reliability" ranking above is by provider-count redundancy as a proxy, not the real success-rate metric the parent brief's §7.4 table calls for. Closing this requires either an authenticated `/endpoints` call or dashboard access with the OpenRouter key — Walter-owned per R-CONFIG-SoT-IS-UX, not decided in this document.
4. `google/gemini-3.1-pro-preview` remains a live option if the roster needs expansion, but is not part of the primary/reserve set above.

## 8. Final boundary (unchanged from parent brief)
This work order authorizes Stages 2–5 only. **No full/scored/paired ladder run** happens as a result of this document. Walter's explicit confirmation is still required before Stage 7 executes, per the parent brief's non-negotiable boundaries.
