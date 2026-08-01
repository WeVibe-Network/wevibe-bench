# wevibe-bench Test Suite — Testing Guide

## ⚠️ WARNING

**NEVER pipe a test run through `tail`/`head`/`grep` alone.**
Shell pipe buffering kills output — you will lose results.
The Makefile handles all logging automatically.

### Log rotation (automatic)

- Each run writes a **timestamped** log: `runs/pytest-YYYYMMDDTHHMMSS.log`
- The **last 10 logs** are kept; older ones are pruned automatically
- `runs/pytest-last.log` is always a **copy of the newest** log
- To read the latest: `cat runs/pytest-last.log`
- To list all logs: `ls -lt runs/pytest-*.log`
- To read a specific run: `cat runs/pytest-YYYYMMDDTHHMMSS.log`

---

## Entry Points

| Target | Command | Description | Expected Duration |
|---|---|---|---|
| `test` | `make test` | Full suite (installs deps, runs all non-slow tests) | ~XXs |
| `test-fast` | `make test-fast` | Non-slow tests only (skips `@pytest.mark.slow`) | ~XXs |
| `test-file` | `make test-file FILE=tests/test_foo.py` | Run a single test file | varies |
| `test-name` | `make test-name NAME=substring` | Run tests matching a name substring | varies |
| `test-slowest` | `make test-slowest` | Show the 10 slowest tests from the last run | instant |
| `test-all` | `make test-all` | Run everything including slow tests (`-m ""`) | ~XXs |

> **Note:** All targets above write timestamped logs to `runs/pytest-*.log`
> (see Log rotation section). `pytest-last.log` always tracks the newest.

### Markers

- `@pytest.mark.slow` — marks a test as slow; excluded by default (`-m "not slow"`)
- `@pytest.mark.serial` — marks a test that must not run in parallel; triggers `--dist no`

---

## Timeout Procedure

**CRITICAL: The per-test timeout is 60 seconds — well below the 120s agent shell limit.**

The agent shell kills any process running longer than 120 seconds. If the pytest timeout were set to 120s or higher, a hung test would be killed by the shell before pytest could report a named timeout, producing NO OUTPUT and making the failure untraceable. The 60s timeout gives a 2× safety margin: pytest reports the exact test that hung with a clear timeout error, not a silent kill.

**Rule: NEVER raise the timeout back up.** If a test consistently needs more than 60s, it should be marked `@pytest.mark.slow` so it runs only with `make test-all`.

To resolve a timeout:

1. **Check** `runs/pytest-*.log` for the hung test name (search for `Timeout`).
2. **Run just that file:** `make test-file FILE=tests/<that_file>.py`
3. **If it is a flaky network test**, mark it `@pytest.mark.slow` so it is
   skipped by default and only runs with `make test-all`.
4. **NEVER raise the timeout** without understanding why the test was slow.

---

## Same-File Test Grouping

**pytest-xdist runs with `--dist=loadfile` by default.**

By default, xdist scatters individual tests across workers in arbitrary order. This breaks tests that share state:
- Shared fixtures (e.g., a database connection pool)
- Fixed ports (e.g., two tests both trying to bind port 5432)
- Temp directories (e.g., two tests writing to the same temp path)

`--dist=loadfile` guarantees that all tests within a single file run on the same worker, in file order. This isolates shared state to one worker and prevents cross-worker collisions. It also preserves test order within a file, which matters for tests that depend on setup/teardown sequences.

The alternative (`--dist=worksteal`) also groups by file but can steal work mid-file, which is riskier for shared state. `loadfile` is the safer choice.

---

## Gates Oracle

The gates oracle (tasks/backgammon/gates) is a **separate JS suite** run
via `report.mjs`. It is **NOT** a pytest suite.

---

## Hardcoded Filenames in Gates

⚠️ Gates tests contain hardcoded server filenames.  They **must** use
`resolveEntrypoint()` to locate the correct build artifact.  Do not assume
a fixed filename — the build pipeline may change it.

---

## Measured Durations

Fill in after running:

- Full suite (`test`): ~XXs
- Fast suite (`test-fast`): ~XXs
- All suite (`test-all`): ~XXs
