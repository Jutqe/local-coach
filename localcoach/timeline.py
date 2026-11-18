"""Актуальность памяти: что из заметок верно сейчас, а что осталось в прошлом.

Заметки охватывают годы, и без этого слоя профиль описывает человека десятилетней
давности как нынешнего. У каждого кластера есть актуальность:
  current — верно сейчас (подтвердил человек, сверено с биографией или свежее упоминание);
  past    — было и прошло (с периодом или датой окончания, если известны);
  history — событие или решение с датой: история по определению;
  unknown — неизвестно.
Источники по убыванию силы: ответ человека → сверка с хронологией (импортированная
биография, сказанное в разговорах и прошлые ответы) → правила по типу и свежести.
Вопросы человеку задаются только про то, что не удалось понять без него.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import date
from typing import Any

from . import guard, i18n, store
from .config import Config, DOMAIN_TITLES, KIND_TITLES, PRIVATE_DOMAINS
from .ollama import Ollama, OllamaError
from .prompts import (CALIBRATE_ANSWER_SCHEMA, CALIBRATE_ANSWER_SYSTEM, TIMELINE_SCHEMA, TIMELINE_VERSION,
                      TIMELINE_SYSTEM)
from .util import Progress, digest, iso_now, normalize_time_ref, today, warn
from .vault import IMPORT_PREFIX
from .vectors import Matrix, truncated, unpack
from .i18n import ru_key, trl

HISTORY_KINDS = {"event", "decision"}
FRESH_MONTHS = 12          # упоминание свежее года считается актуальным, пока не сказано иное
CONFIRM_TTL_MONTHS = 18    # «так и сейчас», сказанное давно, снова требует проверки
STALE_MONTHS = 36          # в профиле помечается как давнее
BATCH = 10
TIMELINE_PER_CLAIM = 8
_PRIVATE_SQL = ",".join(f"'{d}'" for d in PRIVATE_DOMAINS) or "''"
_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")


# --- время и подписи ------------------------------------------------------------------

def months_ago(value: str | None) -> int | None:
    if not value or not value[:4].isdigit():
        return None
    year = int(value[:4])
    month = int(value[5:7]) if len(value) >= 7 and value[5:7].isdigit() else 6
    now = date.today()
    return (now.year - year) * 12 + (now.month - month)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if i18n.LANG != "ru":                      # по-английски две формы: 1 и остальные
        return one if n == 1 else few
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def age_text(months: int | None) -> str:
    if months is None:
        return ""
    if months < 12:
        return trl("в последние месяцы") if months <= 1 else trl("{0} мес. назад", months)
    years = months // 12
    return trl("{0} {1} назад", years, _plural(years, trl("год"), trl("года"), trl("лет")))


def label(currency: str | None, src: str | None, until: str | None, period: str | None,
          last_date: str | None) -> str:
    """Короткая пометка времени для модели и для человека."""
    if currency == "current":
        return {"user": trl("актуально — подтвердил ты"), "timeline": trl("актуально по биографии"),
                "confirmed": trl("подтверждено тобой"), "recent": trl("свежее")}.get(src or "", "актуально")
    if currency == "past":
        when = period if period and len(_YEAR.findall(period)) >= 2 else (trl("до {0}", until) if until else "")
        return trl("в прошлом") + (f" ({when})" if when else "")
    if currency == "history":
        return ""
    m = months_ago(last_date)
    if m is None:
        return trl("дата и актуальность неизвестны")
    if m <= FRESH_MONTHS:
        return ""
    return trl("актуальность не проверена, последнее упоминание {0}", age_text(m))


# --- отметки актуальности ---------------------------------------------------------------

def add_mark(conn: sqlite3.Connection, cluster_id: int, status: str, until: str | None = None,
             period: str | None = None, source: str = "user", note: str = "") -> int | None:
    """Записать актуальность для всех формулировок кластера. Вектор берётся у самих
    фактов, поэтому после переизвлечения заметок решение получат и новые факты с тем же смыслом."""
    members = conn.execute("SELECT id, claim, embedding FROM facts WHERE cluster_id=? AND user_state!='rejected' "
                           "ORDER BY id", (cluster_id,)).fetchall()
    if not members:
        return None
    by_claim: dict[str, Any] = {}
    for m in members:
        by_claim.setdefault(m["claim"], m["embedding"])
    now, last = iso_now(), None
    for claim, emb in by_claim.items():
        cur = conn.execute("INSERT INTO currency_marks(claim, status, until, period, source, note, embedding, created_at) "
                           "VALUES(?,?,?,?,?,?,?,?)", (claim, status, until or None, period or None, source, note, emb, now))
        last = int(cur.lastrowid)
    conn.execute("UPDATE facts SET currency_mark_id=? WHERE cluster_id=? AND user_state!='rejected'", (last, cluster_id))
    refresh_currency(conn, [cluster_id])
    conn.commit()
    return last


def apply_currency_marks(conn: sqlite3.Connection, cfg: Config) -> int:
    """Перенести отметки на похожие по смыслу факты (тот же порог, что у склейки).

    Действует самая поздняя отметка, поэтому новые отметки сверяются со всеми фактами,
    а старые — только с новыми фактами."""
    marks = [(r["id"], unpack(r["embedding"])) for r in
             conn.execute("SELECT id, embedding FROM currency_marks WHERE embedding IS NOT NULL ORDER BY id")]
    marks = [(i, truncated(v)) for i, v in marks if v is not None]
    max_fact = store.scalar(conn, "SELECT MAX(id) FROM facts") or 0
    seen_mark = int(store.get_meta(conn, "currency_mark_seen", "0") or 0)
    seen_fact = int(store.get_meta(conn, "currency_fact_seen", "0") or 0)
    changed = 0
    if marks:
        every = Matrix([i for i, _ in marks], [v for _, v in marks])
        fresh = Matrix([i for i, _ in marks if i > seen_mark], [v for i, v in marks if i > seen_mark])
        since = 0 if len(fresh) else seen_fact
        for row in conn.execute("SELECT id, embedding, currency_mark_id FROM facts WHERE user_state!='rejected' "
                                "AND embedding IS NOT NULL AND id > ?", (since,)).fetchall():
            matrix = every if row["id"] > seen_fact else fresh
            if not len(matrix):
                continue
            q = truncated(unpack(row["embedding"]))
            hits = [i for i, s in zip(matrix.ids, matrix.scores(q)) if s >= cfg.mark_threshold]
            if hits and max(hits) > (row["currency_mark_id"] or 0):
                conn.execute("UPDATE facts SET currency_mark_id=? WHERE id=?", (max(hits), row["id"]))
                changed += 1
    store.set_meta(conn, "currency_mark_seen", str(max((i for i, _ in marks), default=seen_mark)))
    unembedded = store.scalar(conn, "SELECT MIN(id) FROM facts WHERE embedding IS NULL")
    store.set_meta(conn, "currency_fact_seen", str((unembedded - 1) if unembedded else max_fact))
    conn.commit()
    return changed


def refresh_currency(conn: sqlite3.Connection, cluster_ids: list[int] | None = None) -> dict[str, int]:
    """Пересчитать актуальность кластеров (все или перечисленные). Модель не вызывается."""
    where, params = "", ()
    if cluster_ids is not None:
        ids = [int(c) for c in cluster_ids if c is not None]
        if not ids:
            return {}
        where, params = f" AND cl.id IN ({','.join('?' * len(ids))})", tuple(ids)
    marks = {r["id"]: r for r in conn.execute("SELECT id, status, until, period, source, created_at FROM currency_marks")}
    best = {r[0]: r[1] for r in conn.execute(
        "SELECT cluster_id, MAX(currency_mark_id) FROM facts WHERE cluster_id IS NOT NULL AND user_state!='rejected' "
        "AND currency_mark_id IS NOT NULL GROUP BY cluster_id")}
    confirmed = {r[0] for r in conn.execute(
        "SELECT DISTINCT cluster_id FROM facts WHERE cluster_id IS NOT NULL AND (user_state='confirmed' OR origin='chat')")}
    counts: dict[str, int] = {}
    for cl in conn.execute("SELECT cl.id, cl.last_date, f.kind, f.intent_status FROM clusters cl "
                           "JOIN facts f ON f.id=cl.head_fact_id WHERE 1=1" + where, params).fetchall():
        state = _decide(cl, marks.get(best.get(cl["id"])), cl["id"] in confirmed)
        conn.execute("UPDATE clusters SET currency=?, currency_src=?, valid_until=?, period=? WHERE id=?",
                     (*state, cl["id"]))
        counts[state[0]] = counts.get(state[0], 0) + 1
    conn.commit()
    return counts


def _decide(cl: sqlite3.Row, mark: sqlite3.Row | None, confirmed: bool) -> tuple[str, str, str | None, str | None]:
    last = cl["last_date"]
    if mark is not None:
        if mark["status"] == "past":
            until = mark["until"]
            # Свежее упоминание после окончания: ситуация могла вернуться, решение больше не действует.
            if not (until and last and last[:len(until)] > until):
                return "past", mark["source"], until, mark["period"]
        elif mark["status"] == "current":
            age = months_ago(mark["created_at"][:7])
            if age is not None and age <= CONFIRM_TTL_MONTHS:
                return "current", mark["source"], None, mark["period"]
    if cl["kind"] in HISTORY_KINDS or cl["intent_status"] in ("completed", "abandoned"):
        return "history", "kind", None, None
    if confirmed:
        return "current", "confirmed", None, None
    age = months_ago(last)
    if age is not None and age <= FRESH_MONTHS:
        return "current", "recent", None, None
    return "unknown", "", None, None


# --- хронология и сверка ------------------------------------------------------------------

_CHRONO_TITLE = re.compile(r"дневник|летопис|хронолог|биограф|по годам|итоги год|diary|journal|timeline", re.I)


def chronology_paths(conn: sqlite3.Connection) -> list[str]:
    """Заметки-хронологии из vault: разделы по годам (хотя бы два разных года в заголовках)
    или название либо папка вроде «Дневник», «Хронология», «Итоги по годам»."""
    by_years = {r[0] for r in conn.execute(
        "SELECT c.path FROM chunks c JOIN notes n ON n.path=c.path WHERE n.status='indexed' "
        "AND c.chunk_date IS NOT NULL AND c.chunk_date != COALESCE(n.note_date, '') "
        "GROUP BY c.path HAVING COUNT(DISTINCT substr(c.chunk_date, 1, 4)) >= 2")}
    # Заметка проекта с разделами по годам — план работ, а не хроника жизни (4.16).
    projects = {r[0] for r in conn.execute("SELECT DISTINCT path FROM facts WHERE is_project=1")}
    by_title = {r["path"] for r in conn.execute("SELECT path, title FROM notes WHERE status='indexed' AND path NOT LIKE ?",
                                                (IMPORT_PREFIX + "%",)) if _CHRONO_TITLE.search(f'{r["path"]} {r["title"] or ""}')}
    return sorted((by_years | by_title) - projects)


def timeline_entries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Опорная хронология: импортированные файлы (биография), датированные факты дневников,
    сказанное в разговорах и ответы на вопросы об актуальности."""
    out = []
    chrono = chronology_paths(conn)
    marks = ",".join("?" * len(chrono)) or "''"
    for r in conn.execute(
            "SELECT id, claim, event_date, origin, path, embedding FROM facts WHERE user_state!='rejected' "
            f"AND embedding IS NOT NULL AND is_project=0 AND domain NOT IN ({_PRIVATE_SQL}) AND (path LIKE ? OR origin='chat' "
            f"OR (path IN ({marks}) AND date_reliable=1 AND event_date IS NOT NULL)) "
            "ORDER BY event_date, id", (IMPORT_PREFIX + "%", *chrono)):
        if r["origin"] == "chat":
            src = trl("сказал в разговоре")
        else:
            src = trl("биография") if (r["path"] or "").startswith(IMPORT_PREFIX) else trl("дневник")
        out.append({"ref": f"T{r['id']}", "date": r["event_date"] or "", "vec": unpack(r["embedding"]),
                    "text": f"{r['claim']} ({src})"})
    seen: set[tuple] = set()
    for r in conn.execute("SELECT id, claim, status, until, period, created_at, embedding FROM currency_marks "
                          "WHERE source='user' AND embedding IS NOT NULL ORDER BY id"):
        key = (r["created_at"], r["status"])  # одна отметка кластера = одна строка хронологии
        if key in seen:
            continue
        seen.add(key)
        if r["status"] == "current":
            answer = trl("так и сейчас")
        else:
            answer = trl("уже не так") + (trl(", было {0}", r['period']) if r["period"] else "") + (
                trl(", закончилось {0}", r['until']) if r["until"] else "")
        out.append({"ref": f"U{r['id']}", "date": r["until"] or r["created_at"][:10], "vec": unpack(r["embedding"]),
                    "text": trl("«{0}» — ответ человека {1}: {2}", r['claim'], r['created_at'][:10], answer)})
    # Для подбора строк хватает усечённых векторов (как у склейки) — втрое быстрее без numpy.
    return [{**e, "vec": truncated(e["vec"])} for e in out if e["vec"] is not None]


