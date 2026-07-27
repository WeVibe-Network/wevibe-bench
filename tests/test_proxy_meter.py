from __future__ import annotations

import datetime as dt
import logging
from urllib.error import HTTPError

import pytest

from wevibe_bench.proxy_meter import ModelIdentity, PricingVerdict, SpendMeter, verify_pricing


class _FakeCursor:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> object:
        return self._responses.pop(0)

    def fetchall(self) -> object:
        return self._responses.pop(0)


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_run_spend_maps_row_and_is_select_only(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    ts = dt.datetime(2026, 7, 27, 13, 45, tzinfo=dt.timezone.utc)
    cursor = _FakeCursor(
        responses=[
            (
                7,
                "1.23456789",
                "2.34567890",
                110,
                12,
                90,
                5,
                1,
                ts,
            )
        ]
    )

    def fake_connect(dsn: str, *, connect_timeout: int) -> _FakeConnection:
        assert dsn == "postgresql://bench"
        assert connect_timeout == 5
        return _FakeConnection(cursor)

    monkeypatch.setattr("wevibe_bench.proxy_meter.psycopg.connect", fake_connect)
    caplog.set_level(logging.INFO)

    meter = SpendMeter("postgresql://bench")
    result = meter.run_spend("sess-1")

    assert result.calls == 7
    assert result.true_usd == pytest.approx(1.23456789)
    assert result.benchmark_usd == pytest.approx(2.3456789)
    assert result.uncached_input_tokens == 110
    assert result.cached_input_tokens == 12
    assert result.output_tokens == 90
    assert result.reasoning_tokens == 5
    assert result.unmetered_calls == 1
    assert result.last_call_at == ts.isoformat()

    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert params == ("sess-1",)
    assert "FROM spend_events WHERE session_id=%s" in sql
    assert sql.lstrip().upper().startswith("SELECT")
    for forbidden in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE"):
        assert forbidden not in sql.upper()

    assert "session_id=sess-1" in caplog.text
    assert "true_usd=1.23456789" in caplog.text
    assert "benchmark_usd=2.34567890" in caplog.text


def test_model_identity_and_mismatch_basename_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ("kimi/kimi-k3", "kimi/kimi-k3", 2),
        ("kimi/kimi-k3", "kimi-k3", 3),
        ("kimi/kimi-k3", "glm-5.2", 5),
        ("kimi/kimi-k3", None, 7),
    ]
    cursor = _FakeCursor(responses=[rows, rows])

    def fake_connect(dsn: str, *, connect_timeout: int) -> _FakeConnection:
        assert dsn == "postgresql://bench"
        assert connect_timeout == 5
        return _FakeConnection(cursor)

    monkeypatch.setattr("wevibe_bench.proxy_meter.psycopg.connect", fake_connect)
    meter = SpendMeter("postgresql://bench")

    identity = meter.model_identity("sess-2")
    assert identity == [
        ModelIdentity(model="kimi/kimi-k3", upstream_model="kimi/kimi-k3", calls=2),
        ModelIdentity(model="kimi/kimi-k3", upstream_model="kimi-k3", calls=3),
        ModelIdentity(model="kimi/kimi-k3", upstream_model="glm-5.2", calls=5),
        ModelIdentity(model="kimi/kimi-k3", upstream_model=None, calls=7),
    ]

    mismatches = meter.model_identity_mismatches("sess-2")
    assert mismatches == [
        ModelIdentity(model="kimi/kimi-k3", upstream_model="glm-5.2", calls=5),
        ModelIdentity(model="kimi/kimi-k3", upstream_model=None, calls=7),
    ]

    assert len(cursor.executed) == 2
    for sql, params in cursor.executed:
        assert params == ("sess-2",)
        assert "FROM spend_events WHERE session_id=%s" in sql
        assert sql.lstrip().upper().startswith("SELECT")


