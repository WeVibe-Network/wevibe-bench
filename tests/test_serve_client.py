"""Hermetic unit tests for wevibe_bench.serve_client.

No live server, no docker, no model. All HTTP IO is made injectable via the
module-level ``_http_json`` / ``_http_status`` helpers, which these tests
monkeypatch. Never hits the network.
"""

import urllib.error

import pytest

from wevibe_bench.serve_client import (
    ServeClient,
    ServeClientError,
    build_prompt_body,
    classify_step_finish_reason,
    classify_transport_anomaly,
    extract_transcript_metrics,
    founder_attach_command,
    parse_busy_status,
)


# ---------------------------------------------------------------------------
# build_prompt_body
# ---------------------------------------------------------------------------
def test_build_prompt_body_shape():
    body = build_prompt_body("hello world")
    assert body == {"parts": [{"type": "text", "text": "hello world"}]}


# ---------------------------------------------------------------------------
# parse_busy_status
# ---------------------------------------------------------------------------
def test_parse_busy_status_busy():
    assert parse_busy_status({"ses_1": {"type": "busy"}}, "ses_1") is True


def test_parse_busy_status_idle():
    assert parse_busy_status({"ses_1": {"type": "idle"}}, "ses_1") is False


def test_parse_busy_status_absent_session():
    assert parse_busy_status({"ses_2": {"type": "busy"}}, "ses_1") is False


def test_parse_busy_status_empty_dict():
    assert parse_busy_status({}, "ses_1") is False


# ---------------------------------------------------------------------------
# classify_step_finish_reason
# ---------------------------------------------------------------------------
def test_classify_step_finish_reason_stop():
    assert classify_step_finish_reason("stop") == "stop"


def test_classify_step_finish_reason_length():
    assert classify_step_finish_reason("length") == "length"


def test_classify_step_finish_reason_truncation_values():
    assert classify_step_finish_reason("unknown") == "unknown"
    assert classify_step_finish_reason("stream-incomplete") == "stream-incomplete"


def test_classify_step_finish_reason_default():
    assert classify_step_finish_reason(None) == "unknown"
    assert classify_step_finish_reason("tool-calls") == "unknown"


# ---------------------------------------------------------------------------
# extract_transcript_metrics
# ---------------------------------------------------------------------------
def _realistic_transcript():
    return [
        {
            "info": {
                "role": "user",
                "tokens": {"input": 10, "output": 0, "total": 10},
                "finish": "stop",
                "cost": 0.0,
                "time": {"created": 1, "completed": 1},
            },
            "parts": [{"type": "text", "text": "do it"}],
        },
        {
            "info": {
                "role": "assistant",
                "tokens": {"input": 50, "output": 30, "reasoning": 12, "total": 92},
                "finish": "stop",
                "cost": 0.01,
                "time": {"created": 2, "completed": 3},
            },
            "parts": [
                {"type": "step-start", "id": "s1"},
                {"type": "reasoning", "text": "thinking"},
                {"type": "text", "text": "answer"},
                {
                    "type": "step-finish",
                    "id": "s1",
                    "reason": "stop",
                    "tokens": {"input": 50, "output": 30, "total": 80},
                    "cost": 0.01,
                },
            ],
        },
        {
            "info": {
                "role": "assistant",
                "tokens": {"input": 70, "output": 20, "reasoning": 4, "total": 94},
                "finish": "length",
                "cost": 0.005,
                "time": {"created": 4, "completed": 5},
            },
            "parts": [
                {"type": "step-start", "id": "s2"},
                {"type": "step-finish", "id": "s2", "reason": "length"},
            ],
        },
        {
            "info": {
                "role": "assistant",
                "tokens": {"input": 100, "output": 5, "reasoning": 0, "total": 105},
                "finish": "stop",
                "cost": 0.001,
                "time": {"created": 6, "completed": 7},
            },
            "parts": [
                {"type": "step-start", "id": "s3"},
                {"type": "error", "message": "boom"},
                {
                    "type": "step-finish",
                    "id": "s3",
                    "reason": "stream-incomplete",
                },
            ],
        },
    ]


def test_extract_transcript_metrics_realistic():
    m = extract_transcript_metrics(_realistic_transcript())
    # step-finish parts across assistant messages: s1(stop), s2(length), s3(stream-incomplete)
    assert m["turns"] == 3
    # max input across assistants: 50,70,100 -> 100
    assert m["input_tokens"] == 100
    # sum output: 30+20+5
    assert m["output_tokens"] == 55
    # sum reasoning: 12+4+0
    assert m["reasoning_tokens"] == 16
    # sum cost: 0.01+0.005+0.001
    assert abs(m["cost_usd"] - 0.016) < 1e-9
    # truncations: length + stream-incomplete = 2
    assert m["truncations"] == 2
    # last step-finish reason seen is stream-incomplete
    assert m["last_finish"] == "stream-incomplete"
    # error parts: one "error" part
    assert m["error_parts"] == 1
    assert m["assistant_messages"] == 3
    assert m["user_messages"] == 1


def test_extract_transcript_metrics_empty():
    m = extract_transcript_metrics([])
    assert m == {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "truncations": 0,
        "last_finish": None,
        "error_parts": 0,
        "assistant_messages": 0,
        "user_messages": 0,
    }


def test_extract_transcript_metrics_malformed():
    # Non-list / list of non-dicts / missing keys must not raise.
    assert extract_transcript_metrics(None)["turns"] == 0
    m = extract_transcript_metrics([None, "nope", 42, {"info": None}])
    assert m["assistant_messages"] == 0
    assert m["user_messages"] == 0
    assert m["turns"] == 0


