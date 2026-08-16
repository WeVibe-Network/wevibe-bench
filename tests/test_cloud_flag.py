"""Guard tests for the --cloud flag system (WO-CLOUD-2).

Covers the four guard surfaces of the additive ``--cloud`` path:
(a) slug composition ``{router}/{provider}/{model}`` + arg-parser flags,
(b) OrcaRouter API-key resolution (file read, env wins, loud missing-key error),
(c) the non-cloud (local-llm-proxy) config path stays byte-identical,
(d) the key VALUE never appears in any generated config/argv — env refs only.

No real docker, no network: the DockerCell test mirrors the ``_fake_run``
monkeypatch pattern from tests/test_docker_isolation.py.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wevibe_bench.adapters.backgammon import build_worker_opencode_config
from wevibe_bench.adapters.docker_worker import (
    DockerCell,
    DockerCellConfig,
    _build_run_argv,
)
from wevibe_bench.spend_key import (
    SpendKeyError,
    resolve_cloud_api_key,
    resolve_cloud_key_file,
)


CLOUD_MODEL_SLUG = "orcarouter/deepseek/deepseek-v4-pro-0813"
PROVIDER_MODEL_KEY = "deepseek/deepseek-v4-pro-0813"


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contains_pair(argv: list[str], left: str, right: str) -> bool:
    return any(
        idx + 1 < len(argv) and argv[idx] == left and argv[idx + 1] == right
        for idx in range(len(argv))
    )


# ---------------------------------------------------------------------------
# (a) Cloud slug composition + arg-parser flags
# ---------------------------------------------------------------------------


def test_compose_cloud_slug_default_router() -> None:
    module = _load_run_cumulative_module()
    args = SimpleNamespace(cloud=True, router=None, provider="deepseek", model="deepseek-v4-pro-0813")
    assert module._compose_cloud_slug(args) == CLOUD_MODEL_SLUG


def test_compose_cloud_slug_custom_router() -> None:
    module = _load_run_cumulative_module()
    args = SimpleNamespace(cloud=True, router="myrouter", provider="deepseek", model="deepseek-v4-pro-0813")
    assert module._compose_cloud_slug(args) == f"myrouter/{PROVIDER_MODEL_KEY}"


def test_compose_cloud_slug_absent_returns_none() -> None:
    module = _load_run_cumulative_module()
    args = SimpleNamespace(cloud=False, router=None, provider=None, model=None)
    assert module._compose_cloud_slug(args) is None


def test_compose_cloud_slug_requires_provider_and_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_run_cumulative_module()
    args = SimpleNamespace(cloud=True, router=None, provider="", model="deepseek-v4-pro-0813")
    with pytest.raises(SystemExit) as excinfo:
        module._compose_cloud_slug(args)
    assert excinfo.value.code == 2
    assert "--cloud requires --provider" in capsys.readouterr().err


def test_compose_cloud_slug_unknown_model_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_run_cumulative_module()
    args = SimpleNamespace(cloud=True, router=None, provider="bogus", model="x")
    with pytest.raises(SystemExit) as excinfo:
        module._compose_cloud_slug(args)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "bogus/x" in err
    assert "not in the OrcaRouter provider block" in err


def test_apply_model_override_cloud_passthrough() -> None:
    module = _load_run_cumulative_module()
    result = module._apply_model_override(
        ["local-llm-proxy/x"],
        model_override="x",
        cloud_slug=CLOUD_MODEL_SLUG,
    )
    assert result == [CLOUD_MODEL_SLUG]


def test_arg_parser_cloud_flags() -> None:
    module = _load_run_cumulative_module()
    parser = module._build_arg_parser()

    args = parser.parse_args(["state"])
    assert args.cloud is False
    assert args.router is None
    assert args.provider is None

    args = parser.parse_args(
        [
            "--cloud",
            "--router",
            "orcarouter",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-v4-pro-0813",
            "state",
        ]
    )
    assert args.cloud is True
    assert args.router == "orcarouter"
    assert args.provider == "deepseek"
    assert args.model == "deepseek-v4-pro-0813"


# ---------------------------------------------------------------------------
# (b) OrcaRouter key resolution: file read, env wins, loud missing-key error
# ---------------------------------------------------------------------------


def test_resolve_cloud_api_key_from_file(tmp_path: Path) -> None:
    key_file = tmp_path / "cloud.env"
    key_file.write_text("ORCAROUTER_API_KEY=sk-orca-test\n", encoding="utf-8")
    assert resolve_cloud_api_key(env={}, key_file=key_file) == "sk-orca-test"


def test_resolve_cloud_api_key_env_wins(tmp_path: Path) -> None:
    assert (
        resolve_cloud_api_key(
            env={"ORCAROUTER_API_KEY": "sk-orca-env"},
            key_file=tmp_path / "does-not-exist.env",
        )
        == "sk-orca-env"
    )


def test_resolve_cloud_api_key_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(SpendKeyError) as excinfo:
        resolve_cloud_api_key(env={}, key_file=tmp_path / "does-not-exist.env")
    # The error must name the actionable command, not just complain.
    assert "store-cloud-key" in str(excinfo.value)


def test_resolve_cloud_key_file_env_override() -> None:
    resolved = resolve_cloud_key_file(env={"WEVIBE_BENCH_CLOUD_KEY_FILE": "/tmp/xyz"})
    assert resolved == Path("/tmp/xyz")


# ---------------------------------------------------------------------------
# (c) Non-cloud path unchanged (regression guard on the local branch)
# ---------------------------------------------------------------------------


def test_local_opencode_config_unchanged() -> None:
    config = build_worker_opencode_config(
        model="local-llm-proxy/qwen3.6-35b-a3b-bench",
        reasoning_effort=None,
        proxy_base_url="http://host.docker.internal:4545/v1",
        gates_dir="/g",
        golden_dir="/g",
    )
    provider = config["provider"]["local-llm-proxy"]
    assert provider["options"]["apiKey"] == "{env:LOCAL_LLM_PROXY_API_KEY}"
    assert provider["options"]["baseURL"] == "http://host.docker.internal:4545/v1"
    assert "qwen3.6-35b-a3b-bench" in provider["models"]


# ---------------------------------------------------------------------------
# (d) Key VALUE never appears in any generated config/argv — env refs only
# ---------------------------------------------------------------------------


def test_cloud_config_uses_env_ref_not_literal() -> None:
    config = build_worker_opencode_config(
        model=CLOUD_MODEL_SLUG,
        reasoning_effort=None,
        proxy_base_url=None,
        gates_dir="/g",
        golden_dir="/g",
    )
    provider = config["provider"]["orcarouter"]
    assert provider["options"]["apiKey"] == "{env:ORCAROUTER_API_KEY}"
    assert provider["options"]["baseURL"] == "https://api.orcarouter.ai/v1"
    assert config["model"] == CLOUD_MODEL_SLUG
    assert len(provider["models"]) == 5
    assert "sk-orca" not in json.dumps(config)


def test_cloud_config_baseurl_override_for_probe() -> None:
    config = build_worker_opencode_config(
        model=CLOUD_MODEL_SLUG,
        reasoning_effort=None,
        proxy_base_url="http://127.0.0.1:9/api/v1",
        gates_dir="/g",
        golden_dir="/g",
    )
    assert config["provider"]["orcarouter"]["options"]["baseURL"] == "http://127.0.0.1:9/api/v1"


def test_cloud_run_argv_injects_env_ref_not_value(tmp_path: Path) -> None:
    worktree = tmp_path / "cloud-argv-worktree"
    config = DockerCellConfig(
        worktree=worktree,
        memory_mode="off",
        container_name="wevibe-bench-cloud-argv-test",
        cloud=True,
    )
    argv = _build_run_argv(
        config=config,
        worktree=worktree,
        uid=501,
        gid=20,
        memory_mode="off",
    )
    assert _contains_pair(argv, "-e", "ORCAROUTER_API_KEY")
    assert all(not part.startswith("ORCAROUTER_API_KEY=") for part in argv)
    assert all("sk-orca" not in part for part in argv)


def test_cloud_enter_injects_key_from_resolver_not_literal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[0] == "docker" and argv[1] == "run":
            captured["argv"] = list(argv)
            env_payload = kwargs.get("env", {})
            assert isinstance(env_payload, dict)
            captured["env"] = dict(env_payload)
            return subprocess.CompletedProcess(argv, 0, stdout="fake-container-id\n", stderr="")
        if len(argv) >= 2 and argv[0] == "docker" and argv[1] == "rm":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker invocation: {argv!r}")

    monkeypatch.setattr(
        "wevibe_bench.adapters.docker_worker.resolve_cloud_api_key",
        lambda **kwargs: "sk-orca-fake",
    )
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.ensure_network", lambda *_: None)
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker._host_uid", lambda: 501)
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker._host_gid", lambda: 20)
    monkeypatch.setattr("wevibe_bench.adapters.docker_worker.subprocess.run", _fake_run)

    cell = DockerCell(
        DockerCellConfig(
            worktree=tmp_path / "cloud-enter-worktree",
            memory_mode="off",
            container_name="wevibe-bench-cloud-enter-test",
            cloud=True,
        )
    )

    try:
        cell.__enter__()
    finally:
        cell.teardown()

    assert "argv" in captured
    assert "env" in captured
    run_argv = captured["argv"]
    run_env = captured["env"]
    assert isinstance(run_argv, list)
    assert isinstance(run_env, dict)

    # The resolver's value reaches the container ONLY through the child env —
    # never as a literal in argv (bare `-e VAR` form).
    assert run_env.get("ORCAROUTER_API_KEY") == "sk-orca-fake"
    assert "sk-orca-fake" not in " ".join(run_argv)
