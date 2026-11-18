"""Life Reset: программа поверх памяти коуча – этапы, обзор, глубина разговора, блок для
коуча и человекочитаемые итоги в Obsidian.

Процессом управляет код: этап, очереди, сроки пересмотра и показатели считаются из
базы, а не моделью. Этапы последовательны по смыслу, но не строго: к прошлым можно
возвращаться, а этап можно назначить вручную.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import audit, hypotheses, inventory, store, vectors
from .bodydouble import _minutes, item_minutes
from .config import LIFE_RESET_DIR, LR_AREAS, LR_PARKED, LR_STATUS_TITLES, LR_STATUSES, LR_TYPE_TITLES, Config
from .util import atomic_write, iso_now, loads, today
from .i18n import trl

STAGES = (
    ("audit", trl("Аудит жизни"), trl("что сейчас происходит во всех областях: что устраивает, что беспокоит")),
    ("archaeology", trl("Раскопки прошлого"), trl("что накопилось за годы: проекты, идеи, обещания, вопросы")),
    ("triage", trl("Разбор накопленного"), trl("у каждого важного пункта своя судьба")),
    ("reflection", trl("Глубокая рефлексия"), trl("повторяющиеся сценарии, роли, противоречия")),
    ("design", trl("Проектирование будущего"), trl("направления, ценности, сценарии и эксперименты")),
    ("continuous", trl("Сопровождение"), trl("регулярные обзоры, чтобы снежный ком не собрался снова")),
)
STAGE_KEYS = [s[0] for s in STAGES]
DEPTHS = {
    1: (trl("практический"), trl("Глубина разговора – 1, практическая: проекты, дела, быт, решения. Без психологических интерпретаций и разговоров о прошлом, пока он сам о них не заговорит.")),
    2: (trl("аналитический"), trl("Глубина разговора – 2, аналитическая: можно замечать закономерности, причины решений, противоречия, повторы – как наблюдения с опорой на эпизоды и гипотезы, которые проверяются вопросом.")),
    3: (trl("глубокий"), trl("Глубина разговора – 3, глубокая рефлексия: самоопределение, роли, ожидания, страхи, прошлый опыт. Бережно, по одному вопросу; на трудный вопрос он может не отвечать сразу – предложи паузу или смену темы, если видишь, что тяжело. Не ставь диагнозов и не делай выводов о травмах по заметкам.")),
}
DEFAULT_DEPTH = 2
RELEVANT = 0.45         # пункт реестра «к теме разговора» (запрос против названия и сути; на настоящей модели не подбирался)


# --- этап и глубина --------------------------------------------------------------------------

def depth(conn: sqlite3.Connection) -> int:
    try:
        d = int(store.get_meta(conn, "lr_depth") or DEFAULT_DEPTH)
    except ValueError:
        d = DEFAULT_DEPTH
    return d if d in DEPTHS else DEFAULT_DEPTH


def set_depth(conn: sqlite3.Connection, value: int) -> bool:
    if value not in DEPTHS:
        return False
    store.set_meta(conn, "lr_depth", str(value))
    conn.commit()
    return True


def derived_stage(conn: sqlite3.Connection) -> str:
    """Этап по состоянию базы: где сейчас основная незавершённая работа."""
    if not audit.latest_round(conn):
        return "audit"
    if not store.get_meta(conn, "lr_built_at"):
        return "archaeology"
    m = inventory.metrics(conn)
    if m["total"] and m["coverage"] < m["total"]:
        return "triage"
    if not store.scalar(conn, "SELECT COUNT(*) FROM lr_hypotheses WHERE status IN ('pattern', 'rejected')"):
        return "reflection"
    return "design"


def stage(conn: sqlite3.Connection) -> str:
    s = store.get_meta(conn, "lr_stage")
    return s if s in STAGE_KEYS else derived_stage(conn)


def set_stage(conn: sqlite3.Connection, value: str) -> str | None:
    """Номер (1-6) или ключ этапа; «авто» – снова по состоянию базы."""
    value = value.strip().casefold()
    if value in ("авто", "auto", "0"):
        conn.execute("DELETE FROM meta WHERE key='lr_stage'")
        conn.commit()
        return derived_stage(conn)
    key = STAGE_KEYS[int(value) - 1] if value.isdigit() and 1 <= int(value) <= len(STAGES) else value
    if key not in STAGE_KEYS:
        return None
    store.set_meta(conn, "lr_stage", key)
    conn.commit()
    return key


def stage_title(key: str) -> str:
    i = STAGE_KEYS.index(key)
    return f"{i + 1}. {STAGES[i][1]}"


# --- следующий шаг ----------------------------------------------------------------------------

def suggestions(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(формат, что сделать, команда) – короткий, обычный и глубокий варианты по состоянию."""
    m = inventory.metrics(conn)
    out: list[tuple[str, str, str]] = []
    hyp = hypotheses.counts(conn)
    open_hyp = hyp.get("new", 0) + hyp.get("testing", 0)
    if not store.get_meta(conn, "lr_built_at"):
        out.append((trl("обычная, 15-30 мин"), trl("собрать реестр накопленного из памяти (модель работает сама, от тебя – ничего)"),
                    "coach.py inventory build"))
    if m["bulk"]:
        out.append((trl("обычная, 20-30 мин"), trl("одиночные упоминания пачкой: отметить живое, остальное в архив ({0})", m['bulk']),
                    "coach.py sweep"))
    if m["due"] or m["resurfaced"]:
        out.append((trl("быстрая, 5-10 мин"), trl("пересмотреть пункты, у которых подошёл срок или которые снова всплыли ({0})", m['due'] + m['resurfaced']), "coach.py triage --limit 5"))
    if not audit.latest_round(conn):
        out.append((trl("обычная, 15-30 мин"), trl("аудит жизни: 12 областей, по строке на каждую"), "coach.py audit"))
    elif audit.current_round(conn)[1]:
        out.append((trl("быстрая, 5-10 мин"), trl("закончить начатый аудит"), "coach.py audit"))
    left = m["total"] - m["coverage"] - m["bulk"]
    if left > 0:
        out.append((trl("быстрая, 5-10 мин"), trl("разобрать 5 пунктов реестра (ждут решения: {0})", left), "coach.py triage --limit 5"))
        out.append((trl("обычная, 15-30 мин"), trl("разобрать одну группу, например проекты"), "coach.py triage --type project"))
    if open_hyp:
        out.append((trl("глубокая, 30-60 мин"), trl("проверить гипотезы о повторяющихся сценариях ({0})", open_hyp),
                    "coach.py hypotheses"))
    if not out:
        out.append((trl("обычная, 15-30 мин"), trl("поговорить о будущем: что из активного действительно важно и чего не хватает"),
                    "coach.py chat → /depth 3"))
    return out


