import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def test_real_blocka_reader_returns_only_texts_served_for_session(tmp_path: Path) -> None:
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node executable not found on PATH; cannot run real Block A served-store reader")

    reader_path = (
        Path(__file__).resolve().parents[1]
        / "tests/fixtures/served-memory-store.js"
    )
    assert reader_path.is_file(), (
        f"rehomed served-store reader missing at {reader_path}; "
        "the Block A cross-language tripwire requires it (rehome from the commissioned prod wevibe-mcp dist)"
    )

    session_id = "sid-blocka-cross-lang"
    served_store_path = tmp_path / "served-memories.json"
    _write_json(
        served_store_path,
        {
            "version": 1,
            "memories": {
                "c_accept": {
                    "cid": "c_accept",
                    "text": "ACCEPT_TEXT_MARKER",
                    "session_ids": [session_id, "sid-other"],
                    "last_used_at": 200,
                },
                "c_deny": {
                    "cid": "c_deny",
                    "text": "DENY_TEXT_MARKER",
                    "session_ids": ["sid-other"],
                    "last_used_at": 199,
                },
                "c_block": {
                    "cid": "c_block",
                    "text": "BLOCK_TEXT_MARKER",
                    "session_ids": ["sid-other"],
                    "last_used_at": 198,
                },
                "c_report": {
                    "cid": "c_report",
                    "text": "REPORT_TEXT_MARKER",
                    "session_ids": ["sid-other"],
                    "last_used_at": 197,
                },
            },
        },
    )

    script_path = tmp_path / "read_used_memory_texts.mjs"
    script_path.write_text(
        "\n".join(
            [
                "import { pathToFileURL } from 'node:url';",
                "const [modulePath, sessionId] = process.argv.slice(2);",
                "const moduleUrl = pathToFileURL(modulePath).href;",
                "const mod = await import(moduleUrl);",
                "if (typeof mod.readUsedMemoryTexts !== 'function') {",
                "  throw new Error('readUsedMemoryTexts export missing');",
                "}",
                "const result = mod.readUsedMemoryTexts(sessionId);",
                "process.stdout.write(JSON.stringify(result));",
                "",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["WEVIBE_SERVED_MEMORIES_PATH"] = str(served_store_path)
    completed = subprocess.run(
        [node_bin, str(script_path), str(reader_path), session_id],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 0, (
        f"node reader failed exit={completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )

    stdout = completed.stdout.strip()
    assert stdout, "node reader produced empty stdout"
    payload = json.loads(stdout)
    assert isinstance(payload, list)
    assert all(isinstance(item, str) for item in payload)

    assert "ACCEPT_TEXT_MARKER" in payload
    assert "DENY_TEXT_MARKER" not in payload
    assert "BLOCK_TEXT_MARKER" not in payload
    assert "REPORT_TEXT_MARKER" not in payload
