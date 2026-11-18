"""Мелкие общие функции: время, хэши, нормализация текста, даты, прогресс."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(obj: Any) -> str:
    return sha256_text(stable_json(obj))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def safe_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", text).strip("_").lower()
    return slug[:70] or "unknown"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# --- Нормализация для проверки цитат -------------------------------------

_MD_NOISE = re.compile(r"[*_`>#~\[\]|]+")
_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "’": "'", "‘": "'",
                         "—": "-", "–": "-", "ё": "е", "Ё": "Е", " ": " "})
_WORD = re.compile(r"[\wё]+", re.UNICODE)


def normalize_text(value: str) -> str:
    s = (value or "").translate(_QUOTES)
    s = _MD_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def words(value: str, min_len: int = 1) -> list[str]:
    return [w for w in _WORD.findall(normalize_text(value)) if len(w) >= min_len]


def quote_match(quote: str, source: str) -> str:
    """'exact' — цитата есть в тексте; 'partial' — почти все слова цитаты подряд
    встречаются в тексте (модель поправила пунктуацию или регистр); иначе 'none'."""
    q = normalize_text(quote)
    if len(q) < 8:
        return "none"
    src = normalize_text(source)
    if q in src:
        return "exact"
    qw = words(quote, 2)
    if len(qw) < 3:
        return "none"
    sw = words(source, 2)
    # Самый длинный участок источника, где слова цитаты идут в том же порядке.
    best = 0
    positions: dict[str, list[int]] = {}
    for i, w in enumerate(sw):
        positions.setdefault(w, []).append(i)
    for start in positions.get(qw[0], [])[:50]:
        j, hit = start, 0
        for w in qw:
            # Допускаем пропуск до двух слов источника между словами цитаты.
            found = next((p for p in positions.get(w, []) if j <= p <= j + 2), None)
            if found is not None:
                hit += 1
                j = found + 1
        best = max(best, hit)
    return "partial" if best / len(qw) >= 0.85 else "none"


# --- Даты ------------------------------------------------------------------

_Y = r"(19[5-9]\d|20[0-4]\d)"
_DATE_PATTERNS = (
    (re.compile(r"(?<!\d)" + _Y + r"[-._](0[1-9]|1[0-2])[-._](0[1-9]|[12]\d|3[01])(?!\d)"), ("y", "m", "d")),
    (re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])[-._](0[1-9]|1[0-2])[-._]" + _Y + r"(?!\d)"), ("d", "m", "y")),
    # Год-месяц — только через дефис или подчёркивание: «2000.12 руб» — не дата.
    (re.compile(r"(?<![\d.,])" + _Y + r"[-_](0[1-9]|1[0-2])(?![-._,]?\d)"), ("y", "m")),
)
_MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7,
    "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_RU_DATE = re.compile(r"(?<!\d)([12]?\d|3[01])\s+(январ|феврал|март|апрел|ма|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*\s+(20\d\d|19\d\d)", re.I)


def find_date(text: str) -> str | None:
    """Первая дата в строке в виде YYYY-MM-DD или YYYY-MM; None, если нет."""
    if not text:
        return None
    for pattern, order in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            parts = dict(zip(order, m.groups()))
            try:
                if "d" in parts:
                    return date(int(parts["y"]), int(parts["m"]), int(parts["d"])).isoformat()
                return f"{int(parts['y']):04d}-{int(parts['m']):02d}"
            except ValueError:
                continue  # 2023-02-30 и т. п. — пробуем следующее совпадение
    for m in _RU_DATE.finditer(text):
        try:
            return date(int(m.group(3)), _MONTHS_RU[m.group(2).lower()], int(m.group(1))).isoformat()
        except (ValueError, KeyError):
            continue
    return None


# Диапазон («2015–2016») — период, а не дата раздела; «2000–3000 об», «2000-2500» — числа, а не год;
# «2000-е» — десятилетие.
_YEAR_HEADING = re.compile(r"^\s*(?:год\s+|year\s+)?" + _Y + r"(?:\s*(?:год|г\.?|year))?\s*"
                           r"(?:$|[:.,)(]|[—–-](?!\s*\d)(?!\s*[ехЕХ]\b))", re.I)
# Число с единицей измерения или в диапазоне чисел — не год: «2000 оборотов», «2000–3000», «1990 руб».
_NOT_YEAR_AFTER = re.compile(r"\s*(?:[-–—]\s*\d|[ехЕХ]\b|-[ехЕХ]\b|"
                             r"(?:об|оборот|rpm|км|м\b|мм|см|кг|руб|р\.|₽|\$|€|usd|eur|л\.?\s?с|вт|квт|w\b|"
                             r"мач|mah|шт|%|гб|мб|gb|mb|тыс|hz|гц|л\b|литр|мл|грамм|ккал))", re.I)
_YEAR_BEFORE_RANGE = re.compile(r"\d\s*[-–—]\s*$")
_ANY_YEAR = re.compile(r"(?<![\d.,])" + _Y + r"(?![\d.,]\d)")


def heading_year(text: str) -> str | None:
    """Год из заголовка раздела: «2015», «2015 год», «2015 — переезд». Год внутри фразы
    («Цели на 2015») заголовок не датирует."""
    m = _YEAR_HEADING.match(text or "")
    return m.group(1) if m and int(m.group(1)) <= date.today().year else None


def year_in_text(text: str | None, year: str) -> bool:
    """Год упомянут как год, а не как число: «в 2000 году», «2000:» — да; «2000 оборотов»,
    «1500–2000», «2000-е» — нет. Двузначное «'00» тоже считается."""
    if not text:
        return False
    for m in re.finditer(r"(?<![\d.,])" + re.escape(year) + r"(?![\d.,]\d)", text):
        if _NOT_YEAR_AFTER.match(text, m.end()) or _YEAR_BEFORE_RANGE.search(text[max(0, m.start() - 12):m.start()]):
            continue
        return True
    return year[2:] in re.findall(r"'(\d\d)", text)