# --- обзор ------------------------------------------------------------------------------------

def overview_lines(conn: sqlite3.Connection) -> list[str]:
    m = inventory.metrics(conn)
    st = stage(conn)
    manual = store.get_meta(conn, "lr_stage") in STAGE_KEYS
    lines = [trl("Life Reset. Этап: {0}", stage_title(st)) + (trl(" (назначен вручную)") if manual else "")
             + f" – {STAGES[STAGE_KEYS.index(st)][2]}.",
             trl("Глубина разговора: {0} – {1}.", depth(conn), DEPTHS[depth(conn)][0])]
    if m["total"]:
        cov = round(100 * m["coverage"] / m["total"])
        dec = round(100 * m["decided"] / m["total"])
        lines.append(trl("Реестр: {0} пунктов, рассмотрено {1} ({2} %), решение есть у {3} ({4} %).", m['total'], m['coverage'], cov, m['decided'], dec))
        by = m["by_status"]
        lines.append("  " + ", ".join(f"{LR_STATUS_TITLES[s]} – {by[s]}" for s in LR_STATUSES if by.get(s)))
        cap = trl(" при ёмкости {0}", m['capacity']) if m["capacity"] else trl(" (ёмкость не задана)")
        lines.append(trl("  активных направлений: {0}{1}; незакрытых обязательств: {2}; нерешённых вопросов: {3}", m['active'], cap, m['commitments'], m['unresolved_questions']))
        extra = []
        if m["due"]:
            extra.append(trl("пора пересмотреть – {0}", m['due']))
        if m["resurfaced"]:
            extra.append(trl("снова всплыло после решения – {0}", m['resurfaced']))
        if m["stale"]:
            extra.append(trl("активны дольше полугода без пересмотра – {0}", m['stale']))
        if m["bulk"]:
            extra.append(trl("одиночных ждут разбора пачкой – {0}", m['bulk']))
        if m["reopened"]:
            extra.append(trl("закрытое открыто снова – {0}", m['reopened']))
        if extra:
            lines.append("  " + "; ".join(extra))
        focus = _focus_line(conn)
        if focus:
            lines.append(focus)
    elif store.get_meta(conn, "lr_built_at"):
        lines.append(trl("Реестр собран, но пуст: в памяти не нашлось целей, незакрытых дел и проектов."))
    else:
        lines.append(trl("Реестр ещё не собран."))
    r = audit.latest_round(conn)
    if r:
        rows = audit.answers(conn, r)
        when = min(x["created_at"] for x in rows.values())[:10] if rows else ""
        focus = audit.focus_areas(rows)
        answered = sum(1 for x in rows.values() if x["satisfaction"] is not None)
        lines.append(trl("Аудит: раунд {0} ({1}), отвечено областей {2} из {3}", r, when, answered, len(LR_AREAS))
                     + (trl(". Где хочется перемен: ") + ", ".join(audit.AREA_TITLES[k] for k in focus) if focus else "") + ".")
    else:
        lines.append(trl("Аудита ещё не было."))
    hc = hypotheses.counts(conn)
    if hc:
        lines.append(trl("Гипотезы: ") + ", ".join(f"{hypotheses.STATUS_TITLES[k]} – {v}" for k, v in hc.items()) + ".")
    return lines


