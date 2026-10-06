"""Step 9: защитные утилиты, общие для всех мест, где внешние данные
(имя файла от Telegram, имя файла из ответа облака, client_id из чата)
становятся частью пути на диске.

Без этого модуля:
  - подрядчик мог бы прислать документ с именем "../../../etc/cron.d/x"
    (Telegram Bot API не проверяет имя файла) — add_direct_file писал бы
    его как dest_dir / name, то есть ВНЕ каталога пакета;
  - то же для имени файла внутри публичной папки Яндекс Диска/Mail.ru —
    имя в ответе API не наша зона контроля;
  - client_id технически приходит из чата Telegram (chat.id), но раз это
    используется как компонент пути (data/packages/<client_id>/...),
    лучше не доверять ему слепо на случай будущих изменений источника.
"""

from __future__ import annotations

import re

# Не просто "без слэшей": убираем и то, что openpyxl/Excel и файловые системы
# считают спецсимволами, оставляя кириллицу/латиницу/цифры/пробел/типовую
# пунктуацию исполнительной документации (№, скобки, точка, дефис).
_UNSAFE_FILENAME = re.compile(r"[^\w.\- ()№]+", re.UNICODE)
_UNSAFE_CLIENT_ID = re.compile(r"[^\w-]+", re.UNICODE)  # без точки: "." недопустим — иначе ".." не отличить от легального значения


def safe_filename(name: str, default: str = "file") -> str:
    """Имя файла без путей (../, абсолютных путей, NUL) и спецсимволов.
    Всегда возвращает непустую строку, не длиннее 150 символов — этого же
    инварианта придерживается v1 (_reference/service_v1.py:safe_name)."""
    name = (name or "").replace("\x00", "")
    # os.path.basename режет и "/", и "\\" по факту для большинства платформ,
    # но сначала нормализуем бэкслеши сами — basename на Linux их не видит
    name = name.replace("\\", "/")
    name = name.rsplit("/", 1)[-1].strip()
    name = _UNSAFE_FILENAME.sub("_", name)
    name = name.strip(" .") or default
    return name[:150]


def safe_client_id(client_id: str) -> str:
    """client_id используется как компонент файлового пути
    (data/packages/<client_id>/...) — не должен содержать / или .."""
    value = _UNSAFE_CLIENT_ID.sub("_", str(client_id or "").strip())
    return value or "unknown"
