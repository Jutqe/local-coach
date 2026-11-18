"""Чек-ин: три коротких вопроса о том, что сейчас.

Главная беда памяти из заметок за годы — она плохо знает настоящее. Чек-ин раз в
несколько дней дешевле всего это чинит: ответы сразу становятся подтверждёнными
фактами с сегодняшней датой (значит, «сейчас»), а последний чек-ин коуч видит в
разговоре дословно.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import guard, store, vectors
from .config import Config
from .ollama import Ollama, OllamaError
from .prompts import CHECKIN_SCHEMA, CHECKIN_SYSTEM
from .util import dumps, iso_now, loads, today

QUESTIONS = (
    ("mood", "Как ты сейчас? Цифра от 1 до 10 и пара слов."),
    ("focus", "Что сейчас главное — чем в основном занят в эти дни?"),
    ("worry", "Что мешает или беспокоит? (Enter — ничего)"),
)
TITLES = {"mood": "как сейчас", "focus": "главное", "worry": "мешает"}
SHOW_DAYS = 14        # старше — в разговор не идёт
REMIND_DAYS = 3       # при старте чата напомнить, если чек-ина не было дольше
_MOOD = re.compile(r"(?<!\d)(?<!\d[.,])(10|[1-9])(?:\s*/\s*10)?(?!\d|[.,]\d)")


def last(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT 1").fetchone()


def days_since(row: sqlite3.Row | None) -> int | None:
    if row is None:
        return None
    from datetime import date
    return (date.fromisoformat(today()) - date.fromisoformat(row["created_at"][:10])).days


def _ago(days: int) -> str:
    return "сегодня" if days == 0 else "вчера" if days == 1 else f"{days} дн. назад"


def coach_block(conn: sqlite3.Connection) -> str:
    """Последний чек-ин — в системный промпт: это самое свежее, что человек сказал о себе."""
    row = last(conn)
    days = days_since(row)
    if row is None or days is None or days > SHOW_DAYS:
        return ""
    answers = loads(row["answers_json"], {}) or {}
    lines = [f"- {TITLES[k]}: {answers[k]}" for k, _q in QUESTIONS if answers.get(k)]
    if not lines:
        return ""
    return (f"ПОСЛЕДНИЙ ЧЕК-ИН ({row['created_at'][:10]}, {_ago(days)}) — его собственные слова о том, что сейчас; "
            "это свежее, чем заметки:\n" + "\n".join(lines))


def reminder(conn: sqlite3.Connection) -> str:
    days = days_since(last(conn))
    if days is None:
        return "Чек-ина ещё не было — три вопроса о том, что сейчас: /checkin"
    if days > REMIND_DAYS:
        return f"Последний чек-ин {_ago(days)}: /checkin"
    return ""


def _facts(llm: Ollama, answers: dict[str, str]) -> list[dict[str, Any]]:
    text = "\n".join(f"{TITLES[k].capitalize()}: {v}" for k, v in answers.items() if v)
    try:
        raw = llm.chat_json(CHECKIN_SYSTEM, f"Сегодня: {today()}.\n\nОтветы:\n{text}", CHECKIN_SCHEMA,
                            num_predict=600, attempts=2)
    except OllamaError:
        return []
    out = []
    for f in raw.get("facts") or []:
        claim = str(f.get("claim") or "").strip() if isinstance(f, dict) else ""
        if len(claim) < 6 or guard.text_hold(claim):
            continue
        if not claim.casefold().startswith("автор"):
            claim = f"Автор: {claim}"
        out.append({"claim": claim, "kind": f.get("kind") or "fact", "domain": f.get("domain") or "other"})
    return out


def run(conn: sqlite3.Connection, llm: Ollama, cfg: Config, session_id: int | None = None) -> dict[str, str] | None:
    """Задать вопросы, сохранить ответы и факты. None — если ничего не ответил."""
    from .chat import _add_confirmed_fact  # chat импортирует этот модуль

    print("Чек-ин: три коротких вопроса о том, что сейчас. Пустой ответ — пропустить вопрос.\n")
    answers: dict[str, str] = {}
    for key, question in QUESTIONS:
        try:
            ans = input(f"{question}\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if ans and guard.text_hold(ans):
            print("Не записал: похоже на пароль или токен.")
            ans = ""
        answers[key] = ans
    if not any(answers.values()):
        print("Ничего не записал.\n")
        return None
    m = _MOOD.search(answers.get("mood", ""))
    mood = int(m.group(1)) if m else None
    conn.execute("INSERT INTO checkins(mood, answers_json, created_at) VALUES(?, ?, ?)",
                 (mood, dumps({k: v for k, v in answers.items() if v}), iso_now()))
    conn.commit()
    facts = _facts(llm, answers)
    for f in facts:
        _add_confirmed_fact(conn, llm, f["claim"], f["kind"], f["domain"], session_id, today())
    if facts:
        vectors.update_clusters(conn, cfg, quiet=True)
    conn.commit()
    print()
    for f in facts:
        print(f"[Запомнил: {f['claim']}]")
    moods = [r["mood"] for r in conn.execute("SELECT mood FROM checkins WHERE mood IS NOT NULL "
                                             "ORDER BY id DESC LIMIT 7")][::-1]
    if len(moods) >= 2:
        print("Самочувствие по последним чек-инам: " + " → ".join(map(str, moods)))
    print("Записал. Коуч учтёт это в разговоре.\n")
    return answers


def count(conn: sqlite3.Connection) -> int:
    return store.scalar(conn, "SELECT COUNT(*) FROM checkins") or 0