def _focus_line(conn: sqlite3.Connection) -> str:
    """Время в режиме присутствия по активным направлениям за 30 дней – показатель «на важное» из концепции.
    Пока ни одна сессия фокуса не привязана к пункту, строки нет: нули ничего не говорят."""
    mins = item_minutes(conn)
    if not mins:
        return ""
    rows = conn.execute("SELECT id, title, sensitive FROM lr_items WHERE status='active' AND merged_into IS NULL "
                        "ORDER BY decided_at DESC LIMIT 10").fetchall()
    rows = sorted(rows, key=lambda r: -mins.get(r["id"], 0))
    parts = [f"{f'LR-{r[0]}' if r['sensitive'] else r['title']} – {_minutes(mins.get(r['id'], 0))}" for r in rows]
    other = sum(v for k, v in mins.items() if k not in {r["id"] for r in rows})
    if other:
        parts.append(trl("другие пункты – {0}", _minutes(other)))
    return trl("  в фокусе за 30 дней: ") + "; ".join(parts) if parts else ""


def dashboard(conn: sqlite3.Connection) -> None:
    print("\n".join(overview_lines(conn)))
    print(trl("\nЧто можно сделать сейчас:"))
    for fmt, what, cmd in suggestions(conn)[:4]:
        print(f"  [{fmt}] {what}: {cmd}")
    print(trl("\n"
              "Не обязательно идти по порядку: этап можно сменить – coach.py reset stage 4 (или «авто»).\n"))


# --- блок для коуча -------------------------------------------------------------------------

