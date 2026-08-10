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


def test_extract_transcript_metrics_ignores_empty_placeholder():
    # One bare step-finish placeholder (no tokens/text/tool) + one real
    # assistant message carrying a text part -> turns == 1.
    transcript = [
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "step-finish", "reason": "stop"}],
        },
        {
            "info": {"role": "assistant", "tokens": {"output": 0, "reasoning": 0}},
            "parts": [{"type": "text", "text": "real answer"}],
        },
    ]
    m = extract_transcript_metrics(transcript)
    assert m["turns"] == 1
    assert m["assistant_messages"] == 2


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
        "info_errors": 0,
        "guard_aborted_turns": 0,
        "finalize_timeouts": 0,
        "error_texts": [],
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
    # Bare step-finish placeholder: no tokens, no text/tool -> NOT a turn, but
    # the missing keys must not raise and must default to safe zeros.
    msg = {"info": {"role": "assistant"}, "parts": [{"type": "step-finish"}]}
    m = extract_transcript_metrics([msg])
    assert m["turns"] == 0
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
# WO-LOOPREC-1: error-text capture + loop-guard classification
# ---------------------------------------------------------------------------
def _loop_killed_transcript():
    """The relay StreamLoopGuard kill as opencode 1.18.x actually persists it:
    ``info.error`` on the assistant message (processor halt path), NOT an
    "error" part (verified against pinned 1.18.1 source + a live 1.18.15
    session DB — zero error parts, 2113 info.error rows)."""
    return [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "fix it"}]},
        {
            "info": {
                "role": "assistant",
                "tokens": {"input": 500, "output": 300, "reasoning": 50, "total": 850},
                "finish": "error",
                "cost": 0.0,
                "error": {
                    "name": "UnknownError",
                    "data": {
                        # Live-observed shape (2026-08-10 runs): the relay stamps
                        # a per-request trace id in the parens — placeholder here.
                        "message": "relay: generation loop detected (<request-id>)"
                    },
                },
            },
            "parts": [
                {"type": "step-start", "id": "s1"},
                {"type": "text", "text": "repeated repeated repeated"},
            ],
        },
    ]


def test_extract_transcript_metrics_captures_info_error_text():
    m = extract_transcript_metrics(_loop_killed_transcript())
    assert m["info_errors"] == 1
    assert m["error_parts"] == 0
    assert len(m["error_texts"]) == 1
    assert "generation loop detected" in m["error_texts"][0]
    # The killed message carries a text part -> it is a real turn, so the
    # turn-aligned exclusion count records it (WO-TURNACCT-1).
    assert m["guard_aborted_turns"] == 1
    assert m["finalize_timeouts"] == 0
    # The looped turn still meters (turn accounting unchanged).
    assert m["turns"] == 1
    assert m["output_tokens"] == 300


def test_extract_transcript_metrics_error_texts_bounded():
    long_msg = "x" * 500 + " relay_loop_detected"
    msgs = [
        {
            "info": {
                "role": "assistant",
                "error": {"name": "UnknownError", "data": {"message": long_msg}},
            },
            "parts": [],
        }
        for _ in range(12)
    ]
    m = extract_transcript_metrics(msgs)
    assert m["info_errors"] == 12  # count is exact
    assert len(m["error_texts"]) == 8  # capture is capped
    assert all(len(t) <= 240 for t in m["error_texts"])  # and truncated
    # Turn-aligned: a killed message with no text/tool parts is not a real
    # turn, so it must NOT enter the exclusion count (WO-TURNACCT-1) — the
    # subtraction downstream keys on ``turns``, which never counted these.
    assert m["guard_aborted_turns"] == 0


def test_extract_transcript_metrics_guard_aborted_count_is_exact_and_turn_aligned():
    # 12 guard-killed REAL turns (text part each): the exclusion count is
    # exact even though the error_texts capture caps at 8.
    msgs = [
        {
            "info": {
                "role": "assistant",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": "relay: generation loop detected (<request-id>)"},
                },
            },
            "parts": [{"type": "text", "text": "repeated"}],
        }
        for _ in range(12)
    ]
    m = extract_transcript_metrics(msgs)
    assert m["guard_aborted_turns"] == 12
    assert m["turns"] == 12
    assert len(m["error_texts"]) == 8


def test_extract_transcript_metrics_finalize_timeout_counted_separately():
    m = extract_transcript_metrics(
        [
            {
                "info": {
                    "role": "assistant",
                    "error": {
                        "name": "UnknownError",
                        "data": {
                            "message": "relay: upstream completed but the stream "
                            "did not finalize within 30000ms (<request-id>)"
                        },
                    },
                },
                "parts": [{"type": "text", "text": "partial work"}],
            }
        ]
    )
    assert m["finalize_timeouts"] == 1
    assert m["guard_aborted_turns"] == 0


