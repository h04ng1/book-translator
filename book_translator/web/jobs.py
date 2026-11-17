"""In-process job queue: one worker thread translates uploaded books in order.

Books are translated one at a time on purpose. A book is a long run of sequential API
calls (each chunk needs the context of the one before), and running several books at
once would only make them compete for the same rate limit.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional

from .. import config
from ..pipeline import output_name, translate_file
from ..translator import Translator, describe_error

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running", "cancelling")
_TAG = re.compile(r"<[^>]+>")
_INTERRUPTED = (
    "Server khởi động lại khi đang dịch. Bấm Thử lại để dịch tiếp; "
    "các đoạn đã dịch được giữ lại."
)


class JobCancelled(Exception):
    pass


class UploadTooLarge(Exception):
    pass


@dataclass
class Job:
    id: str
    filename: str
    source_path: str
    output_path: str = ""
    status: str = "queued"
    done: int = 0
    total: int = 0
    preview: str = ""
    error: str = ""
    warnings: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_requests: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def public(self) -> dict:
        """What the browser gets: no server paths."""
        data = asdict(self)
        del data["source_path"], data["output_path"]
        data["output_name"] = Path(self.output_path).name if self.output_path else ""
        return data


def _excerpt(text: str, limit: int = 260) -> str:
    plain = " ".join(_TAG.sub("", text).split())
    return plain if len(plain) <= limit else plain[:limit].rstrip() + "…"


class JobManager:
    def __init__(self, data_dir: Path, output_dir: Path):
        self.uploads_dir = data_dir / "uploads"
        self.output_dir = output_dir
        self._state_file = data_dir / "jobs.json"
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._queue: queue.Queue = queue.Queue()

        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._load()
        threading.Thread(target=self._worker, name="translate-worker", daemon=True).start()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            records = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for record in records:
            job = Job(**record)
            if job.status in ACTIVE_STATUSES:
                job.status = "interrupted"
                job.error = _INTERRUPTED
            self._jobs[job.id] = job

    def _save(self) -> None:
        records = [asdict(job) for job in self._jobs.values()]
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)

    # -- public API ------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)
            return [job.public() for job in jobs]

    def submit(self, filename: str, stream: BinaryIO) -> dict:
        """Store an upload and queue it. Raises UploadTooLarge past the size limit."""
        job_id = uuid.uuid4().hex[:12]
        source = self.uploads_dir / job_id / filename
        source.parent.mkdir(parents=True)
        limit = config.MAX_UPLOAD_MB * 1024 * 1024
        written = 0
        with source.open("wb") as target:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                written += len(block)
                if written > limit:
                    target.close()
                    shutil.rmtree(source.parent, ignore_errors=True)
                    raise UploadTooLarge()
                target.write(block)

        job = Job(id=job_id, filename=filename, source_path=str(source))
        with self._lock:
            self._jobs[job_id] = job
            self._enqueue(job)
            return job.public()

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs[job_id]
            if job.status == "queued":
                self._finish(job, "cancelled")
            elif job.status == "running":
                # The worker notices between two chunks, so this can take a moment.
                job.status = "cancelling"
                self._cancel[job_id].set()
            return job.public()

    def retry(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in ACTIVE_STATUSES or job.status == "done":
                raise ValueError("Chỉ thử lại được bản dịch đã dừng hoặc bị lỗi.")
            if not Path(job.source_path).exists():
                raise ValueError("File gốc không còn trên server, hãy tải lên lại.")
            job.status = "queued"
            job.done = job.total = 0
            job.preview = job.error = ""
            job.warnings = []
            job.started_at = job.finished_at = None
            self._enqueue(job)
            return job.public()

    def remove(self, job_id: str) -> None:
        """Forget a finished job and its upload. The translated file in output/ stays."""
        with self._lock:
            job = self._jobs[job_id]
            if job.status in ACTIVE_STATUSES:
                raise ValueError("Hãy hủy bản dịch trước khi xóa.")
            del self._jobs[job_id]
            self._cancel.pop(job_id, None)
            self._save()
        shutil.rmtree(self.uploads_dir / job_id, ignore_errors=True)

    def output_file(self, job_id: str) -> Optional[Path]:
        with self._lock:
            job = self._jobs[job_id]
            path = Path(job.output_path) if job.status == "done" and job.output_path else None
        return path if path and path.exists() else None

    # -- worker ----------------------------------------------------------------

    def _enqueue(self, job: Job) -> None:
        self._cancel[job.id] = threading.Event()
        self._queue.put(job.id)
        self._save()

    def _finish(self, job: Job, status: str, error: str = "") -> None:
        job.status = status
        job.error = error
        job.finished_at = time.time()
        self._save()

    def _unique_output(self, name: str) -> Path:
        path = self.output_dir / name
        counter = 2
        while path.exists():
            path = self.output_dir / f"{Path(name).stem} ({counter}){Path(name).suffix}"
            counter += 1
        return path

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue  # cancelled or removed while waiting
                job.status = "running"
                job.started_at = time.time()
                cancel = self._cancel[job_id]
                self._save()
            self._run(job, cancel)

    def _run(self, job: Job, cancel: threading.Event) -> None:
        output = self._unique_output(output_name(job.filename))
        session = None

        def progress(done: int, total: int, preview: str = "") -> None:
            if cancel.is_set():
                raise JobCancelled()
            with self._lock:
                job.done, job.total = done, total
                if preview:
                    job.preview = _excerpt(preview)
                job.input_tokens = session.input_tokens
                job.output_tokens = session.output_tokens
                job.cached_requests = session.cached_requests

        try:
            session = Translator().session()
            translate_file(Path(job.source_path), output, session, progress)
        except JobCancelled:
            output.unlink(missing_ok=True)
            with self._lock:
                self._finish(job, "cancelled")
        except Exception as exc:
            log.exception("Translating %s failed", job.filename)
            output.unlink(missing_ok=True)
            with self._lock:
                self._finish(job, "failed", describe_error(exc))
        else:
            with self._lock:
                job.output_path = str(output)
                job.warnings = session.warnings
                self._finish(job, "done")
