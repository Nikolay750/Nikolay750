"""Единая точка входа приёма пакета по облачной ссылке.

Выбирает адаптер по виду ссылки (Яндекс Диск — официальный API, Облако
Mail.ru — risk-изолированный best-effort) и преобразует любой сбой в один
и тот же тип ошибки (IngestError), чтобы вызывающий код (бот) мог
одинаково откатиться на просьбу прислать файлы напрямую, не завязываясь
на то, какое облако подвело."""

from __future__ import annotations

from pathlib import Path

import requests

from ..models import SourceFile
from . import mailru, yandex
from .errors import IngestError, UnsupportedLinkError


def fetch_by_link(url: str, dest_dir: Path, timeout: int = 30) -> list[SourceFile]:
    """Скачать файл(ы) по публичной облачной ссылке в dest_dir.

    Бросает IngestError (UnsupportedLinkError/RemoteNotFoundError/
    RemoteUnavailableError), если ссылка не распознана или облако не
    отдало файлы — вызывающий код сам решает, что сказать пользователю."""
    session = requests.Session()
    if yandex.is_yandex_disk_link(url):
        return yandex.fetch_public_resource(url, dest_dir, session, timeout=timeout)
    if mailru.is_mailru_link(url):
        return mailru.fetch_public_resource(url, dest_dir, session, timeout=timeout)
    raise UnsupportedLinkError(
        "Ссылка не похожа на публичную ссылку Яндекс Диска или Облака Mail.ru — "
        "пришлите, пожалуйста, файлы напрямую"
    )
