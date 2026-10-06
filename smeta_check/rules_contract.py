"""Проверки пакета на этапе договора: договор ↔ смета контракта ↔ ЛСР ↔ ВОР ↔ перечень скрытых работ (ТЗ)."""
import re
from difflib import SequenceMatcher

from .models import Document, Finding
from .rules import _f

WORK_CODE = re.compile(r"^(ГЭСН|ФЕР|ТЕР|47-|49-)", re.I)   # работы (не материалы/оборудование)


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[«»\"'()]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def unit_split(u: str) -> tuple:
    u = u.strip().lower().replace(".", "")
    m = re.match(r"^(\d+)\s*(.+)$", u)
    mult, base = (int(m.group(1)), m.group(2)) if m else (1, u)
    base = base.replace("т груза", "т").replace("тонна", "т").strip()
    return mult, base


def eval_formula(f: str):
    f = f.replace(",", ".").replace("х", "*").replace("x", "*").replace("×", "*").replace(" ", "")
    if not f or not re.fullmatch(r"[\d.+\-*/()]+", f) or f.count("(") != f.count(")"):
        return None
    try:
        return float(eval(f, {"__builtins__": {}}))
    except Exception:
        return None


# ------------------------------------------------------------ C: цена и НДС
def c_price_vat(contract: dict, lsr: Document, smeta_contract: dict) -> list:
    out = []
    price = contract.get("price")
    grand = lsr.totals.get("grand", (None,))[0]
    fin = sum(contract.get("financing") or [])
    sc = smeta_contract.get("total")
    vals = {"Цена договора (п. 2.1)": price, "ВСЕГО по ЛСР (прил. 7)": grand,
            "Смета контракта (xlsx)": sc, "Источники финансирования (п. 2.4)": fin or None}
    known = {k: v for k, v in vals.items() if v}
    if len({round(v, 2) for v in known.values()}) == 1:
        out.append(Finding("C-01", "OK", "Цена договора совпадает во всех документах", "Договор ↔ ЛСР ↔ смета контракта",
                           "—", "; ".join(f"{k}: {_f(v)}" for k, v in known.items())))
    else:
        out.append(Finding("C-01", "FAIL", "Цена договора различается в документах", "Договор ↔ ЛСР ↔ смета контракта",
                           "—", "; ".join(f"{k}: {_f(v)}" for k, v in known.items())))

    vat_c = contract.get("vat")
    vat_l = lsr.totals.get("vat")
    if vat_c is not None and vat_l:
        rate_l = vat_l[0]
        rate_c = round(vat_c / (price - vat_c) * 100, 2) if price and price != vat_c else 0
        if abs(rate_c - rate_l) > 0.1:
            wo_c, wo_l = price - vat_c, lsr.totals["after_reduction"][0]
            k = (1 + rate_l / 100) / (1 + rate_c / 100)
            out.append(Finding(
                "C-02", "FAIL", "НДС в договоре и в смете разный — база для КС-2 не определена",
                "Договор п. 2.1, прил. 1 ↔ ЛСР (прил. 7), смета контракта", "итоги",
                f"Договор: НДС {rate_c:g}% ({contract.get('vat_rate_frac', '')}) = {_f(vat_c)}, цена без НДС {_f(wo_c)}. "
                f"ЛСР и смета контракта: НДС {rate_l:g}% = {_f(vat_l[1])}, цена без НДС {_f(wo_l)}. "
                f"Разница в базе без НДС: {_f(wo_c - wo_l)} руб. Оплата — «100% по фактическому объёму». "
                f"Если КС-2 считать по ЛСР без НДС и начислить {rate_c:g}%, максимум к оплате "
                f"{_f(wo_l * (1 + rate_c / 100))} — на {_f(price - wo_l * (1 + rate_c / 100))} меньше цены договора. "
                f"Чтобы выйти на цену договора, в КС-2 нужен пересчётный коэффициент {k:.6f} "
                f"(= {1 + rate_l / 100:g}/{1 + rate_c / 100:g}) — в договоре он не прописан. "
                "Способ расчёта стоимости в КС-2/КС-3 зафиксировать письменно с заказчиком ДО первого акта.",
                wo_c - wo_l))
        else:
            out.append(Finding("C-02", "OK", "Ставка НДС в договоре и смете совпадает", "Договор ↔ ЛСР", "—", ""))

    red = lsr.totals.get("reduction")
    if red and red[0]:
        k = red[0]
        v = lsr.totals["vsego"][0]
        ar = lsr.totals["after_reduction"][0]
        amt = red[1]
        info = (f"Сметная стоимость с НДС 22% была бы {_f(v * 1.22)}; она урезана на {(1 - ar / v) * 100:.1f}% "
                "до суммы финансирования. k «некруглый» — получен делением лимита на сметную стоимость.")
        if abs(v * k - ar) <= 0.05 and (amt is None or abs(v + amt - ar) <= 0.05):
            out.append(Finding("C-03", "INFO", "Коэффициент снижения подобран под лимит финансирования", lsr.ref(),
                               "итоги", f"k = {k}. {info} Применять тот же k в каждой КС-2."))
        else:
            out.append(Finding("C-03", "WARNING", "Итоги ЛСР по коэффициенту снижения не сходятся между собой",
                               lsr.ref(), "итоги",
                               f"Напечатан k = {k}: Всего × k = {_f(v * k)}. Строка снижения {_f(amt)}: "
                               f"Всего − снижение = {_f(v + (amt or 0))}. Итог «Всего с учётом…» = {_f(ar)}. "
                               f"Фактически применённый k = {ar / v:.10f}. {info} "
                               f"Если в КС-2 взять напечатанный k, акт на 100% объёма превысит цену договора на "
                               f"{_f((v * k - ar) * (1 + (lsr.totals.get('vat', (0,))[0] or 0) / 100))} с НДС — "
                               "ПИК ЕАСУЗ/экспертиза может вернуть документ. Согласовать с заказчиком, каким k "
                               "считать акты (практично — тем, что даёт итог сметы).", v * k - ar))
    return out


# ------------------------------------------------------------ L: арифметика ЛСР из PDF
def l_arithmetic(lsr: Document) -> list:
    out, errs = [], 0
    for p in lsr.positions.values():
        if p.qty_base is not None and p.coef is not None and p.qty is not None and \
                abs(p.qty_base * p.coef - p.qty) > max(abs(p.qty) * 1e-6, 1e-6):
            errs += 1
            out.append(Finding("S-05", "FAIL", "Объём × коэффициент ≠ итоговый объём", lsr.ref(), f"поз. {p.key}",
                               f"{p.qty_base} × {p.coef} ≠ {p.qty}"))
        if p.unit_price and p.qty and p.total and abs(p.unit_price * p.qty - p.total) > p.qty * 0.005 + 1:
            errs += 1
            out.append(Finding("S-05", "FAIL", "Цена за единицу × объём ≠ сумма позиции", lsr.ref(), f"поз. {p.key}",
                               f"{_f(p.unit_price)} × {p.qty} = {_f(p.unit_price * p.qty)}, в смете {_f(p.total)}"))
    for s in lsr.sections:
        c = sum(lsr.positions[k].total or 0 for k in s["keys"])
        if abs(c - (s["total"] or 0)) > 1:
            errs += 1
            out.append(Finding("S-05", "FAIL", "Итог раздела ≠ сумме позиций", lsr.ref(), s["name"],
                               f"{_f(c)} vs {_f(s['total'])}", s["total"] - c))
    t = lsr.totals
    tot = sum(s["total"] or 0 for s in lsr.sections)
    if abs(tot - t["vsego"][0]) > 1:
        errs += 1
        out.append(Finding("S-05", "FAIL", "«Всего» ≠ сумме разделов", lsr.ref(), "итоги", f"{_f(tot)} vs {_f(t['vsego'][0])}"))
    if "vat" in t and abs(t["after_reduction"][0] * t["vat"][0] / 100 - t["vat"][1]) > 0.05:
        errs += 1
        out.append(Finding("S-05", "FAIL", "НДС в смете рассчитан неверно", lsr.ref(), "итоги", ""))
    if "grand" in t and abs(t["after_reduction"][0] + t["vat"][1] - t["grand"][0]) > 0.05:
        errs += 1
        out.append(Finding("S-05", "FAIL", "«ВСЕГО по смете» не сходится", lsr.ref(), "итоги", ""))
    if not errs:
        out.append(Finding("S-05", "OK", "Арифметика ЛСР сходится", lsr.ref(), "—",
                           f"{len(lsr.positions)} позиций: объём×коэф., цена×объём, {len(lsr.sections)} итогов разделов, "
                           "«Всего», снижение, НДС, «ВСЕГО по смете»."))
    return out


# ------------------------------------------------------------ V: ВОР ↔ ЛСР
def v_vor_lsr(vor: list, lsr: Document) -> tuple:
    out, rows = [], []
    seen = set()
    cnt = {"unit": 0, "qty": 0, "name": 0, "formula": 0, "missing": 0}
    for it in vor:
        p = lsr.positions.get(it["lsr"])
        row = {"vor": it["n"], "lsr": it["lsr"], "name_vor": it["name"], "unit_vor": it["unit"], "qty_vor": it["qty"],
               "formula": it["formula"], "f_val": eval_formula(it["formula"]) if it["formula"] else None,
               "name_lsr": p.name if p else "", "unit_lsr": p.unit if p else "", "qty_lsr": p.qty if p else None,
               "mult": None, "status": "OK", "note": ""}
        rows.append(row)
        loc = f"ВОР п. {it['n']} → ЛСР поз. {it['lsr']}"
        if p is None:
            cnt["missing"] += 1
            row["status"] = "FAIL"
            out.append(Finding("V-01", "FAIL", "Позиции ВОР нет в ЛСР", "ВОР ↔ ЛСР", loc, it["name"][:90]))
            continue
        seen.add(it["lsr"])
        mv, bv = unit_split(it["unit"])
        ml, bl = unit_split(p.unit)
        row["mult"] = ml / mv
        if bv != bl and not (len(bv) >= 5 and len(bl) >= 5 and (bv.startswith(bl) or bl.startswith(bv))):
            cnt["unit"] += 1
            row["status"] = "FAIL"
            out.append(Finding("V-02", "FAIL", "Единица измерения в ВОР и ЛСР разная", "ВОР ↔ ЛСР", loc,
                               f"«{it['name'][:60]}»: ВОР «{it['unit']}», ЛСР «{p.unit}»."))
        elif p.qty is not None and it["qty"] is not None:
            lq = p.qty * ml / mv
            if abs(lq - it["qty"]) > max(abs(it["qty"]) * 1e-3, 1e-6):
                cnt["qty"] += 1
                row["status"] = "FAIL"
                up = p.unit_price or (p.total / p.qty if p.total and p.qty else None)
                d = (lq - it["qty"]) / (ml / mv) * up if up else None
                out.append(Finding("V-03", "FAIL", "Объём в ЛСР не равен объёму ВОР", "ВОР ↔ ЛСР", loc,
                                   f"«{it['name'][:60]}»: ВОР {it['qty']:g} {it['unit']}, ЛСР {p.qty:g} {p.unit} "
                                   f"(= {lq:g} {it['unit']}).", d))
        if sim(it["name"], p.name) < 0.75 and sim(it["name"][:60], p.name[:60]) < 0.75:
            cnt["name"] += 1
            if row["status"] == "OK":
                row["status"] = "WARNING"
            out.append(Finding("V-04", "WARNING", "Наименование в ВОР и ЛСР не совпадает", "ВОР ↔ ЛСР", loc,
                               f"ВОР: «{it['name'][:70]}»; ЛСР: «{p.name[:70]}»."))
        fv = row["f_val"]
        is_calc = bool(re.search(r"[*/+\-(]", it["formula"] or ""))
        if not is_calc:
            cnt.setdefault("no_calc", 0)
            cnt["no_calc"] = cnt.get("no_calc", 0) + 1
            row["note"] = "расчёт не раскрыт"
        if is_calc and fv is not None and it["qty"] and abs(fv - it["qty"]) > max(abs(it["qty"]) * 0.005, 1e-6):
            cnt["formula"] += 1
            if row["status"] == "OK":
                row["status"] = "WARNING"
            out.append(Finding("V-05", "WARNING", "Формула расчёта в ВОР не даёт указанный объём", "ВОР", loc,
                               f"«{it['name'][:60]}»: формула «{it['formula']}» = {fv:g}, в ВОР {it['qty']:g} {it['unit']}."))
    nc = cnt.pop("no_calc", 0)
    if nc:
        out.append(Finding("V-05", "INFO", "Объёмы в ВОР в основном не обоснованы расчётом", "ВОР", "графа «Формула расчёта»",
                           f"У {nc} из {len(vor)} позиций в графе «Формула» стоит само число (часто округлённое: «0» "
                           "при 0,0132 т), а не расчёт. Без РД это единственное обоснование объёмов. При приёмке по "
                           "фактическому объёму спор об объёме решать будет нечем — подрядчику стоит вести свои обмеры "
                           "с фотофиксацией (ТЗ прил. 6) с первого дня."))
    extra = [k for k in lsr.positions if k not in seen]
    for k in extra:
        p = lsr.positions[k]
        out.append(Finding("V-01", "FAIL", "Позиции ЛСР нет в ВОР", "ЛСР ↔ ВОР", f"ЛСР поз. {k}",
                           f"«{p.name[:80]}», {p.qty} {p.unit}, {_f(p.total)} руб.", p.total))
    if not any(cnt.values()) and not extra:
        out.append(Finding("V-01", "OK", "ВОР и ЛСР совпадают 1:1", "ВОР ↔ ЛСР", "—",
                           f"{len(vor)} позиций ВОР = {len(lsr.positions)} позиций ЛСР; единицы и объёмы совпадают "
                           "с учётом множителей (м2 ↔ 100 м2 и т.п.)."))
    return out, rows


# ------------------------------------------------------------ Z: перечень скрытых работ (ТЗ прил. 9) ↔ ЛСР
HIDDEN_HINT = re.compile(r"гидроизоляц|пароизоляц|изоляция|утеплен|армирован|стяжк|огрунтовк|антисепт|огнебио|огнезащ|"
                         r"обрешетк|стропил|заземлит|прокладка трубопровод|затягивание провод|труба гофрир|кладк|отмостк|"
                         r"закладн|ветрозащит|подстилающ", re.I)


def z_hidden_works(hw: list, lsr: Document, vor: list) -> tuple:
    out, plan = [], []
    keys = [k for k, p in lsr.positions.items() if WORK_CODE.match(p.code)]
    ptr = 0
    used = set()
    vor_by_lsr = {it["lsr"]: it for it in vor}
    for h in hw:
        best, best_s = None, 0.0
        # перечень идёт в порядке ЛСР — ищем вперёд от предыдущего совпадения, потом по всей смете
        for scope in (keys[ptr:], keys):
            for k in scope:
                if k in used:
                    continue
                s = sim(h["name"], lsr.positions[k].name)
                if s > best_s:
                    best, best_s = k, s
                if s > 0.97:
                    break
            if best_s >= 0.9:
                break
        if best and best_s >= 0.85:
            used.add(best)
            ptr = keys.index(best) + 1
            p = lsr.positions[best]
            v = vor_by_lsr.get(best, {})
            plan.append({"n": h["n"], "name": h["name"], "lsr": best, "section": p.section,
                         "qty": v.get("qty"), "unit": v.get("unit", p.unit), "score": round(best_s, 2)})
        else:
            plan.append({"n": h["n"], "name": h["name"], "lsr": None, "section": "", "qty": None, "unit": "",
                         "score": round(best_s, 2)})
            out.append(Finding("Z-01", "WARNING", "Скрытая работа из ТЗ не найдена в ЛСР",
                               "ТЗ прил. 9 ↔ ЛСР", f"перечень п. {h['n']}",
                               f"«{h['name'][:90]}». Лучшее совпадение в смете — {best_s:.0%}. АОСР по работе, "
                               "которой нет в смете, оформить нечем — уточнить у заказчика."))
    found = [x for x in plan if x["lsr"]]
    out.append(Finding("Z-01", "OK" if len(found) == len(hw) else "INFO",
                       f"Перечень скрытых работ привязан к смете: {len(found)} из {len(hw)}",
                       "ТЗ прил. 9 ↔ ЛСР", "—",
                       f"По договору (ТЗ, прил. 9) обязательно {len(hw)} АОСР — это уровень «обязательно по договору». "
                       "Привязка «п. перечня → позиция ЛСР → объём из ВОР» — на листе «План АОСР»; "
                       "её можно сразу отдать генератору актов."))
    add = [x for x in plan if re.search(r"добавлять или исключать", x["name"], re.I)]
    if add:
        pairs = "; ".join(f"п. {x['n'] - 1} + п. {x['n']} (ЛСР поз. {plan[x['n'] - 2]['lsr']} и {x['lsr']})" for x in add)
        out.append(Finding("Z-03", "INFO", "Перечень скрытых работ собран из строк сметы механически", "ТЗ прил. 9", pairs,
                           "Добавочная расценка «на каждые N мм изменения толщины» — это та же стяжка, а не отдельная "
                           "работа: физически один слой и одно освидетельствование. По перечню это два АОСР с одинаковыми "
                           "датами и объёмом — верификатор ИД такое подсветит (правило 4), стройконтроль тоже. "
                           "Предложить заказчику оформлять один АОСР на итоговую толщину со ссылкой на обе позиции."))
    cand = [k for k in keys if k not in used and HIDDEN_HINT.search(lsr.positions[k].name)
            and not re.match(r"(разборк|демонтаж|снятие|отбивк)", lsr.positions[k].name, re.I)
            and "открыто" not in lsr.positions[k].name.lower()]
    if cand:
        lst = "; ".join(f"поз. {k} «{lsr.positions[k].name[:55]}» ({lsr.positions[k].section.split('.')[1].strip()[:18]})"
                        for k in cand)
        out.append(Finding("Z-02", "NEEDS_CONTEXT", "Работы, похожие на скрытые, но не включённые в перечень ТЗ",
                           "ЛСР ↔ ТЗ прил. 9", f"{len(cand)} поз.",
                           f"{lst}. По договору АОСР на них не требуются, но при приёмке стройконтроль может их "
                           "запросить (СП 543 — рекомендательно). Решить заранее: оформлять или согласовать отказ."))
    return out, plan