def _cluster_info(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    rows = conn.execute(
        "SELECT cl.id, cl.domain, cl.support_notes, cl.first_date, cl.last_date, cl.score, cl.currency, "
        "f.claim, f.kind, f.embedding, f.is_project FROM clusters cl JOIN facts f ON f.id=cl.head_fact_id "
        f"WHERE cl.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    order = {cid: i for i, cid in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r["id"]])


def important_clusters(conn: sqlite3.Connection, cfg: Config) -> list[int]:
    """Кластеры, которые видит синтез профиля: сначала процитированные в профиле, затем
    лучшие по каждой области."""
    cited: list[int] = []
    profile = store.cache_any(conn, "profile", "final") or {}
    for value in profile.values():
        for it in value if isinstance(value, list) else []:
            for x in (it.get("ids") or it.get("for") or []) if isinstance(it, dict) else []:
                if isinstance(x, str) and x.startswith("C") and x[1:].isdigit() and int(x[1:]) not in cited:
                    cited.append(int(x[1:]))
    ranked: list[tuple[float, int]] = []
    for (domain,) in conn.execute("SELECT DISTINCT domain FROM clusters").fetchall():
        if domain in PRIVATE_DOMAINS:
            continue
        ranked += [(r["score"], r["id"]) for r in conn.execute(
            "SELECT cl.id, cl.score FROM clusters cl JOIN facts f ON f.id=cl.head_fact_id WHERE cl.domain=? "
            "AND f.user_state!='rejected' ORDER BY cl.score DESC, cl.id LIMIT ?", (domain, cfg.synth_clusters_per_domain))]
    existing = {r[0] for r in conn.execute("SELECT id FROM clusters")}
    rest = [cid for _, cid in sorted(ranked, reverse=True) if cid not in cited]
    return [c for c in cited if c in existing] + rest


def _relevant(entries: list[dict[str, Any]], matrix: Matrix, row: sqlite3.Row) -> list[str]:
    q = unpack(row["embedding"])
    if q is None or not len(matrix):
        return []
    return [entries[i]["ref"] for i, _ in matrix.top(truncated(q), TIMELINE_PER_CLAIM)]


def _fingerprint(cfg: Config, row: sqlite3.Row, refs: list[str], by_ref: dict[str, dict[str, Any]]) -> str:
    return digest([TIMELINE_VERSION, cfg.synth_name, row["claim"], [by_ref[r]["text"] for r in refs]])


def _when(row: sqlite3.Row) -> str:
    if row["first_date"] and row["last_date"] and row["first_date"] != row["last_date"]:
        return f"{row['first_date']}…{row['last_date']}"
    return row["last_date"] or trl("дата неизвестна")


def _judge(conn: sqlite3.Connection, llm: Ollama, cfg: Config, rows: list[sqlite3.Row],
           entries: list[dict[str, Any]], matrix: Matrix) -> list[tuple[int, str, dict[str, Any]]]:
    """Один вызов модели на пакет утверждений. Только чтение базы — годится для фонового потока."""
    by_ref = {e["ref"]: e for e in entries}
    per = {r["id"]: _relevant(entries, matrix, r) for r in rows}
    refs = sorted({x for v in per.values() for x in v}, key=lambda x: (by_ref[x]["date"], x))
    timeline = "\n".join(f"{x} | {by_ref[x]['date'] or 'без даты'} | {by_ref[x]['text']}" for x in refs)
    amap = {f"A{i}": r for i, r in enumerate(rows, 1)}
    claims = "\n".join(f"{a} | {KIND_TITLES.get(r['kind'], r['kind'])} | {_when(r)} | {r['claim']}" for a, r in amap.items())
    user = trl("Сегодня: {0}.\n\nХРОНОЛОГИЯ:\n{1}\n\nУТВЕРЖДЕНИЯ:\n{2}", today(), timeline, claims)
    raw = llm.chat_json(TIMELINE_SYSTEM, user, TIMELINE_SCHEMA, think=cfg.think_synth, num_predict=1800,
                        model=cfg.synth_name)
    verdicts: dict[str, dict[str, Any]] = {}
    for v in raw.get("items") or []:
        if isinstance(v, dict) and str(v.get("id", "")).upper() in amap:
            verdicts[str(v["id"]).upper()] = v
    out = []
    for a, r in amap.items():
        out.append((r["id"], _fingerprint(cfg, r, per[r["id"]], by_ref),
                    _clean_verdict(verdicts.get(a), set(per[r["id"]]), r["last_date"])))
    return out


def _clean_verdict(v: dict[str, Any] | None, allowed: set[str], last_date: str | None = None) -> dict[str, Any]:
    """Вердикт без опоры на строки хронологии не принимается — модель любит угадывать по теме."""
    if not v or v.get("verdict") not in ("past", "current"):
        return {"verdict": "unknown"}
    ids = [str(x).strip().upper() for x in v.get("ids") or [] if str(x).strip().upper() in allowed]
    if not ids:
        return {"verdict": "unknown"}
    until = normalize_time_ref(str(v.get("until") or ""))
    # Окончание не позже сегодняшнего дня и позже последнего упоминания: модель часто
    # ставит сюда просто дату заметки.
    if until and (until > today() or (last_date and until[:len(last_date)] <= last_date[:len(until)])):
        until = ""
    period = _period(v.get("period"))
    # «В прошлом» с периодом до нынешнего года и без даты конца — это длящееся, а не законченное.
    if v["verdict"] == "past" and not until and period.endswith(today()[:4]):
        return {"verdict": "unknown"}
    return {"verdict": v["verdict"], "until": until, "period": period, "ids": ids}


def _period(value: Any) -> str:
    """Период — только диапазон лет («2012–2016»); одиночную дату модель часто берёт из заметки."""
    years = _YEAR.findall(str(value or ""))
    return f"{years[0]}–{years[-1]}" if len(years) >= 2 and years[0] < years[-1] else ""


def _apply(conn: sqlite3.Connection, results: list[tuple[int, str, dict[str, Any]]],
           skip: set[int] | None = None) -> dict[str, int]:
    counts = {"past": 0, "current": 0, "unknown": 0}
    for cid, fp, v in results:
        if skip and cid in skip:
            continue
        state = store.scalar(conn, "SELECT currency FROM clusters WHERE id=?", (cid,))
        if state != "unknown":
            continue  # успел ответить человек или кластер пересобран
        if v["verdict"] in ("past", "current"):
            add_mark(conn, cid, v["verdict"], v.get("until"), v.get("period"), "timeline", ",".join(v["ids"]))
        store.cache_put(conn, "currency", str(cid), fp, v)
        counts[v["verdict"]] += 1
    return counts


def _chronology_clusters(conn: sqlite3.Connection) -> list[int]:
    """Кластеры с фактами из самой хронологии: строка биографии «работал в X, 2010–2015»
    без сверки так и осталась бы «неизвестно»."""
    chrono = chronology_paths(conn)
    marks = ",".join("?" * len(chrono)) or "''"
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT cluster_id FROM facts WHERE cluster_id IS NOT NULL AND user_state!='rejected' "
        f"AND (path LIKE ? OR path IN ({marks})) ORDER BY cluster_id",
        (IMPORT_PREFIX + "%", *chrono))]


