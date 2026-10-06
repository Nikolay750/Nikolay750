"""Детерминированные проверки ЛСР ↔ КС-2 ↔ КС-3 (этап 1).

Арифметику и сравнение объёмов считает только код. LLM сюда не допускается.
"""
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

from .models import Document, Finding, Position

TOL_RUB = 1.0          # допуск округления по сумме позиции, руб.
TOL_QTY = 1e-6         # относительный допуск по объёму


def _f(x: Optional[float], d=2) -> str:
    return "—" if x is None else f"{x:,.{d}f}".replace(",", " ")


def _code(c: str) -> str:
    return re.sub(r"\s+", "", c).upper()


def _sim(a: str, b: str) -> float:
    n = lambda s: re.sub(r"\s+", " ", s.lower()).strip()
    return SequenceMatcher(None, n(a), n(b)).ratio()


def unit_mult(u: str) -> tuple:
    """«100 м2» → (100, «м2»); «1000 шт» → (1000, «шт»); «1т груза» → (1, «т»)."""
    u = u.strip().lower().replace(".", "")
    m = re.match(r"^(\d+)\s*(.+)$", u)
    mult, base = (int(m.group(1)), m.group(2)) if m else (1, u)
    base = base.replace("т груза", "т").replace("тонна", "т").strip()
    if base == "кг":
        return mult / 1000, "т"
    if base == "г":
        return mult / 1e6, "т"
    return mult, base


