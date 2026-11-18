"""Командная строка: update, import, plan, status, chat, calibrate, review, forget, reextract, recluster, sanitize, export."""
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import VERSION, extract, guard, people, report, store, synth, timeline, vault, vectors
from .config import Config
from .ollama import Ollama, OllamaError, answers, check_local_url, is_cloud, port_candidates
from .util import fmt_duration, iso_now


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config()
    if args.workspace:
        cfg.workspace = Path(args.workspace).expanduser().resolve()
    if args.ollama:
        cfg.ollama = args.ollama
    if args.ollama_fallback is not None:
        cfg.ollama_fallback = args.ollama_fallback
    if args.model:
        cfg.model = args.model
    if args.embedding_model:
        cfg.embed_model = args.embedding_model
    if getattr(args, "heavy_model", None):
        cfg.heavy_model = args.heavy_model
    if args.synth_model:
        cfg.synth_model = args.synth_model
    cfg.vault = vault.find_vault(args.vault) if args.command in NEEDS_VAULT else None
    return cfg


@contextmanager
def update_lock(cfg: Config):
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    fh = open(cfg.workspace / ".update.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit("Другое обновление памяти уже идёт (или завис прошлый запуск). Дождись его окончания.")
    try:
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _reach(cfg: Config) -> tuple[Ollama, dict]:
    why = check_local_url(cfg.ollama)
    if why:
        raise SystemExit(f"Адрес Ollama отклонён: {why}. Заметки отправляются только на локальный сервер.")
    llm = Ollama(cfg)
    return llm, llm.models()


def _found_file(cfg: Config) -> Path:
    return cfg.workspace / "ollama_found.txt"


def _other_port(cfg: Config) -> str | None:
    """Ollama того же ПК на другом порту: сначала запомненный, затем LOCAL_COACH_OLLAMA_PORTS по порядку."""
    try:
        remembered = _found_file(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        remembered = ""
    options = port_candidates(cfg.ollama, cfg.ollama_ports)
    if remembered in options:
        options.remove(remembered)
        options.insert(0, remembered)
    return next((u for u in options if answers(u)), None)


def _remember_port(cfg: Config, url: str | None) -> None:
    try:
        if url:
            cfg.workspace.mkdir(parents=True, exist_ok=True)
            _found_file(cfg).write_text(url + "\n", encoding="utf-8")
        else:
            _found_file(cfg).unlink(missing_ok=True)
    except OSError:
        pass


def connect_llm(cfg: Config, need_embed: bool = True, fallback: bool = False) -> Ollama:
    """Ollama ПК: на своём порту, иначе на другом порту того же адреса (найденный запоминается);
    fallback — если ПК недоступен совсем, перейти на запасной Ollama (сервер, процессор)."""
    spare = cfg.ollama_fallback.rstrip("/")
    configured = cfg.ollama
    try:
        remembered = _found_file(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        remembered = ""
    # Запомненный порт — первым: иначе каждый запуск ждал бы таймаут на старом.
    if remembered and remembered in port_candidates(configured, cfg.ollama_ports) and answers(remembered):
        cfg.ollama = remembered
    exc: Exception | None = None
    try:
        llm, names = _reach(cfg)
        if cfg.ollama == configured:
            _remember_port(cfg, None)
    except (OllamaError, OSError) as err:
        exc = err
        cfg.ollama = configured
        other = _other_port(cfg)
        if other:
            cfg.ollama = other
            try:
                llm, names = _reach(cfg)
                _remember_port(cfg, other)
                print(f"Ollama на {configured} не ответил — нашёл его на {other} и запомнил.")
                exc = None
            except (OllamaError, OSError):
                cfg.ollama = configured
    if exc is not None:
        primary = cfg.ollama
        if not spare or spare == primary.rstrip("/"):
            raise SystemExit(f"Ollama недоступен по адресу {primary}: {exc}. Включи ПК и проверь, что Ollama запущен.")
        if not fallback:
            raise SystemExit(f"Ollama недоступен по адресу {primary}: {exc}. Включи ПК и проверь, что Ollama запущен. "
                             "Без ПК работают chat, checkin, review и forget — на модели сервера.")
        cfg.ollama = spare
        try:
            llm, names = _reach(cfg)
        except (OllamaError, OSError) as exc2:
            raise SystemExit(f"Ollama недоступен ни на ПК ({primary}: {exc}), ни на сервере ({spare}: {exc2}).")
        cfg.on_fallback = True
        cfg.think_chat = False
        cfg.chat_budget_chars = min(cfg.chat_budget_chars, cfg.fallback_chat_budget)
        print(f"ПК недоступен — работаю на модели сервера ({spare}, процессор): первый ответ через 2–3 минуты, "
              "дальше быстрее, текст идёт по мере генерации.")

    def pick(wanted: str, role: str) -> str | None:
        name = llm.resolve(wanted, names)
        if name and llm.is_remote(name, names[name]):
            raise SystemExit(f"{role} {name} — облачная: запросы уйдут на ollama.com. "
                             "Укажи локальную модель из ollama list.")
        return name

    local = ", ".join(n for n in names if not is_cloud(n, names[n]))
    model = pick(cfg.model, "Модель")
    if not model:
        raise SystemExit(f"Модели {cfg.model} нет в Ollama {cfg.ollama} (нужно точное имя). Есть: {local}. "
                         "Скрипт модели не скачивает.")
    cfg.model = model
    if cfg.heavy_model:
        heavy = pick(cfg.heavy_model, "Тяжёлая модель")
        if not heavy and not cfg.on_fallback:      # на запасном Ollama тяжёлой нет и не будет: тихо работаем на model
            print(f"Тяжёлая модель {cfg.heavy_model} не найдена – всё на {cfg.model}.")
        cfg.heavy_model = heavy or ""
    if cfg.synth_model:
        synth = pick(cfg.synth_model, "Модель синтеза")
        if not synth:
            print(f"Модель синтеза {cfg.synth_model} не найдена — синтез на {cfg.model}.")
        cfg.synth_model = synth or ""
    emb = pick(cfg.embed_model, "Эмбеддинг-модель")
    if not emb and need_embed:
        raise SystemExit(f"Эмбеддинг-модель {cfg.embed_model} не найдена в {cfg.ollama}. Установи: ollama pull embeddinggemma")
    if emb:
        cfg.embed_model = emb
    return llm


def header(cfg: Config) -> None:
    print(f"Local Coach v{VERSION}")
    if cfg.vault:
        print(f"Vault (только чтение): {cfg.vault}")
    print(f"Рабочая папка:          {cfg.workspace}")
    print(f"Ollama:                 {cfg.ollama} — {cfg.model}, векторы: {cfg.embed_model}"
          + (f", тяжёлая: {cfg.heavy_model}" if cfg.heavy_model else "")
          + (f", синтез: {cfg.synth_model}" if cfg.synth_model else ""))
    print(f"Контекст:               {cfg.num_ctx} токенов, numpy: {'есть' if vectors.np is not None else 'нет (медленнее)'}")
    print()


def scan(cfg: Config) -> list[vault.Note]:
    t = time.monotonic()
    notes = vault.scan(cfg.vault, cfg)
    held = sum(1 for n in notes if n.hold_reason)
    chunks = sum(len(n.chunks) for n in notes)
    sources = {}
    for n in notes:
        sources[n.date_source] = sources.get(n.date_source, 0) + 1
    print(f"Заметок: {len(notes)}, объём {sum(n.size for n in notes) / 1048576:.1f} МБ, фрагментов: {chunks} "
          f"(прочитано за {fmt_duration(time.monotonic() - t)})")
    print(f"Отложено фильтром секретов: {held} — их текст модели не отправляется")
    print("Даты заметок: " + ", ".join(f"{_DATE_NAMES.get(k, k)} {v}" for k, v in sorted(sources.items())))
    return notes


_DATE_NAMES = {"frontmatter": "из шапки", "filename": "из имени файла", "mtime": "по mtime",
               "mtime_bulk": "неизвестна (массовый mtime)", "import": "импорт (дата из текста)"}


# --- команды ------------------------------------------------------------------------

def run_pipeline(cfg: Config, llm: Ollama, *, synth_stage: bool = True, import_v3: str | None = None) -> int:
    """Весь конвейер обновления памяти. Возвращает число неизвлечённых фрагментов."""
    started = time.monotonic()
    notes = scan(cfg)
    if not notes:
        raise SystemExit("В vault нет Markdown-заметок.")
    conn = store.connect(cfg.db_path)
    try:
        print("\n1/8 Синхронизация с vault")
        s = extract.sync_notes(conn, notes, cfg)
        print(f"  новых заметок {s['new']}, изменённых {s['changed']}, удалённых {s['stale']}, отложено {s['held']}; "
              f"фрагменты: новых {s['chunks_new']}, без изменений {s['chunks_reused']}, убрано {s['chunks_dropped']}")
        if import_v3:
            path = Path(import_v3).expanduser()
            if not path.exists():
                raise SystemExit(f"База v3 не найдена: {path}")
            st = extract.import_v3(conn, path, notes, cfg)
            print(f"  импорт v3: заметок {st['notes']}, фактов {st['facts']}; пропущено изменённых "
                  f"{st['skipped_changed']}, недоделанных в v3 {st['skipped_incomplete']}")

        resplit = extract.resplit_saturated(conn, cfg)
        if resplit:
            print(f"  упёрлись в потолок фактов: {resplit} фрагм. — будут разобраны заново по частям (один раз)")
        flagged = extract.refresh_project_flags(conn, cfg)
        if flagged:
            print(f"  пометка «проект» обновлена у {flagged} фактов (папки проектов и идей — не хронология)")
        repaired = extract.repair_dates(conn)
        if repaired:
            print(f"  снято дат, которых нет в тексте (дата изменения файла, число вроде «2000 оборотов»): {repaired}")
        chrono = timeline.chronology_paths(conn)
        if chrono:
            print(f"  дневников и хронологий в vault: {len(chrono)} — их даты станут опорой наравне с биографией")

        print("\n2/8 Извлечение фактов")
        ok, failed = extract.extract_pending(conn, llm, cfg)
        if ok or failed:
            print(f"  готово {ok}, ошибок {failed}")

        print("\n3/8 Векторы")
        vectors.ensure_embed_identity(conn, cfg)
        vec_ok = vectors.embed_missing(conn, llm, cfg)
        marks = vectors.apply_user_marks(conn, cfg)
        if marks["rejected"] or marks["confirmed"]:
            print(f"  по твоим прошлым решениям: отвергнуто {marks['rejected']}, подтверждено {marks['confirmed']}")

        print("\n4/8 Склейка похожих фактов")
        cs = vectors.update_clusters(conn, cfg)
        print(f"  присоединено {cs['assigned']}, новых кластеров {cs['created']}, пересчитано {cs['touched']}, "
              f"удалено пустых {cs['removed']}")

        print("\n5/8 Актуальность: что верно сейчас, что в прошлом")
        tl = timeline.update_currency(conn, llm if vec_ok else None, cfg)
        c = tl["counts"]
        print(f"  сейчас {c.get('current', 0)}, в прошлом {c.get('past', 0)}, события {c.get('history', 0)}, "
              f"не проверено {c.get('unknown', 0)}")
        if not tl["timeline"]:
            print("  биографии нет — сверять не с чем (её можно добавить: python3 coach.py import файл)")
        elif tl["resolved"].get("past") or tl["resolved"].get("current"):
            print(f"  сверено с биографией и твоими ответами: в прошлом {tl['resolved']['past']}, "
                  f"актуально {tl['resolved']['current']}")
        if tl["questions"]:
            print(f"  вопросов к тебе: {tl['questions']} — python3 coach.py calibrate (или /calibrate в чате)")

        print("\n6/8 Люди: кто упоминается в заметках")
        ps = people.update(conn, llm if vec_ok else None, cfg)
        pc = people.counts(conn)
        print(f"  просмотрено фактов {ps['facts']}; людей {pc['people']}, из них уточнено тобой {pc['known']}")
        if pc["questions"]:
            print(f"  вопросов про людей: {pc['questions']} — python3 coach.py people (или /people в чате)")

        domains: dict = {}
        profile = None
        if not synth_stage:
            print("\n7/8 Синтез пропущен — профиль обновится при следующем update")
            profile = store.cache_any(conn, "profile", "final")
        elif not vec_ok:
            print("\n7/8 Синтез пропущен: векторы не готовы, повтори запуск, когда эмбеддинг-модель доступна")
        else:
            print("\n7/8 Обзоры областей, профиль, аудит")
            domains, profile = rebuild_profile(conn, llm, cfg)
        if not domains:
            domains = {r["key"]: store.loads(r["payload_json"], {}) for r in
                       conn.execute("SELECT key, payload_json FROM artifacts WHERE stage='domain'")}

        if vec_ok and store.get_meta(conn, "lr_built_at"):
            # Реестр Life Reset уже начат: дособрать новое (готовые группы берутся из кэша) и отметить,
            # что всплыло снова после решения.
            from . import hypotheses, inventory, lifereset
            try:
                st = inventory.build(conn, llm, cfg, quiet=True)
                hy = hypotheses.build(conn, llm, cfg, quiet=True)
                print(f"\n  Life Reset: новых пунктов в реестре {st['new']}, снова всплыло {st['resurfaced']}, "
                      f"новых гипотез {hy['new']}")
                lifereset.export(conn, cfg, quiet=True)
            except OllamaError as exc:
                print(f"\n  Life Reset: реестр не обновлён ({exc}); обновится в следующий раз")

        print("\n8/8 Файлы памяти")
        store.set_meta(conn, "last_update", iso_now())
        conn.commit()
        report.write_all(conn, cfg, domains, profile)
        people.write_md(conn, cfg.memory_dir / "people.md")
        print()
        for line in report.status_lines(conn):
            print("  " + line)
        print(f"\nГотово за {fmt_duration(time.monotonic() - started)}. Профиль: {cfg.memory_dir / 'profile.md'}")
        left = store.scalar(conn, "SELECT COUNT(*) FROM chunks WHERE status IN ('pending','error')") or 0
        if left:
            print(f"Не извлечено фрагментов: {left} — повторный запуск продолжит с места остановки.")
        return left
    finally:
        conn.close()


def rebuild_profile(conn, llm: Ollama, cfg: Config) -> tuple[dict, dict | None]:
    domains = synth.synthesize_domains(conn, llm, cfg)
    draft = synth.build_profile(conn, llm, cfg, domains)
    return domains, (synth.audit_profile(conn, llm, cfg, draft) if draft else None)


def cmd_update(cfg: Config, args: argparse.Namespace) -> int:
    header(cfg)
    llm = connect_llm(cfg)
    with update_lock(cfg):
        left = run_pipeline(cfg, llm, synth_stage=not args.skip_synth, import_v3=args.import_v3)
    print("Разговор: python3 coach.py chat")
    return 0 if not left else 2


def import_and_ingest(cfg: Config, llm: Ollama, files: list[str]) -> int:
    """Импортировать файлы и сразу разобрать их (без пересборки профиля)."""
    from .importer import import_file
    done = 0
    for f in files:
        try:
            target = import_file(cfg, Path(f).expanduser())
        except PermissionError:
            print(f"Не удалось импортировать {f}: нет доступа на чтение. Если файл на сетевой шаре, "
                  "проверь на ПК: файл не открыт в Excel/Word, не зашифрован (Свойства → Другие → "
                  "«Шифровать содержимое»), не хранится только в облаке OneDrive и имеет право «Чтение» "
                  "в «Безопасности». Или скопируй файл на сервер.")
            continue
        except (ValueError, OSError, KeyError) as exc:
            # KeyError — повреждённый или не тот zip внутри .xlsx/.docx
            print(f"Не удалось импортировать {f}: {exc}")
            continue
        print(f"Импортировано: {f} → {target}")
        done += 1
    if done:
        with update_lock(cfg):
            run_pipeline(cfg, llm, synth_stage=False)
    return done


def cmd_import(cfg: Config, args: argparse.Namespace) -> int:
    header(cfg)
    done = import_and_ingest(cfg, connect_llm(cfg), args.files)
    if done:
        print("Факты из файла уже доступны в разговоре. Профиль учтёт их при следующем python3 coach.py update.")
    return 0 if done else 2


def cmd_plan(cfg: Config, args: argparse.Namespace) -> int:
    header(cfg)
    notes = scan(cfg)
    resplit: list = []
    if cfg.db_path.exists():
        conn = store.connect(cfg.db_path)
        known = {r[0] for r in conn.execute("SELECT sha256 FROM chunks WHERE status IN ('done','imported')")}
        resplit = [r for r in extract.saturated_chunks(conn, cfg) if r["sha256"] in known]
        known -= {r["sha256"] for r in resplit}
        conn.close()
        pending = [c for n in notes for c in n.chunks if extract.chunk_key(n, c) not in known]
        resplit_keys = {r["sha256"] for r in resplit}
        dense = [c for n in notes for c in n.chunks if extract.chunk_key(n, c) in resplit_keys]
        pending = [c for c in pending if c not in dense]
        print(f"Фрагментов потребуется извлечь: {len(pending) + len(dense)} (остальные уже в базе)")
        if dense:
            print(f"  из них упёрлись в потолок фактов и будут разобраны по частям: {len(dense)} "
                  "(каждый — от 3 вызовов модели)")
    else:
        pending, dense = [c for n in notes for c in n.chunks], []
        print(f"База ещё не создана: извлекать придётся все {len(pending)} фрагментов.")
    todo = len(pending) + len(dense)
    chars = sum(len(c.text) + extract.CALL_OVERHEAD_CHARS for c in pending)
    # Плотный фрагмент: целиком, затем две половины (≈ ещё один объём), часто и четверти.
    dense_lo = sum(2 * len(c.text) + 3 * extract.CALL_OVERHEAD_CHARS for c in dense)
    dense_hi = sum(3 * len(c.text) + 7 * extract.CALL_OVERHEAD_CHARS for c in dense)
    conn = store.connect(cfg.db_path) if cfg.db_path.exists() else None
    per_kchar = float(store.get_meta(conn, "sec_per_kchar", "0") or 0) if conn else 0
    rate = float(store.get_meta(conn, "sec_per_chunk", "0") or 0) if conn else 0
    if conn:
        conn.close()
    if per_kchar:
        lo, hi = per_kchar * (chars + dense_lo) / 1000, per_kchar * (chars + dense_hi) / 1000
        span = f"~{fmt_duration(lo)}" if fmt_duration(lo) == fmt_duration(hi) else f"~{fmt_duration(lo)} – {fmt_duration(hi)}"
        print(f"Оценка по прошлому запуску: {span}")
    elif rate:
        print(f"Оценка по прошлому запуску: ~{fmt_duration(rate * (len(pending) + 3 * len(dense)))} "
              f"({rate:.0f} с на фрагмент)")
    else:
        print("Скорость ещё не измерена: первый запуск покажет оценку после нескольких фрагментов.")
    print("\nПлановый режим: модель не вызывалась, база не менялась.")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.db_path.exists():
        print(f"База ещё не создана: {cfg.db_path}")
        return 1
    conn = store.connect(cfg.db_path)
    for line in report.status_lines(conn):
        print(line)
    errors = conn.execute("SELECT error, COUNT(*) AS n FROM chunks WHERE status='error' GROUP BY error "
                          "ORDER BY n DESC LIMIT 5").fetchall()
    if errors:
        print("Частые ошибки извлечения (без содержимого заметок):")
        for e in errors:
            print(f"  {e['n']} × {e['error']}")
    conn.close()
    return 0


def cmd_bio(cfg: Config, args: argparse.Namespace) -> int:
    from . import history
    if not cfg.db_path.exists():
        print(f"База ещё не создана: {cfg.db_path}")
        return 1
    conn = store.connect(cfg.db_path)
    print(history.show(conn, " ".join(args.period), full=args.all))
    conn.close()
    return 0


def cmd_chat(cfg: Config, args: argparse.Namespace) -> int:
    from .chat import chat_loop
    return chat_loop(cfg, connect_llm(cfg, fallback=True))


def _lr_conn(cfg: Config) -> sqlite3.Connection | None:
    if not cfg.db_path.exists():
        print("База ещё не построена. Сначала: python3 coach.py update")
        return None
    return store.connect(cfg.db_path)


def cmd_inventory(cfg: Config, args: argparse.Namespace) -> int:
    from . import hypotheses, inventory, lifereset
    conn = _lr_conn(cfg)
    if conn is None:
        return 2
    try:
        if args.action == "list":
            inventory.print_list(conn, args.status)
            return 0
        if args.action == "plan":
            inventory.print_plan(conn)
            return 0
        if args.action == "dedupe":
            n = inventory.dedupe(conn)
            print(f"Склеено пунктов с одинаковым названием: {n}." if n else "Одинаковых названий среди пунктов нет.")
            if n:
                print("Источники и цитаты перешли в оставшийся пункт; склеенные скрыты, не удалены.")
                lifereset.export(conn, cfg)
            print()
            return 0
        if args.action == "reopen":
            iid = inventory.reopen(conn, args.ref or "")
            print(f"{inventory.code(iid)} снова «не разобрано» – он в очереди разбора.\n" if iid
                  else "Укажи пункт: inventory reopen LR-12\n")
            if iid:
                lifereset.export(conn, cfg)
            return 0
        llm = connect_llm(cfg, fallback=True)
        if cfg.on_fallback:
            print("ПК выключен: сборка пойдёт на процессоре сервера и займёт заметно дольше. Прервать можно в любой "
                  "момент (Ctrl+C) – готовое сохранится.")
        with update_lock(cfg):
            st = inventory.build(conn, llm, cfg)
            hy = hypotheses.build(conn, llm, cfg)
        print(f"Групп в памяти: {st['groups']}, названо моделью сейчас: {st['named']}, не пункты (просто сведения): "
              f"{st['skipped']}, склеено как одна тема: {st['joined']}, с одинаковым названием: {st['deduped']}.\nВ реестре: новых {st['new']}, обновлено {st['updated']}, снова всплыло после решения "
              f"{st['resurfaced']}. Гипотез: новых {hy['new']}, новых наблюдений к известным {hy['observations']}.\n")
        lifereset.export(conn, cfg)
        print("\nДальше – разбор по одному: python3 coach.py triage (или /triage в чате).\n")
    except SystemExit as exc:
        print(exc)
        return 2
    finally:
        conn.close()
    return 0


def cmd_triage(cfg: Config, args: argparse.Namespace) -> int:
    from . import inventory, lifereset
    from .config import LR_TYPES
    conn = _lr_conn(cfg)
    if conn is None:
        return 2
    if args.type and args.type not in LR_TYPES:
        print("Тип – один из: " + ", ".join(LR_TYPES))
        return 2
    area = inventory.resolve_area(args.area) if args.area else None
    if args.area and area is None:
        print("Область не узнал. Ждут решения по областям: "
              + ", ".join(f"{a} – {n}" for a, n in inventory.area_counts(conn))
              + ".\nМожно по-русски и началом слова: --area финансы, --area работа.")
        conn.close()
        return 2
    try:
        llm = connect_llm(cfg, fallback=True)
        if args.quick:
            inventory.quick_triage(conn, llm, cfg, args.type, area)
        else:
            inventory.triage(conn, llm, cfg, args.limit, args.type, area)
        lifereset.export(conn, cfg)
    finally:
        conn.close()
    return 0


def cmd_sweep(cfg: Config, args: argparse.Namespace) -> int:
    from . import inventory, lifereset
    conn = _lr_conn(cfg)
    if conn is None:
        return 2
    try:
        llm = None
        if not args.no_pick:
            try:
                llm = connect_llm(cfg, fallback=True)
            except (SystemExit, OllamaError) as exc:
                print(f"Модель недоступна ({exc}) – покажу списком без отбора.\n")
        inventory.sweep(conn, llm, cfg, args.target)
        lifereset.export(conn, cfg)
    finally:
        conn.close()
    return 0


def cmd_audit(cfg: Config, args: argparse.Namespace) -> int:
    from . import audit, lifereset
    conn = _lr_conn(cfg)
    if conn is None:
        return 2
    try:
        if args.show:
            print(audit.summary_text(conn))
            return 0
        audit.run(conn, [a.strip() for a in args.areas.split(",")] if args.areas else None)
        lifereset.export(conn, cfg)
    finally:
        conn.close()
    return 0


def cmd_hypotheses(cfg: Config, args: argparse.Namespace) -> int:
    from . import hypotheses, lifereset
    conn = _lr_conn(cfg)
    if conn is None:
        return 2
    try:
        if args.action == "build":
            st = hypotheses.build(conn, connect_llm(cfg, fallback=True), cfg)
            print(f"Новых гипотез: {st['new']}, новых наблюдений к известным: {st['observations']}.\n")
        else:
            hypotheses.review(conn, args.limit)
        lifereset.export(conn, cfg)
    finally:
        conn.close()
    return 0


def cmd_reset(cfg: Config, args: argparse.Namespace) -> int:
    from . import lifereset
    conn = _lr_conn(cfg)
    if conn is None:
        return 2
    try:
        words = args.args or []
        if words[:1] == ["stage"] and len(words) > 1:
            key = lifereset.set_stage(conn, words[1])
            print(f"Этап: {lifereset.stage_title(key)}\n" if key else "Этап – число от 1 до 6 или «авто».\n")
        elif words[:1] == ["depth"] and len(words) > 1:
            ok = words[1].isdigit() and lifereset.set_depth(conn, int(words[1]))
            print(f"Глубина разговора: {words[1]}\n" if ok else "Глубина – 1, 2 или 3.\n")
        elif words[:1] == ["export"]:
            lifereset.export(conn, cfg)
            return 0
        lifereset.dashboard(conn)
    finally:
        conn.close()
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from .web import serve
    host = args.host or os.environ.get("LOCAL_COACH_WEB_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("LOCAL_COACH_WEB_PORT", "3012"))
    return serve(lambda: build_config(args), lambda c: connect_llm(c, fallback=True), host, port)


def cmd_calibrate(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.db_path.exists():
        print("База ещё не построена. Сначала: python3 coach.py update")
        return 2
    llm = connect_llm(cfg)
    with update_lock(cfg):
        conn = store.connect(cfg.db_path)
        try:
            n = timeline.calibrate(conn, llm, cfg, limit=args.limit, include_skipped=args.all)
            if n and sys.stdin.isatty():
                try:
                    ans = input("\nПересобрать профиль с учётом ответов сейчас? Это несколько минут. (д/н) ").strip().casefold()
                except (EOFError, KeyboardInterrupt):
                    ans = ""
                if ans.startswith("д"):
                    domains, profile = rebuild_profile(conn, llm, cfg)
                    report.write_all(conn, cfg, domains, profile)
                    print(f"Профиль обновлён: {cfg.memory_dir / 'profile.md'}")
                else:
                    print("Профиль учтёт ответы при следующем python3 coach.py update.")
        finally:
            conn.close()
    return 0


def cmd_eval(cfg: Config, args: argparse.Namespace) -> int:
    from . import evaluate
    if not cfg.db_path.exists():
        print("База ещё не построена. Сначала: python3 coach.py update")
        return 2
    conn = store.connect(cfg.db_path)
    try:
        if args.report:
            print("\n".join(evaluate.report_lines(conn)))
            return 0
        # Ollama нужен только для исправлений по ходу (векторы отметок); без него они досчитаются при update.
        llm = Ollama(cfg)
        with update_lock(cfg):
            evaluate.run(conn, llm, cfg, n=args.n, only=args.only.split(",") if args.only else None)
    finally:
        conn.close()
    return 0


def cmd_agreements(cfg: Config, args: argparse.Namespace) -> int:
    from . import commitments
    conn = store.connect(cfg.db_path)
    try:
        commitments.migrate_legacy(conn)
        commitments.manage(conn)
    finally:
        conn.close()
    return 0


def cmd_checkin(cfg: Config, args: argparse.Namespace) -> int:
    from . import checkin
    conn = store.connect(cfg.db_path)
    try:
        checkin.run(conn, connect_llm(cfg, fallback=True), cfg)
    finally:
        conn.close()
    return 0


def cmd_people(cfg: Config, args: argparse.Namespace) -> int:
    conn = store.connect(cfg.db_path)
    try:
        if args.list:
            people.print_list(conn)
        else:
            people.ask(conn, args.limit, args.all)
            people.write_md(conn, cfg.memory_dir / "people.md")
    finally:
        conn.close()
    return 0


def cmd_review(cfg: Config, args: argparse.Namespace) -> int:
    from .chat import review
    conn = store.connect(cfg.db_path)
    try:
        review(conn, connect_llm(cfg, fallback=True), cfg)
    finally:
        conn.close()
    return 0


def cmd_forget(cfg: Config, args: argparse.Namespace) -> int:
    from .chat import forget
    conn = store.connect(cfg.db_path)
    try:
        forget(conn, connect_llm(cfg, fallback=True), cfg, " ".join(args.text))
    finally:
        conn.close()
    return 0


def cmd_reextract(cfg: Config, args: argparse.Namespace) -> int:
    with update_lock(cfg):
        conn = store.connect(cfg.db_path)
        n = extract.reset_extraction(conn, args.path)
        conn.close()
    print(f"Помечено к повторному извлечению: {n} фрагментов. Запусти: python3 coach.py update")
    return 0


def cmd_recluster(cfg: Config, args: argparse.Namespace) -> int:
    with update_lock(cfg):
        conn = store.connect(cfg.db_path)
        conn.execute("UPDATE facts SET cluster_id=NULL")
        conn.execute("DELETE FROM clusters")
        store.set_meta(conn, "dirty_clusters", "[]")
        conn.commit()
        cs = vectors.update_clusters(conn, cfg)
        timeline.refresh_currency(conn)
        report.write_cluster_review(conn, cfg)
        conn.close()
    print(f"Кластеров: {cs['created']}. Проверь {cfg.memory_dir / 'clusters_review.md'}, затем python3 coach.py update")
    return 0


def cmd_sanitize(cfg: Config, args: argparse.Namespace) -> int:
    """Перепроверить vault фильтром секретов и вычистить производные данные. Модель не вызывается."""
    with update_lock(cfg):
        notes = scan(cfg)
        conn = store.connect(cfg.db_path)
        extract.sync_notes(conn, notes, cfg)
        removed = 0
        for table, cols in (("facts", "claim || ' ' || evidence_quote"), ("hypotheses_raw", "hypothesis || ' ' || evidence_quote"),
                            ("candidates", "claim || ' ' || reason"), ("turns", "content"),
                            ("sessions", "summary || ' ' || agreements_json")):
            for row in conn.execute(f"SELECT id, {cols} AS t FROM {table}").fetchall():
                if guard.text_hold(row["t"] or ""):
                    if table == "facts":
                        store.delete_facts(conn, "id=?", (row["id"],))
                    else:
                        conn.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
                    removed += 1
        # Синтезы могли пересказать удалённое — строятся заново при следующем update.
        conn.execute("DELETE FROM artifacts")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.close()
    if cfg.memory_dir.exists():
        shutil.rmtree(cfg.memory_dir)
    print(f"Удалено записей с признаками секретов: {removed}. Сгенерированная память очищена; "
          "заметки не изменялись, модель не вызывалась. Пересобрать: python3 coach.py update")
    return 0


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    conn = store.connect(cfg.db_path)
    domains = {r["key"]: store.loads(r["payload_json"], {}) for r in
               conn.execute("SELECT key, payload_json FROM artifacts WHERE stage='domain'")}
    report.write_all(conn, cfg, domains, store.cache_any(conn, "profile", "final"))
    conn.close()
    print(f"Файлы памяти обновлены: {cfg.memory_dir}")
    return 0


def cmd_name(cfg: Config, args: argparse.Namespace) -> int:
    conn = store.connect(cfg.db_path)
    if args.value:
        value = "" if args.value == ["-"] else " ".join(args.value).strip()
        store.set_meta(conn, "user_name", value)
        conn.commit()
        print(f"Имя в профиле: {value}." if value else "Имя убрано: в профиле — «он».")
        print("Профиль учтёт это при следующем python3 coach.py update.")
    else:
        current = cfg.user_name or store.get_meta(conn, "user_name") or ""
        print(f"Имя в профиле: {current}" if current else "Имя не задано: в профиле — «он». "
              "Задать: python3 coach.py name Имя")
    conn.close()
    return 0


COMMANDS = {
    "update": (cmd_update, "обновить память: новые и изменённые заметки → факты → профиль (по умолчанию)"),
    "import": (cmd_import, "добавить файл в память: .xlsx, .csv, .docx, .txt, .md (vault не меняется)"),
    "plan": (cmd_plan, "посчитать объём работы без вызова модели"),
    "status": (cmd_status, "состояние базы"),
    "chat": (cmd_chat, "разговор с коучем"),
    "serve": (cmd_serve, "веб-интерфейс с котом Базилием: коуч работает постоянно (PIN в LOCAL_COACH_WEB_PIN)"),
    "bio": (cmd_bio, "хронология жизни по годам из базы, без модели: bio 1988-2026; --all — всё подряд"),
    "history": (cmd_bio, "прежнее имя команды bio"),
    "calibrate": (cmd_calibrate, "что из старых заметок верно сейчас: сверка с биографией и вопросы тебе"),
    "eval": (cmd_eval, "оценить качество памяти и ответов; --report — только числа, можно пересылать"),
    "checkin": (cmd_checkin, "чек-ин: три коротких вопроса о том, что сейчас — коуч узнаёт настоящее"),
    "people": (cmd_people, "люди из заметок: кто это, есть ли сейчас в жизни; --list — список"),
    "agreements": (cmd_agreements, "договорённости из разговоров: посмотреть, отметить сделанное, добавить"),
    "review": (cmd_review, "разобрать предложения в память и вопросы аудита"),
    "forget": (cmd_forget, "найти и забыть неверное утверждение"),
    "reextract": (cmd_reextract, "пометить фрагменты для повторного извлечения (после правки промптов)"),
    "recluster": (cmd_recluster, "пересобрать кластеры (после смены порога)"),
    "sanitize": (cmd_sanitize, "перепроверить фильтр секретов и очистить производные данные"),
    "name": (cmd_name, "как называть тебя в профиле: name Имя; без имени — показать; name - — убрать"),
    "export": (cmd_export, "пересоздать файлы памяти из базы без вызова модели"),
    "reset": (cmd_reset, "Life Reset: обзор программы и следующий шаг; reset stage N, reset depth N, reset export"),
    "inventory": (cmd_inventory, "Life Reset: собрать реестр накопленного из памяти; inventory plan – сколько работы (без модели); "
                               "inventory list [--status S]; inventory dedupe – склеить одинаковые названия"),
    "triage": (cmd_triage, "Life Reset: разобрать реестр по одному пункту (--limit N, --type project, --area финансы); "
                         "--quick – быстро списком"),
    "sweep": (cmd_sweep, "Life Reset: одиночные упоминания пачкой – отметь живое, остальное в архив"),
    "audit": (cmd_audit, "Life Reset: аудит жизни по 12 областям, по строке на область; --show – итог"),
    "hypotheses": (cmd_hypotheses, "Life Reset: проверить гипотезы о повторяющихся сценариях; hypotheses build"),
}
NEEDS_VAULT = {"update", "plan", "sanitize", "import", "chat", "reset", "inventory", "triage", "sweep", "audit",
               "hypotheses"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coach.py", description="Local Coach 5: локальная память, коучинг и Life Reset по заметкам Obsidian.")
    parser.add_argument("--vault", help="путь к vault Obsidian или папке с .md (или переменная LOCAL_COACH_VAULT)")
    parser.add_argument("--workspace", help=f"папка данных коуча (по умолчанию {Config().workspace})")
    parser.add_argument("--ollama", help="адрес Ollama")
    parser.add_argument("--ollama-fallback", help="запасной Ollama, когда ПК выключен (например модель на другой машине;"
                                                  " по умолчанию нет): chat, checkin, review, forget")
    parser.add_argument("--model", help="модель анализа и разговора")
    parser.add_argument("--embedding-model", help="модель эмбеддингов")
    parser.add_argument("--heavy-model", help="тяжёлая модель для извлечения фактов, синтеза, реестра, гипотез и людей "
                                              "(по умолчанию – как --model; чат остаётся на --model)")
    parser.add_argument("--synth-model", help="модель для профиля и сверки с биографией (по умолчанию — как --model)")
    sub = parser.add_subparsers(dest="command")
    for name, (_fn, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        if name == "update":
            p.add_argument("--import-v3", metavar="DB", help="перенести готовые извлечения из базы v3 "
                                                              "(устаревшая версия коуча)")
            p.add_argument("--skip-synth", action="store_true", help="только извлечение и векторы, без синтеза")
        if name in ("bio", "history"):
            p.add_argument("period", nargs="*", help="год или период: 2005, 1988-2026, с 2010")
            p.add_argument("--all", action="store_true", help="всё подряд, как в базе: и проектное, и не-события, "
                                                             "и пересказы биографии")
        if name == "forget":
            p.add_argument("text", nargs="+")
        if name == "import":
            p.add_argument("files", nargs="+", metavar="ФАЙЛ")
        if name == "calibrate":
            p.add_argument("--limit", type=int, default=25, help="сколько вопросов за раз (по умолчанию 25)")
            p.add_argument("--all", action="store_true", help="спросить и про пропущенные раньше")
        if name == "eval":
            p.add_argument("--report", action="store_true", help="только отчёт: числа без содержимого")
            p.add_argument("--n", type=int, default=10, help="пунктов на часть (по умолчанию 10)")
            p.add_argument("--only", help="только части: currency,fact,profile,reply")
        if name == "people":
            p.add_argument("--list", action="store_true", help="только список, без вопросов")
            p.add_argument("--limit", type=int, default=15, help="сколько вопросов за раз (по умолчанию 15)")
            p.add_argument("--all", action="store_true", help="спросить и про пропущенных раньше")
        if name == "serve":
            p.add_argument("--host", help="адрес (по умолчанию 127.0.0.1; в сеть — 0.0.0.0 и обязательно PIN)")
            p.add_argument("--port", type=int, help="порт (по умолчанию 3012)")
        if name == "name":
            p.add_argument("value", nargs="*", help="имя; «-» — убрать")
        if name == "inventory":
            p.add_argument("action", nargs="?", choices=["build", "list", "plan", "reopen", "dedupe"], default="build")
            p.add_argument("ref", nargs="?", help="для reopen: номер пункта, LR-12")
            p.add_argument("--status", help="для list: только этот статус (active, someday, inbox…)")
        if name == "triage":
            p.add_argument("--limit", type=int, default=10, help="сколько пунктов за раз (по умолчанию 10)")
            p.add_argument("--type", help="только этот тип: project, idea, wish, commitment, question…")
            p.add_argument("--area", help="только эта область жизни: финансы, работа, здоровье… (или ключ: finance)")
            p.add_argument("--quick", action="store_true", help="быстро списком: по 10 строк, «1а 2к 3о», «4?» – карточкой")
        if name == "sweep":
            p.add_argument("--target", type=int, default=130, help="сколько самых значимых отобрать моделью "
                                                                   "по всем областям (по умолчанию 130)")
            p.add_argument("--no-pick", action="store_true", help="без отбора моделью – все подряд по заметкам")
        if name == "audit":
            p.add_argument("--areas", help="только эти области через запятую: health,career,finance…")
            p.add_argument("--show", action="store_true", help="показать итог последнего раунда")
        if name == "hypotheses":
            p.add_argument("action", nargs="?", choices=["build", "review"], default="review")
            p.add_argument("--limit", type=int, default=5, help="сколько за раз (по умолчанию 5)")
        if name == "reset":
            p.add_argument("args", nargs="*", help="stage N | stage авто | depth N | export")
        if name == "reextract":
            p.add_argument("--path", help="только заметки, путь которых начинается с этого")
    args = parser.parse_args(argv)
    if not args.command:
        args.command = "update"
        args.import_v3, args.skip_synth = None, False
    try:
        cfg = build_config(args)
        return COMMANDS[args.command][0](cfg, args)
    except KeyboardInterrupt:
        print("\nОстановлено. Всё готовое сохранено; повторный запуск продолжит с места остановки.")
        return 130
    except FileNotFoundError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 2
    except OllamaError as exc:
        print(f"\nОШИБКА Ollama: {exc}\nПовторный запуск продолжит с места остановки.", file=sys.stderr)
        return 2
