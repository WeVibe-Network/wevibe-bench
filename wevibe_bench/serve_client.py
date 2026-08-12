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

- ``turns``: count of assistant messages that carry positive generation
  content (positive output/reasoning tokens, a non-empty text part, or a tool
  part); bare step-finish placeholder rows with no content are NOT counted.
  The served transcript does not reliably carry a ``step-finish`` part per
  assistant message (relay stream-finalize is best-effort), so counting
  step-finish parts undercounts real turns.
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
- ``info_errors``: count of assistant messages whose ``info.error`` is set.
  This is where a mid-stream provider/relay failure actually lands: on the
  pinned worker opencode (1.18.1; verified against source
  ``session/processor.ts`` halt path + ``session/message-v2.ts``
  ``fromError``, and against a live 1.18.15 session DB) a stream kill sets
  ``assistantMessage.error = {name, data:{message, ...}}`` and publishes
  ``Session.Event.Error`` — it does NOT write an "error" part. ``error_parts``
  stays for forward/backward shape tolerance; ``info_errors`` is the
  operative count on 1.18.x.
- ``error_texts``: bounded list of bounded error message strings (at most
  ``_MAX_ERROR_TEXTS`` entries, each truncated to ``_MAX_ERROR_TEXT_CHARS``),
  collected from both "error" parts (``message``/``text``) and assistant
  ``info.error.data.message`` (falling back to ``info.error.message``). This
  is the classification surface: the relay loop guard's signature
  (``relay_loop_detected``) survives only in this text.
- ``assistant_messages``: count of messages with ``info.role == "assistant"``.
- ``user_messages``: count of messages with ``info.role == "user"``.

Transport-anomaly terminal mapping (mirrors the harness ``TURN_TERMINAL_*``
semantics in ``wevibe_bench/adapters/backgammon.py``; the exact strings here
are the documented surface of :func:`classify_transport_anomaly`):

- any error text carries the loop-guard signature
                 -> (``"guard_abort"``, ``"loop_guard"``)   [most specific first]
- truncations > 0  -> (``"truncated"``, ``"stream-incomplete"``)
- error_parts > 0 or info_errors > 0
                 -> (``"transport_error"``, ``"error_event"``)
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
TERMINAL_GUARD_ABORT = "guard_abort"
REASON_STREAM_INCOMPLETE = "stream-incomplete"
REASON_ERROR_EVENT = "error_event"
REASON_LOOP_GUARD = "loop_guard"
REASON_STREAM_FINALIZE_TIMEOUT = "stream_finalize_timeout"

# Signatures the relay's StreamLoopGuard stamps into its terminal error text.
# Two shapes exist in the wild, both relay-emitted:
#   - live (2026-08-10 runs, both cells' preserved worker DBs):
#     ``relay: generation loop detected (<request-id>)``
#   - legacy (older proxy build / pinned stdout fixture):
#     ``relay_loop_detected n=40 limit=3``
# The guard is a safety instrument of the proxy — harness-side recovery keys on
# this text; the proxy is never reconfigured from here.
LOOP_GUARD_SIGNATURES = ("relay_loop_detected", "generation loop detected")

# The relay's 30s stream-finalize watchdog (``relay: upstream completed but the
# stream did not finalize within 30000ms (<request-id>)``, observed 2026-08-10
# in both preserved run DBs): a transport death DISTINCT from the repetition
# guard — recovered with a resume nudge, never the anti-repetition nudge.
FINALIZE_TIMEOUT_SIGNATURE = "did not finalize"

# Bounds for the captured error text (enough to classify, never a transcript
# dump): at most this many entries, each truncated to this many chars.
_MAX_ERROR_TEXTS = 8
_MAX_ERROR_TEXT_CHARS = 240