def resolve_pending(conn: sqlite3.Connection, llm: Ollama, cfg: Config, quiet: bool = False) -> dict[str, int]:
    """Сверить с хронологией важные утверждения с неизвестной актуальностью.
    Уже сверенное с той же хронологией повторно не отправляется."""
    entries = timeline_entries(conn)
    totals = {"past": 0, "current": 0, "unknown": 0, "cached": 0}
    if not entries:
        return totals
    matrix = Matrix(list(range(len(entries))), [e["vec"] for e in entries])
    by_ref = {e["ref"]: e for e in entries}
    rows = [r for r in _cluster_info(conn, important_clusters(conn, cfg) + _chronology_clusters(conn))
            if r["currency"] == "unknown" and r["embedding"] is not None]
    todo = []
    for r in rows:
        if store.cache_get(conn, "currency", str(r["id"]), _fingerprint(cfg, r, _relevant(entries, matrix, r), by_ref)):
            totals["cached"] += 1
        else:
            todo.append(r)
    if not todo:
        return totals
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    progress = None if quiet else Progress(len(batches), trl("сверка с хронологией, пакет"))
    for batch in batches:
        try:
            counts = _apply(conn, _judge(conn, llm, cfg, batch, entries, matrix))
        except OllamaError as exc:
            warn(trl("сверка с хронологией прервана ({0}); продолжится при следующем запуске", type(exc).__name__))
            break
        for k, n in counts.items():
            totals[k] += n
        if progress:
            progress.step(trl("в прошлом {0}, актуально {1}", counts['past'], counts['current']))
    return totals


