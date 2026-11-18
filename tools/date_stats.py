"""Сколько дат знает память: только числа, без текста фактов и заметок.

    python3 tools/date_stats.py [--db ~/.local-coach/coach.sqlite3] [--note "часть пути к дневнику"]
"""
import argparse
import collections
import json
import re
import sqlite3
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=str(Path("~/.local-coach/coach.sqlite3").expanduser()))
ap.add_argument("--note", default="", help="часть пути заметки-дневника по годам")
args = ap.parse_args()
conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)


def one(sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def by(sql, params=()):
    return dict(conn.execute(sql, params).fetchall())


IMP = "_imports/%"
print("Биография (импорт):")
print("  фактов", one("SELECT COUNT(*) FROM facts WHERE path LIKE ? AND user_state!='rejected'", (IMP,)),
      "| с датой", one("SELECT COUNT(*) FROM facts WHERE path LIKE ? AND user_state!='rejected' "
                       "AND event_date IS NOT NULL AND date_reliable=1", (IMP,)))
print("  годы фактов:", dict(sorted(by("SELECT substr(event_date,1,4), COUNT(*) FROM facts WHERE path LIKE ? "
                                      "AND user_state!='rejected' AND event_date IS NOT NULL AND date_reliable=1 "
                                      "GROUP BY 1", (IMP,)).items())))
print("  виды:", by("SELECT kind, COUNT(*) FROM facts WHERE path LIKE ? AND user_state!='rejected' GROUP BY kind", (IMP,)))
print("  актуальность их кластеров:", by(
    "SELECT COALESCE(cl.currency,'?'), COUNT(DISTINCT cl.id) FROM facts f JOIN clusters cl ON cl.id=f.cluster_id "
    "WHERE f.path LIKE ? AND f.user_state!='rejected' GROUP BY 1", (IMP,)))
print("  кластеров из vault, где есть и факт биографии:", one(
    "SELECT COUNT(DISTINCT cluster_id) FROM facts WHERE cluster_id IN (SELECT cluster_id FROM facts WHERE path LIKE ?) "
    "AND path NOT LIKE ?", (IMP, IMP)))

print("Заметки vault:")
print("  фактов с датой из текста", one("SELECT COUNT(*) FROM facts WHERE path NOT LIKE ? AND origin!='chat' "
                                       "AND date_reliable=1 AND event_date IS NOT NULL", (IMP,)),
      "из", one("SELECT COUNT(*) FROM facts WHERE path NOT LIKE ? AND origin!='chat'", (IMP,)))
print("  фрагментов с датой раздела, отличной от даты заметки:", one(
    "SELECT COUNT(*) FROM chunks c JOIN notes n ON n.path=c.path WHERE c.chunk_date IS NOT NULL "
    "AND c.chunk_date != COALESCE(n.note_date,'')"))

print("Кластеры по актуальности:", by("SELECT COALESCE(currency,'?'), COUNT(*) FROM clusters GROUP BY 1"))
print("  источник отметки:", by("SELECT COALESCE(NULLIF(currency_src,''),'-'), COUNT(*) FROM clusters GROUP BY 1"))
verdicts = collections.Counter(json.loads(p).get("verdict") for (p,) in
                               conn.execute("SELECT payload_json FROM artifacts WHERE stage='currency'"))
print("Сверено с хронологией (вердикты модели):", dict(verdicts))

if args.note:
    # LIKE в SQLite не различает регистр только у латиницы — «дневник» и «Дневник» сравниваем сами.
    paths = [r[0] for r in conn.execute("SELECT path FROM notes") if args.note.casefold() in r[0].casefold()]
    print(f"Дневник: найдено заметок {len(paths)}")
    for p in paths:
        chunks = one("SELECT COUNT(*) FROM chunks WHERE path=?", (p,))
        dated = one("SELECT COUNT(*) FROM chunks c JOIN notes n ON n.path=c.path WHERE c.path=? "
                    "AND c.chunk_date IS NOT NULL AND c.chunk_date != COALESCE(n.note_date,'')", (p,))
        facts = one("SELECT COUNT(*) FROM facts WHERE path=? AND user_state!='rejected'", (p,))
        fdated = one("SELECT COUNT(*) FROM facts WHERE path=? AND user_state!='rejected' "
                     "AND date_reliable=1 AND event_date IS NOT NULL", (p,))
        years = by("SELECT substr(event_date,1,4), COUNT(*) FROM facts WHERE path=? AND date_reliable=1 "
                   "AND event_date IS NOT NULL GROUP BY 1", (p,))
        cur = by("SELECT COALESCE(cl.currency,'?'), COUNT(DISTINCT cl.id) FROM facts f JOIN clusters cl "
                 "ON cl.id=f.cluster_id WHERE f.path=? GROUP BY 1", (p,))
        print(f"  фрагментов {chunks} (с датой раздела {dated}), фактов {facts} (с датой {fdated})")
        print(f"  годы фактов: {dict(sorted(years.items()))}")
        # По фрагментам: сколько строк-пунктов против сколько фактов. Пунктов сильно больше, чем
        # фактов, а фактов ровно 15 — похоже на потолок; деление берёт фрагменты от 600 символов.
        print("  фрагменты (дата раздела | символов | пунктов списка | непустых строк | фактов):")
        for cid, cdate, body in conn.execute("SELECT id, chunk_date, body FROM chunks WHERE path=? ORDER BY idx", (p,)):
            lines = [l for l in body.splitlines() if l.strip()]
            items = sum(1 for l in lines if re.match(r"\s*([-*+]|\d+[.)])\s", l))
            nf = one("SELECT COUNT(*) FROM facts WHERE chunk_id=? AND user_state!='rejected'", (cid,))
            print(f"    {cdate or '-':10} | {len(body):6} | {items:4} | {len(lines):4} | {nf:4}")
        print(f"  актуальность кластеров: {cur}")
        # Как в дневнике записаны годы: только форма строки, буквы скрыты («## YYYY ааа»).
        shapes = collections.Counter()
        for (body,) in conn.execute("SELECT body FROM chunks WHERE path=?", (p,)):
            for line in body.splitlines():
                if re.search(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", line):
                    s = re.sub(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", "@@@@", line.strip()[:40])
                    s = re.sub(r"\d", "9", re.sub(r"[^\W\d_]+", "ааа", s)).replace("@@@@", "YYYY")
                    shapes[s] += 1
        print("  строки с годом (форма, без текста):")
        for s, n in shapes.most_common(12):
            print(f"    {n:4d}  {s}")
