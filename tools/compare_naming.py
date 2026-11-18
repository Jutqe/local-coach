"""Сравнение двух моделей на названии групп реестра Life Reset: одни и те же группы, пакет как в `inventory`.

    python3 tools/compare_naming.py --a qwen3.5:9b --b qwen3.6:27b [--n 15] [--seed 1] [--out report.md]

Работает как коуч: адрес Ollama из LOCAL_COACH_OLLAMA / Config, база открывается только на чтение,
в кэш реестра ничего не пишется. Отчёт идёт в терминал и в файл --out (по умолчанию compare_naming.md
в текущей папке): внутри тексты твоих заметок, никуда его не отправляй.
"""
import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localcoach import inventory  # noqa: E402
from localcoach.config import Config  # noqa: E402
from localcoach.ollama import Ollama, OllamaError  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--a", default="", help="первая модель (например, основная 9B); можно не задавать")
ap.add_argument("--b", default="", help="вторая модель (например, Qwen 27B); можно не задавать, тогда прогон одной модели")
ap.add_argument("--n", type=int, default=15, help="сколько групп сравнить")
ap.add_argument("--seed", type=int, default=1, help="зерно выборки: то же зерно – те же группы")
ap.add_argument("--db", default="", help="путь к базе (по умолчанию из Config)")
ap.add_argument("--out", default="compare_naming.md")
args = ap.parse_args()
if not (args.a or args.b):
    sys.exit("Нужна хотя бы одна модель: --a и/или --b.")

cfg = Config()
db = Path(args.db) if args.db else cfg.db_path
if not db.exists():
    sys.exit(f"Нет базы: {db}")
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# Только группы, которые реально идут модели: не одиночные.
pool = [g for g in inventory.groups(conn) if not inventory.is_single(g)]
if not pool:
    sys.exit("Нет групп для сравнения (все одиночные).")
random.Random(args.seed).shuffle(pool)
sample = pool[:args.n]
print(f"Групп всего для модели: {len(pool)}, беру {len(sample)} (зерно {args.seed}).")


def run(model: str) -> tuple[dict, float, int]:
    """Прогон пакетами как в build. Возвращает названия, секунды, число групп без ответа модели."""
    cfg.heavy_model = model          # _name_batch зовёт heavy=True
    llm = Ollama(cfg)
    names: dict = {}
    started = time.monotonic()
    for i in range(0, len(sample), inventory.BATCH):
        batch = sample[i:i + inventory.BATCH]
        try:
            names.update(inventory._name_batch(llm, cfg, batch))
        except OllamaError as exc:
            print(f"  {model}: ошибка {exc}")
    spent = time.monotonic() - started
    lost = sum(1 for g in sample if g["key"] not in names or names[g["key"]].get("fallback"))
    return names, spent, lost


results = {}
for label, model in (("A", args.a), ("B", args.b)):
    if not model:
        continue
    print(f"Прогон {label}: {model} ...")
    results[label] = run(model)
    names, spent, lost = results[label]
    print(f"  {spent:.0f} с, {spent / len(sample):.1f} с на группу, без нормального названия: {lost}")

labels = [x for x in ("A", "B") if x in results]
lines = [f"# Сравнение названий: A = {args.a or '–'}, B = {args.b or '–'}", ""]
for label in labels:
    _, spent, lost = results[label]
    lines.append(f"- {label}: {spent:.0f} с всего, {spent / len(sample):.1f} с на группу, "
                 f"запасное название кодом: {lost} из {len(sample)}")
lines.append("")
agree = 0
for n, g in enumerate(sample, 1):
    a = results["A"][0].get(g["key"]) or {} if "A" in results else {}
    b = results["B"][0].get(g["key"]) or {} if "B" in results else {}
    if len(labels) == 2 and a.get("title", "").lower() == b.get("title", "").lower():
        agree += 1
    lines.append(f"## {n}. {g['key']}")
    lines.append(f"Заметки: {', '.join(g['titles']) or '–'} | упоминаний: {g['mentions']}")
    for f in g["claims"][:3]:
        lines.append(f"  - {f['claim'][:200]}")
    for label, r in (("A", a), ("B", b)):
        if label not in results:
            continue
        flag = " (запасное)" if r.get("fallback") else ""
        lines.append(f"- **{label}**: «{r.get('title', '—')}» [{r.get('type', '?')}]{flag}"
                     f"{' не пункт' if r.get('not_item') else ''}")
        lines.append(f"    суть: {r.get('summary', '')[:200]}")
    lines.append("")
if len(labels) == 2:
    lines.insert(3, f"- Совпали названия дословно: {agree} из {len(sample)}")
text = "\n".join(lines)
Path(args.out).write_text(text, encoding="utf-8")
print("\n" + text)
print(f"\nОтчёт сохранён: {Path(args.out).resolve()}")
