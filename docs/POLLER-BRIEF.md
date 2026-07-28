## Live-session observability invariant (BINDING, Walter-locked 2026-07-27)

> "No benchmark run executes blind. Every run must expose its live worker session state (the opencode session DB or an equivalent event stream) at a known, timestamped path under the run directory, and a poller must observe it in realtime alongside the spend and stream signals. A run that cannot be observed live does not start."

This brief is the reusable handoff artifact for the R-31 `poller` subagent.

## Required inputs (fill per run)

| Input | Value for this run |
|---|---|
| `run_dir` | |
| Session DB path | `<run_dir>/session-db/opencode.db` (default) |
| `events.jsonl` path | |
| Proxy budget path (`budget.json`) | |
| Proxy log path | |
| Worker container name | |
| Coordinator PID / pidfile | |
| Run log path | |
| `window_s` | `900` (default) |
| Warmup grace | `180s` (default) |

Notes:
- Session DB mount contract is fixed: host `<run_dir>/session-db` bind-mounts to
  `/home/worker/.local/share/opencode` in the worker container; DB file is
  `<run_dir>/session-db/opencode.db`.
- Proxy budget/log paths may be absent; poller still runs and evaluates available signals.

## Poll loop (every 60–120s)

1. Run canonical verdict script each poll:
   - `uv run python scripts/session_db_poll.py --run-dir <run_dir> [--proxy-budget <path>] [--proxy-log <path>] --window-s <window_s>`
2. Capture/append the script's evidence line to the run's poller log.
3. Continue cadence while the run is active; do not improvise alternate hang heuristics.

The verdict source of truth is only `scripts/session_db_poll.py`.

## Verdict actions

- `VERDICT=ALIVE`
  - Continue polling. No kill.

- `VERDICT=UNKNOWN`
  - Means session DB absent/unreadable (often warmup).
  - If UNKNOWN persists beyond warmup grace, escalate to coordinator immediately:
    run is effectively blind and violates the invariant.
  - NEVER kill on UNKNOWN.

- `VERDICT=DEAD`
  - Log the evidence line first.
  - Hung-process kill is authorized, process-scoped only:
    - `docker exec <container> pkill -9 -f '[o]pencode'`
  - NEVER `docker rm -f` mid-attempt (harness-limit kill-scope canon).
  - Report action + evidence to coordinator.

Budget thresholds are watch/report signals only; crossings are REPORT-to-coordinator events,
never auto-kill decisions (`D-BENCH-BUDGET-WATCH`).

## SMOKE-3 false-kill walkthrough (why this is binding)

At `18:50:43`, logs were `10m20s` silent, but `budget.json` `outstanding` still held ordinal `6`.
Canonical verdict therefore is `VERDICT=ALIVE` (in-flight request), so the new contract would
NOT have killed. In the actual incident, that live turn settled `4m37s` after the false kill.

Reference: `wevibe-meta/workspace/reports/27-07-26-1204-smoke3-retry-kimik3-zerotool-reasoning-turn.md`.

## Scope note

The poller still watches spend + stream signals per the standing runbook. This brief canonizes
the session-activity leg and its kill authorization contract.
