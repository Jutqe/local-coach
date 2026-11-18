"""Life Reset: аудит жизни по двенадцати областям.

Одна строка на область вместо опросника: «устраивает / важно / хочу ли менять» –
например «6 9 да». Только там, где хочется менять, – вопрос, настоящая ли это
проблема или ощущение, что «так должно быть лучше». Низкая оценка – ещё не цель.
Перед вопросом коуч показывает, что уже знает об области из памяти, чтобы не
пересказывать свою жизнь вручную. Раунды хранятся с датой: каждый следующий
сравнивается с исходной точкой.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import guard, store
from .config import LR_AREAS, PRIVATE_DOMAINS
from .util import iso_now, today
from .i18n import trl

AREA_TITLES = {key: title for key, title, _d, _dom in LR_AREAS}
CONTINUE_DAYS = 14          # незаконченный раунд моложе этого продолжается, а не начинается заново
CHANGE_WORDS = {"да": "yes", "д": "yes", "хочу": "yes", "нет": "no", "н": "no", "не": "no",
                "может": "maybe", "возможно": "maybe", "м": "maybe", "?": "maybe",
                "yes": "yes", "y": "yes", "want": "yes", "no": "no", "n": "no", "not": "no",
                "maybe": "maybe", "perhaps": "maybe", "m": "maybe"}
CHANGE_TITLES = {"yes": trl("хочу менять"), "no": trl("не хочу менять"), "maybe": trl("может быть")}
NATURE_TITLES = {"problem": trl("настоящая проблема"), "should": trl("скорее «так должно быть»")}


def _days(iso: str) -> int:
    from datetime import date
    return (date.fromisoformat(today()) - date.fromisoformat(iso[:10])).days


def rounds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT round, MIN(created_at) AS started, COUNT(*) AS n FROM lr_audit GROUP BY round ORDER BY round"):
        out.append({"round": r["round"], "started": r["started"], "n": r["n"]})
    return out


def current_round(conn: sqlite3.Connection, fix: bool = False) -> tuple[int, bool]:
    """(номер раунда, продолжение ли это уже начатого). Раунд моложе двух недель продолжается –
    в том числе законченный: так `--areas` исправляет ответ, а не начинает новый раунд из одной области.
    fix – исправить ответы последнего раунда, сколько бы ему ни было."""
    rs = rounds(conn)
    if rs and (fix or _days(rs[-1]["started"]) <= CONTINUE_DAYS):
        return rs[-1]["round"], True
    return (rs[-1]["round"] + 1 if rs else 1), False


def answers(conn: sqlite3.Connection, round_: int) -> dict[str, sqlite3.Row]:
    return {r["area"]: r for r in conn.execute("SELECT * FROM lr_audit WHERE round=?", (round_,))}


def known(conn: sqlite3.Connection, domains: tuple[str, ...], limit: int = 4) -> list[str]:
    """Что память знает об области сейчас: свежее и не ушедшее в прошлое."""
    doms = [d for d in domains if d not in PRIVATE_DOMAINS]
    if not doms:
        return []
    marks = ",".join("?" * len(doms))
    rows = conn.execute(
        f"SELECT f.claim, COALESCE(f.event_date, f.note_date) AS d FROM facts f LEFT JOIN clusters c ON c.id=f.cluster_id "
        f"WHERE f.domain IN ({marks}) AND f.user_state!='rejected' AND f.subject='self' "
        f"AND COALESCE(c.currency, 'unknown')!='past' AND f.kind IN ('state', 'concern', 'fact', 'goal', 'habit', 'constraint') "
        f"AND (f.user_state='confirmed' OR COALESCE(c.currency, '')='current' OR COALESCE(f.event_date, f.note_date, '')>=?) "
        f"ORDER BY f.user_state='confirmed' DESC, d DESC LIMIT 40", (*doms, str(int(today()[:4]) - 1))).fetchall()
    seen, out = set(), []
    for r in rows:
        text = re.sub(r"^(?:Автор|The author|Author)[:,]?\s+", "", r["claim"], flags=re.I).rstrip(".")
        key = text.casefold()[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{text}" + (f" ({r['d'][:7]})" if r["d"] else ""))
        if len(out) >= limit:
            break
    return out


_LINE = re.compile(r"^\s*(10|[1-9])(?:\s*[/,;]?\s*(10|[1-9]))?(?!\d)\s*(.*)$")


def parse(text: str) -> dict[str, Any] | None:
    """«6 9 да, сплю плохо» → устраивает 6, важно 9, хочу менять, заметка. None – не разобрал."""
    m = _LINE.match(text)
    if not m:
        return None
    rest = m.group(3).strip(" ,;.–-")
    change, note = "", rest
    first = re.match(r"([\wё?]+)", rest.casefold())
    if first and first.group(1) in CHANGE_WORDS:
        change = CHANGE_WORDS[first.group(1)]
        note = rest[first.end():].strip(" ,;.–-")
    return {"satisfaction": int(m.group(1)), "importance": int(m.group(2)) if m.group(2) else None,
            "change": change, "note": note[:400]}


def _save(conn: sqlite3.Connection, round_: int, area: str, a: dict[str, Any]) -> None:
    conn.execute("INSERT INTO lr_audit(round, area, satisfaction, importance, change, nature, note, created_at) "
                 "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(round, area) DO UPDATE SET satisfaction=excluded.satisfaction, "
                 "importance=excluded.importance, change=excluded.change, nature=excluded.nature, note=excluded.note",
                 (round_, area, a["satisfaction"], a["importance"], a["change"], a.get("nature", ""), a["note"], iso_now()))
    conn.commit()


def _ask(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def run(conn: sqlite3.Connection, areas: list[str] | None = None) -> int:
    """Пройти аудит (или продолжить незаконченный). Возвращает число отвеченных областей."""
    round_, cont = current_round(conn, fix=bool(areas))
    done = answers(conn, round_)
    base = baseline(conn, round_)
    todo = [a for a in LR_AREAS if (areas and a[0] in areas) or (not areas and a[0] not in done)]
    if not todo:
        print(trl("Этот раунд аудита пройден. Новый начнётся сам, если запустить audit через две недели и позже; исправить ответ – audit --areas health (ключи: ") + ", ".join(AREA_TITLES) + ").\n")
        return 0
    print((trl("Продолжаем аудит") if cont else trl("Аудит жизни, раунд {0}", round_)) + trl(": осталось областей – {0}.\n"
                                                                                                               "На каждую – одна строка: насколько устраивает сейчас (1-10), насколько важна (1-10) и хочешь ли менять (да / нет / может), можно с комментарием. Пример: «6 9 да, сплю плохо».\n"
                                                                                                               "Enter – пропустить область, «стоп» – закончить (ответы сохраняются сразу).\n", len(todo)))
    n = 0
    for key, title, desc, domains in todo:
        print(f"{title} – {desc}")
        for line in known(conn, domains):
            print(f"  · {line}")
        prev = base.get(key)
        if prev is not None and prev["satisfaction"] is not None:
            print(trl("  в первом аудите ({0}): устраивало {1}, важно {2}", prev['created_at'][:10], prev['satisfaction'], prev['importance'] if prev['importance'] is not None else '?'))
        while True:
            ans = _ask("> ")
            if ans is None or re.fullmatch(r"(стоп|хватит|/stop|q|stop|enough|quit|exit)", ans or "", re.I):
                print()
                return n
            if not ans:
                # Пропуск запоминается: иначе в ближайшие две недели та же область спрашивалась бы снова.
                _save(conn, round_, key, {"satisfaction": None, "importance": None, "change": "", "note": ""})
                print(trl("Пропустил."))
                break
            if guard.text_hold(ans):
                print(trl("Не записал: похоже на пароль или токен."))
                continue
            got = parse(ans)
            if got is None:
                print(trl("Начни с цифр: «6 9 да» – устраивает 6, важно 9, хочу менять."))
                continue
            if got["change"] == "yes":
                nat = _ask(trl("Это настоящая проблема – или скорее ощущение, что «так должно быть лучше»? (п – проблема, д – «должно быть», Enter – не знаю) "))
                got["nature"] = {"п": "problem", "д": "should", "p": "problem", "s": "should"}.get((nat or "").casefold()[:1], "")
            _save(conn, round_, key, got)
            n += 1
            break
        print()
    print(summary_text(conn, round_))
    return n


def baseline(conn: sqlite3.Connection, round_: int) -> dict[str, sqlite3.Row]:
    """Исходная точка: первый раунд (если текущий – не он)."""
    first = store.scalar(conn, "SELECT MIN(round) FROM lr_audit")
    if first is None or first == round_:
        return {}
    return answers(conn, first)


def latest_round(conn: sqlite3.Connection) -> int | None:
    return store.scalar(conn, "SELECT MAX(round) FROM lr_audit")


def focus_areas(rows: dict[str, sqlite3.Row]) -> list[str]:
    """Где действительно стоит работать: важно, не устраивает, хочется менять и это не «так должно быть»."""
    out = []
    for key, title, _d, _dom in LR_AREAS:
        r = rows.get(key)
        if r is None or r["satisfaction"] is None:
            continue
        if (r["importance"] or 0) >= 7 and r["satisfaction"] <= 5 and r["change"] == "yes" and r["nature"] != "should":
            out.append(key)
    return out


def summary_text(conn: sqlite3.Connection, round_: int | None = None) -> str:
    round_ = round_ or latest_round(conn)
    if not round_:
        return trl("Аудита ещё не было: python3 coach.py audit")
    rows = answers(conn, round_)
    base = baseline(conn, round_)
    answered = sum(1 for r in rows.values() if r["satisfaction"] is not None)
    skipped = len(rows) - answered
    lines = [trl("Аудит, раунд {0} – отвечено областей: {1} из {2}", round_, answered, len(LR_AREAS))
             + (trl(", пропущено: {0}.", skipped) if skipped else ".")]
    for key, title, _d, _dom in LR_AREAS:
        r = rows.get(key)
        if r is None or r["satisfaction"] is None:
            continue
        s = trl("  {0}: устраивает {1}", title, r['satisfaction']) + (trl(", важно {0}", r['importance']) if r["importance"] else "")
        if r["change"]:
            s += f", {CHANGE_TITLES[r['change']]}"
        if r["nature"]:
            s += f" ({NATURE_TITLES[r['nature']]})"
        b = base.get(key)
        if b is not None and b["satisfaction"] is not None and r["satisfaction"] is not None:
            delta = r["satisfaction"] - b["satisfaction"]
            if delta:
                s += trl("; с первого аудита {0}{1}", '+' if delta > 0 else '', delta)
        lines.append(s)
    focus = focus_areas(rows)
    if focus:
        lines.append(trl("Где стоит работать (важно, не устраивает, хочешь менять): ")
                     + ", ".join(AREA_TITLES[k] for k in focus) + ".")
    low_ok = [AREA_TITLES[k] for k, r in rows.items() if r["satisfaction"] is not None and r["satisfaction"] <= 5
              and (r["change"] == "no" or r["nature"] == "should")]
    if low_ok:
        lines.append(trl("Низкая оценка, но менять не хочешь или это «так должно быть» – не обязательно цель: ")
                     + ", ".join(low_ok) + ".")
    return "\n".join(lines) + "\n"


def coach_lines(conn: sqlite3.Connection) -> list[str]:
    round_ = latest_round(conn)
    if not round_:
        return []
    rows = answers(conn, round_)
    rows = {k: r for k, r in rows.items() if r["satisfaction"] is not None}
    if not rows:
        return []
    when = min(r["created_at"] for r in rows.values())[:10]
    out = [trl("Аудит жизни ({0}), его оценки «устраивает/важно»: ", when) + "; ".join(
        f"{AREA_TITLES[k]} {r['satisfaction']}/{r['importance'] or '?'}" + (trl(" – хочет менять") if r["change"] == "yes" else "")
        for k, r in rows.items() if r["satisfaction"] is not None)]
    focus = focus_areas(rows)
    if focus:
        out.append(trl("Области, где он сам хочет перемен: ") + ", ".join(AREA_TITLES[k] for k in focus))
    return out
