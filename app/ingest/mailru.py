"""Приём пакета по публичной ссылке на Облако Mail.ru.

ВАЖНАЯ ОГОВОРКА: у Облака Mail.ru нет официального публичного API — в отличие
от Яндекс Диска. Схема ниже (dispatcher → shard-сервер → прямая ссылка на
основе weblink-токена из URL) взята из распространённого в сообществе
reverse-engineering (похожие реализации: dinlo/Cloud-Mail.Ru-Downloader,
pozitronik/CloudMailRu, habr-статьи о разборе запросов через DevTools) и
НЕ подтверждена живым вызовом — сеть к cloud.mail.ru из этой песочницы
разработки закрыта политикой egress-прокси, а исходники конкретных скриптов
на момент разработки были недоступны для чтения (404 / запрет robots.txt /
платный доступ у части источников).

Поэтому ключевое архитектурное решение здесь — не точность форматов запроса
(она может разойтись с реальностью и будет меняться без предупреждения
стороной Mail.ru), а ИЗОЛЯЦИЯ: любое отклонение формата ответа от ожидаемого
перехватывается и превращается в RemoteUnavailableError, то есть вызывающий
код (bot) откатывается на "пришлите файлы напрямую" вместо падения. Это
соответствует утверждённой архитектуре (цикл A+B+E): Mail.ru — лучше-чем-
ничего канал, не критичный путь.

ПЕРЕД РЕАЛЬНЫМ ИСПОЛЬЗОВАНИЕМ В ПРОДЕ: нужно взять настоящую публичную
ссылку на cloud.mail.ru и прогнать через этот модуль с включённым логом
сырых ответов, чтобы свести реальный формат с тем, что здесь захардкожено,
и поправить при расхождении.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Protocol

from ..models import SourceFile
from .errors import IngestError, RemoteNotFoundError, RemoteUnavailableError, UnsupportedLinkError

PUBLIC_PAGE_RE = re.compile(r"cloud\.mail\.ru/public/([^/]+)/([^/?#]+)")
FOLDER_API = "https://cloud.mail.ru/api/v2/folder"
DISPATCHER_API = "https://cloud.mail.ru/api/v2/dispatcher"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_FILE_BYTES = 300 * 1024 * 1024


class HttpSession(Protocol):
    def get(self, url: str, params: dict, timeout: int): ...


def is_mailru_link(url: str) -> bool:
    return bool(PUBLIC_PAGE_RE.search((url or "").strip()))


def _weblink_token(public_url: str) -> str:
    m = PUBLIC_PAGE_RE.search(public_url.strip())
    if not m:
        raise UnsupportedLinkError("Ссылка не похожа на публичную ссылку Облака Mail.ru")
    return f"{m.group(1)}/{m.group(2)}"


def _safe_get_json(session, url: str, params: dict, timeout: int, what: str) -> dict:
    try:
        resp = session.get(url, params=params, timeout=timeout)
    except Exception as e:
        raise RemoteUnavailableError(f"Облако Mail.ru недоступно ({what}): {e}") from e
    status = getattr(resp, "status_code", None)
    if status == 404:
        raise RemoteNotFoundError("Ссылка не найдена или доступ закрыт")
    if status is not None and status >= 400:
        raise RemoteUnavailableError(f"Облако Mail.ru вернуло ошибку {status} ({what})")
    try:
        data = resp.json()
    except Exception as e:
        raise RemoteUnavailableError(f"Не удалось разобрать ответ Облака Mail.ru ({what}): {e}") from e
    if not isinstance(data, dict):
        raise RemoteUnavailableError(f"Неожиданный формат ответа Облака Mail.ru ({what})")
    return data


def _dispatcher_download_base(session, timeout: int) -> str:
    data = _safe_get_json(session, DISPATCHER_API, {}, timeout, "dispatcher")
    try:
        servers = data["body"]["get"]
        url = servers[0]["url"]
    except (KeyError, IndexError, TypeError) as e:
        raise RemoteUnavailableError(f"Не удалось получить download-сервер Mail.ru: {e}") from e
    if not isinstance(url, str) or not url:
        raise RemoteUnavailableError("Mail.ru не вернул download-сервер")
    return url


def _folder_meta(session, token: str, timeout: int) -> dict:
    return _safe_get_json(session, FOLDER_API, {"weblink": token}, timeout, "folder")


def _download(session, download_base: str, token: str, dest: Path, max_bytes: int, timeout: int) -> int:
    url = download_base.rstrip("/") + "/" + token.lstrip("/")
    try:
        with session.get(url, params={}, timeout=timeout, stream=True) as r:  # type: ignore[call-arg]
            status = getattr(r, "status_code", 200)
            if status >= 400:
                raise RemoteUnavailableError(f"Mail.ru вернул {status} при скачивании {dest.name}")
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
        raise RemoteUnavailableError(f"Сбой скачивания {dest.name} с Mail.ru: {e}") from e


def fetch_public_resource(public_url: str, dest_dir: Path, session: HttpSession,
                           max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                           timeout: int = DEFAULT_TIMEOUT) -> list[SourceFile]:
    """Скачать публичный файл или папку Облака Mail.ru в dest_dir.

    Поведение при РАСХОЖДЕНИИ со схемой ниже (неожиданная форма JSON и т.п.) —
    RemoteUnavailableError, а не необработанное падение: так вызывающий код
    (bot) надёжно откатывается на "пришлите файлы напрямую", даже если схема
    этого модуля устарела из-за изменений на стороне Mail.ru."""
    if not is_mailru_link(public_url):
        raise UnsupportedLinkError("Ссылка не похожа на публичную ссылку Облака Mail.ru")

    token = _weblink_token(public_url)
    meta = _folder_meta(session, token, timeout)
    body = meta.get("body")
    if not isinstance(body, dict):
        raise RemoteUnavailableError("Mail.ru не вернул метаданные ресурса")

    dest_dir.mkdir(parents=True, exist_ok=True)
    download_base = _dispatcher_download_base(session, timeout)

    kind = body.get("type")
    out: list[SourceFile] = []

    if kind == "file":
        name = body.get("name") or token.split("/")[-1]
        dest = dest_dir / name
        size = _download(session, download_base, token, dest, max_file_bytes, timeout)
        out.append(SourceFile(name=dest.name, path=dest.name, size=size, source="mailru", origin_url=public_url))
        return out

    if kind == "folder":
        items = body.get("list")
        if not isinstance(items, list) or not items:
            raise IngestError("Публичная папка Mail.ru пуста или не распознана")
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            name = item.get("name") or "file"
            item_rel = item.get("weblink") or item.get("name")
            if not item_rel:
                continue
            dest = dest_dir / name
            n = 1
            while dest.exists():
                import os as _os
                stem, ext = _os.path.splitext(name)
                dest = dest_dir / f"{stem} ({n}){ext}"
                n += 1
            size = _download(session, download_base, item_rel, dest, max_file_bytes, timeout)
            out.append(SourceFile(name=dest.name, path=dest.name, size=size, source="mailru", origin_url=public_url))
        if not out:
            raise IngestError("В публичной папке Mail.ru не нашлось файлов")
        return out

    raise RemoteUnavailableError(f"Неожиданный тип ресурса Mail.ru: {kind!r}")
