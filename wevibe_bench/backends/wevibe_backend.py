"""Live WeVibe /v1/recall backend adapter for benchmark ON cells."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from wevibe_bench.backends.base import (
    DeliveryVerdict,
    MemoryBackend,
    NeedCard,
    RecallResult,
    RecalledMemory,
)
from wevibe_bench.config import RunConfig


LOGGER = logging.getLogger("wevibe_bench.backend")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CALLED_REASON_CODES = {"decrypt_failed", "filtered_out"}

Transport = Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, Any], bool]]


def _num(value: Any) -> float | None:
    """Return finite float(value) or None when absent/invalid/non-finite."""

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _to_str(value: Any) -> str | None:
    """Return a non-empty string for any non-None value, else None."""

    if value is None:
        return None
    parsed = str(value)
    if parsed:
        return parsed
    return None


def _json_dict(payload: bytes) -> dict[str, Any]:
    """Decode JSON bytes into a dict, or return {} when malformed/non-object."""

    if not payload:
        return {}
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


class WeVibeBackend(MemoryBackend):
    """ON backend that posts a need-card to live `/v1/recall` and normalizes response."""

    def __init__(self, cfg: RunConfig, transport: Transport | None = None) -> None:
        """Initialize backend with optional injectable transport seam.

        transport signature:
          Callable[[str, dict, dict], tuple[int, dict, bool]]
            args (url, headers, body) -> (http_status, response_json, reachable)
        """

        self._cfg = cfg
        self._transport: Transport = transport or self._urllib_transport
        self._session_id: str | None = None

    @staticmethod
    def _urllib_transport(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any], bool]:
        """POST JSON via urllib and always return (status, body_dict, reachable)."""

        try:
            payload = json.dumps(body).encode("utf-8")
            request = urllib.request.Request(
                url=url,
                data=payload,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10.0) as response:
                status = response.getcode()
                response_payload = response.read()
            return status, _json_dict(response_payload), True
        except urllib.error.HTTPError as exc:
            try:
                error_payload = exc.read()
            except OSError:
                error_payload = b""
            return exc.code, _json_dict(error_payload), True
        except (urllib.error.URLError, OSError, socket.timeout):
            return 0, {}, False
        except Exception:
            LOGGER.exception("recall transport failed unexpectedly")
            return 0, {}, False

    def prime_session(self, session_id: str) -> None:
        """Pin a session id used in recall wire calls until changed."""

        self._session_id = session_id

    def _read_token(self, cfg: RunConfig) -> str | None:
        """Load and validate bearer token from cfg.session_token_path seam."""

        token_path = os.path.expanduser(cfg.session_token_path)
        try:
            with open(token_path, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as exc:
            LOGGER.warning(
                "recall token unavailable path=%s err=%s; proceeding with token=None",
                token_path,
                exc,
            )
            return None

        if not token:
            LOGGER.warning(
                "recall token blank path=%s; proceeding with token=None",
                token_path,
            )
            return None

        if not _TOKEN_RE.fullmatch(token):
            LOGGER.warning(
                "recall token invalid-format path=%s len=%d; proceeding with token=None",
                token_path,
                len(token),
            )
            return None

        return token

    def _build_memories(self, response: dict[str, Any]) -> list[RecalledMemory]:
        """Convert response JSON `memories` array into RecalledMemory entries."""

        raw_memories = response.get("memories") or []
        if not isinstance(raw_memories, list):
            return []

        memories: list[RecalledMemory] = []
        for item in raw_memories:
            if not isinstance(item, dict):
                continue

            breakdown = item.get("breakdown") or {}
            if not isinstance(breakdown, dict):
                breakdown = {}

            raw_keywords = item.get("matched_keywords") or []
            matched_keywords = raw_keywords if isinstance(raw_keywords, list) else []

            text_value = item.get("text") or ""
            text = text_value if isinstance(text_value, str) else str(text_value)

            memories.append(
                RecalledMemory(
                    cid=_to_str(item.get("cid")),
                    score=_num(item.get("score")),
                    vector_score=_num(breakdown.get("vector_score")),
                    combined_score=_num(breakdown.get("combined_score")),
                    keyword_score=_num(breakdown.get("keyword_score")),
                    matched_keywords=[str(keyword) for keyword in matched_keywords],
                    text=text,
                )
            )

        return memories

    def _log_recall(self, trace_id: str, result: RecallResult) -> None:
        """Emit mandatory concise observability log for each recall operation."""

        verdict = self.verify_delivery(result)
        text_lengths = [len(memory.text) for memory in result.memories]
        LOGGER.info(
            "recall trace_id=%s http_status=%s reachable=%s n_memories=%d verdict=%s reason_code=%s text_lens=%s",
            trace_id,
            result.http_status,
            result.reachable,
            len(result.memories),
            verdict.value,
            result.reason_code or "none",
            text_lengths,
        )

    def recall(self, need: NeedCard, cfg: RunConfig, org_id: str | None = None) -> RecallResult:
        """Call live `/v1/recall` and normalize transport/result into RecallResult."""

        trace_suffix = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        trace_id = f"bench-{trace_suffix}"
        session_id = self._session_id or f"bench-{trace_suffix}"
        resolved_org_id = org_id or cfg.org_id

        token = self._read_token(cfg)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-WeVibe-Trace-Id": trace_id,
        }
        body = need.to_wire(cfg, session_id, org_id=resolved_org_id)
        url = f"{cfg.mcp_recall_url}/v1/recall"

        try:
            http_status, response_json, reachable = self._transport(url, headers, body)
        except Exception:
            LOGGER.exception("recall transport callable raised")
            result = RecallResult(
                memories=[],
                status="error",
                reason_code=None,
                reachable=False,
                http_status=0,
            )
            self._log_recall(trace_id, result)
            return result

        response = response_json if isinstance(response_json, dict) else {}

        if not reachable:
            result = RecallResult(
                memories=[],
                status="error",
                reason_code=None,
                reachable=False,
                http_status=http_status,
            )
            self._log_recall(trace_id, result)
            return result

        if http_status != 200:
            result = RecallResult(
                memories=[],
                status="error",
                reason_code=_to_str(response.get("code")),
                reachable=True,
                http_status=http_status,
            )
            self._log_recall(trace_id, result)
            return result

        status_value = response.get("status")
        status = status_value if isinstance(status_value, str) and status_value else "ok"
        reason_code = _to_str(response.get("reason_code"))

        memories = self._build_memories(response)
        if cfg.deterministic_topn:
            ranked_memories: list[tuple[RecalledMemory, float | None]] = []
            for memory in memories:
                # `combined_score` is the preferred ranking score; when absent, use top-level `score`.
                rank_score = memory.combined_score if memory.combined_score is not None else memory.score
                ranked_memories.append((memory, rank_score))

            ranked_memories.sort(
                key=lambda item: (
                    -(item[1] if item[1] is not None else float("-inf")),
                    item[0].cid or "",
                )
            )
            ranked_memories = ranked_memories[: cfg.surface_budget]
            memories = [memory for memory, _ in ranked_memories]
            LOGGER.info(
                "[recall] deterministic top-N selected n=%d cids=%s scores=%s",
                len(memories),
                ",".join((memory.cid or "") for memory in memories),
                ",".join("None" if score is None else str(score) for _, score in ranked_memories),
            )

        result = RecallResult(
            memories=memories,
            status=status,
            reason_code=reason_code,
            reachable=True,
            http_status=200,
        )
        self._log_recall(trace_id, result)
        return result

    def verify_delivery(self, result: RecallResult) -> DeliveryVerdict:
        """Map live recall mechanism onto YES/CALLED/NO benchmark delivery verdict.

        Branch mapping:
        - NO: transport unreachable, HTTP error, or status='error'.
        - YES: at least one returned memory contains non-empty decrypted plaintext text.
        - CALLED: recall completed but delivery did not, indicated by reason_code in
          {'decrypt_failed', 'filtered_out'}.
        - NO: all other empty/non-delivered outcomes (no_memories/no_keywords/no_membership,
          or memories with empty text only).
        """

        if not result.reachable or result.status == "error" or result.http_status != 200:
            return DeliveryVerdict.NO

        if any(memory.has_content() for memory in result.memories):
            return DeliveryVerdict.YES

        if result.reason_code in _CALLED_REASON_CODES:
            return DeliveryVerdict.CALLED

        return DeliveryVerdict.NO
