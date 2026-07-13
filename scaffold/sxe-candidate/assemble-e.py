#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


_SCAFFOLD = Path(__file__).resolve().parents[1]

STRATEGY_PATH = _SCAFFOLD / 'sxe-candidate' / 'E-fork-strategy.md'
GATES_PATH = _SCAFFOLD / 'wevibe-mcp-clone' / 'prompts' / 'memory-extraction' / 'gates.md'
EXEMPLAR_PATH = _SCAFFOLD / 'wevibe-mcp-clone' / 'prompts' / 'memory-extraction' / 'exemplar.md'
CONTRACT_PATH = _SCAFFOLD / 'wevibe-mcp-clone' / 'prompts' / 'memory-extraction' / 'contract.md'
OUTPUT_PATH = _SCAFFOLD / 'sxe-candidate' / 'E-assembled.txt'


def strip_single_trailing_newline(text: str) -> str:
    return text[:-1] if text.endswith('\n') else text


def clean_strategy(text: str) -> str:
    lines = text.splitlines(keepends=True)
    cleaned: list[str] = []
    saw_role = False

    for line in lines:
        stripped = line.lstrip()
        if not saw_role and stripped.startswith('ROLE'):
            saw_role = True

        if not saw_role and stripped.startswith('#'):
            continue

        if '{{ORG_VOCABULARY}}' in line or '{{TRANSCRIPT}}' in line:
            if cleaned and cleaned[-1].strip() == 'INPUTS':
                cleaned.pop()
            continue

        cleaned.append(line)

    return ''.join(cleaned)


def main() -> int:
    strategy = clean_strategy(STRATEGY_PATH.read_text(encoding='utf-8'))
    gates = strip_single_trailing_newline(GATES_PATH.read_text(encoding='utf-8'))
    exemplar = strip_single_trailing_newline(EXEMPLAR_PATH.read_text(encoding='utf-8'))
    contract = strip_single_trailing_newline(CONTRACT_PATH.read_text(encoding='utf-8'))

    assembled = strategy + '\n\n' + gates + '\n\n' + exemplar + '\n\n' + contract
    digest8 = hashlib.sha256(assembled.encode('utf-8')).hexdigest()[:8]
    assembled_len = len(assembled)

    if '{{' in assembled:
        print('WARNING: placeholder leak detected in assembled prompt (found "{{").', file=sys.stderr)
        print(f'fp={digest8} len={assembled_len}')
        return 1

    OUTPUT_PATH.write_text(assembled, encoding='utf-8')
    print(f'fp={digest8} len={assembled_len}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