def relevant_items(conn: sqlite3.Connection, qvec: list[float] | None, k: int = 4) -> list[sqlite3.Row]:
    if qvec is None:
        return []
    scored = []
    for r in conn.execute("SELECT * FROM lr_items WHERE merged_into IS NULL AND status!='dismissed' AND embedding IS NOT NULL"):
        v = vectors.unpack(r["embedding"])
        s = vectors.dot(qvec, v) if v is not None else 0.0
        if s >= RELEVANT:
            scored.append((s, r))
    return [r for _s, r in sorted(scored, key=lambda x: -x[0])[:k]]


def _last_reason_text(conn: sqlite3.Connection, iid: int) -> str:
    r = _last_reason(conn, iid)
    return (r["reason"] if r else "") or ""


def _last_reason(conn: sqlite3.Connection, iid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT to_status, reason, created_at FROM lr_decisions WHERE item_id=? AND to_status!='merged' "
                        "ORDER BY id DESC LIMIT 1", (iid,)).fetchone()


RULES = (trl("Правила программы: новую идею или желание из разговора не делай сразу проектом – предложи записать командой /idea, решение будет при разборе. Если активного уже столько, сколько он тянет, а он хочет ещё, – скажи прямо, с числами. Если всплыло то, что он отпустил или закрыл, – напомни, когда и почему он так решил, и спроси, что изменилось, без упрёка. Отдых, отношения и удовольствие – не проекты со сроками. Решает он."))
RECENT_DAYS = 7


def _started(conn: sqlite3.Connection) -> bool:
    return bool(store.get_meta(conn, "lr_built_at") or audit.latest_round(conn))


def coach_static(conn: sqlite3.Connection, compact: bool = False) -> str:
    """Постоянная часть для системного промпта: меняется только после разбора или аудита, поэтому
    идёт раньше меняющихся блоков – Ollama переиспользует уже посчитанное начало промпта.
    compact – без ПК: на процессоре каждая сотня токенов промпта – это секунды ожидания."""
    d = depth(conn)
    depth_text = DEPTHS[d][1].split(":")[0] + "." if compact else DEPTHS[d][1]
    if not _started(conn):
        # Глубину можно задать и без программы: тогда в промпт идёт только она.
        return depth_text if store.get_meta(conn, "lr_depth") else ""
    out = [trl("LIFE RESET (программа разбора накопленного; этап – {0}). {1}", stage_title(stage(conn)), depth_text)]
    m = inventory.metrics(conn)
    active = conn.execute("SELECT id, title, type, next_step FROM lr_items WHERE status='active' AND merged_into IS NULL "
                          "ORDER BY decided_at DESC LIMIT ?", (4 if compact else 8,)).fetchall()
    if active:
        cap = trl(", его ёмкость – {0}", m['capacity']) if m["capacity"] else ""
        out.append(trl("Активное по его решению ({0}{1}):\n", m['active'], cap) + "\n".join(
            f"- {r['title']}" + (trl("; следующий шаг: {0}", r['next_step']) if r["next_step"] and not compact else "")
            for r in active))
    focus = audit.focus_areas(audit.answers(conn, audit.latest_round(conn) or 0))
    if focus:
        out.append(trl("По его аудиту жизни перемен хочется в областях: ") + ", ".join(audit.AREA_TITLES[k] for k in focus))
    if d >= 2 and not compact:
        hyp = hypotheses.coach_lines(conn, 3)
        if hyp:
            out.append(trl("Гипотезы о повторяющихся сценариях (не факты; подтверждённая – описание повторяемости, не черта личности):\n") + "\n".join(hyp))
    out.append(RULES)
    return "\n\n".join(out)


