"""LIVE recall smoke driver for the wevibe-bench harness.

Drives ONE real /v1/recall through WeVibeBackend against the running :4450 stack,
using the live org (wevibe-org-0) and a backgammon NeedCard. Logs everything to a
timestamped logfile under <repo>/runs/ (or $WEVIBE_BENCH_RUNS_DIR) (R-31 observability). NEVER logs
secrets (token is loaded inside the backend from the seam path; only sizes/fields here).

Run:  .venv/bin/python scripts/recall_smoke.py
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
import pathlib
import sys

from wevibe_bench.backends.base import NeedCard
from wevibe_bench.backends.wevibe_backend import WeVibeBackend
from wevibe_bench.config import RunConfig
from wevibe_bench.preflight import preflight


def _setup_logging(log_path: str) -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    # ensure the backend's own logger propagates to root handlers
    logging.getLogger("wevibe_bench.backend").setLevel(logging.INFO)
    return logging.getLogger("recall_smoke")


def main() -> int:
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    log_dir = os.environ.get("WEVIBE_BENCH_RUNS_DIR") or str(pathlib.Path(__file__).resolve().parents[1] / "runs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{ts}-recall-smoke.log")

    log = _setup_logging(log_path)
    log.info("=== LIVE recall smoke START ts=%s log=%s", ts, log_path)

    # Point explicitly at localhost:4450 (task); other defaults already correct.
    cfg = RunConfig(mcp_recall_url="http://localhost:4450")
    log.info("config manifest: %s", json.dumps(cfg.to_dict()))

    # MANDATORY two-tier preflight: fail loud if the hub or mcp recall tier is down,
    # so we never mistake "mcp/clone down" for "hub down" (never build our own infra).
    log.info(
        ">>> preflight: verifying hub (:4440 /health) + mcp recall (%s /v1/health) ...",
        cfg.mcp_recall_url,
    )
    preflight(
        hub_url=cfg.hub_url,
        mcp_recall_url=cfg.mcp_recall_url,
        session_token_path=cfg.session_token_path,
        logger=log,
    )

    token_path = os.path.expanduser(cfg.session_token_path)
    token_present = os.path.isfile(token_path) and bool(open(token_path).read().strip())
    log.info("token seam path=%s present=%s (value NEVER logged)", token_path, token_present)

    # Backgammon NeedCard. INV-6: dense channel = intent + task ONLY.
    # language/stack ride the KEYWORD channel, never the dense digest.
    need = NeedCard(
        intent="debug",
        task="bar re-entry rule when a checker is on the bar in backgammon move validation",
        language="python",
        stack=["backgammon", "move-validation"],
    )
    log.info(
        "needcard dense_digest=%r keyword_channel=%s",
        need.prompt_digest,
        json.dumps({"language": need.language, "stack": need.stack}),
    )

    # Request wire summary (sizes/fields only, no secrets).
    session_id = f"smoke-{ts}"
    wire = need.to_wire(cfg, session_id)
    log.info(
        "request wire url=%s/v1/recall org_id=%s fields=%s relevance_floor=%s surface_budget=%s query_len=%d task_len=%d",
        cfg.mcp_recall_url,
        wire.get("org_id"),
        sorted(wire.keys()),
        wire.get("relevance_floor"),
        wire.get("surface_budget"),
        len(str(wire.get("query", ""))),
        len(str(wire.get("task", ""))),
    )

    backend = WeVibeBackend(cfg)
    backend.prime_session(session_id)

    log.info(">>> issuing ONE live recall ...")
    result = backend.recall(need, cfg)
    verdict = backend.verify_delivery(result)

    # Full response summary (decrypted text length only, never content dump of secrets;
    # memory text is decrypted PLAINTEXT corpus content, safe to preview a short slice).
    log.info(
        "RESULT http_status=%s reachable=%s status=%s reason_code=%s n_memories=%d verdict=%s",
        result.http_status,
        result.reachable,
        result.status,
        result.reason_code,
        len(result.memories),
        verdict.value,
    )
    any_text = False
    for i, mem in enumerate(result.memories):
        has = mem.has_content()
        any_text = any_text or has
        preview = (mem.text[:80] + "...") if len(mem.text) > 80 else mem.text
        log.info(
            "  memory[%d] cid=%s score=%s vector=%s combined=%s keyword=%s text_len=%d has_content=%s preview=%r",
            i, mem.cid, mem.score, mem.vector_score, mem.combined_score,
            mem.keyword_score, len(mem.text), has, preview,
        )

    # ----- Interpretation ladder (reported plainly) -----
    interp = []
    if not result.reachable:
        interp.append("TRANSPORT: endpoint UNREACHABLE (:4450 down or network).")
    elif result.http_status == 503 and (result.reason_code or "") == "chain_unavailable":
        interp.append("HTTP 503 chain_unavailable => KNOWN old-hub batch-cap bug (fix 0b4408d needs redeploy).")
    elif result.http_status != 200:
        interp.append(f"HTTP {result.http_status} error reason_code={result.reason_code}.")
    else:
        interp.append("HTTP 200: wire accepted (auth + body + parse OK).")

    # identity state inference
    rc = (result.reason_code or "").lower()
    if any_text:
        interp.append("IDENTITY: UNLOCKED (real decrypted memory text delivered).")
    elif rc in {"decrypt_failed"}:
        interp.append("IDENTITY: likely key mismatch (decrypt_failed) => key/identity issue.")
    elif "identity" in rc or rc in {"no_identity", "locked"}:
        interp.append("IDENTITY: LOCKED (:4450 stale/locked or no identity).")
    elif result.http_status == 200 and rc in {"no_memories", "no_keywords", "no_membership", ""}:
        interp.append(f"IDENTITY: no delivery but not an auth/identity failure (reason={rc or 'none'}).")

    for line in interp:
        log.info("INTERP: %s", line)

    log.info(
        "=== SUMMARY http=%s verdict=%s n=%d reason=%s wire_confirmed=%s ===",
        result.http_status, verdict.value, len(result.memories),
        result.reason_code or "none",
        result.reachable,  # reachable+parsed => wire confirmed at transport level
    )
    log.info("=== LIVE recall smoke END log=%s", log_path)

    # machine-readable tail for the caller
    print("SMOKE_RESULT_JSON " + json.dumps({
        "http_status": result.http_status,
        "reachable": result.reachable,
        "status": result.status,
        "reason_code": result.reason_code,
        "n_memories": len(result.memories),
        "verdict": verdict.value,
        "any_decrypted_text": any_text,
        "log_path": log_path,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
