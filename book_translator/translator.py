"""OpenAI-backed translation that keeps one book consistent from start to end.

A `Translator` owns the API client and the on-disk cache. Each book gets its own
`BookSession`, which carries a glossary and the tail of the translation so far
into every request, so names, terms and forms of address stay the same across chunks.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import NamedTuple, Optional

import openai
from openai import OpenAI

from . import config
from .common import split_into_chunks

# Bump when the prompts change, so cached translations made with the old ones are not reused.
PROMPT_VERSION = "2"

PREVIOUS_CHARS = 1200
MAX_GLOSSARY_TERMS = 200

_BASE_PROMPT = """\
You are translating a book into {target}. Readers will go through it cover to cover, \
so it has to read as if it had been written in {target}, not like a translation.

Translating
- Carry over meaning, tone and register; rebuild sentences wherever {target} phrases \
things differently instead of following the source word order.
- Translate everything. Do not summarise, skip, explain, or add translator's notes.
- Keep names of people and places in their original spelling unless a {target} form \
is well established.
- Where {target} marks relationships in its pronouns and forms of address (as \
Vietnamese does), choose them from who is speaking to whom, and keep each pair of \
characters consistent.
- Leave numbers, code, formulas, URLs and e-mail addresses as they are.

Consistency
The request may include a <glossary> of terms already fixed for this book and a \
<previous> excerpt, the end of the translation so far. Use the glossary renderings \
exactly and continue naturally from the excerpt, without repeating it.

{mode}

Reply in exactly this shape and nothing else:
<translation>
the translated passage
</translation>
<glossary>
source term => {target} rendering
</glossary>
List in <glossary> only new recurring names, key terms or forms of address that later \
passages must render the same way, one per line; leave it empty when there are none.
"""

_MODE_PROMPTS = {
    "text": (
        "Source format\n"
        "The <source> is plain text. Keep its paragraph breaks (blank lines). When the "
        "lines inside a paragraph are only prose hard-wrapped at a fixed width, write "
        "the translated paragraph as a single line; keep line breaks that carry meaning "
        "(verse, lists, addresses, tables)."
    ),
    "pdf": (
        "Source format\n"
        "The <source> was extracted from a PDF, so it is noisy: lines are hard-wrapped, "
        "words may be hyphenated across lines, page numbers and running headers or "
        "footers may be mixed in, and it may start or end in the middle of a paragraph. "
        "Rebuild clean paragraphs separated by one blank line, drop page numbers and "
        "running headers/footers, and put each chapter or section heading on its own "
        "line starting with '# ' (chapter) or '## ' (section). Use no other markup."
    ),
    "html": (
        "Source format\n"
        'The <source> is a list of <seg id="N">...</seg> blocks taken from the book\'s '
        "HTML. Return every block with the same id, in the same order, with its text "
        "translated. Inline tags inside a block (<i>, <b>, <a href>, <span class>, "
        "<sup>, <br/> and so on) must stay, with identical attributes, wrapped around "
        "the corresponding translated words; never add or drop a tag. Leave the "
        "content of <code> untranslated and keep the text valid HTML (& as &amp;, "
        "< as &lt;)."
    ),
}

_TRANSLATION_BLOCK = re.compile(r"<translation>\s*(.*)</translation>", re.S)
_GLOSSARY_BLOCK = re.compile(r"<glossary>(.*?)</glossary>", re.S)
_SEGMENT = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)
_TAG = re.compile(r"<[^>]+>")

_WARN_TRUNCATED = "đoạn quá dài, model không dịch hết nên giữ nguyên bản gốc"
_WARN_REFUSED = "đoạn bị model từ chối dịch nên giữ nguyên bản gốc"
_WARN_MISSING = "đoạn model trả về thiếu nên giữ nguyên bản gốc"


class _Truncated(Exception):
    """The reply hit the output-token limit before the translation was complete."""


class _Refused(Exception):
    """The model declined or returned nothing usable."""


class Completion(NamedTuple):
    text: str
    finish_reason: str
    input_tokens: int
    output_tokens: int


class TranslationCache:
    """One JSON file per translated chunk, so an interrupted book resumes for free."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> Optional[dict]:
        try:
            return json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, key: str, value: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def _system_prompt(mode: str) -> str:
    return _BASE_PROMPT.format(target=config.TARGET_LANGUAGE, mode=_MODE_PROMPTS[mode])