def coach_dynamic(conn: sqlite3.Connection, qvec: list[float] | None, compact: bool = False) -> str:
    """К теме этой реплики – в блок памяти ответа: пункты реестра с решением и причиной и решения
    последней недели (если они не про то же)."""
    if not store.get_meta(conn, "lr_built_at"):
        return ""
    out = []
    rel = [r for r in relevant_items(conn, qvec, 3 if compact else 4) if r["status"] != "active"]
    if rel:
        lines = []
        for r in rel:
            last = _last_reason(conn, r["id"])
            line = f"- {r['title']}: {LR_STATUS_TITLES[r['status']]}"
            if last and last["to_status"] == r["status"]:
                line += trl(" (решил {0}", last['created_at'][:10]) + (f": «{last['reason']}»" if last["reason"] else "") + ")"
            if r["status"] in LR_PARKED and r["review_on"]:
                line += trl(", пересмотр {0}", r['review_on'])
            lines.append(line)
        out.append(trl("Из его реестра Life Reset к теме разговора:\n") + "\n".join(lines))
    if not compact:
        since = (date.fromisoformat(today()) - timedelta(days=RECENT_DAYS)).isoformat()
        shown = {r["id"] for r in rel}
        recent = [r for r in inventory.recent_decisions(conn, 6) if r["created_at"][:10] >= since and r["item_id"] not in shown][:3]
        if recent:
            out.append(trl("Его решения по реестру за неделю:\n") + "\n".join(
                f"- {r['created_at'][:10]} {r['title']}: {LR_STATUS_TITLES.get(r['to_status'], r['to_status'])}"
                + (f" – «{r['reason']}»" if r["reason"] else "") for r in recent))
    return "\n\n".join(out)


def coach_block(conn: sqlite3.Connection, qvec: list[float] | None = None) -> str:
    """Обе части вместе (для проверок и отладки)."""
    return "\n\n".join(x for x in (coach_static(conn), coach_dynamic(conn, qvec)) if x)


# --- Obsidian ---------------------------------------------------------------------------------

def _link(path: str) -> str:
    return "[[" + re.sub(r"\.md$", "", path) + "]]"


def _fm(title: str) -> str:
    return f"---\ntitle: {title}\ncreated_by: life_reset\nupdated: {today()}\n---\n"


def export_dir(cfg: Config) -> Path:
    if cfg.vault is not None:
        return cfg.vault / LIFE_RESET_DIR
    return cfg.workspace / "life-reset"


def export(conn: sqlite3.Connection, cfg: Config, quiet: bool = False) -> Path | None:
    """Итоги в Markdown: обзор, реестр, решения, аудит, гипотезы. Пишется только в папку Life Reset
    (её коуч не читает как заметки); заметки vault не меняются. Цитат из заметок в файлах нет – только
    названия, суть и ссылки на исходные заметки."""
    target = export_dir(cfg)
    files = {
        trl("Обзор.md"): _overview_md(conn),
        trl("Реестр.md"): _inventory_md(conn),
        trl("Решения.md"): _decisions_md(conn),
        trl("Аудит.md"): _audit_md(conn),
        trl("Гипотезы.md"): _hypotheses_md(conn),
    }
    for attempt in (target, cfg.workspace / "life-reset"):
        try:
            attempt.mkdir(parents=True, exist_ok=True)
            for name, text in files.items():
                path = (attempt / name).resolve()
                if path.parent != attempt.resolve():     # пишем строго в свою папку
                    raise OSError(trl("не та папка: {0}", path))
                atomic_write(path, text)
        except OSError as exc:
            if attempt == target and target != cfg.workspace / "life-reset":
                if not quiet:
                    print(trl("В vault записать не вышло ({0}): пишу в {1}", exc.__class__.__name__, cfg.workspace / 'life-reset'))
                continue
            if not quiet:
                print(trl("Итоги Life Reset не записаны: {0}", exc))
            return None
        if not quiet:
            print(trl("Итоги Life Reset: {0}", attempt))
        return attempt
    return None


