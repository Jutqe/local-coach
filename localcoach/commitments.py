"""Договорённости: что человек решил сделать, со сроком и статусом.

Записываются в конце сессии (новые) и там же обновляются (что сделано, брошено,
продвинулось), без отдельного вызова модели. Коуч видит открытые — чтобы спросить о них
к месту — и историю выполнения, чтобы замечать паттерны.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import guard, store
from .util import iso_now, loads, normalize_time_ref, today
from .i18n import trl

STATUS_TITLES = {"open": trl("открыта"), "done": trl("сделано"), "dropped": trl("брошено")}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[\wё]+", text.casefold().replace("ё", "е")) if len(w) >= 4}


def similar(a: str, b: str, threshold: float = 0.66) -> bool:
    """Почти та же договорённость другими словами («сходить на пробежки» / «продолжать пробежки»):
    большая часть слов более короткой формулировки есть в другой. Глаголы часто разные, поэтому
    мерим по короткой, а не по объединению."""
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return False
    common = len(sa & sb)
    return common >= min(2, len(sa), len(sb)) and common / min(len(sa), len(sb)) >= threshold


def open_list(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Просроченные и со сроком — первыми.
    return conn.execute("SELECT * FROM commitments WHERE status='open' "
                        "ORDER BY due IS NULL, due, id").fetchall()


def add(conn: sqlite3.Connection, session_id: int | None, text: str, due: str | None = None) -> int | None:
    text = text.strip()
    if len(text) < 4 or guard.text_hold(text):
        return None
    recent = conn.execute("SELECT text FROM commitments WHERE status='open' OR updated_at >= date('now', '-7 days')")
    if any(similar(r["text"], text) for r in recent):
        return None
    now = iso_now()
    cur = conn.execute("INSERT INTO commitments(session_id, text, due, created_at, updated_at) VALUES(?,?,?,?,?)",
                       (session_id, text, normalize_time_ref(due or "") or None, now, now))
    return int(cur.lastrowid)


def set_status(conn: sqlite3.Connection, cid: int, status: str, note: str = "") -> None:
    row = conn.execute("SELECT note FROM commitments WHERE id=?", (cid,)).fetchone()
    if not row:
        return
    stamp = f"{today()}: {note}" if note else ""
    merged = "; ".join(x for x in (row["note"], stamp) if x)
    conn.execute("UPDATE commitments SET status=?, note=?, updated_at=? WHERE id=?",
                 (status, merged[-600:], iso_now(), cid))


def migrate_legacy(conn: sqlite3.Connection) -> int:
    """Один раз перенести договорённости из итогов прежних сессий (до появления таблицы)."""
    if store.get_meta(conn, "commitments_migrated"):
        return 0
    n = 0
    for s in conn.execute("SELECT id, started_at, agreements_json FROM sessions WHERE agreements_json!='[]' "
                          "ORDER BY id DESC LIMIT 10").fetchall():
        for text in loads(s["agreements_json"], []) or []:
            if isinstance(text, str) and add(conn, s["id"], text):
                conn.execute("UPDATE commitments SET created_at=? WHERE id=last_insert_rowid()", (s["started_at"],))
                n += 1
    store.set_meta(conn, "commitments_migrated", iso_now())
    conn.commit()
    return n


def _when(r: sqlite3.Row) -> str:
    parts = [trl("с {0}", r['created_at'][:10])]
    if r["due"]:
        parts.append(trl("срок {0}", r['due']) + (trl(" — просрочено") if r["due"] < today() else ""))
    return ", ".join(parts)


def session_input(conn: sqlite3.Connection) -> tuple[str, dict[str, int]]:
    """Открытые договорённости для итога сессии: текст с id D… и карта id."""
    rows = open_list(conn)[:15]
    ids = {f"D{r['id']}": r["id"] for r in rows}
    text = "\n".join(f"D{r['id']}: {r['text']} ({_when(r)})" for r in rows) or "—"
    return text, ids


def apply_session(conn: sqlite3.Connection, session_id: int, raw: dict[str, Any], ids: dict[str, int]) -> list[str]:
    """Записать новые договорённости и обновления из итога сессии. Возвращает строки для показа."""
    shown = []
    for u in raw.get("updates") or []:
        if not isinstance(u, dict):
            continue
        cid = ids.get(str(u.get("id") or "").strip().upper())
        note = str(u.get("note") or "").strip()
        if cid is None or guard.text_hold(note):
            continue
        text = store.scalar(conn, "SELECT text FROM commitments WHERE id=?", (cid,))
        if u.get("status") in ("done", "dropped"):
            set_status(conn, cid, u["status"], note)
            shown.append(f"{STATUS_TITLES[u['status']]}: {text}")
        elif u.get("status") == "progress":
            set_status(conn, cid, "open", note)
            shown.append(trl("продвинулось: {0}", text) + (f" ({note})" if note else ""))
    for c in raw.get("commitments") or []:
        if isinstance(c, dict) and add(conn, session_id, str(c.get("text") or ""), str(c.get("due") or "")):
            due = normalize_time_ref(str(c.get("due") or ""))
            shown.append(trl("новая: {0}", c['text'].strip()) + (trl(" (срок {0})", due) if due else ""))
    conn.commit()
    return shown


def coach_block(conn: sqlite3.Connection, open_limit: int = 6, history_limit: int = 6) -> str:
    """Открытые договорённости и история выполнения — в системный промпт коуча."""
    rows = open_list(conn)
    closed = conn.execute("SELECT text, status, note, updated_at FROM commitments WHERE status!='open' "
                          "ORDER BY updated_at DESC").fetchall()
    if not rows and not closed:
        return ""
    out = []
    if rows:
        out.append(trl("ОТКРЫТЫЕ ДОГОВОРЁННОСТИ (спроси об одной, если к месту):\n")
                   + "\n".join(f"- {r['text']} ({_when(r)})" + (trl("; заметки: {0}", r['note']) if r["note"] else "")
                               for r in rows[:open_limit]))
    if closed:
        done = sum(r["status"] == "done" for r in closed)
        lines = [f"- {'✓' if r['status'] == 'done' else '✗'} {r['text']} ({r['updated_at'][:10]})"
                 + (f": {r['note'][-150:]}" if r["note"] else "") for r in closed[:history_limit]]
        out.append(trl("ИСТОРИЯ ДОГОВОРЁННОСТЕЙ: выполнено {0} из {1} закрытых. Последние:\n", done, len(closed)) + "\n".join(lines))
    return "\n\n".join(out)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM commitments GROUP BY status")}


def manage(conn: sqlite3.Connection) -> None:
    """Посмотреть и отметить договорённости вручную."""
    while True:
        rows = open_list(conn)
        closed = conn.execute("SELECT text, status, updated_at FROM commitments WHERE status!='open' "
                              "ORDER BY updated_at DESC LIMIT 5").fetchall()
        if rows:
            print(trl("Открытые договорённости:"))
            for i, r in enumerate(rows, 1):
                print(f"  {i}. {r['text']} ({_when(r)})" + (f"\n     {r['note']}" if r["note"] else ""))
        else:
            print(trl("Открытых договорённостей нет."))
        if closed:
            print(trl("Недавно закрытые:"))
            for r in closed:
                print(f"  {'✓' if r['status'] == 'done' else '✗'} {r['text']} ({r['updated_at'][:10]})")
        print(trl("\n«2 с» — сделано, «2 б» — брошено, «+ текст [до 2026-11-01]» — добавить, Enter — выйти."))
        try:
            ans = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not ans:
            print()
            return
        if ans.startswith("+"):
            m = re.search(r"\s+(?:до|until|by|due)\s+(\S+)\s*$", ans)
            text = ans[1:m.start()] if m else ans[1:]
            if add(conn, None, text, m.group(1) if m else None):
                conn.commit()
                print(trl("Добавлено.\n"))
            else:
                print(trl("Не добавил: такая уже есть (или текст слишком короткий).\n"))
            continue
        m = re.fullmatch(r"(\d+)\s*([сбСБdDxX])\s*(.*)", ans)
        if not m or not 1 <= int(m.group(1)) <= len(rows):
            print(trl("Не понял. Пример: «2 с» или «+ позвонить врачу до 2026-11-01».\n"))
            continue
        set_status(conn, rows[int(m.group(1)) - 1]["id"], "done" if m.group(2).casefold() in ("с", "d") else "dropped",
                   m.group(3).strip())
        conn.commit()
        print()
