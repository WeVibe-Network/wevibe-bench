# WeVibe Bench — Variance & Repetition Policy (scored ladder cells)

> **Blessed by Walter 21-07-26 (shape); operational triggers = manager-approved interpretation, vetoable.**
> Docs-only policy. Does NOT modify the scorecard schema or harness code.

## Policy

1. **Baseline: N=1 per scored ladder cell.** Every cell (task × condition × model rung) runs once.
2. **Borderline cells repeat to N=3.** If any trigger in §Triggers fires for a cell, that cell — and only
   that cell — is re-run to a total of N=3. The reported verdict is the **majority** (pass/fail, gate set) and
   the **median** (continuous metrics: tokens, turns, wall, cost). All 3 runs' raw artifacts are retained.
3. **Every scorecard/report DISCLOSES N per cell.** N is disclosed in the run report and in the qualification
   snapshot entry for that cell (report/snapshot prose — no schema change). A cell reported without an explicit
   N is non-conforming.

**Rationale:** repetition budget is spent exactly where uncertainty lives; results are honest about
single-run variance instead of pretending N=1 is statistics.

## Triggers — a cell is BORDERLINE iff ≥1 of these holds (deterministic, checkable from run artifacts)

Grounded in the cell schema (`*-backgammon-detail.json`: `attempts_to_green`, `conformed`,
`failed_gates`, `attempt_reports[].{verdict,failed_gates,n_problems}`; `*-scorecard.json`:
`cells[].{total_tokens,turns,wall_seconds}`) and the Stage-4 CEILING/BRACKET/FLOOR classification practice
(ROSTER-WORKORDER-20260718 §5 Stage 4; e.g. kimi-k2.7-code conformed 28/29 gates → BRACKET, report
20-07-26-1057).

- **T1 — Gate margin ≤ 1.** The cell's final attempt has `len(failed_gates) == 1` (a FAIL one gate short of
  PASS), **or** the cell PASSes only on its final permitted attempt (`attempts_to_green == max_attempts`,
  i.e. the last feedback round flipped it). Either way the verdict sits within one gate/round of the boundary.
- **T2 — Lift sign fragile.** For an ON/OFF pair on the same rung: the relative token delta
  `|ON.total_tokens − OFF.total_tokens| / OFF.total_tokens < 0.15`, **or** the pair's
  `attempts_to_green` are equal (lift direction then rests on token/turn deltas alone). A sign that
  flips within ±15% single-run noise is not a reportable sign at N=1. (0.15 = manager-set constant, vetoable.)
- **T3 — Instrument anomaly.** The cell's run log contains any of: HTTP `402` or `429`, a wall-clock
  kill/timeout, a nonzero worker exit that was retried, a checkpoint `--resume` mid-cell, or a provider
  fallback/re-route event — while the cell still produced a scored verdict. Anomalous instrumentation
  invalidates N=1 confidence regardless of the verdict.
- **T4 — Classification flip.** Mapping the cell's result to CEILING/BRACKET/FLOOR yields a different class
  than the rung's previously recorded classification, or the gates-green count lands exactly on a class
  boundary. A single run never re-classifies a rung on its own.

**Procedure:** triggers are evaluated once, immediately after the N=1 run, from the artifacts above — no
judgment calls, no re-litigating after the fact. If fired → 2 more runs, same pinned provider/config/seed
policy, then majority/median as §Policy 2. If the 3 runs disagree on class (T4), escalate to Walter; do not
average across classes.
