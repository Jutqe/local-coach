"""Импорт файлов вне vault (таблицы, документы) в память коуча.

Файл переводится в Markdown-заметку в папке `imports/` рабочей папки коуча и дальше
разбирается как обычная заметка. Vault не меняется. Поддерживаются .xlsx, .csv,
.docx, .txt, .md — только стандартная библиотека.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from .config import Config
from .util import atomic_write, today

SUPPORTED = (".xlsx", ".csv", ".docx", ".txt", ".md")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element) -> str:
    return "".join(t.text or "" for t in el.iter() if _local(t.tag) == "t")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


# --- xlsx -----------------------------------------------------------------------

_BUILTIN_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}


def _date_styles(z: zipfile.ZipFile) -> set[int]:
    """Номера стилей ячеек, которые отображаются как дата."""
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom: set[int] = set()
    for el in root.iter():
        if _local(el.tag) == "numFmt":
            code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", el.get("formatCode", "")).lower()
            if re.search(r"[dmy]", code) and not re.fullmatch(r"[0#.,%\s]*", code):
                custom.add(int(el.get("numFmtId", "0")))
    styles: set[int] = set()
    xfs = next((el for el in root.iter() if _local(el.tag) == "cellXfs"), None)
    if xfs is not None:
        for i, xf in enumerate(_children(xfs, "xf")):
            fmt = int(xf.get("numFmtId", "0"))
            if fmt in _BUILTIN_DATE_FORMATS or fmt in custom:
                styles.add(i)
    return styles


def _excel_date(serial: str) -> str:
    try:
        value = float(serial)
    except ValueError:
        return serial
    dt = datetime(1899, 12, 30) + timedelta(days=value)
    return dt.date().isoformat() if dt.time() == datetime.min.time() else dt.isoformat(sep=" ", timespec="minutes")


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in letters.group(0) if letters else "A":
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _number(value: str) -> str:
    try:
        f = float(value)
    except ValueError:
        return value
    return str(int(f)) if f.is_integer() else value


Sheet = tuple[str, list[list[str]], dict[tuple[int, int], tuple[int, int]]]


def _cell_ref(ref: str) -> tuple[int, int]:
    """«C12» → (12, 2): номер строки листа и индекс столбца."""
    m = re.match(r"[A-Z]+(\d+)", ref or "")
    return (int(m.group(1)) if m else 0), _col_index(ref)


def read_xlsx(path: Path) -> list[Sheet]:
    """Листы: (имя, строки, объединённые ячейки). Объединение — {(строка, столбец): (последняя строка,
    последний столбец)} в индексах списка строк: событие на несколько лет в таблице по годам."""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = [_text(si) for si in root if _local(si.tag) == "si"]
        date_styles = _date_styles(z)
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target", "") for r in rels}
        sheets: list[Sheet] = []
        for sh in (el for el in wb.iter() if _local(el.tag) == "sheet"):
            rid = next((v for k, v in sh.attrib.items() if _local(k) == "id"), None)
            target = targets.get(rid, "")
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            try:
                root = ET.fromstring(z.read(target))
            except KeyError:
                continue
            rows: list[list[str]] = []
            numbers: list[int] = []          # номер строки листа для каждой строки rows
            for n, row in enumerate((el for el in root.iter() if _local(el.tag) == "row"), 1):
                cells: dict[int, str] = {}
                for c in _children(row, "c"):
                    kind, style = c.get("t"), int(c.get("s", "0"))
                    v = next((x.text or "" for x in c if _local(x.tag) == "v"), "")
                    if kind == "s":
                        value = shared[int(v)] if v.isdigit() and int(v) < len(shared) else ""
                    elif kind == "inlineStr":
                        value = _text(c)
                    elif kind == "b":
                        value = "да" if v == "1" else "нет"
                    elif kind in ("str", "e"):
                        value = v
                    else:
                        value = _excel_date(v) if v and style in date_styles else _number(v)
                    if _clean(value):
                        cells[_col_index(c.get("r", ""))] = _clean(value)
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(i, "") for i in range(width)])
                    numbers.append(int(row.get("r") or n))
            merges: dict[tuple[int, int], tuple[int, int]] = {}
            for mc in (el for el in root.iter() if _local(el.tag) == "mergeCell"):
                a, _, b = (mc.get("ref") or "").partition(":")
                (r1, c1), (r2, c2) = _cell_ref(a), _cell_ref(b or a)
                if r1 not in numbers or (r1, c1) == (r2, c2):
                    continue
                i = numbers.index(r1)
                j = max(k for k, num in enumerate(numbers) if num <= r2)
                if c1 < len(rows[i]) and rows[i][c1]:
                    merges[(i, c1)] = (j, c2)
            sheets.append((sh.get("name", "Лист"), rows, merges))
        return sheets


def read_csv(path: Path) -> list[Sheet]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [[_clean(x) for x in r] for r in csv.reader(text.splitlines(), dialect)]
    return [(path.stem, [r for r in rows if any(r)], {})]


_YEARISH = re.compile(r"(19|20)\d\d(\s*(г\.?|год)?)?(\s*[-–—]\s*(19|20)\d\d(\s*(г\.?|год|гг\.?)?)?)?", re.I)


def _is_label(value: str) -> bool:
    """Годится в заголовок столбца: текст или год («2015», «2015–2017»), но не просто число."""
    return bool(value) and (bool(_YEARISH.fullmatch(value)) or not re.fullmatch(r"[\d.,\s-]+", value))


def _span(first: str, last: str, latest: int = 0) -> str:
    """«2015» … «2017» → «2015–2017». Диапазон до последнего года таблицы, если это нынешний или
    прошлый год, — «с 2015 по настоящее время»: иначе «2015–2026» читается как законченный период."""
    if first == last or not last:
        return first
    if _YEARISH.fullmatch(first) and _YEARISH.fullmatch(last):
        start, end = re.findall(r"[0-9]{4}", first)[0], int(re.findall(r"[0-9]{4}", last)[-1])
        if latest and end == latest and latest >= date.today().year - 1:
            return f"с {start} по настоящее время"
        return f"{start}–{end}"
    return f"{first} … {last}"


def _latest_year(values: list[str]) -> int:
    years = [int(y) for v in values if v and _YEARISH.fullmatch(v) for y in re.findall(r"[0-9]{4}", v)]
    return max(years, default=0)


def _header_row(rows: list[list[str]]) -> int | None:
    """Строка заголовков: первая из трёх верхних, заполненная хотя бы наполовину и только подписями.
    Строки над ней (название таблицы) идут в заметку как есть."""
    width = max((len(r) for r in rows), default=0)
    for i, r in enumerate(rows[:3]):
        filled = [v for v in r if v]
        if len(rows) > i + 1 and len(filled) >= max(2, width // 2) and all(_is_label(v) for v in filled):
            return i
    return None


_ONE_YEAR = re.compile(r"\s*((?:19|20)\d\d)\s*(?:г\.?|год)?\s*")


def _age_cells(header: list[str], r: list[str]) -> tuple[set[int], int | None]:
    """Столбцы строки «Возраст» по годам (0, 1, 2 … под заголовками-годами): у трёх и больше числовых ячеек
    разность «год минус число» одна и та же (у не менее 80%). Ячейки такой строки — не факты."""
    cells = [(c, int(m.group(1)) - int(v)) for c, v in enumerate(r)
             if v.strip().isdigit() and c < len(header) and (m := _ONE_YEAR.fullmatch(header[c] or ""))]
    if len(cells) < 3:
        return set(), None
    top, n = Counter(off for _c, off in cells).most_common(1)[0]
    return ({c for c, off in cells if off == top}, top) if n >= 0.8 * len(cells) else (set(), None)


def table_markdown(sheets: list[Sheet]) -> str:
    """Строка таблицы → строка заметки «Заголовок: значение; …» — так модели проще,
    чем Markdown-таблица, и каждая строка остаётся самодостаточной. Объединённая ячейка
    получает весь свой диапазон: «2015–2017: …» по столбцам, «(2015–2017)» по строкам."""
    out = []
    for name, rows, merges in sheets:
        if not rows:
            continue
        out.append(f"## {name}\n")
        h = _header_row(rows)
        header = rows[h] if h is not None else []
        for r in rows[:h or 0]:
            out.append(" ".join(v for v in r if v))
        start = h + 1 if h is not None else 0
        latest_col = _latest_year(header)
        latest_row = _latest_year([r[0] for r in rows[start:] if r])
        this_year = date.today().year
        for i in range(start, len(rows)):
            r, parts = rows[i], []
            ages, born = _age_cells(header, r)
            if ages:                       # вся строка «Возраст» — формула, не факты; остаётся год рождения
                out.append(f"<!-- birth: {born} -->")
                continue
            for c, v in enumerate(r):
                if not v:
                    continue
                if header and c < len(header) and (m := _ONE_YEAR.fullmatch(header[c] or "")) and int(m.group(1)) > this_year:
                    continue            # столбец будущего года (прогноз, план) — в память не идёт
                last_row, last_col = merges.get((i, c), (i, c))
                if last_row > i and r[0] and c > 0:
                    v += f" ({_span(r[0], rows[last_row][0] if rows[last_row] else '', latest_row)})"
                if header:
                    label = header[c] if c < len(header) and header[c] else f"Столбец {c + 1}"
                    if re.match(r"\s*(?:возраст|age)\b", label, re.I):
                        continue        # возраст в таблице считает формула: это не факт о человеке
                    if last_col > c:
                        label = _span(label, header[last_col] if last_col < len(header) else "", latest_col)
                    parts.append(f"{label}: {v}")
                else:
                    parts.append(v)
            if parts:
                out.append("- " + "; ".join(parts))
        out.append("")
    return "\n".join(out)


# --- docx ---------------------------------------------------------------------------

def read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = next((el for el in root if _local(el.tag) == "body"), root)
    out = []
    for el in body:
        name = _local(el.tag)
        if name == "p":
            text = _clean(_text(el))
            if not text:
                continue
            style = ""
            for s in el.iter():
                if _local(s.tag) == "pStyle":
                    style = next((v for k, v in s.attrib.items() if _local(k) == "val"), "")
                    break
            level = re.search(r"(\d)", style) if re.search(r"heading|заголовок", style, re.I) else None
            out.append(("#" * min(int(level.group(1)) + 1, 6) + " " if level else "") + text)
        elif name == "tbl":
            for tr in (x for x in el.iter() if _local(x.tag) == "tr"):
                cells = [_clean(_text(tc)) for tc in tr if _local(tc.tag) == "tc"]
                if any(cells):
                    out.append("- " + " | ".join(c for c in cells if c))
        out.append("")
    return "\n".join(out)


# --- общий вход ------------------------------------------------------------------------

def to_markdown(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".xlsx":
        body = table_markdown(read_xlsx(path))
    elif ext == ".csv":
        body = table_markdown(read_csv(path))
    elif ext == ".docx":
        body = read_docx(path)
    elif ext in (".txt", ".md"):
        body = path.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
    else:
        raise ValueError(f"формат {ext or 'без расширения'} не поддерживается; можно: {', '.join(SUPPORTED)}")
    if not body.strip():
        raise ValueError("в файле не нашлось текста")
    header = f"---\nsource: {path.name}\nimported: {today()}\n---\n# {path.stem}\n\n"
    return header + body.strip() + "\n"


def import_file(cfg: Config, path: Path) -> Path:
    """Сохранить файл как заметку в imports/. Повторный импорт того же файла заменяет её:
    изменённые строки переизвлекутся, неизменённые останутся как были."""
    if not path.is_file():
        raise FileNotFoundError(f"файл не найден: {path}")
    md = to_markdown(path)
    name = re.sub(r'[\\/:*?"<>|]+', "_", path.stem).strip() or "import"
    target = cfg.imports_dir / f"{name}{'' if path.suffix.lower() == '.md' else path.suffix.lower().replace('.', '_')}.md"
    atomic_write(target, md)
    return target