# Transient-read retry (D-SERVE-MESSAGE-500, 2026-08-11). Observation reads are
# idempotent GETs, so a 5xx/429/socket fault is retried rather than allowed to
# kill a cell that is still alive. 4 attempts with linear backoff (0.5/1.0/1.5s)
# spans ~3s — long enough to ride out the observed intermittent Drizzle query
# failure, short enough that a genuinely dead serve still fails fast.
_READ_RETRY_ATTEMPTS = 4
_READ_RETRY_BACKOFF_S = 0.5

# Set by the harness to surface each retry on the progress stream. A retry that
# nobody can see is indistinguishable from a serve that never faulted, and a
# rising retry rate is the leading indicator of the underlying defect.
_READ_RETRY_OBSERVER: Callable[[str, int, Exception], None] = (
    lambda what, attempt, exc: None
)


def set_read_retry_observer(observer: Callable[[str, int, Exception], None]) -> None:
    """Install the callback invoked before each transient-read retry."""
    global _READ_RETRY_OBSERVER
    _READ_RETRY_OBSERVER = observer


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


def _is_real_assistant_turn(msg: dict) -> bool:
    """Return True iff an assistant message carries positive generation content.

    Defensive: missing/None keys are treated as 0/absent. An assistant message
    counts as a real turn when it has positive output tokens, positive
    reasoning tokens, at least one non-empty ``text`` part, or at least one
    ``tool`` part. A bare ``step-finish`` placeholder row with no such content
    is NOT a real turn.
    """
    info = msg.get("info") if isinstance(msg.get("info"), dict) else {}
    tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    if int(tokens.get("output", 0) or 0) > 0:
        return True
    if int(tokens.get("reasoning", 0) or 0) > 0:
        return True
    has_text = False
    has_tool = False
    for part in _as_list(msg.get("parts")):
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text" and str(part.get("text") or "").strip():
            has_text = True
        elif ptype == "tool":
            has_tool = True
    return has_text or has_tool


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
    info_errors = 0
    guard_aborted_turns = 0
    finalize_timeouts = 0
    error_texts: list[str] = []

    def _capture_error_text(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text or len(error_texts) >= _MAX_ERROR_TEXTS:
            return
        error_texts.append(text[:_MAX_ERROR_TEXT_CHARS])

    for msg in _as_list(messages):
        if not isinstance(msg, dict):
            continue
        info = msg.get("info")
        role = info.get("role") if isinstance(info, dict) else None
        if role == "assistant":
            assistant_msgs.append(msg)
            # A mid-stream provider/relay failure persists HERE on opencode
            # 1.18.x (processor halt -> assistantMessage.error), not as a part.
            err = info.get("error") if isinstance(info, dict) else None
            if isinstance(err, dict):
                info_errors += 1
                err_data = err.get("data") if isinstance(err.get("data"), dict) else {}
                err_text = str(err_data.get("message") or err.get("message") or "")
                _capture_error_text(err_text)
                # Turn-aligned signature counts (WO-TURNACCT-1): only a killed
                # message that ALSO counts as a real turn may be subtracted from
                # the scoring turn count downstream. Exact counts — independent
                # of the error_texts capture cap.
                if _is_real_assistant_turn(msg):
                    haystack = err_text.lower()
                    if any(sig in haystack for sig in LOOP_GUARD_SIGNATURES):
                        guard_aborted_turns += 1
                    elif FINALIZE_TIMEOUT_SIGNATURE in haystack:
                        finalize_timeouts += 1
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
                _capture_error_text(part.get("message") or part.get("text"))

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
        "turns": sum(_is_real_assistant_turn(msg) for msg in assistant_msgs),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cost_usd": cost_usd,
        "truncations": truncations,
        "last_finish": step_finish_reasons[-1] if step_finish_reasons else None,
        "error_parts": error_parts,
        "info_errors": info_errors,
        "guard_aborted_turns": guard_aborted_turns,
        "finalize_timeouts": finalize_timeouts,
        "error_texts": error_texts,
        "assistant_messages": len(assistant_msgs),
        "user_messages": user_count,
    }