def update_currency(conn: sqlite3.Connection, llm: Ollama | None, cfg: Config) -> dict[str, Any]:
    """Этап конвейера: отметки → правила → сверка с хронологией → правила."""
    apply_currency_marks(conn, cfg)
    refresh_currency(conn)
    resolved = resolve_pending(conn, llm, cfg) if llm is not None else {}
    counts = refresh_currency(conn)
    return {"counts": counts, "resolved": resolved, "timeline": len(timeline_entries(conn)),
            "questions": len(question_queue(conn, cfg))}


# --- вопросы человеку ---------------------------------------------------------------------

def question_queue(conn: sqlite3.Connection, cfg: Config, include_skipped: bool = False) -> list[sqlite3.Row]:
    rows = [r for r in _cluster_info(conn, important_clusters(conn, cfg))
            if r["currency"] == "unknown" and not r["is_project"]]
    if include_skipped:
        return rows
    return [r for r in rows if store.cache_get(conn, "calib_skip", str(r["id"]), digest(r["claim"])) is None]


LEGEND = trl("Ответы:\n"
             "  с — так и сейчас\n"
             "  б — было, но уже нет; можно сразу с годом: «б 2019» или «б 2012-2016»\n"
             "  и — изменилось, и сразу как сейчас: «и теперь работаю в …»\n"
             "  н — неверно, не про меня\n"
             "  п — пропустить,  в — закончить,  ? — подсказка\n"
             "Можно ответить и своими словами («уволился в 2019, теперь фриланс») — разберу сам.")