def title_year(title: str) -> str | None:
    """Единственный год в названии заметки («Дневник 2015», «Итоги 2019»); будущий год — план, не дата."""
    years = set(_ANY_YEAR.findall(title or ""))
    if len(years) == 1:
        y = years.pop()
        return y if int(y) <= date.today().year else None
    return None


def common_date(dates: list[str | None]) -> str | None:
    """Общая часть дат склеенных разделов: один день → день, один месяц → месяц, один год → год."""
    if not dates or any(not d for d in dates):
        return None
    for n in (10, 7, 4):
        if len({d[:n] for d in dates}) == 1 and all(len(d) >= n for d in dates):
            return dates[0][:n]
    return None


def normalize_time_ref(value: str) -> str:
    """Свести ответ модели к YYYY, YYYY-MM или YYYY-MM-DD; иначе пустая строка."""
    v = (value or "").strip()
    if not v:
        return ""
    found = find_date(v)
    if found:
        return found
    m = re.fullmatch(r"(19|20)\d\d", v)
    if m:
        return v
    # Период («2015–2017», «2015-2017 гг.») — датой служит начало; сам период остаётся в утверждении.
    m = re.fullmatch(r"((?:19|20)\d\d)\s*(?:г\.?|год)?\s*[-–—]\s*(?:19|20)\d\d\s*(?:гг?\.?|годы?)?", v)
    return m.group(1) if m else ""


def date_sort_key(value: str | None) -> str:
    return value or "0000"


# --- Прогресс ------------------------------------------------------------------


class Progress:
    """Строка прогресса с оценкой оставшегося времени.

    Если у шагов разный вес (например, длина фрагмента), оценка считается по весу:
    иначе первые крупные фрагменты завышают прогноз в разы."""

    def __init__(self, total: int, label: str, every: int = 1, total_weight: float = 0):
        self.total = max(total, 0)
        self.label = label
        self.every = max(every, 1)
        self.started = time.monotonic()
        self.done = 0
        self.total_weight = total_weight
        self.done_weight = 0.0

    def step(self, note: str = "", weight: float = 0) -> None:
        self.done += 1
        self.done_weight += weight
        if self.done == 1 or self.done % self.every == 0 or self.done == self.total:
            elapsed = time.monotonic() - self.started
            rate = elapsed / self.done if self.done else 0
            if self.total_weight and self.done_weight:
                left = elapsed / self.done_weight * max(self.total_weight - self.done_weight, 0)
            else:
                left = rate * (self.total - self.done)
            print(f"  {self.label} {self.done}/{self.total}  ~{fmt_duration(rate)}/шт, осталось ~{fmt_duration(left)}"
                  + (f"  {note}" if note else ""), flush=True)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60:02d} с"
    return f"{seconds // 3600} ч {(seconds % 3600) // 60:02d} мин"


def think_command(cfg, arg: str) -> str:
    """Разбор аргумента команды /think (общий для консольного чата и веба).

    Пусто, on/вкл/да/1 или off/выкл/нет/0 меняют cfg.think_chat и возвращают "set";
    любой другой текст — вопрос, на который нужно подумать один раз: возвращает "once",
    режим не меняется.
    """
    low = arg.casefold()
    if low in ("on", "вкл", "да", "1"):
        cfg.think_chat = True
    elif low in ("off", "выкл", "нет", "0"):
        cfg.think_chat = False
    elif not arg:
        cfg.think_chat = not cfg.think_chat
    else:
        return "once"
    return "set"


def warn(message: str) -> None:
    print(f"ПРЕДУПРЕЖДЕНИЕ: {message}", file=sys.stderr, flush=True)
