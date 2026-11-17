"""Format dispatch shared by the CLI and the web app."""

from pathlib import Path

from . import config
from .common import Progress, no_progress
from .formats.epub_handler import translate_epub
from .formats.pdf_handler import translate_pdf
from .formats.txt_handler import translate_txt

# suffix -> (handler function, output file extension)
HANDLERS = {
    ".txt": (translate_txt, ".txt"),
    ".pdf": (translate_pdf, ".docx"),
    ".epub": (translate_epub, ".epub"),
}


def is_supported(filename: str) -> bool:
    return Path(filename).suffix.lower() in HANDLERS


def output_name(filename: str) -> str:
    path = Path(filename)
    _, out_ext = HANDLERS[path.suffix.lower()]
    return f"{path.stem}_{config.TARGET_LANGUAGE_CODE}{out_ext}"


def translate_file(input_path: Path, output_path: Path, session, progress: Progress = no_progress) -> None:
    handler, _ = HANDLERS[input_path.suffix.lower()]
    handler(input_path, output_path, session, progress)
