"""Синхронизация vault с базой, извлечение фактов из фрагментов и импорт результатов v3."""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path, PurePosixPath
from typing import Any

from . import guard, store
from .config import Config, DOMAINS, FACT_KINDS, INTENT_STATUSES
from .ollama import Ollama, OllamaError, TruncatedError
from .prompts import EXTRACT_SYSTEM, PROMPT_VERSION, extract_schema, extract_user
from .util import (Progress, year_in_text, digest, dumps, iso_now, loads, normalize_text, normalize_time_ref, quote_match,
                   sha256_text, stable_json)
from .vault import Note
from .i18n import trl

GOAL_LIKE = {"goal", "decision", "open_loop"}
RELIABLE_DATE_SOURCES = {"frontmatter", "filename"}


def is_project_path(path: str, cfg: Config) -> bool:
    """Заметка в папке проектов или идей: «Проекты», «Проекты 2024», «Projects - cars» (4.16 — и с добавкой
    после слова; раньше только точное имя)."""
    parts = [p.strip().casefold() for p in PurePosixPath(path).parts[:-1]]
    return any(p == d or re.match(re.escape(d) + r"(?:[\s._,:;()\[\]-]|$)", p) for p in parts for d in cfg.project_dirs)


def refresh_project_flags(conn: sqlite3.Connection, cfg: Config) -> int:
    """Пометка «проект» по текущему правилу для уже извлечённых фактов — без переизвлечения."""
    changed = 0
    for (path,) in conn.execute("SELECT DISTINCT path FROM facts WHERE path IS NOT NULL").fetchall():
        flag = int(is_project_path(path, cfg))
        changed += conn.execute("UPDATE facts SET is_project=? WHERE path=? AND is_project!=?",
                                (flag, path, flag)).rowcount
    conn.commit()
    return changed


def _year_plausible(time_ref: str, body: str | None, title: str | None, heading: str | None,
                    date_source: str, note_date: str | None, chunk_date: str | None) -> bool:
    """Год факта на что-то опирается: он есть в тексте как год, это год раздела или — у заметки с надёжной
    датой — год заметки ±1 («в прошлом году», «вчера»). «2000 оборотов» в заметке 2023 года — не опора."""
    if _year_in_source(time_ref, body, title, heading):
        return True
    year = int(time_ref[:4])
    if chunk_date and chunk_date != note_date and chunk_date[:4] == time_ref[:4]:   # год раздела, а не дата файла
        return True
    if date_source in RELIABLE_DATE_SOURCES and note_date:
        return abs(year - int(note_date[:4])) <= 1
    return False


def source_ref(path: str, line_start: int, line_end: int) -> str:
    return f"{path}#L{line_start}-L{line_end}"


# --- синхронизация -----------------------------------------------------------

