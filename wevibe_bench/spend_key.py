"""Spend-proxy key/config resolution for wevibe-bench.

This module keeps bench configuration minimal for open-source users while
failing loudly when required auth is missing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Mapping

from wevibe_bench.adapters.openrouter_proxy import key_fingerprint as _proxy_key_fingerprint


logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DOTENV_PATH = _REPO_ROOT / ".env"
_DEFAULT_OPENCODE_CONFIG_PATH = Path("~/.config/opencode/opencode.json")
_DEFAULT_SPEND_DB_DSN = "postgresql://spend_proxy:spend_proxy_dev@127.0.0.1:5440/spend_proxy"
_DEFAULT_SPEND_PROXY_BASE_URL = "http://127.0.0.1:4480/v1"
# Container-facing default for worker cells; Docker launch adds
# --add-host host.docker.internal:host-gateway (docker_worker.py), which is
# reachable on macOS Docker Desktop and Linux.
DEFAULT_WORKER_SPEND_PROXY_BASE_URL = "http://host.docker.internal:4480/v1"
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


class SpendKeyError(RuntimeError):
    """Raised when spend-proxy key resolution fails."""


def key_fingerprint(token: str) -> str:
    """Return first-8 sha256 fingerprint for safe key-identification logs."""
    return _proxy_key_fingerprint(token)


def _expand(value: str, seen: Mapping[str, str], env: Mapping[str, str]) -> str:
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        return seen.get(name, env.get(name, ""))

    return _VAR_RE.sub(repl, value)


def _read_dotenv(path: Path, *, env: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        value = _expand(value, values, env)
        values.setdefault(key, value)
    return values


def _resolve_dotenv_path(
    *, env: Mapping[str, str], dotenv_path: str | os.PathLike[str] | None
) -> Path:
    candidate = dotenv_path if dotenv_path is not None else env.get("WEVIBE_BENCH_DOTENV")
    if candidate is None:
        return _DEFAULT_DOTENV_PATH
    return Path(candidate).expanduser()


def _resolve_opencode_config_path(path: str | os.PathLike[str] | None) -> Path:
    candidate = path if path is not None else _DEFAULT_OPENCODE_CONFIG_PATH
    return Path(candidate).expanduser()


def _read_orcarouter_api_key_from_opencode(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    provider = payload.get("provider")
    if not isinstance(provider, dict):
        return None
    orcarouter = provider.get("orcarouter")
    if not isinstance(orcarouter, dict):
        return None
    options = orcarouter.get("options")
    if not isinstance(options, dict):
        return None
    key = options.get("apiKey")
    if not isinstance(key, str):
        return None
    key = key.strip()
    return key if key else None


def resolve_orcarouter_api_key(
    *,
    env: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
    opencode_config_path: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    """Resolve OrcaRouter spend-proxy token and return (token, source_label)."""
    env_map = os.environ if env is None else env
    env_file = _resolve_dotenv_path(env=env_map, dotenv_path=dotenv_path)
    opencode_file = _resolve_opencode_config_path(opencode_config_path)
    dot = _read_dotenv(env_file, env=env_map)

    from_dotenv = dot.get("ORCAROUTER_API_KEY", "").strip()
    if from_dotenv:
        fp = key_fingerprint(from_dotenv)
        logger.info(
            "spend_key.resolve_orcarouter_api_key outcome=resolved source=dotenv path=%s token_fp=%s",
            str(env_file),
            fp,
        )
        return from_dotenv, "dotenv"

    from_env = str(env_map.get("ORCAROUTER_API_KEY", "")).strip()
    if from_env:
        fp = key_fingerprint(from_env)
        logger.info(
            "spend_key.resolve_orcarouter_api_key outcome=resolved source=env:ORCAROUTER_API_KEY token_fp=%s",
            fp,
        )
        return from_env, "env:ORCAROUTER_API_KEY"

    from_opencode = _read_orcarouter_api_key_from_opencode(opencode_file)
    if from_opencode:
        fp = key_fingerprint(from_opencode)
        logger.info(
            "spend_key.resolve_orcarouter_api_key outcome=resolved source=opencode.json:provider.orcarouter.options.apiKey path=%s token_fp=%s",
            str(opencode_file),
            fp,
        )
        return from_opencode, "opencode.json:provider.orcarouter.options.apiKey"

    env_example = _REPO_ROOT / ".env.example"
    message = (
        "Missing required ORCAROUTER_API_KEY for spend-proxy routing. "
        f"Checked .env path: {env_file}. "
        "Checked process env key: ORCAROUTER_API_KEY (empty/unset). "
        f"Checked OpenCode config path: {opencode_file} "
        "at provider.orcarouter.options.apiKey. "
        f"Create ORCAROUTER_API_KEY in {env_file} (see {env_example})."
    )
    logger.error(
        "spend_key.resolve_orcarouter_api_key outcome=missing source=none dotenv_path=%s opencode_path=%s",
        str(env_file),
        str(opencode_file),
    )
    raise SpendKeyError(message)


def resolve_spend_db_dsn(
    *, env: Mapping[str, str] | None = None, dotenv_path: str | os.PathLike[str] | None = None
) -> str:
    """Resolve spend DB DSN from env, then .env, then local-compose default."""
    env_map = os.environ if env is None else env
    from_env = str(env_map.get("WEVIBE_BENCH_SPEND_DB_DSN", "")).strip()
    if from_env:
        logger.info("spend_key.resolve_spend_db_dsn outcome=resolved source=env:WEVIBE_BENCH_SPEND_DB_DSN")
        return from_env

    env_file = _resolve_dotenv_path(env=env_map, dotenv_path=dotenv_path)
    dot = _read_dotenv(env_file, env=env_map)
    from_dotenv = dot.get("WEVIBE_BENCH_SPEND_DB_DSN", "").strip()
    if from_dotenv:
        logger.info(
            "spend_key.resolve_spend_db_dsn outcome=resolved source=dotenv path=%s",
            str(env_file),
        )
        return from_dotenv

    logger.info("spend_key.resolve_spend_db_dsn outcome=resolved source=default")
    return _DEFAULT_SPEND_DB_DSN


def resolve_spend_proxy_base_url(
    *, env: Mapping[str, str] | None = None, dotenv_path: str | os.PathLike[str] | None = None
) -> str:
    """Resolve spend-proxy base URL from env, then .env, then local default."""
    env_map = os.environ if env is None else env
    from_env = str(env_map.get("WEVIBE_BENCH_SPEND_PROXY_BASE_URL", "")).strip()
    if from_env:
        logger.info(
            "spend_key.resolve_spend_proxy_base_url outcome=resolved source=env:WEVIBE_BENCH_SPEND_PROXY_BASE_URL"
        )
        return from_env

    env_file = _resolve_dotenv_path(env=env_map, dotenv_path=dotenv_path)
    dot = _read_dotenv(env_file, env=env_map)
    from_dotenv = dot.get("WEVIBE_BENCH_SPEND_PROXY_BASE_URL", "").strip()
    if from_dotenv:
        logger.info(
            "spend_key.resolve_spend_proxy_base_url outcome=resolved source=dotenv path=%s",
            str(env_file),
        )
        return from_dotenv

    logger.info("spend_key.resolve_spend_proxy_base_url outcome=resolved source=default")
    return _DEFAULT_SPEND_PROXY_BASE_URL


def resolve_worker_spend_proxy_base_url(
    *, env: Mapping[str, str] | None = None, dotenv_path: str | os.PathLike[str] | None = None
) -> str:
    """Resolve the CONTAINER-facing spend-proxy base URL (worker opencode.json baseURL).

    Workers run inside Docker cells where 127.0.0.1 is the container's own
    loopback. Cells launch with --add-host host.docker.internal:host-gateway,
    so host.docker.internal reaches the host-published proxy. Resolution:
    env WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL, then .env, then default.
    Deliberately NO fallback to the host-facing WEVIBE_BENCH_SPEND_PROXY_BASE_URL
    (that would re-import the dead-loopback bug; R-13 one path per context).
    """
    env_map = os.environ if env is None else env
    from_env = str(env_map.get("WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL", "")).strip()
    if from_env:
        logger.info(
            "spend_key.resolve_worker_spend_proxy_base_url outcome=resolved source=env:WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL"
        )
        return from_env

    env_file = _resolve_dotenv_path(env=env_map, dotenv_path=dotenv_path)
    dot = _read_dotenv(env_file, env=env_map)
    from_dotenv = dot.get("WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL", "").strip()
    if from_dotenv:
        logger.info(
            "spend_key.resolve_worker_spend_proxy_base_url outcome=resolved source=dotenv path=%s",
            str(env_file),
        )
        return from_dotenv

    logger.info("spend_key.resolve_worker_spend_proxy_base_url outcome=resolved source=default")
    return DEFAULT_WORKER_SPEND_PROXY_BASE_URL
