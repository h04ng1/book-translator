"""HTTP API and single-page UI for the translator."""

from pathlib import Path
from typing import List

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import config
from ..pipeline import HANDLERS, is_supported
from .jobs import JobManager, UploadTooLarge

STATIC_DIR = Path(__file__).parent / "static"


def _safe_name(raw: str) -> str:
    """Keep only the file name of whatever path the browser sent."""
    name = Path((raw or "").replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[-150:] or "book"


def create_app() -> FastAPI:
    app = FastAPI(title="Book Translator", docs_url="/api/docs", redoc_url=None)
    manager = JobManager(Path(config.DATA_DIR), Path(config.OUTPUT_DIR))

    def job_or_404(action, job_id: str):
        try:
            return action(job_id)
        except KeyError:
            raise HTTPException(404, "Không tìm thấy bản dịch này.") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/config")
    def get_config():
        return {
            "model": config.MODEL,
            "target_language": config.TARGET_LANGUAGE,
            "dry_run": config.DRY_RUN,
            "api_key_configured": bool(config.OPENAI_API_KEY),
            "formats": {suffix: out_ext for suffix, (_, out_ext) in HANDLERS.items()},
            "max_upload_mb": config.MAX_UPLOAD_MB,
        }

    @app.get("/api/jobs")
    def list_jobs():
        return manager.snapshot()

    @app.post("/api/jobs", status_code=201)
    def create_jobs(files: List[UploadFile] = File(...)):
        created, rejected = [], []
        for upload in files:
            name = _safe_name(upload.filename)
            if not is_supported(name):
                rejected.append({"filename": name, "reason": "Chỉ hỗ trợ file .epub, .pdf và .txt."})
                continue
            try:
                created.append(manager.submit(name, upload.file))
            except UploadTooLarge:
                rejected.append(
                    {"filename": name, "reason": f"File lớn hơn {config.MAX_UPLOAD_MB} MB."}
                )
        if not created and rejected:
            raise HTTPException(400, rejected[0]["reason"])
        return {"jobs": created, "rejected": rejected}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        return job_or_404(manager.cancel, job_id)

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str):
        return job_or_404(manager.retry, job_id)

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def remove_job(job_id: str):
        job_or_404(manager.remove, job_id)

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str):
        path = job_or_404(manager.output_file, job_id)
        if path is None:
            raise HTTPException(404, "Bản dịch chưa sẵn sàng hoặc file đã bị xóa khỏi output/.")
        return FileResponse(path, filename=path.name)

    return app
