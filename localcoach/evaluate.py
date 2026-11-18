"""Оценка качества памяти и разговоров твоими вердиктами.

Четыре части: актуальность (верно ли «сейчас / в прошлом»), извлечённые факты, пункты
профиля и ответы коуча. На каждый пункт — одна буква. В базе остаются только вердикты и
хэши пунктов, поэтому отчёт (`eval --report`) состоит из чисел и его можно пересылать.

Оценка заодно чинит память там, где это однозначно: неверный факт забывается, неверную
актуальность сразу можно поправить. Профиль только оценивается: ошибка в нём может быть
ошибкой синтеза, а не фактов.
"""
from __future__ import annotations

import math
import random
import sqlite3
from typing import Any

from . import VERSION, store, timeline
from .config import Config, DOMAIN_TITLES, KIND_TITLES, PRIVATE_DOMAINS
from .ollama import Ollama
from .prompts import PROMPT_VERSION
from .util import digest, iso_now

_PRIVATE_SQL = ",".join(f"'{d}'" for d in PRIVATE_DOMAINS) or "''"

# Буква → вердикт. Первый вердикт в каждом наборе — «хорошо», по нему считается точность.
VERDICTS = {
    "currency": {"в": "ok", "н": "wrong"},
    "fact": {"в": "ok", "и": "distorted", "ч": "not_me", "н": "wrong"},
    "profile": {"в": "ok", "ч": "partly", "у": "outdated", "н": "wrong"},
    "reply": {"х": "good", "о": "fine", "м": "miss", "ф": "fact_error", "п": "past_as_present"},
}
VERDICT_TITLES = {
    "ok": "верно", "wrong": "неверно", "distorted": "искажено", "not_me": "не про меня", "partly": "частично",
    "outdated": "устарело", "good": "хорошо", "fine": "нормально", "miss": "мимо", "fact_error": "ошибка в фактах",
    "past_as_present": "прошлое как настоящее",
}
PARTS = {
    "currency": ("Актуальность", "Верно ли определено, что это сейчас или в прошлом?",
                 "в — верно, н — неверно (сразу спрошу, как правильно)"),
    "fact": ("Факты из заметок", "Верно ли модель поняла заметку?",
             "в — верно, и — искажено или преувеличено, ч — это не про меня, н — неверно (забуду)"),
    "profile": ("Профиль", "Верно ли это о тебе сейчас?",
                "в — верно, ч — частично или преувеличено, у — устарело, н — неверно"),
    "reply": ("Ответы коуча", "Как тебе этот ответ?",
              "х — хорошо, о — нормально, м — мимо, ф — ошибка в фактах, п — прошлое выдал за настоящее"),
}
BREAKDOWN_TITLES = {
    "timeline:past": "сверка: в прошлом", "timeline:current": "сверка: сейчас", "recent:current": "свежее",
    "confirmed:current": "подтверждено", "kind:history": "событие", "portrait": "портрет",
    "core_facts": "устойчивое", "current_focus": "сейчас в фокусе", "goals": "цели", "values": "ценности",
    "strengths": "сильные стороны", "tensions": "противоречия", "history": "прошлое",
    "unverified": "не проверено", "patterns": "гипотезы",
}


# Почему пункт профиля неверен — чтобы цифры подсказывали, что чинить.
REASONS = {"в": "invented", "п": "past", "о": "overgeneral", "с": "mixed"}
REASON_TITLES = {"invented": "выдумано", "past": "прошлое как настоящее", "overgeneral": "обобщение из одного случая",
                 "mixed": "смешаны разные вещи"}


def profile_stamp(conn: sqlite3.Connection) -> str:
    """Когда собран текущий профиль: оценки разных сборок нельзя смешивать."""
    profile = store.cache_any(conn, "profile", "final") or {}
    return str(profile.get("generated_at") or "")[:16].replace("T", " ")


def version_tag(cfg: Config, stamp: str = "") -> str:
    heavy = {cfg.heavy_name, cfg.synth_name} - {cfg.model}
    model = cfg.model + "".join(f"+{n}" for n in sorted(heavy))
    return f"{VERSION}|{PROMPT_VERSION}|{model}" + (f"|{stamp}" if stamp else "")


