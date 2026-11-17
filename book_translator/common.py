"""Shared text-chunking helpers used by every format handler."""

from __future__ import annotations

import re
from typing import Callable

# progress(done, total, preview): `preview` is the latest translated text, or "".
Progress = Callable[[int, int, str], None]


def no_progress(done: int, total: int, preview: str = "") -> None:
    pass


_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n+")
# Sentence-final punctuation, optional closing quotes/brackets, then whitespace.
_SENTENCE_END = re.compile(r"[.!?…][\"'”’»)\]]*\s+")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_long(block: str, max_chars: int) -> list[str]:
    """Cut one oversized block at sentence ends, falling back to whitespace."""
    pieces: list[str] = []
    rest = block
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = 0
        for match in _SENTENCE_END.finditer(window):
            cut = match.end()
        if not cut:
            cut = window.rfind(" ") + 1 or window.rfind("\n") + 1 or max_chars
        piece = rest[:cut].rstrip()
        if piece:
            pieces.append(piece)
        rest = rest[cut:].lstrip()
    if rest.strip():
        pieces.append(rest)
    return pieces


def split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Split text into chunks <= max_chars, breaking on paragraph boundaries.

    A paragraph longer than max_chars is cut between sentences, never mid-word.
    """
    paragraphs = _PARAGRAPH_BREAK.split(normalize_newlines(text))
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush():
        nonlocal current_len
        if current:
            chunks.append("\n\n".join(current))
            current.clear()
        current_len = 0

    for p in paragraphs:
        if not p.strip():
            continue
        p_len = len(p) + 2
        if p_len > max_chars:
            flush()
            chunks.extend(_split_long(p, max_chars))
            continue
        if current and current_len + p_len > max_chars:
            flush()
        current.append(p)
        current_len += p_len

    flush()
    return chunks