def test_extract_transcript_metrics_missing_keys():
    msg = {"info": {"role": "assistant"}, "parts": [{"type": "step-finish"}]}
    m = extract_transcript_metrics([msg])
    assert m["turns"] == 1
    assert m["input_tokens"] == 0
    assert m["output_tokens"] == 0
    assert m["last_finish"] == "unknown"  # reason None -> classified "unknown"


# ---------------------------------------------------------------------------
# classify_transport_anomaly
# ---------------------------------------------------------------------------
def test_classify_transport_anomaly_truncated():
    assert classify_transport_anomaly({"truncations": 1, "error_parts": 0}) == (
        "truncated",
        "stream-incomplete",
    )


def test_classify_transport_anomaly_error():
    assert classify_transport_anomaly({"truncations": 0, "error_parts": 1}) == (
        "transport_error",
        "error_event",
    )


def test_classify_transport_anomaly_clean():
    assert classify_transport_anomaly({"truncations": 0, "error_parts": 0}) == (
        None,
        None,
    )


# ---------------------------------------------------------------------------
# founder_attach_command
# ---------------------------------------------------------------------------
def test_founder_attach_command():
    assert founder_attach_command(4096) == "opencode attach http://127.0.0.1:4096"


# ---------------------------------------------------------------------------
# ServeClient (IO injected via monkeypatched module helpers)
# ---------------------------------------------------------------------------
def _fake_json(monkeypatch, responses):
    """Pop a (payload) from ``responses`` per call to ``_http_json``."""
    calls = []

    def fake(method, url, body=None, timeout=5.0):
        calls.append((method, url, body))
        if isinstance(responses, Exception):
            raise responses
        return responses.pop(0)

    monkeypatch.setattr("wevibe_bench.serve_client._http_json", fake)
    return calls


def test_create_session_parses_id(monkeypatch):
    calls = _fake_json(monkeypatch, [{"id": "ses_abc"}])
    client = ServeClient("http://127.0.0.1:4096")
    assert client.create_session() == "ses_abc"
    method, url, body = calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:4096/session"
    assert body == {}


def test_create_session_missing_id(monkeypatch):
    _fake_json(monkeypatch, [{}])
    with pytest.raises(ServeClientError):
        ServeClient("http://127.0.0.1:4096").create_session()


def test_send_prompt_accepts_204(monkeypatch):
    seen = {}

    def fake(method, url, body=None, timeout=5.0):
        seen.update(method=method, url=url, body=body)
        return 204

    monkeypatch.setattr("wevibe_bench.serve_client._http_status", fake)
    client = ServeClient("http://127.0.0.1:4096")
    client.send_prompt("ses_1", "run it")
    assert seen["method"] == "POST"
    assert seen["body"] == build_prompt_body("run it")
    assert "/ses_1/prompt_async" in seen["url"]


def test_send_prompt_rejects_non_204(monkeypatch):
    monkeypatch.setattr(
        "wevibe_bench.serve_client._http_status",
        lambda method, url, body=None, timeout=5.0: 500,
    )
    with pytest.raises(ServeClientError):
        ServeClient("http://127.0.0.1:4096").send_prompt("ses_1", "x")


def test_session_busy_parses(monkeypatch):
    _fake_json(monkeypatch, [{"ses_1": {"type": "busy"}}])
    assert ServeClient("http://127.0.0.1:4096").session_busy("ses_1") is True


def test_session_busy_idle(monkeypatch):
    _fake_json(monkeypatch, [{}])
    assert ServeClient("http://127.0.0.1:4096").session_busy("ses_1") is False


def test_get_messages(monkeypatch):
    _fake_json(monkeypatch, [[{"info": {"role": "assistant"}, "parts": []}]])
    msgs = ServeClient("http://127.0.0.1:4096").get_messages("ses_1")
    assert msgs[0]["info"]["role"] == "assistant"


def test_wait_idle_returns_true(monkeypatch):
    # First poll busy, second poll idle -> True.
    busy = iter([True, False])
    monkeypatch.setattr(
        "wevibe_bench.serve_client.ServeClient.session_busy",
        lambda self, sid: next(busy),
    )
    client = ServeClient("http://127.0.0.1:4096", poll_interval=0.0)
    assert client.wait_idle("ses_1", timeout_s=5) is True


def test_wait_idle_times_out(monkeypatch):
    monkeypatch.setattr(
        "wevibe_bench.serve_client.ServeClient.session_busy",
        lambda self, sid: True,
    )
    client = ServeClient("http://127.0.0.1:4096", poll_interval=0.0)
    assert client.wait_idle("ses_1", timeout_s=0.05) is False


def test_metrics_composition(monkeypatch):
    transcript = [
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "step-finish", "reason": "stop"}],
        }
    ]
    _fake_json(monkeypatch, [transcript])
    m = ServeClient("http://127.0.0.1:4096").metrics("ses_1")
    assert m["turns"] == 1
    assert m["last_finish"] == "stop"


def test_http_error_wraps_serve_client_error(monkeypatch):
    from wevibe_bench import serve_client as sc

    def boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(sc.urllib.request, "urlopen", boom)
    with pytest.raises(ServeClientError):
        sc._http_json("GET", "http://127.0.0.1:4096/session/status")
    with pytest.raises(ServeClientError):
        sc._http_status("POST", "http://127.0.0.1:4096/session")