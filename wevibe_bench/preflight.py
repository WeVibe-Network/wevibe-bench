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
from typing import TYPE_CHECKING, Any, Callable

from wevibe_bench.lifecycle.logging_util import fp, new_trace_id
from wevibe_bench.lifecycle.signing import wevibe_signed_headers

if TYPE_CHECKING:
    from wevibe_bench.lifecycle.identity import Identity

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


def _http_get(
    url: str,
    token: str | None,
    timeout: float = 5.0,
    headers: dict[str, str] | None = None,
    parse_any_json: bool = False,
) -> tuple[int, Any, bool]:
    """GET url; return (status, json_dict_or_empty, reachable). Never raises for network errors."""
    req_headers = {"Accept": "application/json"}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    if headers:
        req_headers.update(headers)
    try:
        request = urllib.request.Request(url=url, headers=req_headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            payload = response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except OSError:
            payload = b""
        return exc.code, _safe_json(payload, parse_any_json=parse_any_json), True
    except (urllib.error.URLError, OSError, socket.timeout):
        return 0, {}, False
    return status, _safe_json(payload, parse_any_json=parse_any_json), True


def _safe_json(payload: bytes, *, parse_any_json: bool = False) -> Any:
    if not payload:
        return {}
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if parse_any_json:
        return parsed
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


def verify_org_checklist(
    *,
    hub_url: str,
    org_id: str,
    identity: "Identity",
    logger: logging.Logger | None = None,
    http_get: Callable[..., tuple[int, Any, bool]] = _http_get,
) -> None:
    """Verify org keyword/profile checklist required before benchmarking."""
    remediation = (
        " run scripts/bootstrap_org_m1.py "
        "(re-seeds org keywords + org profile; idempotent)"
    )
    trace_id = new_trace_id()
    org_fp = fp(org_id)

    keywords_url = f"{hub_url.rstrip('/')}/v1/orgs/{org_id}/keywords"
    signed_headers = wevibe_signed_headers(identity, trace_id)
    kw_status, kw_body, kw_reachable = http_get(
        keywords_url,
        token=None,
        headers=signed_headers,
        parse_any_json=True,
    )
    if not kw_reachable:
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=keywords outcome=fail trace_id=%s org_fp=%s reason=unreachable http_status=%s reachable=%s",
                trace_id,
                org_fp,
                kw_status,
                kw_reachable,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=keywords unreachable "
            f"url={keywords_url}; benchmarking never runs unless the pipeline checklist is proven;"
            + remediation
        )
    if kw_status != 200:
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=keywords outcome=fail trace_id=%s org_fp=%s reason=non_200 http_status=%s",
                trace_id,
                org_fp,
                kw_status,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=keywords returned non-200 "
            f"url={keywords_url} status={kw_status}; benchmarking never runs unless the pipeline checklist is proven;"
            + remediation
        )
    if not isinstance(kw_body, list):
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=keywords outcome=fail trace_id=%s org_fp=%s reason=non_list_body body_type=%s",
                trace_id,
                org_fp,
                type(kw_body).__name__,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=keywords response is not a JSON list "
            f"url={keywords_url}; benchmarking never runs unless the pipeline checklist is proven;"
            + remediation
        )
    has_active_keyword = any(
        isinstance(entry, dict) and entry.get("deprecated") is False
        for entry in kw_body
    )
    if not has_active_keyword:
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=keywords outcome=fail trace_id=%s org_fp=%s reason=no_non_deprecated_keywords",
                trace_id,
                org_fp,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=keywords keyword vocab is empty "
            "(no non-deprecated keywords); benchmarks must not run; "
            "benchmarking never runs unless the pipeline checklist is proven;"
            + remediation
        )
    if logger is not None:
        logger.info(
            "preflight.org_checklist check=keywords outcome=ok trace_id=%s org_fp=%s http_status=%s reachable=%s",
            trace_id,
            org_fp,
            kw_status,
            kw_reachable,
        )

    org_url = f"{hub_url.rstrip('/')}/v1/orgs/{org_id}"
    org_status, org_body, org_reachable = http_get(org_url, token=None, parse_any_json=True)
    if not org_reachable:
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=org_profile outcome=fail trace_id=%s org_fp=%s reason=unreachable http_status=%s reachable=%s",
                trace_id,
                org_fp,
                org_status,
                org_reachable,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=org_profile unreachable "
            f"url={org_url};" + remediation
        )
    if org_status != 200:
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=org_profile outcome=fail trace_id=%s org_fp=%s reason=non_200 http_status=%s",
                trace_id,
                org_fp,
                org_status,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=org_profile returned non-200 "
            f"url={org_url} status={org_status};" + remediation
        )
    if not isinstance(org_body, dict):
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=org_profile outcome=fail trace_id=%s org_fp=%s reason=non_object_body body_type=%s",
                trace_id,
                org_fp,
                type(org_body).__name__,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=org_profile response is not a JSON object "
            f"url={org_url};" + remediation
        )

    description = org_body.get("description")
    if not isinstance(description, str):
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=org_profile outcome=fail trace_id=%s org_fp=%s reason=description_missing_or_non_string",
                trace_id,
                org_fp,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=org_profile description missing or not a string;"
            + remediation
        )
    if not description.strip():
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=org_profile outcome=fail trace_id=%s org_fp=%s reason=description_empty",
                trace_id,
                org_fp,
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=org_profile description is empty after trim;"
            + remediation
        )
    if len(description) > 500:
        if logger is not None:
            logger.info(
                "preflight.org_checklist check=org_profile outcome=fail trace_id=%s org_fp=%s reason=description_too_long description_len=%s",
                trace_id,
                org_fp,
                len(description),
            )
        raise PreflightError(
            "PREFLIGHT FAILED: org checklist check=org_profile description exceeds 500 characters;"
            + remediation
        )
    if logger is not None:
        logger.info(
            "preflight.org_checklist check=org_profile outcome=ok trace_id=%s org_fp=%s http_status=%s reachable=%s",
            trace_id,
            org_fp,
            org_status,
            org_reachable,
        )

    if logger is not None:
        logger.info(
            "preflight.org_checklist check=complete outcome=ok trace_id=%s org_fp=%s",
            trace_id,
            org_fp,
        )
