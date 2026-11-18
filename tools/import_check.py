"""Как таблица разложится при импорте — до того, как её увидит модель.

    python3 tools/import_check.py путь/к/биографии.xlsx          # только числа
    python3 tools/import_check.py путь/к/биографии.xlsx --show   # и сам текст заметки (на экран, никуда не уходит)

Числа можно сравнить с `tools/date_stats.py`: «ячеек по годам» здесь против «фактов биографии по годам» там.
"""
import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from localcoach import importer  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("--show", action="store_true", help="напечатать текст заметки, как его получит модель")
args = ap.parse_args()
path = Path(args.file)
sheets = importer.read_xlsx(path) if path.suffix.lower() == ".xlsx" else importer.read_csv(path)

YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
for name, rows, merges in sheets:
    h = importer._header_row(rows)
    header = rows[h] if h is not None else []
    year_cols = [c for c, v in enumerate(header) if importer._YEARISH.fullmatch(v)]
    first_col_years = sum(1 for r in rows[(h or 0) + 1:] if r and importer._YEARISH.fullmatch(r[0]))
    print(f"Лист «{name}»: строк {len(rows)}, столбцов {max((len(r) for r in rows), default=0)}")
    print(f"  строка заголовков: {h + 1 if h is not None else 'не найдена — подписи столбцов потеряются'}"
          f"; годов в заголовках: {len(year_cols)}; строк, начинающихся с года: {first_col_years}")
    print(f"  объединённых ячеек: {len(merges)} (по годам-столбцам: "
          f"{sum(1 for (i, c), (j, d) in merges.items() if d > c)}, по строкам: "
          f"{sum(1 for (i, c), (j, d) in merges.items() if j > i)})")
    per_year: collections.Counter = collections.Counter()
    for i, r in enumerate(rows[(h or 0) + 1:], (h or 0) + 1):
        for c, v in enumerate(r):
            if not v or c == 0 and first_col_years:
                continue
            last_col = merges.get((i, c), (i, c))[1]
            for cc in range(c, last_col + 1):
                if cc in year_cols:
                    for y in YEAR.findall(header[cc]):
                        per_year[y] += 1
            if first_col_years and r[0]:
                per_year[YEAR.findall(r[0])[0] if YEAR.findall(r[0]) else "?"] += 1
    if per_year:
        print("  непустых ячеек по годам (объединённая — в каждом своём году):")
        print("   ", dict(sorted(per_year.items())))

body = importer.table_markdown(sheets)
lines = [l for l in body.splitlines() if l.startswith("- ")]
print(f"Строк заметки: {len(lines)}, из них с годом: {sum(1 for l in lines if YEAR.search(l))}, "
      f"с диапазоном лет: {sum(1 for l in lines if re.search(r'(19|20)[0-9]{2}–(19|20)[0-9]{2}', l))}")
if args.show:
    print("\n" + body)