def _years(text: str) -> tuple[str, str]:
    """(until, period) из ответа: «2019» → до 2019; «2012-2016» → 2012–2016, до 2016."""
    years = _YEAR.findall(text)
    if len(years) >= 2:
        return years[-1], f"{years[0]}–{years[-1]}"
    if years:
        return years[0], ""
    return "", ""


class _Background:
    """Сверка следующих вопросов с обновлённой хронологией, пока человек отвечает."""

    def __init__(self, cfg: Config, llm: Ollama):
        self.cfg, self.llm = cfg, llm
        self.thread: threading.Thread | None = None
        self.results: list[tuple[int, str, dict[str, Any]]] = []

    def start(self, cids: list[int]) -> None:
        if self.thread is not None or not cids:
            return

        def run() -> None:
            conn = store.connect(self.cfg.db_path)
            try:
                entries = timeline_entries(conn)
                if entries:
                    matrix = Matrix(list(range(len(entries))), [e["vec"] for e in entries])
                    rows = [r for r in _cluster_info(conn, cids) if r["embedding"] is not None]
                    self.results = _judge(conn, self.llm, self.cfg, rows, entries, matrix) if rows else []
            except (OllamaError, sqlite3.Error, OSError):
                self.results = []
            finally:
                conn.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def collect(self, conn: sqlite3.Connection, skip: set[int], wait: bool = False) -> None:
        if self.thread is None or (self.thread.is_alive() and not wait):
            return
        self.thread.join(timeout=300)
        if not self.thread.is_alive():
            _apply(conn, self.results, skip)
        self.thread, self.results = None, []


