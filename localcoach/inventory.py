"""Life Reset: реестр накопленного (Life Inventory) и его разбор (triage).

Пункты собираются кодом из уже извлечённой памяти: цели, незакрытые дела, тревоги,
решения в процессе и всё из папок проектов и идей. Группа = кластер похожих фактов
или одна проектная заметка (заметка-список вроде «Идеи бизнеса» делится по темам). Модель только называет группу и определяет тип; статус
всегда ставит человек – до разбора каждый пункт «не разобран», а то, что видно по
памяти («в заметках отмечено как сделанное»), показывается подсказкой.

Пункт живёт по смыслу: на повторной сборке группы сверяются с пунктами по вектору,
поэтому статусы и журнал решений переживают переизвлечение заметок.
"""
from __future__ import annotations

import base64
import math
import re
import sqlite3
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from . import guard, store, timeline, vectors
from .config import (DOMAIN_TITLES, LR_CLOSED, LR_OPEN, LR_PARKED, LR_STATUS_TITLES, LR_STATUSES, LR_TYPE_TITLES,
                     LR_TYPES, PRIVATE_DOMAINS, Config)
from .ollama import Ollama, OllamaError, TruncatedError, embed_doc_text
from .prompts import LR_ANSWER_SCHEMA, LR_ANSWER_SYSTEM, LR_GROUP_SCHEMA, LR_GROUP_SYSTEM
from .util import Progress, digest, dumps, fmt_duration, iso_now, loads, normalize_time_ref, today, warn

LR_VERSION = "1"            # версия промпта группы: смена – группы называются заново
MATCH = 0.86                # та же тема, что уже есть в реестре (косинус названия и сути)
RELATED = 0.62              # «похоже на» в карточке
BATCH = 10
OLD_CONCERN_MONTHS = 24     # одиночная давняя тревога из одной заметки – не пункт реестра
# Заметка-список (по названию): каждая тема в ней – отдельный пункт. Остальные проектные заметки –
# один пункт, сколько бы тем в них ни было: в заметке о проекте фактов о разном всегда много.
LIST_NOTE = re.compile(r"(?:^|[^\wё])(идеи|ideas|список|списки|list|backlog|бэклог|хотелки|wishlist|мечты)(?:[^\wё]|$)", re.I)
DEFAULT_CAPACITY = 0        # 0 – не задано: спросить при первом «активно»
# Не считаются в ёмкость: это поддержка и справка, а не направления, на которые тратится внимание.
CAPACITY_EXEMPT = ("routine", "material", "experience")
REVIEW_MONTHS = {"someday": 6, "incubating": 3, "waiting": 1}
KEEP_BACKUPS = 5

SHORTCUTS = {
    "а": "active", "и": "incubating", "к": "someday", "ж": "waiting", "п": "delegated",
    "г": "completed", "о": "abandoned", "х": "archived", "у": "clarification", "н": "dismissed",
}
SHORTCUT_HELP = ("а – активно, и – инкубатор (развивать без обязательств), к – когда-нибудь, ж – жду, п – передал, "
                 "г – уже сделано, о – отпускаю, х – в архив (как история), у – уточнить позже, н – это не пункт, "
                 "= N – то же, что пункт N; Enter – пропустить, «стоп» – закончить")
STOP = re.compile(r"^(стоп|хватит|/stop|/exit|q|выход)$", re.I)

# Вопросы по типу: из концепции, код, а не модель – чтобы карточка была предсказуемой.
QUESTIONS = {
    "project": ("Тебе всё ещё интересна сама идея?", "Есть ли у проекта место среди твоих нынешних приоритетов?",
                "Что произойдёт, если ты окончательно от него откажешься?"),
    "idea": ("Цепляет ли эта идея сейчас – или это интерес прежнего тебя?",
             "Что в ней ценного, даже если не делать её целиком?",
             "Проверить её небольшим шагом или спокойно отпустить?"),
    "wish": ("Хочется ли этого сейчас, или это старое желание, которое просто осталось в списке?",
             "Что изменится в жизни, если оно сбудется?", "Что мешало до сих пор?"),
    "commitment": ("Перед кем это обязательство – перед другим человеком или перед собой?",
                   "Оно ещё актуально?", "Выполнить, пересмотреть договорённость или честно закрыть?"),
    "question": ("Нужно ли решать это сейчас?", "Чего не хватает для решения?",
                 "Если не решать – когда к нему вернуться?"),
    "situation": ("Это всё ещё занимает мысли?", "Хочется что-то с этим сделать – или достаточно осмыслить и принять?"),
    "routine": ("Это всё ещё нужно делать?", "Как сделать, чтобы это шло само, без постоянного напоминания?"),
    "material": ("Нужно ли это для нынешних дел?", "Оставить как справку или отпустить?"),
    "experience": ("Что этот опыт тебе дал?", "Есть ли в нём урок, который стоит сохранить?"),
}


# --- сбор групп из памяти ----------------------------------------------------------------

def _date(f: sqlite3.Row) -> str | None:
    return f["event_date"] or (f["note_date"] if f["date_reliable"] else None)


def candidate_facts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    private = ",".join(f"'{d}'" for d in PRIVATE_DOMAINS)
    return conn.execute(f"""
        SELECT f.id, f.claim, f.kind, f.domain, f.intent_status, f.event_date, f.note_date, f.date_reliable, f.path,
               f.source_ref, f.evidence_quote, f.cluster_id, f.is_project, f.sensitivity, f.origin,
               n.title AS note_title, n.mtime, c.currency
        FROM facts f LEFT JOIN notes n ON n.path=f.path LEFT JOIN clusters c ON c.id=f.cluster_id
        WHERE f.user_state!='rejected' AND f.subject='self' AND f.domain NOT IN ({private})
          AND (f.path IS NULL OR n.status='indexed')
          AND (f.kind IN ('goal', 'open_loop', 'concern') OR f.is_project=1
               OR (f.kind='decision' AND f.intent_status IN ('wanted', 'planned', 'in_progress', 'unclear')))
        ORDER BY f.id""").fetchall()


def groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Группы фактов – кандидаты в пункты. Проектная заметка – один пункт (если это не список идей);
    остальное – по кластерам похожих фактов."""
    facts = candidate_facts(conn)
    by_path: dict[str, list[sqlite3.Row]] = {}
    for f in facts:
        if f["is_project"] and f["path"]:
            by_path.setdefault(f["path"], []).append(f)
    list_notes = {p for p, fs in by_path.items() if LIST_NOTE.search(fs[0]["note_title"] or Path(p).stem)}
    buckets: dict[str, list[sqlite3.Row]] = {}
    for f in facts:
        if f["path"] in list_notes and f["kind"] not in ("goal", "open_loop", "concern", "decision"):
            continue      # в заметке-списке пункты – сами идеи и дела, а не попутные сведения
        if f["is_project"] and f["path"] and f["path"] not in list_notes:
            key = f"note:{f['path']}"
        elif f["cluster_id"] is not None:
            key = f"cluster:{f['cluster_id']}"
        else:
            key = f"fact:{f['id']}"
        buckets.setdefault(key, []).append(f)
    out = []
    for key, fs in buckets.items():
        g = _group(key, fs)
        if g:
            out.append(g)
    out.sort(key=lambda g: (-g["notes_count"], -g["mentions"], g["key"]))
    return out


def _group(key: str, fs: list[sqlite3.Row]) -> dict[str, Any] | None:
    dated = sorted(fs, key=lambda f: _date(f) or "", reverse=True)
    dates = [d for d in (_date(f) for f in fs) if d]
    paths = sorted({f["path"] for f in fs if f["path"]})
    mtimes = [f["mtime"] for f in fs if f["mtime"]]
    touched = date.fromtimestamp(max(mtimes)).isoformat() if mtimes else None
    kinds = Counter(f["kind"] for f in fs)
    last = max(dates) if dates else None
    past = [d for d in dates if d[:10] <= today()]
    last_seen = max(past) if past else None
    age = timeline.months_ago(last)
    if set(kinds) == {"concern"} and len(paths) <= 1 and (age is None or age > OLD_CONCERN_MONTHS):
        return None
    seen, claims = set(), []
    for f in dated:
        c = f["claim"].strip()
        if c.casefold() in seen:
            continue
        seen.add(c.casefold())
        claims.append(f)
    titles = []
    for f in fs:
        t = f["note_title"] or ("разговор" if f["origin"] in ("chat", "session") else "")
        if t and t not in titles:
            titles.append(t)
    intents = [f["intent_status"] for f in dated if f["intent_status"] not in ("none", "unclear")]
    domains = Counter(f["domain"] for f in fs)
    return {
        "key": key, "anchors": [key] if key.startswith("note:") else [], "fact_ids": [f["id"] for f in fs], "claims": claims[:6], "paths": paths, "titles": titles[:4],
        "first_date": min(dates) if dates else None, "last_date": last, "last_seen": last_seen, "touched": touched, "mentions": len(fs),
        "notes_count": len(paths) or 1, "kinds": kinds, "intents": intents, "domain": domains.most_common(1)[0][0],
        "sensitive": any(f["sensitivity"] == "sensitive" for f in fs),
        "currency": {f["currency"] for f in fs if f["currency"]},
        "fingerprint": digest([LR_VERSION, sorted(f["claim"] for f in fs)[:20], titles[:4]]),
    }


def hint(g: dict[str, Any]) -> str:
    """Что видно по памяти – для карточки. Это подсказка, а не решение."""
    out = []
    if g["intents"]:
        latest = g["intents"][0]
        out.append({"completed": "в заметках отмечено как сделанное", "abandoned": "в заметках ты от этого отказывался",
                    "in_progress": "в заметках – в работе", "planned": "в заметках был план или срок"}.get(latest, ""))
    if g["currency"] == {"past"}:
        out.append("по сверке с биографией – в прошлом")
    age = timeline.months_ago(g["last_date"])
    if age is not None and age >= 36:
        out.append(f"последнее упоминание {timeline.age_text(age)}")
    return "; ".join(x for x in out if x)


def _claim_text(claim: str) -> str:
    return re.sub(r"^Автор[:,]?\s+", "", claim.strip()).rstrip(".")


def _group_line(gid: str, g: dict[str, Any]) -> str:
    span = g["first_date"][:4] if g["first_date"] else "дата неизвестна"
    if g["last_date"] and g["first_date"] and g["last_date"][:4] != g["first_date"][:4]:
        span += f"-{g['last_date'][:4]}"
    head = f"{gid} | заметки: " + (", ".join(f"«{t}»" for t in g["titles"]) or "–") + f" | {span}, упоминаний: {g['mentions']}"
    lines = [head]
    for f in g["claims"]:
        when = (_date(f) or "")[:7]
        tag = f["kind"] + (f", {f['intent_status']}" if f["intent_status"] not in ("none", "") else "")
        lines.append(f"  - ({when or '?'}) {f['claim'][:300]} [{tag}]")
    return "\n".join(lines)


def _name_batch(llm: Ollama, cfg: Config, batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Название, тип и суть групп одним вызовом. Обрезанный ответ – делим пакет пополам."""
    ids = {f"G{i + 1}": g for i, g in enumerate(batch)}
    user = "Группы:\n\n" + "\n\n".join(_group_line(gid, g) for gid, g in ids.items())
    try:
        raw = llm.chat_json(LR_GROUP_SYSTEM, user, LR_GROUP_SCHEMA, num_predict=350 * len(batch) + 400, attempts=2,
                            heavy=True)
    except TruncatedError:
        if len(batch) == 1:
            # Название кодом: иначе группа не кэшируется и уходит модели на каждом update.
            return {batch[0]["key"]: _fallback_name(batch[0])}
        half = len(batch) // 2
        return {**_name_batch(llm, cfg, batch[:half]), **_name_batch(llm, cfg, batch[half:])}
    out: dict[str, dict[str, Any]] = {}
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        g = ids.get(str(it.get("id") or "").strip().upper())
        if g is None:
            continue
        title = str(it.get("title") or "").strip().strip("«»\"' ")
        same = ids.get(str(it.get("same_as") or "").strip().upper())
        out[g["key"]] = {
            "title": title[:120], "type": it.get("type") if it.get("type") in LR_TYPES else "idea",
            "summary": str(it.get("summary") or "").strip()[:400], "want": str(it.get("want") or "").strip()[:200],
            "same_as": same["fingerprint"] if same is not None and same is not g else "",
            "not_item": bool(it.get("not_item")) or len(title) < 2,
        }
    return out


FALLBACK_TYPE = {"goal": "wish", "open_loop": "question", "decision": "question", "concern": "situation"}


def is_single(g: dict[str, Any]) -> bool:
    """Один факт из одной заметки и не проектная заметка целиком."""
    return g["mentions"] == 1 and g["notes_count"] <= 1 and not g["key"].startswith("note:")


def _fallback_name(g: dict[str, Any]) -> dict[str, Any]:
    first = g["claims"][0] if g["claims"] else None
    text = _claim_text(first["claim"]) if first is not None else (g["titles"][0] if g["titles"] else "Без названия")
    words = text.split()
    title = " ".join(words[:7]) + ("…" if len(words) > 7 else "")
    type_ = "project" if g["key"].startswith("note:") else FALLBACK_TYPE.get(first["kind"] if first is not None else "", "idea")
    return {"title": title[:120], "type": type_, "summary": text[:400], "want": "", "same_as": "", "not_item": False,
            "fallback": True}


def _vec_text(title: str, summary: str) -> str:
    return embed_doc_text(f"{title}. {summary}".strip())


def _vectors(conn: sqlite3.Connection, llm: Ollama, cfg: Config, texts: list[str]) -> list[list[float]]:
    """Векторы с кэшем по тексту: на каждом update названия те же, и считать их заново незачем
    (без ПК это заметная работа процессора сервера)."""
    keys = [digest([cfg.embed_model, t]) for t in texts]
    out: dict[str, list[float]] = {}
    for k in set(keys):
        hit = store.cache_any(conn, "lr_vec", k)
        if hit and hit.get("v"):
            v = vectors.unpack(base64.b64decode(hit["v"]))
            if v is not None:
                out[k] = list(v)
    missing = [(k, t) for k, t in dict(zip(keys, texts)).items() if k not in out]
    for i in range(0, len(missing), cfg.embed_batch):
        part = missing[i:i + cfg.embed_batch]
        for (k, _t), v in zip(part, llm.embed([t for _k, t in part])):
            out[k] = vectors.normalize(v)
            conn.execute("INSERT INTO artifacts(stage,key,fingerprint,payload_json,updated_at) VALUES('lr_vec',?,'',?,?) "
                         "ON CONFLICT(stage,key) DO UPDATE SET payload_json=excluded.payload_json",
                         (k, dumps({"v": base64.b64encode(vectors.pack(out[k])).decode()}), iso_now()))
        conn.commit()
    _vectors.last_new = len(missing)
    return [out[k] for k in keys]


_vectors.last_new = 0


