from __future__ import annotations

import json

from wevibe_bench.contention import ContentionCovariates
from wevibe_bench.proxy_meter import SpendMeter
from wevibe_bench.scorecard import Cell


class _FakeCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_contention_covariates_aggregate_from_spend_events(monkeypatch) -> None:
    cursor = _FakeCursor((2, 1, 3, 900, [100, 300, 900, 1100]))

    def fake_connect(dsn: str, *, connect_timeout: int) -> _FakeConnection:
        assert dsn == "postgresql://bench"
        assert connect_timeout == 5
        return _FakeConnection(cursor)

    monkeypatch.setattr("wevibe_bench.proxy_meter.psycopg.connect", fake_connect)

    covariates = SpendMeter("postgresql://bench").contention_covariates(
        "sess-1",
        retry_count=4,
        wall_seconds=5293.0,
        wall_near_timeout=True,
    )

    assert covariates == ContentionCovariates(
        http_429_count=2,
        http_402_count=1,
        retry_count=4,
        upstream_error_count=3,
        max_request_ms=900,
        median_request_ms=600,
        wall_seconds=5293.0,
        wall_near_timeout=True,
    )
    sql, params = cursor.executed[0]
    assert params == ("sess-1",)
    assert "FROM spend_events WHERE session_id=%s" in sql
    assert "COUNT(*) FILTER (WHERE upstream_status=429)" in sql
    assert "COUNT(*) FILTER (WHERE upstream_status=402)" in sql
    assert "COUNT(*) FILTER (WHERE err IS NOT NULL)" in sql
    assert sql.lstrip().upper().startswith("SELECT")


def test_contention_covariates_empty_without_session_id() -> None:
    covariates = SpendMeter("postgresql://bench").contention_covariates(
        None,
        retry_count=2,
        wall_seconds=12.5,
        wall_near_timeout=False,
    )

    assert covariates == ContentionCovariates.empty(
        retry_count=2,
        wall_seconds=12.5,
        wall_near_timeout=False,
    )


def test_cell_serializes_flat_contention_fields() -> None:
    cell = Cell(
        model="model-a",
        task_id="backgammon",
        condition="ON",
        resolved=True,
        input_tokens=10,
        output_tokens=20,
        turns=3,
        wall_cost_usd=0.12,
        wall_seconds=44.0,
        delivery="YES",
        scored=True,
        http_429_count=2,
        http_402_count=1,
        retry_count=4,
        upstream_error_count=3,
        max_request_ms=1100,
        median_request_ms=600,
        wall_near_timeout=True,
    )

    payload = cell.to_dict()
    assert payload["http_429_count"] == 2
    assert payload["http_402_count"] == 1
    assert payload["retry_count"] == 4
    assert payload["upstream_error_count"] == 3
    assert payload["max_request_ms"] == 1100
    assert payload["median_request_ms"] == 600
    assert payload["wall_near_timeout"] is True
    assert json.loads(json.dumps(payload))["median_request_ms"] == 600
