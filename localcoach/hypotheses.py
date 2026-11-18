"""Life Reset: реестр психологических гипотез.

Источник – осторожные предположения о повторяемости, которые извлечение уже
находит в заметках (`hypotheses_raw`, всегда с цитатой и альтернативой). Здесь они
склеиваются по смыслу, получают формулировку «Возможно, …», другие объяснения и
вопрос для проверки. Статус меняет только человек: «подтверждена» значит «да, это
повторяется» – описание, а не черта личности и не диагноз. Уверенность код ставит
по числу независимых заметок и никогда не поднимает выше «средней» сам.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import guard, store, vectors
from .config import Config
from .ollama import Ollama, OllamaError, TruncatedError, embed_doc_text
from .prompts import LR_HYPOTHESIS_SCHEMA, LR_HYPOTHESIS_SYSTEM
from .util import digest, dumps, iso_now, loads, today, warn

SAME = 0.85
BATCH = 8
STATUS_TITLES = {"new": "новая", "testing": "проверяется", "pattern": "подтверждена как повторяемость",
                 "rejected": "отвергнута"}
CONFIDENCE_TITLES = {"low": "низкая", "medium": "средняя", "high": "высокая"}


def _raw(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT h.*, c.chunk_date, n.title FROM hypotheses_raw h LEFT JOIN chunks c ON c.id=h.chunk_id "
                        "LEFT JOIN notes n ON n.path=h.path WHERE n.status='indexed' ORDER BY h.id").fetchall()


def _confidence(observations: list[dict[str, Any]]) -> str:
    notes = {o.get("ref", "").split("#")[0] for o in observations}
    return "medium" if len(notes) >= 3 else "low"


def build(conn: sqlite3.Connection, llm: Ollama, cfg: Config, quiet: bool = False) -> dict[str, int]:
    """Добавить в реестр новые наблюдения из заметок. Статусы и ответы не трогаются."""
    stats = {"raw": 0, "new": 0, "observations": 0}
    seen = set(loads(store.get_meta(conn, "lr_hyp_seen"), []) or [])
    fresh = [r for r in _raw(conn) if digest([r["hypothesis"], r["evidence_quote"]]) not in seen]
    stats["raw"] = len(fresh)
    if not fresh:
        return stats
    try:
        vecs = []
        texts = [embed_doc_text(r["hypothesis"]) for r in fresh]
        for i in range(0, len(texts), cfg.embed_batch):
            vecs += [vectors.normalize(v) for v in llm.embed(texts[i:i + cfg.embed_batch])]
    except OllamaError as exc:
        warn(f"гипотезы: векторы не посчитаны ({exc}); повторный запуск продолжит.")
        return stats
    existing = [(r["id"], vectors.unpack(r["embedding"])) for r in
                conn.execute("SELECT id, embedding FROM lr_hypotheses WHERE embedding IS NOT NULL")]
    existing = [(i, v) for i, v in existing if v is not None]
    # Сначала – к уже известным; остальные склеиваем между собой.
    groups: list[dict[str, Any]] = []
    handled: set[str] = set()     # «просмотрено» – только то, что записано или осознанно отброшено
    for r, v in zip(fresh, vecs):
        d = digest([r["hypothesis"], r["evidence_quote"]])
        obs = {"quote": r["evidence_quote"][:300], "ref": r["source_ref"], "date": (r["chunk_date"] or "")[:10],
               "title": r["title"] or ""}
        best_id, best = None, 0.0
        for iid, ev in existing:
            s = vectors.dot(v, ev)
            if s > best:
                best_id, best = iid, s
        if best_id is not None and best >= SAME:
            row = conn.execute("SELECT observations_json, status FROM lr_hypotheses WHERE id=?", (best_id,)).fetchone()
            ob = loads(row["observations_json"], []) or []
            if obs["ref"] not in {o.get("ref") for o in ob}:
                ob.append(obs)
                conf = _confidence(ob)
                conn.execute("UPDATE lr_hypotheses SET observations_json=?, confidence=CASE WHEN confidence='high' "
                             "THEN confidence ELSE ? END WHERE id=?", (dumps(ob[-12:]), conf, best_id))
                stats["observations"] += 1
            handled.add(d)
            continue
        for g in groups:
            if vectors.dot(v, g["vec"]) >= SAME:
                g["obs"].append(obs)
                g["alts"].append(r["alternative"])
                g["digests"].append(d)
                break
        else:
            groups.append({"vec": v, "text": r["hypothesis"], "obs": [obs], "alts": [r["alternative"]], "digests": [d]})
    for i in range(0, len(groups), BATCH):
        batch = groups[i:i + BATCH]
        try:
            res = _phrase(llm, batch)
        except OllamaError as exc:
            warn(f"гипотезы: формулировка прервана ({exc}); повторный запуск продолжит.")
            break
        for j, g in enumerate(batch):
            got = res.get(j)
            if got is None:
                continue          # модель пропустила – в следующий раз ещё попытка
            if got.get("dup") is not None:
                if batch[got["dup"]].get("inserted"):
                    handled.update(g["digests"])   # наблюдения ушли в ту, что записана
                continue
            text = got["text"] or g["text"]
            if guard.text_hold(text):
                handled.update(g["digests"])
                continue
            alts = [a for a in got["alternatives"] if a] or [a for a in g["alts"] if a][:3]
            conn.execute("INSERT INTO lr_hypotheses(text, observations_json, alternatives_json, question, confidence, "
                         "embedding, created_at) VALUES(?,?,?,?,?,?,?)",
                         (text[:400], dumps(g["obs"][:12]), dumps(alts[:4]), got["question"][:300],
                          _confidence(g["obs"]), vectors.pack(g["vec"]), iso_now()))
            g["inserted"] = True
            handled.update(g["digests"])
            stats["new"] += 1
    seen |= handled
    store.set_meta(conn, "lr_hyp_seen", dumps(sorted(seen)))
    conn.commit()
    return stats


def _phrase(llm: Ollama, batch: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    ids = {f"H{j + 1}": j for j in range(len(batch))}
    user = "Гипотезы:\n\n" + "\n\n".join(
        f"H{j + 1}: {g['text']}\n  цитаты: " + " | ".join(f"«{o['quote'][:160]}»" for o in g["obs"][:3])
        + "\n  альтернатива из заметки: " + (next((a for a in g["alts"] if a), "–"))
        for j, g in enumerate(batch))
    try:
        raw = llm.chat_json(LR_HYPOTHESIS_SYSTEM, user, LR_HYPOTHESIS_SCHEMA, num_predict=300 * len(batch) + 300,
                            attempts=2,
                            heavy=True)
    except TruncatedError:
        if len(batch) == 1:
            return {}
        half = len(batch) // 2
        lo, hi = _phrase(llm, batch[:half]), _phrase(llm, batch[half:])
        return {**lo, **{k + half: v for k, v in hi.items()}}
    out: dict[int, dict[str, Any]] = {}
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        j = ids.get(str(it.get("id") or "").strip().upper())
        if j is None:
            continue
        dup = ids.get(str(it.get("same_as") or "").strip().upper())
        if dup is not None and dup != j and dup < j:
            batch[dup]["obs"] += batch[j]["obs"]
        out[j] = {"text": str(it.get("text") or "").strip(), "question": str(it.get("question") or "").strip(),
                  "alternatives": [str(a).strip() for a in (it.get("alternatives") or []) if str(a).strip()],
                  "dup": dup if dup is not None and dup < j else None}
    return out


def _ask(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def show(r: sqlite3.Row) -> str:
    lines = [f"[H-{r['id']}] {r['text']}",
             f"Статус: {STATUS_TITLES[r['status']]}; уверенность: {CONFIDENCE_TITLES[r['confidence']]}"]
    obs = loads(r["observations_json"], []) or []
    if obs:
        lines.append("Наблюдения:")
        for o in obs[:4]:
            lines.append(f"  - «{o.get('quote', '')}»" + (f" («{o['title']}»" if o.get("title") else " (")
                         + (f", {o['date'][:7]})" if o.get("date") else ")"))
    alts = loads(r["alternatives_json"], []) or []
    if alts:
        lines.append("Другие объяснения: " + "; ".join(alts))
    if r["user_note"]:
        lines.append(f"Твои слова раньше: {r['user_note'][-300:]}")
    if r["question"]:
        lines.append(f"Вопрос для проверки: {r['question']}")
    return "\n".join(lines)


def review(conn: sqlite3.Connection, limit: int = 5) -> int:
    rows = conn.execute("SELECT * FROM lr_hypotheses WHERE status IN ('new', 'testing') "
                        "ORDER BY status='testing' DESC, confidence='medium' DESC, id LIMIT ?", (limit,)).fetchall()
    if not rows:
        print("Гипотез для проверки нет. Новые появляются из заметок: python3 coach.py hypotheses build\n")
        return 0
    print("Гипотезы – это предположения, не факты и не диагнозы. Отвечай, как видишь сам.\n"
          "п – да, это повторяется; о – не про меня; т – пусть пока проверяется; или ответь на вопрос своими словами; "
          "Enter – пропустить; «стоп» – закончить. На трудный вопрос можно не отвечать сейчас.\n")
    n = 0
    for r in rows:
        print(show(r))
        ans = _ask("> ")
        if ans is None or re.fullmatch(r"(стоп|хватит|/stop|q)", ans or "", re.I):
            break
        if not ans:
            print()
            continue
        if guard.text_hold(ans):
            print("Не записал: похоже на пароль или токен.\n")
            continue
        key = ans.casefold().rstrip(".")
        status, note = {"п": "pattern", "о": "rejected", "т": "testing"}.get(key, "testing"), ""
        if key not in ("п", "о", "т"):
            note = ans
        merged = "; ".join(x for x in (r["user_note"], f"{today()}: {note}" if note else "") if x)[-1500:]
        conn.execute("UPDATE lr_hypotheses SET status=?, user_note=?, checked_at=? WHERE id=?",
                     (status, merged, iso_now(), r["id"]))
        conn.commit()
        n += 1
        print(f"Записал: {STATUS_TITLES[status]}.\n")
    return n


def coach_lines(conn: sqlite3.Connection, limit: int = 5) -> list[str]:
    rows = conn.execute("SELECT text, status, user_note FROM lr_hypotheses WHERE status IN ('pattern', 'testing') "
                        "ORDER BY status='pattern' DESC, checked_at DESC LIMIT ?", (limit,)).fetchall()
    return [f"- {r['text']} [{STATUS_TITLES[r['status']]}]" + (f"; его слова: {r['user_note'][-160:]}" if r["user_note"] else "")
            for r in rows]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM lr_hypotheses GROUP BY status")}