def _rated(conn: sqlite3.Connection, target: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT item_key FROM eval_marks WHERE target=?", (target,))}


# --- выборки ---------------------------------------------------------------------------

def _round_robin(groups: dict[str, list[dict[str, Any]]], n: int) -> list[dict[str, Any]]:
    """Поровну из каждого разреза — иначе редкий источник (сверка) не попадёт в выборку."""
    out, lists = [], [g for g in groups.values() if g]
    while lists and len(out) < n:
        for g in list(lists):
            out.append(g.pop())
            if not g:
                lists.remove(g)
            if len(out) >= n:
                break
    return out


def sample_currency(conn: sqlite3.Connection, n: int, rng: random.Random) -> list[dict[str, Any]]:
    rated = _rated(conn, "currency")
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
            "SELECT cl.id, cl.currency, cl.currency_src, cl.valid_until, cl.period, cl.first_date, cl.last_date, "
            "cl.domain, f.claim FROM clusters cl JOIN facts f ON f.id=cl.head_fact_id "
            "WHERE f.user_state!='rejected' AND cl.currency_src NOT IN ('user', '') "
            f"AND cl.domain NOT IN ({_PRIVATE_SQL})"):
        key = digest(["currency", r["claim"], r["currency"], r["valid_until"], r["period"]])
        if key in rated:
            continue
        item = {**dict(r), "key": key, "breakdown": f"{r['currency_src']}:{r['currency']}"}
        groups.setdefault(item["breakdown"], []).append(item)
    for g in groups.values():
        rng.shuffle(g)
    return _round_robin(groups, n)


def sample_facts(conn: sqlite3.Connection, n: int, rng: random.Random) -> list[dict[str, Any]]:
    rated = _rated(conn, "fact")
    rows = []
    for r in conn.execute(
            "SELECT f.id, f.claim, f.kind, f.domain, f.evidence_quote, f.event_date, n.title FROM facts f "
            "LEFT JOIN notes n ON n.path=f.path WHERE f.origin='note' AND f.user_state!='rejected' "
            f"AND f.domain NOT IN ({_PRIVATE_SQL})"):
        key = digest(["fact", r["claim"], r["evidence_quote"]])
        if key not in rated:
            rows.append({**dict(r), "key": key, "breakdown": r["kind"]})
    rng.shuffle(rows)
    return rows[:n]


def sample_profile(conn: sqlite3.Connection, n: int, rng: random.Random) -> list[dict[str, Any]]:
    from .synth import PROFILE_LISTS
    profile = store.cache_any(conn, "profile", "final")
    if not profile:
        return []
    rated = _rated(conn, "profile")
    items = []
    if profile.get("portrait"):
        items.append({"section": "portrait", "text": profile["portrait"]})
    for key in PROFILE_LISTS:
        items += [{"section": key, "text": it.get("text", "")} for it in profile.get(key, []) if it.get("text")]
    items += [{"section": "patterns", "text": it["hypothesis"]} for it in profile.get("patterns", []) if it.get("hypothesis")]
    out = []
    for it in items:
        it["key"] = digest(["profile", it["section"], it["text"]])
        it["breakdown"] = it["section"]
        if it["key"] not in rated:
            out.append(it)
    rng.shuffle(out)
    # Портрет — всегда первым, если ещё не оценён: это главное, что видит коуч.
    out.sort(key=lambda x: x["section"] != "portrait")
    return out[:n]


def sample_replies(conn: sqlite3.Connection, n: int) -> list[dict[str, Any]]:
    """Только ответы, данные с текущим профилем: старые разговоры оценивают старый профиль."""
    rated = _rated(conn, "reply")
    since = (store.cache_any(conn, "profile", "final") or {}).get("generated_at") or ""
    out = []
    for a in conn.execute("SELECT id, session_id, content, created_at FROM turns WHERE role='assistant' "
                          "AND created_at >= ? ORDER BY id DESC", (since,)):
        key = digest(["reply", a["id"]])
        if key in rated:
            continue
        q = conn.execute("SELECT content FROM turns WHERE session_id=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1",
                         (a["session_id"], a["id"])).fetchone()
        if q:
            out.append({"key": key, "breakdown": "", "question": q["content"], "answer": a["content"],
                        "when": a["created_at"][:10]})
        if len(out) >= n:
            break
    return out


