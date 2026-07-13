"""LIVE python recall smoke — de-risks the ON arm before the Phase-1 measurement.

Issues ONE real /v1/recall through WeVibeBackend against the running :4450 stack
using a PYTHON need-card (the seeded pool is python-only; the backgammon card in
recall_smoke.py won't match). Proves transport + identity-unlocked + HEADLESS
DECRYPTION end-to-end (INV-10 delivery gate).

Verifies: HTTP 200, n_memories > 0, any_decrypted_text=True.

Env overrides (optional):
  SMOKE_FLOOR   relevance_floor (default 0.3 — low, to guarantee matches)
  SMOKE_BUDGET  surface_budget  (default 5)
  SMOKE_HUB     hub url         (default http://localhost:4450)

Run:  .venv/bin/python scripts/recall_smoke_python.py
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
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
    logging.getLogger("wevibe_bench.backend").setLevel(logging.INFO)
    return logging.getLogger("recall_smoke_python")


def main() -> int:
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    log_dir = os.path.expanduser("~/Desktop/benchmark/runs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{ts}-recall-smoke-python.log")

    log = _setup_logging(log_path)
    log.info("=== LIVE python recall smoke START ts=%s log=%s", ts, log_path)

    floor = float(os.environ.get("SMOKE_FLOOR", "0.3"))
    budget = int(os.environ.get("SMOKE_BUDGET", "5"))
    hub = os.environ.get("SMOKE_HUB", "http://localhost:4450")

    cfg = RunConfig(mcp_recall_url=hub, tau=floor, surface_budget=budget)
    preflight(
        hub_url=cfg.hub_url,
        mcp_recall_url=cfg.mcp_recall_url,
        session_token_path=cfg.session_token_path,
        logger=log,
    )
    log.info("config manifest: %s", json.dumps(cfg.to_dict()))

    token_path = os.path.expanduser(cfg.session_token_path)
    token_present = os.path.isfile(token_path) and bool(open(token_path).read().strip())
    log.info("token seam path=%s present=%s (value NEVER logged)", token_path, token_present)

    # PYTHON need-card matching the seeded pool (bowling scoring w/ strike & spare bonuses).
    # INV-6: dense channel = intent + task ONLY; language/stack ride the keyword channel.
    need = NeedCard(
        intent="implement",
        task="bowling game score calculation with strike and spare bonuses",
        language="python",
        stack=["python", "bowling"],
    )
    log.info(
        "needcard dense_digest=%r keyword_channel=%s",
        need.prompt_digest,
        json.dumps({"language": need.language, "stack": need.stack}),
    )

    session_id = f"smoke-py-{ts}"
    wire = need.to_wire(cfg, session_id)
    log.info(
        "request wire url=%s/v1/recall org_id=%s fields=%s relevance_floor=%s surface_budget=%s query_len=%d task_len=%d",
        cfg.mcp_recall_url, wire.get("org_id"), sorted(wire.keys()),
        wire.get("relevance_floor"), wire.get("surface_budget"),
        len(str(wire.get("query", ""))), len(str(wire.get("task", ""))),
    )

    backend = WeVibeBackend(cfg)
    backend.prime_session(session_id)

    log.info(">>> issuing ONE live python recall ...")
    result = backend.recall(need, cfg)
    verdict = backend.verify_delivery(result)

    log.info(
        "RESULT http_status=%s reachable=%s status=%s reason_code=%s n_memories=%d verdict=%s",
        result.http_status, result.reachable, result.status,
        result.reason_code, len(result.memories), verdict.value,
    )
    any_text = False
    for i, mem in enumerate(result.memories):
        has = mem.has_content()
        any_text = any_text or has
        preview = (mem.text[:100] + "...") if len(mem.text) > 100 else mem.text
        log.info(
            "  memory[%d] cid=%s score=%s vector=%s combined=%s keyword=%s text_len=%d has_content=%s preview=%r",
            i, mem.cid, mem.score, mem.vector_score, mem.combined_score,
            mem.keyword_score, len(mem.text), has, preview,
        )

    rc = (result.reason_code or "").lower()
    interp = []
    if not result.reachable:
        interp.append("TRANSPORT: endpoint UNREACHABLE (:4450 down or network).")
    elif result.http_status != 200:
        interp.append(f"HTTP {result.http_status} error reason_code={result.reason_code}.")
    else:
        interp.append("HTTP 200: wire accepted (auth + body + parse OK).")
    if any_text:
        interp.append("IDENTITY: UNLOCKED (real decrypted memory text delivered).")
    elif rc == "decrypt_failed":
        interp.append("IDENTITY: key mismatch (decrypt_failed) => key/identity issue. STOP + report.")
    elif "identity" in rc or rc in {"no_identity", "locked"}:
        interp.append("IDENTITY: LOCKED (:4450 stale/locked or no identity). STOP + report.")
    elif result.http_status == 200 and rc in {"no_memories", "no_keywords", "no_membership", ""}:
        interp.append(f"IDENTITY: no delivery but not an auth/identity failure (reason={rc or 'none'}).")
    for line in interp:
        log.info("INTERP: %s", line)

    log.info("=== python recall smoke END log=%s", log_path)
    print("SMOKE_RESULT_JSON " + json.dumps({
        "http_status": result.http_status,
        "reachable": result.reachable,
        "status": result.status,
        "reason_code": result.reason_code,
        "n_memories": len(result.memories),
        "verdict": verdict.value,
        "any_decrypted_text": any_text,
        "floor": floor,
        "budget": budget,
        "log_path": log_path,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