def test_extract_transcript_metrics_error_part_text_still_captured():
    m = extract_transcript_metrics(
        [
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "error", "message": "relay_loop_detected n=40 limit=3"}],
            }
        ]
    )
    assert m["error_parts"] == 1
    assert m["info_errors"] == 0
    assert m["error_texts"] == ["relay_loop_detected n=40 limit=3"]


def test_classify_transport_anomaly_loop_guard_from_info_error_text():
    m = extract_transcript_metrics(_loop_killed_transcript())
    assert classify_transport_anomaly(m) == ("guard_abort", "loop_guard")


def test_classify_transport_anomaly_loop_guard_beats_truncation():
    assert classify_transport_anomaly(
        {
            "truncations": 1,
            "error_parts": 0,
            "info_errors": 1,
            "error_texts": ["relay_loop_detected n=40 limit=3"],
        }
    ) == ("guard_abort", "loop_guard")


def test_classify_transport_anomaly_loop_guard_legacy_shape_still_matches():
    # The older proxy build's literal signature (pinned stdout fixture shape)
    # must keep classifying as the guard terminal alongside the live shape.
    assert classify_transport_anomaly(
        {
            "truncations": 0,
            "error_parts": 0,
            "info_errors": 1,
            "error_texts": ["relay_loop_detected n=40 limit=3"],
        }
    ) == ("guard_abort", "loop_guard")


def test_classify_transport_anomaly_finalize_timeout_is_not_loop_guard():
    # The relay's 30s stream-finalize watchdog (observed 2x in the 2026-08-10
    # run's worker DB) is a transport death, NOT a repetition guard kill: it
    # gets its own reason so recovery picks the resume nudge, never the
    # anti-repetition nudge.
    assert classify_transport_anomaly(
        {
            "truncations": 0,
            "error_parts": 0,
            "info_errors": 1,
            "error_texts": [
                "relay: upstream completed but the stream did not finalize "
                "within 30000ms (<request-id>)"
            ],
        }
    ) == ("transport_error", "stream_finalize_timeout")


def test_classify_transport_anomaly_info_error_without_signature_is_transport():
    assert classify_transport_anomaly(
        {
            "truncations": 0,
            "error_parts": 0,
            "info_errors": 1,
            "error_texts": ["relay: stream incomplete (a03b34fe888a4c739dbb0fb2c122ec25)"],
        }
    ) == ("transport_error", "error_event")


# ---------------------------------------------------------------------------
# founder_attach_command
# ---------------------------------------------------------------------------
def test_founder_attach_command():
    assert founder_attach_command(4096) == "opencode attach http://127.0.0.1:4096"
    assert founder_attach_command(4096, session_id="ses_abc") == (
        "opencode attach http://127.0.0.1:4096 --session ses_abc"
    )


def test_last_assistant_text_returns_newest_assistant_text_only():
    from wevibe_bench.serve_client import ServeClient

    client = ServeClient(base_url="http://127.0.0.1:1", timeout=0.1, poll_interval=0.01)
    messages = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "chunk one"}]},
        {
            "info": {"role": "assistant"},
            "parts": [
                {"type": "text", "text": "scaffold written. "},
                {"type": "tool", "text": "ignored"},
                {"type": "text", "text": "CHUNK FINISHED"},
            ],
        },
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "chunk two"}]},
    ]
    client.get_messages = lambda session_id: messages  # type: ignore[method-assign]
    assert client.last_assistant_text("ses_x") == "scaffold written. CHUNK FINISHED"

    client.get_messages = lambda session_id: []  # type: ignore[method-assign]
    assert client.last_assistant_text("ses_x") == ""


def test_assistant_texts_since_scans_only_the_window():
    client = ServeClient(base_url="http://127.0.0.1:1", timeout=0.1, poll_interval=0.01)
    messages = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "chunk one"}]},
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "text", "text": "old chunk. CHUNK FINISHED"}],
        },
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "chunk two"}]},
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "text", "text": "CHUNK FINISHED"}],
        },
        {
            "info": {"role": "assistant"},
            "parts": [
                {"type": "tool", "text": "ignored"},
                {"type": "text", "text": "WEVIBE_DISCOVERY: something"},
            ],
        },
    ]
    client.get_messages = lambda session_id: messages  # type: ignore[method-assign]
    # watermark=2: the prior chunk's marker (index 1) is excluded; both new
    # assistant messages are returned in order (marker before the discovery
    # block here — the loop's any() scan catches either order).
    assert client.assistant_texts_since("ses_x", 2) == [
        "CHUNK FINISHED",
        "WEVIBE_DISCOVERY: something",
    ]
    assert client.assistant_texts_since("ses_x", len(messages)) == []