def _parse_reply(reply: str) -> tuple[str, dict[str, str]]:
    """Split a model reply into the translated passage and its new glossary terms."""
    terms: dict[str, str] = {}
    glossary = _GLOSSARY_BLOCK.search(reply)
    if glossary:
        for line in glossary.group(1).splitlines():
            source, arrow, rendering = line.partition("=>")
            if arrow and source.strip() and rendering.strip():
                terms[source.strip()] = rendering.strip()

    translation = _TRANSLATION_BLOCK.search(reply)
    if translation:
        body = translation.group(1)
    else:
        # The model ignored the envelope: take everything that is not glossary.
        body = _GLOSSARY_BLOCK.sub("", reply).replace("<translation>", "")
    return body.strip(), terms


def _fake_completion(mode: str, source: str) -> Completion:
    """Stand-in for the API in demo mode: marks text instead of translating it."""
    time.sleep(0.25)
    if mode == "html":
        body = _SEGMENT.sub(lambda m: f'<seg id="{m.group(1)}">[VI] {m.group(2)}</seg>', source)
    else:
        body = "\n\n".join(f"[VI] {p}" for p in re.split(r"\n\s*\n", source.strip()))
    return Completion(f"<translation>\n{body}\n</translation>\n<glossary>\n</glossary>", "stop", 0, 0)


class Translator:
    def __init__(self, model: Optional[str] = None, max_retries: int = 5):
        self.model = model or config.MODEL
        self.dry_run = config.DRY_RUN
        self.client: Optional[OpenAI] = None
        if not self.dry_run:
            if not config.OPENAI_API_KEY:
                raise RuntimeError(
                    "OPENAI_API_KEY chưa được thiết lập. Hãy copy .env.example thành "
                    ".env và điền API key của bạn."
                )
            self.client = OpenAI(api_key=config.OPENAI_API_KEY, max_retries=max_retries)
        self.cache = TranslationCache(Path(config.DATA_DIR) / "cache")

    def session(self) -> "BookSession":
        return BookSession(self)

    def cache_key(self, mode: str, source: str) -> str:
        parts = [PROMPT_VERSION, self.model, config.TARGET_LANGUAGE, mode, source]
        return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()

    def complete(self, system: str, user: str) -> Completion:
        response = self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=config.MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        choice = response.choices[0]
        usage = response.usage
        return Completion(
            text=choice.message.content or "",
            finish_reason=choice.finish_reason or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )


