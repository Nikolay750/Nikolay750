"""
Модель данных для сервиса проверки ИД.

Package — пакет документов, присланный одним клиентом (подрядчиком).
Finding — одно замечание по пакету, прошедшее через авто-проверку и
(опционально) экспертный разбор оператора в Mini App.

Хранение: JSON-файлы на диске (см. storage.py), без БД — соответствует
Step 1 утверждённого плана. Пакеты разных клиентов физически разделены по
каталогам (data/packages/<client_id>/<package_id>/meta.json), поэтому
смешивание данных между клиентами структурно невозможно, а не только
проверяется в коде.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from enum import Enum
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class PackageStatus(str, Enum):
    NEW = "new"                      # создан, файлы ещё грузятся/не все получены
    QUEUED = "queued"                # все файлы получены, ждёт автопроверки
    PROCESSING = "processing"        # идёт автопроверка (pipeline)
    READY_FOR_REVIEW = "ready_for_review"  # автопроверка готова, ждёт оператора
    REVIEWED = "reviewed"            # оператор разобрал находки
    SENT = "sent"                    # отчёт отправлен клиенту
    ERROR = "error"                  # сбой на любом этапе


class FindingStatus(str, Enum):
    OK = "OK"
    WARNING = "WARNING"
    FAIL = "FAIL"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"
    INFO = "INFO"


class FindingDecision(str, Enum):
    PENDING = "pending"      # ещё не разобрано оператором
    CONFIRMED = "confirmed"  # оператор подтвердил как есть
    EDITED = "edited"        # оператор подтвердил с правкой текста
    REJECTED = "rejected"    # оператор отклонил (не включать в отчёт клиенту)


@dataclasses.dataclass
class SourceFile:
    """Один файл в составе пакета."""
    name: str
    path: str                       # относительный путь внутри каталога пакета
    size: int = 0
    source: str = "direct"          # direct | yandex | mailru
    origin_url: Optional[str] = None  # исходная публичная ссылка, если была

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "SourceFile":
        return SourceFile(**d)


@dataclasses.dataclass
class Finding:
    """Одно замечание по пакету (результат авто-проверки + решение оператора)."""
    id: str
    rule_id: str                    # напр. "rule-1-chain", "S-02", "H-03"
    status: str                     # FindingStatus
    text: str                       # человекочитаемое объяснение
    source_docs: list[str] = dataclasses.field(default_factory=list)
    decision: str = FindingDecision.PENDING.value
    edited_text: Optional[str] = None
    decided_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Finding":
        return Finding(**d)

    def final_text(self) -> str:
        """Текст, который пойдёт в клиентский отчёт (с учётом правки оператора)."""
        return self.edited_text if self.decision == FindingDecision.EDITED.value and self.edited_text else self.text


@dataclasses.dataclass
class Package:
    """Пакет документов одного клиента на проверку."""
    id: str
    client_id: str                  # telegram id подрядчика (строкой — для стабильности путей)
    client_name: Optional[str] = None
    object_title: Optional[str] = None   # название объекта/заказа, если известно
    status: str = PackageStatus.NEW.value
    files: list[SourceFile] = dataclasses.field(default_factory=list)
    findings: list[Finding] = dataclasses.field(default_factory=list)
    report_path: Optional[str] = None
    error: Optional[str] = None
    created_at: str = dataclasses.field(default_factory=_now_iso)
    updated_at: str = dataclasses.field(default_factory=_now_iso)

    @staticmethod
    def create(client_id: str, client_name: Optional[str] = None,
               object_title: Optional[str] = None) -> "Package":
        return Package(
            id=new_id(),
            client_id=str(client_id),
            client_name=client_name,
            object_title=object_title,
        )

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def add_file(self, f: SourceFile) -> None:
        self.files.append(f)
        self.touch()

    def set_findings(self, findings: list[Finding]) -> None:
        self.findings = findings
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["files"] = [f.to_dict() if isinstance(f, SourceFile) else f for f in self.files]
        d["findings"] = [f.to_dict() if isinstance(f, Finding) else f for f in self.findings]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Package":
        d = dict(d)
        files = [SourceFile.from_dict(f) for f in d.get("files", [])]
        findings = [Finding.from_dict(f) for f in d.get("findings", [])]
        d["files"] = files
        d["findings"] = findings
        return Package(**d)
