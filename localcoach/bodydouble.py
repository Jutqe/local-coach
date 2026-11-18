"""Режим присутствия (body double): коуч «сидит рядом», пока человек работает.

Сам, без запроса, раз в 1–2 минуты пишет короткую реплику; каждые 10 минут — сколько
прошло. Реплики готовятся одним вызовом модели при включении (под задачу), дальше
модель не нужна — ничего не тормозит и не зависит от сети. Пишет только когда человек
ничего не набирает: строку ввода не ломает.
"""
from __future__ import annotations

import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from . import guard
from .config import Config
from .ollama import Ollama, OllamaError
from .prompts import FOCUS_SCHEMA, FOCUS_SYSTEM
from .util import iso_now

# Запасные реплики: если модель недоступна или ответила не по делу.
FALLBACK = ("Я здесь.", "Работаем.", "Тут.", "Продолжай.", "Рядом.", "Идёт.", "Я на месте.", "Дальше.",
            "Шаг за шагом.", "Держим темп.", "Спокойно.", "Я с тобой.", "Не спешим.", "Ты в процессе.",
            "Ещё немного.", "Я рядом.")

# Коуч не видит экрана: реплики, будто он оценивает ход работы, отсеиваются.
PRETEND = re.compile(r"(?i)прогресс|видн|вижу|виден|по плану|хорош|молодец|отличн|\bок\b")

MARK_EVERY_MIN = 10
RECENT = 6

# «побудь рядом», «посиди со мной», «body double» — включить режим словами.
START = re.compile(r"(?i)\b(?:body[\s-]?double|бади[\s-]?дабл|режим присутствия|"
                   r"(?:побудь|посиди|поприсутствуй)\s+(?:рядом|со\s+мной|тут|здесь))")
# Короткая реплика целиком — «стоп», «закончил», «готово» — выключить.
STOP = re.compile(r"(?i)^\s*(?:стоп|хватит|готово|всё|все|закончил\w*|заканчиваю|конец|/stop)\s*[.!]*\s*$")


# «/focus LR-12 на 50 минут отчёт»: пункт реестра Life Reset и план по времени – по желанию.
ITEM = re.compile(r"(?i)(?<![\w-])LR-(\d+)(?!\w)")
PLAN_MIN = re.compile(r"(?i)(?<!\w)(?:на\s+)?(\d{1,3})\s*(?:м|мин|минут[аы]?|min)(?!\w)")
PLAN_HOUR = re.compile(r"(?i)(?<!\w)(?:(?:на\s+)?(\d)\s*(?:ч|час|часа|часов)|на\s+(час)|на\s+(полчаса)|на\s+(полтора)\s+часа)(?!\w)")
PLAN_MAX = 240
OUTCOME_WAIT = 30 * 60      # столько секунд после конца сессии ответ «как прошло» записывается к ней


def parse_start(conn: sqlite3.Connection | None, text: str) -> tuple[str, int | None, str, int | None]:
    """Из «LR-12 на 50 минут черновик» – (задача, id пункта, его название, минут по плану).
    Несуществующий пункт остаётся в тексте как есть: пусть видно, что номер не нашёлся."""
    item_id, title = None, ""
    m = ITEM.search(text)
    if m and conn is not None:
        row = conn.execute("SELECT id, title FROM lr_items WHERE id=? AND merged_into IS NULL AND status!='dismissed'",
                           (int(m.group(1)),)).fetchone()
        if row:
            item_id, title = row["id"], row["title"]
            text = text[:m.start()] + text[m.end():]
    planned = None
    m = PLAN_MIN.search(text)
    if m and 1 <= int(m.group(1)) <= PLAN_MAX:
        planned = int(m.group(1))
    else:
        m = PLAN_HOUR.search(text)
        if m:
            planned = int(m.group(1)) * 60 if m.group(1) else 60 if m.group(2) else 30 if m.group(3) else 90
            if not 1 <= planned <= PLAN_MAX:
                planned, m = None, None
    if planned and m:
        text = text[:m.start()] + text[m.end():]
    return re.sub(r"\s+", " ", text).strip(" ,.;:—–-!"), item_id, title, planned


def start_text(planned: int | None) -> str:
    return ("Начали" + (f", на {_minutes(planned)}" if planned else "") + ". Я рядом – буду иногда коротко писать. "
            + ("Когда время выйдет, позову. " if planned else "") + "Закончишь раньше – «стоп».")


def _minutes(n: int) -> str:
    if n >= 60:
        h, m = divmod(n, 60)
        return f"{h} ч" + (f" {m} мин" if m else "")
    tail = "минута" if n % 10 == 1 and n % 100 != 11 else (
        "минуты" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "минут")
    return f"{n} {tail}"


def _clean(lines: object) -> list[str]:
    out = []
    for raw in lines if isinstance(lines, list) else []:
        s = str(raw).strip().strip("«»\"' ")
        if not s or "?" in s or len(s) > 40 or len(s.split()) > 5 or PRETEND.search(s) or guard.text_hold(s):
            continue
        out.append(s if s[-1] in ".…!" else s + ".")
    return list(dict.fromkeys(out))


