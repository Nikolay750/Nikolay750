"""HTTP-сервер Mini App: отдаёт интерфейс и API.

Запуск:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import os
from typing import List, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import config, service
from .tg_auth import AuthError, check_link, sign_link, validate_init_data

app = FastAPI(title="Сверка смет и актов", docs_url=None, redoc_url=None)
WEBAPP = os.path.join(os.path.dirname(__file__), "..", "webapp", "index.html")


@app.on_event("startup")
def _startup():
    service.purge_old()


def current_user(init_data: Optional[str]) -> dict:
    if config.DEV_MODE and not init_data:
        return {"id": 0, "first_name": "dev"}
    try:
        return validate_init_data(init_data or "", config.BOT_TOKEN, config.INITDATA_MAX_AGE)
    except AuthError as e:
        raise HTTPException(401, str(e))


@app.get("/")
def index():
    return FileResponse(WEBAPP, headers={"Cache-Control": "no-cache"})


@app.get("/api/jobs")
def jobs(x_tg_init_data: Optional[str] = Header(None)):
    return service.list_jobs(current_user(x_tg_init_data)["id"])


@app.post("/api/jobs")
def create(files: List[UploadFile] = File(...), title: str = Form(""),
           x_tg_init_data: Optional[str] = Header(None)):
    user = current_user(x_tg_init_data)
    try:
        jid = service.create_job(user, [(f.filename, f.file) for f in files], title)
    except service.UploadError as e:
        raise HTTPException(400, str(e))
    return {"id": jid}


@app.get("/api/jobs/{job_id}")
def job(job_id: str, x_tg_init_data: Optional[str] = Header(None)):
    user = current_user(x_tg_init_data)
    try:
        meta = service.get_job(job_id, user["id"])
    except (KeyError, FileNotFoundError):
        raise HTTPException(404, "Проверка не найдена")
    if meta["status"] == "done" and (meta.get("result") or {}).get("report"):
        meta["report_url"] = f"/api/jobs/{job_id}/report?t={sign_link(config.BOT_TOKEN or 'dev', job_id)}"
    return meta


@app.get("/api/jobs/{job_id}/report")
def report(job_id: str, t: str):
    if not check_link(config.BOT_TOKEN or "dev", job_id, t):
        raise HTTPException(403, "Ссылка устарела — откройте отчёт заново")
    try:
        path = service.report_path(job_id)
    except KeyError:
        raise HTTPException(404)
    if not os.path.exists(path):
        raise HTTPException(404, "Отчёта нет")
    return FileResponse(path, filename=f"otchet_{job_id[:8]}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
