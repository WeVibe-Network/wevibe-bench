"""Signed HTTP client for hub lifecycle endpoints."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .identity import Identity
from .lconfig import LifecycleConfig
from .logging_util import new_trace_id
from .signing import wevibe_signed_headers


Transport = Callable[[str, dict[str, str], dict[str, Any] | None], tuple[int, Any, bool]]


def _json_value(payload: bytes) -> Any:
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def deny_submission_message(org_id: str, reason: str, signed_by: str, submission_hash: str) -> str:
    return "\n".join(
        [
            "wevibe.deny_submission.v1",
            f"org_id:{org_id}",
            f"reason:{reason}",
            f"signed_by:{signed_by}",
            f"submission_hash:{submission_hash}",
        ]
    )


class HubClient:
    def __init__(
        self,
        cfg: LifecycleConfig,
        logger: Any,
        transport: Transport | None = None,
    ) -> None:
        self._cfg = cfg
        self._logger = logger
        self._transport: Transport = transport or self._urllib_transport

    @staticmethod
    def _urllib_transport(
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, Any, bool]:
        try:
            method = "GET" if body is None else "POST"
            payload = None if body is None else json.dumps(body).encode("utf-8")
            request = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=10.0) as response:
                status = response.getcode()
                response_payload = response.read()
            return status, _json_value(response_payload), True
        except urllib.error.HTTPError as exc:
            try:
                error_payload = exc.read()
            except OSError:
                error_payload = b""
            return exc.code, _json_value(error_payload), True
        except (urllib.error.URLError, OSError, socket.timeout):
            return 0, {}, False
        except Exception:
            return 0, {}, False

    @staticmethod
    def _sanitize(value: Any) -> str:
        return " ".join(str(value).split())

    def _log(
        self,
        level: str,
        op: str,
        trace: str,
        status: str,
        dur_ms: int,
        **fields: Any,
    ) -> None:
        details = " ".join(
            f"{key}={self._sanitize(value)}"
            for key, value in fields.items()
            if value is not None
        )
        msg = f"op={op} trace={trace}"
        if details:
            msg = f"{msg} {details}"
        msg = f"{msg} status={status} dur_ms={dur_ms}"
        log_fn = getattr(self._logger, level, self._logger.info)
        log_fn(msg)

    def _request(
        self,
        op: str,
        identity: Identity,
        path: str,
        body: dict[str, Any] | None,
        expected_statuses: tuple[int, ...] = (200,),
    ) -> Any:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        headers = wevibe_signed_headers(identity, trace)
        url = f"{self._cfg.hub_url}{path}"
        http_status, payload, reachable = self._transport(url, headers, body)
        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000

        if not reachable:
            self._log("error", op, trace, "err", int(dur_ms), url=path, reason="unreachable")
            raise RuntimeError(f"hub unreachable for {path}")

        if http_status not in expected_statuses:
            self._log("error", op, trace, "err", int(dur_ms), url=path, http_status=http_status)
            raise RuntimeError(f"hub request failed status={http_status} path={path} payload={payload}")

        self._log("info", op, trace, "ok", int(dur_ms), url=path, http_status=http_status)
        return payload

    @staticmethod
    def _find_result_entry(payload: Any, submission_hash: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError(f"hub response payload must be an object for submission_hash={submission_hash}: {payload}")

        results = payload.get("results")
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"hub response missing results for submission_hash={submission_hash}: {payload}")

        for item in results:
            if not isinstance(item, dict):
                continue
            item_hash = item.get("submission_hash")
            if not isinstance(item_hash, str):
                item_hash = item.get("hash")
            if isinstance(item_hash, str) and item_hash == submission_hash:
                return item

        if len(results) == 1 and isinstance(results[0], dict):
            return results[0]

        raise RuntimeError(f"hub response missing result entry for submission_hash={submission_hash}: {payload}")

    @classmethod
    def _require_passed_result(cls, op: str, payload: Any, submission_hash: str) -> None:
        result = cls._find_result_entry(payload, submission_hash)
        passed = result.get("passed")
        code = result.get("code")
        error = result.get("error")
        if passed is not True or (isinstance(error, str) and error.strip()):
            raise RuntimeError(
                f"{op} failed submission_hash={submission_hash} code={code!r} error={error!r}"
            )

    @classmethod
    def _require_no_error_result(cls, op: str, payload: Any, submission_hash: str) -> None:
        # submit-keyword-results success entries carry NO `passed` field —
        # success is the ABSENCE of a non-empty `error` on the matching result.
        result = cls._find_result_entry(payload, submission_hash)
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            raise RuntimeError(
                f"{op} failed submission_hash={submission_hash} error={error!r}"
            )

    def verify_keywords(self, identity: Identity, org_id: str, entries: list[dict[str, Any]]) -> Any:
        payload = self._request(
            "lifecycle.hub.verify_keywords",
            identity,
            f"/v1/orgs/{org_id}/verify-keywords",
            {"entries": entries},
        )
        for entry in entries:
            submission_hash = entry.get("submission_hash") if isinstance(entry, dict) else None
            if isinstance(submission_hash, str) and submission_hash:
                self._require_passed_result("verify_keywords", payload, submission_hash)
        return payload

    def submit_keyword_results(
        self,
        identity: Identity,
        org_id: str,
        submission_hash: str,
        classified: list[dict[str, Any]],
    ) -> Any:
        payload = self._request(
            "lifecycle.hub.submit_keyword_results",
            identity,
            f"/v1/orgs/{org_id}/submit-keyword-results",
            {
                "memories": [
                    {
                        "submission_hash": submission_hash,
                        "classified": classified,
                        "suggestions": [],
                    }
                ]
            },
        )
        self._require_no_error_result("submit_keyword_results", payload, submission_hash)
        return payload

    def batch_submit(self, identity: Identity, org_id: str) -> Any:
        return self._request(
            "lifecycle.hub.batch_submit",
            identity,
            f"/v1/orgs/{org_id}/moderation/batch-submit",
            {},
        )

    def commit_status(self, identity: Identity, org_id: str) -> Any:
        return self._request(
            "lifecycle.hub.commit_status",
            identity,
            f"/v1/orgs/{org_id}/commit-status",
            None,
        )

    def moderation_queue(self, identity: Identity, org_id: str) -> Any:
        return self._request(
            "lifecycle.hub.moderation_queue",
            identity,
            f"/v1/orgs/{org_id}/moderation/queue",
            None,
        )

    def deny_submission(
        self,
        leader: Identity,
        org_id: str,
        submission_hash: str,
        reason: str,
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty")

        body_signature = leader.sign_hex(
            deny_submission_message(org_id, reason, leader.ed_pubkey_hex, submission_hash).encode("utf-8")
        )

        op = "lifecycle.hub.deny_submission"
        t0 = time.perf_counter_ns()
        payload = self._request(
            op,
            leader,
            f"/v1/orgs/{org_id}/moderation/{submission_hash}/deny",
            {
                "reason": reason,
                "signed_by": leader.ed_pubkey_hex,
                "signature": body_signature,
            },
        )
        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        self._log(
            "info",
            op,
            "-",
            "ok",
            int(dur_ms),
            leader_fp=leader.ed_pub_fp(),
            org=org_id,
            submission_hash=submission_hash,
            http_status=200,
        )

        if not isinstance(payload, dict):
            raise RuntimeError(f"hub deny_submission expected object payload, got: {payload}")
        return payload
