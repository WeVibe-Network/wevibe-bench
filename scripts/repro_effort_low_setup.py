"""Throwaway repro-setup: build a faithful backgammon worker worktree (scaffold + task prompt
+ fixed opencode config with the NEW effort=low default) for a direct :4480 worker repro.
Prints paths + asserts the default landed. Does NOT run the worker (that's done via docker)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wevibe_bench.adapters.backgammon import BackgammonRunner  # noqa: E402

REPRO_DIR = Path(sys.argv[1]).expanduser().resolve()
TOKEN = os.environ["ORCAROUTER_API_KEY"]
TAG = sys.argv[2] if len(sys.argv) > 2 else "repro-effort"

runner = BackgammonRunner(
    task_dir=Path("tasks/backgammon"),
    work_root=REPRO_DIR,
    model="orcarouter/kimi/kimi-k3",
    memory_mode="off",
    proxy_base_url="http://host.docker.internal:4480/v1",
    proxy_token=TOKEN,
    session_id=TAG,
)
print("resolved reasoning_effort =", runner.reasoning_effort)

worktree = REPRO_DIR / "worktree"
worktree.mkdir(parents=True, exist_ok=True)
runner._copy_tree_contents(Path("tasks/backgammon/scaffold"), worktree)
runner._write_worker_permission_config(worktree=worktree)
prompt = runner._build_task_prompt(injected_memory=[])
(REPRO_DIR / "prompt.txt").write_text(prompt, encoding="utf-8")

cfg = json.loads((worktree / "opencode.json").read_text())
mb = cfg["provider"]["orcarouter"]["models"]["kimi/kimi-k3"]
print("config options =", json.dumps(mb.get("options")))
print("config headers =", json.dumps(mb.get("headers")))
print("config baseURL =", cfg["provider"]["orcarouter"]["options"].get("baseURL"))
print("prompt_chars =", len(prompt))
print("worktree =", worktree)
print("scaffold files =", sorted(p.name for p in worktree.iterdir()))
