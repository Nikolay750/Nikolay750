"""Step 6: очередь оператора — чистая логика без FastAPI, чтобы быть
тестируемой в этой песочнице (fastapi не ставится, см. app/main.py).

Оператор видит пакеты во всех "живых" статусах — не только готовые к
разбору (READY_FOR_REVIEW), но и те, что застряли в QUEUED/PROCESSING
дольше ожидаемого, и упавшие в ERROR: иначе зависший или сломанный пакет
был бы не виден никому и клиент просто не получил бы ответа."""

from __future__ import annotations

from typing import Optional

from . import storage
from .models import Package, PackageStatus

QUEUE_STATUSES = (
    PackageStatus.QUEUED.value,
    PackageStatus.PROCESSING.value,
    PackageStatus.READY_FOR_REVIEW.value,
    PackageStatus.ERROR.value,
)
HISTORY_STATUSES = (PackageStatus.REVIEWED.value, PackageStatus.SENT.value)


def _finding_counts(pkg: Package) -> dict:
    counts = {}
    for f in pkg.findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    return counts


def _pending_decisions(pkg: Package) -> int:
    from .models import FindingDecision
    return sum(1 for f in pkg.findings if f.decision == FindingDecision.PENDING.value)


def _summary(pkg: Package) -> dict:
    return {
        "id": pkg.id,
        "client_id": pkg.client_id,
        "client_name": pkg.client_name,
        "object_title": pkg.object_title,
        "status": pkg.status,
        "files": len(pkg.files),
        "findings_total": len(pkg.findings),
        "findings_by_status": _finding_counts(pkg),
        "pending_decisions": _pending_decisions(pkg),
        "error": pkg.error,
        "created_at": pkg.created_at,
        "updated_at": pkg.updated_at,
    }


def list_queue(include_history: bool = False) -> list[dict]:
    """Пакеты для экрана очереди, старые сверху (FIFO — кто раньше прислал,
    тот раньше проверяется)."""
    statuses = set(QUEUE_STATUSES) | (set(HISTORY_STATUSES) if include_history else set())
    pkgs = [p for p in storage.list_all_packages() if p.status in statuses]
    pkgs.sort(key=lambda p: p.created_at)
    return [_summary(p) for p in pkgs]


def get_package_detail(client_id: str, package_id: str) -> dict:
    pkg = storage.load_package(client_id, package_id)
    if pkg is None:
        raise KeyError(f"Пакет {package_id} не найден для клиента {client_id}")
    d = _summary(pkg)
    d["files"] = [f.to_dict() for f in pkg.files]
    d["findings"] = [f.to_dict() for f in pkg.findings]
    d["report_path"] = pkg.report_path
    return d


def find_package_detail(package_id: str) -> Optional[dict]:
    """Для ссылок вида /package/<id> без известного client_id (напр. из уведомления)."""
    pkg = storage.find_package(package_id)
    if pkg is None:
        return None
    return get_package_detail(pkg.client_id, package_id)
