import codecs
from pathlib import Path

from .. import config
from ..common import Progress, no_progress, split_into_chunks


def _decode(data: bytes) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def translate_txt(input_path: Path, output_path: Path, session, progress: Progress = no_progress) -> None:
    chunks = split_into_chunks(_decode(input_path.read_bytes()), config.MAX_CHARS_PER_CHUNK)
    if not chunks:
        raise ValueError("File không có nội dung để dịch.")

    progress(0, len(chunks), "")
    translated_chunks = []
    for done, chunk in enumerate(chunks, 1):
        translated_chunks.append(session.translate(chunk, mode="text"))
        progress(done, len(chunks), translated_chunks[-1])

    output_path.write_text("\n\n".join(translated_chunks) + "\n", encoding="utf-8")