def sync_notes(conn: sqlite3.Connection, notes: list[Note], cfg: Config) -> dict[str, int]:
    """Привести базу к текущему состоянию vault. Неизменённые фрагменты переиспользуются
    по хэшу, поэтому правка одного раздела не заставляет переизвлекать всю заметку."""
    stats = {"new": 0, "changed": 0, "held": 0, "stale": 0, "chunks_new": 0, "chunks_reused": 0, "chunks_dropped": 0}
    chunk_cfg = digest([cfg.chunk_chars, cfg.chunk_min_chars, cfg.chunk_overlap_chars, "v4.14"])
    rechunk_all = store.get_meta(conn, "chunk_cfg") != chunk_cfg
    dirty: set[int] = set()
    seen = set()
    now = iso_now()

    for note in notes:
        seen.add(note.path)
        old = conn.execute("SELECT sha256, status, note_date FROM notes WHERE path=?", (note.path,)).fetchone()
        unchanged = old is not None and old["sha256"] == note.sha256
        fields = (note.title, note.sha256, note.size, note.mtime, note.note_date, note.date_source,
                  dumps(note.tags), dumps(note.links))

        if note.hold_reason:
            if old is None or old["status"] != "security_hold" or not unchanged:
                dirty |= store.delete_note_derived(conn, note.path)
                stats["held"] += 1
            _upsert_note(conn, note.path, fields, "security_hold", note.hold_reason, now)
            continue

        if unchanged and old["status"] == "indexed" and not rechunk_all:
            _upsert_note(conn, note.path, fields, "indexed", None, now)
            continue

        stats["new" if old is None else "changed"] += 1
        _upsert_note(conn, note.path, fields, "indexed", None, now)
        # Факты, импортированные из v3, привязаны к старой нарезке: при изменении
        # заметки перестраиваем её целиком.
        has_import = store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE path=? AND chunk_id IS NULL "
                                        "AND origin='v3_import'", (note.path,))
        if has_import:
            dirty |= store.delete_note_derived(conn, note.path)

        old_rows = conn.execute("SELECT id, sha256, status FROM chunks WHERE path=?", (note.path,)).fetchall()
        by_sha: dict[str, list[sqlite3.Row]] = {}
        for r in old_rows:
            by_sha.setdefault(r["sha256"], []).append(r)
        # Освобождаем номера idx, чтобы не упереться в UNIQUE(path, idx).
        conn.execute("UPDATE chunks SET idx = -id WHERE path=?", (note.path,))
        kept: set[int] = set()
        for ch in note.chunks:
            sha = chunk_key(note, ch)
            prev = next((r for r in by_sha.get(sha, []) if r["id"] not in kept), None)
            if prev is not None:
                kept.add(prev["id"])
                conn.execute("UPDATE chunks SET idx=?, char_start=?, char_end=?, line_start=?, line_end=?, heading=?, "
                             "chunk_date=?, updated_at=? WHERE id=?",
                             (ch.idx, ch.char_start, ch.char_end, ch.line_start, ch.line_end, ch.heading,
                              ch.chunk_date, now, prev["id"]))
                conn.execute("UPDATE facts SET source_ref=?, note_date=? WHERE chunk_id=?",
                             (source_ref(note.path, ch.line_start, ch.line_end), note.note_date, prev["id"]))
                conn.execute("UPDATE hypotheses_raw SET source_ref=? WHERE chunk_id=?",
                             (source_ref(note.path, ch.line_start, ch.line_end), prev["id"]))
                stats["chunks_reused"] += 1
            else:
                cur = conn.execute(
                    "INSERT INTO chunks(path, idx, char_start, char_end, line_start, line_end, heading, chunk_date, "
                    "sha256, body, status, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                    (note.path, ch.idx, ch.char_start, ch.char_end, ch.line_start, ch.line_end, ch.heading,
                     ch.chunk_date, sha, ch.text, now))
                store.fts_put_chunk(conn, int(cur.lastrowid), ch.text, note.title)
                stats["chunks_new"] += 1
        for prev in old_rows:
            if prev["id"] not in kept:
                dirty |= store.delete_chunk_derived(conn, prev["id"])
                conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (prev["id"],))
                conn.execute("DELETE FROM chunks WHERE id=?", (prev["id"],))
                stats["chunks_dropped"] += 1
        store.mark_clusters_dirty(conn, dirty)
        conn.commit()

    for row in conn.execute("SELECT path FROM notes WHERE status!='stale'").fetchall():
        if row["path"] not in seen:
            dirty |= store.delete_note_derived(conn, row["path"])
            conn.execute("UPDATE notes SET status='stale', updated_at=? WHERE path=?", (now, row["path"]))
            stats["stale"] += 1

    store.mark_clusters_dirty(conn, dirty)
    store.set_meta(conn, "chunk_cfg", chunk_cfg)
    conn.commit()
    return stats


def chunk_key(note: Note, ch) -> str:
    """Ключ переиспользования фрагмента: текст, путь заголовков и надёжная дата.
    Меняется дата заметки или заголовок раздела — фрагмент извлекается заново, иначе у
    фактов останутся старые даты. mtime-дата в ключ не входит: она меняется при каждом
    сохранении файла и обесценила бы переиспользование."""
    reliable = note.date_source in RELIABLE_DATE_SOURCES
    date_part = ch.chunk_date if reliable or ch.chunk_date != note.note_date else None
    return sha256_text(stable_json([ch.text, ch.heading, date_part, reliable]))


