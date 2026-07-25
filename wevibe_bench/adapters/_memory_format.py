from __future__ import annotations

import re

from wevibe_bench.backends.base import RecalledMemory


def _format_memory(memories: list[RecalledMemory]) -> str:
    lines = [
        "# WEVIBE MEMORY CONTEXT",
        "# Read-only context loaded via aider --read",
    ]
    included = 0
    for memory in memories:
        if not memory.has_content():
            continue

        included += 1
        cid = memory.cid or "unknown"
        keywords = ",".join(memory.matched_keywords) if memory.matched_keywords else "none"
        text = re.sub(r"\s+", " ", memory.text.strip())
        lines.append(f"- m{included} cid={cid} kw={keywords} text={text}")

    if included == 0:
        return ""

    return "\n".join(lines) + "\n"


__all__ = ["_format_memory"]
