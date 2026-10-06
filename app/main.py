"""Step 6: HTTP backend Mini App — экран очереди оператора.

НЕ ЗАПУСКАЛСЯ в этой сессии: fastapi/uvicorn не ставятся — та же причина,
что у aiogram в bot/bot.py (pypi.org отвечает 403 даже при прямом обращении
из этой песочницы). Вся логика очереди вынесена в app/queue_service.py и
протестирована без FastAPI (tests/test_queue_service.py); этот файл —
тонкий HTTP-слой поверх неё, написанный по документированному API FastAPI.
ПЕРЕД ПРОДОМ обязателен смоук-тест: запустить uvicorn и открыть реальный
Mini App с initData настоящего оператора.

Запуск:  uvicorn app.main:app --host 0.0.0.0 --port 8000

Экран открыт ТОЛЬКО оператору (config.OPERATOR_TG_ID) — это не самообслуживание
клиента, см. архитектуру цикла A+B+E: Mini App = экран эксперта, а не канал
приёма файлов от подрядчика (тот идёт через бота, см. Step 4).
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from . import config, delivery_service, queue_service, review_service
from .tg_auth import AuthError, check_link, sign_link, validate_init_data

app = FastAPI(title="Проверка ИД — очередь оператора", docs_url=None, redoc_url=None)
WEBAPP_INDEX = os.path.join(os.path.dirname(__file__), "..", "webapp", "index.html")


def current_operator(init_data: Optional[str]) -> dict:
    """Проверяет подпись initData И что это именно оператор, а не случайный
    пользователь, узнавший адрес Mini App — экран очереди не самообслуживание."""
    if config.DEV_MODE and not init_data:
        return {"id": config.OPERATOR_TG_ID or "0"}
    try:
        user = validate_init_data(init_data or "", config.BOT_TOKEN, config.INITDATA_MAX_AGE)
    except AuthError as e:
        raise HTTPException(401, str(e))
    if config.OPERATOR_TG_ID and str(user.get("id")) != str(config.OPERATOR_TG_ID):
        raise HTTPException(403, "Экран доступен только оператору проверки")
    return user


@app.get("/")
def index():
    return FileResponse(WEBAPP_INDEX, headers={"Cache-Control": "no-cache"})


@app.get("/api/queue")
def queue(include_history: bool = False, x_tg_init_data: Optional[str] = Header(None)):
    current_operator(x_tg_init_data)
    return queue_service.list_queue(include_history=include_history)


@app.get("/api/packages/{client_id}/{package_id}")
def package_detail(client_id: str, package_id: str, x_tg_init_data: Optional[str] = Header(None)):
    current_operator(x_tg_init_data)
    try:
        detail = queue_service.get_package_detail(client_id, package_id)
    except KeyError:
        raise HTTPException(404, "Пакет не найден")
    if detail.get("report_path"):
        detail["report_url"] = (
            f"/api/packages/{client_id}/{package_id}/report"
            f"?t={sign_link(config.link_signing_secret(), f'{client_id}:{package_id}')}"
        )
    return detail


@app.get("/api/packages/{client_id}/{package_id}/report")
def package_report(client_id: str, package_id: str, t: str):
    if not check_link(config.link_signing_secret(), f"{client_id}:{package_id}", t):
        raise HTTPException(403, "Ссылка устарела — откройте пакет заново")
    try:
        detail = queue_service.get_package_detail(client_id, package_id)
    except KeyError:
        raise HTTPException(404, "Пакет не найден")
    from . import storage
    if not detail.get("report_path"):
        raise HTTPException(404, "Отчёта пока нет")
    path = str(storage.files_dir(client_id, package_id).parent / detail["report_path"])
    if not os.path.exists(path):
        raise HTTPException(404, "Отчёта нет")
    return FileResponse(path, filename=f"report_{package_id[:8]}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/packages/{client_id}/{package_id}/findings/{finding_id}")
def decide_finding(client_id: str, package_id: str, finding_id: str,
                    body: dict = Body(...), x_tg_init_data: Optional[str] = Header(None)):
    current_operator(x_tg_init_data)
    try:
        return review_service.decide_finding(
            client_id, package_id, finding_id,
            decision=body.get("decision", ""), edited_text=body.get("edited_text"),
        )
    except review_service.ReviewError as e:
        raise HTTPException(400, str(e))


@app.post("/api/packages/{client_id}/{package_id}/finalize")
def finalize(client_id: str, package_id: str, x_tg_init_data: Optional[str] = Header(None)):
    current_operator(x_tg_init_data)
    try:
        return review_service.finalize_review(client_id, package_id)
    except review_service.ReviewError as e:
        raise HTTPException(400, str(e))


@app.post("/api/packages/{client_id}/{package_id}/prepare_client_report")
def prepare_client_report(client_id: str, package_id: str, x_tg_init_data: Optional[str] = Header(None)):
    """Собрать клиентский Excel (Step 8) — саму отправку сообщения клиенту
    делает бот после скачивания этого файла по подписанной ссылке ниже;
    этот эндпоинт не переводит пакет в SENT (см. delivery_service.mark_sent,
    который бот вызывает после успешной отправки)."""
    current_operator(x_tg_init_data)
    try:
        pkg = delivery_service.prepare_client_report(client_id, package_id)
    except delivery_service.DeliveryError as e:
        raise HTTPException(400, str(e))
    return {
        "client_report_path": pkg.client_report_path,
        "client_report_url": (
            f"/api/packages/{client_id}/{package_id}/client_report"
            f"?t={sign_link(config.link_signing_secret(), f'{client_id}:{package_id}:client')}"
        ),
    }


@app.get("/api/packages/{client_id}/{package_id}/client_report")
def client_report_file(client_id: str, package_id: str, t: str):
    if not check_link(config.link_signing_secret(), f"{client_id}:{package_id}:client", t):
        raise HTTPException(403, "Ссылка устарела — соберите отчёт заново")
    from . import storage
    try:
        detail = queue_service.get_package_detail(client_id, package_id)
    except KeyError:
        raise HTTPException(404, "Пакет не найден")
    path_name = detail.get("client_report_path")
    if not path_name:
        raise HTTPException(404, "Клиентский отчёт ещё не собран")
    path = str(storage.files_dir(client_id, package_id).parent / path_name)
    if not os.path.exists(path):
        raise HTTPException(404, "Файл отчёта не найден")
    return FileResponse(path, filename=f"замечания_{package_id[:8]}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
