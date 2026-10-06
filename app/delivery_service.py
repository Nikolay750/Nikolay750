"""Step 8: отправка клиенту — строит клиентский Excel из разобранного (REVIEWED)
пакета, переводит его в SENT. Саму отправку сообщения в Telegram делает
bot/bot.py (не тестируется здесь — та же причина, что в Step 4: aiogram не
ставится в этой песочнице); эта функция отвечает только за то, что можно
проверить чистым Python: правильный файл собран и статус пакета корректен."""

from __future__ import annotations

from pathlib import Path

from . import storage
from .client_report import build_client_report
from .models import Package, PackageStatus, _now_iso


def list_ready_to_send() -> list[Package]:
    """Пакеты, у которых разбор закрыт, клиентский отчёт собран, но ещё не
    отправлен (sent_at is None) — это и есть очередь для фоновой рассылки
    ботом (см. bot/bot.py: send_pending_client_reports)."""
    return [
        p for p in storage.list_all_packages()
        if p.status == PackageStatus.REVIEWED.value and p.client_report_path and not p.sent_at
    ]


class DeliveryError(Exception):
    pass


def prepare_client_report(client_id: str, package_id: str) -> Package:
    """Собрать клиентский отчёт для REVIEWED-пакета. Не меняет статус сама —
    отправка подтверждается отдельным шагом (mark_sent), чтобы повторная
    генерация файла (например, после правки) не считалась отправкой."""
    pkg = storage.load_package(client_id, package_id)
    if pkg is None:
        raise DeliveryError(f"Пакет {package_id} не найден")
    if pkg.status not in (PackageStatus.REVIEWED.value, PackageStatus.SENT.value):
        raise DeliveryError(f"Пакет в статусе {pkg.status} — разбор не закрыт (finalize_review)")

    out_path = storage.files_dir(pkg.client_id, pkg.id).parent / "client_report.xlsx"
    build_client_report(pkg, out_path)
    pkg.client_report_path = out_path.name
    pkg.touch()
    storage.save_package(pkg)
    return pkg


def mark_sent(client_id: str, package_id: str) -> Package:
    """Подтвердить, что отчёт реально дошёл до клиента (бот отправил сообщение)."""
    pkg = storage.load_package(client_id, package_id)
    if pkg is None:
        raise DeliveryError(f"Пакет {package_id} не найден")
    if not pkg.client_report_path:
        raise DeliveryError("Клиентский отчёт ещё не собран — вызовите prepare_client_report")
    pkg.status = PackageStatus.SENT.value
    pkg.sent_at = _now_iso()
    pkg.touch()
    storage.save_package(pkg)
    return pkg
