"""HTTP client for driving scoring cells through a running ``opencode serve``.

This module talks to an ``opencode serve`` instance via its HTTP API using ONLY
the standard-library ``urllib`` package (consistent with the rest of the
harness, which uses stdlib urllib exclusively -- no requests/httpx). It lets
the harness enqueue a prompt asynchronously, wait for the session to go idle,
and pull the resulting transcript metrics.

Empirically validated against opencode 1.18.10 (serve at host:port):

- ``POST {base}/session`` body ``{}`` -> 201 ``{"id": "ses_...", ...}``.
  (create_session parses ``id`` from the returned JSON.)
- ``POST {base}/session/{sid}/prompt_async`` body
  ``{"parts":[{"type":"text","text":"<prompt>"}]}`` -> 204. The prompt is
  enqueued and the call returns immediately.
- ``GET {base}/session/status`` -> ``{}`` when idle, or
  ``{"<sid>":{"type":"busy"}}`` while that session is generating. A session
  drops out of the map when it goes idle. This is the completion signal.
- ``GET {base}/session/{sid}/message`` -> a JSON list of message objects, each
  with ``info`` (role, tokens{input,output,reasoning,total,cache}, finish,
  cost, time{created,completed}) and ``parts`` (a list of parts, each with a
  ``type`` of "text" | "reasoning" | "step-start" | "step-finish" | "tool" |
  "error" | ...; a step-finish part carries ``reason`` e.g. "stop" plus
  ``tokens`` and ``cost``).
- ``GET {base}/session/{sid}`` -> the session object with tokens/cost/time.

Field semantics for :func:`extract_transcript_metrics` (defensive -- missing
keys default safely):

- ``turns``: count of ``step-finish`` parts across all assistant messages
  (one generation step per step-finish part).
- ``input_tokens``: max ``info.tokens.input`` across assistant messages
  (default 0).
- ``output_tokens``: sum ``info.tokens.output`` across assistant messages.
- ``reasoning_tokens``: sum ``info.tokens.reasoning`` across assistant
  messages.
- ``cost_usd``: sum ``info.cost`` across assistant messages.
- ``truncations``: count of ``step-finish`` parts whose ``reason`` is in
  {length, unknown, stream-incomplete} (truncation signals).
- ``last_finish``: the ``reason`` of the LAST step-finish part seen, else
  ``None``.
- ``error_parts``: count of parts whose ``type`` is "error".
- ``assistant_messages``: count of messages with ``info.role == "assistant"``.
- ``user_messages``: count of messages with ``info.role == "user"``.

Transport-anomaly terminal mapping (mirrors the harness ``TURN_TERMINAL_*``
semantics in ``wevibe_bench/adapters/backgammon.py``; the exact strings here
are the documented surface of :func:`classify_transport_anomaly`):

- truncations > 0  -> (``"truncated"``, ``"stream-incomplete"``)
- error_parts > 0  -> (``"transport_error"``, ``"error_event"``)
- otherwise        -> (``None``, ``None``)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

# Truncation step-finish reasons (mirrors backgammon.py
# ``TRUNCATED_STEP_FINISH_REASONS = frozenset({"unknown", "stream-incomplete"})``,
# extended with "length").
TRUNCATED_STEP_FINISH_REASONS = frozenset({"unknown", "stream-incomplete", "length"})

# Anomaly terminals returned by :func:`classify_transport_anomaly`.
TERMINAL_TRUNCATED = "truncated"
TERMINAL_TRANSPORT_ERROR = "transport_error"
REASON_STREAM_INCOMPLETE = "stream-incomplete"
REASON_ERROR_EVENT = "error_event"


class ServeClientError(Exception):
    """Raised for any HTTP/transport failure from :class:`ServeClient`.

    The underlying message is preserved as ``__cause__``-style context via the
    ``reason`` attribute.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def build_prompt_body(prompt: str) -> dict:
    """Return the JSON body for ``POST /session/{sid}/prompt_async``."""
    return {"parts": [{"type": "text", "text": prompt}]}


def parse_busy_status(payload: dict, session_id: str) -> bool:
    """Return True iff ``payload`` (parsed ``GET /session/status``) marks the
    session busy.

    ``payload`` maps session_id -> {"type":"busy"} while generating; the
    session is absent or the map empty once idle.
    """
    entry = payload.get(session_id)
    return bool(entry) and entry.get("type") == "busy"


def classify_step_finish_reason(reason: Optional[str]) -> str:
    """Return a normalized terminal reason string for a step-finish reason.

    Maps: "stop"->"stop", "length"->"length"; values in {"unknown",
    "stream-incomplete"} are returned unchanged (truncation signals); anything
    else (including None) -> "unknown".
    """
    if reason == "stop":
        return "stop"
    if reason == "length":
        return "length"
    if reason in {"unknown", "stream-incomplete"}:
        return reason
    return "unknown"


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def extract_transcript_metrics(messages: list) -> dict:
    """Compute transcript metrics from a ``GET /session/{sid}/message`` payload.

    See the module docstring for exact field semantics. Defensive: malformed
    or empty payloads yield safe zeros/None.
    """
    assistant_msgs: list[dict] = []
    user_count = 0
    step_finish_reasons: list[str] = []
    truncations = 0
    error_parts = 0

    for msg in _as_list(messages):
        if not isinstance(msg, dict):
            continue
        info = msg.get("info")
        role = info.get("role") if isinstance(info, dict) else None
        if role == "assistant":
            assistant_msgs.append(msg)
        elif role == "user":
            user_count += 1

        for part in _as_list(msg.get("parts")):
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "step-finish":
                reason = classify_step_finish_reason(part.get("reason"))
                step_finish_reasons.append(reason)
                if reason in TRUNCATED_STEP_FINISH_REASONS:
                    truncations += 1
            elif ptype == "error":
                error_parts += 1

    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    cost_usd = 0.0
    for msg in assistant_msgs:
        info = msg.get("info") if isinstance(msg, dict) else {}
        tokens = info.get("tokens") if isinstance(info, dict) else {}
        if not isinstance(tokens, dict):
            tokens = {}
        input_tokens = max(input_tokens, tokens.get("input", 0) or 0)
        output_tokens += tokens.get("output", 0) or 0
        reasoning_tokens += tokens.get("reasoning", 0) or 0
        cost_usd += info.get("cost", 0.0) or 0.0

    return {
        "turns": len(step_finish_reasons),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cost_usd": cost_usd,
        "truncations": truncations,
        "last_finish": step_finish_reasons[-1] if step_finish_reasons else None,
        "error_parts": error_parts,
        "assistant_messages": len(assistant_msgs),
        "user_messages": user_count,
    }


