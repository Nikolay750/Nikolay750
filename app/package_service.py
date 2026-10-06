"""Приём пакета от клиента (подрядчика) — логика, не зависящая от aiogram.

Вынесена в отдельный модуль специально, чтобы её можно было протестировать
без установки aiogram/FastAPI (в этой песочнице нет сетевого доступа к
PyPI для новых пакетов — см. коммит Step 4). bot/bot.py остаётся тонкой
обвязкой поверх этих функций: Dispatcher-хендлеры вызывают их и больше
никакой бизнес-логики не содержат.

Модель диалога с клиентом:
  - у каждого client_id в любой момент не более одного "черновика" пакета
    (Package.status == NEW) — туда копятся файлы, присланные прямо в чат
    (до 20 МБ — лимит Bot API), и общий текст;
  - ссылка на облако (Яндекс Диск / Mail.ru) создаёт СВОЙ отдельный пакет
    и скачивается сразу — черновик из прямых файлов она не трогает;
  - команда /готово переводит текущий черновик в QUEUED и уведомляет
    оператора; если черновика нет — явная ошибка, а не тихое "ничего".
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from . import storage
from .ingest import fetch_by_link
from .ingest.errors import IngestError
from .models import Package, PackageStatus, SourceFile


class PackageServiceError(Exception):
    """Ошибка уровня диалога с клиентом (не ошибка сети/облака — та оборачивается отдельно)."""


def get_draft(client_id: str) -> Optional[Package]:
    for pkg in storage.list_packages_for_client(client_id):
        if pkg.status == PackageStatus.NEW.value:
            return pkg
    return None


def get_or_create_draft(client_id: str, client_name: Optional[str] = None) -> Package:
    draft = get_draft(client_id)
    if draft:
        return draft
    pkg = Package.create(client_id=client_id, client_name=client_name)
    storage.save_package(pkg)
    return pkg


def add_direct_file(client_id: str, client_name: Optional[str], filename: str,
                     tmp_path: str, size: int) -> Package:
    """Файл, присланный прямо в чат (бот уже скачал его сам через Bot API, ≤20 МБ)."""
    pkg = get_or_create_draft(client_id, client_name)
    dest = storage.files_dir(pkg.client_id, pkg.id) / filename
    n = 1
    while dest.exists():
        stem, suffix = dest.stem, dest.suffix
        dest = dest.parent / f"{stem} ({n}){suffix}"
        n += 1
    shutil.copy2(tmp_path, dest)
    pkg.add_file(SourceFile(name=dest.name, path=dest.name, size=size, source="direct"))
    storage.save_package(pkg)
    return pkg


def add_cloud_link(client_id: str, client_name: Optional[str], link: str) -> Package:
    """Ссылка на облако — отдельный пакет, скачивается немедленно.

    При сбое адаптера (нет официального API у Mail.ru, сеть легла, ссылка не
    публична и т.п.) пакет не падает: уходит в статус ERROR с понятным
    текстом, а вызывающий бот должен сам предложить клиенту прислать файлы
    напрямую — деградация, а не исключение наружу."""
    pkg = Package.create(client_id=client_id, client_name=client_name)
    storage.save_package(pkg)
    dest_dir = storage.files_dir(pkg.client_id, pkg.id)
    try:
        files = fetch_by_link(link, dest_dir)
    except IngestError as e:
        pkg.status = PackageStatus.ERROR.value
        pkg.error = str(e)
        pkg.touch()
        storage.save_package(pkg)
        return pkg
    for f in files:
        pkg.add_file(f)
    pkg.status = PackageStatus.QUEUED.value
    pkg.touch()
    storage.save_package(pkg)
    return pkg


def finalize_draft(client_id: str) -> Package:
    """Команда /готово — закрыть черновик (файлы, присланные прямо в чат) и поставить в очередь."""
    pkg = get_draft(client_id)
    if pkg is None:
        raise PackageServiceError("Нет открытого пакета: пришлите хотя бы один файл перед /готово")
    if not pkg.files:
        raise PackageServiceError("В пакете нет файлов — пришлите хотя бы один перед /готово")
    pkg.status = PackageStatus.QUEUED.value
    pkg.touch()
    storage.save_package(pkg)
    return pkg
