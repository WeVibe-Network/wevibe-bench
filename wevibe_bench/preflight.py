"""Mandatory two-tier recall preflight for the wevibe-bench harness.

WHY THIS EXISTS: the bench talks to a TWO-TIER recall topology and agents have
repeatedly confused the tiers — concluding "the hub is down" (and drifting into
building their own hub) when in fact the MCP recall client or its Option-C clone
was down. This guard checks BOTH tiers with the CORRECT, DISTINCT health paths and
raises a loud, actionable, one-path error that names the exact remediation and
forbids improvising infrastructure.

  TIER 1  hub  = Docker container `wevibe-hub`, http://127.0.0.1:4440,
                 health GET /health  (public, no auth) -> 200 {"status":"ok","db":"connected",...}
  TIER 2  mcp  = wevibe-mcp recall client, http://127.0.0.1:4450 (default) or
                 http://127.0.0.1:4550 (Option-C bench clone),
                 health GET /v1/health (bearer-gated) -> 200 with token / 401 without

No silent operation (R-37): every check logs its outcome to the provided logger.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import socket
import urllib.error
import urllib.request

LOGGER = logging.getLogger("wevibe_bench.preflight")
_REPO = pathlib.Path(__file__).resolve().parents[1]

# The exact command to start the Option-C bench clone on :4550 (mirrors how
# wevibe_bench.lifecycle.orchestrator launches it: bench endpoints + env seed
# backend + leader identity seed + hub url + port). The identity seed MUST be the
# same bench leader seed the corpus was seeded with, or recall cannot decrypt.
CLONE_START_CMD = (
    f"cd {_REPO / 'scaffold' / 'wevibe-mcp-clone'} && "
    "WEVIBE_MCP_HTTP_PORT=4550 WEVIBE_HTTP_HOST=127.0.0.1 "
    "WEVIBE_BENCH_ENDPOINTS=1 WEVIBE_SEED_BACKEND=env "
    'WEVIBE_IDENTITY_SEED_HEX="$WEVIBE_BENCH_LEADER_SEED_HEX" '
    "WEVIBE_HUB_URL=http://127.0.0.1:4440 "
    "node dist/server.js"
)

# Appended to EVERY preflight failure so the reader can never mistake the tiers
# or drift into standing up their own infrastructure.
REMEDIATION = (
    "\n---------------------------------------------------------------------------\n"
    "TOPOLOGY (do not confuse the two tiers):\n"
    "  * The hub is the Docker container `wevibe-hub` at 127.0.0.1:4440 "
    "(health GET /health, public). It is the ONE hub and is normally ALREADY RUNNING.\n"
    "  * The recall client is the wevibe-mcp process/clone at 127.0.0.1:4450 "
    "(default) or :4550 (Option-C bench clone) (health GET /v1/health, bearer-gated).\n"
    "\nDO NOT build, compile, or start your own hub or mcp. They already exist.\n"
    "  * To bring the HUB up:   `make redeploy`  (run from wevibe-meta, Walter-run; "
    "the `backend-restart` error it prints is EXPECTED/harmless).\n"
    "  * To bring the CLONE (:4550) up:\n      " + CLONE_START_CMD + "\n"
    "\nIf you cannot bring it up, STOP and report — do NOT improvise infrastructure.\n"
    "---------------------------------------------------------------------------"
)


class PreflightError(RuntimeError):
    """Raised when a required recall tier is down/unhealthy. Message names the fix."""


def _http_get(url: str, token: str | None, timeout: float = 5.0) -> tuple[int, dict, bool]:
    """GET url; return (status, json_dict_or_empty, reachable). Never raises for network errors."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(url=url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            payload = response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except OSError:
            payload = b""
        return exc.code, _safe_json(payload), True
    except (urllib.error.URLError, OSError, socket.timeout):
        return 0, {}, False
    return status, _safe_json(payload), True


