"""EPUB translation that rewrites only the text and leaves the rest of the archive alone.

An EPUB is a zip of XHTML chapters plus CSS, images and metadata. Each chapter is
translated one block at a time (a paragraph, a heading, a list item) so the model sees
whole sentences together with their inline markup. The output is a new zip in which
only the translated chapters and the metadata differ from the original.
"""

from __future__ import annotations

import html
import posixpath
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString, Tag
from lxml import etree

from .. import config
from ..common import Progress, no_progress

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
_DOCUMENT_TYPES = ("application/xhtml+xml", "text/html")
_NCX_TYPE = "application/x-dtbncx+xml"

# Elements that start a new block of text; everything else is inline markup.
_BLOCK_TAGS = [
    "address", "article", "aside", "blockquote", "body", "caption", "dd", "details",
    "div", "dl", "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "html", "li", "main", "nav", "ol", "p", "pre", "section",
    "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
]
_SKIP_TAGS = {"head", "math", "pre", "script", "style", "svg", "template"}
# Inline tags whose loss would break the book (links, notes, line breaks), unlike <i> or <span>.
_STRUCTURAL_TAGS = {"a", "br", "code", "img", "rt", "ruby", "sub", "sup"}

_TAG = re.compile(r"<[^>]+>")
_XML_ENCODING = re.compile(rb"^(<\?xml[^>]*encoding=[\"'])[^\"']+")
# html.parser lowercases attribute names, which breaks these two in inline SVG.
_SVG_ATTRIBUTES = [(b" viewbox=", b" viewBox="), (b" preserveaspectratio=", b" preserveAspectRatio=")]

_WARN_MARKUP = "đoạn bị hỏng thẻ định dạng khi dịch nên giữ nguyên bản gốc"


def _visible_text(node) -> str:
    if isinstance(node, Tag):
        return "" if node.name in _SKIP_TAGS else "".join(_visible_text(c) for c in node.children)
    return str(node)


def _collect_units(element: Tag, units: list[list]) -> None:
    """Gather runs of sibling nodes that form one translatable piece of text.

    A paragraph with only inline children is a single unit. In a container that mixes
    loose text with block children, each stretch between the blocks is its own unit.
    """
    run: list = []

    def flush():
        if run and any(ch.isalpha() for node in run for ch in _visible_text(node)):
            units.append(list(run))
        run.clear()

    for child in list(element.children):
        if isinstance(child, Tag):
            if child.name in _SKIP_TAGS:
                flush()
            elif child.name in _BLOCK_TAGS or child.find(_BLOCK_TAGS) is not None:
                flush()
                _collect_units(child, units)
            else:
                run.append(child)
        elif type(child) is NavigableString:
            run.append(child)
        else:
            flush()  # comments, CDATA, processing instructions
    flush()


def _serialize(nodes: list) -> str:
    return "".join(n.output_ready() if isinstance(n, NavigableString) else n.decode() for n in nodes)


def _structural_tags(fragment: str) -> Counter:
    soup = BeautifulSoup(fragment, "html.parser")
    return Counter(tag.name for tag in soup.find_all(True) if tag.name in _STRUCTURAL_TAGS)


def _replace(nodes: list, fragment: str) -> None:
    anchor = nodes[0]
    for new in list(BeautifulSoup(fragment, "html.parser").contents):
        anchor.insert_before(new)
    for old in nodes:
        old.extract()


def _translate_batch(batch: list[list], session) -> str:
    """Translate one batch of units in place and return the last translated fragment."""
    originals = [_serialize(nodes) for nodes in batch]
    sources = [original.strip() for original in originals]
    results = session.translate_segments(sources)

    preview = ""
    for nodes, original, source, translated in zip(batch, originals, sources, results):
        if translated is None:
            continue
        if _structural_tags(translated) != _structural_tags(source):
            # Asking for the block on its own usually fixes it; a batch of one is that already.
            translated = session.translate_segments([source])[0] if len(batch) > 1 else None
            if translated is None or _structural_tags(translated) != _structural_tags(source):
                session.warn(_WARN_MARKUP)
                continue
        leading = original[: len(original) - len(original.lstrip())]
        trailing = original[len(original.rstrip()):]
        _replace(nodes, leading + translated + trailing)
        preview = translated
    return preview


def _batches(units: list[list], max_chars: int) -> list[list[list]]:
    batches: list[list[list]] = []
    current: list[list] = []
    current_len = 0
    for nodes in units:
        unit_len = len(_serialize(nodes)) + 20
        if current and current_len + unit_len > max_chars:
            batches.append(current)
            current = []
            current_len = 0
        current.append(nodes)
        current_len += unit_len
    if current:
        batches.append(current)
    return batches


def _find_opf(archive: zipfile.ZipFile) -> str:
    try:
        container = etree.fromstring(archive.read("META-INF/container.xml"))
        return container.find(f".//{{{_CONTAINER_NS}}}rootfile").get("full-path")
    except (KeyError, AttributeError, etree.XMLSyntaxError):
        raise ValueError("File EPUB không hợp lệ: thiếu META-INF/container.xml.") from None