def calibrate(conn: sqlite3.Connection, llm: Ollama, cfg: Config, limit: int = 25,
              include_skipped: bool = False) -> int:
    """Спросить человека про важные утверждения, актуальность которых неизвестна.
    Возвращает число ответов."""
    from .chat import _add_confirmed_fact, _cluster_members, _mark  # chat импортирует этот модуль

    apply_currency_marks(conn, cfg)
    refresh_currency(conn)
    if timeline_entries(conn):
        print(trl("Сверяю старые утверждения с биографией и твоими ответами…"), flush=True)
        r = resolve_pending(conn, llm, cfg)
        if r["past"] or r["current"]:
            print(trl("  без вопросов понятно: в прошлом {0}, актуально {1}", r['past'], r['current']))
    items = question_queue(conn, cfg, include_skipped)
    if not items:
        print(trl("Вопросов нет: всё важное для профиля либо свежее, либо уже сверено.\n"))
        return 0
    print(trl("\n"
              "В профиль попадают утверждения из старых заметок, про которые неясно, верны ли они сейчас: {0}. Начну с самых важных, за раз — до {1}.\n", len(items), limit))
    print(LEGEND + "\n")
    bg = _Background(cfg, llm)
    answered: set[int] = set()
    rejected = auto = 0
    timeline_changed = False
    i = 0
    while i < len(items) and len(answered) < limit:
        item = items[i]
        bg.collect(conn, answered)
        state = conn.execute("SELECT currency, currency_src, valid_until, period, last_date FROM clusters WHERE id=?",
                             (item["id"],)).fetchone()
        if state is None or state["currency"] != "unknown":
            # Понятое из ответов молча пропускаем: в конце будет только число.
            auto += state is not None
            i += 1
            continue
        if timeline_changed and bg.thread is None:
            bg.start([r["id"] for r in items[i + 1:i + 1 + BATCH] if r["id"] not in answered])
            timeline_changed = False
        when = label("unknown", "", None, None, item["last_date"])
        print(trl("[{0}] {1} · заметок: {2} · {3}", len(answered) + 1, DOMAIN_TITLES.get(item['domain'], item['domain']), item['support_notes'], when))
        print(f"  {item['claim']}")
        try:
            ans = ru_key(input("  > ").strip())
        except (EOFError, KeyboardInterrupt):
            print()
            break
        low = ans.casefold()
        if low in ("в", "выход", "q", "quit", "exit"):
            break
        if low in ("?", "помощь", "help"):
            print(LEGEND + "\n")
            continue
        if not ans:
            continue
        cid = item["id"]
        head, rest = low[:1], ans[1:].strip()
        one_letter = len(low) == 1 or (head in "сбинп" and not low[1:2].isalpha())
        verdict, until, period, now_claim = "", "", "", ""
        explained = False
        if one_letter and head == "с":
            verdict = "current"
        elif one_letter and head == "б" or _YEAR.fullmatch(low) or re.fullmatch(r"\s*\d{4}\s*[-–—]\s*\d{4}\s*", low):
            verdict = "past"
            until, period = _years(ans)
            if not until:
                try:
                    until, period = _years(input(trl("  Когда примерно закончилось? (год, Enter — не помню): ")))
                except (EOFError, KeyboardInterrupt):
                    break
        elif one_letter and head == "и":
            text = rest
            if not text:
                try:
                    text = input(trl("  Как сейчас: ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
            if not text:
                continue
            # Модель только переформулирует «как сейчас» и вытаскивает год; вердикт уже известен.
            _v, until, period, now_claim = _interpret(llm, item, trl("Уже не так. Сейчас: {0}", text))
            verdict, now_claim = "past", now_claim or text
            print(trl("  [понял: {0}]", _describe(verdict, until, period, now_claim)))
            explained = True
        elif one_letter and head == "н":
            verdict = "wrong"
        elif one_letter and head == "п":
            store.cache_put(conn, "calib_skip", str(cid), digest(item["claim"]), {"at": iso_now()})
            i += 1
            print()
            continue
        else:
            verdict, until, period, now_claim = _interpret(llm, item, ans)
            print(trl("  [понял: {0}]", _describe(verdict, until, period, now_claim)))
            explained = True
            if verdict == "wrong":
                # Забыть — необратимо для профиля, поэтому по свободному ответу только с подтверждением.
                try:
                    sure = input(trl("  Забыть это утверждение совсем? (д — да, иначе запишу как «в прошлом»): "))
                except (EOFError, KeyboardInterrupt):
                    break
                if not sure.strip().casefold().startswith(("д", "y")):
                    verdict = "past"
            if verdict == "skip":
                i += 1
                print()
                continue
        if verdict == "wrong":
            _mark(conn, llm, _cluster_members(conn, store.scalar(conn, "SELECT head_fact_id FROM clusters WHERE id=?",
                                                                 (cid,))), "rejected")
            conn.commit()
            rejected += 1
        elif verdict in ("current", "past"):
            add_mark(conn, cid, verdict, until or None, period or None, "user")
            timeline_changed = True
        if not explained:
            print(f"  ✓ {_describe(verdict, until, period, now_claim)}")
        if now_claim and not guard.text_hold(now_claim):
            if not now_claim.casefold().startswith(("автор", "the author", "author")):
                now_claim = trl("Автор: {0}", now_claim)
            _add_confirmed_fact(conn, llm, now_claim, item["kind"], item["domain"], None)
            conn.commit()
        answered.add(cid)
        i += 1
        print()
    if bg.thread is not None:
        print(trl("Досверяю следующие вопросы с твоими ответами…"), flush=True)
        bg.collect(conn, answered, wait=True)
    if rejected or answered:
        from .vectors import update_clusters
        update_clusters(conn, cfg, quiet=True)
        apply_currency_marks(conn, cfg)
        refresh_currency(conn)
    left = len(question_queue(conn, cfg))
    print(trl("Ответов: {0}, понято из твоих ответов без вопроса: {1}. Осталось вопросов: {2}.", len(answered), auto, left))
    return len(answered)


def _interpret(llm: Ollama, item: sqlite3.Row, answer: str) -> tuple[str, str, str, str]:
    """Свободный ответ → вердикт. Ничего не выдумываем: непонятное = пропуск."""
    try:
        raw = llm.chat_json(CALIBRATE_ANSWER_SYSTEM, trl("Сегодня: {0}.\nУтверждение: {1}\nОтвет: {2}", today(), item['claim'], answer),
                            CALIBRATE_ANSWER_SCHEMA, num_predict=300, attempts=2)
    except OllamaError:
        return "skip", "", "", ""
    verdict = raw.get("verdict") if raw.get("verdict") in ("current", "past", "wrong", "skip") else "skip"
    until = normalize_time_ref(str(raw.get("until") or ""))
    if until and until > today():
        until = ""
    period = _period(raw.get("period"))
    if verdict == "past" and not until and not period:
        until, period = _years(answer)
    return verdict, until, period, str(raw.get("now_claim") or "").strip()


def _describe(verdict: str, until: str, period: str, now_claim: str) -> str:
    text = {"current": trl("так и сейчас"), "wrong": trl("неверно, забуду"), "skip": trl("пропускаю")}.get(verdict, "")
    if verdict == "past":
        text = label("past", "", until, period, None)
    if now_claim:
        text += trl("; сейчас: {0}", now_claim)
    return text