# --- показ пунктов -------------------------------------------------------------------------

def _show(target: str, it: dict[str, Any]) -> None:
    if target == "currency":
        when = timeline.label(it["currency"], it["currency_src"], it["valid_until"], it["period"], it["last_date"])
        if it["currency"] == "history":
            when = "событие прошлого"
        dates = it["last_date"] if it["first_date"] in (None, it["last_date"]) else f"{it['first_date']}…{it['last_date']}"
        print(f"  {it['claim']}")
        print(f"  → {when or 'сейчас'}" + (f"  (упоминания: {dates})" if dates else ""))
    elif target == "fact":
        src = f"заметка «{it['title']}»" if it.get("title") else "заметка"
        print(f"  [{KIND_TITLES.get(it['kind'], it['kind'])}, {DOMAIN_TITLES.get(it['domain'], it['domain'])}] {it['claim']}")
        if it.get("evidence_quote"):
            print(f"  цитата: «{it['evidence_quote']}»")
        print(f"  {src}" + (f", {it['event_date']}" if it.get("event_date") else ""))
    elif target == "profile":
        print(f"  [{BREAKDOWN_TITLES.get(it['section'], it['section'])}] {it['text']}")
    else:
        q = it["question"] if len(it["question"]) <= 400 else it["question"][:400] + "…"
        a = it["answer"] if len(it["answer"]) <= 1500 else it["answer"][:1500] + "…"
        print(f"  {it['when']}, ты: {q}")
        print(f"  коуч: {a}")


def _save(conn: sqlite3.Connection, cfg: Config, target: str, it: dict[str, Any], verdict: str,
          reason: str = "") -> None:
    stamp = profile_stamp(conn) if target in ("profile", "reply") else ""
    conn.execute("INSERT INTO eval_marks(target, item_key, breakdown, verdict, reason, version, created_at) "
                 "VALUES(?,?,?,?,?,?,?)", (target, it["key"], it["breakdown"], verdict, reason,
                                           version_tag(cfg, stamp), iso_now()))
    conn.commit()


