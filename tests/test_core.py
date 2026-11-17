import re
import zipfile

import pytest

from book_translator import config
from book_translator.common import split_into_chunks
from book_translator.formats.epub_handler import translate_epub
from book_translator.formats.pdf_handler import _strip_running_lines
from book_translator.translator import Completion, TranslationCache, Translator, _parse_reply

SEGMENT = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)


def reply(body: str, glossary: str = "") -> Completion:
    text = f"<translation>\n{body}\n</translation>\n<glossary>\n{glossary}\n</glossary>"
    return Completion(text, "stop", 10, 10)


@pytest.fixture
def translator(tmp_path, monkeypatch):
    """A Translator with no API client; each test supplies `complete`."""
    monkeypatch.setattr(config, "DRY_RUN", True)
    translator = Translator()
    translator.dry_run = False
    translator.cache = TranslationCache(tmp_path / "cache")
    return translator


# -- chunking ------------------------------------------------------------------


def test_chunks_split_on_windows_paragraph_breaks():
    text = "First paragraph.\r\n\r\nSecond paragraph.\r\n\r\nThird paragraph."
    assert split_into_chunks(text, 40) == [
        "First paragraph.\n\nSecond paragraph.",
        "Third paragraph.",
    ]


def test_long_paragraph_is_cut_between_sentences():
    sentences = [f"Sentence number {i} ends here." for i in range(40)]
    chunks = split_into_chunks(" ".join(sentences), 200)
    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert all(chunk.endswith("ends here.") for chunk in chunks)
    assert " ".join(chunks) == " ".join(sentences)


def test_text_without_sentence_ends_is_never_cut_mid_word():
    words = ["lantern"] * 100
    chunks = split_into_chunks(" ".join(words), 90)
    assert [word for chunk in chunks for word in chunk.split()] == words


# -- reply parsing -------------------------------------------------------------


def test_parse_reply_reads_translation_and_glossary():
    body, terms = _parse_reply(reply("Xin chào.", "Mira => Mira\nthe Guild => Hội").text)
    assert body == "Xin chào."
    assert terms == {"Mira": "Mira", "the Guild": "Hội"}


def test_parse_reply_without_envelope_keeps_the_text():
    assert _parse_reply("Xin chào.") == ("Xin chào.", {})


# -- book session --------------------------------------------------------------


def test_glossary_and_previous_text_reach_the_next_request(translator):
    requests = []

    def complete(system, user):
        requests.append(user)
        return reply(f"dịch {len(requests)}", "the Guild => Hội")

    translator.complete = complete
    session = translator.session()
    session.translate("First passage.")
    session.translate("Second passage.")

    assert "<glossary>" not in requests[0]
    assert "the Guild => Hội" in requests[1]
    assert "<previous>\ndịch 1\n</previous>" in requests[1]


def test_cached_chunk_is_not_requested_again(translator):
    calls = []

    def complete(system, user):
        calls.append(user)
        return reply("bản dịch")

    translator.complete = complete
    assert translator.session().translate("Same passage.") == "bản dịch"
    second = translator.session()
    assert second.translate("Same passage.") == "bản dịch"
    assert len(calls) == 1
    assert second.cached_requests == 1


def test_segment_dropped_by_the_model_is_requested_again(translator):
    calls = []

    def complete(system, user):
        source = user.split("<source>")[1]
        segments = SEGMENT.findall(source)
        calls.append(len(segments))
        if len(segments) == 3:
            segments = [segments[0], segments[2]]  # the model forgets the middle one
        return reply("".join(f'<seg id="{i}">VI {text}</seg>' for i, text in segments))

    translator.complete = complete
    result = translator.session().translate_segments(["one", "two", "three"])
    assert result == ["VI one", "VI two", "VI three"]
    assert calls == [3, 1]


def test_truncated_reply_is_retried_in_smaller_pieces(translator):
    def complete(system, user):
        source = user.split("<source>\n")[1].split("\n</source>")[0]
        if len(source) > 60:
            return Completion("<translation>cut off", "length", 10, 10)
        return reply(source.upper())

    translator.complete = complete
    session = translator.session()
    text = "The first sentence is here. The second sentence follows it. The third one closes."
    assert session.translate(text).replace("\n\n", " ") == text.upper()
    assert session.warnings == []