def _date(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y")
    except Exception:
        return None


# ---------------------------------------------------------------- S-01
def s01_positions_in_estimate(lsr: Document, acts: list) -> list:
    out, missing = [], 0
    for act in acts:
        for p in act.positions.values():
            q = lsr.positions.get(p.key)
            loc = f"поз. {p.key} (стр. {p.row})"
            if q is None:
                missing += 1
                out.append(Finding("S-01", "FAIL", "Позиция КС-2 отсутствует в смете",
                                   act.ref(), loc,
                                   f"«{p.name[:80]}» ({p.code}) — в ЛСР нет позиции №{p.key}. "
                                   "Работа вне сметы: нужно допсоглашение или исправление акта. "
                                   "Если допсоглашение есть — приложите его, статус станет NEEDS_CONTEXT."))
                continue
            if _code(p.code) != _code(q.code):
                out.append(Finding("S-01", "FAIL", "Шифр расценки в КС-2 не совпадает со сметой",
                                   f"{act.ref(p.row)} ↔ {lsr.ref(q.row)}", loc,
                                   f"КС-2: {p.code}; ЛСР: {q.code}."))
            elif _sim(p.name, q.name) < 0.85:
                out.append(Finding("S-01", "WARNING", "Наименование позиции отличается от сметы",
                                   f"{act.ref(p.row)} ↔ {lsr.ref(q.row)}", loc,
                                   f"КС-2: «{p.name[:70]}»; ЛСР: «{q.name[:70]}»."))
    if not missing:
        n = sum(len(a.positions) for a in acts)
        out.append(Finding("S-01", "OK", "Все позиции КС-2 есть в смете", "КС-2 ↔ ЛСР", "—",
                           f"Проверено позиций: {n}. Сопоставление по номеру позиции + шифру расценки."))
    return out


# ---------------------------------------------------------------- S-02
def s02_cumulative_volume(lsr: Document, acts: list) -> tuple[list, list]:
    out, table = [], []
    over = 0
    for q in lsr.positions.values():
        done = [a.positions[q.key].qty or 0 for a in acts if q.key in a.positions]
        cum = sum(done) if done else 0.0
        table.append({"key": q.key, "code": q.code, "name": q.name, "unit": q.unit,
                      "lsr_qty": q.qty, "ks_qty": cum, "lsr_price": q.unit_price,
                      "ks_price": next((a.positions[q.key].unit_price for a in acts if q.key in a.positions), None),
                      "lsr_total": q.total,
                      "ks_total": sum((a.positions[q.key].total or 0) for a in acts if q.key in a.positions),
                      "is_sub": q.parent is not None})
        if q.qty is None:
            continue
        if abs(cum) > abs(q.qty) * (1 + TOL_QTY) + 1e-9:   # q.qty < 0 — исключённый ресурс
            over += 1
            excess = cum - q.qty
            rub = excess * q.unit_price if q.unit_price else None
            out.append(Finding("S-02", "FAIL", "Объём по КС-2 нарастающим итогом превышает смету",
                               f"{lsr.ref(q.row)} ↔ КС-2 ({len(done)} шт.)", f"поз. {q.key}",
                               f"«{q.name[:70]}»: смета {_f(q.qty, 4)} {q.unit}, закрыто {_f(cum, 4)} "
                               f"(+{_f(excess, 4)}). Оценка переплаты до коэффициентов — {_f(rub)} руб.",
                               rub))
    closed = [r for r in table if r["ks_qty"] and r["lsr_qty"]]
    full = [r for r in closed if abs(r["ks_qty"] - r["lsr_qty"]) <= abs(r["lsr_qty"]) * TOL_QTY]
    if not over:
        out.append(Finding("S-02", "OK", "Перерасхода объёмов нет", "ЛСР ↔ КС-2", "—",
                           f"Позиций в смете: {len(table)}, закрыто полностью: {len(full)}, "
                           f"частично: {len(closed) - len(full)}, не закрыто: {len(table) - len(closed)}."))
    if len(full) == len(table) and len(acts) == 1:
        out.append(Finding("S-02", "INFO", "Один акт закрывает 100% сметы", "ЛСР ↔ КС-2", "—",
                           "Все объёмы КС-2 равны сметным до 4-го знака. Это допустимо при закрытии "
                           "контракта одним актом, но значит, что КС-2 — копия сметы, а не замер факта. "
                           "Фактическое выполнение объёмов этой проверкой не подтверждается — для этого "
                           "нужна сверка с АОСР/исполнительными схемами (модуль ИД)."))
    return out, table


# ---------------------------------------------------------------- S-03
def s03_units_prices(lsr: Document, acts: list) -> list:
    out, bad = [], 0
    for act in acts:
        for p in act.positions.values():
            q = lsr.positions.get(p.key)
            if q is None:
                continue
            loc = f"поз. {p.key}"
            if p.unit.strip().lower() != q.unit.strip().lower():
                bad += 1
                out.append(Finding("S-03", "FAIL", "Единица измерения отличается от сметы",
                                   f"{act.ref(p.row)} ↔ {lsr.ref(q.row)}", loc,
                                   f"КС-2: «{p.unit}», ЛСР: «{q.unit}». Риск ошибки ×100 (м² ↔ 100 м²)."))
            if p.unit_price is not None and q.unit_price is not None and abs(p.unit_price - q.unit_price) > 0.01:
                bad += 1
                d = (p.unit_price - q.unit_price) * (p.qty or 0)
                out.append(Finding("S-03", "FAIL", "Цена за единицу в КС-2 отличается от сметы",
                                   f"{act.ref(p.row)} ↔ {lsr.ref(q.row)}", loc,
                                   f"КС-2: {_f(p.unit_price)}, ЛСР: {_f(q.unit_price)} руб./{q.unit}. "
                                   f"Влияние на сумму: {_f(d)} руб.", d))
    if not bad:
        out.append(Finding("S-03", "OK", "Единицы и цены за единицу совпадают со сметой", "КС-2 ↔ ЛСР", "—", ""))
    return out


# ---------------------------------------------------------------- S-04
def s04_coefficients(lsr: Document, acts: list) -> list:
    out, bad = [], 0
    for act in acts:
        for p in act.positions.values():
            q = lsr.positions.get(p.key)
            if q is None:
                continue
            if (p.coef or 1) != (q.coef or 1):
                bad += 1
                out.append(Finding("S-04", "FAIL", "Коэффициент к позиции отличается от сметы",
                                   f"{act.ref(p.row)} ↔ {lsr.ref(q.row)}", f"поз. {p.key}",
                                   f"КС-2: {p.coef}, ЛСР: {q.coef}."))
            po = [(o.kind, o.pct) for o in p.overheads]
            qo = [(o.kind, o.pct) for o in q.overheads]
            if po != qo:
                bad += 1
                out.append(Finding("S-04", "FAIL", "НР/СП в КС-2 отличаются от сметы",
                                   f"{act.ref(p.row)} ↔ {lsr.ref(q.row)}", f"поз. {p.key}",
                                   f"КС-2: {po}; ЛСР: {qo}."))
        for name, label in (("reduction", "коэффициент снижения"), ("tax_line", "строка УСН/НДС")):
            a, b = act.totals.get(name), lsr.totals.get(name)
            if (a is None) != (b is None) or (a and b and a[0] != b[0]):
                bad += 1
                out.append(Finding("S-04", "FAIL", f"Итоговый {label} в КС-2 отличается от сметы",
                                   f"{act.ref()} ↔ {lsr.ref()}", "итоги",
                                   f"КС-2: {a[3] if a else 'нет'}; ЛСР: {b[3] if b else 'нет'}."))
    if not bad:
        out.append(Finding("S-04", "OK", "Коэффициенты, НР/СП и итоговые коэффициенты совпадают", "КС-2 ↔ ЛСР", "—", ""))

    red = lsr.totals.get("reduction")
    if red and red[0] is not None:
        k = red[0]
        if round(k, 4) != k:
            out.append(Finding("S-04", "NEEDS_CONTEXT", "Коэффициент снижения — «некруглое» число",
                               lsr.ref(red[2]), "итоги",
                               f"k = {k}. Такое значение обычно получают делением цены контракта на сметную "
                               f"стоимость (подгонка под цену): {_f(lsr.totals['vsego'][0])} × k = {_f(red[1])}. "
                               "Само по себе не ошибка, но k должен следовать из контракта/протокола торгов. "
                               "Нужен контракт с ценой и порядком применения коэффициента."))
    tax = lsr.totals.get("tax_line")
    if tax:
        out.append(Finding("S-07", "NEEDS_CONTEXT", "Надбавка «пересчёт при УСН» сверх сметы",
                           lsr.ref(tax[2]), "итоги",
                           f"«{tax[3]}» = {_f(tax[1])} руб. (+{_f((tax[0] or 1) - 1, 2)} к сумме после снижения). "
                           "По размеру совпадает с НДС 20%, но подрядчик на УСН. Основание такой надбавки "
                           "должно быть в контракте (цена контракта «включая все налоги» и т.п.). "
                           "Нужен контракт; ставку и пороги НДС для УСН проверять по действующей редакции НК РФ."))
    return out


# ---------------------------------------------------------------- S-05
def s05_arithmetic(doc: Document) -> list:
    out = []
    errs = 0
    for p in doc.positions.values():
        loc = f"поз. {p.key} (стр. {p.row})"
        if p.qty_base is not None and p.coef is not None and p.qty is not None and p.is_block:
            if abs(p.qty_base * p.coef - p.qty) > max(abs(p.qty) * 1e-6, 1e-6):
                errs += 1
                out.append(Finding("S-05", "FAIL", "Объём × коэффициент ≠ итоговый объём", doc.ref(p.row), loc,
                                   f"{p.qty_base} × {p.coef} ≠ {p.qty}."))
        for o in p.overheads:
            if p.fot and o.pct is not None and o.value is not None:
                exp = p.fot * o.pct / 100
                if abs(exp - o.value) > 0.05:
                    errs += 1
                    out.append(Finding("S-05", "FAIL", f"{o.kind} не равны ФОТ × %", doc.ref(o.row), loc,
                                       f"ФОТ {_f(p.fot)} × {o.pct}% = {_f(exp)}, в документе {_f(o.value)}.",
                                       o.value - exp))
        if p.is_block and p.total:
            parts = (p.direct or 0) + sum(doc.positions[c].total or 0 for c in p.children) \
                    + sum(o.value or 0 for o in p.overheads)
            if p.direct is not None and abs(parts - p.total) > TOL_RUB:
                errs += 1
                out.append(Finding("S-05", "FAIL", "«Всего по позиции» ≠ прямые + материалы + НР + СП",
                                   doc.ref(p.total_row), loc,
                                   f"Сумма составляющих {_f(parts)}, в документе {_f(p.total)}.", p.total - parts))
            if p.unit_price and p.qty and abs(p.unit_price * p.qty - p.total) > p.qty * 0.005 + TOL_RUB:
                errs += 1
                out.append(Finding("S-05", "FAIL", "Цена за единицу × объём ≠ сумма позиции",
                                   doc.ref(p.total_row), loc,
                                   f"{_f(p.unit_price)} × {p.qty} = {_f(p.unit_price * p.qty)}, в документе {_f(p.total)}."))
        if p.is_block and (p.qty or 0) > 0 and not p.total and not p.children:
            out.append(Finding("S-05", "WARNING", "Позиция с объёмом, но без стоимости", doc.ref(p.row), loc,
                               f"«{p.name[:80]}», {p.qty} {p.unit}, «Всего по позиции» пусто. Работа включена "
                               "в документ, но не оценена (или стоимость учтена в соседней позиции — "
                               "например, отдельной строкой машины). Проверить, не потеряна ли расценка."))
    # разделы
    for s in doc.sections:
        calc = sum(doc.positions[k].total or 0 for k in s["keys"] if doc.positions[k].is_block)
        if s["total"] is not None and abs(calc - s["total"]) > TOL_RUB:
            errs += 1
            out.append(Finding("S-05", "FAIL", "Итог раздела ≠ сумме позиций", doc.ref(s["row"]), s["name"],
                               f"Сумма позиций {_f(calc)}, итог раздела {_f(s['total'])}.", s["total"] - calc))
    t = doc.totals
    if "vsego" in t and doc.sections:
        calc = sum(s["total"] or 0 for s in doc.sections)
        if abs(calc - t["vsego"][0]) > TOL_RUB:
            errs += 1
            out.append(Finding("S-05", "FAIL", "«Всего» ≠ сумме разделов", doc.ref(t["vsego"][1]), "итоги",
                               f"Сумма разделов {_f(calc)}, «Всего» {_f(t['vsego'][0])}."))
    if "reduction" in t and "vsego" in t and t["reduction"][0] is not None:
        exp = t["vsego"][0] * t["reduction"][0]
        if abs(exp - t["reduction"][1]) > 0.05:
            errs += 1
            out.append(Finding("S-05", "FAIL", "Неверно применён коэффициент снижения", doc.ref(t["reduction"][2]),
                               "итоги", f"{_f(t['vsego'][0])} × {t['reduction'][0]} = {_f(exp)}, "
                                        f"в документе {_f(t['reduction'][1])}."))
    if "tax_line" in t and "reduction" in t and t["tax_line"][0]:
        exp = t["reduction"][1] * (t["tax_line"][0] - 1)
        if abs(exp - t["tax_line"][1]) > 0.05:
            errs += 1
            out.append(Finding("S-05", "FAIL", "Неверно рассчитана строка УСН/НДС", doc.ref(t["tax_line"][2]),
                               "итоги", f"Ожидалось {_f(exp)}, в документе {_f(t['tax_line'][1])}."))
    if "grand" in t:
        base = t["reduction"][1] if "reduction" in t else t.get("vsego", (0,))[0]
        exp = base + (t["tax_line"][1] if "tax_line" in t else 0)
        if abs(exp - t["grand"][0]) > 0.05:
            errs += 1
            out.append(Finding("S-05", "FAIL", "Итог документа не сходится", doc.ref(t["grand"][1]), "итоги",
                               f"Ожидалось {_f(exp)}, в документе {_f(t['grand'][0])}."))
    # титульный блок ЛСР
    hs = doc.header_summary
    if hs and "total" in hs and "grand" in t:
        if abs(hs["total"] * 1000 - t["grand"][0]) > 10:
            out.append(Finding("S-05", "FAIL", "«Сметная стоимость» на титуле ≠ итогу сметы", doc.ref(), "титул",
                               f"Титул {_f(hs['total'])} тыс., итог {_f(t['grand'][0] / 1000)} тыс."))
        parts = sum(hs.get(k, 0) for k in ("строительных работ", "монтажных работ", "оборудования", "прочих затрат"))
        if abs(parts - hs["total"]) > 0.02:
            out.append(Finding("S-05", "WARNING", "Расшифровка на титуле ЛСР не сходится с итогом", doc.ref(), "титул",
                               f"Сметная стоимость {_f(hs['total'])} тыс., а «в том числе» даёт {_f(parts)} тыс. "
                               "Строительные и монтажные работы показаны до коэффициента снижения и надбавки УСН, "
                               "итог — после. Форма 421/пр предполагает, что расшифровка складывается в итог; "
                               "заказчик или экспертиза могут вернуть смету на исправление."))
    if errs == 0:
        out.append(Finding("S-05", "OK", "Арифметика документа сходится", doc.ref(), "—",
                           "Проверено: объём×коэф., НР/СП от ФОТ, состав «Всего по позиции», цена×объём, "
                           "итоги разделов, коэффициент снижения, итог документа."))
    return out


# ---------------------------------------------------------------- S-06 / S-08
def s06_ks3(acts: list, ks3: list) -> list:
    if not ks3:
        return [Finding("S-06", "NEEDS_CONTEXT", "КС-3 не приложена", "—", "—",
                        "Сверка КС-2 ↔ КС-3 (сумма за период, нарастающий итог) не выполнена. "
                        f"Для сверки ожидается итог КС-3 = {_f(sum(a.totals.get('grand', (0,))[0] for a in acts))} руб.")]
    return []


def s08_act_series(lsr: Document, acts: list) -> list:
    out = []
    for a in acts:
        m = a.meta
        cd, pf, pt, ad = (_date(m.get(k, "")) for k in ("contract_date", "period_from", "period_to", "act_date"))
        if cd and pf and pf < cd:
            out.append(Finding("S-08", "FAIL", "Отчётный период начинается раньше даты контракта", a.ref(), "шапка",
                               f"Период с {m['period_from']}, контракт от {m['contract_date']}."))
        if pt and ad and ad < pt:
            out.append(Finding("S-08", "WARNING", "Акт составлен до окончания отчётного периода", a.ref(), "шапка",
                               f"Дата акта {m['act_date']}, период по {m['period_to']}."))
        obj_a, obj_l = m.get("объект", ""), lsr.meta.get("объект", "")
        if obj_a and obj_l and _sim(obj_a, obj_l) < 0.95:
            out.append(Finding("S-08", "WARNING", "Наименование объекта в КС-2 отличается от сметы",
                               f"{a.ref()} ↔ {lsr.ref()}", "шапка", f"КС-2: «{obj_a[:90]}»; ЛСР: «{obj_l[:90]}»."))
    if len(acts) > 1:
        ordered = sorted(acts, key=lambda a: _date(a.meta.get("period_from", "")) or datetime.min)
        for prev, nxt in zip(ordered, ordered[1:]):
            a, b = _date(prev.meta.get("period_to", "")), _date(nxt.meta.get("period_from", ""))
            if a and b and b <= a:
                out.append(Finding("S-08", "FAIL", "Отчётные периоды актов перекрываются",
                                   f"{prev.ref()} ↔ {nxt.ref()}", "шапки",
                                   f"№{prev.meta.get('act_number')} по {prev.meta.get('period_to')}, "
                                   f"№{nxt.meta.get('act_number')} с {nxt.meta.get('period_from')}."))
    if not out:
        m = acts[0].meta
        out.append(Finding("S-08", "OK", "Реквизиты и период акта согласованы", "КС-2", "шапка",
                           f"Контракт {m.get('contract_number')} от {m.get('contract_date')}; акт №{m.get('act_number')} "
                           f"от {m.get('act_date')}, период {m.get('period_from')}–{m.get('period_to')}; объект совпадает со сметой."))
    return out


# ---------------------------------------------------------------- эвристики (H-*)
PIPE_RE = re.compile(r"труб|колен|хомут|отвод|муфт", re.I)
GUTTER_RE = re.compile(r"желоб|воронк", re.I)
DIAM_RE = re.compile(r"диаметр\w*\s*(?:трубы\s*)?(\d+(?:[.,]\d+)?)\s*мм", re.I)


def h01_diameters(doc: Document) -> list:
    out = []
    for p in doc.positions.values():
        if not p.is_block:
            continue
        items = [(r.name, r.row, r.marker) for r in p.resources if not r.marker] + \
                [(doc.positions[c].name, doc.positions[c].row, "") for c in p.children]
        el = []
        for name, row, mk in items:
            if not PIPE_RE.search(name) or GUTTER_RE.search(name) or re.match(r"\s*(дюбел|шуруп|болт)", name, re.I):
                continue
            ds = {d.replace(",", ".") for d in DIAM_RE.findall(name)}
            if ds:
                el.append((name, row, ds))
        conflicts = [(a, b) for i, a in enumerate(el) for b in el[i + 1:] if not (a[2] & b[2])]
        if conflicts:
            a, b = conflicts[0]
            out.append(Finding("H-01", "WARNING", "Разные диаметры труб и комплектующих в одной позиции",
                               doc.ref(p.row), f"поз. {p.key} «{p.name[:50]}»",
                               f"«{a[0][:70]}» (Ø{', '.join(sorted(a[2]))}) и «{b[0][:70]}» (Ø{', '.join(sorted(b[2]))}). "
                               "Трубы и фасонные части/крепёж одной системы должны быть одного типоразмера — иначе "
                               "в смете оплачен комплект, который нельзя смонтировать. Эвристика: подтвердить по ТЗ."))
    return out


def h02_debris_balance(doc: Document) -> list:
    debris = sum(r.qty or 0 for p in doc.positions.values() for r in p.resources
                 if r.code == "999-9900" and not re.search(r"очистк|погрузк|мусор", p.name, re.I))
    haul = [p for p in doc.positions.values()
            if unit_mult(p.unit)[1] == "т" and re.search(r"мусор|перевозк", p.name, re.I)]
    if not haul or not debris:
        return []
    h = max(p.qty or 0 for p in haul)
    if abs(h - debris) / h > 0.05:
        return [Finding("H-02", "NEEDS_CONTEXT", "Масса мусора к вывозу не подтверждена расчётом",
                        doc.ref(), "поз. " + ", ".join(p.key for p in haul),
                        f"По нормативным ресурсам демонтажа (999-9900) образуется {_f(debris, 3)} т, "
                        f"к погрузке/вывозу принято {_f(h, 3)} т. Разница может быть законной (масса "
                        "окон, дверей и т.п. по отдельному расчёту), но расчёт массы мусора не приложен.")]
    return []


def _stem(name: str) -> str:
    w = re.findall(r"[а-яё]+", name.lower())
    return w[0][:4] if w else ""


def _match_norm_materials(doc: Document, p) -> list:
    """Строки неучтённых материалов нормы → материалы, которыми их закрыли.
    Порядок: (1) код ФСБЦ начинается с кода группы нормы; (2) тот же корень наименования;
    (3) единственный ещё не сопоставленный материал с той же единицей. Возвращает [(norm, mats, method)]."""
    norms = [r for r in p.resources if r.marker in ("Н", "П,Н", "Уд") and (r.qty or 0) > 0
             and not r.code.startswith("999-")]
    kids = [doc.positions[c] for c in p.children if (doc.positions[c].qty or 0) > 0]
    used, res = set(), []
    for r in norms:
        rc = _code(r.code)
        mats = [m for m in kids if (lambda b: b == rc or b.startswith(rc + "-"))(_code(m.code).replace("ФСБЦ-", ""))]
        method = "код"
        if not mats:
            # корень первого слова + слова в скобках («Материалы водосточной системы (трубы водосточные…)»)
            inner = " ".join(re.findall(r"\(([^)]*)\)", r.name.lower()))
            stems = {_stem(r.name)} | {w[:4] for w in re.findall(r"[а-яё]{4,}", inner)}
            mats = [m for m in kids if _stem(m.name) and _stem(m.name) in stems]
            method = "наименование"
        res.append([r, mats, method])
        used |= {m.key for m in mats}
    for item in res:
        if item[1]:
            continue
        br = unit_mult(item[0].unit)[1]
        free = [m for m in kids if m.key not in used and unit_mult(m.unit)[1] == br]
        if len(free) == 1:
            item[1], item[2] = free, "замена (единственный материал той же единицы)"
            used.add(free[0].key)
    return res


def h03_replacement_qty(doc: Document) -> list:
    """Неучтённый материал нормы ГЭСН (строки Н / П,Н / Уд) ↔ материал, которым его закрыли
    (подпозиции 28.1… в xlsx или отдельные позиции ФСБЦ сразу за работой в PDF).
    H-03: количество отличается от нормы. H-04: материал нормы в смете не найден."""
    out = []
    for p in doc.positions.values():
        if not p.is_block:
            continue
        pairs = _match_norm_materials(doc, p)
        # несколько строк нормы на один и тот же набор материалов (верхний + нижний слой кровли) — сравниваем суммой
        groups = {}
        for r, mats, how in pairs:
            if mats:
                groups.setdefault(tuple(m.key for m in mats), []).append((r, how))
        for keys, rows in groups.items():
            mats = [doc.positions[k] for k in keys]
            mr, br = unit_mult(rows[0][0].unit)
            if any(unit_mult(r.unit)[1] != br for r, _ in rows) or any(unit_mult(m.unit)[1] != br for m in mats):
                continue                                   # м2 ↔ шт и т.п. — несравнимо, не гадаем
            norm = sum(r.qty * unit_mult(r.unit)[0] for r, _ in rows) / mr
            got = sum(m.qty * unit_mult(m.unit)[0] for m in mats) / mr
            ratio = got / norm
            if 0.67 <= ratio <= 1.5:
                continue
            price = sum(m.total or 0 for m in mats) / got if got else None
            rub = (got - norm) * price if price else None
            r0 = rows[0][0]
            hint = ""
            if len(rows) == 1 and r0.qty_per and abs(got - r0.qty_per) / got < 0.02 and abs(p.qty or 1) != 1:
                hint = (f" Количество совпадает с нормой на одну единицу расценки ({_f(r0.qty_per, 3)} на "
                        f"{p.unit}) — похоже, норма перенесена без умножения на объём {p.qty:g} {p.unit}.")
            nr = "; ".join(f"[{r.marker}] {r.code} «{r.name[:35]}» {_f(r.qty, 4)} {r.unit}" for r, _ in rows)
            mt = ", ".join(f"поз. {m.key} «{m.name[:40]}» {m.qty:g} {m.unit}" for m in mats)
            how = rows[0][1]
            out.append(Finding("H-03", "WARNING",
                               "Материала в смете больше нормы" if ratio > 1 else "Материала в смете меньше нормы",
                               doc.ref(r0.row), f"поз. {p.key} «{p.name[:45]}»",
                               f"Норма ГЭСН на {p.qty:g} {p.unit}: {nr}. Заложено ({how}): {mt} "
                               f"= {_f(got, 4)} {rows[0][0].unit if mr == 1 else br} (×{ratio:.2f}).{hint} "
                               f"Разница до коэффициентов: {_f(rub)} руб. Отклонение допустимо, если обосновано "
                               "(ТЗ, спецификация, техкарта) — в пакете обоснования нет.", rub))
        missing = [r for r, mats, _ in pairs if not mats]
        if missing:
            lst = "; ".join(f"{r.code} «{r.name[:45]}» {_f(r.qty, 4)} {r.unit}" for r in missing)
            kids = " ".join(doc.positions[c].name.lower() for c in p.children)
            if any("фланц" in r.name.lower() for r in missing) and re.search(r"муфтов|сгон|резьб", kids):
                why = ("Норма рассчитана на фланцевое присоединение (в её составе фланцы), а заложены муфтовые "
                       "краны/сгоны — фланцы физически не нужны, поэтому их нет. Но тогда расценка подобрана "
                       "не под изделие: трудозатраты фланцевого монтажа оплачиваются за муфтовую сборку. "
                       "Вопрос к сметчику заказчика; при проверке КС-2 экспертиза может снять разницу.")
            elif all(re.search(r"растворит", r.name, re.I) for r in missing):
                why = "Мелочь: растворитель для ЛКМ по норме не оценён — расход небольшой, но покупать его подрядчику."
            else:
                why = ("Материал по технологии нужен, а отдельной позиции с ним нет — подрядчик поставит его за свой "
                       "счёт (в цене договора его нет), либо он заменён без явной связи. Проверить по ТЗ и факту.")
            out.append(Finding("H-04", "NEEDS_CONTEXT", "Материал нормы не оценён в смете",
                               doc.ref(missing[0].row), f"поз. {p.key} «{p.name[:45]}»",
                               f"По норме нужен неучтённый материал: {lst}. {why}"))
    return out


def info_replacements(doc: Document) -> list:
    neg = [p for p in doc.positions.values() if (p.qty or 0) < 0]
    if not neg:
        return []
    lst = "; ".join(f"поз. {p.key} «{p.name[:50]}» {p.qty} {p.unit}" for p in neg)
    return [Finding("INFO", "INFO", "Замены материалов в смете (исключённые ресурсы)", doc.ref(), "—",
                    f"Исключено ресурсов: {len(neg)} — {lst}. Это нормальная практика замены материала; "
                    "на этапе сверки с ТЗ проверить, что заменяющий материал соответствует требованиям ТЗ.")]


def _merge_same(findings: list) -> list:
    """Одна и та же внутренняя находка в ЛСР и в КС-2 (КС-2 — копия сметы) → одна строка отчёта."""
    strip = lambda t: re.sub(r"\(?стр\. \d+\)?", "", t)
    merged, seen = [], {}
    for f in findings:
        k = (f.rule_id, f.title, strip(f.location), strip(f.explanation))
        if k in seen:
            seen[k].docs += " ; " + f.docs
        else:
            seen[k] = f
            merged.append(f)
    return merged


# ---------------------------------------------------------------- запуск
def run_all(lsr: Document, acts: list, ks3: Optional[list] = None) -> tuple[list, list]:
    findings = []
    findings += s01_positions_in_estimate(lsr, acts)
    f2, table = s02_cumulative_volume(lsr, acts)
    findings += f2
    findings += s03_units_prices(lsr, acts)
    findings += s04_coefficients(lsr, acts)
    for d in [lsr] + acts:
        findings += s05_arithmetic(d)
        findings += h01_diameters(d)
        findings += h02_debris_balance(d)
        findings += h03_replacement_qty(d)
    findings += info_replacements(lsr)
    findings = _merge_same(findings)
    findings += s06_ks3(acts, ks3 or [])
    findings += s08_act_series(lsr, acts)
    order = {s: i for i, s in enumerate(("FAIL", "WARNING", "NEEDS_CONTEXT", "INFO", "OK"))}
    findings.sort(key=lambda f: (order[f.status], -(f.delta_rub or 0), f.rule_id))
    return findings, table