def test_verify_pricing_gate_down_503(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def fake_urlopen(req, timeout: float):
        url = req.full_url
        if url.endswith("/health"):
            return _Resp({"ok": True, "db_ok": True, "pricing_ok": True, "pricing_version": "v1"})
        raise HTTPError(url=url, code=503, msg="Service Unavailable", hdrs=None, fp=None)

    monkeypatch.setattr("wevibe_bench.proxy_meter.urlopen", fake_urlopen)
    caplog.set_level(logging.INFO)

    verdict = verify_pricing(
        roster_models=["kimi/kimi-k3"],
        expected_version="v1",
        base_url="http://127.0.0.1:4480/v1",
        bearer_token="super-secret-token",
    )

    assert verdict == PricingVerdict(ok=False, version="v1", missing_models=[], reason="gate-down")
    assert "super-secret-token" not in caplog.text


def test_verify_pricing_version_mismatch_names_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_urlopen(req, timeout: float):
        calls.append((req.full_url, req.headers.get("Authorization")))
        if req.full_url.endswith("/health"):
            return _Resp({"ok": True, "db_ok": True, "pricing_ok": True, "pricing_version": "actual-v"})
        return _Resp(
            {
                "pricing": {"ok": True, "version": "actual-v"},
                "models": {"kimi/kimi-k3": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}},
            }
        )

    monkeypatch.setattr("wevibe_bench.proxy_meter.urlopen", fake_urlopen)
    verdict = verify_pricing(
        roster_models=["kimi/kimi-k3"],
        expected_version="wanted-v",
        base_url="http://127.0.0.1:4480/v1",
        bearer_token="token-123",
    )

    assert verdict.ok is False
    assert verdict.version == "actual-v"
    assert verdict.missing_models == []
    assert verdict.reason == "version-mismatch got actual-v want wanted-v"
    assert len(calls) == 2
    assert calls[1][1] == "Bearer token-123"


def test_verify_pricing_missing_models(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout: float):
        if req.full_url.endswith("/health"):
            return _Resp({"ok": True, "db_ok": True, "pricing_ok": True, "pricing_version": "v-good"})
        return _Resp(
            {
                "pricing": {"ok": True, "version": "v-good"},
                "models": {"kimi/kimi-k3": {"input": 1, "output": 1}},
            }
        )

    monkeypatch.setattr("wevibe_bench.proxy_meter.urlopen", fake_urlopen)
    verdict = verify_pricing(
        roster_models=["kimi/kimi-k3", "openai/gpt-5.2"],
        expected_version="v-good",
        base_url="http://127.0.0.1:4480/v1",
        bearer_token="token-456",
    )
    assert verdict.ok is False
    assert verdict.version == "v-good"
    assert verdict.missing_models == ["openai/gpt-5.2"]
    assert verdict.reason == "missing models: openai/gpt-5.2"


def test_verify_pricing_all_good_and_resolves_bearer(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    seen_auth: list[str | None] = []

    def fake_urlopen(req, timeout: float):
        if req.full_url.endswith("/health"):
            return _Resp({"ok": True, "db_ok": True, "pricing_ok": True, "pricing_version": "v-good"})
        seen_auth.append(req.headers.get("Authorization"))
        return _Resp(
            {
                "pricing": {"ok": True, "version": "v-good"},
                "models": {
                    "kimi/kimi-k3": {"input": 1, "output": 1},
                    "openai/gpt-5.2": {"input": 2, "output": 2},
                },
            }
        )

    monkeypatch.setattr("wevibe_bench.proxy_meter.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "wevibe_bench.proxy_meter.resolve_orcarouter_api_key",
        lambda: ("resolved-secret", "dotenv"),
    )
    caplog.set_level(logging.INFO)

    verdict = verify_pricing(
        roster_models=["kimi/kimi-k3", "openai/gpt-5.2"],
        expected_version="v-good",
        base_url="http://127.0.0.1:4480/v1",
        bearer_token=None,
    )

    assert verdict == PricingVerdict(ok=True, version="v-good", missing_models=[], reason="ok")
    assert seen_auth == ["Bearer resolved-secret"]
    assert "resolved-secret" not in caplog.text
    assert "token_fp=" in caplog.text


class _Resp:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = __import__("json").dumps(payload).encode("utf-8")

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body
