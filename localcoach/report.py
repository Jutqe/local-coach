"""Человекочитаемые файлы памяти: профиль, области, гипотезы, проекты, отчёт."""
from __future__ import annotations

import sqlite3
from typing import Any

from . import store
from .config import Config, DOMAIN_TITLES
from .synth import STATUS_TITLES
from .util import atomic_write, iso_now, safe_slug
from .i18n import trl

AUDIT_MARKS = {"weak": trl("смягчено аудитом"), "outdated": trl("возможно устарело"), "unchecked": trl("не проверено аудитом")}


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
            marks.append(trl("подтверждено тобой"))
        elif it.get("support") == 1:
            marks.append(trl("одно упоминание"))
        elif it.get("support"):
            marks.append(trl("заметок: {0}", it['support']))
        lines.append(f"- {it.get(key, '')}" + (f" *({', '.join(marks)})*" if marks else ""))
        if it.get("original"):
            lines.append(trl("  было: {0}", it['original']))
        lines.append(trl("  источники: {0}", _sources(conn, it.get('ids') or it.get('for') or [])))
    lines.append("")
    return lines


def _patterns(conn, title: str, items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return []
    lines = [f"## {title}", "", trl("> Гипотезы, а не факты: их стоит проверять вопросом, а не принимать на веру."), ""]
    for p in items:
        lines.append(f"- **{p['hypothesis']}**" + (trl(" *(уверенность: {0})*", p.get('confidence')) if p.get("confidence") else ""))
        lines.append(trl("  за: {0}", _sources(conn, p.get('for', []))))
        if p.get("against"):
            lines.append(trl("  против: {0}", _sources(conn, p['against'])))
        for a in p.get("alternatives", []):
            lines.append(trl("  альтернатива: {0}", a))
        if p.get("question"):
            lines.append(trl("  проверить вопросом: {0}", p['question']))
    lines.append("")
    return lines


def _plain(title: str, items: list[dict[str, Any]], key: str = "text") -> list[str]:
    texts = [it.get(key, "").strip() for it in items if it.get(key, "").strip()]
    return [f"## {title}", "", *[f"- {t}" for t in texts], ""] if texts else []


def plain_profile(profile: dict[str, Any] | None) -> str:
    """Профиль для чтения: только содержание — без источников, дат, пометок аудита и служебных разделов."""
    if not profile:
        return trl("# Профиль\n\nПрофиль ещё не построен.\n")
    lines = [trl("# Профиль"), ""]
    if profile.get("portrait"):
        lines += [profile["portrait"].strip(), ""]
    for title, key in (("Устойчивое", "core_facts"), ("Сейчас в фокусе", "current_focus"), ("Цели", "goals"),
                       ("Ценности", "values"), ("Ресурсы и сильные стороны", "strengths"),
                       ("Противоречия", "tensions"), ("Прошлое", "history"),
                       ("Не проверено, актуально ли сейчас", "unverified")):
        lines += _plain(title, profile.get(key, []))
    if profile.get("patterns"):
        lines += [trl("## Гипотезы"), "", trl("> Догадки модели, а не факты."), ""]
        lines += [f"- {p['hypothesis']}" for p in profile["patterns"] if p.get("hypothesis")] + [""]
    if profile.get("coach_notes"):
        lines += [trl("## Как лучше разговаривать"), ""] + [f"- {x}" for x in profile["coach_notes"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


def write_all(conn: sqlite3.Connection, cfg: Config, domains: dict[str, dict[str, Any]],
              profile: dict[str, Any] | None) -> None:
    mem = cfg.memory_dir
    (mem / "domains").mkdir(parents=True, exist_ok=True)
    head = ["", trl("Обновлено: {0}", iso_now()), "",
            trl("> Черновик памяти, собранный локальной моделью по заметкам. Не подтверждённые факты о всей жизни: у каждого пункта есть источники, одно упоминание — не черта характера. Исправлять — через `coach.py review` и команду /forget в чате."), ""]

    # --- профиль
    lines = [trl("# Опорный профиль"), *head]
    if profile:
        if profile.get("portrait"):
            lines += [trl("## Портрет"), "", profile["portrait"], ""]
        lines += _items(conn, trl("Устойчивое"), profile.get("core_facts", []))
        lines += _items(conn, trl("Сейчас в фокусе"), profile.get("current_focus", []))
        lines += _items(conn, trl("Цели"), profile.get("goals", []), status=True)
        lines += _items(conn, trl("Ценности"), profile.get("values", []))
        lines += _items(conn, trl("Ресурсы и сильные стороны"), profile.get("strengths", []))
        lines += _items(conn, trl("Противоречия"), profile.get("tensions", []))
        lines += _items(conn, trl("Прошлое"), profile.get("history", []))
        lines += _items(conn, trl("Не проверено, актуально ли сейчас"), profile.get("unverified", []))
        lines += _patterns(conn, trl("Гипотезы"), profile.get("patterns", []))
        if profile.get("coach_notes"):
            lines += [trl("## Как лучше разговаривать"), ""] + [f"- {x}" for x in profile["coach_notes"]] + [""]
        if profile.get("removed"):
            lines += [trl("## Убрано аудитом"), ""] + [f"- {r['text']} — {r['note']}" for r in profile["removed"]] + [""]
    else:
        lines.append(trl("Профиль ещё не построен."))
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
        lines += _items(conn, trl("Главное"), d.get("key_points", []))
        lines += _items(conn, trl("Цели"), d.get("goals", []), status=True)
        lines += _items(conn, trl("Что изменилось"), d.get("changes", []))
        lines += _items(conn, trl("Противоречия"), d.get("tensions", []))
        lines += _patterns(conn, trl("Гипотезы"), d.get("patterns", []))
        lines += _items(conn, trl("Возможные рычаги"), d.get("levers", []), key="idea")
        if d.get("open_questions"):
            lines += [trl("## Открытые вопросы"), ""] + [f"- {q['question']}" + (f" — {q['why']}" if q.get("why") else "")
                                                    for q in d["open_questions"]] + [""]
        all_patterns += [(title, p) for p in d.get("patterns", [])]
        atomic_write(mem / "domains" / f"{safe_slug(domain)}.md", "\n".join(lines) + "\n")

    # --- все гипотезы
    lines = [trl("# Гипотезы о паттернах"), *head]
    for title, p in all_patterns:
        lines += _patterns(conn, title, [p])
    atomic_write(mem / "hypotheses.md", "\n".join(lines) + "\n")

    # --- проекты и идеи отдельно от целей жизни
    rows = conn.execute(
        "SELECT cl.domain, f.claim, f.intent_status, f.source_ref, cl.last_date FROM clusters cl "
        "JOIN facts f ON f.id=cl.head_fact_id WHERE f.is_project=1 AND f.user_state!='rejected' "
        "ORDER BY cl.domain, cl.score DESC").fetchall()
    lines = [trl("# Проекты и идеи"), *head,
             trl("> Упоминание идеи не значит, что проект начат или что это актуальная цель."), ""]
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
    lines = [trl("# Проверка склейки фактов"), "",
             trl("Порог сходства: {0} (LOCAL_COACH_CLUSTER_THRESHOLD).", cfg.cluster_threshold),
             trl("Если в одном кластере оказываются разные по смыслу утверждения — порог стоит поднять (0.92–0.94); если одинаковые утверждения разбросаны по разным кластерам — опустить (0.87–0.88). После смены порога: `coach.py recluster`."), ""]
    for cl in conn.execute("SELECT id, domain, size, support_notes FROM clusters WHERE size>1 "
                           "ORDER BY size DESC LIMIT 30"):
        lines.append(trl("## C{0} — {1}, фактов {2}, заметок {3}", cl['id'], DOMAIN_TITLES.get(cl['domain'], cl['domain']), cl['size'], cl['support_notes']))
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
        trl("Заметок: {0} (отложено фильтром секретов: {1}, удалено из vault: {2})", n['notes'], n['held'], n['stale']),
        trl("Даты заметок: из шапки {0}, из имени файла {1}, только по mtime {2}; импортированных файлов: {3}", n['d_fm'], n['d_fn'], n['d_mt'], n['d_imp']),
        trl("Фрагментов: {0} — готово {1}, из v3 {2}, ждут {3}, с ошибкой {4}", n['chunks'], n['c_done'], n['c_imp'], n['c_pend'], n['c_err']),
        trl("Фактов: {0} (цитата найдена точно: {1}, подтверждено тобой: {2}, отвергнуто: {3})", n['facts'], n['f_exact'], n['f_conf'], n['f_rej']),
        trl("Кластеров: {0} (из нескольких фактов: {1}, крупнейший: {2})", n['clusters'], n['cl_multi'], n['cl_max']),
        trl("Векторы: фрагменты {0}, факты {1}", n['v_chunks'], n['v_facts']),
        trl("Актуальность кластеров: сейчас {0}, в прошлом {1}, события {2}, не проверено {3}; твоих ответов: {4}", n['t_cur'], n['t_past'], n['t_hist'], n['t_unk'], n['t_user']),
        trl("Сессий: {0}; ждут решения в /review: {1}", n['sessions'], n['pending']),
        trl("Договорённостей: открыто {0}, сделано {1}, брошено {2}", n['d_open'], n['d_done'], n['d_drop']),
        trl("Людей: {0} (уточнено тобой: {1}); чек-инов: {2}, последний {3}", n['p_all'], n['p_known'], n['checkins'], n['checkin_last']),
        trl("Последнее обновление: {0}", store.get_meta(conn, 'last_update', '—')),
    ]


def write_status(conn: sqlite3.Connection, cfg: Config) -> None:
    lines = [trl("# Local Coach — состояние"), "", *[f"- {x}" for x in status_lines(conn)], "",
             trl("## Файлы"), "",
             trl("- Профиль: `{0}`", cfg.memory_dir / 'profile.md'),
             trl("- Области жизни: `{0}`", cfg.memory_dir / 'domains'),
             trl("- Гипотезы: `{0}`", cfg.memory_dir / 'hypotheses.md'),
             trl("- Проекты и идеи: `{0}`", cfg.memory_dir / 'projects_and_ideas.md'),
             trl("- Проверка склейки: `{0}`", cfg.memory_dir / 'clusters_review.md'),
             trl("- Люди: `{0}`", cfg.memory_dir / 'people.md'),
             trl("- База: `{0}`", cfg.db_path), ""]
    atomic_write(cfg.workspace / "STATUS.md", "\n".join(lines) + "\n")