def _upsert_note(conn, path, fields, status, reason, now) -> None:
    conn.execute(
        "INSERT INTO notes(path, title, sha256, size_bytes, mtime, note_date, date_source, tags_json, links_json, "
        "status, hold_reason, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "title=excluded.title, sha256=excluded.sha256, size_bytes=excluded.size_bytes, mtime=excluded.mtime, "
        "note_date=excluded.note_date, date_source=excluded.date_source, tags_json=excluded.tags_json, "
        "links_json=excluded.links_json, status=excluded.status, hold_reason=excluded.hold_reason, "
        "updated_at=excluded.updated_at",
        (path, *fields, status, reason, now))


# --- извлечение -----------------------------------------------------------------

def _date_line(note_date: str | None, date_source: str, chunk_date: str | None) -> str:
    if date_source in RELIABLE_DATE_SOURCES and note_date:
        line = trl("Дата заметки: {0}.", note_date)
    elif date_source == "import":
        line = (trl("Это документ, который пользователь загрузил сам (например, биография или таблица). Дата загрузки не означает дату событий: время бери только из самого текста."))
    elif date_source == "mtime" and note_date:
        line = (trl("Дата написания неизвестна; файл последний раз изменён {0} — заметка могла быть написана раньше.", note_date))
    else:
        line = trl("Дата заметки неизвестна.")
    if chunk_date and chunk_date != note_date:
        line += trl(" Раздел датирован: {0}.", chunk_date)
    return line


def extract_pending(conn: sqlite3.Connection, llm: Ollama, cfg: Config) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT c.id, c.path, c.idx, c.line_start, c.line_end, c.heading, c.chunk_date, c.body, c.attempts, "
        "n.title, n.note_date, n.date_source, n.tags_json FROM chunks c JOIN notes n ON n.path=c.path "
        "WHERE n.status='indexed' AND c.status IN ('pending','error') AND c.attempts < ? "
        "ORDER BY c.path, c.idx", (cfg.max_extract_attempts,)).fetchall()
    if not rows:
        print(trl("  Новых фрагментов для извлечения нет."))
        return 0, 0
    print(trl("  Фрагментов к извлечению: {0}", len(rows)))
    # Время извлечения растёт с длиной фрагмента, поэтому прогноз считается по символам
    # (плюс постоянная часть на вызов), а не по числу фрагментов.
    weight = lambda r: len(r["body"]) + CALL_OVERHEAD_CHARS  # noqa: E731
    progress = Progress(len(rows), trl("фрагмент"), total_weight=sum(weight(r) for r in rows))
    ok = failed = 0
    spent = 0.0
    chars = 0
    for row in rows:
        try:
            t = time.monotonic()
            n, sent = _extract_one(conn, llm, cfg, row)
            spent += time.monotonic() - t
            # Скорость — по всем отправленным символам: деление плотного фрагмента не
            # должно завышать прогноз для обычных.
            chars += sent
            ok += 1
            if ok % 10 == 0 or ok == 3:
                _save_rate(conn, spent, ok, chars)
            progress.step(trl("фактов: {0}", n), weight(row))
        except (OllamaError, OSError, ValueError) as exc:
            conn.execute("UPDATE chunks SET status='error', attempts=attempts+1, error=?, updated_at=? WHERE id=?",
                         (f"{type(exc).__name__}: {str(exc)[:300]}", iso_now(), row["id"]))
            conn.commit()
            failed += 1
            # Текст ошибки не содержит содержимого заметки: только тип и служебное сообщение.
            progress.step(trl("ОШИБКА ({0}), будет повтор при следующем запуске", type(exc).__name__), weight(row))
    if ok:
        _save_rate(conn, spent, ok, chars)
    return ok, failed


# Постоянная часть времени вызова (промпт, ответ модели) в пересчёте на символы фрагмента.
CALL_OVERHEAD_CHARS = 2000