def classify_transport_anomaly(metrics: dict) -> tuple:
    """Detect a transport truncation from ``metrics`` (see module docstring).

    Returns ``(terminal, reason)``; both ``None`` when no anomaly. The
    loop-guard signature is checked FIRST: it is the most specific terminal
    (a guard kill can coincide with a truncation part, and the guard kill —
    not the truncation — is what ended the turn).
    """
    for text in metrics.get("error_texts") or []:
        haystack = str(text).lower()
        if any(sig in haystack for sig in LOOP_GUARD_SIGNATURES):
            return TERMINAL_GUARD_ABORT, REASON_LOOP_GUARD
        if FINALIZE_TIMEOUT_SIGNATURE in haystack:
            # Second-specific check, same precedence logic as the guard: the
            # named relay terminal (the 30s finalize watchdog) is what ended
            # the turn — it beats the derived truncation reading.
            return TERMINAL_TRANSPORT_ERROR, REASON_STREAM_FINALIZE_TIMEOUT
    if metrics.get("truncations", 0) > 0:
        return TERMINAL_TRUNCATED, REASON_STREAM_INCOMPLETE
    if metrics.get("error_parts", 0) > 0 or metrics.get("info_errors", 0) > 0:
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


def _is_transient(exc: BaseException) -> bool:
    """True when ``exc`` is a retryable observation fault, not a real answer.

    A 5xx, a 429, or any socket-level failure is the serve failing to ANSWER —
    the session behind it is untouched. A 4xx other than 429 is a real answer
    (bad request, unknown session) and must never be retried into a false read.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    # URLError/OSError/socket.timeout: the request never landed.
    return isinstance(exc, (urllib.error.URLError, OSError))


def _retry_read(
    call,
    *,
    what: str,
    attempts: int = _READ_RETRY_ATTEMPTS,
    backoff_s: float = _READ_RETRY_BACKOFF_S,
    sleep=None,
):
    """Run an IDEMPOTENT read ``call``, retrying transient observation faults.

    D-SERVE-MESSAGE-500 (2026-08-11): a single ``GET /session/{id}/message``
    returning HTTP 500 from an opencode-internal Drizzle query killed a cell
    32 minutes in. The session was alive and generating; only the harness's
    ability to OBSERVE it failed. Recovery could not fire, because the drive
    loop decides whether to nudge by reading this very endpoint — a blind
    sensor reports no anomaly to recover from.

    Retrying here is safe ONLY because every caller is a read (GET). Writes
    (prompt_async, abort, summarize) are NEVER routed through this: replaying
    a prompt would duplicate a turn and corrupt the measurement.

    Raises the LAST :class:`ServeClientError` when every attempt fails, so a
    genuinely dead serve still surfaces loudly rather than hanging.
    """
    last = None
    # Resolved per call, never bound at def time, so a monkeypatched
    # ``time.sleep`` (tests) is honoured.
    nap = sleep if sleep is not None else time.sleep
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except ServeClientError as exc:
            cause = exc.__cause__ or exc
            if not _is_transient(cause):
                raise
            last = exc
            if attempt < attempts:
                _READ_RETRY_OBSERVER(what, attempt, exc)
                nap(backoff_s * attempt)
    raise ServeClientError(
        f"{what}: {attempts} consecutive transient failures; last: {last}"
    ) from last


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
        """GET /session/status -> parse_busy_status for ``session_id``.

        Retried: this is the completion signal polled by :meth:`wait_idle` and
        :meth:`wait_busy`. A transient fault here must not read as "idle".
        """
        payload = _retry_read(
            lambda: _http_json(
                "GET", self._url("/session/status"), body=None, timeout=self.timeout
            ),
            what="session_busy",
        )
        if not isinstance(payload, dict):
            return False
        return parse_busy_status(payload, session_id)

    def get_messages(self, session_id: str) -> list:
        """GET /session/{sid}/message -> parsed JSON message list.

        Retries transient observation faults (:func:`_retry_read`): this is the
        endpoint D-SERVE-MESSAGE-500 intermittently 500s on, and it is also the
        harness's only window onto the session — a single unretried failure
        here previously voided a 32-minute cell.
        """
        url = self._url(
            f"/session/{urllib.parse.quote(session_id, safe='')}/message"
        )
        payload = _retry_read(
            lambda: _http_json("GET", url, body=None, timeout=self.timeout),
            what=f"get_messages({session_id})",
        )
        return _as_list(payload)

    def wait_idle(
        self, session_id: str, *, timeout_s: float = 600.0
    ) -> bool:
        """Poll :meth:`session_busy` until idle or timeout.

        Returns True if the session reached idle, False on timeout.

        A probe that fails even after retries is treated as STILL BUSY, never
        as idle. Reading "idle" from a failed probe is the dangerous direction:
        it releases the harness to gate a worktree the worker is still writing
        (the 2026-08-09 turns=0/gates-race void). Waiting costs only time; a
        sustained outage still ends at ``timeout_s`` and returns False.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                busy = self.session_busy(session_id)
            except ServeClientError:
                busy = True
            if not busy:
                return True
            time.sleep(self.poll_interval)
        return False

    def wait_busy(
        self, session_id: str, *, timeout_s: float = 60.0
    ) -> bool:
        """Poll :meth:`session_busy` until busy or timeout.

        ``prompt_async`` is fire-and-forget: the serve marks the session busy
        only when it picks the prompt up. A bare ``wait_idle`` started in that
        window sees a false idle and returns instantly (the 2026-08-09
        turns=0/gates-race-the-worktree void). Always confirm busy first.
        Returns True if the session went busy, False on timeout.

        A failed probe is treated as NOT-yet-busy so the wait continues: the
        conservative direction here is to keep waiting for confirmation rather
        than declare a pickup that was never observed.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.session_busy(session_id):
                    return True
            except ServeClientError:
                pass
            time.sleep(self.poll_interval)
        return False

    def metrics(self, session_id: str, *, since: int | None = None) -> dict:
        """Return :func:`extract_transcript_metrics` for the session messages.

        ``since`` windows the scan to messages at index >= ``since`` (the same
        watermark discipline as :meth:`assistant_texts_since`). A killed turn's
        ``info.error`` persists in the transcript FOREVER, so a caller that
        classifies transport anomalies must scan only what the current drive
        produced — a cumulative read re-matches the same kill on every later
        phase (the 2026-08-10 chunk-2 defect: a recovered, CHUNK FINISHED-
        landing drive was reclassified guard_abort until the budget exhausted).
        """
        messages = self.get_messages(session_id)
        if since is not None:
            messages = messages[since:]
        return extract_transcript_metrics(messages)

    def last_assistant_text(self, session_id: str) -> str:
        """Return the concatenated text parts of the newest assistant message.

        Used by the chunked first pass to check for the CHUNK FINISHED marker.
        Empty string when no assistant message exists or none carry text.
        """
        for msg in reversed(self.get_messages(session_id)):
            if not isinstance(msg, dict):
                continue
            info = msg.get("info")
            role = info.get("role") if isinstance(info, dict) else None
            if role != "assistant":
                continue
            texts = [
                str(part.get("text") or "")
                for part in _as_list(msg.get("parts"))
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "".join(texts)
        return ""

    def assistant_texts_since(self, session_id: str, watermark: int) -> list[str]:
        """Concatenated text of each assistant message at index >= ``watermark``.

        The chunked pass watermarks the message list before driving a chunk and
        scans only what THAT chunk produced. This is the durable marker check:
        the worker emits the CHUNK marker and its WEVIBE_DISCOVERY block in
        either order, sometimes in separate assistant messages, so scanning
        only the newest message (or joining across messages) misdetects.
        """
        texts: list[str] = []
        for msg in self.get_messages(session_id)[watermark:]:
            if not isinstance(msg, dict):
                continue
            info = msg.get("info")
            role = info.get("role") if isinstance(info, dict) else None
            if role != "assistant":
                continue
            texts.append(
                "".join(
                    str(part.get("text") or "")
                    for part in _as_list(msg.get("parts"))
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            )
        return texts

    def compaction_since(self, session_id: str, watermark: int) -> bool:
        """True when any message at index >= ``watermark`` carries a compaction part.

        OpenCode writes a completed summarize as a synthetic user message with
        a ``compaction`` part; that is the only durable evidence a compaction
        happened (busy/idle alone cannot distinguish it from a normal turn).
        """
        for msg in self.get_messages(session_id)[watermark:]:
            if not isinstance(msg, dict):
                continue
            for part in _as_list(msg.get("parts")):
                if isinstance(part, dict) and part.get("type") == "compaction":
                    return True
        return False

    def session_model(self, session_id: str) -> tuple[str, str] | None:
        """(providerID, modelID) of the newest real user message.

        Skips synthetic compaction parents (mirrors the self-compact plugin's
        resolution). The summarize payload requires both fields; None when no
        real user message carries them.
        """
        for msg in reversed(self.get_messages(session_id)):
            if not isinstance(msg, dict):
                continue
            info = msg.get("info")
            if not isinstance(info, dict) or info.get("role") != "user":
                continue
            parts = _as_list(msg.get("parts"))
            if any(
                isinstance(part, dict) and part.get("type") == "compaction"
                for part in parts
            ):
                continue
            model = info.get("model")
            if isinstance(model, dict):
                provider_id = model.get("providerID")
                model_id = model.get("modelID")
                if provider_id and model_id:
                    return str(provider_id), str(model_id)
        return None

    def summarize(
        self,
        session_id: str,
        *,
        provider_id: str,
        model_id: str,
        auto: bool = False,
        timeout_s: float = 1800.0,
    ) -> None:
        """POST /session/{sid}/summarize — the real AI-compaction endpoint.

        ``auto=False`` summarizes WITHOUT the synthetic autocontinue turn (the
        harness itself sends the next chunk). The request can block for the
        whole summarize pass, hence the long default timeout. Raises
        :class:`ServeClientError` on non-2xx or transport failure; callers
        treat compaction as fail-open.
        """
        url = self._url(
            f"/session/{urllib.parse.quote(session_id, safe='')}/summarize"
        )
        status = _http_status(
            "POST",
            url,
            body={"providerID": provider_id, "modelID": model_id, "auto": auto},
            timeout=timeout_s,
        )
        if status < 200 or status >= 300:
            raise ServeClientError(f"summarize: expected 2xx, got {status}")


def founder_attach_command(host_port: int, session_id: str | None = None) -> str:
    """Return the one-line re-attach command for an ``opencode serve``.

    Carries ``--session`` whenever the cell session id is known: without it the
    TUI opens on the attach client's own default project view (a "new session"
    screen) instead of the live worker session (2026-08-09 founder trap).
    """
    cmd = f"opencode attach http://127.0.0.1:{host_port}"
    if session_id:
        cmd += f" --session {session_id}"
    return cmd


# Explicit public surface.
__all__ = [
    "FINALIZE_TIMEOUT_SIGNATURE",
    "LOOP_GUARD_SIGNATURES",
    "REASON_LOOP_GUARD",
    "REASON_STREAM_FINALIZE_TIMEOUT",
    "ServeClient",
    "ServeClientError",
    "TERMINAL_GUARD_ABORT",
    "build_prompt_body",
    "classify_step_finish_reason",
    "classify_transport_anomaly",
    "extract_transcript_metrics",
    "founder_attach_command",
    "parse_busy_status",
]