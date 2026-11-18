"""Тест извлечения фактов на длинном синтетическом дневнике: держит ли модель длинный текст и рассуждает ли в несколько шагов.

Текст выдуманный, из заметок ничего не берётся, база не открывается. Поэтому отчёт можно показывать кому угодно.
В дневник вложены «иголки» с известным ответом, проверки автоматические:
  - recall: факт из начала, середины и конца длинного текста найден;
  - subject: факт про брата отнесён к другому человеку, а не к автору;
  - hold: отказ от курсов остаётся «abandoned», хотя дальше в тексте автор к теме возвращается;
  - hop: дата отказа считается из фразы «ровно N дней назад» (цифры и дата разнесены по тексту);
  - dates: «вчера», «два года назад», «через две недели после 3 апреля» пересчитываются в дату;
  - overstate: «подумываю завести собаку» не превращается в план;
  - general: конспект про TCP не попадает в память как факт об авторе.
Плюс метрика: какая доля цитат-доказательств дословно есть в тексте.

Запуск (одна модель за раз, как на ПК):
    python3 tools/compare_extract.py --model qwen3.8:27b [--sizes 4000,7000,14000] [--seed 1] [--think] [--out r27.json]
Сравнение двух прогонов:
    python3 tools/compare_extract.py --compare r9.json r27.json

Идёт тем же путём, что в проде (`extract._call_extract`: тот же промпт, схема, деление плотного текста), тяжёлая
модель = --model. --think включает рассуждение модели (в проде по умолчанию выключено: LOCAL_COACH_THINK_EXTRACT).
"""
import argparse
import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localcoach.config import Config  # noqa: E402
from localcoach.extract import _call_extract  # noqa: E402
from localcoach.ollama import Ollama, OllamaError, TruncatedError  # noqa: E402
from localcoach.util import normalize_time_ref, quote_match  # noqa: E402

START = date(2024, 1, 5)

# Нейтральные фразы-наполнитель: без дат, имён и решений, которые можно спутать с «иголками».
FILLER = [
    "Утром было {w}, на завтрак {f}.", "До обеда разбирал почту и сортировал бумаги на столе.",
    "Дорога на работу заняла {m} минут, в трамвае было людно.", "Вечером смотрел сериал и долго не мог заснуть.",
    "В магазине купил {f} и батарейки для пульта.", "Днём гулял во дворе, на скамейке сидела соседка с внучкой.",
    "Погода: {w}. Окно на кухне пришлось закрыть.", "Приготовил ужин, получилось нормально, но пересолил.",
    "Читал статью про бытовые счётчики, ничего нового.", "Разговор с коллегой про график отпусков ничем не закончился.",
    "Убирался в комнате, нашёл старую зарядку и два кабеля.", "Поздно вечером позвонила мама, поговорили о ерунде.",
    "На обед была {f}, к чаю печенье.", "Полчаса искал ключи, они лежали в кармане куртки.",
    "Лифт в доме снова не работал, поднимался пешком.", "Составил список покупок на выходные.",
    "После работы зашёл в аптеку за витаминами и ничего не купил.", "Включил радио, играла какая-то старая музыка.",
]
WEATHER = ["пасмурно", "солнечно", "морозно", "дождливо", "ветрено", "туманно"]
FOOD = ["овсянка", "бутерброды", "яичница", "суп", "макароны", "гречка"]


def filler(rng: random.Random) -> str:
    return " ".join(rng.choice(FILLER).format(w=rng.choice(WEATHER), f=rng.choice(FOOD), m=rng.randint(15, 50))
                    for _ in range(rng.randint(2, 4)))


def blob(f: dict) -> str:
    return f"{f.get('claim') or ''} {f.get('evidence_quote') or ''}".casefold()


def find(facts: list[dict], *keys: str) -> list[dict]:
    keys = tuple(k.casefold() for k in keys)
    return [f for f in facts if any(k in blob(f) for k in keys)]


def tref(f: dict) -> str:
    return normalize_time_ref(str(f.get("time_ref") or ""))


