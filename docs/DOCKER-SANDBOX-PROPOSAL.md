# Docker-Sandboxed Benchmark Workers (Oracle-Unreachable) — Architecture A

**Status:** APPROVED (Walter-locked 15-07-26) + IMPLEMENTED (Architecture A). Full/scored run still gated on Walter roster confirmation.

---

## 1. The problem (plain)
Once we stopped leaking the answers through feedback (Tier-1 fix), the coding models — when stuck on
the last 1–4 gates — went looking for the **answer key on disk**: the gate test files
(`tasks/backgammon/gates/**`) and the **golden reference solution** (`tasks/backgammon/golden/**`). In the
2-model validation, **3 of 4 cells cheated** (minimax-OFF: 45 hits, including reading the golden solution).

Our cheat **detector** caught all of them (good — cells were invalidated, not falsely passed). But the
**permission lockout only detected; it did not prevent**: path-pattern `deny` can't stop `bash`
indirection (base64/node/python) or a worker copying oracle files into its own worktree. So we cannot
produce clean scored PASS/FAIL numbers — a determined agent with filesystem access to the oracle will
always find it.

## 2. Why the current approach can't fully win
The oracle lives on the same filesystem the worker runs on, and the worker needs broad tool access
(read/bash) to build the game. You cannot both (a) give an agent a real dev environment and (b)
reliably forbid it from reading specific on-disk files by policy. **The only robust fix is to make the
oracle physically ABSENT from the worker's world** — not present-but-forbidden.

## 3. How mature benchmarks solve this (standard practice)
This is exactly the SWE-bench / terminal-bench pattern: the agent runs inside a **per-task container**
that contains ONLY the codebase it's allowed to touch; the **evaluation (tests / golden patch) is held
out of that container** and applied by the harness *after* the agent finishes, in a separate step the
agent never sees. Isolation is by **container filesystem boundary**, not by permission flags. We should
adopt the same shape.

## 4. Implemented architecture: run each worker in Docker; keep the oracle on the host
Architecture A is now live in `wevibe-bench`: workers run in per-cell containers and oracle files stay host-only.

```
HOST (trusted)                          CONTAINER (untrusted worker)
──────────────────────────────         ─────────────────────────────
tasks/backgammon/gates/**   ← NEVER mounted
tasks/backgammon/golden/**  ← NEVER mounted
:4550 clone MCP (recall+decrypt, keys) ◄── http://host.docker.internal:4550
:4440 hub, :11434 Ollama               ◄── (recall path only, ON cells)
                                         │
  worktree/  ──mounted read-write──►    /work  (ONLY the scaffold + the model's edits)
                                         opencode run --model … --dir /work  (--pure for OFF)
                                         game dev-server binds :8002 INSIDE the container
AFTER the worker exits:
  host copies /work out → runs report.mjs (gates) on the host, agent never present
```

**Key properties now in force:**
- **Oracle unreachable by workers** — `gates/` and `golden/` are physically absent from container FS.
- **Parallel-ready isolation** — per-container netns removes the prior :8002 collision.
- **Recall path preserved** — ON recalls go to host clone `host.docker.internal:4550`; decrypt/keys stay host-side.
- **Host scoring unchanged** — gates still run via host `report.mjs` against exported `/work`.
- **Cheat detector retained** — still enforced as defense-in-depth audit.

## 5. Implemented components (Architecture A)
1. **Worker image:** `wevibe-bench-worker:v1` from `docker/worker/Dockerfile` (pinned Node digest +
   `opencode-ai@1.18.1`, non-root runtime).
2. **Docker layer module:** `wevibe_bench/adapters/docker_worker.py` with `DockerCell` launching one container per
   cell using `--user <host-uid>`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only` + tmpfs,
   `--network wevibe-bench-net`, `--add-host host.docker.internal:host-gateway`, and ONLY the worktree bind-mounted
   at `/work`.
3. **Adapter cutover:** `wevibe_bench/adapters/backgammon.py` runs `docker exec … opencode run … --dir /work`
   (`--pure` for OFF), re-injects problems-only feedback into the same container/session, and performs deterministic
   teardown via `docker rm -f`.
4. **Host gates unchanged:** gate/oracle scoring stays host-side; oracle artifacts are never mounted to worker
   containers.
5. **Cheat detector unchanged:** retained and still enforced as defense-in-depth.

## 6. Network egress — honest residual
The container runs on a user bridge and reaches host services through `host.docker.internal` (`:4550` recall clone,
`:4440` hub). Host filesystem access remains constrained to mounted `/work` only. Portable per-domain egress
ALLOWLISTING is NOT implemented (would require an egress proxy we do not build), so the worker still has general
outbound internet — REQUIRED for OpenRouter provider access.

Live plugin recall from inside the container (ON-cell path) is wired, but its live validation is deferred to the
Walter-gated ON smoke (not yet run).

## 7. Validation sequence (locked)
Validation contract remains: **spike OFF cell → ON smoke → cutover → full ladder**.

Current state: OFF isolation spike and cutover are implemented; live ON smoke is intentionally deferred (Walter-gated);
full/scored ladder is blocked pending explicit Walter roster confirmation.

## 8. One-path rule (R-13)
Docker is the ONLY worker path. If Docker is unavailable, the adapter raises a clear error and stops (no silent
host-side fallback run).

## 9. Alternatives considered (for completeness)
- **Filesystem jail / chroot / macOS sandbox-exec instead of Docker:** lighter but fragile on macOS and
  doesn't give the clean per-worker network namespace (no parallelism win). Docker is the standard, portable
  choice and matches how SWE-bench-class benchmarks isolate.
- **Keep detection-only, discard cheated cells:** cheap but wastes cells and can't guarantee a full clean
  ladder; rejected for scored/publishable runs.
- **Move oracle out of reach on the SAME host (no container):** reduces but doesn't eliminate the vector (a
  broad-access agent can still traverse the host FS); container boundary is the real fix.
