"""Человекочитаемые файлы памяти: профиль, области, гипотезы, проекты, отчёт."""
from __future__ import annotations

import sqlite3
from typing import Any

from . import store
from .config import Config, DOMAIN_TITLES
from .synth import STATUS_TITLES
from .util import atomic_write, iso_now, safe_slug

AUDIT_MARKS = {"weak": "смягчено аудитом", "outdated": "возможно устарело", "unchecked": "не проверено аудитом"}


def _sources(conn: sqlite3.Connection, ids: list[str], per: int = 3) -> str:
    refs: list[str] = []
    for x in ids:
        if x.startswith("C") and x[1:].isdigit():
            for r in conn.execute("SELECT source_ref, event_date FROM facts WHERE cluster_id=? AND user_state!='rejected' "
                                  "ORDER BY event_date DESC LIMIT ?", (int(x[1:]), per)):
                refs.append(f"`{r['source_ref']}`" + (f" ({r['event_date']})" if r["event_date"] else ""))
        elif x.startswith("H") and x[1:].isdigit():
            r = conn.execute("SELECT source_ref FROM hypotheses_raw WHERE id=?", (int(x[1:]),)).fetchone()
            if r:
                refs.append(f"`{r['source_ref']}`")
    refs = list(dict.fromkeys(refs))
    return "; ".join(refs[:6]) if refs else "—"


def _items(conn, title: str, items: list[dict[str, Any]], key: str = "text", status: bool = False) -> list[str]:
    if not items:
        return []
    lines = [f"## {title}", ""]
    for it in items:
        marks = []
        if status and it.get("status"):
            marks.append(STATUS_TITLES.get(it["status"], it["status"]))
        if it.get("when"):
            marks.append(it["when"])
        if it.get("stale"):
            marks.append(it["stale"])
        if it.get("audit") in AUDIT_MARKS:
            marks.append(AUDIT_MARKS[it["audit"]])
        if it.get("confirmed"):
            marks.append("подтверждено тобой")
        elif it.get("support") == 1:
            marks.append("одно упоминание")
        elif it.get("support"):
            marks.append(f"заметок: {it['support']}")
        lines.append(f"- {it.get(key, '')}" + (f" *({', '.join(marks)})*" if marks else ""))
        if it.get("original"):
            lines.append(f"  было: {it['original']}")
        lines.append(f"  источники: {_sources(conn, it.get('ids') or it.get('for') or [])}")
    lines.append("")
    return lines


