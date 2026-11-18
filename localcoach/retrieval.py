"""Гибридный поиск: векторы + полнотекстовый (trigram), объединение через RRF."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from .config import Config, PRIVATE_DOMAINS
from .ollama import Ollama, OllamaError, embed_query_text
from .vectors import load_matrix, normalize

RRF_K = 60
_STOP = {
    "что", "как", "это", "для", "мне", "меня", "мой", "моя", "мои", "или", "есть", "был", "была", "быть",
    "при", "про", "когда", "если", "тогда", "тоже", "просто", "очень", "уже", "чтобы", "потом", "сейчас",
    "почему", "какой", "какая", "какие", "хочу", "можно", "нужно", "надо", "будет", "было", "его", "она",
    "они", "оно", "тебя", "тебе", "ты", "все", "всё", "так", "вот", "там", "тут", "где", "кто", "чем",
    "the", "and", "that", "with", "what", "this", "from",
}
_PRIVATE_SQL = ",".join(f"'{d}'" for d in PRIVATE_DOMAINS) or "''"


def fts_query(text: str) -> str | None:
    """Запрос для trigram-индекса: основы слов без окончаний, через OR."""
    terms = []
    for w in re.findall(r"[\wЁё-]+", text.casefold()):
        w = w.replace("ё", "е").strip("-")
        if len(w) < 3 or w in _STOP or w.isdigit() and len(w) < 4:
            continue
        stem = w[:max(4, len(w) - 2)] if len(w) > 5 else w
        if stem not in terms:
            terms.append(stem)
    if not terms:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms[:14])


def rrf(*rankings: list[int]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


class Retriever:
    def __init__(self, conn: sqlite3.Connection, llm: Ollama, cfg: Config):
        self.conn, self.llm, self.cfg = conn, llm, cfg
        self.reload()

    def reload(self) -> None:
        c = self.conn
        self.facts = load_matrix(c, f"SELECT id, embedding FROM facts WHERE user_state!='rejected' "
                                    f"AND domain NOT IN ({_PRIVATE_SQL}) AND embedding IS NOT NULL")
        self.chunks = load_matrix(c, "SELECT c.id, c.embedding FROM chunks c JOIN notes n ON n.path=c.path "
                                     "WHERE n.status='indexed' AND c.embedding IS NOT NULL")
        self.sessions = load_matrix(c, "SELECT id, embedding FROM sessions WHERE embedding IS NOT NULL")

    def with_conn(self, conn: sqlite3.Connection) -> "Retriever":
        """Тот же индекс в памяти, но с другим соединением (для фонового потока)."""
        clone = object.__new__(Retriever)
        clone.__dict__.update(self.__dict__)
        clone.conn = conn
        return clone

    last_qvec: list[float] | None = None   # вектор последнего запроса: им же ищутся пункты реестра Life Reset

    def _qvec(self, query: str) -> list[float] | None:
        self.last_qvec = None
        if not (len(self.facts) or len(self.chunks) or len(self.sessions)):
            return None
        try:
            self.last_qvec = normalize(self.llm.embed([embed_query_text(query)], timeout=60)[0])
            return self.last_qvec
        except OllamaError:
            return None

    def _fts(self, table: str, query: str, limit: int) -> list[int]:
        match = fts_query(query)
        if not match:
            return []
        try:
            return [r[0] for r in self.conn.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH ? ORDER BY bm25({table}) LIMIT ?", (match, limit))]
        except sqlite3.OperationalError:
            return []

    def search(self, query: str) -> dict[str, list[dict[str, Any]]]:
        cfg = self.cfg
        q = self._qvec(query)
        # --- факты, сгруппированные по кластерам
        floor = cfg.chat_min_similarity
        vec_facts = [i for i, s in self.facts.top(q, 80) if s >= floor] if q is not None else []
        fact_scores = rrf(vec_facts, self._fts("facts_fts", query, 80))
        facts = self._load_facts(fact_scores)
        # --- исходные фрагменты
        vec_chunks = [i for i, s in self.chunks.top(q, 30) if s >= floor] if q is not None else []
        chunk_scores = rrf(vec_chunks, self._fts("chunks_fts", query, 30))
        excerpts = self._load_chunks(chunk_scores, query)
        # --- прошлые сессии
        sessions = []
        if q is not None and len(self.sessions):
            for sid, score in self.sessions.top(q, cfg.chat_sessions):
                if score < 0.35:
                    continue
                row = self.conn.execute("SELECT id, started_at, title, summary, agreements_json FROM sessions "
                                        "WHERE id=?", (sid,)).fetchone()
                if row:
                    sessions.append(dict(row))
        return {"facts": facts, "excerpts": excerpts, "sessions": sessions}

    def _load_facts(self, scores: dict[int, float]) -> list[dict[str, Any]]:
        if not scores:
            return []
        ranked = sorted(scores, key=scores.get, reverse=True)[:120]
        rows = self.conn.execute(
            "SELECT f.id, f.claim, f.kind, f.intent_status, f.event_date, f.date_reliable, f.evidence_quote, "
            "f.explicitness, f.origin, f.user_state, f.path, f.source_ref, f.cluster_id, f.domain, "
            "n.title, cl.support_notes, cl.first_date, cl.last_date, cl.score AS cluster_score, "
            "cl.currency, cl.currency_src, cl.valid_until, cl.period "
            "FROM facts f LEFT JOIN notes n ON n.path=f.path LEFT JOIN clusters cl ON cl.id=f.cluster_id "
            f"WHERE f.id IN ({','.join('?' * len(ranked))}) AND f.user_state!='rejected' "
            f"AND f.domain NOT IN ({_PRIVATE_SQL})", ranked).fetchall()
        by_cluster: dict[Any, dict[str, Any]] = {}
        for r in rows:
            item = dict(r)
            # Небольшая поправка на подтверждённость кластера, чтобы устойчивое шло выше.
            item["rank"] = scores[r["id"]] * (1.0 + 0.08 * min(r["cluster_score"] or 0, 4))
            if r["currency"] == "past":
                item["rank"] *= 0.85  # прошлое тоже нужно, но нынешнее важнее
            key = r["cluster_id"] if r["cluster_id"] is not None else f"f{r['id']}"
            if key not in by_cluster or item["rank"] > by_cluster[key]["rank"]:
                by_cluster[key] = item
        out = sorted(by_cluster.values(), key=lambda x: x["rank"], reverse=True)
        return out[:self.cfg.chat_facts]

    def _load_chunks(self, scores: dict[int, float], query: str) -> list[dict[str, Any]]:
        if not scores:
            return []
        ranked = sorted(scores, key=scores.get, reverse=True)[:self.cfg.chat_excerpts * 3]
        rows = self.conn.execute(
            "SELECT c.id, c.path, c.line_start, c.line_end, c.heading, c.chunk_date, c.body, c.summary, c.status, "
            "n.title, n.date_source FROM chunks c JOIN notes n ON n.path=c.path "
            f"WHERE c.id IN ({','.join('?' * len(ranked))}) AND n.status='indexed'", ranked).fetchall()
        # В чат не идут: ещё не разобранные фрагменты (их область неизвестна) и те, где
        # большинство фактов — из закрытых областей. Для импорта v3 факты ищутся по заметке.
        out = []
        for r in sorted(rows, key=lambda r: scores[r["id"]], reverse=True):
            if r["status"] not in ("done", "imported"):
                continue
            where, arg = ("path=?", r["path"]) if r["status"] == "imported" else ("chunk_id=?", r["id"])
            total, private = self.conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(domain IN ({_PRIVATE_SQL})), 0) FROM facts WHERE {where}",
                (arg,)).fetchone()
            if total and private * 2 >= total:
                continue
            item = dict(r)
            item["excerpt"] = best_window(r["body"], query, self.cfg.chat_excerpt_chars)
            out.append(item)
            if len(out) >= self.cfg.chat_excerpts:
                break
        return out

    def recent(self, days: int = 120, limit: int = 8) -> list[dict[str, Any]]:
        """Самое свежее и значимое — что происходит в жизни в последнее время."""
        rows = self.conn.execute(
            "SELECT f.id, f.claim, f.kind, f.intent_status, cl.last_date AS event_date, cl.support_notes "
            "FROM clusters cl JOIN facts f ON f.id=cl.head_fact_id "
            f"WHERE cl.last_date >= date('now', ?) AND f.user_state!='rejected' AND cl.domain NOT IN ({_PRIVATE_SQL}) "
            "AND cl.currency!='past' "
            "ORDER BY cl.last_date DESC, cl.score DESC LIMIT ?", (f"-{days} days", limit)).fetchall()
        return [dict(r) for r in rows]


def best_window(body: str, query: str, size: int) -> str:
    """Кусок фрагмента вокруг места с наибольшим числом слов запроса."""
    if len(body) <= size:
        return body
    stems = [t.strip('"') for t in (fts_query(query) or "").split(" OR ") if t]
    if not stems:
        return body[:size]
    low = body.casefold().replace("ё", "е")
    step = max(200, size // 4)
    best_start, best = 0, -1
    for start in range(0, max(1, len(body) - size + step), step):
        window = low[start:start + size]
        hits = sum(window.count(s) for s in stems)
        if hits > best:
            best_start, best = start, hits
    # Начало окна — с начала строки, чтобы не резать слова.
    nl = body.rfind("\n", 0, best_start)
    start = nl + 1 if nl >= 0 and best_start - nl < 300 else best_start
    text = body[start:start + size]
    return ("…" if start > 0 else "") + text + ("…" if start + size < len(body) else "")