def _safe_json(payload: bytes) -> dict:
    if not payload:
        return {}
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_token(session_token_path: str | None) -> str | None:
    if not session_token_path:
        return None
    path = os.path.expanduser(session_token_path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        return None
    return token or None


def preflight(
    *,
    hub_url: str,
    mcp_recall_url: str | None,
    session_token_path: str | None = "~/.wevibe/mcp-session-token",
    logger: logging.Logger | None = None,
) -> None:
    """Verify the recall topology BEFORE any recall/seed op. Raise PreflightError (loud) on failure.

    Checks, with the CORRECT distinct paths:
      (a) hub  GET {hub_url}/health  == 200 and db==connected and status==ok
      (b) mcp  GET {mcp_recall_url}/v1/health reachable (200 w/ token, 401 w/o both => UP)
      (c) identity/unlock: mcp reachable but 401 => identity LOCKED / token missing

    Pass mcp_recall_url=None to skip the mcp check (e.g. seed_corpus, which brings up
    its own mcp processes and only needs the hub to be up first).
    """
    log = logger or LOGGER

    # --- Tier 1: the hub (Docker wevibe-hub, :4440, GET /health, no auth) ---
    hub_health = f"{hub_url.rstrip('/')}/health"
    status, body, reachable = _http_get(hub_health, token=None)
    log.info(
        "preflight.hub url=%s http_status=%s reachable=%s db=%s status=%s",
        hub_health,
        status,
        reachable,
        body.get("db"),
        body.get("status"),
    )
    if not reachable:
        raise PreflightError(
            f"PREFLIGHT FAILED: the HUB (Docker container `wevibe-hub`, {hub_url}) is DOWN "
            f"(connection refused at GET {hub_health}). This is the HUB tier, NOT the mcp. "
            f"Bring the hub up with `make redeploy` (wevibe-meta, Walter-run). "
            f"DO NOT build or start your own hub." + REMEDIATION
        )
    if status != 200 or body.get("status") != "ok" or body.get("db") != "connected":
        raise PreflightError(
            f"PREFLIGHT FAILED: the HUB ({hub_url}) is UNHEALTHY at GET {hub_health} "
            f"(http_status={status}, status={body.get('status')!r}, db={body.get('db')!r}; "
            f"expected 200 / status=ok / db=connected). This is the HUB tier, NOT the mcp. "
            f"Bring the hub up with `make redeploy`." + REMEDIATION
        )
    log.info("preflight.hub OK (%s healthy, db=connected)", hub_url)

    # --- Tier 2: the mcp recall client (:4450/:4550, GET /v1/health, bearer-gated) ---
    if mcp_recall_url is None:
        log.info("preflight.mcp SKIPPED (mcp_recall_url=None; caller brings up its own mcp)")
        return

    is_clone = ":4550" in mcp_recall_url
    tier_name = "Option-C bench clone (:4550)" if is_clone else "wevibe-mcp recall client"
    mcp_health = f"{mcp_recall_url.rstrip('/')}/v1/health"
    token = _read_token(session_token_path)
    status, body, reachable = _http_get(mcp_health, token=token)
    log.info(
        "preflight.mcp url=%s http_status=%s reachable=%s token_present=%s",
        mcp_health,
        status,
        reachable,
        token is not None,
    )

    if not reachable:
        clone_hint = (
            f"If this is :4550, the {tier_name} is NOT running; start it with the command below."
            if is_clone
            else f"The {tier_name} at {mcp_recall_url} is not listening; start it (see below)."
        )
        raise PreflightError(
            f"PREFLIGHT FAILED: the MCP RECALL CLIENT at {mcp_recall_url} is DOWN "
            f"(connection refused at GET {mcp_health}). The HUB is a SEPARATE service and is "
            f"NOT the problem — this is the mcp/clone tier. {clone_hint} "
            f"DO NOT build or start your own hub or mcp beyond the documented command." + REMEDIATION
        )
    if status == 401:
        raise PreflightError(
            f"PREFLIGHT FAILED: the mcp recall client at {mcp_recall_url} is UP but IDENTITY is "
            f"LOCKED / the session token is missing or invalid (GET {mcp_health} -> 401). This is "
            f"NOT a hub problem and NOT a reason to start a new mcp. Provide a valid session token "
            f"at {session_token_path} (unlock the mcp identity)." + REMEDIATION
        )
    if status != 200:
        raise PreflightError(
            f"PREFLIGHT FAILED: the mcp recall client at {mcp_recall_url} returned an unexpected "
            f"GET {mcp_health} -> http_status={status}. Expected 200 (token) or 401 (no token). "
            f"This is the mcp/clone tier, NOT the hub." + REMEDIATION
        )
    log.info("preflight.mcp OK (%s reachable and healthy)", mcp_recall_url)
