"""Focused R-37 + substrate-event regression tests for backgammon SxE.

The canonical SxE driver (`scripts/backgammon_sxe.py`) previously logged a memory
plaintext fragment (`fragment=<first-84-chars>`) into stdout, the run logfile, and the
`BACKGAMMON_SXE_RESULT_JSON` payload (which the ladder persists to its checkpoint on disk).
R-37 requires logs/results emit ONLY a hash fingerprint + size + outcome — never plaintext.
These tests guard `_memory_fingerprint_fields` and substrate-event construction.
No paid model, no network, no docker.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import backgammon_sxe as sx  # noqa: E402


_SECRET = "SYNTHETIC-FIXTURE: add the --frobnicate flag to the launch command when targeting Widget 9.x or the gizmo fails to boot"


def test_fingerprint_fields_emit_only_hash_and_size_never_plaintext() -> None:
    fields = sx._memory_fingerprint_fields(_SECRET)

    # Exactly the R-37-safe keys, no plaintext key.
    assert set(fields) == {"memory_fp", "text_size"}

    # sha256 first-8 hex fingerprint.
    assert fields["memory_fp"] == sx._sha256_first8(_SECRET)
    assert len(fields["memory_fp"]) == 8
    assert all(c in "0123456789abcdef" for c in fields["memory_fp"])

    # Byte/char size of the plaintext (size is not the plaintext).
    assert fields["text_size"] == len(_SECRET)

    # The plaintext (or any distinctive slice of it) must NOT appear anywhere in the
    # serialized fields — this is the property that used to be violated.
    serialized = json.dumps(fields)
    assert _SECRET not in serialized
    assert "frobnicate" not in serialized
    assert "gizmo" not in serialized


def test_fingerprint_fields_distinguish_distinct_plaintext() -> None:
    a = sx._memory_fingerprint_fields("alpha memory text")
    b = sx._memory_fingerprint_fields("bravo memory text")
    assert a["memory_fp"] != b["memory_fp"]


def test_result_payload_construction_carries_no_plaintext_key() -> None:
    # The SxE result payload must not reintroduce a plaintext-bearing field. The success
    # path spreads `_memory_fingerprint_fields(...)` (memory_fp/text_size) in place of the
    # old `memory_fragment`. Guard against a regression reintroducing a fragment/text key.
    src = (SCRIPTS / "backgammon_sxe.py").read_text(encoding="utf-8")
    assert '"memory_fragment":' not in src, "memory_fragment must not be a logged/result field (R-37)"
    assert "fragment={memory_fragment" not in src, "delivery-proof log must not print the plaintext fragment (R-37)"
    assert '"delivery_proof": delivery_payload' in src
    assert '"n_memories": len(committed_memories)' in src
    assert '"memories": [' in src


def test_memory_fragment_uses_shared_m2_limit_and_single_line() -> None:
    text = "alpha\n\n beta    gamma " * 8

    fragment = sx._memory_fragment(text)

    assert fragment == sx.M2Proof.memory_fragment(text)
    assert len(fragment) <= 64
    assert "\n" not in fragment


def test_build_substrate_events_maps_user_assistant_reasoning_tool_and_edit(tmp_path: pathlib.Path) -> None:
    user_sidecar = tmp_path / "worktree.user-events.jsonl"
    worker_events = tmp_path / "worktree.events.jsonl"

    user_sidecar.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": 1_700_000_000_000,
                        "attempt": 1,
                        "text": "initial task prompt",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": 1_700_000_000_500,
                        "attempt": 2,
                        "text": "repair feedback",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    worker_events.write_text(
        "\n".join(
            [
                json.dumps({"type": "step_start", "timestamp": 1_700_000_000_001, "part": {}}, separators=(",", ":"), sort_keys=True),
                json.dumps(
                    {
                        "type": "text",
                        "timestamp": 1_700_000_000_100,
                        "part": {
                            "text": "assistant says hello",
                            "time": {"start": 1_700_000_000_101, "end": 1_700_000_000_120},
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_000_200,
                        "part": {
                            "tool": "read",
                            "time": {"start": 1_700_000_000_201, "end": 1_700_000_000_260},
                            "state": {
                                "status": "completed",
                                "input": {"offset": 3, "filePath": "src/game.ts"},
                                "output": "file body",
                                "metadata": {"exit": 0},
                            },
                            "metadata": {
                                "openrouter": {
                                    "reasoning_details": [
                                        {"type": "reasoning.text", "text": "reasoning alpha"},
                                        {"type": "other", "text": "ignored"},
                                    ]
                                }
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_000_300,
                        "part": {
                            "tool": "edit",
                            "time": {"start": 1_700_000_000_301},
                            "state": {
                                "status": "completed",
                                "input": {
                                    "filePath": "src/game.ts",
                                    "oldString": "old body",
                                    "newString": "new body",
                                },
                                "output": "ok",
                                "metadata": {},
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_000_400,
                        "part": {
                            "tool": "write",
                            "time": {"start": 1_700_000_000_401},
                            "state": {
                                "status": "completed",
                                "input": {
                                    "filePath": "src/ai.ts",
                                    "content": "export const x = 1;",
                                },
                                "output": "ok",
                                "metadata": {},
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_000_500,
                        "part": {
                            "tool": "bash",
                            "time": {"start": 1_700_000_000_501},
                            "state": {
                                "status": "error",
                                "input": {"cwd": "/tmp/repo", "command": "npm test"},
                                "output": "boom failed",
                                "metadata": {"exit": 1},
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {"type": "step_finish", "timestamp": 1_700_000_000_600, "part": {"tokens": 999}},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events, stats, events_files = sx._build_substrate_events(session_dir=tmp_path)

    assert events_files == [worker_events.resolve()]
    assert [event["seq"] for event in events] == list(range(len(events)))
    assert [event["kind"] for event in events] == [
        "user",
        "user",
        "assistant",
        "reasoning",
        "tool",
        "edit",
        "edit",
        "tool",
    ]

    assert events[2] == {
        "kind": "assistant",
        "time": 1_700_000_000_101,
        "seq": 2,
        "role": "assistant",
        "text": "assistant says hello",
    }
    assert events[3] == {
        "kind": "reasoning",
        "time": 1_700_000_000_201,
        "seq": 3,
        "role": "assistant",
        "text": "reasoning alpha",
    }
    assert events[4] == {
        "kind": "tool",
        "time": 1_700_000_000_201,
        "seq": 4,
        "name": "read",
        "input": '{"filePath":"src/game.ts","offset":3}',
        "output": "file body",
        "exit": 0,
        "status": "completed",
    }
    assert events[5] == {
        "kind": "edit",
        "time": 1_700_000_000_301,
        "seq": 5,
        "file": "src/game.ts",
        "detail": "new body",
    }
    assert events[6] == {
        "kind": "edit",
        "time": 1_700_000_000_401,
        "seq": 6,
        "file": "src/ai.ts",
        "detail": "export const x = 1;",
    }
    assert events[7] == {
        "kind": "tool",
        "time": 1_700_000_000_501,
        "seq": 7,
        "name": "bash",
        "input": '{"command":"npm test","cwd":"/tmp/repo"}',
        "output": "boom failed",
        "exit": 1,
        "status": "error",
        "error": "boom failed",
    }

    canonical_events = json.dumps(events, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert stats == {
        "event_count": 8,
        "kind_counts": {
            "user": 2,
            "assistant": 1,
            "reasoning": 1,
            "tool": 2,
            "edit": 2,
        },
        "skipped_error_events": 0,
        "total_chars": len(canonical_events),
        "events_sha256_first8": sx._sha256_first8(canonical_events),
    }


def test_build_substrate_events_skips_worker_type_error_events_and_counts_them(
    tmp_path: pathlib.Path,
) -> None:
    user_sidecar = tmp_path / "worktree.user-events.jsonl"
    worker_events = tmp_path / "worktree.events.jsonl"

    user_sidecar.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": 1_700_000_001_000,
                "attempt": 1,
                "text": "run extraction",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    worker_events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "text",
                        "timestamp": 1_700_000_001_100,
                        "part": {
                            "text": "assistant output",
                            "time": {"start": 1_700_000_001_101},
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "error",
                        "timestamp": 1_700_000_001_150,
                        "sessionID": "session-error",
                        "error": {
                            "name": "APIError",
                            "data": {"provider": "openrouter", "status": 400},
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_001_200,
                        "part": {
                            "tool": "bash",
                            "time": {"start": 1_700_000_001_201},
                            "state": {
                                "status": "completed",
                                "input": {"command": "npm test"},
                                "output": "ok",
                                "metadata": {"exit": 0},
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events, stats, _ = sx._build_substrate_events(session_dir=tmp_path)

    assert [event["kind"] for event in events] == ["user", "assistant", "tool"]
    assert stats["skipped_error_events"] == 1
    assert stats["kind_counts"] == {
        "user": 1,
        "assistant": 1,
        "reasoning": 0,
        "tool": 1,
        "edit": 0,
    }


def test_errored_write_missing_filepath_uses_generic_tool_event_and_nonerror_stays_strict(
    tmp_path: pathlib.Path,
) -> None:
    error_text = (
        "The write tool was called with invalid arguments: "
        "SchemaError(Missing key in arguments: filePath)"
    )

    errored_dir = tmp_path / "errored-write"
    errored_dir.mkdir(parents=True, exist_ok=True)
    (errored_dir / "worktree.user-events.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": 1_700_000_010_000,
                "attempt": 1,
                "text": "trigger write",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (errored_dir / "worktree.events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_use",
                "timestamp": 1_700_000_010_100,
                "part": {
                    "tool": "write",
                    "time": {"start": 1_700_000_010_101},
                    "state": {
                        "status": "error",
                        "input": {},
                        "output": None,
                        "error": error_text,
                    },
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    events, _, _ = sx._build_substrate_events(session_dir=errored_dir)
    assert [event["kind"] for event in events] == ["user", "tool"]
    assert events[1] == {
        "kind": "tool",
        "time": 1_700_000_010_101,
        "seq": 1,
        "name": "write",
        "input": "{}",
        "output": None,
        "exit": None,
        "status": "error",
        "error": error_text,
    }

    strict_dir = tmp_path / "strict-write"
    strict_dir.mkdir(parents=True, exist_ok=True)
    (strict_dir / "worktree.user-events.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": 1_700_000_020_000,
                "attempt": 1,
                "text": "trigger strict write",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (strict_dir / "worktree.events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_use",
                "timestamp": 1_700_000_020_100,
                "part": {
                    "tool": "write",
                    "time": {"start": 1_700_000_020_101},
                    "state": {
                        "status": "completed",
                        "input": {},
                        "output": "never emitted",
                    },
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="edit/write tool missing filePath"):
        sx._build_substrate_events(session_dir=strict_dir)


def test_build_substrate_events_requires_user_sidecar_for_each_events_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "worktree.events.jsonl").write_text(
        json.dumps(
            {
                "type": "text",
                "timestamp": 1_700_000_000_000,
                "part": {"text": "assistant", "time": {"start": 1_700_000_000_001}},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="missing user sidecar for worker events file"):
        sx._build_substrate_events(session_dir=tmp_path)


def _run_main_capture_extract_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    env: dict[str, str | None],
) -> tuple[int, dict[str, Any], list[str]]:
    # Test-isolation hardening: force dotenv resolution to a non-existent fixture path
    # so ambient repo/root `.env` cannot override the controlled per-test env map.
    monkeypatch.setenv("WEVIBE_BENCH_DOTENV", str(tmp_path / "does-not-exist.env"))

    for key in (
        "WEVIBE_BENCH_EXTRACT_BASE_URL",
        "WEVIBE_BENCH_EXTRACT_API_KEY_FILE",
        "WEVIBE_BENCH_EXTRACT_NUM_CTX",
        "WEVIBE_BENCH_API_KEY",
        "OPENROUTER_API_KEY",
        "ORCAROUTER_API_KEY",
        "WEVIBE_BENCH_SPEND_PROXY_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    run_label = "fixture-run"
    source_mode = "off"
    runs_dir = tmp_path / "runs"
    (runs_dir / run_label / source_mode).mkdir(parents=True, exist_ok=True)

    args = argparse.Namespace(
        run_label=run_label,
        source_mode=source_mode,
        org_id="wevibe-org-2",  # D5a: no DEFAULT_ORG_ID; extraction must pin an explicit (non-org-0) arm target.
        session_model="orcarouter/opencode/big-pickle",
        extract_model=None,
        extract_timeout=60,
        runs_dir=str(runs_dir),
    )

    class _FakeParser:
        def parse_args(self) -> argparse.Namespace:
            return args

    monkeypatch.setattr(sx, "_build_arg_parser", lambda: _FakeParser())
    monkeypatch.setattr(sx, "load_bench_env", lambda: None)

    log_lines: list[str] = []

    class _FakeLogger:
        logfile_path = str(tmp_path / "fixture.log")

        def info(self, message: str) -> None:
            log_lines.append(message)

    logger = _FakeLogger()
    monkeypatch.setattr(sx, "run_logger", lambda *_args, **_kwargs: logger)

    monkeypatch.setattr(sx, "_load_prompt", lambda _path: "fixture prompt")
    monkeypatch.setattr(
        sx,
        "_build_substrate_events",
        lambda *, session_dir: (
            [{"kind": "user", "time": 1, "seq": 0, "text": "hello"}],
            {
                "event_count": 1,
                "kind_counts": {
                    "user": 1,
                    "assistant": 0,
                    "reasoning": 0,
                    "tool": 0,
                    "edit": 0,
                },
                "total_chars": 5,
                "events_sha256_first8": "deadbeef",
            },
            [session_dir / "worktree.events.jsonl"],
        ),
    )
    monkeypatch.setattr(sx, "_session_id_counts_from_events", lambda _session_dir: {"session-1": 1})
    monkeypatch.setattr(
        sx,
        "_session_id_from_events",
        lambda _session_dir, *, session_counts=None: "session-1",
    )
    monkeypatch.setattr(sx, "_load_identity", lambda _name: object())
    monkeypatch.setattr(sx, "preflight", lambda **_kwargs: None)
    monkeypatch.setattr(sx, "_required_env", lambda _name: "wallet")

    class _FakeInstance:
        def __init__(self, port: int, pid: int) -> None:
            self.port = port
            self.pid = pid

    class _FakeProcman:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def stop(self, _instance: _FakeInstance) -> None:
            return None

    class _FakeOrchestrator:
        def __init__(self, *_args, **_kwargs) -> None:
            self.org_id = "wevibe-org-2"

        def run_m1(self) -> dict[str, str]:
            return {"org_id": "wevibe-org-2"}

    monkeypatch.setattr(sx, "McpProcessManager", _FakeProcman)
    monkeypatch.setattr(sx, "LifecycleOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr(
        sx,
        "_bring_up_for_resume",
        lambda **_kwargs: (_FakeInstance(4550, 1001), _FakeInstance(4551, 1002), True),
    )

    captured_extract_kwargs: dict[str, Any] = {}

    class _FakeProof:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        @staticmethod
        def memory_fragment(text: str) -> str:
            return " ".join(text.split())[:64]

        def produce_memories(self, **kwargs: Any) -> list[dict[str, Any]]:
            captured_extract_kwargs.update(kwargs)
            return [
                {
                    "text": "fixture memory",
                    "keywords": ["backgammon", "typescript"],
                    "stack_hint": ["node"],
                    "memory_type": "memory",
                }
            ]

        def submit_memory(self, _org_id: str, _memory: dict[str, Any]) -> str:
            return "submission-1"

        def leader_verify_and_commit(
            self,
            _org_id: str,
            submission_hash: str,
            _keywords: list[str],
            producer_model_id: str | None = None,
        ) -> dict[str, Any]:
            if producer_model_id is not None:
                assert producer_model_id == "opencode/big-pickle"
            return {
                "commit_status": {
                    "submissions": [
                        {
                            "submission_hash": submission_hash,
                            "status": "committed",
                        }
                    ]
                }
            }

        def prove_delivery(self, _org_id: str, memory_fragments: list[str]) -> dict[str, Any]:
            return {
                "delivery": "YES",
                "n_memories": len(memory_fragments),
                "matched": True,
                "per_memory": [
                    {
                        "fragment_fp": "abc12345",
                        "matched": True,
                    }
                    for _fragment in memory_fragments
                ],
            }

    monkeypatch.setattr(sx, "M2Proof", _FakeProof)

    exit_code = sx.main()
    return exit_code, captured_extract_kwargs, log_lines




