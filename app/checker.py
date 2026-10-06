"""Step 5: автопроверка пакета — связывает очередь (Package, статус QUEUED)
с движком smeta_check.pipeline и переводит пакет в READY_FOR_REVIEW с
находками в формате Finding из Step 1.

process_package() — синхронная, детерминированная, без побочных эффектов
кроме записи самого пакета и report.xlsx на диск: так её можно гонять в
тестах без фоновых потоков. submit_for_processing() — тонкая обёртка поверх
общего ThreadPoolExecutor для вызова из бота/веб-сервера, чтобы не блокировать
event loop на время работы pipeline (парсинг больших xlsx/pdf может занимать
заметное время).

Сбой автопроверки (битый файл, неожиданный формат, исключение внутри
smeta_check) НЕ должен ронять процесс и НЕ должен тихо "терять" пакет:
пакет уходит в status=ERROR с текстом причины, виден оператору в очереди,
и файлы клиента при этом никуда не пропадают — это то же правило изоляции
рисков, что и в app/ingest/*.
"""

from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from . import storage
from .models import Finding, Package, PackageStatus, new_id
from .smeta_bridge import run_pipeline_for_package

logger = logging.getLogger(__name__)

_executor: Optional[ThreadPoolExecutor] = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="checker")
    return _executor


def _to_finding(d: dict) -> Finding:
    title = (d.get("title") or "").strip()
    explanation = (d.get("text") or "").strip()
    text = f"{title}. {explanation}" if title and explanation else (title or explanation)
    docs = [x for x in (d.get("docs"), d.get("where")) if x and x != "—"]
    return Finding(
        id=new_id(),
        rule_id=d.get("rule") or "?",
        status=d.get("status") or "INFO",
        text=text or "(без описания)",
        source_docs=docs,
    )


def process_package(pkg: Package) -> Package:
    """Прогнать автопроверку для пакета в статусе QUEUED. Идемпотентна: повторный
    вызов на READY_FOR_REVIEW/REVIEWED/SENT пакете просто пересчитает заново —
    решения оператора (confirmed/edited/rejected) при этом НЕ переносятся
    автоматически на новый набор находок, поэтому вызывать повторно вручную
    не стоит после того, как оператор начал разбор (это сознательное решение
    Step 5 — перепроверка после разбора оператора не входит в объём Step 5)."""
    pkg.status = PackageStatus.PROCESSING.value
    pkg.touch()
    storage.save_package(pkg)

    files_dir = storage.files_dir(pkg.client_id, pkg.id)
    pkg_dir = files_dir.parent
    local_paths = [str(files_dir / f.name) for f in pkg.files]
    missing = [p for p in local_paths if not Path(p).exists()]
    if missing:
        pkg.status = PackageStatus.ERROR.value
        pkg.error = f"Файлы пакета не найдены на диске: {', '.join(Path(p).name for p in missing)}"
        pkg.touch()
        storage.save_package(pkg)
        return pkg

    try:
        result = run_pipeline_for_package(local_paths, str(pkg_dir))
    except Exception as e:
        logger.exception("Автопроверка пакета %s упала", pkg.id)
        pkg.status = PackageStatus.ERROR.value
        pkg.error = f"Сбой автопроверки: {e}"
        pkg.touch()
        storage.save_package(pkg)
        return pkg

    findings = [_to_finding(d) for d in result.get("findings", [])]
    pkg.set_findings(findings)
    if not pkg.object_title and result.get("title"):
        pkg.object_title = result["title"]
    pkg.report_path = result.get("report")
    pkg.error = None
    pkg.status = PackageStatus.READY_FOR_REVIEW.value
    pkg.touch()
    storage.save_package(pkg)
    return pkg


def submit_for_processing(pkg: Package) -> None:
    """Запустить process_package в фоне (не блокируя вызывающий код)."""
    _get_executor().submit(_run_safely, pkg.client_id, pkg.id)


def _run_safely(client_id: str, package_id: str) -> None:
    pkg = storage.load_package(client_id, package_id)
    if pkg is None:
        logger.error("Пакет %s/%s исчез до начала автопроверки", client_id, package_id)
        return
    try:
        process_package(pkg)
    except Exception:
        logger.error("Необработанный сбой автопроверки %s/%s:\n%s", client_id, package_id, traceback.format_exc())


def process_all_queued() -> list[Package]:
    """Синхронно обработать все пакеты в статусе QUEUED — для воркер-луп/cron
    или для ручного прогона без фонового executor'а."""
    out = []
    for pkg in storage.list_all_packages():
        if pkg.status == PackageStatus.QUEUED.value:
            out.append(process_package(pkg))
    return out
