from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from docx import Document
from docx.shared import Pt
from pypdf import PdfReader

from .. import config
from ..common import Progress, no_progress, split_into_chunks

_HEADING = re.compile(r"^(#{1,3})\s+(.+)$")
_DIGITS = re.compile(r"\d+")


def _running_line_key(line: str) -> str:
    """Headers and footers repeat page after page except for the page number."""
    return _DIGITS.sub("#", line.strip().lower())


def _strip_running_lines(pages: list[list[str]]) -> list[list[str]]:
    """Drop the first/last line of a page when the same line recurs on many pages."""
    counts: Counter = Counter()
    for lines in pages:
        for key in {_running_line_key(line) for line in (lines[:1] + lines[-1:])}:
            counts[key] += 1
    threshold = max(3, len(pages) // 3)
    repeated = {key for key, count in counts.items() if count >= threshold}

    cleaned = []
    for lines in pages:
        if lines and _running_line_key(lines[0]) in repeated:
            lines = lines[1:]
        if lines and _running_line_key(lines[-1]) in repeated:
            lines = lines[:-1]
        cleaned.append(lines)
    return cleaned


def extract_pdf_text(input_path: Path) -> str:
    reader = PdfReader(str(input_path))
    pages = []
    for page in reader.pages:
        lines = [line.rstrip() for line in (page.extract_text() or "").splitlines()]
        pages.append([line for line in lines if line.strip()])

    text_chars = sum(len(line) for lines in pages for line in lines)
    if text_chars < 20 * max(len(pages), 1):
        raise ValueError(
            "PDF này gần như không có lớp văn bản (có thể là bản scan). "
            "Cần chạy OCR trước khi dịch."
        )

    # Pages are joined with a single newline: a sentence often continues on the next page.
    return "\n".join("\n".join(lines) for lines in _strip_running_lines(pages) if lines)


def _add_block(doc, block: str) -> None:
    heading = _HEADING.match(block)
    if heading and "\n" not in block:
        doc.add_heading(heading.group(2).strip(), level=len(heading.group(1)))
    else:
        doc.add_paragraph(block)


def translate_pdf(input_path: Path, output_path: Path, session, progress: Progress = no_progress) -> None:
    """PDF layout is hard to preserve, so output is a .docx with translated
    paragraphs in reading order."""
    text = extract_pdf_text(input_path)
    chunks = split_into_chunks(text, config.MAX_CHARS_PER_CHUNK)

    doc = Document()
    doc.styles["Normal"].font.name = "Times New Roman"
    doc.styles["Normal"].font.size = Pt(12)

    progress(0, len(chunks), "")
    for done, chunk in enumerate(chunks, 1):
        translated = session.translate(chunk, mode="pdf")
        for block in re.split(r"\n\s*\n", translated):
            if block.strip():
                _add_block(doc, block.strip())
        progress(done, len(chunks), translated)

    doc.save(str(output_path))
