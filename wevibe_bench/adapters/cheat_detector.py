"""Hard cheat-gate detection for backgammon benchmark worker transcripts."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re


logger = logging.getLogger(__name__)

_MAX_EXCERPT_CHARS = 200
_DISTINCTIVE_ORACLE_BASENAMES: tuple[str, ...] = (
    "report.mjs",
    "run.mjs",
    "aesthetic.ts",
    "harness.ts",
    "pregate.ts",
    "pregate.spec.ts",
    "gates-01-08.test.ts",
    "gates-09-12.test.ts",
    "gates-13-16.test.ts",
    "core.spec.ts",
    "edges.spec.ts",
)


@dataclass
class CheatHit:
    tool: str
    marker: str
    excerpt: str
    call_id: str = ""


@dataclass
class CheatFinding:
    cheated: bool
    hits: list[CheatHit] = field(default_factory=list)

    def summary(self) -> str:
        if not self.cheated:
            return "CLEAN: no oracle access detected"
        if not self.hits:
            return "CHEAT: oracle access detected"

        samples = [f"{hit.tool}->{hit.marker}" for hit in self.hits[:3]]
        if len(self.hits) > 3:
            samples.append(f"...(+{len(self.hits) - 3} more)")
        return f"CHEAT: {len(self.hits)} oracle access(es): {', '.join(samples)}"


def build_oracle_markers(*, gates_dir: Path, golden_dir: Path, extra_basenames: list[str] | None = None) -> list[str]:
    candidates = [
        str(gates_dir.expanduser().resolve()),
        str(golden_dir.expanduser().resolve()),
        "/gates/",
        "/golden/",
        *_DISTINCTIVE_ORACLE_BASENAMES,
    ]
    if extra_basenames:
        candidates.extend(extra_basenames)
    return _dedupe_casefolded(candidates)


def scan_events_for_oracle_access(*, events_path: Path, markers: list[str]) -> CheatFinding:
    hits: list[CheatHit] = []
    prepared_markers = _dedupe_casefolded(markers)
    if not prepared_markers:
        return CheatFinding(cheated=False, hits=[])

    if not events_path.exists():
        logger.error("cheat_detector events file missing path=%s", events_path)
        return CheatFinding(cheated=False, hits=[])

    with events_path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(payload, dict):
                continue

            tool_call = _extract_tool_call(payload)
            if tool_call is None:
                continue

            tool, call_id, input_payload = tool_call
            search_text = _stringify_input_values(input_payload)
            if not search_text:
                continue

            lowered_input = search_text.lower()
            for marker in prepared_markers:
                if marker.lower() not in lowered_input:
                    continue

                hit = CheatHit(
                    tool=tool,
                    marker=marker,
                    excerpt=_excerpt_around_match(search_text, marker),
                    call_id=call_id,
                )
                hits.append(hit)
                logger.warning(
                    "cheat_detector oracle access tool=%s marker=%s call_id=%s",
                    tool,
                    marker,
                    call_id,
                )

    return CheatFinding(cheated=(len(hits) > 0), hits=hits)


def _dedupe_casefolded(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def _extract_tool_call(payload: dict[str, object]) -> tuple[str, str, object] | None:
    part_obj = payload.get("part")
    if isinstance(part_obj, dict):
        part_type = str(part_obj.get("type", "")).strip().lower()
        if part_type and part_type != "tool":
            return None

        tool_obj = part_obj.get("tool")
        state_obj = part_obj.get("state")
        input_obj: object | None = None
        if isinstance(state_obj, dict):
            input_obj = state_obj.get("input")

        if not isinstance(tool_obj, str) or not tool_obj or input_obj is None:
            return None

        call_id_obj = part_obj.get("callID")
        if call_id_obj is None:
            call_id_obj = part_obj.get("callId")
        call_id = "" if call_id_obj is None else str(call_id_obj)
        return tool_obj, call_id, input_obj

    tool_obj = payload.get("tool")
    if not isinstance(tool_obj, str) or not tool_obj:
        return None

    call_id_obj = payload.get("callID")
    if call_id_obj is None:
        call_id_obj = payload.get("callId")
    call_id = "" if call_id_obj is None else str(call_id_obj)

    input_obj: object | None = None
    if "input" in payload:
        input_obj = payload.get("input")
    elif "args" in payload:
        input_obj = payload.get("args")
    elif "command" in payload:
        input_obj = {"command": payload.get("command")}

    if input_obj is None:
        return None

    return tool_obj, call_id, input_obj


def _stringify_input_values(input_obj: object) -> str:
    values: list[str] = []
    _collect_values(input_obj, values)
    return " ".join(values).strip()


def _collect_values(value: object, out: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        for nested in value.values():
            _collect_values(nested, out)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            _collect_values(nested, out)
        return

    text = str(value).strip()
    if text:
        out.append(text)


def _excerpt_around_match(text: str, marker: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""

    lowered = compact.lower()
    marker_lower = marker.lower()
    hit_index = lowered.find(marker_lower)

    if hit_index < 0:
        return compact[:_MAX_EXCERPT_CHARS]

    start = max(0, hit_index - 80)
    end = min(len(compact), hit_index + len(marker_lower) + 80)

    excerpt = compact[start:end]
    if start > 0:
        excerpt = f"...{excerpt}"
    if end < len(compact):
        excerpt = f"{excerpt}..."

    if len(excerpt) > _MAX_EXCERPT_CHARS:
        excerpt = excerpt[: _MAX_EXCERPT_CHARS - 3] + "..."
    return excerpt
