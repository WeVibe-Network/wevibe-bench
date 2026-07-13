"""Load the durable bench env file (config/bench.env) into os.environ.

Single source of truth for the throwaway local-dev bench identity + paths so the
seed->recall loop runs hands-off with no manual `source`. Values already present
in the environment WIN (setdefault semantics) so an explicit shell export or CI
override is never clobbered. Not a general dotenv library — just enough to load
our own committed, well-formed bench.env.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_ENV = _REPO_ROOT / "config" / "bench.env"
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand(value: str, scope: dict[str, str]) -> str:
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        return scope.get(name, os.environ.get(name, ""))

    return _VAR_RE.sub(repl, value)


def load_bench_env(path: str | os.PathLike[str] | None = None) -> Path | None:
    """Load KEY=VALUE lines from bench.env into os.environ (setdefault).

    Returns the resolved path if a file was loaded, else None. Silent no-op when
    the file is absent so callers can invoke it unconditionally.
    """

    env_path = Path(path) if path else Path(
        os.environ.get("WEVIBE_BENCH_ENV_FILE", _DEFAULT_ENV)
    )
    if not env_path.is_file():
        return None

    seen: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        value = _expand(value, seen)
        seen[key] = value
        os.environ.setdefault(key, value)
    return env_path
