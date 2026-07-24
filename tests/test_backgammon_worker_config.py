from __future__ import annotations

import json
from pathlib import Path

import pytest

from wevibe_bench.adapters.backgammon import BackgammonRunner
from wevibe_bench.adapters.openrouter_proxy import (
    DEFAULT_PROFILES,
    ModelMismatchError,
    apply_policy,
)


TASK_DIR = (Path(__file__).resolve().parents[1] / "tasks" / "backgammon").resolve()


def test_write_worker_permission_config_pins_model_and_small_model(tmp_path: Path) -> None:
    model = "openrouter/z-ai/glm-5.2"
    runner = BackgammonRunner(
        task_dir=TASK_DIR,
        work_root=tmp_path / "work-root",
        model=model,
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    runner._write_worker_permission_config(worktree=worktree)
    config = json.loads((worktree / "opencode.json").read_text(encoding="utf-8"))

    assert config["model"] == model
    assert config["small_model"] == model
    assert config["permission"]["external_directory"]["*"] == "deny"
    assert config["permission"]["doom_loop"] == "deny"
    assert config["permission"]["question"] == "deny"


def test_apply_policy_rejects_aux_model_and_accepts_same_model() -> None:
    glm_profile = DEFAULT_PROFILES()["glm"]
    cap = 1024

    with pytest.raises(ModelMismatchError):
        apply_policy(
            {
                "model": "google/gemini-3.1-flash-image",
                "messages": [{"role": "user", "content": "hello"}],
            },
            glm_profile,
            max_tokens_cap=cap,
        )

    transformed = apply_policy(
        {
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "hello"}],
        },
        glm_profile,
        max_tokens_cap=cap,
    )
    assert "provider" not in transformed
    assert transformed["model"] == "z-ai/glm-5.2"
    assert transformed["max_tokens"] == cap