def backup(conn: sqlite3.Connection, cfg: Config, label: str) -> Path:
    """Копия базы перед массовыми изменениями реестра; хранятся последние пять."""
    folder = cfg.workspace / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = iso_now()[:19].replace(":", "").replace("-", "").replace("T", "-")
    target = folder / f"coach-{stamp}-{label}.sqlite3"
    dst = sqlite3.connect(str(target))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    for old in sorted(folder.glob("coach-*.sqlite3"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def build(conn: sqlite3.Connection, llm: Ollama, cfg: Config, quiet: bool = False) -> dict[str, int]:
    """Собрать или обновить реестр из памяти. Статусы и решения не меняются; закрытый пункт,
    который снова появился в заметках после решения, помечается «снова всплыло»."""
    say: Callable[[str], None] = (lambda _s: None) if quiet else print
    stats = {"groups": 0, "named": 0, "new": 0, "updated": 0, "skipped": 0, "joined": 0, "resurfaced": 0,
             "embedded": 0}
    # Копия – только при явной сборке: на каждом update пять копий быстро вытеснили бы ту, что до разбора.
    if not quiet and store.scalar(conn, "SELECT COUNT(*) FROM lr_items"):
        path = backup(conn, cfg, "inventory")
        say(f"Копия базы перед сборкой: {path}")
    gs = groups(conn)
    stats["groups"] = len(gs)
    named: dict[str, dict[str, Any]] = {}
    todo = []
    for g in gs:
        if is_single(g):
            # Одиночное упоминание: модели не нужно – факт и так самодостаточное предложение; такие
            # разбираются пачкой списком (sweep), а не карточкой каждое.
            named[g["key"]] = {**_fallback_name(g), "bulk": True}
            stats["single"] = stats.get("single", 0) + 1
            continue
        hit = store.cache_get(conn, "lr_group", g["fingerprint"], LR_VERSION)
        if hit is not None:
            named[g["key"]] = hit
        else:
            todo.append(g)
    if todo:
        batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
        progress = Progress(len(batches), "реестр: называю группы, пакет") if not quiet else None
        for batch in batches:
            started = time.monotonic()
            try:
                res = _name_batch(llm, cfg, batch)
            except OllamaError as exc:
                warn(f"сборка реестра прервана: {exc}. Готовое сохранено, повторный запуск продолжит.")
                break
            for g in batch:
                if g["key"] not in res:
                    # Модель пропустила группу в ответе: название кодом, чтобы не спрашивать её снова на каждом update.
                    res[g["key"]] = _fallback_name(g)
                if g["key"] in res:
                    named[g["key"]] = res[g["key"]]
                    store.cache_put(conn, "lr_group", g["fingerprint"], LR_VERSION, res[g["key"]])
                    stats["named"] += 1
            if len(batch) == BATCH:
                store.set_meta(conn, "lr_sec_per_batch", f"{time.monotonic() - started:.1f}")
            if progress:
                progress.step()
    by_key = {g["key"]: g for g in gs}
    by_fp = {g["fingerprint"]: g["key"] for g in gs}
    # Склейка внутри пакета: «то же, что G3». Цель хранится отпечатком группы, поэтому склейка
    # повторяется и на следующих сборках, пока обе группы на месте.
    for key, n in list(named.items()):
        target = by_fp.get(n.get("same_as") or "")
        if (target and target != key and target in named and not named[target].get("same_as")
                and not named[target].get("not_item") and key in by_key):
            t, g = by_key[target], by_key[key]
            t["fact_ids"] += g["fact_ids"]
            t["anchors"] = sorted(set(t["anchors"]) | set(g["anchors"]))
            t["paths"] = sorted(set(t["paths"]) | set(g["paths"]))
            t["mentions"] += g["mentions"]
            t["notes_count"] = len(t["paths"]) or 1
            dates = [d for d in (t["first_date"], g["first_date"], t["last_date"], g["last_date"]) if d]
            t["first_date"], t["last_date"] = (min(dates), max(dates)) if dates else (None, None)
            t["touched"] = max(x for x in (t["touched"], g["touched"], "") if x is not None) or None
            t["last_seen"] = max(x for x in (t["last_seen"], g["last_seen"], "") if x is not None) or None
            t["sensitive"] = t["sensitive"] or g["sensitive"]
            named[key] = {**n, "joined": True}
            stats["joined"] += 1
    ready = [(by_key[k], n) for k, n in named.items()
             if k in by_key and not n.get("not_item") and not n.get("joined")]
    stats["skipped"] = sum(1 for k, n in named.items() if n.get("not_item"))
    if ready:
        try:
            vecs = _vectors(conn, llm, cfg, [_vec_text(n["title"], n["summary"]) for _g, n in ready])
        except OllamaError as exc:
            warn(f"векторы реестра не посчитаны: {exc}. Повторный запуск продолжит.")
            conn.commit()
            return stats
        stats["embedded"] = _vectors.last_new
        # Склеенные вручную тоже сверяются: их группа уходит туда, куда пункт склеен.
        existing = [(r["id"], vectors.unpack(r["embedding"])) for r in conn.execute(
            "SELECT id, embedding FROM lr_items WHERE embedding IS NOT NULL")]
        existing = [(i, v) for i, v in existing if v is not None]
        fresh: set[int] = set()     # пункты, чьи источники уже заменены в этой сборке: дальше – дополняются
        anchored = {a: r["id"] for r in conn.execute("SELECT id, anchors_json FROM lr_items")
                    for a in loads(r["anchors_json"], []) or []}
        for (g, n), v in zip(ready, vecs):
            best_id, best = None, 0.0
            hit = next((anchored[a] for a in g["anchors"] if a in anchored), None)
            if hit is not None:
                best_id, best = hit, 1.0
            else:
                for iid, ev in existing:
                    s = vectors.dot(v, ev)
                    if s > best:
                        best_id, best = iid, s
            if best_id is not None and best >= MATCH:
                best_id = _resolve(conn, best_id)
                if _refresh(conn, best_id, g, replace=best_id not in fresh):
                    stats["resurfaced"] += 1
                fresh.add(best_id)
                stats["updated"] += 1
            else:
                iid = _insert(conn, g, n, v)
                existing.append((iid, v))
                fresh.add(iid)      # иначе следующая группа той же темы заменила бы только что записанные источники
                stats["new"] += 1
            for a in g["anchors"]:
                anchored.setdefault(a, best_id if best_id is not None and best >= MATCH else iid)
    stats["deduped"] = dedupe(conn)      # одинаковое название – одна тема, даже если описания разошлись
    store.set_meta(conn, "lr_built_at", iso_now())
    conn.commit()
    return stats


def _resolve(conn: sqlite3.Connection, iid: int) -> int:
    for _ in range(20):
        nxt = store.scalar(conn, "SELECT merged_into FROM lr_items WHERE id=?", (iid,))
        if nxt is None:
            return iid
        iid = int(nxt)
    return iid


def source_sig(conn: sqlite3.Connection, paths: list[str]) -> str | None:
    """Отпечаток содержимого проектных заметок пункта. Дата изменения файла не годится: синхронизация
    vault может её сбросить, и тогда всё закрытое «всплыло» бы разом."""
    if not paths:
        return None
    marks = ",".join("?" * len(paths))
    rows = conn.execute(f"SELECT n.path, n.sha256 FROM notes n WHERE n.path IN ({marks}) AND EXISTS "
                        f"(SELECT 1 FROM facts f WHERE f.path=n.path AND f.is_project=1) ORDER BY n.path",
                        paths).fetchall()
    return digest([[r["path"], r["sha256"]] for r in rows]) if rows else None


def _insert(conn: sqlite3.Connection, g: dict[str, Any], n: dict[str, Any], v: list[float]) -> int:
    now = iso_now()
    cur = conn.execute(
        "INSERT INTO lr_items(title, type, status, domain, summary, want, hint, origin, sensitive, first_date, last_date, "
        "touched, mentions, notes_count, fact_ids_json, paths_json, anchors_json, embedding, bulk, created_at, updated_at) "
        "VALUES(?,?,'inbox',?,?,?,?,'memory',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (n["title"], n["type"], g["domain"], n["summary"], n["want"], hint(g), int(g["sensitive"]), g["first_date"],
         g["last_date"], g["touched"], g["mentions"], g["notes_count"], dumps(g["fact_ids"]), dumps(g["paths"]),
         dumps(g["anchors"]), vectors.pack(v), int(bool(n.get("bulk"))), now, now))
    return int(cur.lastrowid)


def _refresh(conn: sqlite3.Connection, iid: int, g: dict[str, Any], replace: bool = True) -> bool:
    """Обновить источники пункта по нынешней памяти (replace – первая группа этой сборки: старые id
    фактов после переизвлечения уже недействительны). True – если закрытый пункт снова всплыл."""
    r = conn.execute("SELECT * FROM lr_items WHERE id=?", (iid,)).fetchone()
    keep_f = set() if replace else set(loads(r["fact_ids_json"], []) or [])
    keep_p = set() if replace else set(loads(r["paths_json"], []) or [])
    facts = sorted(keep_f | set(g["fact_ids"]))
    paths = sorted(keep_p | set(g["paths"]))
    dates = [d for d in (r["first_date"], r["last_date"], g["first_date"], g["last_date"]) if d]
    last = max(dates) if dates else None
    closed = r["status"] in LR_CLOSED and r["decided_at"] and not r["resurfaced"]
    # Только прошедшие даты: срок «к июню 2027» в заметке – не новое упоминание, иначе пункт всплывал бы
    # на каждой сборке до июня 2027.
    later = bool(g["last_seen"] and g["last_seen"][:10] > (r["decided_at"] or "")[:10])
    edited = bool(r["src_sig"] and source_sig(conn, paths) not in (None, r["src_sig"]))
    again = bool(closed and (later or edited))
    touched = max((x for x in (r["touched"], g["touched"]) if x), default=None)
    anchors = sorted(set(loads(r["anchors_json"], []) or []) | set(g["anchors"]))
    conn.execute("UPDATE lr_items SET fact_ids_json=?, paths_json=?, anchors_json=?, first_date=?, last_date=?, touched=?, "
                 "mentions=?, notes_count=?, hint=?, resurfaced=MAX(resurfaced, ?), sensitive=MAX(sensitive, ?), "
                 "updated_at=? WHERE id=?",
                 (dumps(facts), dumps(paths), dumps(anchors), min(dates) if dates else None, last, touched, len(facts), len(paths) or 1,
                  hint(g) if r["origin"] == "memory" else r["hint"], int(again), int(g["sensitive"]), iso_now(), iid))
    return again


# --- быстрый захват ----------------------------------------------------------------------------

def _norm_title(text: str) -> str:
    return re.sub(r"[^\wё]+", " ", text.casefold().replace("ё", "е")).strip()


def find_same_title(conn: sqlite3.Connection, text: str) -> int | None:
    key = _norm_title(text)
    for r in conn.execute("SELECT id, title FROM lr_items WHERE merged_into IS NULL"):
        if _norm_title(r["title"]) == key:
            return int(r["id"])
    return None


def capture(conn: sqlite3.Connection, llm: Ollama | None, text: str, type_: str = "idea", origin: str = "chat",
            check_dup: bool = True) -> int | None:
    """Новая идея из разговора – в реестр «не разобрано», а не сразу в активные проекты.
    Тот же текст второй раз нового пункта не заводит: вернётся уже записанный (см. find_same_title)."""
    text = text.strip().rstrip(".")
    if len(text) < 3 or guard.text_hold(text):
        return None
    same = find_same_title(conn, text) if check_dup else None
    if same is not None:
        return same
    now = iso_now()
    vec = None
    if llm is not None:
        try:
            vec = vectors.pack(vectors.normalize(llm.embed([_vec_text(text, "")])[0]))
        except OllamaError:
            pass
    cur = conn.execute("INSERT INTO lr_items(title, type, status, summary, origin, first_date, last_date, mentions, "
                       "notes_count, embedding, created_at, updated_at) VALUES(?,?,'inbox','',?,?,?,1,0,?,?,?)",
                       (text[:120], type_ if type_ in LR_TYPES else "idea", origin, today(), today(), vec, now, now))
    conn.commit()
    return int(cur.lastrowid)


# --- статус и решения --------------------------------------------------------------------------

def code(iid: int) -> str:
    return f"LR-{iid}"


def capacity(conn: sqlite3.Connection) -> int:
    try:
        return int(store.get_meta(conn, "lr_capacity") or DEFAULT_CAPACITY)
    except ValueError:
        return DEFAULT_CAPACITY


def active_count(conn: sqlite3.Connection, exclude: int | None = None) -> int:
    marks = ",".join("?" * len(CAPACITY_EXEMPT))
    return store.scalar(conn, f"SELECT COUNT(*) FROM lr_items WHERE status='active' AND merged_into IS NULL "
                              f"AND type NOT IN ({marks}) AND id!=?", (*CAPACITY_EXEMPT, exclude or 0)) or 0


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    day = min(d.day, 28)
    return date(d.year + y, m + 1, day)


_IN = re.compile(r"через\s+(\d+|полгода|год|месяц|неделю|пару\s+месяцев)\s*(дн|день|нед|мес|год|лет)?", re.I)


def parse_review(text: str) -> str | None:
    """«2027-05», «через 3 мес», «через полгода», «через год» → дата пересмотра."""
    text = text.strip()
    if not text:
        return None
    norm = normalize_time_ref(text)
    if norm and re.fullmatch(r"\d{4}(-\d{2}){0,2}", norm):
        return norm + {4: "-01-01", 7: "-01"}.get(len(norm), "")
    m = _IN.search(text)
    if not m:
        return None
    n, unit = m.group(1).casefold(), (m.group(2) or "").casefold()
    base = date.fromisoformat(today())
    if n == "полгода":
        return add_months(base, 6).isoformat()
    if n == "год":
        return add_months(base, 12).isoformat()
    if n == "месяц":
        return add_months(base, 1).isoformat()
    if n == "неделю":
        return (base + timedelta(days=7)).isoformat()
    if n.startswith("пару"):
        return add_months(base, 2).isoformat()
    k = int(n)
    if unit.startswith(("дн", "день")):
        return (base + timedelta(days=k)).isoformat()
    if not unit:
        return None      # «через 10» – непонятно чего; лучше спросить, чем угадать месяцы
    if unit.startswith("нед"):
        return (base + timedelta(days=7 * k)).isoformat()
    if unit.startswith(("год", "лет")):
        return add_months(base, 12 * k).isoformat()
    return add_months(base, k).isoformat()


def default_review(status: str) -> str | None:
    months = REVIEW_MONTHS.get(status)
    return add_months(date.fromisoformat(today()), months).isoformat() if months else None


def decide(conn: sqlite3.Connection, iid: int, status: str, *, reason: str = "", answer: str = "", proposal: str = "",
           next_step: str | None = None, review_on: str | None = None) -> None:
    """Записать решение человека и строку журнала. Пересмотр у «когда-нибудь» обязателен:
    без срока пункт становится бездонным складом."""
    if status not in LR_STATUSES:
        raise ValueError(status)
    r = conn.execute("SELECT status, next_step, paths_json FROM lr_items WHERE id=?", (iid,)).fetchone()
    if r is None:
        return
    if status in LR_PARKED and not review_on:
        review_on = default_review(status)
    if status not in LR_PARKED:
        review_on = None
    now = iso_now()
    conn.execute("UPDATE lr_items SET status=?, review_on=?, next_step=?, resurfaced=0, bulk=0, src_sig=?, decided_at=?, "
                 "updated_at=? WHERE id=?", (status, review_on, r["next_step"] if next_step is None else next_step.strip()[:300],
                                             source_sig(conn, loads(r["paths_json"], []) or []), now, now, iid))
    conn.execute("INSERT INTO lr_decisions(item_id, from_status, to_status, proposal, answer, reason, created_at) "
                 "VALUES(?,?,?,?,?,?,?)", (iid, r["status"], status, proposal[:300], answer[:1000], reason.strip()[:400], now))
    conn.commit()


_CONTENT_COLS = ("fact_ids_json", "paths_json", "anchors_json", "mentions", "notes_count", "first_date", "last_date")
_MERGE_COLS = _CONTENT_COLS + ("updated_at",)


def merge(conn: sqlite3.Connection, iid: int, into: int, snap: dict[str, Any] | None = None) -> bool:
    """«Это то же, что пункт N»: источники переходят туда, сам пункт скрывается.
    Если передан snap, в него кладётся то, что нужно, чтобы расклеить (см. unmerge)."""
    a = conn.execute("SELECT * FROM lr_items WHERE id=?", (iid,)).fetchone()
    b = conn.execute("SELECT * FROM lr_items WHERE id=? AND merged_into IS NULL", (into,)).fetchone()
    if a is None or b is None or iid == into:
        return False
    if snap is not None:
        snap.update({"item": iid, "into": into, "target": {c: b[c] for c in _MERGE_COLS}, "updated_at": a["updated_at"]})
    facts = sorted(set(loads(a["fact_ids_json"], []) or []) | set(loads(b["fact_ids_json"], []) or []))
    paths = sorted(set(loads(a["paths_json"], []) or []) | set(loads(b["paths_json"], []) or []))
    dates = [d for d in (a["first_date"], b["first_date"], a["last_date"], b["last_date"]) if d]
    now = iso_now()
    anchors = sorted(set(loads(a["anchors_json"], []) or []) | set(loads(b["anchors_json"], []) or []))
    conn.execute("UPDATE lr_items SET fact_ids_json=?, paths_json=?, anchors_json=?, mentions=?, notes_count=?, "
                 "first_date=?, last_date=?, updated_at=? WHERE id=?",
                 (dumps(facts), dumps(paths), dumps(anchors), len(facts), len(paths) or 1,
                  min(dates) if dates else None, max(dates) if dates else None, now, into))
    conn.execute("UPDATE lr_items SET merged_into=?, updated_at=? WHERE id=?", (into, now, iid))
    conn.execute("INSERT INTO lr_decisions(item_id, from_status, to_status, reason, created_at) VALUES(?,?,?,?,?)",
                 (iid, a["status"], "merged", f"то же, что {code(into)}", now))
    if snap is not None:
        after = conn.execute("SELECT * FROM lr_items WHERE id=?", (into,)).fetchone()
        snap["after"] = {c: after[c] for c in _CONTENT_COLS}
        snap["decision_id"] = conn.execute("SELECT id FROM lr_decisions WHERE item_id=? ORDER BY id DESC LIMIT 1",
                                           (iid,)).fetchone()[0]
    conn.commit()
    return True


def unmerge(conn: sqlite3.Connection, snap: dict[str, Any]) -> bool:
    """Расклеить: пункт снова виден, источники цели такие, какими были до склейки. Только пока цель после
    этой склейки не менялась (иначе потерялись бы чужие правки): тогда False."""
    b = conn.execute("SELECT * FROM lr_items WHERE id=? AND merged_into IS NULL", (snap["into"],)).fetchone()
    a = conn.execute("SELECT merged_into FROM lr_items WHERE id=?", (snap["item"],)).fetchone()
    if b is None or a is None or a["merged_into"] != snap["into"]:
        return False
    last = conn.execute("SELECT id FROM lr_decisions WHERE item_id=? ORDER BY id DESC LIMIT 1", (snap["item"],)).fetchone()
    if last is None or last[0] != snap["decision_id"]:
        return False
    if any(b[c] != snap["after"][c] for c in _CONTENT_COLS):
        return False          # к цели после этой склейки что-то добавили: откат стёр бы чужое
    cols = snap["target"]
    conn.execute(f"UPDATE lr_items SET {', '.join(c + '=?' for c in _MERGE_COLS)} WHERE id=?",
                 (*[cols[c] for c in _MERGE_COLS], snap["into"]))
    conn.execute("UPDATE lr_items SET merged_into=NULL, updated_at=? WHERE id=?", (snap["updated_at"], snap["item"]))
    conn.execute("DELETE FROM lr_decisions WHERE id=?", (snap["decision_id"],))
    conn.commit()
    return True


DEDUPE_MIN_CHARS = 8        # короче – слишком общее название («Идеи», «Планы»): склеивать нельзя


def dedupe(conn: sqlite3.Connection, snaps: list[dict[str, Any]] | None = None) -> int:
    """Склеить пункты, у которых название совпадает дословно (без регистра и знаков). Склеиваются только
    неразобранные: в уже решённый пункт с тем же названием (его решение остаётся) или в самый «весомый»
    из неразобранных. Между собой решённые не склеиваются. Возвращает число склеенных."""
    by_title: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM lr_items WHERE merged_into IS NULL"):
        key = _norm_title(r["title"])
        if len(key) >= DEDUPE_MIN_CHARS:
            by_title.setdefault(key, []).append(r)
    n = 0
    for rows in by_title.values():
        if len(rows) < 2:
            continue
        open_ = [r for r in rows if r["status"] in LR_OPEN]
        decided = [r for r in rows if r["status"] not in LR_OPEN]
        if not open_:
            continue
        if decided:
            target = max(decided, key=lambda r: (r["decided_at"] or "", r["id"]))
            sources_ = open_
        else:
            target = max(open_, key=lambda r: (r["notes_count"], r["mentions"], -r["id"]))
            sources_ = [r for r in open_ if r["id"] != target["id"]]
        for r in sources_:
            snap: dict[str, Any] = {}
            if merge(conn, r["id"], target["id"], snap):
                n += 1
                if snaps is not None:
                    snaps.append(snap)
    return n


# --- очередь и карточка ------------------------------------------------------------------------

def _score(r: sqlite3.Row) -> float:
    age = timeline.months_ago(r["last_date"] or r["touched"])
    recent = 3.0 if age is not None and age <= 24 else 0.0
    weight = {"commitment": 3, "question": 2, "situation": 2, "project": 1}.get(r["type"], 0)
    picked = 2 if (r["bulk_rank"] or 0) > 0 else 0      # одиночное, которое модель отобрала как значимое
    return 2 * r["notes_count"] + min(r["mentions"], 10) + recent + weight + picked + (6 if r["resurfaced"] else 0)


def queue(conn: sqlite3.Connection, type_: str | None = None, area: str | None = None) -> list[sqlite3.Row]:
    """Что ждёт решения: сначала то, что пора пересмотреть и что снова всплыло, потом
    неразобранное по тому, сколько внимания оно занимало, в конце – «уточнить»."""
    rows = conn.execute(
        "SELECT * FROM lr_items WHERE merged_into IS NULL AND NOT (bulk=1 AND status='inbox') "
        "AND (status IN ('inbox', 'clarification') "
        "OR (status IN ('someday', 'incubating', 'waiting') AND review_on IS NOT NULL AND review_on<=?) "
        "OR resurfaced=1)" + (" AND type=?" if type_ else "") + (" AND domain=?" if area else ""),
        (today(), *([type_] if type_ else []), *([area] if area else []))).fetchall()

    def order(r: sqlite3.Row) -> tuple:
        due = r["status"] in LR_PARKED or r["resurfaced"]
        return (0 if due else 1 if r["status"] == "inbox" else 2, -_score(r), r["id"])
    return sorted(rows, key=order)


def related(conn: sqlite3.Connection, r: sqlite3.Row, k: int = 2) -> list[tuple[sqlite3.Row, float]]:
    v = vectors.unpack(r["embedding"])
    if v is None:
        return []
    out = []
    for o in conn.execute("SELECT * FROM lr_items WHERE merged_into IS NULL AND status!='dismissed' AND id!=? "
                          "AND embedding IS NOT NULL", (r["id"],)):
        ov = vectors.unpack(o["embedding"])
        s = vectors.dot(v, ov) if ov is not None else 0.0
        if s >= RELATED:
            out.append((o, s))
    return sorted(out, key=lambda x: -x[1])[:k]


def sources(conn: sqlite3.Connection, r: sqlite3.Row, limit: int = 3) -> list[sqlite3.Row]:
    ids = loads(r["fact_ids_json"], []) or []
    if not ids:
        return []
    marks = ",".join("?" * len(ids[:200]))
    rows = conn.execute(f"SELECT f.*, n.title AS note_title FROM facts f LEFT JOIN notes n ON n.path=f.path "
                        f"WHERE f.id IN ({marks})", ids[:200]).fetchall()
    # Сначала то, ради чего пункт в реестре (цели, незакрытые дела), потом свежее.
    rows = sorted(rows, key=lambda f: f["event_date"] or f["note_date"] or "", reverse=True)
    rank = {"goal": 0, "open_loop": 0, "decision": 1, "concern": 1}
    return sorted(rows, key=lambda f: rank.get(f["kind"], 2))[:limit]


def span(r: sqlite3.Row) -> str:
    if not r["first_date"]:
        return f"дат в тексте нет, файл менялся {r['touched'][:7]}" if r["touched"] else "дата неизвестна"
    s = r["first_date"][:4]
    if r["last_date"] and r["last_date"][:4] != s:
        s += f"-{r['last_date'][:4]}"
    age = timeline.months_ago(r["last_date"])
    return s + (f" (последнее упоминание {timeline.age_text(age)})" if age is not None and age >= 12 else "")


def card(conn: sqlite3.Connection, r: sqlite3.Row) -> str:
    lines = [f"[{code(r['id'])}] {r['title']} · {LR_TYPE_TITLES.get(r['type'], r['type'])} · "
             f"{DOMAIN_TITLES.get(r['domain'], r['domain'])}"]
    if r["status"] != "inbox":
        state = LR_STATUS_TITLES[r["status"]]
        if r["status"] in LR_PARKED and r["review_on"]:
            state += f", пересмотр был назначен на {r['review_on']}"
        lines.append(f"Сейчас: {state}")
    if r["resurfaced"]:
        last = conn.execute("SELECT reason, created_at FROM lr_decisions WHERE item_id=? ORDER BY id DESC LIMIT 1",
                            (r["id"],)).fetchone()
        lines.append("Снова появилось в заметках после решения"
                     + (f" от {last['created_at'][:10]}" + (f" («{last['reason']}»)" if last["reason"] else "") if last else ""))
    where = f"{r['notes_count']} {timeline._plural(r['notes_count'], 'заметка', 'заметки', 'заметок')}" \
        if r["origin"] == "memory" else "из разговора"
    lines.append(f"Откуда: {where}, {span(r)}")
    if r["summary"]:
        lines.append(f"Суть: {r['summary']}")
    if r["want"]:
        lines.append(f"Чего хотелось: {r['want']}")
    for f in sources(conn, r):
        when = (f["event_date"] or f["note_date"] or "")[:7]
        src = f"«{f['note_title']}»" if f["note_title"] else "разговор"
        line = f"  - {_claim_text(f['claim'])} ({src}{', ' + when if when else ''})"
        if f["evidence_quote"] and f["quote_match"] in ("exact", "partial"):
            line += f"\n    «{f['evidence_quote'][:160]}»"
        lines.append(line)
    if r["hint"]:
        lines.append(f"По памяти: {r['hint']}")
    if r["next_step"]:
        lines.append(f"Следующий шаг: {r['next_step']}")
    rel = related(conn, r)
    if rel:
        lines.append("Похоже на: " + "; ".join(f"{code(o['id'])} {o['title']} ({LR_STATUS_TITLES[o['status']]})"
                                                for o, _s in rel))
    return "\n".join(lines)


# --- разбор -------------------------------------------------------------------------------------

def _ask(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _interpret(llm: Ollama, card_text: str, answer: str) -> dict[str, str]:
    user = (f"Сегодня: {today()}.\n\nКарточка:\n{card_text}\n\nОтвет человека:\n{answer}")
    try:
        raw = llm.chat_json(LR_ANSWER_SYSTEM, user, LR_ANSWER_SCHEMA, num_predict=300, attempts=2)
    except OllamaError:
        return {"status": "", "reason": "", "next_step": "", "review_on": ""}
    out = {k: str(raw.get(k) or "").strip() for k in ("status", "reason", "next_step", "review_on")}
    if out["status"] not in LR_STATUSES:
        out["status"] = ""
    return out


def _confirm_active(conn: sqlite3.Connection, r: sqlite3.Row) -> bool:
    """Защита от снежного кома: новые активные – с оглядкой на личную ёмкость."""
    if r["type"] in CAPACITY_EXEMPT:
        return True
    cap = capacity(conn)
    if not cap:
        ans = _ask("Сколько направлений одновременно ты реально тянешь? Не «сколько хочется», а сколько получает "
                   "регулярное внимание. Число (Enter – спросить потом): ")
        if ans and ans.isdigit() and int(ans) > 0:
            store.set_meta(conn, "lr_capacity", ans)
            conn.commit()
            cap = int(ans)
    n = active_count(conn, exclude=r["id"])
    if cap and n >= cap:
        names = [x["title"] for x in conn.execute(
            "SELECT title FROM lr_items WHERE status='active' AND merged_into IS NULL AND id!=? ORDER BY decided_at",
            (r["id"],)).fetchall()][:8]
        print(f"Активных уже {n} при твоей ёмкости {cap}: " + "; ".join(names) + ".")
        ans = _ask("Всё равно сделать активным? «д» – да, Enter – нет (выбери другой статус): ")
        return bool(ans) and ans.casefold() in ("д", "да", "y")
    return True


def record_clarification(conn: sqlite3.Connection, r: sqlite3.Row, ans: str) -> None:
    """В ответе не нашлось решения: ответ сохраняется, пункт остаётся «уточнить»."""
    conn.execute("UPDATE lr_items SET status='clarification', bulk=0, updated_at=? WHERE id=?", (iso_now(), r["id"]))
    conn.execute("INSERT INTO lr_decisions(item_id, from_status, to_status, answer, created_at) "
                 "VALUES(?,?,?,?,?)", (r["id"], r["status"], "clarification", ans[:1000], iso_now()))
    conn.commit()


def _triage_one(conn: sqlite3.Connection, llm: Ollama, r: sqlite3.Row) -> str:
    """Один пункт карточкой: «done» – решение записано, «skip» – пропущен, «stop» – человек закончил."""
    shown = card(conn, r)
    print(shown)
    print("\n" + "\n".join(f" {i}. {qq}" for i, qq in enumerate(QUESTIONS.get(r["type"], ()), 1)))
    ans = _ask("> ")
    while ans == "?":
        print(SHORTCUT_HELP + "\n")
        ans = _ask("> ")
    if ans is None or STOP.match(ans or ""):
        return "stop"
    if not ans:
        print("Пропустил.\n")
        return "skip"
    m = re.fullmatch(r"=\s*(?:LR-)?(\d+)", ans, re.I)
    if m:
        if merge(conn, r["id"], int(m.group(1))):
            print("Склеил с " + code(int(m.group(1))) + ".\n")
            return "done"
        print("Нет такого пункта (или это он сам).\n")
        return "skip"
    status, reason, step, review, proposal, answer = "", "", "", "", "", ""
    letter = ans.casefold().rstrip(".")
    if letter in SHORTCUTS:
        status = SHORTCUTS[letter]
    else:
        if guard.text_hold(ans):
            print("Не записал: похоже на пароль или токен.\n")
            return "skip"
        answer = ans
        got = _interpret(llm, shown, ans)
        if not got["status"]:
            record_clarification(conn, r, ans)
            print("Решения в ответе не услышал – записал ответ, пункт остаётся «уточнить».\n")
            return "done"
        proposal = got["status"]
        reason, step, review = got["reason"], got["next_step"], got["review_on"]
        say = f"Понял так: {LR_STATUS_TITLES[proposal]}" + (f" – {reason}" if reason else "")
        if step:
            say += f"; следующий шаг: {step}"
        ok = _ask(say + ". Так? (Enter – да, буква – другой статус, 0 – пока не решать) ")
        if ok is None or ok == "0":
            print()
            return "skip"
        if ok and ok.casefold().rstrip(".") in SHORTCUTS:
            status = SHORTCUTS[ok.casefold().rstrip(".")]
        elif ok and ok.casefold() not in ("да", "д", "так", "y"):
            print("Не понял – пропускаю.\n")
            return "skip"
        else:
            status = proposal
    if status == "active" and not _confirm_active(conn, r):
        print("Не стал делать активным.\n")
        return "skip"
    if status == "active" and not step and r["type"] not in ("material", "experience"):
        step = _ask("Какой следующий конкретный шаг? (Enter – пропустить) ") or ""
    if status in ("abandoned", "archived") and not reason:
        reason = _ask("Почему – одной фразой? Это поможет не начинать тот же круг заново. (Enter – пропустить) ") or ""
    if status in LR_PARKED:
        default = default_review(status)
        got_review = parse_review(review) if review else None
        if not got_review:
            ans2 = _ask(f"Когда пересмотреть? (Enter – {default}; «через год», «2027-06») ")
            got_review = parse_review(ans2 or "") or default
        review = got_review
    decide(conn, r["id"], status, reason=reason, answer=answer, proposal=proposal, next_step=step or None,
           review_on=review or None)
    print(f"Записал: {LR_STATUS_TITLES[status]}" + (f", пересмотр {review}" if status in LR_PARKED else "") + ".\n")
    return "done"


def resolve_area(text: str) -> str | None:
    """«finance», «финансы», «фин» → ключ области. None – не нашлось."""
    t = text.strip().casefold()
    if not t:
        return None
    if t in DOMAIN_TITLES:
        return t
    hits = [k for k, title in DOMAIN_TITLES.items() if title.casefold().startswith(t) or t in title.casefold()]
    return hits[0] if len(hits) == 1 else None


def area_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    c = Counter(r["domain"] for r in queue(conn))
    return [(DOMAIN_TITLES.get(d, d), n) for d, n in c.most_common()]


def _start(conn: sqlite3.Connection, q: list[sqlite3.Row]) -> bool:
    total = store.scalar(conn, "SELECT COUNT(*) FROM lr_items WHERE merged_into IS NULL AND status!='dismissed'") or 0
    if not total:
        print("Реестр пуст. Собрать его из памяти: python3 coach.py inventory build\n")
        return False
    if not q:
        n = bulk_count(conn)
        print(f"Здесь разбирать нечего. Одиночных упоминаний ждут разбора пачкой: {n} – python3 coach.py sweep\n"
              if n else "Здесь всё разобрано, пересматривать пока нечего.\n")
        return False
    return True


def _left_line(conn: sqlite3.Connection, done: int, type_: str | None, area: str | None) -> None:
    left = len(queue(conn, type_, area))
    where = f" в области «{DOMAIN_TITLES.get(area, area)}»" if area else ""
    print(f"Решений за этот раз: {done}. Ждут решения{where}: {left}." + ("" if area else
          "  По областям: " + ", ".join(f"{a} – {n}" for a, n in area_counts(conn)[:6])) + "\n")


def triage(conn: sqlite3.Connection, llm: Ollama, cfg: Config, limit: int = 10, type_: str | None = None,
           area: str | None = None) -> int:
    """Разобрать пункты по одному карточкой. Возвращает число принятых решений."""
    q = queue(conn, type_, area)
    if not _start(conn, q):
        return 0
    print(f"Разбор накопленного: ждут решения {len(q)}" + (f" в области «{DOMAIN_TITLES.get(area, area)}»" if area else "")
          + f". За раз – до {limit}. Отвечай своими словами или сразу буквой.\n" + SHORTCUT_HELP + "\n")
    done = 0
    for r in q[:limit]:
        r = conn.execute("SELECT * FROM lr_items WHERE id=?", (r["id"],)).fetchone()
        if r is None or r["merged_into"] is not None:
            continue
        res = _triage_one(conn, llm, r)
        if res == "stop":
            break
        done += res == "done"
    _left_line(conn, done, type_, area)
    return done


QUICK_PAGE = 10
_QUICK = re.compile(r"(\d+)\s*([аикжпгохун?])", re.I)
QUICK_HELP = ("Одной строкой: номер и буква, например «1а 2к 3о 5х» (а – активно, и – инкубатор, к – когда-нибудь, "
              "ж – жду, п – передал, г – сделано, о – отпускаю, х – в архив, у – уточнить, н – не пункт). «4?» – открыть "
              "пункт 4 карточкой (подумать, назвать причину, склеить). Неуказанное остаётся неразобранным. "
              "Enter – дальше, «стоп» – закончить.")


def parse_quick(text: str, n: int) -> list[tuple[int, str]] | None:
    """«1а 2к 4?» → [(1, 'а'), (2, 'к'), (4, '?')]. None – если в строке есть что-то кроме таких пар."""
    pairs = [(int(m.group(1)), m.group(2).casefold()) for m in _QUICK.finditer(text)]
    if _QUICK.sub("", text).strip(" ,;") or any(not 1 <= i <= n for i, _l in pairs):
        return None
    return pairs


def _quick_line(conn: sqlite3.Connection, i: int, r: sqlite3.Row) -> str:
    s = f"  {i:>2}. {r['title']} · {LR_TYPE_TITLES.get(r['type'], r['type'])}"
    if r["status"] != "inbox":
        s += f" · сейчас: {LR_STATUS_TITLES[r['status']]}"
    if r["resurfaced"]:
        s += " · снова всплыло"
    extra = span(r) if r["origin"] == "memory" else "из разговора"
    s += f"\n      {extra}"
    if r["summary"] and r["summary"].strip().rstrip(".") != r["title"].strip().rstrip(".").rstrip("…"):
        s += f" – {r['summary'][:140].strip().rstrip('.')}"
    hints = [h for h in (r["hint"] or "").split("; ") if h and h not in extra]
    if hints:
        s += "; " + "; ".join(hints)
    return s


def quick_triage(conn: sqlite3.Connection, llm: Ollama, cfg: Config, type_: str | None = None,
                 area: str | None = None, page: int = QUICK_PAGE) -> int:
    """Быстрый разбор списком: по 10 строк, решение одной строкой. Для простых пунктов; то, над чем надо
    подумать, открывается карточкой. Причину и шаг в быстром режиме не спрашиваем, кроме шага у активного."""
    q = queue(conn, type_, area)
    if not _start(conn, q):
        return 0
    print(f"Быстрый разбор: ждут решения {len(q)}" + (f" в области «{DOMAIN_TITLES.get(area, area)}»" if area else "")
          + f", по {page} на экране.\n" + QUICK_HELP + "\n")
    done = 0
    for start in range(0, len(q), page):
        part = [conn.execute("SELECT * FROM lr_items WHERE id=?", (r["id"],)).fetchone() for r in q[start:start + page]]
        part = [r for r in part if r is not None and r["merged_into"] is None]
        if not part:
            continue
        print(f"– {start + 1}-{start + len(part)} из {len(q)} –")
        for i, r in enumerate(part, 1):
            print(_quick_line(conn, i, r))
        while True:
            ans = _ask("> ")
            if ans is None or STOP.match(ans or ""):
                _left_line(conn, done, type_, area)
                return done
            pairs = parse_quick(ans, len(part)) if ans else []
            if pairs is not None:
                break
            print(f"Не понял. Номера от 1 до {len(part)} с буквой: «1а 3о», «4?» – карточкой; Enter – дальше.")
        later = []
        for i, letter in pairs:
            r = conn.execute("SELECT * FROM lr_items WHERE id=?", (part[i - 1]["id"],)).fetchone()
            if letter == "?":
                later.append(r)
                continue
            status = SHORTCUTS[letter]
            step = None
            if status == "active":
                if not _confirm_active(conn, r):
                    print(f"{i}: не стал делать активным.")
                    continue
                if r["type"] not in ("material", "experience"):
                    step = _ask(f"{i}. «{r['title']}» – следующий конкретный шаг? (Enter – пропустить) ") or None
            decide(conn, r["id"], status, next_step=step)
            done += 1
        if pairs and not later:
            print(f"Записал: {sum(1 for _i, l in pairs if l != '?')}.")
        for r in later:
            print()
            res = _triage_one(conn, llm, r)
            if res == "stop":
                _left_line(conn, done, type_, area)
                return done
            done += res == "done"
        print()
    _left_line(conn, done, type_, area)
    return done


# --- показатели ---------------------------------------------------------------------------------

def metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT status, type, review_on, resurfaced, decided_at FROM lr_items "
                        "WHERE merged_into IS NULL AND status!='dismissed'").fetchall()
    total = len(rows)
    by_status = Counter(r["status"] for r in rows)
    decided = sum(1 for r in rows if r["status"] not in LR_OPEN)
    touched = sum(1 for r in rows if r["status"] != "inbox")
    due = sum(1 for r in rows if r["status"] in LR_PARKED and r["review_on"] and r["review_on"] <= today())
    stale_before = (date.fromisoformat(today()) - timedelta(days=183)).isoformat()
    stale = sum(1 for r in rows if r["status"] == "active" and (r["decided_at"] or "")[:10] < stale_before)
    reopened = store.scalar(conn, "SELECT COUNT(DISTINCT d.item_id) FROM lr_decisions d WHERE d.from_status IN "
                                  "('completed', 'abandoned', 'archived') AND d.to_status IN ('active', 'inbox', "
                                  "'incubating', 'someday', 'clarification')") or 0
    return {
        "total": total, "by_status": dict(by_status), "decided": decided, "coverage": touched,
        "active": active_count(conn), "capacity": capacity(conn),
        "commitments": sum(1 for r in rows if r["type"] == "commitment" and r["status"] not in LR_CLOSED),
        "unresolved_questions": sum(1 for r in rows if r["type"] == "question" and r["status"] in LR_OPEN),
        "due": due, "stale": stale, "resurfaced": sum(1 for r in rows if r["resurfaced"]), "reopened": reopened,
        "decisions": store.scalar(conn, "SELECT COUNT(*) FROM lr_decisions") or 0,
        "bulk": bulk_count(conn),
    }


def recent_decisions(conn: sqlite3.Connection, limit: int = 6) -> list[sqlite3.Row]:
    return conn.execute("SELECT d.*, i.title, i.type FROM lr_decisions d JOIN lr_items i ON i.id=d.item_id "
                        "WHERE d.to_status!='merged' ORDER BY d.id DESC LIMIT ?", (limit,)).fetchall()


def print_list(conn: sqlite3.Connection, status: str | None = None) -> None:
    rows = conn.execute("SELECT * FROM lr_items WHERE merged_into IS NULL" + (" AND status=?" if status else
                        " AND status!='dismissed'") + " ORDER BY status, type, title",
                        (status,) if status else ()).fetchall()
    if not rows:
        print("Пусто.\n")
        return
    cur = None
    for r in rows:
        if r["status"] != cur:
            cur = r["status"]
            print(f"\n{LR_STATUS_TITLES[cur].capitalize()}:")
        extra = f", пересмотр {r['review_on']}" if r["review_on"] else ""
        print(f"  {code(r['id'])} {r['title']} ({LR_TYPE_TITLES.get(r['type'], r['type'])}{extra})")
    print()


# --- оценка до сборки ---------------------------------------------------------------------------

def plan(conn: sqlite3.Connection) -> dict[str, Any]:
    """Сколько будет работы, без вызова модели. Только числа: их можно пересылать."""
    gs = groups(conn)
    named = [g for g in gs if not is_single(g)]
    cached = sum(1 for g in named if store.cache_get(conn, "lr_group", g["fingerprint"], LR_VERSION) is not None)
    todo = len(named) - cached
    single = [g for g in gs if is_single(g)]
    ages = [timeline.months_ago(g["last_date"]) for g in single]
    per_batch = float(store.get_meta(conn, "lr_sec_per_batch", "0") or 0)
    by_source = Counter(g["key"].split(":", 1)[0] for g in gs)
    return {
        "groups": len(gs), "cached": cached, "todo": todo, "calls": math.ceil(todo / BATCH) if todo else 0,
        "sec_per_batch": per_batch, "note_groups": by_source.get("note", 0),
        "cluster_groups": by_source.get("cluster", 0) + by_source.get("fact", 0),
        "single": len(single), "single_notes": len({p for g in single for p in g["paths"]}),
        "single_chat": sum(1 for g in single if not g["paths"]),
        "single_3y": sum(1 for a in ages if a is not None and a >= 36),
        "single_5y": sum(1 for a in ages if a is not None and a >= 60),
        "single_undated": sum(1 for a in ages if a is None),
        "items": store.scalar(conn, "SELECT COUNT(*) FROM lr_items WHERE merged_into IS NULL") or 0,
    }


def print_plan(conn: sqlite3.Connection) -> None:
    p = plan(conn)
    print(f"Групп-кандидатов в реестр: {p['groups']} (проектных заметок: {p['note_groups']}, тем из остальной памяти: "
          f"{p['cluster_groups']}). Уже в реестре пунктов: {p['items']}.")
    print(f"Моделью называются темы и проектные заметки: нужно {p['todo']} (уже названо раньше: {p['cached']}) – "
          f"вызовов модели: {p['calls']}.")
    if p["calls"]:
        if p["sec_per_batch"]:
            print(f"Оценка по прошлой сборке: ~{fmt_duration(p['calls'] * p['sec_per_batch'])} "
                  f"({p['sec_per_batch']:.0f} с на вызов).")
        else:
            print("Сколько займёт вызов, станет известно после первой сборки (ожидаю порядка минуты на вызов на "
                  "видеокарте, на процессоре сервера – в несколько раз дольше; не замерено).")
    print(f"Одиночные упоминания (один факт, одна заметка): {p['single']} – без модели, разбор пачкой списком "
          f"(coach.py sweep); они из {p['single_notes']} заметок" + (f" и {p['single_chat']} из разговоров" if p["single_chat"] else "")
          + f". Старше 3 лет: {p['single_3y']}, старше 5 лет: {p['single_5y']}, без даты: {p['single_undated']}.")
    print("Здесь только числа, без содержимого заметок – их можно переслать.\n")


# --- разбор пачкой -------------------------------------------------------------------------------

SWEEP_PAGE = 20
SWEEP_REASON = "разобрано пачкой: не отмечено как живое"


def bulk_count(conn: sqlite3.Connection) -> int:
    return store.scalar(conn, "SELECT COUNT(*) FROM lr_items WHERE bulk=1 AND status='inbox' AND merged_into IS NULL") or 0


def parse_numbers(text: str, n: int) -> set[int] | None:
    """«3 7 15», «3,7», «2-5» → номера 1..n. None – не разобрал."""
    out: set[int] = set()
    for part in re.split(r"[\s,;]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= a <= b <= n:
            return None
        out |= set(range(a, b + 1))
    return out


PICK_TARGET = 130          # сколько одиночных показать после отбора моделью (всего, по всем областям)
PICK_CHUNK = 40            # одиночных на один вызов модели
REST_ANSWER = "хвост пачки: модель сочла менее значимым; в архив одним решением человека"


def _quotas(counts: dict[str, int], total: int) -> dict[str, int]:
    """Поровну на область; где пунктов меньше доли – берутся все, остаток делится между остальными."""
    quotas: dict[str, int] = {}
    left, open_ = total, dict(counts)
    while open_ and left > 0:
        share = left / len(open_)
        small = {d: n for d, n in open_.items() if n <= share}
        if not small:
            base = int(share)
            extra = left - base * len(open_)
            for i, d in enumerate(sorted(open_, key=lambda d: -open_[d])):
                quotas[d] = base + (1 if i < extra else 0)
            return quotas
        for d, n in small.items():
            quotas[d] = n
            left -= n
            del open_[d]
    for d in open_:
        quotas.setdefault(d, 0)
    return quotas


def rank_bulk(conn: sqlite3.Connection, llm: Ollama, cfg: Config, target: int = PICK_TARGET, quiet: bool = False) -> int:
    """Модель выбирает в каждой области самое значимое из одиночных упоминаний. Ничего не решает:
    только порядок показа в sweep. Возвращает число размеченных."""
    from .prompts import LR_PICK_SYSTEM, lr_pick_schema
    rows = conn.execute("SELECT id, domain, summary, title FROM lr_items WHERE bulk=1 AND status='inbox' "
                        "AND merged_into IS NULL AND bulk_rank IS NULL ORDER BY id").fetchall()
    if not rows:
        return 0
    ratio = store.get_meta(conn, "lr_pick_ratio")
    if ratio is None:
        # Доля задаётся на первой разметке; новые одиночные потом размечаются в той же пропорции.
        ratio = f"{min(1.0, target / len(rows)):.4f}"
        store.set_meta(conn, "lr_pick_ratio", ratio)
        conn.commit()
    by_dom: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_dom.setdefault(r["domain"], []).append(r)
    quotas = _quotas({d: len(v) for d, v in by_dom.items()}, max(1, round(float(ratio) * len(rows))))
    calls = sum(math.ceil(len(v) / PICK_CHUNK) for d, v in by_dom.items() if quotas.get(d, 0) < len(v))
    progress = Progress(calls, "отбор значимого, вызов") if calls and not quiet else None
    done = 0
    for dom, items in by_dom.items():
        q = quotas.get(dom, 0)
        if q >= len(items):
            for i, r in enumerate(items, 1):
                conn.execute("UPDATE lr_items SET bulk_rank=? WHERE id=?", (i, r["id"]))
            conn.commit()
            done += len(items)
            continue
        rank = 0
        for c in range(0, len(items), PICK_CHUNK):
            chunk = items[c:c + PICK_CHUNK]
            k = math.ceil(q * len(chunk) / len(items)) if q else 0
            ids = {f"S{j + 1}": r["id"] for j, r in enumerate(chunk)}
            chosen: list[int] = []
            if k:
                user = (f"Область: {DOMAIN_TITLES.get(dom, dom)}. Выбери не больше {k}.\n\n"
                        + "\n".join(f"S{j + 1} | {(r['summary'] or r['title'])[:220]}" for j, r in enumerate(chunk)))
                try:
                    raw = llm.chat_json(LR_PICK_SYSTEM.replace("{k}", str(k)), user, lr_pick_schema(k),
                                        num_predict=40 * k + 200, attempts=2, heavy=True)
                except OllamaError as exc:
                    warn(f"отбор прерван ({exc}); неразмеченное покажу целиком, повторный sweep доразметит.")
                    return done
                for sid in raw.get("chosen") or []:
                    iid = ids.get(str(sid).strip().upper())
                    if iid is not None and iid not in chosen:
                        chosen.append(iid)
                chosen = chosen[:k]
            for iid in chosen:
                rank += 1
                conn.execute("UPDATE lr_items SET bulk_rank=? WHERE id=?", (rank, iid))
            for r in chunk:
                if r["id"] not in chosen:
                    conn.execute("UPDATE lr_items SET bulk_rank=0 WHERE id=?", (r["id"],))
            conn.commit()
            done += len(chunk)
            if progress:
                progress.step()
    return done


def _bulk_rows(conn: sqlite3.Connection, where: str) -> list[sqlite3.Row]:
    order = {d: i for i, d in enumerate(DOMAIN_TITLES)}
    rows = conn.execute("SELECT i.*, n.title AS note_title FROM lr_items i LEFT JOIN notes n "
                        "ON n.path = json_extract(i.paths_json, '$[0]') "
                        f"WHERE i.bulk=1 AND i.status='inbox' AND i.merged_into IS NULL AND ({where})").fetchall()
    return sorted(rows, key=lambda r: (order.get(r["domain"], 99), r["bulk_rank"] or 10 ** 6,
                                       json_first(r["paths_json"]), r["id"]))


def json_first(paths_json: str) -> str:
    p = loads(paths_json, []) or []
    return p[0] if p else ""


def _pages(conn: sqlite3.Connection, rows: list[sqlite3.Row], page: int, stats: dict[str, int], by_area: bool) -> bool:
    """Страницы списка: номера живого – в разбор, остальное – в архив. False – человек остановился."""
    total = len(rows)
    for start in range(0, total, page):
        part = rows[start:start + page]
        print(f"– {start + 1}-{start + len(part)} из {total} –")
        last = None
        for i, r in enumerate(part, 1):
            note = r["note_title"] or ("из разговоров" if not loads(r["paths_json"], []) else "заметка")
            head = DOMAIN_TITLES.get(r["domain"], r["domain"]) if by_area else (f"«{note}»" if r["note_title"] else note)
            if head != last:
                print(f"  {head}")
                last = head
            text = (r["summary"] or r["title"])[:160]
            print(f"  {i:>2}. {text}" + (f"  («{note}»)" if by_area and r["note_title"] else ""))
        while True:
            ans = _ask("> ")
            if ans is None or STOP.match(ans or ""):
                return False
            if ans.casefold() in ("все", "всё", "all"):
                keep = set(range(1, len(part) + 1))
                break
            keep = parse_numbers(ans, len(part)) if ans else set()
            if keep is not None:
                break
            print(f"Номера от 1 до {len(part)}, например «3 7» или «2-5»; Enter – живого нет.")
        now = iso_now()
        for i, r in enumerate(part, 1):
            if i in keep:
                conn.execute("UPDATE lr_items SET bulk=0, updated_at=? WHERE id=?", (now, r["id"]))
                stats["kept"] += 1
            else:
                decide(conn, r["id"], "archived", reason=SWEEP_REASON)
                stats["archived"] += 1
        conn.commit()
        print(f"Живым: {len(keep)}, в архив: {len(part) - len(keep)}.\n")
    return True


def sweep(conn: sqlite3.Connection, llm: Ollama | None = None, cfg: Config | None = None, target: int = PICK_TARGET,
          page: int = SWEEP_PAGE) -> dict[str, int]:
    """Одиночные упоминания списком. С моделью – сначала отбор: в каждой области модель выбирает самое
    значимое (около target всего), оно показывается первым, по областям. Хвост модель не трогает:
    пролистать, отправить в архив одним решением или оставить – решает человек. Отмеченное номерами –
    живое, идёт в обычный разбор; остальное на странице – в архив (вернуть – inventory reopen)."""
    stats = {"kept": 0, "archived": 0, "rest": 0}
    if not bulk_count(conn):
        print("Разбирать пачкой нечего.\n")
        return stats
    if llm is not None and cfg is not None:
        unranked = store.scalar(conn, "SELECT COUNT(*) FROM lr_items WHERE bulk=1 AND status='inbox' "
                                      "AND merged_into IS NULL AND bulk_rank IS NULL") or 0
        if unranked:
            print(f"Модель отбирает самое значимое по областям жизни среди {unranked} одиночных упоминаний…")
            rank_bulk(conn, llm, cfg, target)
            print()
    chosen = _bulk_rows(conn, "i.bulk_rank IS NULL OR i.bulk_rank>0")
    rest = _bulk_rows(conn, "i.bulk_rank=0")
    ranked = bool(rest) or bool(store.get_meta(conn, "lr_pick_ratio"))
    print(f"Разбор пачкой: {len(chosen)}" + (f" самых значимых по областям (ещё {len(rest)} модель сочла менее "
                                             f"значимыми – о них в конце)" if ranked else " одиночных упоминаний")
          + f", по {page} на странице.\n"
          "Напиши номера того, что ещё живо и стоит разобрать подробно («3 7 15», «2-5»): оно пойдёт в обычный "
          "разбор. Всё остальное на странице – в архив (не удаляется; вернуть – coach.py inventory reopen LR-N).\n"
          "Enter – на этой странице живого нет, «все» – вся страница живая, «стоп» – закончить, эту страницу не трогая.\n")
    if not _pages(conn, chosen, page, stats, by_area=ranked):
        print(f"\nОстановился. Оставлено живым: {stats['kept']}, в архив: {stats['archived']}; "
              f"осталось на разбор пачкой: {bulk_count(conn)}.\n")
        return stats
    if rest:
        areas = Counter(DOMAIN_TITLES.get(r["domain"], r["domain"]) for r in rest)
        print(f"Остались {len(rest)}, которые модель сочла менее значимыми: "
              + ", ".join(f"{a} – {n}" for a, n in areas.most_common()) + ".")
        ans = (_ask("«смотреть» – пролистать их так же, «архив» – все в архив одним решением, Enter – оставить на потом: ")
               or "").casefold()
        if ans.startswith("смотр"):
            if not _pages(conn, rest, page, stats, by_area=True):
                print(f"\nОстановился. Осталось на разбор пачкой: {bulk_count(conn)}.\n")
                return stats
        elif ans.startswith("архив"):
            for r in rest:
                decide(conn, r["id"], "archived", reason=SWEEP_REASON, answer=REST_ANSWER)
            stats["archived"] += len(rest)
            stats["rest"] = len(rest)
            print(f"В архив: {len(rest)}. Вернуть любой – coach.py inventory reopen LR-N "
                  "(список – inventory list --status archived).\n")
        else:
            print(f"Оставил: {len(rest)} ждут, вернуться – coach.py sweep.\n")
    print(f"Живым оставлено {stats['kept']} – они в обычном разборе: python3 coach.py triage\n")
    return stats


def reopen(conn: sqlite3.Connection, ref: str) -> int | None:
    """Вернуть пункт в «не разобрано» (из архива, отпущенного, завершённого): решение не стирается,
    в журнал пишется новое."""
    m = re.fullmatch(r"(?:LR-)?(\d+)", ref.strip(), re.I)
    if not m:
        return None
    iid = _resolve(conn, int(m.group(1)))
    if not store.scalar(conn, "SELECT 1 FROM lr_items WHERE id=?", (iid,)):
        return None
    decide(conn, iid, "inbox", reason="открыто снова вручную")
    return iid
