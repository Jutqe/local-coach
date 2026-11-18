"""Люди: кто упоминается в заметках, кем приходится, есть ли сейчас в жизни.

Имена в заметках за годы — частый источник путаницы: одна «Персона» в 2015 и другая в
2024, коллега, который давно уже не коллега. Здесь люди становятся отдельными
записями: модель находит их в фактах (в именительном падеже), код собирает
упоминания и даты, а человек отвечает, кто это и есть ли он сейчас в его жизни.
Ответы хранятся по имени и переживают повторное извлечение.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from . import guard, store
from .config import Config
from .ollama import Ollama, OllamaError
from .prompts import PEOPLE_SCHEMA, PEOPLE_SYSTEM
from .util import atomic_write, digest, iso_now, normalize_time_ref
from .i18n import ru_key, trl

BATCH = 30
ASK_MIN_MENTIONS = 3
BLOCK_LIMIT = 8
STATUS_TITLES = {"current": trl("сейчас в твоей жизни"), "past": trl("в прошлом"), "unknown": "", "ignored": ""}

# Предварительный отбор: без заглавного слова (кроме «Автор») и без слова-роли модель не спрашиваем.
_CAPITAL = re.compile(r"(?<![.!?]\s)(?<!^)\b[А-ЯЁA-Z][а-яёa-z]{2,}")
_ROLE = re.compile(r"(?i)\b(?:мам|пап|мать|матер|отец|отц|брат|сестр|жен[аыуеой]\b|муж|сын|доч|бабушк|дедушк|"
                   r"друг|друз|подруг|коллег|начальни|руководител|партн[её]р|девушк|парн[еяю]|т[её]щ|свекр|"
                   r"племянни|т[её]т|дяд|сосед|врач|психолог|терапевт|тренер|учител|наставни|реб[её]н|дет[иейям])|"
                   r"\b(?:mom|mum|mother|dad|father|brother|sister|wife|husband|son|daughter|grandma|grandpa|grandmother|"
                   r"grandfather|friend|colleague|coworker|boss|manager|partner|girlfriend|boyfriend|mother-in-law|"
                   r"father-in-law|nephew|niece|uncle|aunt|cousin|neighbou?r|doctor|therapist|psychologist|coach|teacher|"
                   r"mentor|child|children|kids?)\b")


def _fold(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _stem(name: str) -> str:
    """Основа для падежей: «Персона» → «персон» (Персоны, Персоной), «Имярек» → «имярек» (Имяреку; согласный на конце не отбрасывается)."""
    first = _fold(name.split()[0]) if name.split() else ""
    return first[:-1] if len(first) >= 3 and first[-1] in "аяоеиыуюйь" else first


def _mentions_name(name: str, text: str) -> bool:
    stem = _stem(name)
    if len(stem) < 2:
        return False
    # Короткая основа («ол») — только для слов не длиннее самого имени с окончанием.
    limit = len(_fold(name.split()[0])) + 3 if len(stem) < 3 else 99
    return any(w.startswith(stem) and len(w) <= limit for w in re.findall(r"[\wё]+", _fold(text)))


def _maybe_people(claim: str) -> bool:
    body = re.sub(r"^\s*(?:Автор\w*|The author|Author)[:\s]*", "", claim)
    return bool(_ROLE.search(body) or any(m.group(0) not in ("Автор", "Author") for m in _CAPITAL.finditer(" " + body)))


def _canonical(conn: sqlite3.Connection, pid: int) -> int:
    seen = set()
    while pid not in seen:
        seen.add(pid)
        nxt = store.scalar(conn, "SELECT merged_into FROM people WHERE id=?", (pid,))
        if not nxt:
            return pid
        pid = int(nxt)
    return pid


def _person(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM people WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return _canonical(conn, row["id"])
    now = iso_now()
    cur = conn.execute("INSERT INTO people(name, created_at, updated_at) VALUES(?, ?, ?)", (name, now, now))
    return int(cur.lastrowid)


def refresh_stats(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM fact_people WHERE fact_id NOT IN (SELECT id FROM facts)")
    conn.execute("""
        UPDATE people SET
          mentions = (SELECT COUNT(DISTINCT fp.fact_id) FROM fact_people fp JOIN facts f ON f.id = fp.fact_id
                      WHERE fp.person_id = people.id AND f.user_state != 'rejected'),
          first_date = (SELECT MIN(f.event_date) FROM fact_people fp JOIN facts f ON f.id = fp.fact_id
                        WHERE fp.person_id = people.id AND f.user_state != 'rejected'),
          last_date = (SELECT MAX(f.event_date) FROM fact_people fp JOIN facts f ON f.id = fp.fact_id
                       WHERE fp.person_id = people.id AND f.user_state != 'rejected')""")


def update(conn: sqlite3.Connection, llm: Ollama | None, cfg: Config, quiet: bool = False) -> dict[str, int]:
    """Найти людей в ещё не просмотренных фактах. Возвращает {'facts': просмотрено, 'linked': связей}."""
    rows = conn.execute("SELECT id, claim, event_date FROM facts WHERE people_scanned=0 AND user_state!='rejected' "
                        "ORDER BY id").fetchall()
    todo = []
    for r in rows:
        if _maybe_people(r["claim"]):
            todo.append(r)
        else:
            conn.execute("UPDATE facts SET people_scanned=1 WHERE id=?", (r["id"],))
    stats = {"facts": len(rows), "linked": 0}
    if llm is None:
        conn.commit()
        refresh_stats(conn)
        conn.commit()
        return stats
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        if not quiet:
            print(trl("  люди: {0}/{1}", min(start + BATCH, len(todo)), len(todo)), flush=True)
        ids = {f"F{r['id']}": r for r in batch}
        text = "\n".join(f"F{r['id']} ({(r['event_date'] or '')[:7] or 'без даты'}): {r['claim']}" for r in batch)
        try:
            raw = llm.chat_json(PEOPLE_SYSTEM, text, PEOPLE_SCHEMA, num_predict=2500, attempts=2, heavy=True)
        except OllamaError:
            continue   # этот пакет повторится при следующем обновлении
        latest_relation: dict[int, tuple[str, str]] = {}
        for item in raw.get("items") or []:
            fact = ids.get(str(item.get("id") or "").strip().upper()) if isinstance(item, dict) else None
            if fact is None:
                continue
            for p in item.get("people") or []:
                if not isinstance(p, dict):
                    continue
                name = re.sub(r"\s+", " ", str(p.get("name") or "")).strip(" .,«»\"'")
                if not 2 <= len(name) <= 40 or _fold(name).startswith(("автор", "author", "the author")) or guard.text_hold(name):
                    continue
                # Имя должно быть в тексте утверждения — иначе модель его придумала.
                if not _mentions_name(name, fact["claim"]):
                    continue
                pid = _person(conn, name)
                if store.scalar(conn, "SELECT status FROM people WHERE id=?", (pid,)) == "ignored":
                    continue
                conn.execute("INSERT OR IGNORE INTO fact_people(fact_id, person_id) VALUES(?, ?)", (fact["id"], pid))
                stats["linked"] += 1
                rel = str(p.get("relation") or "").strip()[:40]
                if not rel and name == name.lower():
                    rel = name   # «мама», «начальник»: роль и есть «кто это»
                when = fact["event_date"] or ""
                if rel and (pid not in latest_relation or when >= latest_relation[pid][0]):
                    latest_relation[pid] = (when, rel)
        for pid, (_when, rel) in latest_relation.items():
            conn.execute("UPDATE people SET relation=?, relation_src='model', updated_at=? "
                         "WHERE id=? AND relation_src!='user'", (rel, iso_now(), pid))
        conn.executemany("UPDATE facts SET people_scanned=1 WHERE id=?", [(r["id"],) for r in batch])
        conn.commit()
    refresh_stats(conn)
    conn.commit()
    return stats


def _years(row: sqlite3.Row) -> str:
    a, b = (row["first_date"] or "")[:4], (row["last_date"] or "")[:4]
    return f"{a}–{b}" if a and b and a != b else a or b


def _line(row: sqlite3.Row) -> str:
    parts = [row["relation"] + (trl(" (сказал ты)") if row["relation_src"] == "user" else "")] if row["relation"] else []
    if row["mentions"]:
        parts.append(trl("упоминаний {0}", row['mentions']) + (f", {_years(row)}" if _years(row) else ""))
    status = STATUS_TITLES.get(row["status"], "")
    if row["status"] == "past" and row["until"]:
        status += trl(" (до {0})", row['until'])
    if status:
        parts.append(status)
    if row["note"]:
        parts.append(row["note"])
    return f"{row['name']} — " + "; ".join(parts) if parts else row["name"]


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM people WHERE merged_into IS NULL AND status!='ignored' AND mentions>0 "
                        "ORDER BY mentions DESC, name").fetchall()


def coach_block(conn: sqlite3.Connection, user_text: str, fact_ids: list[int]) -> str:
    """Люди, о которых речь сейчас: названные в реплике и связанные с найденными фактами."""
    people = active(conn)
    chosen = [p for p in people if _mentions_name(p["name"], user_text)]
    if fact_ids and len(chosen) < BLOCK_LIMIT:
        marks = ",".join("?" * len(fact_ids))
        linked = {r[0] for r in conn.execute(f"SELECT DISTINCT person_id FROM fact_people WHERE fact_id IN ({marks})",
                                             fact_ids)}
        chosen += [p for p in people if p["id"] in linked and p not in chosen]
    if not chosen:
        return ""
    return (trl("ЛЮДИ (кто есть кто — из заметок и его ответов; одно имя в разные годы может быть разными людьми):\n")
            + "\n".join(f"- {_line(p)}" for p in chosen[:BLOCK_LIMIT]))


def queue(conn: sqlite3.Connection, include_skipped: bool = False) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM people WHERE merged_into IS NULL AND status='unknown' AND mentions>=? "
                        "ORDER BY mentions DESC, name", (ASK_MIN_MENTIONS,)).fetchall()
    if include_skipped:
        return rows
    return [r for r in rows if store.cache_get(conn, "people_skip", str(r["id"]), digest(r["name"])) is None]


def _examples(conn: sqlite3.Connection, pid: int, n: int = 2) -> list[sqlite3.Row]:
    return conn.execute("SELECT f.claim, f.event_date FROM fact_people fp JOIN facts f ON f.id = fp.fact_id "
                        "WHERE fp.person_id=? AND f.user_state!='rejected' ORDER BY f.event_date DESC LIMIT ?",
                        (pid, n)).fetchall()


def merge(conn: sqlite3.Connection, src: int, dst: int) -> None:
    if src == dst:
        return
    conn.execute("INSERT OR IGNORE INTO fact_people(fact_id, person_id) SELECT fact_id, ? FROM fact_people "
                 "WHERE person_id=?", (dst, src))
    conn.execute("DELETE FROM fact_people WHERE person_id=?", (src,))
    conn.execute("UPDATE people SET merged_into=?, updated_at=? WHERE id=?", (dst, iso_now(), src))
    refresh_stats(conn)


LEGEND = trl("Ответы:\n"
             "  с — сейчас в моей жизни\n"
             "  б — в прошлом (можно с годом: «б 2019»)\n"
             "  к текст — кто это: «к младшая сестра», «к коллега с прошлой работы»\n"
             "  = имя — это тот же человек, что … («= Персона»)\n"
             "  н — не человек или ошибка (больше не покажу)\n"
             "  п — пропустить,  в — закончить\n"
             "Своими словами («сестра, видимся каждую неделю») — запишу как «кто это».")


def ask(conn: sqlite3.Connection, limit: int = 15, include_skipped: bool = False) -> int:
    """Спросить про людей, о которых известно мало. Возвращает число ответов."""
    items = queue(conn, include_skipped)
    if not items:
        print(trl("Вопросов про людей нет.\n"))
        return 0
    print(trl("Людей, про которых неясно, кто это и есть ли они сейчас в твоей жизни: {0}. За раз — до {1}.\n", len(items), limit))
    print(LEGEND + "\n")
    answered = 0
    for row in items:
        if answered >= limit:
            break
        row = conn.execute("SELECT * FROM people WHERE id=?", (row["id"],)).fetchone()
        if row is None or row["merged_into"] or row["status"] != "unknown":
            continue
        print(f"[{answered + 1}] {_line(row)}")
        for ex in _examples(conn, row["id"]):
            print(f"  · {ex['claim']}" + (f" ({ex['event_date'][:7]})" if ex["event_date"] else ""))
        try:
            ans = ru_key(input("  > ").strip())
        except (EOFError, KeyboardInterrupt):
            print()
            break
        low = ans.casefold()
        if low in ("в", "выход", "q", "quit", "exit"):
            break
        if not ans:
            continue
        now = iso_now()
        if low == "п":
            store.cache_put(conn, "people_skip", str(row["id"]), digest(row["name"]), {"at": now})
            print()
            continue
        if low == "с":
            conn.execute("UPDATE people SET status='current', updated_at=? WHERE id=?", (now, row["id"]))
            msg = trl("сейчас в твоей жизни")
        elif low[:1] == "б" and (len(low) == 1 or not low[1].isalpha()):
            until = normalize_time_ref(ans[1:].strip()) or None
            conn.execute("UPDATE people SET status='past', until=?, updated_at=? WHERE id=?", (until, now, row["id"]))
            msg = trl("в прошлом") + (trl(" (до {0})", until) if until else "")
        elif low == "н":
            conn.execute("UPDATE people SET status='ignored', updated_at=? WHERE id=?", (now, row["id"]))
            conn.execute("DELETE FROM fact_people WHERE person_id=?", (row["id"],))
            msg = trl("не человек — больше не покажу")
        elif ans.startswith("="):
            other = ans[1:].strip()
            target = conn.execute("SELECT id FROM people WHERE name=? COLLATE NOCASE", (other,)).fetchone()
            if not other or target is None:
                print(trl("  Не нашёл «{0}» среди людей. Можно ответить «к …», чтобы просто записать, кто это.\n", other))
                continue
            dst = _canonical(conn, target["id"])
            merge(conn, row["id"], dst)
            msg = trl("тот же человек, что {0}", store.scalar(conn, 'SELECT name FROM people WHERE id=?', (dst,)))
        else:
            text = ans[1:].strip() if low[:1] == "к" and (len(low) == 1 or low[1] == " ") else ans
            if not text or guard.text_hold(text):
                continue
            conn.execute("UPDATE people SET relation=?, relation_src='user', updated_at=? WHERE id=?",
                         (text[:80], now, row["id"]))
            msg = trl("кто это: {0}", text[:80])
            try:
                more = input(trl("  Сейчас в твоей жизни? (с — да, б — в прошлом, Enter — не знаю): ")).strip().casefold()
            except (EOFError, KeyboardInterrupt):
                more = ""
            more = ru_key(more)
            if more == "с":
                conn.execute("UPDATE people SET status='current' WHERE id=?", (row["id"],))
                msg += trl("; сейчас в жизни")
            elif more[:1] == "б":
                until = normalize_time_ref(more[1:].strip()) or None
                conn.execute("UPDATE people SET status='past', until=? WHERE id=?", (until, row["id"]))
                msg += trl("; в прошлом")
        conn.commit()
        answered += 1
        print(f"  ✓ {msg}\n")
    left = len(queue(conn))
    print(trl("Ответов: {0}. Осталось вопросов про людей: {1}.\n", answered, left))
    return answered


def print_list(conn: sqlite3.Connection) -> None:
    rows = active(conn)
    if not rows:
        print(trl("Людей пока нет — они появятся после python3 coach.py update."))
        return
    for r in rows:
        print(f"  {_line(r)}")
    print(trl("\nВсего: {0}. Уточнить: python3 coach.py people (или /people в чате).", len(rows)))


def write_md(conn: sqlite3.Connection, path: Path) -> None:
    rows = active(conn)
    lines = [trl("# Люди"), "", trl("Кто упоминается в заметках. Уточнить: `python3 coach.py people`."), ""]
    lines += [f"- {_line(r)}" for r in rows] or [trl("Пока никого.")]
    atomic_write(path, "\n".join(lines) + "\n")


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = active(conn)
    return {"people": len(rows), "known": sum(1 for r in rows if r["status"] != "unknown" or r["relation_src"] == "user"),
            "questions": len(queue(conn))}