def classify_transport_anomaly(metrics: dict) -> tuple:
    """Detect a transport truncation from ``metrics`` (see module docstring).

    Returns ``(terminal, reason)``; both ``None`` when no anomaly.
    """
    if metrics.get("truncations", 0) > 0:
        return TERMINAL_TRUNCATED, REASON_STREAM_INCOMPLETE
    if metrics.get("error_parts", 0) > 0:
        return TERMINAL_TRANSPORT_ERROR, REASON_ERROR_EVENT
    return None, None


# ---- injectable HTTP primitives (swapped in unit tests) ----

def _read_json_response(resp) -> Any:
    """Read and parse a JSON response body, tolerating an empty body."""
    raw = resp.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _http_json(method: str, url: str, body=None, timeout: float = 5.0) -> Any:
    """Perform an HTTP request expecting a JSON (or empty) response body.

    ``body`` is a dict or None; ``None`` is sent as an empty JSON object.
    Raises :class:`ServeClientError` on HTTP error or network failure.
    """
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read_json_response(resp)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise ServeClientError(f"{method} {url} failed: {exc}") from exc


def _http_status(method: str, url: str, body=None, timeout: float = 5.0) -> int:
    """Perform an HTTP request and return the response status code."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise ServeClientError(f"{method} {url} failed: {exc}") from exc


class ServeClient:
    """Thin stdlib-urllib client for a running ``opencode serve``.

    Real IO only; no retries on transient errors (the harness decides).
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        poll_interval: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval

    def _url(self, path: str) -> str:
        return self.base_url + path

    def create_session(self) -> str:
        """POST /session {} -> return the created session id."""
        payload = _http_json(
            "POST", self._url("/session"), body={}, timeout=self.timeout
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ServeClientError(f"create_session: no 'id' in response: {payload!r}")
        return payload["id"]

    def send_prompt(self, session_id: str, prompt: str) -> None:
        """POST prompt_async; raise :class:`ServeClientError` on non-204."""
        url = self._url(
            f"/session/{urllib.parse.quote(session_id, safe='')}/prompt_async"
        )
        status = _http_status(
            "POST", url, body=build_prompt_body(prompt), timeout=self.timeout
        )
        if status != 204:
            raise ServeClientError(
                f"send_prompt: expected 204, got {status}"
            )

    def abort(self, session_id: str) -> None:
        """POST /session/{sid}/abort to stop serve-side generation.

        Returns None on any 2xx; raises :class:`ServeClientError` on a non-2xx
        status or on any HTTP/URLError/OSError (wrapped by :func:`_http_status`).
        """
        url = self._url(
            f"/session/{urllib.parse.quote(session_id, safe='')}/abort"
        )
        status = _http_status("POST", url, body=None, timeout=self.timeout)
        if status < 200 or status >= 300:
            raise ServeClientError(f"abort: expected 2xx, got {status}")

    def session_busy(self, session_id: str) -> bool:
        """GET /session/status -> parse_busy_status for ``session_id``."""
        payload = _http_json(
            "GET", self._url("/session/status"), body=None, timeout=self.timeout
        )
        if not isinstance(payload, dict):
            return False
        return parse_busy_status(payload, session_id)

    def get_messages(self, session_id: str) -> list:
        """GET /session/{sid}/message -> parsed JSON message list."""
        url = self._url(
            f"/session/{urllib.parse.quote(session_id, safe='')}/message"
        )
        payload = _http_json("GET", url, body=None, timeout=self.timeout)
        return _as_list(payload)

    def wait_idle(
        self, session_id: str, *, timeout_s: float = 600.0
    ) -> bool:
        """Poll :meth:`session_busy` until idle or timeout.

        Returns True if the session reached idle, False on timeout.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.session_busy(session_id):
                return True
            time.sleep(self.poll_interval)
        return False

    def metrics(self, session_id: str) -> dict:
        """Return :func:`extract_transcript_metrics` for the session messages."""
        return extract_transcript_metrics(self.get_messages(session_id))


def founder_attach_command(host_port: int) -> str:
    """Return the one-line re-attach command for an ``opencode serve``."""
    return f"opencode attach http://127.0.0.1:{host_port}"


# Explicit public surface.
__all__ = [
    "ServeClient",
    "ServeClientError",
    "build_prompt_body",
    "classify_step_finish_reason",
    "classify_transport_anomaly",
    "extract_transcript_metrics",
    "founder_attach_command",
    "parse_busy_status",
]