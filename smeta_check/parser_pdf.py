"""Парсеры текстового слоя PDF договора: ВОР, ЛСР (печать ГРАНД-Сметы), перечень скрытых работ, цена/НДС.

Работает по `pdftotext -layout`. Это хрупче xlsx: колонки определяются по позициям чисел в строке,
поэтому каждый результат парсинга сверяется контрольными суммами (итоги разделов, «ВСЕГО по смете»).
"""
import re
from typing import Optional

import pdfplumber

from .models import Document, Position, Resource

NUM = r"-?\d{1,3}(?: \d{3})*(?:,\d+)?|-?\d+(?:,\d+)?"
CODE_RE = r"(?:ГЭСН\S*|ФСБЦ-\S+|ФСЭМ-\S+|ФЕР\S*|ТЕР\S*|ФССЦ\S*|ТЦ\S*|ТССЦ\S*|Прайс\S*|прайс\S*|КП\S*|Цена\S*)"
POS_RE = re.compile(rf"^\s{{0,12}}(\d+(?:\.\d+)?)\s{{2,}}({CODE_RE})\s{{2,}}(.+)$")
# общий вид строки позиции: «номер␣␣обоснование␣␣наименование…»; обоснование бывает «Текущая цена», «47-1»
GEN_RE = re.compile(r"^\s{0,12}(\d+(?:\.\d+)?)\s{2,}(\S+(?: \S+)?)\s{2,}(\S.+)$")


RES_RE = re.compile(r"^\s*(?:(Н|П,Н|П|Уд|О)\s{2,})?(\d[\d.\-]*\d)\s+(\S.*)$")
STOP_RE = re.compile(r"^\s*(Итого прямые затраты|ФОТ|Всего по позиции|Пр/\d|\d+ (ОТ|ЭМ|М)\b|ОТм)")


def _parse_resource(ln: str, pos: Position, row: int):
    m = RES_RE.match(ln)
    if not m or STOP_RE.match(ln):
        return None
    marker, code, rest = m.groups()
    cells = _cells(rest)
    if len(cells) < 3:
        return None
    name, unit = cells[0], cells[1]
    if re.fullmatch(r"\d+\.\d+", code) and " " in name:     # «262.1  421/пр… Вспомогательные ресурсы  %  2»
        code, name = name.split(" ", 1)
    nums = [n(x) for x in cells[2:]]
    if any(v is None for v in nums[:2]):
        return None
    q = pos.qty or 0
    per, coef, total_q = nums[0], None, None
    # самопроверка раскладки колонок: qty_total = норма × (коэф.) × объём позиции
    if len(nums) >= 2 and q and abs(per * q - nums[1]) <= max(abs(nums[1]) * 2e-3, 1e-6):
        total_q = nums[1]
    elif len(nums) >= 3 and q and abs(per * nums[1] * q - nums[2]) <= max(abs(nums[2]) * 2e-3, 1e-6):
        coef, total_q = nums[1], nums[2]
    elif len(nums) >= 2:
        total_q = nums[1]          # раскладка не подтвердилась — берём как есть
    money = nums[-1] if len(nums) >= 4 else None
    return Resource(row, code, name, unit, total_q, money, marker or "", per, coef)


def group_materials(doc: Document) -> None:
    """Материалы ФСБЦ/«Текущая цена», идущие отдельными позициями сразу за работой, — её состав."""
    work = None
    for p in doc.positions.values():
        is_mat = p.code.upper().startswith("ФСБЦ") or p.code.lower().startswith("текущая")
        if is_mat and work is not None and p.section == work.section:
            p.parent = work.key
            work.children.append(p.key)
        elif not is_mat:
            work = p


def _next_ok(prev: str, key: str) -> bool:
    if prev is None:
        return key == "1"
    pi = int(prev.split(".")[0])
    if "." in key:
        return int(key.split(".")[0]) == pi
    return int(key) == pi + 1