def _overview_md(conn: sqlite3.Connection) -> str:
    lines = [_fm(trl("Life Reset – обзор")), trl("# Life Reset – обзор"), ""]
    for line in overview_lines(conn):
        lines.append(("  - " if line.startswith("  ") else "- ") + line.strip())
    lines += ["", trl("## Что можно сделать сейчас"), ""]
    lines += [f"- {what} ({fmt}): `{cmd}`" for fmt, what, cmd in suggestions(conn)[:4]]
    lines += ["", trl("Файлы этой папки пишет коуч, вручную их править бесполезно: при следующем разборе они пересоздаются. Решения меняются через разбор (`coach.py triage`)."), ""]
    return "\n".join(lines)


def _inventory_md(conn: sqlite3.Connection) -> str:
    lines = [_fm(trl("Life Reset – реестр")), trl("# Реестр накопленного"), ""]
    rows = conn.execute("SELECT * FROM lr_items WHERE merged_into IS NULL AND status!='dismissed' "
                        "ORDER BY status, type, title").fetchall()
    if not rows:
        return "\n".join(lines + [trl("Реестр пуст."), ""])
    for st in LR_STATUSES:
        group = [r for r in rows if r["status"] == st]
        swept = [r for r in group if st == "archived" and _last_reason_text(conn, r["id"]) == inventory.SWEEP_REASON]
        group = [r for r in group if r not in swept]
        if not group and not swept:
            continue
        if not group:
            lines += [f"## {LR_STATUS_TITLES[st].capitalize()} ({len(swept)})", "",
                      trl("- разобрано пачкой: {0} одиночных упоминаний (список – `coach.py inventory list --status archived`, вернуть – `inventory reopen LR-N`)", len(swept)), ""]
            continue
        lines += [f"## {LR_STATUS_TITLES[st].capitalize()} ({len(group)})", ""]
        for r in group:
            if r["sensitive"]:
                # Личное (здоровье, деньги, отношения): vault может синхронизироваться – подробности только в коуче.
                lines.append(trl("- **{0}** личное – подробности в коуче (`coach.py inventory list`)", inventory.code(r['id'])))
                continue
            s = f"- **{inventory.code(r['id'])}** {r['title']} – {LR_TYPE_TITLES[r['type']]}, {inventory.span(r)}"
            if r["review_on"]:
                s += trl("; пересмотр {0}", r['review_on'])
            lines.append(s)
            if r["summary"]:
                lines.append(f"  - {r['summary']}")
            if r["next_step"]:
                lines.append(trl("  - следующий шаг: {0}", r['next_step']))
            last = _last_reason(conn, r["id"])
            if last and last["reason"]:
                lines.append(trl("  - решение {0}: «{1}»", last['created_at'][:10], last['reason']))
            paths = loads(r["paths_json"], []) or []
            if paths:
                lines.append(trl("  - источники: ") + ", ".join(_link(p) for p in paths[:5])
                             + (trl(" и ещё {0}", len(paths) - 5) if len(paths) > 5 else ""))
        if swept:
            lines.append(trl("- и ещё разобрано пачкой: {0} одиночных упоминаний (`coach.py inventory list --status archived`)", len(swept)))
        lines.append("")
    return "\n".join(lines)


def _decisions_md(conn: sqlite3.Connection) -> str:
    lines = [_fm(trl("Life Reset – решения")), trl("# Журнал решений"), "",
             trl("Что было, что предлагал коуч, что решил ты и почему. Новые сверху."), ""]
    rows = conn.execute("SELECT d.*, i.title, i.sensitive FROM lr_decisions d JOIN lr_items i ON i.id=d.item_id "
                        "WHERE d.reason!=? ORDER BY d.id DESC LIMIT 500", (inventory.SWEEP_REASON,)).fetchall()
    swept = conn.execute("SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM lr_decisions WHERE reason=? "
                         "GROUP BY day ORDER BY day DESC", (inventory.SWEEP_REASON,)).fetchall()
    if not rows and not swept:
        return "\n".join(lines + [trl("Решений пока нет."), ""])
    for r in swept:
        lines.append(trl("- {0}: разобрано пачкой, в архив – {1} одиночных упоминаний", r['day'], r['n']))
    for r in rows:
        to = trl("склеено") if r["to_status"] == "merged" else LR_STATUS_TITLES.get(r["to_status"], r["to_status"])
        if r["sensitive"]:
            lines.append(trl("- {0} **{1}** личное: {2} → {3}", r['created_at'][:10], inventory.code(r['item_id']), LR_STATUS_TITLES.get(r['from_status'], r['from_status']), to))
            continue
        s = (f"- {r['created_at'][:10]} **{inventory.code(r['item_id'])}** {r['title']}: "
             f"{LR_STATUS_TITLES.get(r['from_status'], r['from_status'])} → {to}")
        if r["proposal"] and r["proposal"] != r["to_status"]:
            s += trl(" (коуч понял как «{0}»)", LR_STATUS_TITLES.get(r['proposal'], r['proposal']))
        if r["reason"]:
            s += trl("; почему: {0}", r['reason'])
        lines.append(s)
    return "\n".join(lines + [""])


