"""Модель данных модуля сверки смета ↔ КС-2 ↔ КС-3."""
from dataclasses import dataclass, field
from typing import Optional

STATUSES = ("FAIL", "WARNING", "NEEDS_CONTEXT", "INFO", "OK")


@dataclass
class Resource:
    """Строка ресурса внутри позиции (труд, машина, материал, неучтённый материал)."""
    row: int
    code: str
    name: str
    unit: str
    qty: Optional[float]      # всего с учётом коэффициентов
    total: Optional[float]    # всего в текущем уровне цен
    marker: str = ""          # Н / П,Н / Уд — пометки ГРАНД-Сметы
    qty_per: Optional[float] = None   # норма расхода на единицу позиции
    coef: Optional[float] = None


@dataclass
class Overhead:
    """НР или СП позиции."""
    row: int
    kind: str                 # "НР" | "СП"
    code: str
    pct: Optional[float]      # итоговый % с учётом коэффициента
    pct_base: Optional[float]
    coef: Optional[float]
    value: Optional[float]


@dataclass
class Position:
    doc_id: str
    key: str                  # номер позиции по смете: "28", "28.1", "5.1"
    row: int
    code: str
    name: str
    unit: str
    qty_base: Optional[float]
    coef: Optional[float]
    qty: Optional[float]      # всего с учётом коэффициентов — это и есть объём
    unit_price: Optional[float] = None   # на ед. в текущем уровне цен
    total: Optional[float] = None        # всего в текущем уровне цен
    section: str = ""
    act_num: Optional[str] = None        # № по порядку в КС-2
    parent: Optional[str] = None         # для 28.1 внутри блока 28
    direct: Optional[float] = None       # «Итого прямые затраты»
    fot: Optional[float] = None
    overheads: list = field(default_factory=list)
    resources: list = field(default_factory=list)
    children: list = field(default_factory=list)
    total_row: Optional[int] = None

    @property
    def is_block(self) -> bool:
        return self.parent is None


@dataclass
class Document:
    doc_id: str
    kind: str                 # "ЛСР" | "КС-2" | "КС-3"
    path: str
    sheet: str
    meta: dict = field(default_factory=dict)
    positions: dict = field(default_factory=dict)   # key -> Position (в порядке файла)
    sections: list = field(default_factory=list)    # [{name, total, row, keys}]
    totals: dict = field(default_factory=dict)
    header_summary: dict = field(default_factory=dict)
    parse_warnings: list = field(default_factory=list)

    def ref(self, row: Optional[int] = None) -> str:
        return f"{self.kind} «{self.doc_id}»" + (f", стр. {row}" if row else "")


@dataclass
class Finding:
    rule_id: str
    status: str
    title: str
    docs: str
    location: str
    explanation: str
    delta_rub: Optional[float] = None

    def __post_init__(self):
        assert self.status in STATUSES, self.status