def _save_rate(conn: sqlite3.Connection, spent: float, ok: int, chars: int) -> None:
    store.set_meta(conn, "sec_per_chunk", f"{spent / ok:.1f}")
    store.set_meta(conn, "sec_per_kchar", f"{spent / chars * 1000:.3f}")
    conn.commit()


# Плотный текст (таблица, дневник по годам) упирается в потолок фактов на вызов: тогда
# фрагмент делится пополам по строкам и каждая половина разбирается отдельно.
SPLIT_MIN_CHARS = 300
# Метка разового переразбора: меняется, когда деление начинает брать фрагменты, которые раньше не брало.
RESPLIT_KEY = "resplit_v472"
SPLIT_DEPTH = 3


def _halves(body: str) -> tuple[str, str] | None:
    if len(body) < 2 * SPLIT_MIN_CHARS:
        return None
    mid = len(body) // 2
    cut = body.rfind("\n", SPLIT_MIN_CHARS, mid + 1)
    if cut < 0:
        cut = body.find("\n", mid, len(body) - SPLIT_MIN_CHARS)
    if cut < 0:
        return None
    return body[:cut + 1], body[cut + 1:]


def _call_extract(llm: Ollama, cfg: Config, meta: dict[str, Any], body: str, sent: list[int],
                  depth: int = 0) -> dict[str, Any]:
    user = extract_user(meta, body)
    sent.append(len(body) + CALL_OVERHEAD_CHARS)
    try:
        raw = llm.chat_json(EXTRACT_SYSTEM, user, extract_schema(cfg.max_facts_per_chunk),
                            think=cfg.think_extract, num_predict=cfg.extract_predict, heavy=True)
        saturated = len(raw.get("facts") or []) >= cfg.max_facts_per_chunk
    except TruncatedError:
        halves = _halves(body) if depth < SPLIT_DEPTH else None
        if halves is None:
            # Ответ не влез, а делить нечего: просим меньше фактов, а не повторяем тот же запрос.
            sent.append(len(body) + CALL_OVERHEAD_CHARS)
            return llm.chat_json(EXTRACT_SYSTEM + trl("\n\nОграничься 8 самыми важными фактами."), user,
                                 extract_schema(8), think=False, num_predict=cfg.extract_predict, heavy=True)
        raw, saturated = {"summary": ""}, True
    halves = _halves(body) if saturated and depth < SPLIT_DEPTH else None
    if halves is None:
        return raw
    parts = [_call_extract(llm, cfg, meta, half, sent, depth + 1) for half in halves]
    return {"summary": raw.get("summary") or parts[0].get("summary") or "",
            "facts": [f for p in parts for f in p.get("facts") or []],
            "hypotheses": [h for p in parts for h in p.get("hypotheses") or []]}


def _extract_one(conn: sqlite3.Connection, llm: Ollama, cfg: Config, row: sqlite3.Row) -> tuple[int, int]:
    """Факты одного фрагмента; второе число — сколько символов ушло модели (для скорости)."""
    meta = {"title": row["title"], "path": row["path"], "heading": row["heading"],
            "tags": loads(row["tags_json"], []),
            "date_line": _date_line(row["note_date"], row["date_source"], row["chunk_date"])}
    sent: list[int] = []
    raw = _call_extract(llm, cfg, meta, row["body"], sent)
    return save_extraction(conn, cfg, row, raw), sum(sent)


def _year_in_source(time_ref: str, *texts: str | None) -> bool:
    # «2000 оборотов» года 2000 не даёт (4.15): раньше годом считалось любое такое число.
    return any(year_in_text(t, time_ref[:4]) for t in texts)