def _ask_reason() -> str:
    try:
        ans = input("  Почему? в — выдумано, п — прошлое как настоящее, о — обобщение из одного случая, "
                    "с — смешаны разные вещи, Enter — пропустить: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return ""
    return REASONS.get(ans[:1], "")


def _fix(conn: sqlite3.Connection, llm: Ollama, target: str, it: dict[str, Any], verdict: str) -> None:
    """Однозначные исправления по ходу оценки."""
    if target == "fact" and verdict == "wrong":
        from .chat import _mark
        _mark(conn, llm, [it["id"]], "rejected")
        conn.commit()
        print("  (забыл)")
    elif target == "currency" and verdict == "wrong":
        try:
            ans = input("  Как правильно? с — так и сейчас, б [год] — в прошлом, Enter — не знаю: ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            return
        if ans.startswith("с"):
            timeline.add_mark(conn, it["id"], "current", source="user")
            print("  ✓ так и сейчас")
        elif ans.startswith("б") or timeline._YEAR.search(ans):
            until, period = timeline._years(ans)
            timeline.add_mark(conn, it["id"], "past", until or None, period or None, "user")
            print(f"  ✓ {timeline.label('past', '', until, period, None)}")


def run(conn: sqlite3.Connection, llm: Ollama, cfg: Config, n: int = 10, only: list[str] | None = None) -> int:
    rng = random.Random()
    samples = {
        "currency": lambda: sample_currency(conn, n, rng),
        "fact": lambda: sample_facts(conn, n, rng),
        "profile": lambda: sample_profile(conn, n, rng),
        "reply": lambda: sample_replies(conn, max(3, n // 2)),
    }
    stamp = profile_stamp(conn)
    if stamp and (not only or "profile" in only or "reply" in only):
        print(f"Профиль собран {stamp}; ответы коуча — только из разговоров после этого.")
    targets = [t for t in samples if not only or t in only]
    print("Оценка качества: по каждой части — небольшая случайная выборка. Уже оценённое не повторяется.\n"
          "Enter — пропустить пункт, 0 — к следующей части, 00 — закончить.\n")
    total = 0
    stop = False
    for target in targets:
        items = samples[target]()
        title, question, legend = PARTS[target]
        if not items:
            print(f"— {title}: оценивать нечего.\n")
            continue
        print(f"=== {title} ({len(items)}) — {question}\n    {legend}\n")
        letters = VERDICTS[target]
        for k, it in enumerate(items, 1):
            print(f"[{k}/{len(items)}]")
            _show(target, it)
            while True:
                try:
                    ans = input("  > ").strip().casefold()
                except (EOFError, KeyboardInterrupt):
                    print()
                    ans = "00"
                if ans in ("", "0", "00") or ans[:1] in letters:
                    break
                print(f"    {legend}")
            if ans in ("0", "00"):
                stop = ans == "00"
                break
            if ans:
                verdict = letters[ans[:1]]
                reason = _ask_reason() if target == "profile" and verdict in ("wrong", "partly") else ""
                _save(conn, cfg, target, it, verdict, reason)
                _fix(conn, llm, target, it, verdict)
                total += 1
            print()
        print()
        if stop:
            break
    if total:
        from .vectors import update_clusters
        update_clusters(conn, cfg, quiet=True)
        print(f"Оценено: {total}.\n")
        for line in report_lines(conn, latest_only=True):
            print(line)
        print("\nПолный отчёт по всем версиям: python3 coach.py eval --report")
    return total


# --- отчёт --------------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95%-интервал для доли: на выборке из 10 точность 80% — это 49–94%, а не «80%»."""
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - r) / d, (c + r) / d


def _line(label: str, verdicts: list[str], good: str) -> str:
    n = len(verdicts)
    k = sum(v == good for v in verdicts)
    lo, hi = wilson(k, n)
    others = {}
    for v in verdicts:
        if v != good:
            others[v] = others.get(v, 0) + 1
    rest = ", ".join(f"{VERDICT_TITLES.get(v, v)} {c * 100 // n}%" for v, c in sorted(others.items(), key=lambda x: -x[1]))
    return (f"  {label:<{max(30, len(label) + 1)}} n={n:<3} {VERDICT_TITLES[good]} {k * 100 // n}% ({lo * 100:.0f}–{hi * 100:.0f}%)"
            + (f"; {rest}" if rest else ""))


def report_lines(conn: sqlite3.Connection, latest_only: bool = False) -> list[str]:
    lines = ["Оценка качества — только числа, без содержимого (можно пересылать)."]
    any_rows = False
    for target, (title, _q, _l) in PARTS.items():
        rows = conn.execute("SELECT version, breakdown, verdict, reason FROM eval_marks WHERE target=? ORDER BY id",
                            (target,)).fetchall()
        if not rows:
            continue
        any_rows = True
        good = next(iter(VERDICTS[target].values()))
        versions = list(dict.fromkeys(r["version"] for r in rows))
        if latest_only:
            versions = versions[-1:]
        lines.append(f"\n{title}:")
        for v in versions:
            code, _prompts, model, stamp = (v.split("|") + ["", "", ""])[:4]
            label = f"v{code} · {model}" + (f" · профиль {stamp}" if stamp else "")
            lines.append(_line(label, [r["verdict"] for r in rows if r["version"] == v], good))
        # Разрез по источнику или разделу — по последней версии.
        last = [r for r in rows if r["version"] == versions[-1]]
        cuts: dict[str, list[str]] = {}
        for r in last:
            if r["breakdown"]:
                cuts.setdefault(r["breakdown"], []).append(r["verdict"])
        if len(cuts) > 1:
            for b, vs in sorted(cuts.items(), key=lambda x: -len(x[1])):
                name = BREAKDOWN_TITLES.get(b) or KIND_TITLES.get(b) or b
                lines.append("  " + _line(f"· {name}", vs, good).lstrip())
        reasons: dict[str, int] = {}
        for r in last:
            if r["reason"]:
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        if reasons:
            lines.append("  причины ошибок: " + ", ".join(f"{REASON_TITLES.get(k, k)} {n}" for k, n in
                                                         sorted(reasons.items(), key=lambda x: -x[1])))
    if not any_rows:
        lines.append("Оценок пока нет: python3 coach.py eval")
    return lines