def _read_manifest(opf_root, opf_path: str) -> tuple[list[str], Optional[str]]:
    """Return the chapter files in reading order, and the NCX table of contents if any."""
    base = posixpath.dirname(opf_path)
    items = {}
    for item in opf_root.iter(f"{{{_OPF_NS}}}item"):
        path = posixpath.normpath(posixpath.join(base, unquote(item.get("href", ""))))
        items[item.get("id")] = (path, item.get("media-type", ""))

    spine = [ref.get("idref") for ref in opf_root.iter(f"{{{_OPF_NS}}}itemref")]
    documents = [items[i][0] for i in spine if i in items and items[i][1] in _DOCUMENT_TYPES]
    # Anything outside the spine (the EPUB 3 nav page, for one) comes after the chapters.
    documents += [p for p, kind in items.values() if kind in _DOCUMENT_TYPES and p not in documents]
    ncx = next((p for p, kind in items.values() if kind == _NCX_TYPE), None)
    return documents, ncx


def _translate_metadata(opf_root, ncx_root, session) -> None:
    """Translate the book title and NCX entries, and mark the book as the target language."""
    elements = list(opf_root.iter(f"{{{_DC_NS}}}title"))
    if ncx_root is not None:
        elements += list(ncx_root.iter(f"{{{_NCX_NS}}}text"))
    elements = [e for e in elements if e.text and any(ch.isalpha() for ch in e.text)]

    for start in range(0, len(elements), 80):
        group = elements[start:start + 80]
        results = session.translate_segments([html.escape(e.text.strip(), quote=False) for e in group])
        for element, translated in zip(group, results):
            if translated:
                element.text = html.unescape(_TAG.sub("", translated))

    for language in opf_root.iter(f"{{{_DC_NS}}}language"):
        language.text = config.TARGET_LANGUAGE_CODE


def _render_document(soup: BeautifulSoup) -> bytes:
    root = soup.find("html")
    if root is not None:
        for attribute in ("lang", "xml:lang"):
            if root.has_attr(attribute):
                root[attribute] = config.TARGET_LANGUAGE_CODE
    data = _XML_ENCODING.sub(rb"\1utf-8", soup.encode("utf-8"), count=1)
    for lowered, original in _SVG_ATTRIBUTES:
        data = data.replace(lowered, original)
    return data


def _to_bytes(root) -> bytes:
    return etree.tostring(root.getroottree(), xml_declaration=True, encoding="utf-8")


def translate_epub(input_path: Path, output_path: Path, session, progress: Progress = no_progress) -> None:
    try:
        archive = zipfile.ZipFile(input_path)
    except zipfile.BadZipFile:
        raise ValueError("File EPUB bị hỏng hoặc không phải file EPUB.") from None

    with archive:
        names = set(archive.namelist())
        opf_path = _find_opf(archive)
        opf_root = etree.fromstring(archive.read(opf_path))
        documents, ncx_path = _read_manifest(opf_root, opf_path)

        chapters = []
        for name in documents:
            if name not in names:
                continue
            soup = BeautifulSoup(archive.read(name), "html.parser")
            units: list[list] = []
            if soup.head and soup.head.title:
                _collect_units(soup.head.title, units)
            _collect_units(soup.body or soup, units)
            if units:
                chapters.append((name, soup, units))

        total = sum(len(units) for _, _, units in chapters)
        if not total:
            raise ValueError(
                "Không tìm thấy nội dung văn bản trong EPUB (file có thể được bảo vệ DRM)."
            )

        # Only files in `replaced` are rewritten; everything else is copied byte for byte.
        replaced: dict[str, bytes] = {}
        done = 0
        progress(0, total, "")
        for name, soup, units in chapters:
            for batch in _batches(units, config.MAX_CHARS_PER_CHUNK):
                preview = _translate_batch(batch, session)
                done += len(batch)
                progress(done, total, preview)
            replaced[name] = _render_document(soup)

        ncx_root = None
        if ncx_path in names:
            ncx_root = etree.fromstring(archive.read(ncx_path))
        # Done last, so titles pick up the names the glossary settled on.
        _translate_metadata(opf_root, ncx_root, session)
        replaced[opf_path] = _to_bytes(opf_root)
        if ncx_root is not None:
            replaced[ncx_path] = _to_bytes(ncx_root)

        with zipfile.ZipFile(output_path, "w") as output:
            # The spec requires `mimetype` to be the first entry and stored uncompressed.
            for info in sorted(archive.infolist(), key=lambda info: info.filename != "mimetype"):
                data = replaced.get(info.filename)
                if data is None:
                    data = archive.read(info.filename)
                stored = info.filename == "mimetype"
                output.writestr(
                    info.filename, data, zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
                )
