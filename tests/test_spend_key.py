from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from wevibe_bench.spend_key import (
    DEFAULT_WORKER_SPEND_PROXY_BASE_URL,
    SpendKeyError,
    _read_dotenv,
    key_fingerprint,
    resolve_orcarouter_api_key,
    resolve_spend_db_dsn,
    resolve_spend_proxy_base_url,
    resolve_worker_spend_proxy_base_url,
)












def test_resolve_spend_proxy_base_url_defaults_and_overrides(tmp_path: Path) -> None:
    default = resolve_spend_proxy_base_url(env={}, dotenv_path=tmp_path / ".env")
    assert default == "http://127.0.0.1:4545/v1"

    dotenv = tmp_path / ".env"
    dotenv.write_text("WEVIBE_BENCH_SPEND_PROXY_BASE_URL=http://from-dotenv/v1\n", encoding="utf-8")
    assert resolve_spend_proxy_base_url(env={}, dotenv_path=dotenv) == "http://from-dotenv/v1"
    assert (
        resolve_spend_proxy_base_url(
            env={"WEVIBE_BENCH_SPEND_PROXY_BASE_URL": "http://from-env/v1"},
            dotenv_path=dotenv,
        )
        == "http://from-env/v1"
    )


def test_resolve_worker_spend_proxy_base_url_defaults_and_overrides(tmp_path: Path) -> None:
    default = resolve_worker_spend_proxy_base_url(env={}, dotenv_path=tmp_path / ".env")
    assert default == "http://host.docker.internal:4545/v1"
    assert default == DEFAULT_WORKER_SPEND_PROXY_BASE_URL

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL=http://from-dotenv/v1\n",
        encoding="utf-8",
    )
    assert resolve_worker_spend_proxy_base_url(env={}, dotenv_path=dotenv) == "http://from-dotenv/v1"
    assert (
        resolve_worker_spend_proxy_base_url(
            env={"WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL": "http://from-env/v1"},
            dotenv_path=dotenv,
        )
        == "http://from-env/v1"
    )

    assert (
        resolve_worker_spend_proxy_base_url(
            env={"WEVIBE_BENCH_SPEND_PROXY_BASE_URL": "http://127.0.0.1:4545/v1"},
            dotenv_path=tmp_path / "missing.env",
        )
        == DEFAULT_WORKER_SPEND_PROXY_BASE_URL
    )


def test_key_fingerprint_returns_sha256_first8_not_raw_token() -> None:
    token = "bench-token-abc123"
    fp = key_fingerprint(token)
    assert fp == hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    assert fp != token


def test_dotenv_parser_handles_quotes_comments_blanks_export_and_expansion(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n"
        "# comment\n"
        "export BASE=alpha\n"
        "A=' spaced value '\n"
        'B="${BASE}-beta"\n'
        "C=$BASE-gamma\n"
        "MISSING_EQUALS\n",
        encoding="utf-8",
    )
    values = _read_dotenv(dotenv, env={})
    assert values["BASE"] == "alpha"
    assert values["A"] == " spaced value "
    assert values["B"] == "alpha-beta"
    assert values["C"] == "alpha-gamma"
    assert "MISSING_EQUALS" not in values


def test_dotenv_parser_returns_empty_dict_for_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.env"
    assert _read_dotenv(missing, env={}) == {}


def test_dotenv_setdefault_semantics_first_wins(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ORCAROUTER_API_KEY=first\nORCAROUTER_API_KEY=second\n", encoding="utf-8")
    token, source = resolve_orcarouter_api_key(
        env={},
        dotenv_path=dotenv,
        opencode_config_path=tmp_path / "missing-opencode.json",
    )
    assert token == "first"
    assert source == "dotenv"


def test_repo_gitignore_includes_dotenv() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    gitignore = repo_root / ".gitignore"
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    assert ".env" in lines