def repair_dates(conn: sqlite3.Connection) -> int:
    """Снять даты фактов, у которых год ни на что не опирается (см. _year_plausible): выведен из даты
    изменения файла (извлечения до v4.6) или из числа вроде «2000 оборотов» (до 4.16 — и в заметках
    с надёжной датой). Факт получает дату раздела, иначе надёжную дату заметки, иначе остаётся без даты."""
    rows = conn.execute(
        "SELECT f.id, f.cluster_id, f.event_date, c.body, c.heading, c.chunk_date, n.title, n.note_date, n.date_source "
        "FROM facts f JOIN chunks c ON c.id=f.chunk_id JOIN notes n ON n.path=f.path "
        "WHERE f.origin='note' AND f.date_reliable=1 AND f.event_date IS NOT NULL").fetchall()
    bad = [r for r in rows if not _year_plausible(r["event_date"], r["body"], r["title"], r["heading"],
                                                  r["date_source"], r["note_date"], r["chunk_date"])]
    for r in bad:
        section = r["chunk_date"] if r["chunk_date"] and r["chunk_date"] != r["note_date"] else None
        if not section and r["date_source"] in RELIABLE_DATE_SOURCES:
            section = r["note_date"]
        conn.execute("UPDATE facts SET event_date=?, date_reliable=? WHERE id=?", (section, int(bool(section)), r["id"]))
    store.mark_clusters_dirty(conn, {r["cluster_id"] for r in bad if r["cluster_id"]})
    conn.commit()
    return len(bad)


def saturated_chunks(conn: sqlite3.Connection, cfg: Config) -> list[sqlite3.Row]:
    """Извлечённые фрагменты, упёршиеся в потолок фактов и ещё не разобранные по частям.

    До 4.6 не делился ни один; в 4.6 делились от 1200 символов — их уже переразобрали,
    теперь остались короткие (от 2 × SPLIT_MIN_CHARS)."""
    if store.get_meta(conn, RESPLIT_KEY):
        return []
    upper = 1200 if store.get_meta(conn, "resplit_v46") else 10 ** 9
    return conn.execute(
        "SELECT c.id, c.sha256, length(c.body) AS size FROM chunks c JOIN facts f ON f.chunk_id=c.id "
        "WHERE c.status='done' AND f.origin='note' AND length(c.body) >= ? AND length(c.body) < ? "
        "GROUP BY c.id HAVING COUNT(*) >= ?",
        (2 * SPLIT_MIN_CHARS, upper, cfg.max_facts_per_chunk)).fetchall()


def resplit_saturated(conn: sqlite3.Connection, cfg: Config) -> int:
    """Один раз (на RESPLIT_KEY): фрагменты, упёршиеся в потолок фактов, извлечь заново с делением."""
    if store.get_meta(conn, RESPLIT_KEY):
        return 0
    ids = [r[0] for r in saturated_chunks(conn, cfg)]
    for cid in ids:
        store.mark_clusters_dirty(conn, store.delete_chunk_derived(conn, cid))
        conn.execute("UPDATE chunks SET status='pending', attempts=0, error=NULL WHERE id=?", (cid,))
    store.set_meta(conn, RESPLIT_KEY, iso_now())
    store.set_meta(conn, "resplit_v46", iso_now())
    conn.commit()
    return len(ids)


