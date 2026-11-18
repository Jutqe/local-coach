"""Life Reset: разбор реестра в вебе.

Те же правила, что у `triage` в терминале (ёмкость, срок пересмотра, склейка, свободный ответ
через модель), но без диалога: каждая функция – один шаг, который страница вызывает кнопкой.
Решение всегда записывает человек; модель только предлагает статус по его словам.
Решение и склейку можно отменить: прежнее состояние хранится в памяти процесса.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from . import guard, inventory, store
from .config import DOMAIN_TITLES, LR_STATUS_TITLES, LR_TYPE_TITLES, PRIVATE_DOMAINS
from .ollama import Ollama
from .util import iso_now, today
from .i18n import trl

PAGE = 10
STATUSES = ("active", "incubating", "someday", "waiting", "delegated", "completed", "abandoned", "archived",
            "clarification", "dismissed")
REVIEW_CHOICES = (1, 3, 6, 12)        # через сколько месяцев вернуться к отложенному
KEEP_UNDO = 30
# Области, которые можно назначить пункту вручную (закрытая «Документы и администрирование» в реестр не идёт).
AREAS = [{"key": k, "title": t} for k, t in DOMAIN_TITLES.items() if k not in PRIVATE_DOMAINS]
_UNDO: dict[str, dict[str, Any]] = {}
_COLS = ("status", "review_on", "next_step", "resurfaced", "bulk", "src_sig", "decided_at", "updated_at")
_counter = [0]


def _row(conn: sqlite3.Connection, iid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM lr_items WHERE id=? AND merged_into IS NULL", (iid,)).fetchone()


def _keep(token: str, entry: dict[str, Any]) -> str:
    _UNDO[token] = entry
    for old in list(_UNDO)[:-KEEP_UNDO]:
        _UNDO.pop(old, None)
    return token


def _item(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "code": inventory.code(r["id"]), "title": r["title"], "type": r["type"],
            "type_title": LR_TYPE_TITLES.get(r["type"], r["type"]), "area": r["domain"],
            "area_title": DOMAIN_TITLES.get(r["domain"], r["domain"]), "status": r["status"],
            "status_title": LR_STATUS_TITLES.get(r["status"], r["status"]), "resurfaced": bool(r["resurfaced"]),
            "where": inventory.span(r) if r["origin"] == "memory" else trl("из разговора")}


def waiting(conn: sqlite3.Connection) -> int:
    """Сколько пунктов ждёт решения; 0, пока реестр не собран."""
    return len(inventory.queue(conn)) if conn.execute("SELECT 1 FROM lr_items LIMIT 1").fetchone() else 0


def list_page(conn: sqlite3.Connection, area: str = "", exclude: list[int] | None = None,
              limit: int = PAGE) -> dict[str, Any]:
    """Очередь на разбор: следующие пункты, кроме уже показанных (exclude). Порядок тот же, что в терминале."""
    area = area if area in DOMAIN_TITLES else ""
    every = inventory.queue(conn)
    counts: dict[str, int] = {}
    for r in every:
        counts[r["domain"]] = counts.get(r["domain"], 0) + 1
    q = [r for r in every if not area or r["domain"] == area]
    skip = set(exclude or [])
    rest = [r for r in q if r["id"] not in skip]
    return {"total": len(every), "area_total": len(q), "area": area, "page": limit,
            "areas": [{"key": k, "title": DOMAIN_TITLES.get(k, k), "n": n} for k, n in sorted(counts.items(), key=lambda x: -x[1])],
            "items": [_item(r) for r in rest[:limit]], "more": len(rest) > limit,
            "capacity": inventory.capacity(conn), "active": inventory.active_count(conn),
            "bulk": inventory.bulk_count(conn)}


def card(conn: sqlite3.Connection, iid: int) -> dict[str, Any]:
    """Всё, что нужно для решения, по частям (страница сама решает, как показать)."""
    r = _row(conn, iid)
    if r is None:
        return {"error": "gone"}
    out = _item(r)
    out["summary"] = r["summary"] if r["summary"] and r["summary"].strip().rstrip(".") != r["title"].strip().rstrip(".") else ""
    out["want"] = r["want"] or ""
    out["hint"] = r["hint"] or ""
    out["next_step"] = r["next_step"] or ""
    out["sources"] = []
    for f in inventory.sources(conn, r):
        when = (f["event_date"] or f["note_date"] or "")[:7]
        where = (f"«{f['note_title']}»" if f["note_title"] else "разговор") + (f", {when}" if when else "")
        quote = f["evidence_quote"][:200] if f["evidence_quote"] and f["quote_match"] in ("exact", "partial") else ""
        out["sources"].append({"claim": inventory._claim_text(f["claim"]), "where": where, "quote": quote})
    out["related"] = [{"id": o["id"], "code": inventory.code(o["id"]), "title": o["title"],
                       "status_title": LR_STATUS_TITLES[o["status"]]} for o, _s in inventory.related(conn, r)]
    last = conn.execute("SELECT reason, created_at FROM lr_decisions WHERE item_id=? AND to_status!='merged' "
                        "ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
    out["areas_all"] = AREAS
    out["last_decision"] = ({"date": last["created_at"][:10], "reason": last["reason"] or ""} if last else None)
    return out


def find(conn: sqlite3.Connection, query: str, exclude: int = 0) -> list[dict[str, Any]]:
    """Поиск пункта по названию или номеру – чтобы склеить с ним."""
    q = query.strip()
    if not q:
        return []
    num = q.upper().replace("LR-", "").strip()
    if num.isdigit():
        r = _row(conn, int(num))
        return [_item(r)] if r is not None and r["id"] != exclude else []
    key = inventory._norm_title(q)
    out = []
    for r in conn.execute("SELECT * FROM lr_items WHERE merged_into IS NULL AND status!='dismissed' AND id!=? "
                          "ORDER BY notes_count DESC, mentions DESC", (exclude,)):
        if key and key in inventory._norm_title(r["title"]):
            out.append(_item(r))
            if len(out) == 6:
                break
    return out


def set_capacity(conn: sqlite3.Connection, n: Any) -> dict[str, Any]:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return {"error": "number"}
    if not 1 <= n <= 99:
        return {"error": "number"}
    store.set_meta(conn, "lr_capacity", str(n))
    conn.commit()
    return {"ok": True, "capacity": n}


def decide(conn: sqlite3.Connection, iid: int, status: str, *, step: str = "", reason: str = "", answer: str = "",
           proposal: str = "", review: str = "", months: int = 0, force: bool = False,
           skip_capacity: bool = False) -> dict[str, Any]:
    r = _row(conn, iid)
    if r is None:
        return {"error": "gone"}
    if status not in STATUSES:
        return {"error": "status"}
    if any(guard.text_hold(t) for t in (step, reason, answer) if t):
        return {"error": "secret"}
    if status == "active" and r["type"] not in inventory.CAPACITY_EXEMPT:
        cap = inventory.capacity(conn)
        if not cap and not skip_capacity:
            return {"need": "capacity"}
        n = inventory.active_count(conn, exclude=iid)
        if cap and n >= cap and not force:
            names = [x["title"] for x in conn.execute(
                "SELECT title FROM lr_items WHERE status='active' AND merged_into IS NULL AND id!=? ORDER BY decided_at",
                (iid,)).fetchall()][:8]
            return {"need": "confirm", "active": n, "capacity": cap, "names": names}
    review_on = inventory.parse_review(review) if review else None
    if months in REVIEW_CHOICES:
        review_on = inventory.add_months(date.fromisoformat(today()), months).isoformat()
    snap = {c: r[c] for c in _COLS}
    inventory.decide(conn, iid, status, reason=reason, answer=answer, proposal=proposal if proposal in STATUSES else "",
                     next_step=step or None, review_on=review_on)
    did = conn.execute("SELECT id FROM lr_decisions WHERE item_id=? ORDER BY id DESC LIMIT 1", (iid,)).fetchone()[0]
    token = _keep(str(did), {"kind": "decision", "item": iid, "cols": snap, "decision_id": did})
    after = _row(conn, iid)
    return {"ok": True, "status": status, "label": LR_STATUS_TITLES[status], "review_on": after["review_on"] or "",
            "left": waiting(conn), "undo": token}


def undo(conn: sqlite3.Connection, token: Any) -> dict[str, Any]:
    """Вернуть всё, как было до решения или склейки. Только если после этого по пункту ничего не менялось."""
    entry = _UNDO.get(str(token))
    if entry is None:
        return {"error": "late"}
    if entry["kind"] == "area":
        r = _row(conn, entry["item"])
        if r is None or r["domain"] != entry["new"]:
            return {"error": "late"}
        conn.execute("UPDATE lr_items SET domain=?, updated_at=? WHERE id=?", (entry["prev"], iso_now(), entry["item"]))
        conn.commit()
        _UNDO.pop(str(token), None)
        return {"ok": True, "left": waiting(conn), "restored": 1}
    if entry["kind"] == "merge":
        # Склейки откатываются с конца: каждая проверяет, что цель после неё не менялась.
        for snap in reversed(entry["snaps"]):
            if not inventory.unmerge(conn, snap):
                return {"error": "late"}
        _UNDO.pop(str(token), None)
        return {"ok": True, "left": waiting(conn), "restored": len(entry["snaps"])}
    last = conn.execute("SELECT id FROM lr_decisions WHERE item_id=? ORDER BY id DESC LIMIT 1", (entry["item"],)).fetchone()
    if last is None or last[0] != entry["decision_id"]:
        return {"error": "late"}
    cols = entry["cols"]
    conn.execute(f"UPDATE lr_items SET {', '.join(c + '=?' for c in _COLS)} WHERE id=?",
                 (*[cols[c] for c in _COLS], entry["item"]))
    conn.execute("DELETE FROM lr_decisions WHERE id=?", (entry["decision_id"],))
    conn.commit()
    _UNDO.pop(str(token), None)
    return {"ok": True, "left": waiting(conn), "restored": 1}


def set_area(conn: sqlite3.Connection, iid: int, area: str) -> dict[str, Any]:
    """Сменить область пункта вручную (модель при извлечении могла отнести факты не туда). Пересборка реестра
    область существующего пункта не меняет. Решение по пункту и журнал решений не трогаются; можно отменить."""
    r = _row(conn, iid)
    if r is None:
        return {"error": "gone"}
    if area not in DOMAIN_TITLES or area in PRIVATE_DOMAINS:
        return {"error": "area"}
    if r["domain"] == area:
        return {"ok": True, "same": True, "area": area, "area_title": DOMAIN_TITLES[area]}
    conn.execute("UPDATE lr_items SET domain=?, updated_at=? WHERE id=?", (area, iso_now(), iid))
    conn.commit()
    _counter[0] += 1
    token = _keep(f"a{_counter[0]}", {"kind": "area", "item": iid, "prev": r["domain"], "new": area})
    return {"ok": True, "area": area, "area_title": DOMAIN_TITLES[area], "prev_title": DOMAIN_TITLES.get(r["domain"], r["domain"]),
            "left": waiting(conn), "undo": token}


def merge(conn: sqlite3.Connection, iid: int, into: Any) -> dict[str, Any]:
    try:
        target = int(str(into).upper().replace("LR-", "").strip())
    except ValueError:
        return {"error": "number"}
    snap: dict[str, Any] = {}
    if not inventory.merge(conn, iid, target, snap):
        return {"error": "merge"}
    token = _keep(f"m{_counter[0] + 1}", {"kind": "merge", "snaps": [snap]})
    _counter[0] += 1
    return {"ok": True, "into": inventory.code(target), "left": waiting(conn), "undo": token}


def dedupe(conn: sqlite3.Connection) -> dict[str, Any]:
    """Склеить пункты с одинаковым названием (при открытии разбора); результат можно отменить целиком."""
    snaps: list[dict[str, Any]] = []
    n = inventory.dedupe(conn, snaps)
    if not n:
        return {"merged": 0}
    token = _keep(f"m{_counter[0] + 1}", {"kind": "merge", "snaps": snaps})
    _counter[0] += 1
    return {"merged": n, "undo": token, "left": waiting(conn)}


def say(conn: sqlite3.Connection, llm: Ollama, iid: int, text: str) -> dict[str, Any]:
    """Свободный ответ: модель предлагает статус, причину, шаг и срок. Записывает только «decide» после кивка человека."""
    text = text.strip()
    r = _row(conn, iid)
    if r is None:
        return {"error": "gone"}
    if not text:
        return {"error": "empty"}
    if guard.text_hold(text):
        return {"error": "secret"}
    got = inventory._interpret(llm, inventory.card(conn, r), text)
    if not got["status"]:
        inventory.record_clarification(conn, r, text)
        return {"recorded": "clarification", "left": waiting(conn)}
    review = inventory.parse_review(got["review_on"]) if got["review_on"] else None
    return {"status": got["status"], "label": LR_STATUS_TITLES[got["status"]], "reason": got["reason"],
            "next_step": got["next_step"], "review_on": review or ""}