class BookSession:
    """Translation state for one book: glossary, running context, usage, warnings."""

    def __init__(self, translator: Translator):
        self.translator = translator
        self.glossary: dict[str, str] = {}
        self.previous = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_requests = 0
        self._warnings: Counter = Counter()

    @property
    def warnings(self) -> list[str]:
        return [f"{count} {message}" for message, count in self._warnings.items()]

    def warn(self, message: str, count: int = 1) -> None:
        self._warnings[message] += count

    def translate(self, text: str, mode: str = "text") -> str:
        """Translate a passage of plain text, returning the source if it cannot be done."""
        if not text.strip():
            return text
        return self._translate_text(text, mode, depth=0)

    def _translate_text(self, text: str, mode: str, depth: int) -> str:
        try:
            body = self._request(mode, text)
        except _Refused:
            self._warnings[_WARN_REFUSED] += 1
            return text
        except _Truncated:
            halves = split_into_chunks(text, len(text) // 2 + 1) if depth < 3 else []
            if len(halves) < 2:
                self._warnings[_WARN_TRUNCATED] += 1
                return text
            return "\n\n".join(self._translate_text(half, mode, depth + 1) for half in halves)
        self.previous = body[-PREVIOUS_CHARS:]
        return body

    def translate_segments(self, segments: list[str]) -> list[Optional[str]]:
        """Translate HTML fragments. A None entry means that fragment must stay as it was."""
        if not segments:
            return []
        source = "\n".join(f'<seg id="{i}">{seg}</seg>' for i, seg in enumerate(segments, 1))
        try:
            body = self._request("html", source)
        except _Refused:
            self._warnings[_WARN_REFUSED] += len(segments)
            return [None] * len(segments)
        except _Truncated:
            if len(segments) == 1:
                self._warnings[_WARN_TRUNCATED] += 1
                return [None]
            middle = len(segments) // 2
            return self.translate_segments(segments[:middle]) + self.translate_segments(segments[middle:])

        found = {int(m.group(1)): m.group(2).strip() for m in _SEGMENT.finditer(body)}
        result: list[Optional[str]] = [found.get(i) or None for i in range(1, len(segments) + 1)]
        for index, value in enumerate(result):
            if value is not None:
                continue
            if len(segments) == 1:
                self._warnings[_WARN_MISSING] += 1
            else:
                # The model dropped this block from the batch: ask for it on its own.
                result[index] = self.translate_segments([segments[index]])[0]

        plain = _TAG.sub("", " ".join(value for value in result if value))
        if plain:
            self.previous = plain[-PREVIOUS_CHARS:]
        return result

    def _request(self, mode: str, source: str) -> str:
        translator = self.translator
        key = translator.cache_key(mode, source)
        entry = None if translator.dry_run else translator.cache.get(key)
        if entry is not None:
            self.cached_requests += 1
        else:
            if translator.dry_run:
                completion = _fake_completion(mode, source)
            else:
                completion = translator.complete(_system_prompt(mode), self._user_message(source))
            self.input_tokens += completion.input_tokens
            self.output_tokens += completion.output_tokens
            if completion.finish_reason == "length":
                raise _Truncated()
            body, terms = _parse_reply(completion.text)
            if completion.finish_reason == "content_filter" or not body:
                raise _Refused()
            entry = {"translation": body, "glossary": terms}
            if not translator.dry_run:
                translator.cache.put(key, entry)

        for term, rendering in entry["glossary"].items():
            # First rendering wins: changing a name halfway through is the bug we are avoiding.
            if len(self.glossary) < MAX_GLOSSARY_TERMS and len(term) <= 80:
                self.glossary.setdefault(term, rendering)
        return entry["translation"]

    def _user_message(self, source: str) -> str:
        parts = []
        if self.glossary:
            terms = "\n".join(f"{term} => {rendering}" for term, rendering in self.glossary.items())
            parts.append(f"<glossary>\n{terms}\n</glossary>")
        if self.previous:
            parts.append(f"<previous>\n{self.previous}\n</previous>")
        parts.append(f"<source>\n{source}\n</source>")
        return "\n\n".join(parts)


def describe_error(exc: Exception) -> str:
    """Turn an exception from a translation run into a message for the user."""
    if isinstance(exc, openai.AuthenticationError):
        return "API key OpenAI không hợp lệ. Kiểm tra lại OPENAI_API_KEY trong file .env."
    if isinstance(exc, openai.PermissionDeniedError):
        return f"API key không có quyền dùng model này: {exc.message}"
    if isinstance(exc, openai.NotFoundError):
        return f"Không tìm thấy model. Kiểm tra TRANSLATE_MODEL trong file .env ({exc.message})"
    if isinstance(exc, openai.RateLimitError):
        if "quota" in str(exc).lower() or "credit" in str(exc).lower():
            return (
                "Tài khoản OpenAI đã hết credit. Nạp thêm tại "
                "platform.openai.com/settings/organization/billing rồi bấm Thử lại; "
                "các đoạn đã dịch được giữ lại."
            )
        return "OpenAI đang giới hạn tốc độ. Đợi một lúc rồi bấm Thử lại."
    if isinstance(exc, openai.BadRequestError):
        return f"OpenAI từ chối yêu cầu: {exc.message}"
    if isinstance(exc, openai.APIStatusError):
        return f"Máy chủ OpenAI đang lỗi ({exc.status_code}). Thử lại sau, các đoạn đã dịch được giữ lại."
    if isinstance(exc, openai.APIConnectionError):
        return "Không kết nối được tới OpenAI. Kiểm tra mạng rồi thử lại."
    return str(exc) or exc.__class__.__name__