def build(size: int, seed: int) -> tuple[str, list[tuple[str, object]]]:
    """Дневник длиной около `size` символов и список проверок (имя, функция(facts) -> (ok, подробности))."""
    rng = random.Random(seed)
    n = max(14, size // 205)
    dates, d = [], START
    for _ in range(n + 12):
        dates.append(d)
        d += timedelta(days=rng.randint(1, 3))

    # (доля текста, фраза-«иголка» как функция даты записи). Порядок и доли фиксированы, дальше записи не пересекаются.
    quit_idx = [0]

    def quit_text(_d):
        return "Всё, бросаю курсы немецкого: нет времени и сил."

    def exam_text(dd):
        days = (dd - dates[quit_idx[0]]).days
        return (f"Сегодня ровно {days} дней с тех пор, как я бросил курсы немецкого. Скучаю по ним, "
                "и сегодня записался на экзамен уровня B1 на июнь.")

    needles = [
        (0.04, "recall_penicillin", lambda _d: "У меня аллергия на пенициллин, это выяснилось в больнице."),
        (0.12, "quit", quit_text),
        (0.30, "brother", lambda _d: "Брат Person1 переехал в Томск и устроился в университет."),
        (0.36, "yesterday", lambda _d: "Вчера защитил проект перед комиссией, всё прошло хорошо."),
        (0.44, "two_years", lambda _d: "Два года назад я впервые пробежал полумарафон."),
        (0.50, "recall_kazan", lambda _d: "К осени перееду в Казань: договор аренды уже подписан."),
        (0.56, "tcp", lambda _d: ("Конспект: трёхстороннее рукопожатие TCP — клиент шлёт SYN, сервер отвечает "
                                  "SYN-ACK, клиент подтверждает ACK.")),
        (0.64, "dog", lambda _d: "Иногда подумываю завести собаку, но пока это просто мысль."),
        (0.72, "dentist", lambda _d: ("Запись к стоматологу назначена ровно через две недели после дня рождения Person2: "
                                      "он у неё третьего апреля.")),
        (0.82, "exam", exam_text),
        (0.96, "recall_cello", lambda _d: "Купил виолончель и записался на первый урок."),
    ]
    slot, used = {}, -1
    for frac, key, fn in needles:
        used = max(used + 1, int(frac * n))
        slot[used] = (key, fn)
    total = max(n, used + 1)

    key_date = {}
    parts = []
    for i in range(total):
        sents = [filler(rng)]
        if i in slot:
            key, fn = slot[i]
            key_date[key] = dates[i]
            if key == "quit":
                quit_idx[0] = i
            sents.insert(rng.randint(0, 1), fn(dates[i]))
        parts.append(f"## {dates[i].isoformat()}\n" + " ".join(sents))
    body = "\n\n".join(parts)

    d_y, d_two, d_quit = key_date["yesterday"], key_date["two_years"], key_date["quit"]

    def got(fs, field):
        return [f"{f.get(field)}" for f in fs][:4]

    def recall(*keys):
        def check(facts):
            fs = find(facts, *keys)
            return bool(fs), "найден" if fs else "не найден"
        return check

    def subject(facts):
        fs = find(facts, "Person1", "Томск")
        return any(f.get("subject") == "other_person" for f in fs), f"subject: {got(fs, 'subject') or 'факта нет'}"

    def hold(facts):
        fs = find(facts, "немецк")
        return any(f.get("intent_status") == "abandoned" for f in fs), f"статусы: {got(fs, 'intent_status') or 'факта нет'}"

    def exam(facts):
        fs = find(facts, "экзамен", "B1")
        return any(f.get("intent_status") in ("planned", "in_progress") for f in fs), \
            f"статусы: {got(fs, 'intent_status') or 'факта нет'}"

    def hop(facts):
        fs = [f for f in find(facts, "немецк", "курс") if f.get("intent_status") == "abandoned"]
        return any(tref(f) == d_quit.isoformat() for f in fs), \
            f"ждали {d_quit}, получили {[tref(f) for f in fs] or 'факта нет'}"

    def yesterday(facts):
        fs = find(facts, "защит")
        want = (d_y - timedelta(days=1)).isoformat()
        return any(tref(f) == want for f in fs), f"ждали {want}, получили {[tref(f) for f in fs] or 'факта нет'}"

    def two_years(facts):
        fs = find(facts, "полумарафон")
        want = str(d_two.year - 2)
        return any(tref(f).startswith(want) for f in fs), \
            f"ждали {want}, получили {[tref(f) for f in fs] or 'факта нет'}"

    def dentist(facts):
        fs = find(facts, "стоматолог")
        return any(tref(f) == "2024-04-17" for f in fs), \
            f"ждали 2024-04-17, получили {[tref(f) for f in fs] or 'факта нет'}"

    def dog(facts):
        fs = find(facts, "собак")
        bad = [f.get("intent_status") for f in fs if f.get("intent_status") in ("planned", "in_progress", "completed")]
        return not bad, f"статусы: {got(fs, 'intent_status') or 'факта нет (не страшно)'}"

    def tcp(facts):
        # Допустим один общий факт «Автор изучал TCP»; провал – если в память попали детали конспекта (SYN, ACK).
        fs = find(facts, "SYN", "ACK")
        return not fs, "детали не извлечены" if not fs else f"детали извлечены: {len(fs)}"

    checks = [
        ("recall: аллергия (начало)", recall("пенициллин")), ("recall: Казань (середина)", recall("Казан")),
        ("recall: виолончель (конец)", recall("виолончел")),
        ("subject: брат = другой человек", subject), ("hold: отказ от курсов не забыт", hold),
        ("hop: дата отказа из «N дней назад»", hop), ("exam: экзамен = план", exam),
        ("date: «вчера»", yesterday), ("date: «два года назад»", two_years),
        ("date: «через 2 недели после 3 апреля»", dentist),
        ("overstate: собака не стала планом", dog), ("general: TCP не в память", tcp),
    ]
    return body, checks


def run(args) -> dict:
    cfg = Config()
    cfg.heavy_model = args.model
    cfg.think_extract = bool(args.think)
    llm = Ollama(cfg)
    cut = [0]
    chat_json = llm.chat_json

    def counting(*a, **k):
        """Считает вызовы, где ответ упёрся в лимит токенов (рассуждение съело num_predict); прод их делит или упрощает."""
        try:
            return chat_json(*a, **k)
        except TruncatedError:
            cut[0] += 1
            raise

    llm.chat_json = counting
    result = {"model": args.model, "think": bool(args.think), "seed": args.seed, "sizes": {}}
    for size in (int(s) for s in args.sizes.split(",")):
        body, checks = build(size, args.seed)
        meta = {"title": "Дневник (синтетический)", "path": "synthetic/diary.md", "heading": None, "tags": [],
                "date_line": "Дата заметки неизвестна."}
        sent: list[int] = []
        cut[0] = 0
        print(f"Размер ~{size}, фактический {len(body)} символов, модель {args.model}, think={bool(args.think)} ...")
        started = time.monotonic()
        try:
            raw = _call_extract(llm, cfg, meta, body, sent)
        except OllamaError as exc:
            print(f"  ошибка: {exc}")
            result["sizes"][str(size)] = {"chars": len(body), "error": str(exc)[:200]}
            continue
        spent = time.monotonic() - started
        facts = [f for f in raw.get("facts") or [] if isinstance(f, dict)]
        quoted = [f for f in facts if str(f.get("evidence_quote") or "").strip()]
        verbatim = sum(1 for f in quoted if quote_match(str(f["evidence_quote"]), body) != "none")
        res = {}
        for name, fn in checks:
            ok, detail = fn(facts)
            res[name] = {"ok": bool(ok), "detail": detail}
        passed = sum(1 for r in res.values() if r["ok"])
        result["sizes"][str(size)] = {
            "chars": len(body), "seconds": round(spent, 1), "calls": len(sent), "truncated": cut[0], "facts": len(facts),
            "quotes_verbatim": f"{verbatim}/{len(quoted)}", "passed": passed, "of": len(res), "checks": res}
        print(f"  {spent:.0f} с, вызовов модели: {len(sent)}, из них обрезано: {cut[0]}, фактов: {len(facts)}, "
              f"цитаты дословно: {verbatim}/{len(quoted)}, проверок пройдено: {passed}/{len(res)}")
        for name, r in res.items():
            print(f"    {'OK ' if r['ok'] else 'НЕТ'} {name} – {r['detail']}")
    return result


def compare(paths: list[str]) -> None:
    runs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    names = [f"{r['model']}{' +think' if r.get('think') else ''}" for r in runs]
    sizes = sorted({s for r in runs for s in r["sizes"]}, key=int)
    for size in sizes:
        print(f"\n== ~{size} символов ==")
        print("  " + " | ".join(f"{n:<28}" for n in names))
        rows = [r["sizes"].get(size, {}) for r in runs]
        for label, key in (("секунд", "seconds"), ("вызовов", "calls"), ("обрезано", "truncated"), ("фактов", "facts"),
                           ("цитаты дословно", "quotes_verbatim")):
            print(f"  {label:<16}" + " | ".join(f"{str(x.get(key, '–')):<28}" for x in rows))
        print(f"  {'пройдено':<16}" + " | ".join(f"{str(x.get('passed', '–')) + '/' + str(x.get('of', '–')):<28}" for x in rows))
        checks = [c for x in rows for c in x.get("checks", {})]
        for name in dict.fromkeys(checks):
            cells = [("OK" if x.get("checks", {}).get(name, {}).get("ok") else "НЕТ") if name in x.get("checks", {}) else "–"
                     for x in rows]
            print(f"  {name:<40}" + " | ".join(f"{c:<6}" for c in cells))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="", help="тег модели в Ollama (одна модель за прогон)")
    ap.add_argument("--sizes", default="4000,7000,14000", help="длины дневника в символах; в проде фрагмент ~7000")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--think", action="store_true", help="включить рассуждение модели")
    ap.add_argument("--out", default="", help="сохранить результат в JSON (по умолчанию compare_extract_<модель>.json)")
    ap.add_argument("--compare", nargs="+", metavar="JSON", help="показать рядом готовые прогоны")
    args = ap.parse_args()
    if args.compare:
        compare(args.compare)
        return
    if not args.model:
        sys.exit("Нужна --model или --compare.")
    result = run(args)
    out = Path(args.out or f"compare_extract_{args.model.replace(':', '_')}{'_think' if args.think else ''}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nРезультат сохранён: {out.resolve()}")


if __name__ == "__main__":
    main()
