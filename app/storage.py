"""
JSON-файловое хранилище пакетов (Step 1 плана).

Структура на диске:
    data/packages/<client_id>/<package_id>/meta.json
    data/packages/<client_id>/<package_id>/files/<...>   (сами файлы пакета)
    data/packages/<client_id>/<package_id>/report.xlsx   (готовый отчёт, если есть)

Изоляция клиентов обеспечена физически: функции, читающие список пакетов
для оператора, обходят подкаталоги client_id и никогда не читают файлы одного
клиента как файлы другого. Запись meta.json атомарна (через временный файл +
os.replace), чтобы параллельная обработка нескольких пакетов не могла оставить
битый JSON при одновременной записи.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .models import Package
from .security import safe_client_id, safe_filename

# Можно переопределить через переменную окружения (удобно для тестов).
DATA_DIR = Path(os.environ.get("MINIAPP_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
PACKAGES_DIR = DATA_DIR / "packages"

# Лок на процесс: отдельные пакеты независимы, но запись meta.json одного и
# того же пакета из двух потоков (напр. обновление статуса + дозапись находки)
# должна быть последовательной.
_locks_guard = threading.Lock()
_package_locks: dict[str, threading.Lock] = {}


def _lock_for(package_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _package_locks.setdefault(package_id, threading.Lock())
    return lock


def _package_dir(client_id: str, package_id: str) -> Path:
    # defense in depth: client_id и package_id становятся компонентами пути
    # на диске — санитизируем даже если вызывающий код (bot-хендлер,
    # напрямую переданный chat.id) этого не сделал. package_id — свой же
    # uuid4().hex (models.new_id()), но тоже не доверяем слепо чужому вводу,
    # если он когда-нибудь попадёт сюда из URL (см. app/main.py).
    safe_pkg_id = safe_filename(str(package_id), default="unknown")
    return PACKAGES_DIR / safe_client_id(client_id) / safe_pkg_id


def _meta_path(client_id: str, package_id: str) -> Path:
    return _package_dir(client_id, package_id) / "meta.json"


def files_dir(client_id: str, package_id: str) -> Path:
    d = _package_dir(client_id, package_id) / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_package(pkg: Package) -> None:
    """Атомарно сохранить/обновить пакет на диске."""
    lock = _lock_for(pkg.id)
    with lock:
        pkg_dir = _package_dir(pkg.client_id, pkg.id)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        meta_path = _meta_path(pkg.client_id, pkg.id)
        fd, tmp_path = tempfile.mkstemp(dir=str(pkg_dir), prefix=".meta.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pkg.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, meta_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


def load_package(client_id: str, package_id: str) -> Optional[Package]:
    meta_path = _meta_path(client_id, package_id)
    if not meta_path.exists():
        return None
    with open(meta_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Package.from_dict(data)


def find_package(package_id: str) -> Optional[Package]:
    """Найти пакет по id, не зная client_id (нужно для коротких ссылок в Mini App)."""
    if not PACKAGES_DIR.exists():
        return None
    safe_pkg_id = safe_filename(str(package_id), default="unknown")
    for client_dir in PACKAGES_DIR.iterdir():
        if not client_dir.is_dir():
            continue
        candidate = client_dir / safe_pkg_id / "meta.json"
        if candidate.exists():
            return load_package(client_dir.name, safe_pkg_id)
    return None


def list_packages_for_client(client_id: str) -> list[Package]:
    client_dir = PACKAGES_DIR / str(client_id)
    if not client_dir.exists():
        return []
    out = []
    for pkg_dir in sorted(client_dir.iterdir()):
        if (pkg_dir / "meta.json").exists():
            pkg = load_package(client_id, pkg_dir.name)
            if pkg:
                out.append(pkg)
    return out


def list_all_packages() -> list[Package]:
    """Для очереди оператора в Mini App — пакеты всех клиентов, явно помеченные client_id."""
    if not PACKAGES_DIR.exists():
        return []
    out = []
    for client_dir in sorted(PACKAGES_DIR.iterdir()):
        if not client_dir.is_dir():
            continue
        out.extend(list_packages_for_client(client_dir.name))
    out.sort(key=lambda p: p.created_at)
    return out