def save_extraction(conn: sqlite3.Connection, cfg: Config, row: sqlite3.Row, raw: dict[str, Any]) -> int:
    chunk_id, path, body = row["id"], row["path"], row["body"]
    ref = source_ref(path, row["line_start"], row["line_end"])
    reliable = row["date_source"] in RELIABLE_DATE_SOURCES or (row["chunk_date"] and row["chunk_date"] != row["note_date"])
    fallback_date = row["chunk_date"] if reliable else None
    project = int(is_project_path(path, cfg))
    now = iso_now()

    store.mark_clusters_dirty(conn, store.delete_chunk_derived(conn, chunk_id))
    seen: set[str] = set()
    count = 0
    for f in raw.get("facts") or []:
        if not isinstance(f, dict):
            continue
        claim = str(f.get("claim") or "").strip()
        subject = f.get("subject") if f.get("subject") in ("self", "other_person", "general") else "self"
        if len(claim) < 6 or subject == "general":
            continue
        key = normalize_text(claim)
        if key in seen:
            continue
        seen.add(key)
        quote = str(f.get("evidence_quote") or "").strip()
        if guard.text_hold(claim + " " + quote):
            continue
        kind = f.get("kind") if f.get("kind") in FACT_KINDS else "fact"
        status = f.get("intent_status") if f.get("intent_status") in INTENT_STATUSES else "unclear"
        if kind not in GOAL_LIKE:
            status = "none"
        elif status == "none":
            status = "unclear"
        match = quote_match(quote, body)
        time_ref = normalize_time_ref(str(f.get("time_ref") or ""))
        if time_ref and not _year_plausible(time_ref, body, row["title"], row["heading"], row["date_source"],
                                            row["note_date"], row["chunk_date"]):
            # Год, которого нет в тексте, модель берёт из даты изменения файла, угадывает («в прошлом году»
            # от неё же) или читает число как год («2000 оборотов»). Тогда остаётся дата раздела или заметки.
            time_ref = ""
        cur = conn.execute(
            "INSERT INTO facts(chunk_id, path, source_ref, origin, claim, kind, domain, subject, explicitness, "
            "intent_status, event_date, note_date, date_reliable, evidence_quote, quote_match, sensitivity, "
            "is_project, created_at) VALUES(?,?,?,'note',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chunk_id, path, ref, claim, kind,
             f.get("domain") if f.get("domain") in DOMAINS else "other",
             subject, "inferred" if f.get("explicitness") == "inferred" else "explicit", status,
             time_ref or fallback_date, row["note_date"], int(bool(time_ref or reliable)),
             quote if match != "none" else "", match,
             "sensitive" if f.get("sensitivity") == "sensitive" else "normal", project, now))
        store.fts_put_fact(conn, int(cur.lastrowid), claim)
        count += 1

    for h in raw.get("hypotheses") or []:
        if not isinstance(h, dict):
            continue
        text, quote = str(h.get("hypothesis") or "").strip(), str(h.get("evidence_quote") or "").strip()
        # Гипотеза без проверяемой цитаты не сохраняется.
        if text and quote_match(quote, body) != "none" and not guard.text_hold(text + quote):
            conn.execute("INSERT INTO hypotheses_raw(chunk_id, path, source_ref, hypothesis, evidence_quote, "
                         "alternative, created_at) VALUES(?,?,?,?,?,?,?)",
                         (chunk_id, path, ref, text, quote, str(h.get("alternative") or "").strip(), now))

    summary = str(raw.get("summary") or "").strip()
    conn.execute("UPDATE chunks SET status='done', summary=?, attempts=attempts+1, error=NULL, updated_at=? WHERE id=?",
                 ("" if guard.text_hold(summary) else summary, now, chunk_id))
    store.set_meta(conn, "extract_prompt", PROMPT_VERSION)
    conn.commit()
    return count


def reset_extraction(conn: sqlite3.Connection, path_prefix: str | None) -> int:
    """Пометить фрагменты для повторного извлечения (например, после правки промпта)."""
    where, params = "", ()
    if path_prefix:
        where, params = " WHERE path LIKE ?", (path_prefix.rstrip("%") + "%",)
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks" + where, params)]
    for cid in ids:
        store.delete_chunk_derived(conn, cid)
    conn.execute("UPDATE chunks SET status='pending', attempts=0, error=NULL" + where, params)
    if path_prefix:
        store.delete_facts(conn, "origin='v3_import' AND path LIKE ?", params)
        conn.execute("DELETE FROM hypotheses_raw WHERE chunk_id IS NULL AND path LIKE ?", params)
    else:
        store.delete_facts(conn, "origin='v3_import'")
        conn.execute("DELETE FROM hypotheses_raw WHERE chunk_id IS NULL")
    conn.commit()
    return len(ids)


# --- импорт v3 ------------------------------------------------------------------

_V3_KIND = {
    "fact": "fact", "goal": "goal", "preference": "preference", "value": "value", "constraint": "constraint",
    "decision": "decision", "event": "event", "experience": "event", "strategy": "strategy",
    "pattern_evidence": "fact", "open_loop": "open_loop", "strength": "strength", "concern": "concern",
    "context": "fact",
}