class BodyDouble:
    def __init__(self, llm: Ollama | None, cfg: Config, task: str, say: Callable[[str], None],
                 can_speak: Callable[[], bool], every: tuple[float, float] | None = None,
                 item_id: int | None = None, item_title: str = "", planned: int | None = None,
                 on_plan: Callable[[], None] | None = None):
        self.llm, self.cfg, self.say, self.can_speak = llm, cfg, say, can_speak
        self.task = task.strip() or item_title.strip()
        self.typed = self.task      # как человек сам написал: это показывается над котом, а task модель может сократить
        self.item_id, self.item_title = item_id, item_title
        self.planned = planned      # минут по плану или None
        self.on_plan = on_plan      # время вышло: веб сам заканчивает сессию; без него – одна реплика, и дальше как было
        self.plan_reached = False
        self.row_id: int | None = None
        hi = max(10.0, float(cfg.focus_every))
        self.every = every or (hi / 2, hi)
        self.poll = min(5.0, self.every[0])   # как часто проверять, не перестал ли человек набирать
        self.pool: list[str] = list(FALLBACK)
        self.recent: list[str] = []
        self.started = time.monotonic()
        self.started_at = iso_now()
        self.marks = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def elapsed_min(self) -> int:
        return int((time.monotonic() - self.started) // 60)

    def elapsed_sec(self) -> int:
        return int(time.monotonic() - self.started)

    def plan_left(self) -> float | None:
        """Секунд до конца плана; None – плана нет или он уже позади."""
        if not self.planned or self.plan_reached:
            return None
        return self.planned * 60 - (time.monotonic() - self.started)

    def _prepare(self) -> None:
        if not self.llm:
            return
        try:
            raw = self.llm.chat_json(FOCUS_SYSTEM, f"Задача: {self.task or 'не названа'}", FOCUS_SCHEMA,
                                     num_predict=500, attempts=1, timeout=60)
        except (OllamaError, OSError):
            return
        own = _clean(raw.get("lines"))
        if len(own) >= 5:
            # Свои реплики под задачу — чаще, запасные — для разнообразия.
            self.pool = own * 2 + list(FALLBACK)
        title = str(raw.get("task") or "").strip()
        if title and len(title) <= 60 and not guard.text_hold(title):
            self.task = title

    def next_line(self) -> str:
        minutes = self.elapsed_min()
        if minutes // MARK_EVERY_MIN > self.marks:
            self.marks = minutes // MARK_EVERY_MIN
            return f"{_minutes(self.marks * MARK_EVERY_MIN)}."
        choices = [s for s in self.pool if s not in self.recent] or self.pool
        line = random.choice(choices)
        self.recent = (self.recent + [line])[-RECENT:]
        return line

    def _run(self) -> None:
        self._prepare()
        while True:
            wait, left = random.uniform(*self.every), self.plan_left()
            if left is not None:
                wait = max(0.0, min(wait, left))
            if self._stop.wait(wait):
                return
            left = self.plan_left()
            if left is not None and left <= 0.5:
                self.plan_reached = True
                if self.on_plan:
                    self.on_plan()      # дальше сессию закрывает веб, в своём потоке
                    return
                line = f"{_minutes(self.planned).capitalize()} – столько ты наметил. Можно продолжать; закончишь – «стоп»."
            elif left is not None and left < self.every[0] / 2:
                continue                # конец плана почти сейчас: реплику не вставлять перед ним
            else:
                line = ""
            # Человек набирает или коуч отвечает — подождать и попробовать чуть позже.
            while not self.can_speak():
                if self._stop.wait(self.poll):
                    return
            self.say(line or self.next_line())

    def stop(self) -> int:
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)
        return self.elapsed_min()


def record(conn: sqlite3.Connection, session_id: int | None, bd: BodyDouble) -> int:
    minutes = bd.elapsed_min()
    cur = conn.execute("INSERT INTO focus_sessions(session_id, task, started_at, ended_at, minutes, item_id, planned) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (session_id, bd.task, bd.started_at, iso_now(), minutes, bd.item_id, bd.planned))
    bd.row_id = cur.lastrowid
    conn.commit()
    return minutes


def end_text(bd: BodyDouble, minutes: int) -> str:
    """Что сказать в конце. После хотя бы минуты работы – спросить, как прошло: ответ запишется к сессии."""
    if bd.plan_reached and bd.planned:
        head = f"{_minutes(bd.planned).capitalize()} – как и наметил."
    else:
        head = "Закончили. " + (f"{_minutes(minutes).capitalize()} работы." if minutes else "Меньше минуты.")
    return head + (" Как прошло? Одной фразой – запишу к этой сессии." if minutes else "")


def save_outcome(conn: sqlite3.Connection, row_id: int, text: str) -> None:
    conn.execute("UPDATE focus_sessions SET outcome=? WHERE id=?", (text.strip()[:500], row_id))
    conn.commit()


def outcome_reply(text: str) -> bool:
    """Ответ на «как прошло?» – обычная фраза, не команда и не новый старт фокуса."""
    return bool(text) and not text.startswith("/") and not START.search(text) and not guard.text_hold(text)


def item_minutes(conn: sqlite3.Connection, days: int = 30) -> dict[int, int]:
    """Минут фокуса по пунктам реестра за последние days дней."""
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    return {r[0]: r[1] for r in conn.execute(
        "SELECT item_id, SUM(minutes) FROM focus_sessions WHERE item_id IS NOT NULL AND started_at >= ? "
        "GROUP BY item_id", (since,))}
