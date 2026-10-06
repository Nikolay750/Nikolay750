"""Бот — приём пакета от клиента (подрядчика) и уведомление оператора.

НЕ ИСПОЛНЯЛСЯ в этой сессии: aiogram не устанавливается — pypi.org отвечает
403 даже при прямом (не через agent-прокси) обращении из этой песочницы
разработки, то есть сетевой доступ к PyPI для новых пакетов здесь закрыт на
уровне окружения, а не только политикой прокси (requests и openpyxl взяты
из уже закэшированных локально колёс — fastapi/aiogram в кэше не было).
Вся бизнес-логика поэтому вынесена в app/package_service.py и покрыта
тестами без aiogram (tests/test_package_service.py); этот файл — тонкая
обвязка Dispatcher-хендлеров поверх неё. ПЕРЕД ПРОДОМ: обязателен смоук-тест
с реальным токеном бота — здесь это не проверено запуском, только по
документированному API aiogram 3.x.

Запуск:  python -m bot.bot
Переменные окружения: BOT_TOKEN, OPERATOR_CHAT_ID (куда слать уведомления
о новом пакете в очереди), WEBAPP_URL (экран оператора — см. Step 6/7).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message

from app import delivery_service, package_service as svc, storage
from app.ingest.mailru import is_mailru_link
from app.ingest.yandex import is_yandex_disk_link
from app.models import PackageStatus

SEND_REPORTS_INTERVAL_SECONDS = 30

TOKEN = os.environ["BOT_TOKEN"]
OPERATOR_CHAT_ID = os.environ.get("OPERATOR_CHAT_ID")

dp = Dispatcher()

LINK_RE = re.compile(r"https?://\S+")

WELCOME = (
    "Пришлите документы на проверку: файлами в чат (до 20 МБ каждый) или "
    "ссылкой на Яндекс Диск/Облако Mail.ru для крупных пакетов.\n\n"
    "Когда всё прислали — напишите /готово. Если прислали ссылку на облако — "
    "пакет уйдёт в проверку сразу, этот шаг не нужен."
)


def _client_name(m: Message) -> str | None:
    u = m.from_user
    if not u:
        return None
    parts = [p for p in (u.first_name, u.last_name) if p]
    return " ".join(parts) or (f"@{u.username}" if u.username else None)


async def _notify_operator(bot: Bot, text: str) -> None:
    if not OPERATOR_CHAT_ID:
        return
    try:
        await bot.send_message(OPERATOR_CHAT_ID, text)
    except Exception:
        pass  # уведомление — не критичный путь; пакет уже сохранён на диске


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(WELCOME)


@dp.message(Command("готово"))
async def finish(m: Message):
    client_id = str(m.chat.id)
    try:
        pkg = svc.finalize_draft(client_id)
    except svc.PackageServiceError as e:
        await m.answer(str(e))
        return
    await m.answer(f"Пакет принят в очередь, файлов: {len(pkg.files)}. Сообщим, когда проверим.")
    await _notify_operator(
        m.bot, f"Новый пакет в очереди: {pkg.client_name or pkg.client_id}, файлов: {len(pkg.files)} (id {pkg.id})"
    )


@dp.message(F.document)
async def on_document(m: Message):
    client_id = str(m.chat.id)
    doc = m.document
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        await m.bot.download(doc.file_id, destination=tmp.name)
        tmp_path = tmp.name
    try:
        pkg = svc.add_direct_file(client_id, _client_name(m), doc.file_name or "file", tmp_path, doc.file_size or 0)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    await m.answer(f"Файл «{doc.file_name}» принят ({len(pkg.files)} в пакете). "
                    "Присылайте остальные или напишите /готово.")


@dp.message(F.text.regexp(LINK_RE.pattern))
async def on_link(m: Message):
    links = LINK_RE.findall(m.text or "")
    link = next((u for u in links if is_yandex_disk_link(u) or is_mailru_link(u)), None)
    if not link:
        await m.answer("Эта ссылка не похожа на Яндекс Диск или Облако Mail.ru — "
                        "пришлите, пожалуйста, файлы прямо в чат или проверьте ссылку.")
        return
    client_id = str(m.chat.id)
    await m.answer("Принял ссылку, скачиваю — это может занять время для крупных пакетов…")
    pkg = svc.add_cloud_link(client_id, _client_name(m), link)
    if pkg.status == PackageStatus.ERROR.value:
        await m.answer(
            "Не удалось скачать пакет по ссылке: "
            f"{pkg.error}\n\nПришлите, пожалуйста, файлы прямо в чат (до 20 МБ каждый)."
        )
        return
    await m.answer(f"Пакет скачан и поставлен в очередь, файлов: {len(pkg.files)}. Сообщим, когда проверим.")
    await _notify_operator(
        m.bot, f"Новый пакет в очереди (по ссылке): {pkg.client_name or pkg.client_id}, "
               f"файлов: {len(pkg.files)} (id {pkg.id})"
    )


@dp.message()
async def fallback(m: Message):
    await m.answer(WELCOME)


async def send_pending_client_reports(bot: Bot) -> None:
    """Фоновая рассылка: пакеты, разбор которых закрыт и для которых собран
    клиентский Excel (status=REVIEWED, client_report_path задан, sent_at
    пуст — см. delivery_service.list_ready_to_send), отправляются клиенту
    и переводятся в SENT. Отдельная функция на процесс бота, а не вызов из
    веб-backend'а: избегает межпроцессного RPC между FastAPI и aiogram —
    они разные процессы (см. README/Caddyfile)."""
    for pkg in delivery_service.list_ready_to_send():
        path = storage.files_dir(pkg.client_id, pkg.id).parent / pkg.client_report_path
        try:
            await bot.send_document(
                pkg.client_id, FSInputFile(str(path), filename=f"замечания_{pkg.id[:8]}.xlsx"),
                caption="Проверка пакета завершена — список замечаний во вложении.",
            )
        except Exception:
            logging.getLogger(__name__).exception("Не удалось отправить отчёт клиенту %s (пакет %s)",
                                                    pkg.client_id, pkg.id)
            continue
        delivery_service.mark_sent(pkg.client_id, pkg.id)


async def _delivery_loop(bot: Bot) -> None:
    while True:
        try:
            await send_pending_client_reports(bot)
        except Exception:
            logging.getLogger(__name__).exception("Сбой цикла рассылки клиентских отчётов")
        await asyncio.sleep(SEND_REPORTS_INTERVAL_SECONDS)


async def main():
    bot = Bot(TOKEN)
    asyncio.create_task(_delivery_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
