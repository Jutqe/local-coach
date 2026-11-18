"""Синтез: обзоры областей жизни, опорный профиль и его аудит.

Модель получает кластеры фактов с короткими id (C12) и ссылается на них; ссылки
проверяются кодом. Выход аудита применяется к профилю, а не просто сохраняется.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import store, timeline
from .config import Config, DOMAIN_TITLES, DOMAINS, KIND_TITLES, PRIVATE_DOMAINS
from .ollama import Ollama, OllamaError, TruncatedError
from .prompts import (AUDIT_SCHEMA, AUDIT_SYSTEM, DOMAIN_SCHEMA, DOMAIN_SYSTEM, PORTRAIT_FIX_SCHEMA,
                      PORTRAIT_FIX_SYSTEM, PROFILE_SCHEMA, PROFILE_SYSTEM, PROMPT_VERSION)
from .util import digest, iso_now, warn

STATUS_TITLES = {"idea": "идея", "wanted": "хочет", "planned": "в планах", "in_progress": "в процессе",
                 "completed": "сделано", "abandoned": "отказался", "unclear": "статус неясен", "none": ""}


# --- представление кластеров -------------------------------------------------------

def cluster_rows(conn: sqlite3.Connection, where: str = "", params: tuple = (), limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT cl.id, cl.domain, cl.size, cl.support_notes, cl.first_date, cl.last_date, cl.score, "
        "cl.currency, cl.currency_src, cl.valid_until, cl.period, "
        "f.claim, f.kind, f.intent_status, f.explicitness, f.is_project, f.date_reliable, "
        "EXISTS(SELECT 1 FROM facts x WHERE x.cluster_id=cl.id AND (x.user_state='confirmed' OR x.origin='chat')) AS confirmed "
        "FROM clusters cl JOIN facts f ON f.id=cl.head_fact_id "
        "WHERE f.user_state!='rejected' " + where +
        # Актуальное — выше, прошедшее — ниже: синтез видит в первую очередь нынешнюю жизнь.
        " ORDER BY cl.score + CASE cl.currency WHEN 'current' THEN 1.0 WHEN 'past' THEN -1.0 ELSE 0 END DESC, "
        "cl.id LIMIT ?",
        (*params, limit)).fetchall()


def _earlier_claim(conn: sqlite3.Connection, cid: int, head_claim: str, last_date: str | None) -> str:
    """Самая ранняя датированная формулировка, если кластер тянется больше года."""
    row = conn.execute("SELECT claim, event_date FROM facts WHERE cluster_id=? AND date_reliable=1 AND event_date IS NOT NULL "
                       "AND user_state!='rejected' ORDER BY event_date LIMIT 1", (cid,)).fetchone()
    if not row or not last_date or row["claim"] == head_claim:
        return ""
    if last_date[:4].isdigit() and row["event_date"][:4].isdigit() and int(last_date[:4]) - int(row["event_date"][:4]) >= 1:
        return f"\n    раньше ({row['event_date']}): {row['claim']}"
    return ""


def cluster_line(conn: sqlite3.Connection, r: sqlite3.Row) -> str:
    kind = KIND_TITLES.get(r["kind"], r["kind"])
    status = STATUS_TITLES.get(r["intent_status"], "")
    if r["first_date"] and r["last_date"] and r["first_date"] != r["last_date"]:
        when = f"{r['first_date']}…{r['last_date']}"
    else:
        when = r["last_date"] or "дата неизвестна"
    flags = [f"заметок: {r['support_notes']}"]
    when_label = timeline.label(r["currency"], r["currency_src"], r["valid_until"], r["period"], r["last_date"])
    if when_label:
        flags.insert(0, when_label)
    if r["confirmed"]:
        flags.append("подтверждено в разговоре")
    if r["explicitness"] == "inferred":
        flags.append("вывод, не прямые слова")
    if r["is_project"]:
        flags.append("из папки проектов/идей")
    head = f"{kind} ({status})" if status else kind
    return (f"C{r['id']} | {head} | {when} | {', '.join(flags)} | {r['claim']}"
            + _earlier_claim(conn, r["id"], r["claim"], r["last_date"]))


def hypothesis_domains(conn: sqlite3.Connection) -> dict[int, str]:
    """Область гипотезы — самая частая область фактов того же фрагмента (или заметки для импорта v3)."""
    by_chunk: dict[int, dict[str, int]] = {}
    by_path: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT chunk_id, path, domain, COUNT(*) AS n FROM facts WHERE path IS NOT NULL "
                          "GROUP BY chunk_id, path, domain"):
        if r["chunk_id"] is not None:
            by_chunk.setdefault(r["chunk_id"], {})[r["domain"]] = r["n"]
        counts = by_path.setdefault(r["path"], {})
        counts[r["domain"]] = counts.get(r["domain"], 0) + r["n"]
    out: dict[int, str] = {}
    for h in conn.execute("SELECT id, chunk_id, path FROM hypotheses_raw"):
        counts = by_chunk.get(h["chunk_id"]) if h["chunk_id"] is not None else None
        counts = counts or by_path.get(h["path"])
        if counts:
            out[h["id"]] = max(counts.items(), key=lambda kv: kv[1])[0]
    return out


def _hypothesis_lines(conn: sqlite3.Connection, ids_for_domain: list[int], limit: int = 8) -> tuple[list[str], set[str]]:
    wanted = sorted(ids_for_domain, reverse=True)[:limit]
    if not wanted:
        return [], set()
    rows = conn.execute("SELECT id, hypothesis, evidence_quote, alternative FROM hypotheses_raw WHERE id IN (%s) "
                        "ORDER BY id DESC" % ",".join("?" * len(wanted)), wanted).fetchall()
    lines, ids = [], set()
    for r in rows:
        ids.add(f"H{r['id']}")
        alt = f" Альтернатива: {r['alternative']}" if r["alternative"] else ""
        lines.append(f"H{r['id']} | {r['hypothesis']} (цитата: «{r['evidence_quote'][:200]}»){alt}")
    return lines, ids


# --- проверка ответов модели ------------------------------------------------------------

def _clean_ids(value: Any, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for x in value:
        s = str(x).strip().upper().lstrip("#")
        if s in allowed and s not in out:
            out.append(s)
    return out


def _clean_items(items: Any, allowed: set[str], text_key: str = "text", need_ids: bool = True,
                 extra: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not str(it.get(text_key) or "").strip():
            continue
        ids = _clean_ids(it.get("ids"), allowed)
        if need_ids and not ids:
            continue
        clean = {text_key: str(it[text_key]).strip(), "ids": ids}
        for k in extra:
            if k in it:
                clean[k] = it[k]
        out.append(clean)
    return out


def _clean_patterns(items: Any, allowed: set[str]) -> list[dict[str, Any]]:
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not str(it.get("hypothesis") or "").strip():
            continue
        support = _clean_ids(it.get("for"), allowed)
        if not support:
            continue
        out.append({"hypothesis": str(it["hypothesis"]).strip(), "for": support,
                    "against": _clean_ids(it.get("against"), allowed),
                    "alternatives": [str(a) for a in (it.get("alternatives") or []) if str(a).strip()][:2],
                    "confidence": it.get("confidence") if it.get("confidence") in ("low", "medium") else "low",
                    "question": str(it.get("question") or "").strip()})
    return out


_STATUS_ORDER = {"idea": 1, "wanted": 2, "planned": 3, "in_progress": 4, "completed": 5}


def _cluster_ids(ids: list[str]) -> list[int]:
    return [int(x[1:]) for x in ids if x.startswith("C") and x[1:].isdigit()]


def clamp_status(conn: sqlite3.Connection, ids: list[str], status: str | None) -> str:
    """Статус цели не может быть выше, чем у исходных фактов: модель любит превращать
    «хочу» в «делает». Проверяется кодом, а не просьбой в промпте."""
    cids = _cluster_ids(ids)
    if not cids:
        return status or "unclear"
    rows = conn.execute("SELECT intent_status, event_date FROM facts WHERE cluster_id IN (%s) AND user_state!='rejected'"
                        % ",".join("?" * len(cids)), cids).fetchall()
    seen = [r["intent_status"] for r in rows if r["intent_status"] in _STATUS_ORDER or r["intent_status"] == "abandoned"]
    if status == "abandoned":
        return status if "abandoned" in seen else "unclear"
    if status not in _STATUS_ORDER:
        return "unclear"  # «неясно» не повышаем до статуса из фактов
    ceiling = max((_STATUS_ORDER[s] for s in seen if s in _STATUS_ORDER), default=0)
    if status in _STATUS_ORDER and _STATUS_ORDER[status] <= ceiling:
        return status
    if ceiling:
        return next(k for k, v in _STATUS_ORDER.items() if v == ceiling)
    return "unclear"


def support_of(conn: sqlite3.Connection, ids: list[str]) -> tuple[int, bool]:
    """Сколько независимых заметок подтверждают пункт и подтверждал ли его пользователь."""
    cids = _cluster_ids(ids)
    if not cids:
        return 0, False
    marks = ",".join("?" * len(cids))
    notes = store.scalar(conn, f"SELECT COUNT(DISTINCT path) FROM facts WHERE cluster_id IN ({marks}) "
                               "AND user_state!='rejected' AND path IS NOT NULL", cids) or 0
    confirmed = bool(store.scalar(conn, f"SELECT COUNT(*) FROM facts WHERE cluster_id IN ({marks}) "
                                        "AND (user_state='confirmed' OR origin='chat')", cids))
    return int(notes), confirmed


def _call(llm: Ollama, cfg: Config, system: str, user: str, schema: dict, label: str) -> dict[str, Any]:
    try:
        return llm.chat_json(system, user, schema, think=cfg.think_synth, num_predict=cfg.synth_predict,
                             model=cfg.synth_name)
    except TruncatedError:
        print(f"  {label}: ответ не влез, повторяю с просьбой сократить формулировки.", flush=True)
        return llm.chat_json(system + "\n\nФормулируй каждый пункт одним коротким предложением.", user, schema,
                             think=False, num_predict=cfg.synth_predict + 1500, model=cfg.synth_name)


# --- области жизни ---------------------------------------------------------------------

def synthesize_domains(conn: sqlite3.Connection, llm: Ollama, cfg: Config) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    hyp_domain = hypothesis_domains(conn)
    for domain in DOMAINS:
        if domain in PRIVATE_DOMAINS:
            continue
        rows = cluster_rows(conn, "AND cl.domain=?", (domain,), cfg.synth_clusters_per_domain)
        if not rows:
            continue
        lines = [cluster_line(conn, r) for r in rows]
        hyp_lines, hyp_ids = _hypothesis_lines(conn, [h for h, d in hyp_domain.items() if d == domain])
        allowed = {f"C{r['id']}" for r in rows} | hyp_ids
        body = ("Утверждения (по убыванию важности):\n" + "\n".join(lines)
                + ("\n\nГипотезы из заметок:\n" + "\n".join(hyp_lines) if hyp_lines else ""))
        user = f"Область: {DOMAIN_TITLES[domain]} ({domain}). Сегодня: {iso_now()[:10]}.\n" + body
        # В отпечаток идёт месяц, а не день: иначе синтез пересчитывался бы каждые сутки.
        fingerprint = digest([PROMPT_VERSION, cfg.synth_name, domain, iso_now()[:7], body])
        cached = store.cache_get(conn, "domain", domain, fingerprint)
        if cached is not None:
            results[domain] = cached
            continue
        if len(rows) < 3:
            # Слишком мало данных для обзора — без вызова модели.
            result = {"overview": "", "key_points": [{"text": r["claim"], "ids": [f"C{r['id']}"]} for r in rows],
                      "goals": [], "changes": [], "patterns": [], "tensions": [], "open_questions": [], "levers": []}
        else:
            print(f"  Обзор области «{DOMAIN_TITLES[domain]}»: {len(rows)} кластеров", flush=True)
            try:
                raw = _call(llm, cfg, DOMAIN_SYSTEM, user, DOMAIN_SCHEMA, domain)
            except OllamaError as exc:
                warn(f"обзор области {domain} не построен ({type(exc).__name__}); остаётся прежний, если был")
                prev = store.cache_any(conn, "domain", domain)
                if prev is not None:
                    results[domain] = prev
                continue
            result = {
                "overview": str(raw.get("overview") or "").strip(),
                "key_points": _clean_items(raw.get("key_points"), allowed),
                "goals": [{**g, "status": clamp_status(conn, g["ids"], g.get("status"))}
                          for g in _clean_items(raw.get("goals"), allowed, extra=("status",))],
                "changes": _clean_items(raw.get("changes"), allowed),
                "patterns": _clean_patterns(raw.get("patterns"), allowed),
                "tensions": _clean_items(raw.get("tensions"), allowed),
                "open_questions": [{"question": str(q.get("question")).strip(), "why": str(q.get("why") or "").strip()}
                                   for q in raw.get("open_questions") or [] if isinstance(q, dict) and q.get("question")],
                "levers": _clean_items(raw.get("levers"), allowed, text_key="idea", extra=("why",)),
            }
        result["domain"] = domain
        result["clusters"] = [r["id"] for r in rows]
        store.cache_put(conn, "domain", domain, fingerprint, result)
        results[domain] = result
    # Области, где больше нет данных, убираем из кэша.
    for row in conn.execute("SELECT key FROM artifacts WHERE stage='domain'").fetchall():
        if row["key"] not in results:
            conn.execute("DELETE FROM artifacts WHERE stage='domain' AND key=?", (row["key"],))
    conn.commit()
    return results


# --- профиль -------------------------------------------------------------------------------

PROFILE_LISTS = ("core_facts", "current_focus", "goals", "values", "strengths", "tensions", "history", "unverified")


# Глагол прошедшего времени: «работал», «жила», «воспитывали», «занимался». Грубо, но для
# сравнения двух версий одного текста хватает.
_PAST_VERB = re.compile(r"\b(?:[а-яё]{2,}[аяиеыу](?:л|ла|ли|лся|лась|лись)|(?:бы|жи)(?:л|ла|ли))\b")


def past_verbs(text: str) -> int:
    return len(_PAST_VERB.findall((text or "").lower()))


def user_name(conn: sqlite3.Connection, cfg: Config) -> str:
    return (cfg.user_name or store.get_meta(conn, "user_name") or "").strip()


def name_line(name: str) -> str:
    return (f"Имя: {name}. Называй его так или «он»." if name else
            "Имя: не задано. Не называй его по имени — пиши «он»; имена в утверждениях — это другие люди.")


def _leading_name(text: str) -> str:
    m = re.match(r"\s*([А-ЯЁ][а-яё]+)\s+[а-яё]", text or "")
    return m.group(1) if m else ""


def portrait_ok(new: str, old: str, name: str, others: set[str]) -> bool:
    """Переписанный портрет берём, только если он не стал «прошлее» и не назвал человека чужим именем."""
    if not new or len(new) < len(old) * 0.5:
        return False
    if past_verbs(new) > past_verbs(old):
        return False
    lead = _leading_name(new)
    return not (lead and lead != name and lead in others)


def fix_portrait(conn: sqlite3.Connection, llm: Ollama, cfg: Config, portrait: str) -> str:
    """Портрет — свободный текст модели: проверяем время и имя и при нужде переписываем одним вызовом."""
    name = user_name(conn, cfg)
    others = {r[0] for r in conn.execute("SELECT name FROM people WHERE merged_into IS NULL")} - {name}
    lead = _leading_name(portrait)
    # Первое слово с большой буквы перед глаголом — скорее всего имя; не то, что задано, — повод переписать.
    wrong_name = bool(lead and lead != name and lead not in ("Он", "Его", "Ему"))
    if past_verbs(portrait) < 2 and not wrong_name:
        return portrait
    past = past_claims(conn)
    user = (name_line(name) + "\n\nПРОШЛОЕ (уже не так):\n" + ("\n".join(f"- {x}" for x in past) or "—")
            + "\n\nПОРТРЕТ:\n" + portrait)
    try:
        raw = _call(llm, cfg, PORTRAIT_FIX_SYSTEM, user, PORTRAIT_FIX_SCHEMA, "портрет")
    except OllamaError as exc:
        warn(f"портрет не переписан ({type(exc).__name__})")
        return portrait
    new = str(raw.get("portrait") or "").strip()
    return new if portrait_ok(new, portrait, name, others | ({lead} if wrong_name else set())) else portrait


def build_profile(conn: sqlite3.Connection, llm: Ollama, cfg: Config,
                  domains: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    top = cluster_rows(conn, "AND cl.domain NOT IN (%s)" % ",".join("?" * len(PRIVATE_DOMAINS)),
                       tuple(PRIVATE_DOMAINS), 40)
    allowed = {f"C{r['id']}" for r in top}
    parts = []
    for domain, d in domains.items():
        allowed |= {f"C{c}" for c in d.get("clusters", [])}
        for p in d.get("patterns", []):
            allowed |= {x for x in p.get("for", []) + p.get("against", []) if x.startswith("H")}
        block = [f"## {DOMAIN_TITLES.get(domain, domain)}"]
        if d.get("overview"):
            block.append(d["overview"])
        # Пункты обзоров — пересказ модели; пометку времени берём заново из опоры.
        for p in d.get("key_points", [])[:6]:
            block.append(f"- {p['text']} [{', '.join(p['ids'])}]{time_note(conn, p['ids'])}")
        for g in d.get("goals", [])[:4]:
            block.append(f"- цель ({STATUS_TITLES.get(g.get('status'), '')}): {g['text']} [{', '.join(g['ids'])}]"
                         f"{time_note(conn, g['ids'])}")
        for c in d.get("changes", [])[:2]:
            block.append(f"- изменение: {c['text']} [{', '.join(c['ids'])}]")
        for p in d.get("patterns", [])[:2]:
            block.append(f"- гипотеза: {p['hypothesis']} [за: {', '.join(p['for'])}; против: {', '.join(p['against']) or '—'}]")
        for t in d.get("tensions", [])[:2]:
            block.append(f"- противоречие: {t['text']} [{', '.join(t['ids'])}]")
        parts.append("\n".join(block))
    if not parts and not top:
        return None
    body = ("ОБЗОРЫ ОБЛАСТЕЙ:\n\n" + "\n\n".join(parts)
            + "\n\nСАМЫЕ ПОДТВЕРЖДЁННЫЕ УТВЕРЖДЕНИЯ ВСЕХ ОБЛАСТЕЙ:\n" + "\n".join(cluster_line(conn, r) for r in top))
    # Грубая защита контекста: ~3,5 символа на токен, оставляем место под ответ.
    max_chars = int((cfg.num_ctx - cfg.synth_predict - 1500) * 3.2)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n[вход сокращён]"
    user = f"Сегодня: {iso_now()[:10]}. {name_line(user_name(conn, cfg))}\n\n" + body
    fingerprint = digest([PROMPT_VERSION, cfg.synth_name, iso_now()[:7], user_name(conn, cfg), body])
    cached = store.cache_get(conn, "profile", "draft", fingerprint)
    if cached is not None:
        return cached
    print("  Сборка опорного профиля", flush=True)
    try:
        raw = _call(llm, cfg, PROFILE_SYSTEM, user, PROFILE_SCHEMA, "профиль")
    except OllamaError as exc:
        warn(f"профиль не построен ({type(exc).__name__}); остаётся прежний")
        return store.cache_any(conn, "profile", "draft")
    profile: dict[str, Any] = {"portrait": fix_portrait(conn, llm, cfg, str(raw.get("portrait") or "").strip())}
    for key in PROFILE_LISTS:
        profile[key] = _clean_items(raw.get(key), allowed, extra=("status",) if key == "goals" else ())
    profile["goals"] = [{**g, "status": clamp_status(conn, g["ids"], g.get("status"))} for g in profile["goals"]]
    profile["patterns"] = _clean_patterns(raw.get("patterns"), allowed)
    profile["coach_notes"] = [re.sub(r"\s*\(?\b[CH]\d+\b\)?", "", str(x)).strip()
                              for x in raw.get("coach_notes") or [] if str(x).strip()][:5]
    time_guard(conn, profile)
    store.cache_put(conn, "profile", "draft", fingerprint, profile)
    return profile


def _currencies(conn: sqlite3.Connection, ids: list[str]) -> list[sqlite3.Row]:
    cids = _cluster_ids(ids)
    if not cids:
        return []
    return conn.execute(f"SELECT currency, currency_src, valid_until, period, last_date FROM clusters "
                        f"WHERE id IN ({','.join('?' * len(cids))})", cids).fetchall()


def time_note(conn: sqlite3.Connection, ids: list[str]) -> str:
    """« (в прошлом, до 2017)» — если вся опора пункта в прошлом; « (актуальность не проверена…)» — если
    актуальной опоры нет и она давняя; иначе пусто."""
    rows = _currencies(conn, ids)
    if not rows:
        return ""
    states = {r["currency"] for r in rows}
    if "current" in states:
        return ""
    past = [r for r in rows if r["currency"] == "past"]
    if past and states <= {"past", "history"}:
        return f" ({timeline.label('past', '', past[0]['valid_until'], past[0]['period'], None)})"
    newest = max((r["last_date"] or "" for r in rows), default="")
    age = timeline.months_ago(newest)
    if age is None or age > timeline.FRESH_MONTHS:
        return f" ({timeline.label('unknown', '', None, None, newest)})"
    return ""


def past_claims(conn: sqlite3.Connection, limit: int = 15) -> list[str]:
    """Что уже в прошлом — для проверки портрета, свободного текста без ссылок."""
    return [f"{r['claim']} — {timeline.label('past', '', r['valid_until'], r['period'], None)}" for r in conn.execute(
        "SELECT f.claim, cl.valid_until, cl.period FROM clusters cl JOIN facts f ON f.id=cl.head_fact_id "
        f"WHERE cl.currency='past' AND f.user_state!='rejected' AND cl.domain NOT IN ({','.join('?' * len(PRIVATE_DOMAINS))}) "
        "ORDER BY cl.score DESC LIMIT ?", (*PRIVATE_DOMAINS, limit))]


def time_guard(conn: sqlite3.Connection, profile: dict[str, Any]) -> None:
    """Прошлое не выдаётся за настоящее — проверяется кодом по актуальности опоры:
    - пункт, вся опора которого в прошлом, уходит в «Прошлое»;
    - «Сейчас в фокусе» без актуальной опоры уходит в «Не проверено»;
    - пункт только с давней непроверенной опорой получает пометку."""
    history = profile.setdefault("history", [])
    unverified = profile.setdefault("unverified", [])
    for key in ("core_facts", "current_focus", "goals", "values", "strengths", "tensions"):
        keep = []
        for it in profile.get(key, []):
            rows = _currencies(conn, it.get("ids", []))
            states = {r["currency"] for r in rows}
            if rows and states <= {"past", "history"}:
                past = [r for r in rows if r["currency"] == "past"]
                when = timeline.label("past", "", past[0]["valid_until"], past[0]["period"], None) if past else ""
                history.append({**it, "moved_from": key, "when": when})
                continue
            if "current" not in states:
                newest = max((r["last_date"] or "" for r in rows), default="")
                age = timeline.months_ago(newest)
                if key == "current_focus":
                    unverified.append({**it, "moved_from": key, "stale": timeline.label("unknown", "", None, None, newest)})
                    continue
                if age is None or age > timeline.STALE_MONTHS:
                    it = {**it, "stale": timeline.label("unknown", "", None, None, newest)}
            keep.append(it)
        profile[key] = keep


# --- аудит ----------------------------------------------------------------------------------

def _evidence(conn: sqlite3.Connection, ids: list[str], per_cluster: int = 2) -> list[str]:
    lines = []
    for cid in ids[:3]:
        if cid.startswith("H") and cid[1:].isdigit():
            h = conn.execute("SELECT hypothesis, evidence_quote FROM hypotheses_raw WHERE id=?", (int(cid[1:]),)).fetchone()
            if h:
                lines.append(f"  {cid}: гипотеза из заметки — {h['hypothesis']} — цитата: «{h['evidence_quote'][:220]}»")
            continue
        if not cid.startswith("C") or not cid[1:].isdigit():
            continue
        rows = conn.execute(
            "SELECT claim, evidence_quote, event_date, date_reliable, origin FROM facts WHERE cluster_id=? "
            "AND user_state!='rejected' ORDER BY (quote_match='exact') DESC, event_date DESC LIMIT ?",
            (int(cid[1:]), per_cluster)).fetchall()
        meta = conn.execute("SELECT support_notes, first_date, last_date, currency, currency_src, valid_until, period "
                            "FROM clusters WHERE id=?",
                            (int(cid[1:]),)).fetchone()
        if not meta:
            continue
        when = timeline.label(meta["currency"], meta["currency_src"], meta["valid_until"], meta["period"], meta["last_date"])
        lines.append(f"  {cid}: заметок {meta['support_notes']}, даты {meta['first_date'] or '?'}…{meta['last_date'] or '?'}"
                     + (f", {when}" if when else ""))
        for r in rows:
            q = f" — цитата: «{r['evidence_quote'][:220]}»" if r["evidence_quote"] else (
                " — сказано в разговоре" if r["origin"] == "chat" else " — без цитаты")
            lines.append(f"    · {r['claim']} ({r['event_date'] or 'без даты'}){q}")
    return lines


def audit_profile(conn: sqlite3.Connection, llm: Ollama, cfg: Config, profile: dict[str, Any]) -> dict[str, Any]:
    """Проверить пункты профиля по опорным цитатам и применить вердикты."""
    items: list[tuple[str, str, dict[str, Any]]] = []
    for key in PROFILE_LISTS:
        for it in profile.get(key, []):
            items.append((key, it["text"], it))
    for it in profile.get("patterns", []):
        items.append(("patterns", it["hypothesis"], it))
    if profile.get("portrait"):
        # Портрет — свободный текст; проверяем его по опоре трёх главных пунктов.
        anchor = [x for key in ("core_facts", "current_focus", "goals") for it in profile.get(key, [])[:1]
                  for x in it.get("ids", [])[:1]]
        items.append(("portrait", profile["portrait"], {"text": profile["portrait"], "ids": anchor}))
    past = past_claims(conn)
    fingerprint = digest([PROMPT_VERSION, cfg.synth_name, profile, past])
    cached = store.cache_get(conn, "profile", "final", fingerprint)
    if cached is not None:
        return cached

    verdicts: dict[str, dict[str, Any]] = {}
    failed = False
    batch = 15
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        blocks = []
        for i, (key, text, it) in enumerate(chunk, start + 1):
            ids = it.get("ids") or it.get("for") or []
            block = f"P{i} [{key}]: {text}\n" + "\n".join(_evidence(conn, ids))
            if key == "portrait" and past:
                block += ("\n  Уже в прошлом — если портрет описывает это как настоящее, это outdated: "
                          "safer_text — тот же портрет в настоящем времени без этого (не переводи весь портрет "
                          "в прошедшее время):\n" + "\n".join(f"    · {x}" for x in past))
            blocks.append(block)
        print(f"  Аудит профиля: пункты {start + 1}–{start + len(chunk)} из {len(items)}", flush=True)
        try:
            raw = _call(llm, cfg, AUDIT_SYSTEM, "\n\n".join(blocks), AUDIT_SCHEMA, "аудит")
        except OllamaError as exc:
            warn(f"аудит части профиля не выполнен ({type(exc).__name__}); эти пункты остаются непроверенными")
            failed = True
            continue
        for v in raw.get("items") or []:
            if isinstance(v, dict) and str(v.get("id", "")).upper().startswith("P"):
                verdicts[str(v["id"]).upper()] = v

    final = {"portrait": profile.get("portrait", ""), "coach_notes": profile.get("coach_notes", []),
             "removed": [], "generated_at": iso_now()}
    for key in (*PROFILE_LISTS, "patterns"):
        final[key] = []
    for i, (key, text, it) in enumerate(items, 1):
        v = verdicts.get(f"P{i}")
        entry = dict(it)
        entry["audit"] = v.get("verdict") if v else "unchecked"
        entry["support"], entry["confirmed"] = support_of(conn, it.get("ids") or it.get("for") or [])
        if key == "portrait":
            if v and v.get("verdict") == "unsupported":
                final["portrait"] = ""
                final["removed"].append({"section": key, "text": text, "note": str(v.get("note") or "")})
            elif v and v.get("verdict") in ("weak", "outdated") and \
                    portrait_ok(str(v.get("safer_text") or "").strip(), text, user_name(conn, cfg), set()):
                final["portrait"] = str(v["safer_text"]).strip()
            final["portrait_audit"] = entry["audit"]
            continue
        if v:
            if v.get("verdict") == "unsupported":
                final["removed"].append({"section": key, "text": text, "note": str(v.get("note") or "")})
                continue
            if v.get("verdict") == "weak" and str(v.get("safer_text") or "").strip():
                entry["original"] = text
                entry["hypothesis" if key == "patterns" else "text"] = str(v["safer_text"]).strip()
            if str(v.get("ask_user") or "").strip():
                _queue_question(conn, str(v["ask_user"]).strip(), text, entry)
        final[key].append(entry)
    # Неполный аудит сохраняем с другим отпечатком: следующий запуск его повторит.
    store.cache_put(conn, "profile", "final", fingerprint + (":partial" if failed else ""), final)
    return final


def _queue_question(conn: sqlite3.Connection, question: str, claim: str, entry: dict[str, Any]) -> None:
    exists = store.scalar(conn, "SELECT COUNT(*) FROM candidates WHERE source='audit' AND claim=?", (claim,))
    if exists:
        return
    ids = entry.get("ids") or entry.get("for") or []
    target = None
    if ids and ids[0].startswith("C") and ids[0][1:].isdigit():
        target = store.scalar(conn, "SELECT head_fact_id FROM clusters WHERE id=?", (int(ids[0][1:]),))
    domain = store.scalar(conn, "SELECT domain FROM facts WHERE id=?", (target,)) if target else "other"
    conn.execute("INSERT INTO candidates(source, claim, kind, domain, reason, target_fact_id, created_at) "
                 "VALUES('audit', ?, 'fact', ?, ?, ?, ?)", (claim, domain or "other", question, target, iso_now()))
    conn.commit()
