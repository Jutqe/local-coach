"""Хронология из базы по годам — без модели: ровно то, что лежит в памяти, ничего не досочиняя.

`coach.py bio [1988-2026]` (прежнее имя `history` работает) и `/bio` в чате; просьбы вроде «выведи всё, что есть в базе, по годам»
в чате идут сюда же, мимо модели (модель видит только выборку поиска и честно говорит, что «больше нет»).

Схема (4.16). Сходство векторов одно событие от одной темы не отличает («купил Porsche» и «владел Porsche
2021–2026» — 0,88, «брат/сестра живёт в Москве» — тоже 0,88), поэтому хронология собирается по ролям:
1. Опора — строки импортированной биографии, прочитанные кодом: строка таблицы = одно событие со своим
   периодом. Факты, которые модель извлекла из этих строк (по 2–3 пересказа на строку), сюда не идут.
2. Из заметок и разговоров — только события и решения (не состояния, привычки, цели, суммы),
   и не из папок проектов и идей: там годы — планы и характеристики («2000 оборотов»), а не жизнь.
3. Событие из заметки, которое уже есть в биографии (год внутри периода строки и общий предмет —
   латинское название или число вроде «Porsche 911», либо два общих корня слов), не повторяется:
   строке биографии засчитывается упоминание. Так же сводятся заметки одного года об одном предмете.
`bio --all` — всё подряд, как лежит в базе: и проектное, и не-события, и пересказы биографии.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import date
from typing import Any

from .config import PRIVATE_DOMAINS
from .vault import IMPORT_PREFIX
from .vectors import dot, truncated, unpack

_PRIVATE_SQL = ",".join(f"'{d}'" for d in PRIVATE_DOMAINS) or "''"
# Похожий факт следующего года — продолжение того же, если он попадает в период события
# (или идёт сразу за последним упоминанием) и почти перефразировка (0,90, как склейка кластеров: «брат/сестра
# живёт в Москве» дают 0,88 — RESEARCH.md). Склейка кластеров при этом делит факты по областям жизни,
# а здесь области не важны и сравниваются только соседние годы.
SPAN_SIMILARITY = 0.90
_YEAR4 = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")
_TILL_NOW = re.compile(r"по\s+(?:настоящее|наст\.|сей\s+день|сегодня)|до\s+сих\s+пор|по\s+сейчас", re.I)
_YEARISH = re.compile(r"^\s*(?:с\s+)?((?:19|20)\d\d)(?:\s*(?:г\.?|год|гг\.?))?(?:\s*(?:[-–—]|по)\s*((?:19|20)\d\d|настоящее\s+время|н\.?\s*в\.?|сейчас))?"
                      r"(?:\s*(?:г\.?|год|гг\.?))?\s*$", re.I)
_NOW_WORD = re.compile(r"^\s*(?:по\s+)?(?:настоящее\s+время|н\.?\s*в\.?|сейчас|наст\.?\s*вр\.?|—|-)\s*$", re.I)
CHRONICLE_KINDS = {"event", "decision"}
_STOP = {"автор", "автора", "автору", "автором", "году", "годах", "годом", "период", "время", "настоящее",
         "который", "которая", "которые", "своей", "своего", "свою", "также", "этого", "около"}
# Столбец «Возраст» в таблице биографии проставляется формулой — это не событие и не факт.
AGE_LABEL = re.compile(r"^\s*(?:возраст|age)\b", re.I)
# Ячейка из одних чисел («100», «6 000», «12,5%») — ряд значений по годам (курс, возраст), а не событие.
_NUMBERISH = re.compile(r"^[\d\s.,%+\-–−]+$")
_BIRTH_MARK = re.compile(r"<!-- birth: (\d{4}) -->")
_RANGE = re.compile(r"((?:19|20)\d\d)(?:\s*(?:-|–|—|\.\.|по|до)\s*((?:19|20)\d\d))?")

# «выведи … по годам / из базы / хронологию / все факты / то, что у тебя есть». Просьба о теме
# («покажи, что есть в памяти про работу») сюда не попадает — её отвечает коуч по найденному.
_VERB = r"(?:выведи|выпиши|покажи|перечисли|распечатай|дай|вывести|показать)"
_WHAT = (r"(?:по\s+(?:тем\s+)?годам|из\s+базы|в\s+базе|хронологи\w*|все\s+факты|весь\s+список"
         r"|что\s+(?:у\s+тебя\s+|в\s+(?:базе|памяти)\s+)?есть\s*[.!]*\s*$)")
ASK = re.compile(rf"\b{_VERB}\b.{{0,80}}?{_WHAT}", re.I | re.S)


def parse_range(text: str) -> tuple[int | None, int | None]:
    """«1988-2026» → (1988, 2026); «2005» → (2005, 2005); без годов — (None, None)."""
    m = _RANGE.search(text or "")
    if not m:
        return None, None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else (a if not re.search(r"\b(?:с|от|начиная)\s+" + m.group(1), text) else None)
    return (min(a, b), max(a, b)) if b else (a, None)


def claim_span(claim: str) -> int | None:
    """Последний год периода из текста утверждения: «2010–2017» → 2017, «с 2023 по настоящее время» → нынешний."""
    years = [int(y) for y in _YEAR4.findall(claim or "")]
    if _TILL_NOW.search(claim or "") and years:
        return date.today().year
    return max(years) if len(set(years)) > 1 else None


def _source(r: sqlite3.Row) -> str:
    if r["origin"] in ("chat", "session"):
        return "разговор"
    return "биография" if (r["path"] or "").startswith(IMPORT_PREFIX) else "заметка"


def age_row_birth(by_label: list[tuple[int, int, str]]) -> int | None:
    """Год рождения, если строка — «Возраст» по годам в столбцах (0, 1, 2 …, пусть в первой ячейке «Старт»):
    у трёх и больше числовых ячеек разность «год минус число» одна и та же (у не менее 80%)."""
    offsets = [a - int(v) for a, b, v in by_label if a == b and v.isdigit()]
    if len(offsets) < 3:
        return None
    year, n = Counter(offsets).most_common(1)[0]
    return year if n >= 0.8 * len(offsets) else None


def _row_events(line: str, births: list[int] | None = None) -> list[dict[str, Any]]:
    """Строка таблицы биографии («- Год начала: 2010; Год окончания: 2017; Событие: …») → события.

    Ячейки-годы дают период строки; столбцы с годом в заголовке («2015–2017: …») — каждый своё событие;
    остальное — текст. Строка без годов — не хронология."""
    text = line.strip()[2:] if line.strip().startswith("- ") else ""
    if not text:
        return []
    years: list[int] = []
    now = False
    words: list[tuple[str, str]] = []
    by_label: list[tuple[int, int, str]] = []
    age: int | None = None
    for part in text.split("; "):
        label, _, value = part.partition(": ") if ": " in part else ("", "", part)
        label, value = label.strip(), value.strip()
        if AGE_LABEL.match(label):      # возраст в строке — только для подписи годов, в текст события не идёт
            age = int(value) if value.isdigit() else age
            continue
        m = _YEARISH.match(value)
        lm = _YEARISH.match(label) if label else None
        if m:
            years.append(int(m.group(1)))
            if m.group(2):
                years.append(int(m.group(2)) if m.group(2)[:1].isdigit() else date.today().year)
                now = now or not m.group(2)[:1].isdigit()
        elif years and _NOW_WORD.match(value):
            now = True
        elif lm and value:
            a = int(lm.group(1))
            b = (int(lm.group(2)) if lm.group(2)[:1].isdigit() else date.today().year) if lm.group(2) else a
            by_label.append((a, b, value))
        elif value:
            words.append((label, value))
    born = age_row_birth(by_label)
    if born is not None:                           # строка «Возраст» — не события, но даёт год рождения
        by_label = []
        if births is not None:
            births.append(born)
    by_label = [x for x in by_label if not _NUMBERISH.match(x[2])]
    body = words[0][1] if len(words) == 1 else "; ".join(f"{k}: {v}" if k else v for k, v in words)
    out = []
    if years and body:
        a, b = min(years), max(years) if not now else date.today().year
        out.append({"year": a, "till": b, "claim": body, "now": now, "age": age})
    for a, b, value in by_label:
        context = f"{words[0][1]}: " if words else ""
        out.append({"year": a, "till": b, "claim": context + value, "now": False, "age": None})
    return out


def birth_year(events: list[dict[str, Any]]) -> int | None:
    """Год рождения по столбцу «Возраст» таблицы: год строки минус возраст, самое частое значение.
    Строки с периодом не берём — к какому году относится возраст, неизвестно."""
    votes = Counter(e["year"] - e["age"] for e in events if e.get("age") is not None and e["year"] == e["till"])
    votes.update(e["birth"] for e in events if e.get("birth"))
    return votes.most_common(1)[0][0] if votes else None


def _years_old(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} год"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} года"
    return f"{n} лет"


_AUTHOR = re.compile(r"^Автор(?:а|у|ом|е)?\s+", re.I)


def _plain(claim: str) -> str:
    """«Автор переехал в Минск» → «Переехал в Минск»: факты записаны от третьего лица, в хронологии это шум."""
    out = _AUTHOR.sub("", claim, count=1)
    return out[:1].upper() + out[1:] if out != claim else claim


def _span_text(e: dict[str, Any]) -> str:
    if e.get("now"):
        return f"с {e['year']} по настоящее время"
    return f"{e['year']}–{e['till']}" if e["till"] != e["year"] else ""


def _keys(text: str) -> tuple[set[str], set[str]]:
    """Предмет утверждения: латинские слова и числа, кроме годов («porsche», «911»), и корни слов от пяти букв."""
    marks = {w.casefold() for w in re.findall(r"[A-Za-z][A-Za-z0-9-]*|(?<!\d)\d{3,}(?!\d)", text or "")
             if not _YEAR4.fullmatch(w) and len(w) >= 2}
    stems = {w.casefold()[:5] for w in re.findall(r"[А-Яа-яЁё]{5,}", text or "") if w.casefold() not in _STOP}
    return marks, stems


def _same_subject(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return bool(a["marks"] & b["marks"]) or len(a["stems"] & b["stems"]) >= 2


def biography(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], set[str], list[int]]:
    """События из строк импортированных таблиц, пути, где такие строки нашлись, и годы рождения из строк «Возраст».
    Будущее (после нынешнего года — прогнозы, плановые значения) не берётся."""
    events, paths, seen, births = [], set(), set(), []
    this_year = date.today().year
    for r in conn.execute("SELECT c.path, c.body FROM chunks c JOIN notes n ON n.path=c.path "
                          "WHERE c.path LIKE ? AND n.status='indexed' ORDER BY c.path, c.idx", (IMPORT_PREFIX + "%",)):
        births.extend(int(y) for y in _BIRTH_MARK.findall(r["body"]))     # метка импорта вместо строки «Возраст»
        for line in r["body"].splitlines():
            for e in _row_events(line, births):
                if e["year"] > this_year:
                    continue
                e["till"] = min(e["till"], this_year)
                key = (e["year"], e["till"], e["claim"])
                if key in seen:          # соседние фрагменты перекрываются
                    continue
                seen.add(key)
                paths.add(r["path"])
                marks, stems = _keys(e["claim"])
                events.append({**e, "date": str(e["year"]), "last": e["till"], "source": "биография", "n": 1,
                               "notes": 0, "marks": marks, "stems": stems, "vec": None})
    return events, paths, births


def entries(conn: sqlite3.Connection, start: int | None = None, end: int | None = None,
            full: bool = False) -> dict[str, Any]:
    """Хронология по годам (схема — в начале модуля); full — всё подряд, как в базе."""
    bio, bio_paths, births = biography(conn)
    if full:
        bio, bio_paths = [], set()
    this_year = date.today().year
    rows = conn.execute(
        "SELECT id, claim, kind, event_date, origin, path, cluster_id, embedding, is_project FROM facts "
        f"WHERE user_state!='rejected' AND subject!='general' AND domain NOT IN ({_PRIVATE_SQL}) "
        "AND date_reliable=1 AND event_date IS NOT NULL AND event_date != '' "
        "ORDER BY event_date, id").fetchall()
    rank = {"биография": 0, "разговор": 1, "заметка": 2}
    hidden = {"project": 0, "kind": 0, "bio_retold": 0, "covered": 0, "grouped": 0}
    in_range = lambda y: not ((start and y < start) or (end and y > end))   # noqa: E731
    items: dict[Any, dict[str, Any]] = {}
    lo, hi = start or 0, end or 9999
    # Строка биографии, чей период задевает запрошенные годы, стоит в первом из них («2010–2017» при «с 2015» — в 2015).
    shown: list[dict[str, Any]] = [{**e, "show": max(e["year"], lo)} for e in bio if e["till"] >= lo and e["year"] <= hi]
    bio = [e for e in shown] + [e for e in bio if not (e["till"] >= lo and e["year"] <= hi)]
    active: list[dict[str, Any]] = []
    seen_years: set[int] = {y for e in shown for y in range(max(e["year"], lo), min(e["till"], hi) + 1)}
    total = 0
    for r in rows:
        try:
            year = int(r["event_date"][:4])
        except ValueError:
            continue
        if not in_range(year) or year > this_year:       # будущее («планирую в 2027») — не хронология
            continue
        total += 1
        if not full:
            if r["path"] in bio_paths:
                hidden["bio_retold"] += 1
                continue
            if r["is_project"]:
                hidden["project"] += 1
                continue
            if r["kind"] not in CHRONICLE_KINDS:
                hidden["kind"] += 1
                continue
        seen_years.add(year)
        active = [a for a in active if a["till"] + 1 >= year]
        key = r["cluster_id"] if r["cluster_id"] is not None else f"f{r['id']}"
        src = _source(r)
        vec = unpack(r["embedding"])
        vec = truncated(vec) if vec is not None else None
        till = claim_span(r["claim"])
        marks, stems = _keys(r["claim"])
        it = items.get(key)
        if it is None and vec is not None:
            it = max((a for a in active if a["year"] < year <= a["till"] + 1 and a["vec"] is not None),
                     key=lambda a: dot(a["vec"], vec), default=None)
            if it is not None and dot(it["vec"], vec) < SPAN_SIMILARITY:
                it = None
            if it is not None:
                items[key] = it
        if it is None and not full:
            me = {"marks": marks, "stems": stems}
            cover = next((e for e in bio if e["year"] <= year <= e["till"] and _same_subject(e, me)), None)
            if cover is not None:
                cover["notes"] += 1
                hidden["covered"] += 1
                continue
            it = next((e for e in shown if e["source"] != "биография" and e["year"] == year and _same_subject(e, me)),
                      None)
            if it is not None:
                items[key] = it
                hidden["grouped"] += 1
        if it is None:
            it = items[key] = {"date": r["event_date"], "year": year, "last": year, "till": max(year, till or 0),
                               "claim": r["claim"], "source": src, "n": 1, "notes": 0, "vec": vec,
                               "marks": marks, "stems": stems,
                               "project": bool(r["is_project"])}
            active.append(it)
            shown.append(it)
            continue
        it["n"] += 1
        it["last"] = max(it["last"], year)
        it["till"] = max(it["till"], year, till or 0)
        if rank[src] < rank[it["source"]] and year == it["year"]:
            it.update(claim=r["claim"], source=src, date=r["event_date"])
    undated = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE user_state!='rejected' AND subject!='general' "
        f"AND domain NOT IN ({_PRIVATE_SQL}) AND (date_reliable=0 OR event_date IS NULL OR event_date='')").fetchone()[0]
    by_year: dict[int, list[dict[str, Any]]] = {}
    for it in shown:
        by_year.setdefault(it.get("show", it["year"]), []).append(it)
    # В году сначала события именно этого года, потом те, у которых указан период (он начался в этом году).
    by_year = {y: sorted(v, key=lambda e: (bool(e.get("now")) or e["till"] > e["year"],
                                           rank[e["source"]] if e["source"] == "биография" else 1, e["date"]))
               for y, v in sorted(by_year.items())}
    return {"years": by_year, "seen": seen_years, "total": total, "undated": undated, "start": start, "end": end,
            "hidden": hidden, "full": full, "bio": len(bio), "birth": birth_year(bio + [{"birth": b} for b in births])}


def _rows(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} строка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} строки"
    return f"{n} строк"


def _facts(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} факт"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} факта"
    return f"{n} фактов"


def _when(value: str) -> str:
    parts = value.split("-")
    if len(parts) >= 3:
        return f"{parts[2][:2]}.{parts[1]}"
    if len(parts) == 2:
        return parts[1]
    return ""


def _spans(years: list[int]) -> str:
    out, i = [], 0
    while i < len(years):
        j = i
        while j + 1 < len(years) and years[j + 1] == years[j] + 1:
            j += 1
        out.append(str(years[i]) if i == j else f"{years[i]}–{years[j]}")
        i = j + 1
    return ", ".join(out)


def render(data: dict[str, Any]) -> str:
    years, seen, hidden = data["years"], data["seen"], data["hidden"]
    if not years:
        span = f" за {data['start']}–{data['end'] or date.today().year}" if data["start"] else ""
        return f"В базе нет событий с датой{span}. Без надёжной даты: {_facts(data['undated'])}."
    lines = []
    for year, items in years.items():
        old = f" — {_years_old(year - data['birth'])}" if data.get("birth") and year >= data["birth"] else ""
        lines.append(f"{year}{old}")
        for e in items:
            when = _when(e["date"]) if e["source"] != "биография" else ""
            span = _span_text(e) if e["source"] == "биография" else ""
            more = [e["source"]]
            if e.get("project"):
                more.append("проект")
            if e["n"] > 1:
                more.append(f"упоминаний: {e['n']}" + (f", до {e['last']}" if e["last"] != e["year"] else ""))
            if e.get("notes"):
                more.append(f"и в заметках: {e['notes']}")
            lead = " ".join(x for x in (when, span + ":" if span else "") if x)
            lines.append(f"  - {lead + ' ' if lead else ''}{_plain(e['claim'])} ({', '.join(more)})")
    first = data["start"] or min(seen)
    last = data["end"] or max(seen)
    empty = [y for y in range(first, last + 1) if y not in seen]
    shown = sum(len(v) for v in years.values())
    lines.append("")
    if data["full"]:
        lines.append(f"Всё, что есть в базе с датой за {first}–{last}: {_rows(shown)} из {_facts(data['total'])} "
                     "(похожие сведены в одну строку, в год первого упоминания).")
    else:
        lines.append(f"Хронология за {first}–{last}: {_rows(shown)} — строки биографии ({data['bio']}) "
                     "и события из заметок и разговоров, без повторов.")
        skipped = [(hidden["covered"], "уже есть в биографии"), (hidden["grouped"], "о том же в том же году"),
                   (hidden["bio_retold"], "пересказы строк биографии"), (hidden["project"], "из папок проектов и идей"),
                   (hidden["kind"], "не события: состояния, привычки, цели, суммы")]
        skipped = [f"{_facts(n)} — {why}" for n, why in skipped if n]
        if skipped:
            lines.append("Не показаны: " + "; ".join(skipped) + ". Всё подряд: bio --all (в чате /bio --all).")
    if empty:
        lines.append(f"Пустые годы: {_spans(empty)}.")
    lines.append(f"Без надёжной даты: {_facts(data['undated'])} — в списке их нет.")
    return "\n".join(lines)


def show(conn: sqlite3.Connection, text: str = "", full: bool = False) -> str:
    full = full or bool(re.search(r"(?:^|\s)(?:--all|-a|полностью|полный|без\s+сокращений)(?:\s|$)", text or "", re.I))
    start, end = parse_range(text)
    return render(entries(conn, start, end, full))