def test_compaction_since_detects_compaction_part_in_window_only():
    client = ServeClient(base_url="http://127.0.0.1:1", timeout=0.1, poll_interval=0.01)
    messages = [
        {"info": {"role": "user"}, "parts": [{"type": "compaction"}]},
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "chunk two"}]},
    ]
    client.get_messages = lambda session_id: messages  # type: ignore[method-assign]
    assert client.compaction_since("ses_x", 0) is True
    # watermark past the compaction parent: no evidence in-window.
    assert client.compaction_since("ses_x", 1) is False


def test_metrics_since_windows_the_classification_surface():
    """A killed turn's info.error persists in the transcript FOREVER: the
    cumulative read keeps matching it (metering never forgets), while the
    windowed read — the anomaly-classification surface — excludes it. This is
    the 2026-08-10 chunk-2 fix: a recovered, marker-landing drive must not be
    reclassified by the stale kill."""
    client = ServeClient(base_url="http://127.0.0.1:1", timeout=0.1, poll_interval=0.01)
    messages = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "chunk one"}]},
        {
            "info": {
                "role": "assistant",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": "relay: generation loop detected (req-1)"},
                },
            },
            "parts": [{"type": "step-start"}],
        },
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "nudge"}]},
        {
            "info": {
                "role": "assistant",
                "tokens": {"input": 10, "output": 5, "reasoning": 0},
            },
            "parts": [{"type": "text", "text": "recovered work. CHUNK FINISHED"}],
        },
    ]
    client.get_messages = lambda session_id: messages  # type: ignore[method-assign]
    cumulative = client.metrics("ses_x")
    assert cumulative["info_errors"] == 1
    assert classify_transport_anomaly(cumulative) == ("guard_abort", "loop_guard")
    windowed = client.metrics("ses_x", since=2)
    assert windowed["info_errors"] == 0
    assert windowed["output_tokens"] == 5
    assert classify_transport_anomaly(windowed) == (None, None)


def test_session_model_skips_compaction_parents():
    client = ServeClient(base_url="http://127.0.0.1:1", timeout=0.1, poll_interval=0.01)
    messages = [
        {
            "info": {
                "role": "user",
                "model": {"providerID": "local-llm-proxy", "modelID": "kimi/kimi-k3"},
            },
            "parts": [{"type": "text", "text": "chunk one"}],
        },
        {
            "info": {"role": "user"},
            "parts": [{"type": "compaction"}],
        },
    ]
    client.get_messages = lambda session_id: messages  # type: ignore[method-assign]
    assert client.session_model("ses_x") == ("local-llm-proxy", "kimi/kimi-k3")

    client.get_messages = lambda session_id: [  # type: ignore[method-assign]
        {"info": {"role": "user"}, "parts": [{"type": "compaction"}]}
    ]
    assert client.session_model("ses_x") is None


def test_summarize_posts_required_body(monkeypatch):
    import wevibe_bench.serve_client as sc

    calls: list[tuple[str, str, object, float]] = []

    def fake_status(method, url, body=None, timeout=5.0):
        calls.append((method, url, body, timeout))
        return 200

    monkeypatch.setattr(sc, "_http_status", fake_status)
    client = ServeClient(base_url="http://127.0.0.1:9", timeout=0.1, poll_interval=0.01)
    client.summarize("ses_abc", provider_id="local-llm-proxy", model_id="kimi/kimi-k3")
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:9/session/ses_abc/summarize",
            {"providerID": "local-llm-proxy", "modelID": "kimi/kimi-k3", "auto": False},
            1800.0,
        )
    ]

    def raising_status(method, url, body=None, timeout=5.0):
        raise ServeClientError("boom")

    monkeypatch.setattr(sc, "_http_status", raising_status)
    with pytest.raises(ServeClientError):
        client.summarize("ses_abc", provider_id="p", model_id="m")


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


def test_abort_accepts_2xx(monkeypatch):
    seen = {}

    def fake(method, url, body=None, timeout=5.0):
        seen.update(method=method, url=url, body=body)
        return 200

    monkeypatch.setattr("wevibe_bench.serve_client._http_status", fake)
    client = ServeClient("http://127.0.0.1:4096")
    assert client.abort("ses_1") is None
    assert seen["method"] == "POST"
    assert seen["body"] is None
    assert seen["url"] == "http://127.0.0.1:4096/session/ses_1/abort"


def test_abort_rejects_non_2xx(monkeypatch):
    monkeypatch.setattr(
        "wevibe_bench.serve_client._http_status",
        lambda method, url, body=None, timeout=5.0: 500,
    )
    with pytest.raises(ServeClientError):
        ServeClient("http://127.0.0.1:4096").abort("ses_1")


def test_abort_wraps_transport_error(monkeypatch):
    from wevibe_bench import serve_client as sc

    def boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(sc.urllib.request, "urlopen", boom)
    with pytest.raises(ServeClientError):
        ServeClient("http://127.0.0.1:4096").abort("ses_1")


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
    # Bare step-finish with no tokens/text/tool is a placeholder -> 0 turns.
    assert m["turns"] == 0
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