def test_refused_chunk_keeps_the_source_and_warns(translator):
    translator.complete = lambda system, user: Completion("", "content_filter", 10, 0)
    session = translator.session()
    assert session.translate("Some passage.") == "Some passage."
    assert len(session.warnings) == 1


# -- pdf -----------------------------------------------------------------------


def test_running_headers_and_page_numbers_are_removed():
    pages = [["The Quiet Harbour", f"Body text of page {n}.", str(n)] for n in range(1, 7)]
    assert _strip_running_lines(pages) == [[f"Body text of page {n}."] for n in range(1, 7)]


# -- epub ----------------------------------------------------------------------

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">
<head><title>Chapter One</title><link href="style.css" rel="stylesheet" type="text/css"/></head>
<body>
  <h1>Chapter One</h1>
  <p class="first">She was <i>very</i> tired<a id="n1" href="#fn1"><sup>1</sup></a>, but kept walking.</p>
  <div>Loose text before a block.<p>Fish &amp; chips cost &lt; 5 coins.</p></div>
  <p>* * *</p>
  <pre>keep = this(code)</pre>
  <img src="cover.png" alt=""/>
</body>
</html>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>The Quiet Harbour</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="img" href="cover.png" media-type="image/png"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="ch1"/></spine>
</package>
"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap><navPoint id="p1" playOrder="1"><navLabel><text>Chapter One</text></navLabel>
  <content src="ch1.xhtml"/></navPoint></navMap>
</ncx>
"""

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

PNG = b"\x89PNG\r\n\x1a\n not really an image"
CSS = b"p.first { text-indent: 0 }"


@pytest.fixture
def translated_epub(tmp_path, translator):
    source = tmp_path / "book.epub"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("OEBPS/ch1.xhtml", CHAPTER)
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr("OEBPS/toc.ncx", NCX)
        archive.writestr("OEBPS/style.css", CSS)
        archive.writestr("OEBPS/cover.png", PNG)

    sent = []

    def complete(system, user):
        segments = SEGMENT.findall(user.split("<source>")[1])
        sent.extend(text for _, text in segments)
        return reply("".join(f'<seg id="{i}">VI {text}</seg>' for i, text in segments))

    translator.complete = complete
    output = tmp_path / "book_vi.epub"
    progress = []
    translate_epub(source, output, translator.session(), lambda done, total, preview="": progress.append((done, total)))
    return zipfile.ZipFile(output), sent, progress


def test_epub_sends_whole_blocks_with_their_inline_markup(translated_epub):
    _, sent, _ = translated_epub
    assert 'She was <i>very</i> tired<a href="#fn1" id="n1"><sup>1</sup></a>, but kept walking.' in sent
    assert "Loose text before a block." in sent
    assert "Fish &amp; chips cost &lt; 5 coins." in sent
    assert not any("keep = this" in text or "* * *" in text for text in sent)


def test_epub_chapter_keeps_structure_around_translated_text(translated_epub):
    archive, _, progress = translated_epub
    chapter = archive.read("OEBPS/ch1.xhtml").decode("utf-8")
    assert '<p class="first">VI She was <i>very</i> tired' in chapter
    assert "<div>VI Loose text before a block.<p>VI Fish &amp; chips cost &lt; 5 coins.</p></div>" in chapter
    assert "<pre>keep = this(code)</pre>" in chapter
    assert "<p>* * *</p>" in chapter
    assert '<link href="style.css" rel="stylesheet" type="text/css"/>' in chapter
    assert 'lang="vi"' in chapter and 'xml:lang="vi"' in chapter
    assert progress[0] == (0, 5) and progress[-1] == (5, 5)


def test_epub_metadata_is_translated_and_other_files_are_untouched(translated_epub):
    archive, _, _ = translated_epub
    assert archive.infolist()[0].filename == "mimetype"
    assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED
    assert archive.read("OEBPS/cover.png") == PNG
    assert archive.read("OEBPS/style.css") == CSS
    opf = archive.read("OEBPS/content.opf").decode("utf-8")
    assert "<dc:title>VI The Quiet Harbour</dc:title>" in opf
    assert "<dc:language>vi</dc:language>" in opf
    assert "<text>VI Chapter One</text>" in archive.read("OEBPS/toc.ncx").decode("utf-8")