def import_v3(conn: sqlite3.Connection, v3_db: Path, notes: list[Note], cfg: Config) -> dict[str, int]:
    """Перенести готовые извлечения v3 для заметок, не изменившихся с тех пор.
    Экономит часы первого прогона; качество — как у v3 (без дат и субъекта)."""
    src = sqlite3.connect(f"file:{v3_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    by_path = {n.path: n for n in notes if not n.hold_reason}
    stats = {"notes": 0, "facts": 0, "skipped_changed": 0, "skipped_incomplete": 0}
    now = iso_now()
    try:
        v3_sources = {r["path"]: r for r in src.execute("SELECT path, sha256, status FROM sources")}
        for path, note in by_path.items():
            v3 = v3_sources.get(path)
            if v3 is None or v3["status"] == "security_hold":
                continue
            if v3["sha256"] != note.sha256:
                stats["skipped_changed"] += 1
                continue
            pending = store.scalar(conn, "SELECT COUNT(*) FROM chunks WHERE path=? AND status='pending'", (path,))
            total = store.scalar(conn, "SELECT COUNT(*) FROM chunks WHERE path=?", (path,))
            if not total or pending != total:
                continue  # уже извлечено v4 или нечего импортировать
            v3_chunks = src.execute("SELECT line_start, line_end, status, result_json FROM chunks WHERE path=? "
                                    "ORDER BY chunk_idx", (path,)).fetchall()
            if not v3_chunks or any(c["status"] != "done" for c in v3_chunks):
                stats["skipped_incomplete"] += 1
                continue
            reliable = note.date_source in RELIABLE_DATE_SOURCES
            project = int(is_project_path(path, cfg))
            for c in v3_chunks:
                r = loads(c["result_json"], {}) or {}
                domain = r.get("domain") if r.get("domain") in DOMAINS else "other"
                ref = source_ref(path, c["line_start"], c["line_end"])
                for f in r.get("facts") or []:
                    if not isinstance(f, dict) or not str(f.get("claim") or "").strip():
                        continue
                    claim = str(f["claim"]).strip()
                    quote = str(f.get("evidence_quote") or "").strip()
                    if guard.text_hold(claim + " " + quote):
                        continue
                    kind = _V3_KIND.get(str(f.get("category")), "fact")
                    status = f.get("intent_status") if f.get("intent_status") in INTENT_STATUSES else "unclear"
                    if kind not in GOAL_LIKE:
                        status = "none"
                    match = quote_match(quote, note.content) if quote else "none"
                    time_ref = normalize_time_ref(str(f.get("time_hint") or ""))
                    cur = conn.execute(
                        "INSERT INTO facts(chunk_id, path, source_ref, origin, claim, kind, domain, subject, "
                        "explicitness, intent_status, event_date, note_date, date_reliable, evidence_quote, "
                        "quote_match, sensitivity, is_project, created_at) "
                        "VALUES(NULL,?,?,'v3_import',?,?,?,'self',?,?,?,?,?,?,?,'normal',?,?)",
                        (path, ref, claim, kind, domain,
                         "explicit" if f.get("explicitness") == "explicit" else "inferred", status,
                         time_ref or (note.note_date if reliable else None), note.note_date,
                         int(bool(time_ref or reliable)), quote if match != "none" else "", match, project, now))
                    store.fts_put_fact(conn, int(cur.lastrowid), claim)
                    stats["facts"] += 1
                for p in r.get("pattern_candidates") or []:
                    if isinstance(p, dict) and p.get("hypothesis") and p.get("evidence_quote"):
                        conn.execute("INSERT INTO hypotheses_raw(chunk_id, path, source_ref, hypothesis, "
                                     "evidence_quote, alternative, created_at) VALUES(NULL,?,?,?,?,?,?)",
                                     (path, ref, str(p["hypothesis"]), str(p["evidence_quote"]),
                                      str(p.get("alternative_explanation") or ""), now))
            conn.execute("UPDATE chunks SET status='imported', updated_at=? WHERE path=?", (now, path))
            stats["notes"] += 1
            conn.commit()
    finally:
        src.close()
    return stats
