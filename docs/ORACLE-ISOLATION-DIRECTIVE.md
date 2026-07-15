# Backgammon Benchmark Oracle Isolation Directive

## The invariant

**Benchmark workers must never read or access gate oracle/test sources, and worker feedback must be problems-only.**

## Why it exists

On 2026-07-14 we confirmed two measurement leaks that invalidate memory-lift results:

1. **Feedback target-leak:** failure feedback exposed expected values, observed mismatches, oracle file paths, and stack traces. Models could pattern-match the answer key instead of learning the rule.
2. **Oracle read-access:** workers launched with `--dangerously-skip-permissions` could read `tasks/backgammon/gates/` directly and hard-code outputs.

Memory-lift is only measurable when workers solve tasks from memory and reasoning, not leaked oracle targets.

## The three enforcement layers (belt-and-suspenders)

### 1) Physical isolation

- Oracle material is never copied into the worker worktree: `tasks/backgammon/gates/`, `tasks/backgammon/golden/`, `judge/`, `report.mjs`, and `*.test.ts`/`*.spec.ts` gate sources stay outside.
- Worker `--dir` is seeded only from `tasks/backgammon/scaffold/` (7 stub files).
- Gate runner (`report.mjs`) runs from `cwd=gates/` and reaches the worker output only via `BENCH_TARGET` env var.

### 2) Permission-deny

- Worker launches **without** `--dangerously-skip-permissions`.
- A worktree-local `opencode.json` grants autonomy inside the worktree (`allow`) and denies external reads with `external_directory: {"*":"deny"}` plus targeted `bash` deny patterns for oracle paths.
- Permissions are `allow`/`deny` only; never `ask`. Headless `opencode run` has no human to answer prompts; `ask` can hang forever (OpenCode #32388/#30635). `deny` fails fast and the model continues.
- Verified headless behavior: denied external read returns tool error and exits cleanly in ~7s (no hang, no leak).
- **Known residual hole:** `bash` can still exfiltrate external files via indirection (base64/node/python/etc.) that path-pattern denies cannot fully close.

### 3) Transcript hard-gate (guaranteed backstop)

- After every cell, scan worker `events.jsonl` for any tool call (`read`/`bash`/`glob`/`grep`/`list`/`edit`) whose input references oracle paths or distinctive oracle filenames (including `report.mjs`).
- Any hit forces verdict to `CHEAT` -> `INVALID/FAIL` (never PASS even if gate results pass).
- Write loud `CHEAT.json` marker and surface it in the scorecard.

## Feedback is problems-only

Worker reinjection includes only failing gate IDs + human titles, e.g.:

- `- [G02] pip count: FAILING`
- `- [G08] bear-off incl overshoot: FAILING`

Forbidden in worker-facing feedback: expected values, observed outputs, file paths, stack traces, oracle snippets.
Rich expected/observed detail remains in our internal run reports/logs (R-37) and is stripped only from worker prompts.

## What counts as a cheat / INVALID cell

Any oracle reference in worker tool-call inputs (path or oracle filename) is a cheat event. The cell is INVALID and must not be counted as a valid PASS under any circumstances.

## For maintainers

- Never re-add `--dangerously-skip-permissions` to worker launches.
- Never include expected/observed/path/stack detail in worker-facing failure feedback.
- Never copy oracle assets into worker worktrees.
- Keep transcript hard-gate enabled: it is the guaranteed backstop when other controls are bypassed.
