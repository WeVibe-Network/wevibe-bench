from __future__ import annotations

import importlib
import json
import pathlib
import sys
from typing import Any

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import backgammon_ladder as bl


class _DummyLogger:
    def __init__(self, logfile_path: pathlib.Path | None = None) -> None:
        self.logfile_path = str(logfile_path) if logfile_path is not None else ""

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def exception(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _patch_driver_logging(monkeypatch: Any, runs_dir: pathlib.Path) -> None:
    driver_log = runs_dir / "backgammon-ladder-driver.log"
    driver_log.write_text("driver\n", encoding="utf-8")
    monkeypatch.setattr(bl, "run_logger", lambda *_args, **_kwargs: _DummyLogger(driver_log))
    monkeypatch.setattr(bl, "new_trace_id", lambda: "trace-test")


def _invoke_main(monkeypatch: Any, argv: list[str]) -> int:
    # D5a: org must be explicitly pinned (no silent default). Inject a valid arm
    # org unless the test already supplies its own.
    if "--org-id" not in argv:
        argv = [*argv, "--org-id", "wevibe-org-2"]
    monkeypatch.setattr(sys, "argv", ["backgammon_ladder.py", *argv])
    return bl.main()


def _extract_prefixed_json(stdout: str, prefix: str) -> dict[str, Any]:
    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if line.startswith(prefix):
            return json.loads(line[len(prefix) :].strip())
    raise AssertionError(f"missing line prefixed by {prefix!r}")


def _unit_entry(*, run_number: int, model: str, phase: str, attempts: int = 1) -> dict[str, Any]:
    return {
        "run_number": run_number,
        "model": model,
        "phase": phase,
        "status": "ok",
        "attempts": attempts,
        "completed_at": "2026-07-13T00:00:00Z",
        "logfiles": [],
    }


def test_retry_abort_escalate_fires(tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any) -> None:
    _patch_driver_logging(monkeypatch, tmp_path)

    calls: list[str] = []

    def _always_fail(*, phase: str, cmd: list[str], logfile_path: str | pathlib.Path, dry_run: bool) -> tuple[bool, dict[str, Any]]:
        del cmd, dry_run
        calls.append(phase)
        pathlib.Path(logfile_path).write_text("injected failure\n", encoding="utf-8")
        return (
            False,
            {
                "status": "error",
                "error_text": "injected extract failure",
                "exit_code": 1,
                "delivery": "NO",
                "stderr": "injected extract failure",
                "stdout": "",
                "dur_seconds": 0.001,
                "logfile": str(logfile_path),
            },
        )

    monkeypatch.setattr(bl, "run_unit", _always_fail)

    model = "anthropic/claude-opus-4.8"
    exit_code = _invoke_main(
        monkeypatch,
        [
            "--model",
            model,
            "--run-number",
            "7",
            "--runs-dir",
            str(tmp_path),
        ],
    )

    assert exit_code == 3
    assert len(calls) == bl.DEFAULT_MAX_RETRIES == 5
    assert set(calls) == {"session"}

    run_label = f"run7-{bl._slugify_model(model)}"
    escalation_path = tmp_path / f"{run_label}-ESCALATE.json"
    assert escalation_path.is_file()

    escalation = _read_json(escalation_path)
    assert escalation["status"] == "aborted"
    assert escalation["run_number"] == 7
    assert escalation["model"] == model
    assert escalation["phase"] == "session"
    assert escalation["attempts"] == 5
    assert escalation["last_error"]

    stdout = capsys.readouterr().out
    ladder_result = _extract_prefixed_json(stdout, "BACKGAMMON_LADDER_RESULT_JSON ")
    assert ladder_result["status"] == "aborted"

    checkpoint_path = tmp_path / "ladder-checkpoint.json"
    units = _read_json(checkpoint_path).get("units", []) if checkpoint_path.exists() else []
    assert not any(unit.get("phase") == "session" and unit.get("status") == "ok" for unit in units)


def test_resume_skips_completed_units(tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any) -> None:
    _patch_driver_logging(monkeypatch, tmp_path)

    model = "model-m"
    checkpoint_path = tmp_path / "ladder-checkpoint.json"
    _write_json(
        checkpoint_path,
        {
            "units": [
                _unit_entry(run_number=3, model=model, phase="session", attempts=2),
                _unit_entry(run_number=3, model=model, phase="extraction", attempts=1),
            ],
        },
    )

    called = False

    def _must_not_run(*, phase: str, cmd: list[str], logfile_path: str | pathlib.Path, dry_run: bool) -> tuple[bool, dict[str, Any]]:
        del phase, cmd, logfile_path, dry_run
        nonlocal called
        called = True
        raise AssertionError("run_unit must not be called")

    monkeypatch.setattr(bl, "run_unit", _must_not_run)

    exit_code = _invoke_main(
        monkeypatch,
        [
            "--resume",
            "--model",
            model,
            "--run-number",
            "3",
            "--runs-dir",
            str(tmp_path),
        ],
    )

    assert exit_code == 0
    assert called is False

    stdout = capsys.readouterr().out
    assert "resume-skip run_number=3 model=model-m phase=session" in stdout
    assert "resume-skip run_number=3 model=model-m phase=extraction" in stdout


def test_resume_partial_runs_only_remaining_unit(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    _patch_driver_logging(monkeypatch, tmp_path)

    model = "model-n"
    checkpoint_path = tmp_path / "ladder-checkpoint.json"
    _write_json(
        checkpoint_path,
        {
            "units": [
                _unit_entry(run_number=4, model=model, phase="session", attempts=1),
            ],
        },
    )

    phases: list[str] = []

    def _succeed_once(*, phase: str, cmd: list[str], logfile_path: str | pathlib.Path, dry_run: bool) -> tuple[bool, dict[str, Any]]:
        del cmd, dry_run
        phases.append(phase)
        pathlib.Path(logfile_path).write_text("ok\n", encoding="utf-8")
        return (
            True,
            {
                "status": "ok",
                "delivery": "YES",
                "exit_code": 0,
                "stderr": "",
                "stdout": "",
                "dur_seconds": 0.001,
                "error_text": "",
            },
        )

    monkeypatch.setattr(bl, "run_unit", _succeed_once)

    exit_code = _invoke_main(
        monkeypatch,
        [
            "--resume",
            "--model",
            model,
            "--run-number",
            "4",
            "--runs-dir",
            str(tmp_path),
        ],
    )

    assert exit_code == 0
    assert phases == ["extraction"]

    checkpoint = _read_json(checkpoint_path)
    units = [
        unit
        for unit in checkpoint.get("units", [])
        if unit.get("run_number") == 4 and unit.get("model") == model
    ]
    assert {unit.get("phase") for unit in units} == {"session", "extraction"}
    assert all(unit.get("status") == "ok" for unit in units)


def test_self_extraction_cmd_wiring(tmp_path: pathlib.Path) -> None:
    parser = bl._build_arg_parser()

    session_model = "anthropic/claude-opus-4.8"
    args_default = parser.parse_args(
        [
            "--model",
            session_model,
            "--run-number",
            "1",
            "--extract-timeout",
            "321",
            "--runs-dir",
            str(tmp_path),
        ],
    )
    cmd_default = bl.build_extract_cmd("python3", SCRIPTS, args_default, "run1")

    idx_session = cmd_default.index("--session-model")
    assert cmd_default[idx_session + 1] == session_model
    assert "--extract-model" not in cmd_default
    idx_timeout = cmd_default.index("--extract-timeout")
    assert cmd_default[idx_timeout + 1] == "321"
    idx_runs = cmd_default.index("--runs-dir")
    assert cmd_default[idx_runs + 1] == str(tmp_path)

    args_override = parser.parse_args(
        [
            "--model",
            session_model,
            "--run-number",
            "2",
            "--extract-model",
            "OVERRIDE",
            "--extract-timeout",
            "654",
            "--runs-dir",
            str(tmp_path),
        ],
    )
    cmd_override = bl.build_extract_cmd("python3", SCRIPTS, args_override, "run2")
    idx_override = cmd_override.index("--extract-model")
    assert cmd_override[idx_override + 1] == "OVERRIDE"


def test_logging_and_cleanup_on_success(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    _patch_driver_logging(monkeypatch, tmp_path)

    run_number = 9
    model = "openai/gpt-5.2"
    run_label = f"run{run_number}-{bl._slugify_model(model)}"

    preserved_markdown = tmp_path / "retained-notes.md"
    preserved_markdown.write_text("# keep\n", encoding="utf-8")

    foreign_log = tmp_path / "20260713T132143Z-t1-opus48-off.log"
    foreign_log.write_text("foreign artifact\n", encoding="utf-8")

    created_logs: list[pathlib.Path] = []

    def _successful_unit(*, phase: str, cmd: list[str], logfile_path: str | pathlib.Path, dry_run: bool) -> tuple[bool, dict[str, Any]]:
        del phase, cmd, dry_run
        log_path = pathlib.Path(logfile_path)
        created_logs.append(log_path)
        log_path.write_text("verbose step logfile\n", encoding="utf-8")

        (tmp_path / f"{run_label}-scorecard.json").write_text('{"ok":true}\n', encoding="utf-8")
        (tmp_path / f"{run_label}-detail.json").write_text('{"ok":true}\n', encoding="utf-8")

        return (
            True,
            {
                "status": "ok",
                "delivery": "YES",
                "exit_code": 0,
                "stderr": "",
                "stdout": "",
                "dur_seconds": 0.001,
                "error_text": "",
            },
        )

    monkeypatch.setattr(bl, "run_unit", _successful_unit)

    exit_code = _invoke_main(
        monkeypatch,
        [
            "--model",
            model,
            "--run-number",
            str(run_number),
            "--runs-dir",
            str(tmp_path),
        ],
    )

    assert exit_code == 0
    assert len(created_logs) == 2
    assert all(not path.exists() for path in created_logs)

    assert (tmp_path / f"{run_label}-scorecard.json").is_file()
    assert (tmp_path / f"{run_label}-detail.json").is_file()
    assert (tmp_path / "ladder-checkpoint.json").is_file()
    assert preserved_markdown.is_file()
    assert foreign_log.is_file()


def test_sxe_no_silent_fallback_and_guard() -> None:
    source = (SCRIPTS / "backgammon_sxe.py").read_text(encoding="utf-8")

    assert "_default_direct_memory" not in source
    assert "proof_direct" not in source
    assert "DISTILLED_DEFAULT_MODEL" not in source
    assert "orcarouter/" in source

    backgammon_sxe = importlib.import_module("backgammon_sxe")
    parser_builder = getattr(backgammon_sxe, "_build_arg_parser", None)
    assert callable(parser_builder)

    parser = parser_builder()
    options = {opt for action in parser._actions for opt in action.option_strings}
    assert "--session-model" in options
    assert "--extract-model" in options
    assert "--extract-timeout" in options
