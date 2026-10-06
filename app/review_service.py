"""Step 7: разбор находок оператором — confirm/reject/edit, затем отправка
решения в клиентский отчёт (сам Excel-экспорт — Step 8).

Правило: пока оператор явно не закрыл пакет (finalize_review), пакет
остаётся в READY_FOR_REVIEW — его можно открывать и доразбирать сколько
угодно раз, промежуточное состояние каждой находки сохраняется сразу
после каждого решения (не пачкой в конце), чтобы долгий разбор пакета не
терялся при случайном закрытии Mini App."""

from __future__ import annotations

from typing import Optional

from . import storage
from .models import FindingDecision, PackageStatus


class ReviewError(Exception):
    """Ошибка уровня разбора (не найден пакет/находка, или нарушена логика разбора)."""


def _load_or_raise(client_id: str, package_id: str):
    pkg = storage.load_package(client_id, package_id)
    if pkg is None:
        raise ReviewError(f"Пакет {package_id} не найден")
    return pkg


def decide_finding(client_id: str, package_id: str, finding_id: str,
                    decision: str, edited_text: Optional[str] = None) -> dict:
    """decision: confirmed | edited | rejected. edited обязательно требует edited_text."""
    if decision not in (FindingDecision.CONFIRMED.value, FindingDecision.EDITED.value,
                         FindingDecision.REJECTED.value):
        raise ReviewError(f"Неизвестное решение: {decision!r}")
    if decision == FindingDecision.EDITED.value and not (edited_text or "").strip():
        raise ReviewError("Для решения «edited» нужен текст правки")

    pkg = _load_or_raise(client_id, package_id)
    if pkg.status not in (PackageStatus.READY_FOR_REVIEW.value, PackageStatus.REVIEWED.value):
        raise ReviewError(f"Пакет в статусе {pkg.status} — разбор находок недоступен")

    target = next((f for f in pkg.findings if f.id == finding_id), None)
    if target is None:
        raise ReviewError(f"Находка {finding_id} не найдена в пакете")

    from .models import _now_iso  # локальный импорт — не тащим приватную функцию в публичный API модуля
    target.decision = decision
    target.edited_text = edited_text.strip() if decision == FindingDecision.EDITED.value else None
    target.decided_at = _now_iso()
    # если пакет уже был закрыт (REVIEWED), а оператор меняет решение — откатываем в READY_FOR_REVIEW,
    # чтобы finalize_review() заново проверил полноту разбора, а не держал статус "готов", пока
    # на самом деле в нём опять есть незакрытые изменения
    if pkg.status == PackageStatus.REVIEWED.value:
        pkg.status = PackageStatus.READY_FOR_REVIEW.value
    pkg.touch()
    storage.save_package(pkg)
    return target.to_dict()


def pending_findings(client_id: str, package_id: str) -> list[dict]:
    pkg = _load_or_raise(client_id, package_id)
    return [f.to_dict() for f in pkg.findings if f.decision == FindingDecision.PENDING.value]


def finalize_review(client_id: str, package_id: str) -> dict:
    """Закрыть разбор: требует, чтобы у КАЖДОЙ находки было решение — иначе
    забытая по рассеянности находка молча не попадёт в клиентский отчёт."""
    pkg = _load_or_raise(client_id, package_id)
    if pkg.status not in (PackageStatus.READY_FOR_REVIEW.value,):
        raise ReviewError(f"Пакет в статусе {pkg.status} — закрывать разбор рано или уже закрыт")
    pending = [f for f in pkg.findings if f.decision == FindingDecision.PENDING.value]
    if pending:
        raise ReviewError(f"Не разобрано находок: {len(pending)} — разберите все перед отправкой клиенту")
    pkg.status = PackageStatus.REVIEWED.value
    pkg.touch()
    storage.save_package(pkg)
    from .queue_service import get_package_detail
    return get_package_detail(client_id, package_id)
