"""Эмбеддинги, векторный поиск и склейка похожих фактов в кластеры.

numpy используется, если установлен (apt install python3-numpy); без него всё
работает на чистом Python, просто медленнее. Для кластеризации берутся первые
256 измерений вектора (embeddinggemma обучена с Matryoshka, усечение допустимо).
"""
from __future__ import annotations

import math
import operator
import sqlite3
from array import array
from datetime import date
from typing import Iterable, Sequence

from . import store
from .config import Config
from .ollama import Ollama, OllamaError, embed_doc_text
from .util import Progress, iso_now, warn
from .i18n import trl

try:  # pragma: no cover - зависит от окружения
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

CLUSTER_DIM = 256


# --- упаковка и арифметика -----------------------------------------------------

def normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def pack(vec: Sequence[float]) -> bytes:
    return array("f", normalize(vec)).tobytes()


def unpack(blob: bytes | None) -> array | None:
    if not blob:
        return None
    a = array("f")
    a.frombytes(blob)
    return a


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(map(operator.mul, a, b))


def truncated(vec: Sequence[float], dim: int = CLUSTER_DIM) -> list[float]:
    return normalize(list(vec[:dim]))


class Matrix:
    """Набор нормированных векторов с поиском ближайших (numpy или чистый Python)."""

    def __init__(self, ids: list[int], vectors: list[Sequence[float]]):
        self.ids = ids
        if np is not None and vectors:
            self.m = np.asarray(vectors, dtype=np.float32)
            self.rows = None
        else:
            self.m = None
            # array('f') занимает 4 байта на число, список float — около 32.
            self.rows = list(vectors)

    def __len__(self) -> int:
        return len(self.ids)

    def scores(self, q: Sequence[float]) -> list[float]:
        if not self.ids:
            return []
        if self.m is not None:
            return (self.m @ np.asarray(q, dtype=np.float32)).tolist()
        return [dot(r, q) for r in self.rows]

    def top(self, q: Sequence[float], k: int) -> list[tuple[int, float]]:
        scored = sorted(zip(self.ids, self.scores(q)), key=lambda x: x[1], reverse=True)
        return scored[:k]


