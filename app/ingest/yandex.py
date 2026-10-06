"""Приём пакета по публичной ссылке на Яндекс Диск.

Официальный, не требующий авторизации REST API:
    GET https://cloud-api.yandex.net/v1/disk/public/resources
    GET https://cloud-api.yandex.net/v1/disk/public/resources/download

Поддерживает и один публичный файл, и целую публичную папку (рекурсивный
обход через параметр path, а не выгрузка всей папки одним zip — так видно
прогресс и размер каждого файла до скачивания, и можно отбросить слишком
большой файл без скачивания всей папки).

Сетевой доступ к cloud-api.yandex.net из песочницы разработки закрыт
политикой прокси, поэтому здесь это проверено модульными тестами с
подменой HTTP-сессии (см. tests/test_ingest_yandex.py), а не живым вызовом.
Перед первым реальным использованием в проде — ручная проверка на
реальной публичной ссылке.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Protocol

from ..models import SourceFile
from .errors import IngestError, RemoteNotFoundError, RemoteUnavailableError, UnsupportedLinkError

API_BASE = "https://cloud-api.yandex.net/v1/disk/public/resources"
DOWNLOAD_API = API_BASE + "/download"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_FILE_BYTES = 300 * 1024 * 1024   # 300 МБ на файл — реальные сканы ИД доходят до ~100-110 МБ
LIST_PAGE_LIMIT = 200


class HttpSession(Protocol):
    """Минимальный протокол HTTP-сессии — для тестов подменяется фейком."""

    def get(self, url: str, params: dict, timeout: int): ...


def is_yandex_disk_link(url: str) -> bool:
    url = (url or "").strip().lower()
    return "yadi.sk" in url or "disk.yandex" in url


def _request_json(session, url: str, params: dict, timeout: int) -> dict:
    try:
        resp = session.get(url, params=params, timeout=timeout)
    except Exception as e:  # requests.RequestException и сетевые сбои
        raise RemoteUnavailableError(f"Яндекс Диск недоступен: {e}") from e
    if resp.status_code == 404:
        raise RemoteNotFoundError("Ссылка не найдена или больше не публична")
    if resp.status_code >= 400:
        raise RemoteUnavailableError(f"Яндекс Диск вернул ошибку {resp.status_code}")
    try:
        return resp.json()
    except Exception as e:
        raise RemoteUnavailableError(f"Не удалось разобрать ответ Яндекс Диска: {e}") from e


def _download_file(session, public_key: str, path: Optional[str], dest: Path,
                    max_bytes: int, timeout: int) -> int:
    params = {"public_key": public_key}
    if path:
        params["path"] = path
    data = _request_json(session, DOWNLOAD_API, params, timeout)
    href = data.get("href")
    if not href:
        raise RemoteUnavailableError("Яндекс Диск не вернул ссылку для скачивания")
    try:
        with session.get(href, params={}, timeout=timeout, stream=True) as r:  # type: ignore[call-arg]
            r.raise_for_status()
            written = 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise IngestError(
                            f"Файл {dest.name} превышает лимит {max_bytes // (1024*1024)} МБ — остановлено"
                        )
                    f.write(chunk)
            return written
    except IngestError:
        if dest.exists():
            dest.unlink(missing_ok=True)
        raise
    except Exception as e:
        if dest.exists():
            dest.unlink(missing_ok=True)
        raise RemoteUnavailableError(f"Сбой скачивания {dest.name} с Яндекс Диска: {e}") from e


def _list_dir(session, public_key: str, path: str, timeout: int) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        data = _request_json(
            session,
            API_BASE,
            {"public_key": public_key, "path": path, "limit": LIST_PAGE_LIMIT, "offset": offset},
            timeout,
        )
        embedded = data.get("_embedded", {})
        page = embedded.get("items", [])
        items.extend(page)
        total = embedded.get("total", len(items))
        offset += len(page)
        if not page or offset >= total:
            break
    return items


def fetch_public_resource(public_url: str, dest_dir: Path, session: HttpSession,
                           max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                           timeout: int = DEFAULT_TIMEOUT) -> list[SourceFile]:
    """Скачать публичный файл или рекурсивно всю публичную папку Яндекс Диска в dest_dir.

    Возвращает список SourceFile с path относительно dest_dir. Бросает IngestError
    (и подклассы) при любом сбое — вызывающий код решает, как деградировать
    (попросить прислать файлы напрямую)."""
    if not is_yandex_disk_link(public_url):
        raise UnsupportedLinkError("Ссылка не похожа на ссылку Яндекс Диска")

    root_meta = _request_json(session, API_BASE, {"public_key": public_url, "limit": 0}, timeout)
    kind = root_meta.get("type")
    dest_dir.mkdir(parents=True, exist_ok=True)

    if kind == "file":
        name = root_meta.get("name") or "file"
        dest = dest_dir / name
        size = _download_file(session, public_url, None, dest, max_file_bytes, timeout)
        return [SourceFile(name=name, path=dest.name, size=size, source="yandex", origin_url=public_url)]

    if kind == "dir":
        out: list[SourceFile] = []
        _walk_dir(session, public_url, "/", dest_dir, out, max_file_bytes, timeout)
        if not out:
            raise IngestError("Публичная папка пуста")
        return out

    raise RemoteUnavailableError(f"Неожиданный тип ресурса от Яндекс Диска: {kind!r}")


def _walk_dir(session, public_key: str, path: str, dest_dir: Path, out: list[SourceFile],
              max_file_bytes: int, timeout: int) -> None:
    for item in _list_dir(session, public_key, path, timeout):
        item_path = item.get("path", "")
        name = item.get("name", os.path.basename(item_path) or "file")
        if item.get("type") == "dir":
            _walk_dir(session, public_key, item_path, dest_dir, out, max_file_bytes, timeout)
            continue
        # сохраняем плоско по имени, с дедупликацией — операторy важно видеть
        # исходные имена файлов, а не воссозданную вложенность каталогов
        dest = dest_dir / name
        n = 1
        while dest.exists():
            stem, ext = os.path.splitext(name)
            dest = dest_dir / f"{stem} ({n}){ext}"
            n += 1
        size = _download_file(session, public_key, item_path, dest, max_file_bytes, timeout)
        out.append(SourceFile(name=dest.name, path=dest.name, size=size, source="yandex", origin_url=public_key))