def n(s: str) -> Optional[float]:
    if s is None:
        return None
    s = s.replace("\xa0", " ").strip()
    if not s:
        return None
    try:
        return float(s.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def pdf_pages(path: str) -> list:
    """Step 9.1: заменено с subprocess-вызова внешнего `pdftotext` (poppler,
    которого нет на Windows) на pdfplumber — см. pipeline.py:_pdf_text для
    той же замены и объяснения. "\f" между страницами — чтобы сохранить
    прежний формат разбиения, на который рассчитан код ниже по файлу."""
    try:
        with pdfplumber.open(path) as pdf:
            return [p.extract_text(layout=True) or "" for p in pdf.pages]
    except Exception:
        return [""]


def _cells(s: str) -> list:
    return [c for c in re.split(r"\s{2,}", s.strip()) if c]


# ------------------------------------------------------------------ ЛСР
def parse_lsr_pdf(pages: list, doc_id: str = "ЛСР (PDF)") -> Document:
    start = next(i for i, t in enumerate(pages) if re.search(r"Обоснование", t) and re.search(r"Раздел 1\.", t))
    end = next(i for i in range(start, len(pages)) if "ВСЕГО по смете" in pages[i])
    lines = "\n".join(pages[start:end + 1]).splitlines()
    doc = Document(doc_id=doc_id, kind="ЛСР", path="PDF", sheet=f"стр. PDF {start + 1}–{end + 1}")
    section, cur, last = None, None, None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("Раздел "):
            section = {"name": s, "total": None, "row": i, "keys": []}
            doc.sections.append(section)
            cur = None
            continue
        m = GEN_RE.match(ln)
        if m and (not _next_ok(last, m.group(1)) or not re.search(r"[А-Яа-я]", m.group(3))):
            m = None
        if m:
            last = m.group(1)
            key, code, rest = m.groups()
            cells = _cells(rest)
            # rest: name, unit, qty_unit, coef, qty_total, [base price, index, cur price, coef, total]
            name = cells[0]
            nums = cells[1:]
            unit = nums[0] if nums else ""
            vals = [n(x) for x in nums[1:]]
            qty_base = vals[0] if len(vals) > 0 else None
            coef = vals[1] if len(vals) > 1 else None
            qty = vals[2] if len(vals) > 2 else None
            p = Position(doc_id=doc_id, key=key, row=i, code=code, name=name, unit=unit,
                         qty_base=qty_base, coef=coef, qty=qty, section=section["name"] if section else "")
            # продолжение наименования — следующие строки с текстом в колонке наименования
            j = i + 1
            col = ln.find(name)
            while j < len(lines) and lines[j].strip() and not GEN_RE.match(lines[j]):
                nxt = lines[j]
                lead = len(nxt) - len(nxt.lstrip())
                if abs(lead - col) <= 3 and len(_cells(nxt)) == 1 and not re.match(r"\s*\d", nxt[col:col + 3] or "x"):
                    p.name += " " + _cells(nxt)[0]
                    j += 1
                else:
                    break
            doc.positions[key] = p
            if section:
                section["keys"].append(key)
            cur = p
            continue
        if cur is not None and not s.startswith("Всего по позиции"):
            r = _parse_resource(ln, cur, i)
            if r is not None:
                cur.resources.append(r)
                continue
            if cur.resources and len(_cells(ln)) == 1 and s and not STOP_RE.match(ln) \
                    and lines[i - 1].strip() and RES_RE.match(lines[i - 1]):
                cur.resources[-1].name += " " + s      # продолжение наименования ресурса
                continue
        if s.startswith("Всего по позиции") and cur is not None:
            vals = [n(x) for x in _cells(s)[1:]]
            vals = [v for v in vals if v is not None]
            if vals:
                cur.total = vals[-1]
                cur.unit_price = vals[-2] if len(vals) > 1 else None
            cur = None
            continue
        if s.startswith("Всего по разделу") and section is not None:
            v = [n(x) for x in _cells(s)[1:]]
            section["total"] = v[-1] if v else None
            continue
        if re.match(r"^Всего\s{2,}", s):
            doc.totals["vsego"] = (n(_cells(s)[-1]), i)
        elif "тендерного снижения" in s or "коэффициент снижения" in s.lower():
            m2 = re.search(r"(\d+,\d+)", s)
            doc.totals["reduction"] = (n(m2.group(1)) if m2 else None, n(_cells(s)[-1]), i, s.split("  ")[0])
        elif s.startswith("Всего с учетом"):
            doc.totals["after_reduction"] = (n(_cells(s)[-1]), i)
        elif s.startswith("НДС"):
            m2 = re.search(r"(\d+)\s*%", s)
            doc.totals["vat"] = (float(m2.group(1)) if m2 else None, n(_cells(s)[-1]), i)
        elif s.startswith("ВСЕГО по смете"):
            doc.totals["grand"] = (n(_cells(s)[-1]), i)
    group_materials(doc)
    return doc


# ------------------------------------------------------------------ ВОР
VOR_RE = re.compile(r"^\s{0,10}(\d+)\s{2,}(\d+(?:\.\d+)?)\s{2,}(.+)$")


def parse_vor_pdf(pages: list) -> list:
    start = next(i for i, t in enumerate(pages) if "Ведомость объёмов работ" in t[:400])
    end = next(i for i in range(start, len(pages)) if "Составил" in pages[i])
    lines = "\n".join(pages[start:end + 1]).splitlines()
    items, section, sub = [], "", ""
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("Раздел "):
            section, sub = s, ""
            continue
        m = VOR_RE.match(ln)
        if not m:
            if s and not re.search(r"\d", s) and len(s) < 40 and not s.startswith("Страница"):
                if items and lines[i - 1].strip() == "" or s[0].isupper() and len(_cells(ln)) == 1 and \
                        (len(ln) - len(ln.lstrip())) < 3:
                    sub = s
            continue
        num, lsr_no, rest = m.groups()
        cells = _cells(rest)
        if len(cells) < 3:
            continue
        name, unit, qty = cells[0], cells[1], n(cells[2])
        if not re.search(r"[А-Яа-я]", name) or qty is None:
            continue
        formula = cells[3] if len(cells) > 3 else ""
        it = {"n": int(num), "lsr": lsr_no, "name": name, "unit": unit, "qty": qty, "formula": formula,
              "section": section, "sub": sub, "line": i}
        # продолжение наименования / формулы
        j, col = i + 1, ln.find(name)
        while j < len(lines) and lines[j].strip() and not VOR_RE.match(lines[j]) \
                and not lines[j].strip().startswith("Раздел"):
            c = _cells(lines[j])
            lead = len(lines[j]) - len(lines[j].lstrip())
            if abs(lead - col) <= 3:
                it["name"] += " " + c[0]
                if len(c) > 1 and it["formula"]:
                    it["formula"] += c[-1]
            elif it["formula"] and lead > col + 40:
                it["formula"] += c[-1]
            j += 1
        items.append(it)
    return items


# ------------------------------------------------------------------ перечень скрытых работ (ТЗ)
def parse_hidden_works(pages: list) -> list:
    idx = next(i for i, t in enumerate(pages) if "Перечень скрытых работ" in t and "Наименование работ" in t)
    text = "\n".join(pages[idx:idx + 3])
    text = text.split("Ведомость объёмов работ")[0]
    body = text.split("Наименование работ", 1)[1]
    out, cur = [], None
    for ln in body.splitlines():
        m = re.match(r"^\s*(\d{1,3})\s+(\S.*)$", ln)
        if m and (cur is None or int(m.group(1)) == cur["n"] + 1):
            cur = {"n": int(m.group(1)), "name": m.group(2).strip()}
            out.append(cur)
        elif cur and ln.strip() and not re.fullmatch(r"\s*\d+\s*", ln):
            cur["name"] += " " + ln.strip()
    return out


# ------------------------------------------------------------------ цена и НДС договора
def parse_contract_price(pages: list) -> dict:
    t = "\n".join(pages[:16])
    r = {}
    m = re.search(r"Цена Договора составляет\s+([\d ]+)\s*\(.*?\)\s*рублей\s*(\d{2})\s*копеек,\s*в том числе НДС\s*-\s*"
                  r"([\d ]+)\s*\(.*?\)\s*рублей\s*(\d{2})", t, re.S)
    if m:
        r["price"] = n(m.group(1)) + int(m.group(2)) / 100
        r["vat"] = n(m.group(3)) + int(m.group(4)) / 100
    elif re.search(r"НДС не облагается", t):
        r["vat"] = 0.0
    m = re.search(r"Цена договора без НДС:\s*([\d ,]+)", t)
    if m:
        r["price_wo_vat"] = n(m.group(1))
    m = re.search(r"\b(\d+/\d+)\b\s*\n?.*?Размер НДС", t, re.S)
    rate = re.search(r"Условная\s+.*?\b(\d+/105|\d+/110|\d+/120|\d+/122|\d+/107)\b", t, re.S)
    if rate:
        r["vat_rate_frac"] = rate.group(1)
    fin = re.findall(r"856-\S+\s+([\d ]+,\d{2})", t)
    r["financing"] = [n(x) for x in fin]
    m = re.search(r"Договор №\s*(\S+)", t)
    r["number"] = m.group(1) if m else ""
    return r