def load_matrix(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> Matrix:
    ids, vecs = [], []
    for row in conn.execute(sql, tuple(params)):
        v = unpack(row[1])
        if v is not None:
            ids.append(int(row[0]))
            vecs.append(v)
    return Matrix(ids, vecs)


# --- этап эмбеддингов -------------------------------------------------------------

def ensure_embed_identity(conn: sqlite3.Connection, cfg: Config) -> bool:
    """Векторы разных моделей несравнимы. При смене эмбеддинг-модели все векторы и
    кластеры сбрасываются и пересчитываются. True — если был сброс."""
    stored = store.get_meta(conn, "embed_model")
    if stored == cfg.embed_model:
        return False
    if stored:
        warn(trl("эмбеддинг-модель сменилась ({0} → {1}): пересчитываю все векторы и кластеры", stored, cfg.embed_model))
        for table in ("chunks", "facts", "user_marks", "currency_marks", "sessions"):
            conn.execute(f"UPDATE {table} SET embedding=NULL")
        conn.execute("UPDATE facts SET cluster_id=NULL")
        conn.execute("DELETE FROM clusters")
        conn.execute("DELETE FROM meta WHERE key IN ('embed_dim', 'dirty_clusters', 'marks_fact_seen', 'marks_mark_seen', "
                     "'currency_fact_seen', 'currency_mark_seen')")
    store.set_meta(conn, "embed_model", cfg.embed_model)
    conn.commit()
    return bool(stored)


def embed_missing(conn: sqlite3.Connection, llm: Ollama, cfg: Config) -> bool:
    """Досчитать векторы фрагментов и фактов. False — если эмбеддинг-модель недоступна."""
    jobs = (
        (trl("фрагменты"), "SELECT c.id, c.heading, c.body, n.title FROM chunks c JOIN notes n ON n.path=c.path "
                      "WHERE c.embedding IS NULL AND n.status='indexed' ORDER BY c.id",
         lambda r: embed_doc_text((r["heading"] + "\n" if r["heading"] else "") + r["body"][:6000], r["title"]),
         "UPDATE chunks SET embedding=? WHERE id=?"),
        (trl("факты"), "SELECT id, claim FROM facts WHERE embedding IS NULL ORDER BY id",
         lambda r: embed_doc_text(r["claim"]),
         "UPDATE facts SET embedding=? WHERE id=?"),
        (trl("твои отметки"), "SELECT id, claim FROM user_marks WHERE embedding IS NULL",
         lambda r: embed_doc_text(r["claim"]),
         "UPDATE user_marks SET embedding=? WHERE id=?"),
        (trl("отметки актуальности"), "SELECT id, claim FROM currency_marks WHERE embedding IS NULL",
         lambda r: embed_doc_text(r["claim"]),
         "UPDATE currency_marks SET embedding=? WHERE id=?"),
        (trl("сессии"), "SELECT id, summary AS claim FROM sessions WHERE embedding IS NULL AND summary!=''",
         lambda r: embed_doc_text(r["claim"]),
         "UPDATE sessions SET embedding=? WHERE id=?"),
    )
    for label, sql, to_text, update in jobs:
        rows = conn.execute(sql).fetchall()
        if not rows:
            continue
        batches = [rows[i:i + cfg.embed_batch] for i in range(0, len(rows), cfg.embed_batch)]
        progress = Progress(len(batches), trl("векторы ({0}), пакет", label), every=max(1, len(batches) // 20))
        for batch in batches:
            try:
                vectors = llm.embed([to_text(r) for r in batch])
            except OllamaError as exc:
                warn(trl("векторный этап прерван: {0}. Поиск будет только полнотекстовым до следующего запуска.", exc))
                return False
            dim = store.get_meta(conn, "embed_dim")
            if dim is None:
                store.set_meta(conn, "embed_dim", str(len(vectors[0])))
            elif any(len(v) != int(dim) for v in vectors):
                raise OllamaError(trl("размерность векторов изменилась ({0} → {1}): сменилась модель? Запусти update ещё раз — векторы пересчитаются.", dim, len(vectors[0])))
            for r, v in zip(batch, vectors):
                conn.execute(update, (pack(v), r["id"]))
            conn.commit()
            progress.step()
    return True


def apply_user_marks(conn: sqlite3.Connection, cfg: Config) -> dict[str, int]:
    """Перенести решения пользователя на факты без отметки: близкий по смыслу к
    отвергнутому — отвергнут, к подтверждённому — подтверждён."""
    marks = [(r["mark"], unpack(r["embedding"])) for r in
             conn.execute("SELECT mark, embedding FROM user_marks WHERE embedding IS NOT NULL ORDER BY id")]
    marks = [(m, v) for m, v in marks if v is not None]
    counts = {"rejected": 0, "confirmed": 0}
    if not marks:
        return counts
    # Все факты перепроверяются, только если появились новые отметки; иначе — только новые факты.
    max_mark = store.scalar(conn, "SELECT MAX(id) FROM user_marks WHERE embedding IS NOT NULL") or 0
    max_fact = store.scalar(conn, "SELECT MAX(id) FROM facts") or 0
    seen_mark = int(store.get_meta(conn, "marks_mark_seen", "0") or 0)
    seen_fact = int(store.get_meta(conn, "marks_fact_seen", "0") or 0)
    since = 0 if max_mark > seen_mark else seen_fact
    # То же пространство и порог, что у склейки: что склеилось бы с отмеченным — наследует отметку.
    matrix = Matrix(list(range(len(marks))), [truncated(v) for _, v in marks])
    for row in conn.execute("SELECT id, embedding, cluster_id FROM facts WHERE user_state='none' "
                            "AND embedding IS NOT NULL AND id > ?", (since,)).fetchall():
        v = unpack(row["embedding"])
        if v is None:
            continue
        hits = [i for i, s in zip(matrix.ids, matrix.scores(truncated(v))) if s >= cfg.mark_threshold]
        # Действует самое позднее решение: можно передумать и подтвердить ранее отвергнутое.
        state = marks[max(hits)][0] if hits else None
        if state:
            conn.execute("UPDATE facts SET user_state=? WHERE id=?", (state, row["id"]))
            store.mark_clusters_dirty(conn, [row["cluster_id"]])
            counts[state] += 1
    store.set_meta(conn, "marks_mark_seen", str(max_mark))
    # Факты без вектора ещё не проверены — их отметит следующий запуск.
    unembedded = store.scalar(conn, "SELECT MIN(id) FROM facts WHERE embedding IS NULL")
    store.set_meta(conn, "marks_fact_seen", str((unembedded - 1) if unembedded else max_fact))
    conn.commit()
    return counts


# --- кластеры ------------------------------------------------------------------------

def update_clusters(conn: sqlite3.Connection, cfg: Config, quiet: bool = False) -> dict[str, int]:
    """Инкрементальная склейка: новые факты присоединяются к ближайшему кластеру своей
    области, если сходство с его центром не ниже порога, иначе открывают новый кластер."""
    dirty = store.take_dirty_clusters(conn)
    # Страховка: кластер, чья «голова» удалена, отвергнута или ушла в другой кластер.
    dirty |= {r[0] for r in conn.execute(
        "SELECT cl.id FROM clusters cl LEFT JOIN facts f ON f.id=cl.head_fact_id "
        "WHERE f.id IS NULL OR f.user_state='rejected' OR f.cluster_id IS NOT cl.id")}
    # Отвергнутые факты выходят из кластеров.
    for row in conn.execute("SELECT id, cluster_id FROM facts WHERE user_state='rejected' AND cluster_id IS NOT NULL"):
        dirty.add(row["cluster_id"])
    conn.execute("UPDATE facts SET cluster_id=NULL WHERE user_state='rejected'")

    pools: dict[str, _Pool] = {}
    for row in conn.execute("SELECT id, domain, centroid, size FROM clusters ORDER BY id"):
        v = unpack(row["centroid"])
        if v is not None:
            pools.setdefault(row["domain"], _Pool()).add(row["id"], list(v), row["size"])

    new_facts = conn.execute("SELECT id, domain, embedding FROM facts WHERE cluster_id IS NULL "
                             "AND user_state!='rejected' AND embedding IS NOT NULL ORDER BY id").fetchall()
    stats = {"assigned": 0, "created": 0, "touched": 0, "removed": 0}
    if np is None and len(new_facts) > 10000 and not quiet:
        warn(trl("склеиваю {0} фактов без numpy — это может занять десятки минут. Ускорить: apt install python3-numpy", len(new_facts)))
    progress = (Progress(len(new_facts), trl("кластеризация, факт"), every=max(1, len(new_facts) // 20))
                if new_facts and not quiet else None)
    now = iso_now()
    for row in new_facts:
        q = truncated(unpack(row["embedding"]))
        pool = pools.setdefault(row["domain"], _Pool())
        best_id, best = pool.best(q)
        if best_id is not None and best >= cfg.cluster_threshold:
            pool.merge(best_id, q)
            cid = best_id
            stats["assigned"] += 1
        else:
            cur = conn.execute("INSERT INTO clusters(domain, centroid, size, updated_at) VALUES(?,?,1,?)",
                               (row["domain"], array("f", q).tobytes(), now))
            cid = int(cur.lastrowid)
            pool.add(cid, q, 1)
            stats["created"] += 1
        conn.execute("UPDATE facts SET cluster_id=? WHERE id=?", (cid, row["id"]))
        dirty.add(cid)
        if progress:
            progress.step()
    conn.commit()

    alive = []
    for cid in sorted(c for c in dirty if c is not None):
        if refresh_cluster(conn, cid):
            stats["touched"] += 1
            alive.append(cid)
        else:
            stats["removed"] += 1
    conn.commit()
    from .timeline import refresh_currency  # timeline импортирует этот модуль
    for i in range(0, len(alive), 500):
        refresh_currency(conn, alive[i:i + 500])
    return stats


class _Pool:
    """Центры кластеров одной области; в numpy-режиме — матрица с запасом ёмкости."""

    def __init__(self) -> None:
        self.ids: list[int] = []
        self.pos: dict[int, int] = {}
        self.sizes: list[int] = []
        self.rows: list[list[float]] = []
        self.m = np.zeros((64, CLUSTER_DIM), dtype=np.float32) if np is not None else None

    def add(self, cid: int, vec: Sequence[float], size: int) -> None:
        self.pos[cid] = len(self.ids)
        self.ids.append(cid)
        self.sizes.append(max(size, 1))
        if self.m is not None:
            if len(self.ids) > self.m.shape[0]:
                self.m = np.vstack([self.m, np.zeros_like(self.m)])
            self.m[len(self.ids) - 1] = np.asarray(vec, dtype=np.float32)
        else:
            self.rows.append(list(vec))

    def best(self, q: Sequence[float]) -> tuple[int | None, float]:
        if not self.ids:
            return None, -1.0
        if self.m is not None:
            scores = self.m[:len(self.ids)] @ np.asarray(q, dtype=np.float32)
            k = int(scores.argmax())
            return self.ids[k], float(scores[k])
        best_k, best = 0, -1.0
        for k, row in enumerate(self.rows):
            s = dot(row, q)
            if s > best:
                best_k, best = k, s
        return self.ids[best_k], best

    def merge(self, cid: int, q: Sequence[float]) -> None:
        k = self.pos[cid]
        n = self.sizes[k]
        old = self.m[k].tolist() if self.m is not None else self.rows[k]
        new = normalize([(c * n + x) / (n + 1) for c, x in zip(old, q)])
        self.sizes[k] = n + 1
        if self.m is not None:
            self.m[k] = np.asarray(new, dtype=np.float32)
        else:
            self.rows[k] = new


def refresh_cluster(conn: sqlite3.Connection, cid: int) -> bool:
    """Пересчитать статистику кластера. False — кластер опустел и удалён."""
    members = conn.execute(
        "SELECT id, path, origin, event_date, date_reliable, user_state, quote_match, explicitness, embedding "
        "FROM facts WHERE cluster_id=? AND user_state!='rejected'", (cid,)).fetchall()
    if not members:
        conn.execute("DELETE FROM clusters WHERE id=?", (cid,))
        return False
    paths = {m["path"] for m in members if m["path"]}
    confirmed = any(m["user_state"] == "confirmed" or m["origin"] == "chat" for m in members)
    dated = sorted(m["event_date"] for m in members if m["event_date"] and m["date_reliable"])
    # Центр кластера пересчитываем по фактическим членам (после удалений он мог сместиться).
    vecs = [truncated(unpack(m["embedding"])) for m in members if m["embedding"]]
    centroid = normalize([sum(col) / len(vecs) for col in zip(*vecs)]) if vecs else None

    def rank(m: sqlite3.Row) -> tuple:
        return (m["user_state"] == "confirmed" or m["origin"] == "chat",
                (m["event_date"] or "") if m["date_reliable"] else "",
                m["quote_match"] == "exact", m["explicitness"] == "explicit", m["id"])

    head = max(members, key=rank)
    support = len(paths) + (1 if confirmed else 0)
    last = dated[-1] if dated else None
    score = math.log2(1 + support) + (1.5 if confirmed else 0.0) + _recency(last)
    conn.execute("UPDATE clusters SET size=?, support_notes=?, first_date=?, last_date=?, head_fact_id=?, score=?, "
                 "centroid=COALESCE(?, centroid), updated_at=? WHERE id=?",
                 (len(members), len(paths), dated[0] if dated else None, last, head["id"], round(score, 3),
                  array("f", centroid).tobytes() if centroid else None, iso_now(), cid))
    return True


def _recency(last_date: str | None) -> float:
    if not last_date:
        return 0.0
    try:
        year = int(last_date[:4])
        month = int(last_date[5:7]) if len(last_date) >= 7 else 6
    except ValueError:
        return 0.0
    now = date.today()
    months = (now.year - year) * 12 + (now.month - month)
    if months <= 6:
        return 1.0
    if months <= 18:
        return 0.6
    if months <= 48:
        return 0.25
    return 0.0