def _audit_md(conn: sqlite3.Connection) -> str:
    lines = [_fm(trl("Life Reset – аудит")), trl("# Аудит жизни"), ""]
    rs = audit.rounds(conn)
    if not rs:
        return "\n".join(lines + [trl("Аудита ещё не было."), ""])
    data = {r["round"]: audit.answers(conn, r["round"]) for r in rs}
    head = trl("| Область | ") + " | ".join(f"{r['started'][:10]}" for r in rs) + " |"
    lines += [trl("Устраивает / важно (1-10); ↑ – хочу менять."), "", head, "|---|" + "---|" * len(rs)]
    for key, title, _d, _dom in LR_AREAS:
        cells = []
        for r in rs:
            a = data[r["round"]].get(key)
            if a is None or a["satisfaction"] is None:
                cells.append("")
            else:
                cells.append(f"{a['satisfaction']} / {a['importance'] or '?'}" + (" ↑" if a["change"] == "yes" else ""))
        lines.append(f"| {title} | " + " | ".join(cells) + " |")
    lines += ["", audit.summary_text(conn).strip(), ""]
    notes = [(title, data[rs[-1]["round"]][key]["note"]) for key, title, _d, _dom in LR_AREAS
             if key in data[rs[-1]["round"]] and data[rs[-1]["round"]][key]["note"]]
    if notes:
        lines += [trl("## Комментарии последнего раунда"), ""] + [f"- {t}: {n}" for t, n in notes] + [""]
    return "\n".join(lines)


def _hypotheses_md(conn: sqlite3.Connection) -> str:
    lines = [_fm(trl("Life Reset – гипотезы")), trl("# Гипотезы о повторяющихся сценариях"), "",
             trl("Предположения, не факты и не диагнозы. «Подтверждена» – ты признал, что это повторяется; это описание, а не черта характера."), ""]
    rows = conn.execute("SELECT * FROM lr_hypotheses ORDER BY status, id").fetchall()
    if not rows:
        return "\n".join(lines + [trl("Гипотез пока нет."), ""])
    for r in rows:
        lines.append(f"## H-{r['id']} · {hypotheses.STATUS_TITLES[r['status']]}")
        lines += ["", r["text"], ""]
        alts = loads(r["alternatives_json"], []) or []
        if alts:
            lines.append(trl("Другие объяснения: ") + "; ".join(alts))
        if r["question"]:
            lines.append(trl("Вопрос для проверки: {0}", r['question']))
        obs = loads(r["observations_json"], []) or []
        refs = sorted({o.get("ref", "").split("#")[0] for o in obs if o.get("ref")})
        if refs:
            lines.append(trl("Наблюдения в заметках: ") + ", ".join(_link(p) for p in refs[:6]))
        if r["user_note"]:
            lines.append(trl("Твои слова: {0}", r['user_note']))
        lines.append("")
    return "\n".join(lines)


def status_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    return {"stage": stage(conn), "depth": depth(conn), **inventory.metrics(conn)}
