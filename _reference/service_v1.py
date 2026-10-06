"""Задания на проверку: хранение файлов, запуск в фоне, статус, уведомление в Telegram.

MVP хранит всё на диске (data/jobs/<id>/meta.json). Когда появятся организации и роли —
перенести в PostgreSQL по схеме organizations/projects/documents/checks/findings.
"""
import json
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config
from smeta_check.pipeline import run_package

ALLOWED_EXT = {".xlsx", ".xlsm", ".xls", ".pdf"}
_executor = ThreadPoolExecutor(max_workers=config.WORKERS)
_lock = threading.Lock()


class UploadError(Exception):
    pass


def _jobs_dir() -> str:
    d = os.path.join(config.DATA_DIR, "jobs")
    os.makedirs(d, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    """Имя файла без путей и спецсимволов: защита от ../../ в имени."""
    name = os.path.basename(name.replace("\\", "/")).strip() or "file"
    name = re.sub(r"[^\w.\- ()№]+", "_", name, flags=re.UNICODE)
    return name[:150]


def _meta_path(job_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise KeyError(job_id)
    return os.path.join(_jobs_dir(), job_id, "meta.json")


def _save(meta: dict) -> None:
    p = _meta_path(meta["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, p)


def create_job(user: dict, uploads: list, title: str = "") -> str:
    """uploads: [(filename, file-like)]. Пишем потоково, считаем общий объём."""
    if not uploads:
        raise UploadError("Добавьте хотя бы один файл")
    job_id = uuid.uuid4().hex
    jdir = os.path.join(_jobs_dir(), job_id)
    fdir = os.path.join(jdir, "files")
    os.makedirs(fdir)
    limit, total, names = config.MAX_UPLOAD_MB * 1024 * 1024, 0, []
    try:
        for fname, fobj in uploads:
            name = safe_name(fname)
            if os.path.splitext(name)[1].lower() not in ALLOWED_EXT:
                raise UploadError(f"«{name}»: поддерживаются .xlsx, .xls и .pdf")
            base, ext = os.path.splitext(name)
            i = 1
            while name in names:
                name, i = f"{base}_{i}{ext}", i + 1
            with open(os.path.join(fdir, name), "wb") as out:
                while chunk := fobj.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise UploadError(f"Пакет больше {config.MAX_UPLOAD_MB} МБ — разделите его на части")
                    out.write(chunk)
            names.append(name)
    except Exception:
        shutil.rmtree(jdir, ignore_errors=True)
        raise
    meta = {"id": job_id, "user_id": user["id"], "title": title.strip()[:120], "created": time.time(),
            "status": "queued", "files": names, "bytes": total, "result": None, "error": None}
    _save(meta)
    _executor.submit(_run, job_id)
    return job_id


def _run(job_id: str) -> None:
    meta = load(job_id)
    meta["status"] = "running"
    _save(meta)
    jdir = os.path.dirname(_meta_path(job_id))
    try:
        paths = [os.path.join(jdir, "files", n) for n in meta["files"]]
        meta["result"] = run_package(paths, jdir)
        meta["status"] = "done"
    except Exception as e:  # отчёт об ошибке — пользователю, трассировка — в лог
        traceback.print_exc()
        meta["status"], meta["error"] = "error", f"Проверка не выполнилась: {type(e).__name__}. Мы уже видим ошибку в логах."
    meta["finished"] = time.time()
    _save(meta)
    notify(meta)


def notify(meta: dict) -> None:
    """Сообщение в чат с ботом: проверка готова. Без внешних библиотек."""
    if not config.BOT_TOKEN:
        return
    if meta["status"] == "done":
        c = meta["result"]["counts"]
        text = (f"Проверка готова: {meta['result']['title']}\n"
                f"🔴 {c['FAIL']} · 🟡 {c['WARNING']} · 🔵 {c['NEEDS_CONTEXT']} · 🟢 {c['OK']}")
    else:
        text = meta.get("error") or "Проверка не выполнилась"
    body = {"chat_id": meta["user_id"], "text": text}
    if config.WEBAPP_URL:
        body["reply_markup"] = json.dumps({"inline_keyboard": [[{
            "text": "Открыть отчёт", "web_app": {"url": f"{config.WEBAPP_URL.rstrip('/')}/?job={meta['id']}"}}]]})
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                                     data=urllib.parse.urlencode(body).encode())
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        traceback.print_exc()


def load(job_id: str) -> dict:
    with open(_meta_path(job_id), encoding="utf-8") as f:
        return json.load(f)


def get_job(job_id: str, user_id: int) -> dict:
    meta = load(job_id)
    if meta["user_id"] != user_id:
        raise KeyError(job_id)          # чужое задание выглядит как несуществующее
    return meta


def list_jobs(user_id: int, limit: int = 20) -> list:
    out = []
    for jid in os.listdir(_jobs_dir()):
        try:
            m = load(jid)
        except (KeyError, FileNotFoundError, json.JSONDecodeError):
            continue
        if m["user_id"] == user_id:
            r = m.get("result") or {}
            out.append({"id": m["id"], "title": m["title"] or r.get("title") or ", ".join(m["files"][:2]),
                        "created": m["created"], "status": m["status"], "counts": r.get("counts")})
    return sorted(out, key=lambda x: -x["created"])[:limit]


def report_path(job_id: str) -> str:
    return os.path.join(os.path.dirname(_meta_path(job_id)), "report.xlsx")


def purge_old() -> int:
    cutoff, n = time.time() - config.RETENTION_DAYS * 86400, 0
    for jid in os.listdir(_jobs_dir()):
        try:
            if load(jid)["created"] < cutoff:
                shutil.rmtree(os.path.join(_jobs_dir(), jid), ignore_errors=True)
                n += 1
        except Exception:
            continue
    return n