def _patterns(conn, title: str, items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return []
    lines = [f"## {title}", "", "> Гипотезы, а не факты: их стоит проверять вопросом, а не принимать на веру.", ""]
    for p in items:
        lines.append(f"- **{p['hypothesis']}**" + (f" *(уверенность: {p.get('confidence')})*" if p.get("confidence") else ""))
        lines.append(f"  за: {_sources(conn, p.get('for', []))}")
        if p.get("against"):
            lines.append(f"  против: {_sources(conn, p['against'])}")
        for a in p.get("alternatives", []):
            lines.append(f"  альтернатива: {a}")
        if p.get("question"):
            lines.append(f"  проверить вопросом: {p['question']}")
    lines.append("")
    return lines


def _plain(title: str, items: list[dict[str, Any]], key: str = "text") -> list[str]:
    texts = [it.get(key, "").strip() for it in items if it.get(key, "").strip()]
    return [f"## {title}", "", *[f"- {t}" for t in texts], ""] if texts else []


def plain_profile(profile: dict[str, Any] | None) -> str:
    """Профиль для чтения: только содержание — без источников, дат, пометок аудита и служебных разделов."""
    if not profile:
        return "# Профиль\n\nПрофиль ещё не построен.\n"
    lines = ["# Профиль", ""]
    if profile.get("portrait"):
        lines += [profile["portrait"].strip(), ""]
    for title, key in (("Устойчивое", "core_facts"), ("Сейчас в фокусе", "current_focus"), ("Цели", "goals"),
                       ("Ценности", "values"), ("Ресурсы и сильные стороны", "strengths"),
                       ("Противоречия", "tensions"), ("Прошлое", "history"),
                       ("Не проверено, актуально ли сейчас", "unverified")):
        lines += _plain(title, profile.get(key, []))
    if profile.get("patterns"):
        lines += ["## Гипотезы", "", "> Догадки модели, а не факты.", ""]
        lines += [f"- {p['hypothesis']}" for p in profile["patterns"] if p.get("hypothesis")] + [""]
    if profile.get("coach_notes"):
        lines += ["## Как лучше разговаривать", ""] + [f"- {x}" for x in profile["coach_notes"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


def write_all(conn: sqlite3.Connection, cfg: Config, domains: dict[str, dict[str, Any]],
              profile: dict[str, Any] | None) -> None:
    mem = cfg.memory_dir
    (mem / "domains").mkdir(parents=True, exist_ok=True)
    head = ["", f"Обновлено: {iso_now()}", "",
            "> Черновик памяти, собранный локальной моделью по заметкам. Не подтверждённые факты о всей жизни: "
            "у каждого пункта есть источники, одно упоминание — не черта характера. Исправлять — через "
            "`coach.py review` и команду /forget в чате.", ""]

    # --- профиль
    lines = ["# Опорный профиль", *head]
    if profile:
        if profile.get("portrait"):
            lines += ["## Портрет", "", profile["portrait"], ""]
        lines += _items(conn, "Устойчивое", profile.get("core_facts", []))
        lines += _items(conn, "Сейчас в фокусе", profile.get("current_focus", []))
        lines += _items(conn, "Цели", profile.get("goals", []), status=True)
        lines += _items(conn, "Ценности", profile.get("values", []))
        lines += _items(conn, "Ресурсы и сильные стороны", profile.get("strengths", []))
        lines += _items(conn, "Противоречия", profile.get("tensions", []))
        lines += _items(conn, "Прошлое", profile.get("history", []))
        lines += _items(conn, "Не проверено, актуально ли сейчас", profile.get("unverified", []))
        lines += _patterns(conn, "Гипотезы", profile.get("patterns", []))
        if profile.get("coach_notes"):
            lines += ["## Как лучше разговаривать", ""] + [f"- {x}" for x in profile["coach_notes"]] + [""]
        if profile.get("removed"):
            lines += ["## Убрано аудитом", ""] + [f"- {r['text']} — {r['note']}" for r in profile["removed"]] + [""]
    else:
        lines.append("Профиль ещё не построен.")
    atomic_write(mem / "profile.md", "\n".join(lines) + "\n")
    atomic_write(mem / "profile_plain.md", plain_profile(profile))

    # --- области
    for old in (mem / "domains").glob("*.md"):
        old.unlink()
    all_patterns: list[tuple[str, dict]] = []
    for domain, d in domains.items():
        title = DOMAIN_TITLES.get(domain, domain)
        lines = [f"# {title}", *head]
        if d.get("overview"):
            lines += [d["overview"], ""]
        lines += _items(conn, "Главное", d.get("key_points", []))
        lines += _items(conn, "Цели", d.get("goals", []), status=True)
        lines += _items(conn, "Что изменилось", d.get("changes", []))
        lines += _items(conn, "Противоречия", d.get("tensions", []))
        lines += _patterns(conn, "Гипотезы", d.get("patterns", []))
        lines += _items(conn, "Возможные рычаги", d.get("levers", []), key="idea")
        if d.get("open_questions"):
            lines += ["## Открытые вопросы", ""] + [f"- {q['question']}" + (f" — {q['why']}" if q.get("why") else "")
                                                    for q in d["open_questions"]] + [""]
        all_patterns += [(title, p) for p in d.get("patterns", [])]
        atomic_write(mem / "domains" / f"{safe_slug(domain)}.md", "\n".join(lines) + "\n")

    # --- все гипотезы
    lines = ["# Гипотезы о паттернах", *head]
    for title, p in all_patterns:
        lines += _patterns(conn, title, [p])
    atomic_write(mem / "hypotheses.md", "\n".join(lines) + "\n")

    # --- проекты и идеи отдельно от целей жизни
    rows = conn.execute(
        "SELECT cl.domain, f.claim, f.intent_status, f.source_ref, cl.last_date FROM clusters cl "
        "JOIN facts f ON f.id=cl.head_fact_id WHERE f.is_project=1 AND f.user_state!='rejected' "
        "ORDER BY cl.domain, cl.score DESC").fetchall()
    lines = ["# Проекты и идеи", *head,
             "> Упоминание идеи не значит, что проект начат или что это актуальная цель.", ""]
    current = None
    for r in rows:
        if r["domain"] != current:
            lines += ["", f"## {DOMAIN_TITLES.get(r['domain'], r['domain'])}", ""]
            current = r["domain"]
        st = STATUS_TITLES.get(r["intent_status"], "")
        lines.append(f"- {r['claim']}" + (f" *({st})*" if st else "") + f" — `{r['source_ref']}`")
    atomic_write(mem / "projects_and_ideas.md", "\n".join(lines) + "\n")

    write_cluster_review(conn, cfg)
    write_status(conn, cfg)


def write_cluster_review(conn: sqlite3.Connection, cfg: Config) -> None:
    """Самые крупные кластеры с участниками — чтобы проверить порог склейки."""
    lines = ["# Проверка склейки фактов", "",
             f"Порог сходства: {cfg.cluster_threshold} (LOCAL_COACH_CLUSTER_THRESHOLD).",
             "Если в одном кластере оказываются разные по смыслу утверждения — порог стоит поднять (0.92–0.94); "
             "если одинаковые утверждения разбросаны по разным кластерам — опустить (0.87–0.88). "
             "После смены порога: `coach.py recluster`.", ""]
    for cl in conn.execute("SELECT id, domain, size, support_notes FROM clusters WHERE size>1 "
                           "ORDER BY size DESC LIMIT 30"):
        lines.append(f"## C{cl['id']} — {DOMAIN_TITLES.get(cl['domain'], cl['domain'])}, фактов {cl['size']}, "
                     f"заметок {cl['support_notes']}")
        for f in conn.execute("SELECT claim, event_date FROM facts WHERE cluster_id=? AND user_state!='rejected' "
                              "ORDER BY event_date LIMIT 12", (cl["id"],)):
            lines.append(f"- {f['claim']}" + (f" ({f['event_date']})" if f["event_date"] else ""))
        lines.append("")
    atomic_write(cfg.memory_dir / "clusters_review.md", "\n".join(lines) + "\n")


_STATUS_SQL = {
    "notes": "SELECT COUNT(*) FROM notes WHERE status='indexed'",
    "held": "SELECT COUNT(*) FROM notes WHERE status='security_hold'",
    "stale": "SELECT COUNT(*) FROM notes WHERE status='stale'",
    "d_fm": "SELECT COUNT(*) FROM notes WHERE status='indexed' AND date_source='frontmatter'",
    "d_fn": "SELECT COUNT(*) FROM notes WHERE status='indexed' AND date_source='filename'",
    "d_mt": "SELECT COUNT(*) FROM notes WHERE status='indexed' AND date_source LIKE 'mtime%'",
    "d_imp": "SELECT COUNT(*) FROM notes WHERE status='indexed' AND date_source='import'",
    "chunks": "SELECT COUNT(*) FROM chunks",
    "c_done": "SELECT COUNT(*) FROM chunks WHERE status='done'",
    "c_imp": "SELECT COUNT(*) FROM chunks WHERE status='imported'",
    "c_pend": "SELECT COUNT(*) FROM chunks WHERE status='pending'",
    "c_err": "SELECT COUNT(*) FROM chunks WHERE status='error'",
    "facts": "SELECT COUNT(*) FROM facts WHERE user_state!='rejected'",
    "f_exact": "SELECT COUNT(*) FROM facts WHERE quote_match='exact' AND user_state!='rejected'",
    "f_conf": "SELECT COUNT(*) FROM facts WHERE user_state='confirmed' OR origin='chat'",
    "f_rej": "SELECT COUNT(*) FROM facts WHERE user_state='rejected'",
    "clusters": "SELECT COUNT(*) FROM clusters",
    "cl_multi": "SELECT COUNT(*) FROM clusters WHERE size>1",
    "cl_max": "SELECT MAX(size) FROM clusters",
    "v_chunks": "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL",
    "v_facts": "SELECT COUNT(*) FROM facts WHERE embedding IS NOT NULL",
    "sessions": "SELECT COUNT(*) FROM sessions WHERE summary!=''",
    "pending": "SELECT COUNT(*) FROM candidates WHERE status='pending'",
    "t_cur": "SELECT COUNT(*) FROM clusters WHERE currency='current'",
    "t_past": "SELECT COUNT(*) FROM clusters WHERE currency='past'",
    "t_hist": "SELECT COUNT(*) FROM clusters WHERE currency='history'",
    "t_unk": "SELECT COUNT(*) FROM clusters WHERE currency='unknown'",
    "t_user": "SELECT COUNT(DISTINCT created_at) FROM currency_marks WHERE source='user'",
    "d_open": "SELECT COUNT(*) FROM commitments WHERE status='open'",
    "d_done": "SELECT COUNT(*) FROM commitments WHERE status='done'",
    "d_drop": "SELECT COUNT(*) FROM commitments WHERE status='dropped'",
    "p_all": "SELECT COUNT(*) FROM people WHERE merged_into IS NULL AND status!='ignored' AND mentions>0",
    "p_known": "SELECT COUNT(*) FROM people WHERE merged_into IS NULL AND mentions>0 "
               "AND (status IN ('current','past') OR relation_src='user')",
    "checkins": "SELECT COUNT(*) FROM checkins",
    "checkin_last": "SELECT COALESCE(substr(MAX(created_at), 1, 10), '—') FROM checkins",
}


def status_lines(conn: sqlite3.Connection) -> list[str]:
    n = {k: store.scalar(conn, sql) or 0 for k, sql in _STATUS_SQL.items()}
    return [
        f"Заметок: {n['notes']} (отложено фильтром секретов: {n['held']}, удалено из vault: {n['stale']})",
        f"Даты заметок: из шапки {n['d_fm']}, из имени файла {n['d_fn']}, только по mtime {n['d_mt']}; "
        f"импортированных файлов: {n['d_imp']}",
        f"Фрагментов: {n['chunks']} — готово {n['c_done']}, из v3 {n['c_imp']}, ждут {n['c_pend']}, "
        f"с ошибкой {n['c_err']}",
        f"Фактов: {n['facts']} (цитата найдена точно: {n['f_exact']}, подтверждено тобой: {n['f_conf']}, "
        f"отвергнуто: {n['f_rej']})",
        f"Кластеров: {n['clusters']} (из нескольких фактов: {n['cl_multi']}, крупнейший: {n['cl_max']})",
        f"Векторы: фрагменты {n['v_chunks']}, факты {n['v_facts']}",
        f"Актуальность кластеров: сейчас {n['t_cur']}, в прошлом {n['t_past']}, события {n['t_hist']}, "
        f"не проверено {n['t_unk']}; твоих ответов: {n['t_user']}",
        f"Сессий: {n['sessions']}; ждут решения в /review: {n['pending']}",
        f"Договорённостей: открыто {n['d_open']}, сделано {n['d_done']}, брошено {n['d_drop']}",
        f"Людей: {n['p_all']} (уточнено тобой: {n['p_known']}); чек-инов: {n['checkins']}, последний {n['checkin_last']}",
        f"Последнее обновление: {store.get_meta(conn, 'last_update', '—')}",
    ]


def write_status(conn: sqlite3.Connection, cfg: Config) -> None:
    lines = ["# Local Coach — состояние", "", *[f"- {x}" for x in status_lines(conn)], "",
             "## Файлы", "",
             f"- Профиль: `{cfg.memory_dir / 'profile.md'}`",
             f"- Области жизни: `{cfg.memory_dir / 'domains'}`",
             f"- Гипотезы: `{cfg.memory_dir / 'hypotheses.md'}`",
             f"- Проекты и идеи: `{cfg.memory_dir / 'projects_and_ideas.md'}`",
             f"- Проверка склейки: `{cfg.memory_dir / 'clusters_review.md'}`",
             f"- Люди: `{cfg.memory_dir / 'people.md'}`",
             f"- База: `{cfg.db_path}`", ""]
    atomic_write(cfg.workspace / "STATUS.md", "\n".join(lines) + "\n")
